# -*- coding: utf-8 -*-
r'''
DotTTS 引擎 —— audiocpp_server 常驻子进程（audio.cpp / Vulkan GPU）

- 模型：DotTTS SOAR Q8_0（GGUF，本地离线，E:\dearpebble\models\dotstts）
- 运行时：audio.cpp 的 audiocpp_server.exe（OpenAI 兼容 HTTP，Vulkan 后端）
- 克隆：voice_dir 声音库（与 OmniVoice / Fish Audio 共用——参考音频放
  audio-cpp/voice/，prompt_text 映射转写文本，服务端自动注入 reference_text，
  prompt prefill 提升克隆相似度）
- 语言标签：language（空/none=不注入；en/zh 等注入 [XX] 前缀锁定目标语言发音）
- 推理步数：num_inference_steps（10=默认；20/32 质量档，步数越多越慢越稳）
- 语速：不支持（引擎对未知 request option 严格报错，speed 会被拒绝）
- 指令：instruction_tts 模板存在但指令格式未发布（自然语言指令实测不可靠：
  指令文本被当作正文朗读），不对外暴露
- 输出：48 kHz mono PCM（流式）/ WAV（整段）
- 生命周期：懒启动 -> 常驻 -> atexit 清理；端口复用防孤儿进程；崩溃自动重启
'''
import asyncio
import atexit
import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path


from .paths import (
    ENGINE_STATE_DIR,
    VOICE_DIR as SHARED_VOICE_DIR,
    audio_cpp_dir,
    model_path,
    resolve_user_path,
)
import httpx

logger = logging.getLogger('dotstts')

BASE_DIR = Path(__file__).resolve().parent
AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / 'audiocpp_server.exe'
CONFIG_FILE = ENGINE_STATE_DIR / 'server_dotstts.json'
VOICE_DIR = SHARED_VOICE_DIR
SERVER_LOG = ENGINE_STATE_DIR / 'server_dotstts_runtime.log'
_override_model_path: str = ''
_override_backend: str = ''


def set_override(model_path_str: str = '', compute_backend: str = '') -> None:
    global _override_model_path, _override_backend
    _override_model_path = (model_path_str or '').strip()
    _override_backend = (compute_backend or '').strip().lower()


def _model_path() -> Path:
    """Resolved on each call so a change made in the UI applies on next start."""
    if _override_model_path:
        p = resolve_user_path(_override_model_path)
        if p is not None:
            return p
    return model_path('dotstts', 'dots-tts-soar-q8_0.gguf')

MODEL_ID = 'dots-tts'
SERVER_PORT = 18245
SAMPLE_RATE = 48000

READY_TIMEOUT = 180.0
SYNTH_TIMEOUT = 300.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
STREAM_READ_TIMEOUT = 120.0        # 流式读取块间超时（单块合成最坏十几秒，冗余留足）
IDLE_UNLOAD_MS = 120000            # 闲置 2 分钟自动卸载模型释放显存（下次请求自动重载）
MIN_FREE_MEMORY_MB = 128           # 加载前预检 headroom（估算含 1.5x 运行时系数，128 足够；过大误挡边缘场景）
RELEASE_TIMEOUT = 15.0             # 切换释放（unload_all_models）请求超时


class DotTtsRequestError(RuntimeError):
    '''请求类错误（4xx）——不触发服务重启'''


class DotTtsInsufficientMemoryError(RuntimeError):
    '''内存/显存不足（503 insufficient_memory）——重启无法解决，不触发重启重试'''


def _server_config() -> dict:
    '''audiocpp_server 配置：voice_dir 声音库 + dots_tts 模型。

    mode=streaming：流式请求走 chunked PCM 推送（生成中逐块产出），
    非流式请求（/tts 整段 WAV）行为不变。
    '''
    return {
        'host': '127.0.0.1',
        'port': SERVER_PORT,
        'backend': 'cpu' if _override_backend == 'cpu' else 'vulkan',
        'device': 0,
        'threads': 4,
        'lazy_load': True,
        'idle_unload_ms': IDLE_UNLOAD_MS,
        'min_free_memory_mb': MIN_FREE_MEMORY_MB,
        'voice_dir': str(VOICE_DIR),
        'models': [
            {
                'id': MODEL_ID,
                'family': 'dots_tts',
                'path': str(_model_path()),
                'task': 'tts',
                'mode': 'streaming',
            }
        ],
    }


class DotTtsEngine:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = threading.RLock()
            cls._instance._proc = None
            cls._instance._ready = False
        return cls._instance

    # ---- 生命周期 ----
    def _base_url(self):
        return f'http://127.0.0.1:{SERVER_PORT}'

    def _healthy(self):
        try:
            r = httpx.get(f'{self._base_url()}/health', timeout=HEALTH_TIMEOUT, trust_env=False)
            return r.status_code == 200
        except Exception:
            return False

    def _port_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', SERVER_PORT))
                return True
            except OSError:
                return False

    def _write_config(self) -> bool:
        '''写配置文件；返回内容是否发生变更（False=磁盘已是目标内容）。'''
        text = json.dumps(_server_config(), indent=2, ensure_ascii=False)
        try:
            if CONFIG_FILE.read_text(encoding='utf-8') == text:
                return False
        except Exception:
            pass
        CONFIG_FILE.write_text(text, encoding='utf-8')
        return True

    def _rotate_log(self):
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > LOG_MAX_BYTES:
                bak = SERVER_LOG.with_suffix('.log.1')
                if bak.exists():
                    bak.unlink()
                SERVER_LOG.rename(bak)
        except Exception:
            pass

    def _spawn_locked(self):
        if not SERVER_EXE.exists():
            raise RuntimeError(f'audiocpp_server.exe 不存在: {SERVER_EXE}')
        if not _model_path().exists():
            raise RuntimeError(f'DotTTS 模型不存在: {_model_path()}')
        if not self._port_free():
            if self._healthy():
                self._ready = True
                return
            raise RuntimeError(f'端口 {SERVER_PORT} 被非本服务进程占用')

        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_config()
        self._rotate_log()
        logger.info('启动 audiocpp_server（DotTTS / Vulkan）: %s', SERVER_EXE)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        logf = open(SERVER_LOG, 'ab')
        try:
            self._proc = subprocess.Popen(
                [str(SERVER_EXE), '--config', str(CONFIG_FILE)],
                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=str(AUDIOCPP_DIR), creationflags=flags,
            )
        finally:
            logf.close()

        t0 = time.time()
        while time.time() - t0 < READY_TIMEOUT:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                self._proc = None
                raise RuntimeError(f'audiocpp_server 启动失败（exit={code}），见日志: {SERVER_LOG}')
            if self._healthy():
                self._ready = True
                logger.info('audiocpp_server（DotTTS）就绪（%.1fs）', time.time() - t0)
                return
            time.sleep(0.5)
        self._kill_locked()
        raise RuntimeError(f'audiocpp_server {READY_TIMEOUT:.0f}s 内未就绪，见日志: {SERVER_LOG}')

    def _kill_locked(self):
        proc = self._proc
        self._proc = None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:
            pass

    def _kill_port_owner(self):
        '''按端口清理实例（孤儿/外部进程兜底——_proc 句柄不可用时使用；仅 Windows）。'''
        if os.name != 'nt':
            return
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            out = subprocess.run(
                ['netstat', '-ano', '-p', 'tcp'],
                capture_output=True, text=True, timeout=10, creationflags=flags,
            ).stdout
        except Exception:
            return
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            # 形如: TCP    127.0.0.1:18245    0.0.0.0:0    LISTENING    12345
            if (len(parts) >= 5 and parts[0] == 'TCP' and parts[3] == 'LISTENING'
                    and parts[1].startswith('127.0.0.1:')
                    and parts[1].endswith(f':{SERVER_PORT}')):
                pids.add(parts[4])
        for pid in pids:
            if pid == '0' or pid == str(os.getpid()):
                continue
            logger.info('清理占用端口 %s 的旧实例（PID=%s）', SERVER_PORT, pid)
            try:
                subprocess.run(['taskkill', '/F', '/PID', pid],
                               capture_output=True, timeout=10, creationflags=flags)
            except Exception:
                pass
        t0 = time.time()
        while time.time() - t0 < 5 and not self._port_free():
            time.sleep(0.2)

    def _ensure_ready(self):
        with self._lock:
            changed = self._write_config()   # 配置以代码为准：先落盘（返回是否发生变更）
            if self._proc is not None and self._proc.poll() is None and self._ready and self._healthy():
                if not changed:
                    return
                # 配置变更：自有进程需重启加载新配置
                logger.info('检测到服务配置变更，重启 audiocpp_server')
                self._kill_locked()
                self._spawn_locked()
                return
            if self._healthy():
                if not changed:
                    self._ready = True
                    self._proc = None
                    return
                # 复用的外部/孤儿实例加载的是旧配置，无法原地确认模式 → 清理后重开
                logger.info('检测到服务配置变更，清理旧实例并重启 audiocpp_server')
                self._kill_port_owner()
            self._kill_locked()
            self._spawn_locked()

    def restart(self):
        with self._lock:
            self._kill_locked()
            if not self._port_free():
                # _proc 句柄不可用（孤儿/外部实例）→ 按端口兜底清理，确保能重开
                self._kill_port_owner()
            self._spawn_locked()

    def close(self):
        with self._lock:
            self._kill_locked()

    def release(self):
        '''卸载模型释放显存（进程保留，下次请求自动重载）——切换 TTS 配置时调用。

        走官方 /v1/tasks/unload_all_models 端点（audio.cpp v0.9.0 支持，源码核查）；
        实例未运行/不健康时跳过（返回 released=False）；失败不抛异常（尽力而为）。
        '''
        with self._lock:
            proc_alive = self._proc is not None and self._proc.poll() is None
        if not proc_alive and self._port_free():
            return {'released': False, 'reason': 'not_running'}
        if not self._healthy():
            return {'released': False, 'reason': 'unhealthy'}
        try:
            r = httpx.post(f'{self._base_url()}/v1/tasks/unload_all_models',
                           timeout=RELEASE_TIMEOUT, trust_env=False)
            if r.status_code == 200:
                logger.info('模型已卸载（显存释放，进程保留）: %s', r.text[:200])
                return {'released': True}
            return {'released': False, 'reason': f'http_{r.status_code}'}
        except Exception as e:
            return {'released': False, 'reason': str(e)[:160]}

    # ---- 业务 ----
    def list_voices(self):
        '''voice_dir 声音库列表（前端参考音频下拉数据源）。'''
        self._ensure_ready()
        r = httpx.get(f'{self._base_url()}/v1/audio/voices', timeout=30, trust_env=False)
        r.raise_for_status()
        data = r.json()
        voices = data.get('voices') or []
        return [str(v) for v in voices if str(v).strip()]

    @staticmethod
    def _apply_common(payload, voice, language, num_steps):
        '''公共字段：克隆声音 / 语言标签 / 推理步数。

        - num_inference_steps 走 options 子对象（引擎按 family 严格校验通道）
        - 默认值 10 不传（服务端默认即 10）；20/32 才显式传
        - language 为空或 none 不传（不注入语言标签）
        '''
        if voice:
            payload['voice'] = voice
        options = {}
        if num_steps and int(num_steps) > 0 and int(num_steps) != 10:
            options['num_inference_steps'] = int(num_steps)
        if options:
            payload['options'] = options
        if language and language != 'none':
            payload['language'] = language

    def synthesize(self, text, voice='', language='', num_steps=0):
        self._ensure_ready()
        payload = {'model': MODEL_ID, 'input': text, 'response_format': 'wav'}
        self._apply_common(payload, voice, language, num_steps)
        try:
            return self._post(payload)
        except (DotTtsRequestError, DotTtsInsufficientMemoryError):
            raise
        except Exception as e:
            logger.warning('dotstts 合成失败(%s)，重启服务后重试一次', e)
            self.restart()
            return self._post(payload)

    # ---- 流式合成（async；与 omnivoice 流式同构：首块前失败重启重试一次）----

    async def stream(self, text, voice='', language='', num_steps=0):
        """流式合成：异步生成 s16le PCM 块（48 kHz mono）。

        - response_format="pcm" + stream_format="audio"：服务端生成中逐块推送
          （实测 ~80ms/160ms/640ms 渐进块）；客户端断开 → 服务端中止剩余合成
        - 首块前失败（连接/5xx）：重启服务重试一次；首块后失败直接抛出（已播部分无法回滚）
        - 阻塞操作（服务启动/重启）走 to_thread，不阻塞事件循环
        """
        if not text or not text.strip():
            raise ValueError('text 不能为空')
        await asyncio.to_thread(self._ensure_ready)
        payload = self._stream_payload(text, voice, language, num_steps)
        started = False
        try:
            async for chunk in self._open_stream(payload):
                started = True
                yield chunk
        except (DotTtsRequestError, DotTtsInsufficientMemoryError):
            raise
        except Exception as e:
            if started:
                raise
            logger.warning('dotstts 流式合成失败(%s)，重启服务后重试一次', e)
            await asyncio.to_thread(self.restart)
            async for chunk in self._open_stream(payload):
                yield chunk

    def _stream_payload(self, text, voice, language, num_steps):
        payload = {
            'model': MODEL_ID,
            'input': text,
            'response_format': 'pcm',
            'stream_format': 'audio',
        }
        self._apply_common(payload, voice, language, num_steps)
        return payload

    async def _open_stream(self, payload):
        """单次流式请求：逐块产出 s16le PCM；取消时随 async with 自动关闭连接。"""
        async with httpx.AsyncClient(
            trust_env=False,   # 本地回环调用禁止走系统代理
            timeout=httpx.Timeout(STREAM_READ_TIMEOUT, connect=5.0),
        ) as client:
            async with client.stream(
                'POST', f'{self._base_url()}/v1/audio/speech', json=payload
            ) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode('utf-8', 'replace')[:300]
                    msg = f'audiocpp_server HTTP {r.status_code}: {detail}'
                    if r.status_code == 503 and 'insufficient_memory' in detail:
                        raise DotTtsInsufficientMemoryError(msg)
                    if 400 <= r.status_code < 500:
                        raise DotTtsRequestError(msg)
                    raise RuntimeError(msg)
                async for chunk in r.aiter_bytes():
                    if chunk:
                        yield chunk

    def _post(self, payload):
        try:
            r = httpx.post(
                f'{self._base_url()}/v1/audio/speech',
                json=payload,
                timeout=httpx.Timeout(SYNTH_TIMEOUT, connect=5.0),
                trust_env=False,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f'audiocpp_server 请求失败: {e}') from e
        if r.status_code == 503 and 'insufficient_memory' in r.text:
            raise DotTtsInsufficientMemoryError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 500:
            raise RuntimeError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 400:
            raise DotTtsRequestError(f'合成请求被拒 {r.status_code}: {r.text[:200]}')
        if not r.content:
            raise RuntimeError('audiocpp_server 返回空音频')
        return r.content


_dotstts = DotTtsEngine()
atexit.register(_dotstts.close)

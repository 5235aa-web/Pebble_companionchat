# -*- coding: utf-8 -*-
r'''
Ming 3.0 TTS 0.5B 引擎 —— audiocpp_server 常驻子进程（audio.cpp / Vulkan GPU）

- 模型：Ming-3.0-tts-0.5b Q8_0（GGUF，本地离线，
  E:\dearpebble\models\ming30tts\ming-3.0-tts-0.5b-q8_0.gguf，1.62 GB）
- 运行时：audio.cpp 的 audiocpp_server.exe（OpenAI 兼容 HTTP，Vulkan 后端）
- 三任务路由：tts（指令 / 内置音色）、clon（参考音频克隆）、vdes（音色设计）。
  同一个 GGUF 注册三个 model 条目（唯一 id），max_loaded_models=1，服务端按条目
  懒加载并在切换时卸载前一份，显存始终只占一份。
- 八种模板：tts / multilingual / basic / emotion / dialect / ip / voice_design / freeform
- 结构化控制（仅对应模板生效）：basic 用 speed(慢速|中速|快速) + pitch(低|中|高) +
  volume(低|中|高)，emotion 用 emotion 标签，dialect 用 dialect(川渝话|粤语)，
  ip 用 voice_id(内置英文音色 US_Emma/US_James/UK_George/UK_Charlotte)
- 生成参数：guidance_scale / sigma / temperature / seed / max_tokens
- 长文本：text_chunk_size / text_chunk_mode / raw_text / trim_silence
- 流式：stream_frames_per_event / stream_chunk_policy
- 说话人：speaker_embedding_file（192 维 CAMPPlus）/ export_speaker_embedding_file /
  use_speaker_embedding / zero_speaker_embedding / reference_text
- 会话：reference_cache（复用最近一份参考波形的条件）
- 输出：44.1 kHz mono PCM（流式）/ WAV（整段）
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
    audio_cpp_exe_name,
    model_path,
    resolve_user_path,
)
import httpx

logger = logging.getLogger('ming3tts')

BASE_DIR = Path(__file__).resolve().parent
AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / audio_cpp_exe_name()
CONFIG_FILE = ENGINE_STATE_DIR / 'server_ming3tts.json'
VOICE_DIR = SHARED_VOICE_DIR
SERVER_LOG = ENGINE_STATE_DIR / 'server_ming3tts_runtime.log'
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
    return model_path('ming30tts', 'ming-3.0-tts-0.5b-q8_0.gguf')

SERVER_PORT = 18247
SAMPLE_RATE = 44100

READY_TIMEOUT = 180.0
SYNTH_TIMEOUT = 300.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
STREAM_READ_TIMEOUT = 120.0
DEFAULT_STREAM_CHUNK_SIZE = 50     # 官方默认 CJK 按 50 codepoints 切块（非 CJK 为 300）
IDLE_UNLOAD_MS = 120000
MIN_FREE_MEMORY_MB = 128
RELEASE_TIMEOUT = 15.0

# 三个任务路由 -> audio.cpp model 条目 id
MODEL_IDS = {
    'tts': 'ming3-tts',
    'clon': 'ming3-clone',
    'vdes': 'ming3-design',
}

# 官方 26 项 request option 的白名单与类型（audio.cpp 对未知 option 严格报错）
_BOOL_OPTIONS = {
    'use_speaker_embedding',
    'zero_speaker_embedding',
    'raw_text',
    'trim_silence',
}
_INT_OPTIONS = {
    'max_tokens',
    'seed',
    'text_chunk_size',
    'stream_frames_per_event',
}
_FLOAT_OPTIONS = {
    'guidance_scale',
    'sigma',
    'temperature',
}
_STR_OPTIONS = {
    'template_name',
    'language',
    'instruct',
    'instruction_schema',
    'speed',
    'pitch',
    'volume',
    'emotion',
    'dialect',
    'voice_id',
    'reference_text',
    'speaker_embedding_file',
    'export_speaker_embedding_file',
    'text_chunk_mode',
    'stream_chunk_policy',
}
_KNOWN_OPTIONS = _BOOL_OPTIONS | _INT_OPTIONS | _FLOAT_OPTIONS | _STR_OPTIONS

# 会话级选项（不是 inference-request option，只能在模型条目里配置）。
# 键必须带 family 前缀，否则 audio.cpp 加载配置时会拒绝。
_session_options = {
    'ming3_tts.reference_cache': True,
}


def set_reference_cache(enabled: bool) -> None:
    '''设置会话级参考条件缓存开关。

    该值不属于请求参数，改动后由 _ensure_ready 检测到配置变更并重启服务生效。
    '''
    _session_options['ming3_tts.reference_cache'] = bool(enabled)


class Ming3TtsRequestError(RuntimeError):
    '''请求类错误（4xx）——不触发服务重启'''


class Ming3TtsInsufficientMemoryError(RuntimeError):
    '''内存/显存不足（503 insufficient_memory）——重启无法解决，不触发重启重试'''


def _clean_options(options) -> dict:
    '''按官方白名单清洗 request options。

    - 丢弃空值（None / 空字符串 / 空容器），audio.cpp 对空枚举值和未知键会严格报错
    - 按声明类型转换；布尔必须是 JSON true/false，不能是 "true" 字符串
    - 数字选项接受前端传来的字符串形式（输入框），转换失败则丢弃
    '''
    if not isinstance(options, dict):
        return {}
    cleaned = {}
    for key, value in options.items():
        if key not in _KNOWN_OPTIONS:
            continue
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if key in _BOOL_OPTIONS:
            if isinstance(value, bool):
                cleaned[key] = value
            elif isinstance(value, (int, float)):
                cleaned[key] = bool(value)
            elif isinstance(value, str):
                lowered = value.lower()
                if lowered in ('true', '1', 'yes', 'on'):
                    cleaned[key] = True
                elif lowered in ('false', '0', 'no', 'off'):
                    cleaned[key] = False
            continue
        if key in _INT_OPTIONS:
            try:
                cleaned[key] = int(float(value))
            except (TypeError, ValueError):
                continue
            continue
        if key in _FLOAT_OPTIONS:
            try:
                cleaned[key] = float(value)
            except (TypeError, ValueError):
                continue
            continue
        cleaned[key] = str(value)
    return cleaned


def _server_config() -> dict:
    '''audiocpp_server 配置：voice_dir 声音库 + ming3_tts 三个任务条目。

    mode=streaming：流式请求走 chunked PCM 推送，非流式请求（/tts 整段 WAV）行为不变。
    三个条目指向同一 GGUF，仅 task 不同；max_loaded_models=1 保证显存只占一份。
    '''
    return {
        'host': '127.0.0.1',
        'port': SERVER_PORT,
        'backend': 'cpu' if _override_backend == 'cpu' else 'vulkan',
        'device': 0,
        'threads': 4,
        'lazy_load': True,
        'max_loaded_models': 1,
        'idle_unload_ms': IDLE_UNLOAD_MS,
        'min_free_memory_mb': MIN_FREE_MEMORY_MB,
        'voice_dir': str(VOICE_DIR),
        'models': [
            {
                'id': MODEL_IDS['tts'],
                'family': 'ming3_tts',
                'path': str(_model_path()),
                'task': 'tts',
                'mode': 'streaming',
                'session_options': dict(_session_options),
            },
            {
                'id': MODEL_IDS['clon'],
                'family': 'ming3_tts',
                'path': str(_model_path()),
                'task': 'clon',
                'mode': 'streaming',
                'session_options': dict(_session_options),
            },
            {
                'id': MODEL_IDS['vdes'],
                'family': 'ming3_tts',
                'path': str(_model_path()),
                'task': 'vdes',
                'mode': 'streaming',
                'session_options': dict(_session_options),
            },
        ],
    }


class Ming3TtsEngine:
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
            raise RuntimeError(f'Ming3 TTS 模型不存在: {_model_path()}')
        if not self._port_free():
            if self._healthy():
                self._ready = True
                return
            raise RuntimeError(f'端口 {SERVER_PORT} 被非本服务进程占用')

        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_config()
        self._rotate_log()
        logger.info('启动 audiocpp_server（Ming3 TTS / Vulkan）: %s', SERVER_EXE)
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
                logger.info('audiocpp_server（Ming3 TTS）就绪（%.1fs）', time.time() - t0)
                return
            time.sleep(0.3)
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
            # 形如: TCP    127.0.0.1:18247    0.0.0.0:0    LISTENING    12345
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
                logger.info('检测到服务配置变更，重启 audiocpp_server')
                self._kill_locked()
                self._spawn_locked()
                return
            if self._healthy():
                if not changed:
                    self._ready = True
                    self._proc = None
                    return
                logger.info('检测到服务配置变更，清理旧实例并重启 audiocpp_server')
                self._kill_port_owner()
            self._kill_locked()
            self._spawn_locked()

    def restart(self):
        with self._lock:
            self._kill_locked()
            if not self._port_free():
                self._kill_port_owner()
            self._spawn_locked()

    def close(self):
        with self._lock:
            self._kill_locked()

    def release(self):
        '''卸载模型释放显存（进程保留，下次请求自动重载）——切换 TTS 配置时调用。'''
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
    def _route(task: str) -> str:
        route = (task or 'tts').strip().lower()
        if route not in MODEL_IDS:
            raise ValueError(f'未知的 Ming3 任务路由: {task!r}（可用 tts / clon / vdes）')
        return MODEL_IDS[route]

    @staticmethod
    def _build_payload(text, task, voice, options, response_format='wav', stream=False, chunk_size=0):
        payload = {
            'model': Ming3TtsEngine._route(task),
            'input': text,
            'response_format': response_format,
        }
        if stream:
            payload['stream_format'] = 'audio'
        if voice:
            payload['voice'] = voice
        opts = _clean_options(options)
        if chunk_size and int(chunk_size) > 0:
            opts['text_chunk_size'] = int(chunk_size)
        if opts:
            payload['options'] = opts
        return payload

    def synthesize(self, text, task='tts', voice='', options=None):
        '''整段合成，返回 WAV 字节（44.1 kHz mono）。

        task: tts（指令 / 内置音色）、clon（参考音频克隆）、vdes（音色设计）
        voice: voice_dir 中的参考音频名（clon 用）
        options: 官方 26 项 request option 的字典，按白名单与类型清洗后下传
        '''
        self._ensure_ready()
        payload = self._build_payload(text, task, voice, options, response_format='wav')
        try:
            return self._post(payload)
        except (Ming3TtsRequestError, Ming3TtsInsufficientMemoryError):
            raise
        except Exception as e:
            logger.warning('ming3tts 合成失败(%s)，重启服务后重试一次', e)
            self.restart()
            return self._post(payload)

    # ---- 流式合成（async；与 omnivoice / dotstts 流式同构）----
    async def stream(self, text, task='tts', voice='', options=None, chunk_size=0):
        '''流式合成：异步生成 s16le PCM 块（44.1 kHz mono）。

        - response_format="pcm" + stream_format="audio"：服务端按 text_chunk_size 切块，
          逐块合成、逐块 chunked 推送；客户端断开 → 服务端中止剩余合成
        - stream_frames_per_event / stream_chunk_policy 控制每事件的最大 latent patch 数
          与块增长策略（fixed / grow）
        - 首块前失败（连接/5xx）：重启服务重试一次；首块后失败直接抛出
        '''
        if not text or not text.strip():
            raise ValueError('text 不能为空')
        await asyncio.to_thread(self._ensure_ready)
        payload = self._build_payload(
            text, task, voice, options,
            response_format='pcm', stream=True,
            chunk_size=chunk_size or DEFAULT_STREAM_CHUNK_SIZE,
        )
        started = False
        try:
            async for chunk in self._open_stream(payload):
                started = True
                yield chunk
        except (Ming3TtsRequestError, Ming3TtsInsufficientMemoryError):
            raise
        except Exception as e:
            if started:
                raise
            logger.warning('ming3tts 流式合成失败(%s)，重启服务后重试一次', e)
            await asyncio.to_thread(self.restart)
            async for chunk in self._open_stream(payload):
                yield chunk

    async def _open_stream(self, payload):
        '''单次流式请求：逐块产出 s16le PCM；取消时随 async with 自动关闭连接。'''
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(STREAM_READ_TIMEOUT, connect=5.0),
        ) as client:
            async with client.stream(
                'POST', f'{self._base_url()}/v1/audio/speech', json=payload
            ) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode('utf-8', 'replace')[:300]
                    msg = f'audiocpp_server HTTP {r.status_code}: {detail}'
                    if r.status_code == 503 and 'insufficient_memory' in detail:
                        raise Ming3TtsInsufficientMemoryError(msg)
                    if 400 <= r.status_code < 500:
                        raise Ming3TtsRequestError(msg)
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
            raise Ming3TtsInsufficientMemoryError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 500:
            raise RuntimeError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 400:
            raise Ming3TtsRequestError(f'合成请求被拒 {r.status_code}: {r.text[:200]}')
        if not r.content:
            raise RuntimeError('audiocpp_server 返回空音频')
        return r.content


_ming3tts = Ming3TtsEngine()
atexit.register(_ming3tts.close)

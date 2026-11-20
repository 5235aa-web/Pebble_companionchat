# -*- coding: utf-8 -*-
r'''
Fish Audio S2 Pro 引擎 —— audiocpp_server 常驻子进程（audio.cpp / Vulkan GPU）

- 模型：Fish Audio S2 Pro Q8_0（GGUF，自包含，本地离线，
  E:\dearpebble\models\fishaudio\fish-audio-s2-pro-q8_0.gguf，6.32 GB）
- 运行时：audio.cpp 的 audiocpp_server.exe（OpenAI 兼容 HTTP，Vulkan 后端）
- 克隆：voice_dir 声音库（与 OmniVoice / DotTTS 共用——参考音频放
  audio-cpp/voice/，prompt_text 映射转写文本，服务端自动注入 reference_text）
- 采样参数：temperature / top_p / top_k（留空用服务端默认 0.8 / 0.8 / 30）
- 输出：44.1 kHz mono WAV（整段）
- 模式：offline —— audio.cpp v0.9.0 的 fish_audio 加载器仅注册 tts(offline)；
  官方流式支持（PR #870）尚未进入任何 release，暂不支持 /tts/stream
  （前端流式开关对该引擎自动回退整段模式）
- 生命周期：懒启动 -> 常驻 -> atexit 清理；端口复用防孤儿进程；崩溃自动重启
'''
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

logger = logging.getLogger('fishaudio')

BASE_DIR = Path(__file__).resolve().parent
AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / 'audiocpp_server.exe'
CONFIG_FILE = ENGINE_STATE_DIR / 'server_fishaudio.json'
VOICE_DIR = SHARED_VOICE_DIR
SERVER_LOG = ENGINE_STATE_DIR / 'server_fishaudio_runtime.log'
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
    return model_path('fishaudio', 'fish-audio-s2-pro-q8_0.gguf')

MODEL_ID = 'fishaudio'
SERVER_PORT = 18246
SAMPLE_RATE = 44100

READY_TIMEOUT = 240.0              # 模型 6.3 GB，首次加载留足余量
SYNTH_TIMEOUT = 300.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
IDLE_UNLOAD_MS = 120000            # 闲置 2 分钟自动卸载模型释放显存（下次请求自动重载）
# 加载前内存预检 headroom（MiB）；0 = 完全禁用预检（audio.cpp 官方语义"0 disables it entirely"）。
# 原因：Fish Audio 权重 6.32GB → 官方估算 = 权重 × 1.5 + 128MiB ≈ 8.95 GiB，在 16GB 主机
# （开前端后可用 ~8.2-8.8 GiB）上被预检拦截；但实测实际物理需求仅 ~7.4 GiB（CLI peak_rss），
# 估算的 1.5x 运行时系数对本模型过于保守。禁用后依赖既有保护：idle_unload（2 分钟闲置卸载）
# + 切换即释放 + 懒加载；若加载期实际内存不足，会在分配阶段以明确错误失败。
MIN_FREE_MEMORY_MB = 0
RELEASE_TIMEOUT = 15.0             # 切换释放（unload_all_models）请求超时


class FishAudioRequestError(RuntimeError):
    '''请求类错误（4xx）——不触发服务重启'''


class FishAudioInsufficientMemoryError(RuntimeError):
    '''内存/显存不足（503 insufficient_memory）——重启无法解决，不触发重启重试'''


def _server_config() -> dict:
    '''audiocpp_server 配置：voice_dir 声音库 + fish_audio 模型。

    mode=offline：v0.9.0 的 fish_audio 加载器仅注册 tts(offline)，流式不可用。
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
                'family': 'fish_audio',
                'path': str(_model_path()),
                'task': 'tts',
                'mode': 'offline',
            }
        ],
    }


class FishAudioEngine:
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
            raise RuntimeError(f'Fish Audio 模型不存在: {_model_path()}')
        if not self._port_free():
            if self._healthy():
                self._ready = True
                return
            raise RuntimeError(f'端口 {SERVER_PORT} 被非本服务进程占用')

        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_config()
        self._rotate_log()
        logger.info('启动 audiocpp_server（Fish Audio / Vulkan）: %s', SERVER_EXE)
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
                logger.info('audiocpp_server（Fish Audio）就绪（%.1fs）', time.time() - t0)
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
            # 形如: TCP    127.0.0.1:18246    0.0.0.0:0    LISTENING    12345
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
    def _apply_sampling(payload, voice, temperature, top_p, top_k):
        '''公共字段：克隆声音（voice_dir 库名）/ 采样参数（留空用服务端默认 0.8/0.8/30）。

        - voice：voice_dir 库名，服务端解析为 voice_ref + reference_text（prompt_text 注入）
        - temperature (0,2)、top_p (0,1]、top_k >=1：越界值不传（防 4xx 请求拒绝）
        '''
        if voice:
            payload['voice'] = voice
        if temperature and float(temperature) > 0:
            payload['temperature'] = float(temperature)
        if top_p and 0 < float(top_p) <= 1:
            payload['top_p'] = float(top_p)
        if top_k and int(top_k) > 0:
            payload['top_k'] = int(top_k)

    def synthesize(self, text, voice='', temperature=0, top_p=0, top_k=0):
        self._ensure_ready()
        payload = {'model': MODEL_ID, 'input': text, 'response_format': 'wav'}
        self._apply_sampling(payload, voice, temperature, top_p, top_k)
        try:
            return self._post(payload)
        except (FishAudioRequestError, FishAudioInsufficientMemoryError):
            raise
        except Exception as e:
            logger.warning('fishaudio 合成失败(%s)，重启服务后重试一次', e)
            self.restart()
            return self._post(payload)

    # ---- 注：v0.9.0 的 fish_audio 不支持流式（仅 tts(offline)），
    #         故本引擎不提供 stream()；/tts/stream 白名单不含 fishaudio，
    #         前端流式开关对该引擎自动回退整段模式。----

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
            raise FishAudioInsufficientMemoryError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 500:
            raise RuntimeError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 400:
            raise FishAudioRequestError(f'合成请求被拒 {r.status_code}: {r.text[:200]}')
        if not r.content:
            raise RuntimeError('audiocpp_server 返回空音频')
        return r.content


_fishaudio = FishAudioEngine()
atexit.register(_fishaudio.close)

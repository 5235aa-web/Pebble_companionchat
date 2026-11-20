# -*- coding: utf-8 -*-
'''
Qwen3-ASR STT 引擎 —— audiocpp_server 常驻子进程（audio.cpp / Vulkan GPU）

- 模型：Qwen3-ASR 1.7B Q8_0（GGUF，本地离线，约 2.3 GiB）
- 运行时：audio.cpp 的 audiocpp_server.exe（OpenAI 兼容 HTTP，Vulkan 后端）
- 接口：POST /v1/audio/transcriptions（multipart 上传 WAV，返回 JSON text）
- 生命周期：懒启动 -> 常驻 -> 退出即清理（atexit / lifespan / kill-on-exit job 三层）；
  端口复用防孤儿进程，复用来的实例由端口扫描兜底结束；崩溃自动重启
- 独立 server 实例（端口 18243 / 独立配置），与其他引擎互不影响；
  转写走 /v1/audio/transcriptions，服务端内部自动重采样，无需 Python 侧预处理
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


from . import audio_cpp_job
from .paths import (
    ENGINE_STATE_DIR,
    VOICE_DIR as SHARED_VOICE_DIR,
    audio_cpp_dir,
    model_path,
    resolve_user_path,
)
import httpx

logger = logging.getLogger('qwen3asr')

BASE_DIR = Path(__file__).resolve().parent
AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / 'audiocpp_server.exe'
CONFIG_FILE = ENGINE_STATE_DIR / 'qwen3asr_server.json'
SERVER_LOG = ENGINE_STATE_DIR / 'qwen3asr_runtime.log'
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
    return model_path('qwen3-asr', 'qwen3-asr-1.7b-q8_0.gguf')

MODEL_ID = 'qwen3-asr'
SERVER_PORT = 18243

READY_TIMEOUT = 180.0
TRANSCRIBE_TIMEOUT = 300.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
IDLE_UNLOAD_MS = 120000            # 闲置 2 分钟自动卸载模型释放显存（下次请求自动重载）
MIN_FREE_MEMORY_MB = 128           # 加载前预检 headroom（估算含 1.5x 运行时系数，128 足够；过大误挡边缘场景）


class Qwen3AsrRequestError(RuntimeError):
    '''请求类错误（4xx）——不触发服务重启'''


class Qwen3AsrInsufficientMemoryError(RuntimeError):
    '''内存/显存不足（503 insufficient_memory）——重启无法解决，不触发重启重试'''


def _server_config() -> dict:
    '''audiocpp_server 配置：qwen3_asr 单模型，streaming 模式。

    实时通话走 /v1/audio/transcriptions/live，该端点要求模型以 mode=streaming
    加载（否则 400 "requires a model configured with mode=streaming"）。普通转写
    /v1/audio/transcriptions 不校验 mode，所以一份配置同时服务两条链路；只有
    batch 与 alignment 端点硬性要求 offline，本项目不用它们。
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
        'models': [
            {
                'id': MODEL_ID,
                'family': 'qwen3_asr',
                'path': str(_model_path()),
                'task': 'asr',
                'mode': 'streaming',
            }
        ],
    }


class Qwen3AsrSTT:
    """Qwen3-ASR 1.7B（Q8_0）引擎：audiocpp_server 常驻 + HTTP 转写。"""

    # See confucius4_stt_engine: the call bridge reads this off the instance.
    MODEL_ID = MODEL_ID
    ENGINE_ID = 'qwen3_asr'
    ENGINE_LABEL = 'Qwen3-ASR'
    ENGINE_DESC = 'Qwen3-ASR 1.7B Q8 · 30 语言 + 22 方言 · Vulkan'

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = threading.RLock()
            cls._instance._proc = None
            cls._instance._ready = False
        return cls._instance

    def __init__(self):
        # 快速失败：文件缺失时构造即报错（registry 会记录 error 并回退）
        if not SERVER_EXE.exists():
            raise FileNotFoundError(f'audiocpp_server.exe 不存在: {SERVER_EXE}')
        if not _model_path().exists():
            raise FileNotFoundError(f'Qwen3-ASR 模型不存在: {_model_path()}')
        logger.info('Qwen3AsrSTT initialized: model=%s', _model_path())

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

    def _write_config(self):
        text = json.dumps(_server_config(), indent=2, ensure_ascii=False)
        try:
            if CONFIG_FILE.read_text(encoding='utf-8') == text:
                return
        except Exception:
            pass
        CONFIG_FILE.write_text(text, encoding='utf-8')

    def _rotate_log(self):
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > LOG_MAX_BYTES:
                bak = SERVER_LOG.with_suffix('.log.1')
                if bak.exists():
                    bak.unlink()
                SERVER_LOG.rename(bak)
        except Exception:
            pass

    def _config_matches(self) -> bool:
        """True when the file on disk is what this process would write now.

        The model's mode is baked in at load time, so a server left over from an
        older config keeps answering with the old behaviour (offline vs
        streaming) no matter what the request asks for.
        """
        try:
            current = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except Exception:
            return True   # cannot tell; leave a running server alone
        return current == _server_config()

    def _kill_port_owner(self) -> bool:
        """End whatever holds SERVER_PORT. Only called after /health answered.

        True once the port can be bound again: a caller that reports success to
        the user must not do so on a taskkill the OS refused.
        """
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            out = subprocess.run(
                ['netstat', '-ano'], capture_output=True, text=True, timeout=10,
                creationflags=flags,
            ).stdout
        except Exception as e:
            logger.warning('qwen3asr netstat 失败: %s', e)
            return False
        pids = set()
        for line in out.splitlines():
            if f':{SERVER_PORT} ' in line and 'LISTENING' in line:
                tail = line.split()
                if tail and tail[-1].isdigit():
                    pids.add(int(tail[-1]))
        for pid in pids:
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/F'],
                    capture_output=True, timeout=10, creationflags=flags,
                )
            except Exception as e:
                logger.warning('qwen3asr taskkill %s 失败: %s', pid, e)
        # Give the socket a moment to be released before the new spawn binds it.
        for _ in range(20):
            if self._port_free():
                return True
            time.sleep(0.25)
        return False

    def _spawn_locked(self):
        if not SERVER_EXE.exists():
            raise RuntimeError(f'audiocpp_server.exe 不存在: {SERVER_EXE}')
        if not _model_path().exists():
            raise RuntimeError(f'Qwen3-ASR 模型不存在: {_model_path()}')
        if not self._port_free():
            if not self._healthy():
                raise RuntimeError(f'端口 {SERVER_PORT} 被非本服务进程占用')
            if self._config_matches():
                self._ready = True
                return
            logger.info('qwen3asr 配置已变更，结束旧服务进程后按新配置重启')
            self._kill_port_owner()

        self._write_config()
        self._rotate_log()
        logger.info('启动 audiocpp_server（Qwen3-ASR / Vulkan）: %s', SERVER_EXE)
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

        audio_cpp_job.adopt(self._proc)

        t0 = time.time()
        while time.time() - t0 < READY_TIMEOUT:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                self._proc = None
                raise RuntimeError(f'audiocpp_server 启动失败（exit={code}），见日志: {SERVER_LOG}')
            if self._healthy():
                self._ready = True
                logger.info('audiocpp_server(Qwen3-ASR) 就绪（%.1fs）', time.time() - t0)
                return
            time.sleep(0.5)
        self._kill_locked()
        raise RuntimeError(f'audiocpp_server {READY_TIMEOUT:.0f}s 内未就绪，见日志: {SERVER_LOG}')

    def _kill_locked(self) -> str:
        """End the child this process spawned, if there is one.

        'stopped' / 'idle' / 'failed'. On 'failed' the handle is kept: a process
        that survived terminate and kill is still reachable on a retry, and
        dropping it would hide the orphan from every later attempt.
        """
        proc = self._proc
        self._ready = False
        if proc is None:
            return 'idle'
        alive = True
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            alive = proc.poll() is None
        except Exception:
            alive = True
        if alive:
            return 'failed'
        self._proc = None
        return 'stopped'

    def _release_locked(self) -> str:
        """End the server holding this port, whoever spawned it.

        Returns 'stopped', 'idle' (nothing was listening) or 'failed' (a server
        answered but the OS would not let it go). The caller reports that
        verbatim, so a refused taskkill must not come back as success.

        `_proc` is set only when this process started the server. One that
        survived an earlier backend keeps answering on the port and gets reused
        with `_proc = None`, so a pid-less terminate cannot touch it: the port
        sweep is the only way to end such an instance, and skipping it is how an
        orphan outlives every later stop.
        """
        verdict = self._kill_locked()
        if verdict == 'stopped':
            return 'stopped'
        if self._healthy():
            # Either this process never owned the server - it came from an earlier
            # backend - or the terminate above did not take. Both mean something is
            # still holding the port, and the sweep is the only way to end it.
            logger.info('qwen3asr 端口 %s 上仍有服务应答，按端口结束', SERVER_PORT)
            return 'stopped' if self._kill_port_owner() else 'failed'
        return verdict

    def stop(self) -> str:
        """Release the server and the VRAM it holds. Safe to call repeatedly.

        Switching engines has to free the previous one explicitly: each server
        loads a full model, and the card cannot hold two of them next to the LLM.

        Returns 'stopped' / 'idle' / 'failed' - see `_release_locked`.
        """
        with self._lock:
            return self._release_locked()

    def _ensure_ready(self):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._ready and self._healthy():
                return
            if self._healthy():
                # A server started before the last config change still holds the
                # old mode; reusing it would keep the live endpoint refusing.
                if self._config_matches():
                    self._ready = True
                    self._proc = None
                    return
                logger.info('qwen3asr 配置已变更，结束旧服务进程后按新配置重启')
                self._kill_port_owner()
            self._release_locked()
            self._spawn_locked()

    def restart(self):
        with self._lock:
            self._release_locked()
            self._spawn_locked()

    def close(self):
        with self._lock:
            self._release_locked()

    # ---- public surface for the realtime call bridge ----
    @property
    def base_url(self) -> str:
        """HTTP root of the audio.cpp server, for callers streaming raw PCM."""
        return self._base_url()

    def ensure_ready(self) -> None:
        """Start the server and wait until it answers. Safe to call repeatedly."""
        self._ensure_ready()

    # ---- 业务 ----
    def transcribe_bytes(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        '''WAV bytes -> 文本。直接上传给 audiocpp_server（服务端内部自动重采样）。'''
        if not audio_bytes:
            return ''
        self._ensure_ready()
        try:
            return self._post(audio_bytes)
        except (Qwen3AsrRequestError, Qwen3AsrInsufficientMemoryError):
            raise
        except Exception as e:
            logger.warning('qwen3asr 转写失败(%s)，重启服务后重试一次', e)
            self.restart()
            return self._post(audio_bytes)

    def _post(self, audio_bytes: bytes) -> str:
        try:
            r = httpx.post(
                f'{self._base_url()}/v1/audio/transcriptions',
                files={'file': ('recording.wav', audio_bytes, 'audio/wav')},
                data={'model': MODEL_ID},
                timeout=httpx.Timeout(TRANSCRIBE_TIMEOUT, connect=5.0),
                trust_env=False,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f'audiocpp_server 请求失败: {e}') from e
        if r.status_code == 503 and 'insufficient_memory' in r.text:
            raise Qwen3AsrInsufficientMemoryError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 500:
            raise RuntimeError(f'audiocpp_server {r.status_code}: {r.text[:200]}')
        if r.status_code >= 400:
            raise Qwen3AsrRequestError(f'转写请求被拒 {r.status_code}: {r.text[:200]}')
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f'audiocpp_server 响应解析失败: {e}') from e
        text = str(data.get('text', '')).strip()
        if not text:
            logger.warning('qwen3asr 返回空转写')
        return text


# No module-level instance: stt_registry owns the lifecycle so nothing
# loads until a transcription asks for it.


def _close_on_exit() -> None:
    """Release on interpreter exit, for the launch mode nothing else covers.

    The packaged backend exits through run_backend, the desktop shell kills the
    tree outright, and audio_cpp_job covers a hard kill. What is left is
    `uvicorn --reload` in development: the worker is replaced in place, so
    without this hook its server survives the worker that started it.
    Reading `_instance` instead of constructing keeps an engine that was never
    used from being created just to be closed.
    """
    inst = Qwen3AsrSTT._instance
    if inst is None:
        return
    try:
        inst.close()
    except Exception:
        logger.warning('qwen3asr atexit close failed', exc_info=True)


atexit.register(_close_on_exit)

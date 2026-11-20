# -*- coding: utf-8 -*-
"""Generic STT engines for user-configured local models.

Provides two engines sharing the same lifecycle interface as ``Qwen3AsrSTT``:
1. ``AudioCppSttEngine``: runs ``audiocpp_server.exe`` on port 18250 for any
   ASR model family declared in ``audio-cpp/model_specs/*.json``.
2. ``OpenAICompatSttEngine``: forwards WAV clips to any local or remote
   OpenAI-compatible ``/v1/audio/transcriptions`` server (e.g. whisper.cpp,
   faster-whisper-server, SenseVoice API).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from . import audio_cpp_job
from .audio_specs import get_spec
from .paths import ENGINE_STATE_DIR, audio_cpp_dir, resolve_user_path

logger = logging.getLogger("audiocpp_stt")

AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / "audiocpp_server.exe"
CONFIG_FILE = ENGINE_STATE_DIR / "custom_stt_server.json"
SERVER_LOG = ENGINE_STATE_DIR / "custom_stt_runtime.log"

SERVER_PORT = 18250
READY_TIMEOUT = 180.0
TRANSCRIBE_TIMEOUT = 300.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
IDLE_UNLOAD_MS = 120000
MIN_FREE_MEMORY_MB = 128

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


class AudioCppSttEngine:
    """Managed audiocpp_server instance for a user-configured ASR family/GGUF."""

    def __init__(
        self,
        *,
        engine_id: str,
        label: str,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        language: str = "",
    ) -> None:
        self.ENGINE_ID = engine_id
        self.ENGINE_LABEL = label
        self.MODEL_ID = f"stt-{engine_id}"
        self.family = family.strip()
        self.raw_model_path = model_path.strip()
        self.compute_backend = "cpu" if str(compute_backend).lower() == "cpu" else "vulkan"
        self.language = language.strip()
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False

        resolved = resolve_user_path(self.raw_model_path)
        if not SERVER_EXE.exists():
            raise FileNotFoundError(f"audiocpp_server.exe 不存在: {SERVER_EXE}")
        if resolved is None or not resolved.exists():
            raise FileNotFoundError(f"ASR 模型文件不存在: {resolved or self.raw_model_path}")
        if not self.family:
            raise ValueError("ASR 模型家族 (family) 不能为空")

    def _resolved_model(self) -> Path:
        p = resolve_user_path(self.raw_model_path)
        if p is None:
            raise FileNotFoundError("ASR 模型路径为空")
        return p

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{SERVER_PORT}"

    @property
    def base_url(self) -> str:
        return self._base_url()

    def _healthy(self) -> bool:
        try:
            r = httpx.get(f"{self._base_url()}/health", timeout=HEALTH_TIMEOUT, trust_env=False)
            return r.status_code == 200
        except Exception:
            return False

    def _port_free(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", SERVER_PORT))
                return True
            except OSError:
                return False

    def _server_config(self) -> dict:
        spec = get_spec(self.family) or {}
        modes = spec.get("modes") or []
        mode = "streaming" if "streaming" in modes else "offline"
        return {
            "host": "127.0.0.1",
            "port": SERVER_PORT,
            "backend": self.compute_backend,
            "device": 0,
            "threads": 4,
            "lazy_load": True,
            "idle_unload_ms": IDLE_UNLOAD_MS,
            "min_free_memory_mb": MIN_FREE_MEMORY_MB,
            "models": [
                {
                    "id": self.MODEL_ID,
                    "family": self.family,
                    "path": str(self._resolved_model()),
                    "task": "asr",
                    "mode": mode,
                }
            ],
        }

    def _config_matches(self) -> bool:
        try:
            disk = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return False
        return disk == self._server_config()

    def _write_config(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(self._server_config(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _rotate_log(self) -> None:
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > LOG_MAX_BYTES:
                bak = SERVER_LOG.with_suffix(".log.1")
                if bak.exists():
                    bak.unlink()
                SERVER_LOG.rename(bak)
        except Exception:
            pass

    def _kill_locked(self) -> str:
        proc = self._proc
        self._ready = False
        if proc is None:
            return "idle"
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
            return "failed"
        self._proc = None
        return "stopped"

    def _kill_port_owner(self) -> bool:
        if os.name != "nt":
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, timeout=10, creationflags=flags,
            ).stdout
        except Exception:
            return False
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if (
                len(parts) >= 5
                and parts[0] == "TCP"
                and parts[3] == "LISTENING"
                and parts[1].startswith("127.0.0.1:")
                and parts[1].endswith(f":{SERVER_PORT}")
            ):
                pids.add(parts[4])
        for pid in pids:
            if pid == "0" or pid == str(os.getpid()):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    capture_output=True, timeout=10, creationflags=flags,
                )
            except Exception:
                pass
        for _ in range(20):
            if self._port_free():
                return True
            time.sleep(0.25)
        return False

    def _release_locked(self) -> str:
        verdict = self._kill_locked()
        if verdict == "stopped":
            return "stopped"
        if self._healthy():
            return "stopped" if self._kill_port_owner() else "failed"
        return verdict

    def _spawn_locked(self) -> None:
        model = self._resolved_model()
        if not SERVER_EXE.exists():
            raise RuntimeError(f"audiocpp_server.exe 不存在: {SERVER_EXE}")
        if not model.exists():
            raise RuntimeError(f"ASR 模型文件不存在: {model}")
        if not self._port_free():
            if not self._healthy():
                raise RuntimeError(f"端口 {SERVER_PORT} 被非本服务进程占用")
            if self._config_matches():
                self._ready = True
                return
            self._kill_port_owner()

        self._write_config()
        self._rotate_log()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        logf = open(SERVER_LOG, "ab")
        try:
            self._proc = subprocess.Popen(
                [str(SERVER_EXE), "--config", str(CONFIG_FILE)],
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
                raise RuntimeError(f"audiocpp_server 启动失败（exit={code}），见日志: {SERVER_LOG}")
            if self._healthy():
                self._ready = True
                return
            time.sleep(0.5)
        self._kill_locked()
        raise RuntimeError(f"audiocpp_server {READY_TIMEOUT:.0f}s 内未就绪，见日志: {SERVER_LOG}")

    def ensure_ready(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._ready and self._healthy():
                if self._config_matches():
                    return
            if self._healthy():
                if self._config_matches():
                    self._ready = True
                    self._proc = None
                    return
                self._kill_port_owner()
            self._release_locked()
            self._spawn_locked()

    def stop(self) -> str:
        with self._lock:
            return self._release_locked()

    def close(self) -> None:
        self.stop()

    def restart(self) -> None:
        with self._lock:
            self._release_locked()
            self._spawn_locked()

    def transcribe_bytes(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        if not audio_bytes:
            return ""
        self.ensure_ready()
        data = {"model": self.MODEL_ID}
        if self.language:
            data["language"] = self.language
        r = httpx.post(
            f"{self._base_url()}/v1/audio/transcriptions",
            files={"file": ("recording.wav", audio_bytes, "audio/wav")},
            data=data,
            timeout=httpx.Timeout(TRANSCRIBE_TIMEOUT, connect=5.0),
            trust_env=False,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"audiocpp_server {r.status_code}: {r.text[:240]}")
        try:
            return (r.json().get("text") or "").strip()
        except Exception:
            return (r.text or "").strip()


class OpenAICompatSttEngine:
    """STT client for any local/remote OpenAI-compatible /audio/transcriptions API."""

    def __init__(
        self,
        *,
        engine_id: str,
        label: str,
        base_url: str,
        api_key: str = "",
        model: str = "whisper-1",
        language: str = "",
    ) -> None:
        self.ENGINE_ID = engine_id
        self.ENGINE_LABEL = label
        self.MODEL_ID = model.strip() or "whisper-1"
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.language = language.strip()
        if not self.base_url:
            raise ValueError("STT 服务地址 (baseUrl) 不能为空")

    def _endpoint(self) -> str:
        if self.base_url.endswith("/audio/transcriptions"):
            return self.base_url
        if self.base_url.endswith("/transcriptions"):
            return self.base_url
        return f"{self.base_url}/audio/transcriptions"

    def ensure_ready(self) -> None:
        return None

    def stop(self) -> str:
        return "idle"

    def close(self) -> None:
        return None

    def transcribe_bytes(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        if not audio_bytes:
            return ""
        url = self._endpoint()
        trust_env = not any(h in url.lower() for h in _LOCAL_HOSTS)
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {"model": self.MODEL_ID}
        if self.language:
            data["language"] = self.language
        r = httpx.post(
            url,
            files={"file": ("recording.wav", audio_bytes, "audio/wav")},
            data=data,
            headers=headers,
            timeout=httpx.Timeout(TRANSCRIBE_TIMEOUT, connect=10.0),
            trust_env=trust_env,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"STT HTTP {r.status_code}: {r.text[:240]}")
        try:
            return (r.json().get("text") or "").strip()
        except Exception:
            return (r.text or "").strip()

# -*- coding: utf-8 -*-
"""Generic audio.cpp TTS engine for arbitrary user-configured local models.

Supports any TTS family present in ``audio-cpp/model_specs/*.json`` with a
user-chosen ``model_path`` (single GGUF file or package directory), compute
backend (``vulkan`` or ``cpu``), multi-task routing (``tts`` / ``clon`` /
``vdes``), and full ``request`` / ``session`` / ``load`` options.
"""
from __future__ import annotations

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
from typing import Any, Dict, Optional

import httpx

from . import audio_cpp_job
from .audio_specs import clean_options, get_spec, normalize_server_task
from .paths import (
    ENGINE_STATE_DIR,
    VOICE_DIR as SHARED_VOICE_DIR,
    audio_cpp_dir,
    resolve_user_path,
)

logger = logging.getLogger("audiocpp_tts")

AUDIOCPP_DIR = audio_cpp_dir()
SERVER_EXE = AUDIOCPP_DIR / "audiocpp_server.exe"
CONFIG_FILE = ENGINE_STATE_DIR / "server_custom_tts.json"
VOICE_DIR = SHARED_VOICE_DIR
SERVER_LOG = ENGINE_STATE_DIR / "server_custom_tts_runtime.log"

MODEL_ID = "custom-tts"
SERVER_PORT = 18249
READY_TIMEOUT = 240.0
SYNTH_TIMEOUT = 300.0
STREAM_READ_TIMEOUT = 120.0
HEALTH_TIMEOUT = 3.0
LOG_MAX_BYTES = 5 * 1024 * 1024
IDLE_UNLOAD_MS = 120000
MIN_FREE_MEMORY_MB = 128
RELEASE_TIMEOUT = 15.0


class AudioCppTtsRequestError(RuntimeError):
    """4xx request error — do not restart server."""


class AudioCppTtsInsufficientMemoryError(RuntimeError):
    """503 insufficient_memory — restarting cannot free memory."""


class AudioCppTtsEngine:
    """Managed audiocpp_server instance for user-selected TTS family + weights."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = threading.RLock()
            cls._instance._proc = None
            cls._instance._ready = False
        return cls._instance

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{SERVER_PORT}"

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

    @staticmethod
    def _model_id_for_task(family: str, task: str = "") -> str:
        spec = get_spec(family) or {}
        server_tasks = spec.get("serverTasks") or ["tts"]
        resolved_task = normalize_server_task(family, task)
        if resolved_task == server_tasks[0] or resolved_task not in server_tasks:
            return MODEL_ID
        return f"{MODEL_ID}-{resolved_task}"

    @staticmethod
    def _build_server_config(
        family: str,
        resolved_model: Path,
        compute_backend: str = "vulkan",
        task: str = "tts",
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        spec = get_spec(family) or {}
        modes = spec.get("modes") or []
        server_tasks = list(spec.get("serverTasks") or ["tts"])
        norm_task = normalize_server_task(family, task)
        if norm_task not in server_tasks:
            server_tasks = [norm_task] + [t for t in server_tasks if t != norm_task]

        mode = "streaming" if "streaming" in modes else "offline"
        backend_name = "cpu" if str(compute_backend).lower() == "cpu" else "vulkan"

        cleaned_load = clean_options(load_options, spec.get("loadOptions"))
        cleaned_session = clean_options(
            session_options, spec.get("sessionOptions"), prefix_family=family,
        )

        models = []
        for idx, tcode in enumerate(server_tasks):
            entry: Dict[str, Any] = {
                "id": MODEL_ID if idx == 0 else f"{MODEL_ID}-{tcode}",
                "family": family,
                "path": str(resolved_model),
                "task": tcode,
                "mode": mode,
            }
            if cleaned_load:
                entry["load_options"] = cleaned_load
            if cleaned_session:
                entry["session_options"] = cleaned_session
            models.append(entry)

        cfg: Dict[str, Any] = {
            "host": "127.0.0.1",
            "port": SERVER_PORT,
            "backend": backend_name,
            "device": 0,
            "threads": 4,
            "lazy_load": True,
            "idle_unload_ms": IDLE_UNLOAD_MS,
            "min_free_memory_mb": MIN_FREE_MEMORY_MB,
            "voice_dir": str(VOICE_DIR),
            "models": models,
        }
        if len(models) > 1:
            cfg["max_loaded_models"] = 1
        return cfg

    def _write_config(self, cfg: Dict[str, Any]) -> bool:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(cfg, indent=2, ensure_ascii=False)
        try:
            if CONFIG_FILE.read_text(encoding="utf-8") == text:
                return False
        except Exception:
            pass
        CONFIG_FILE.write_text(text, encoding="utf-8")
        return True

    def _rotate_log(self) -> None:
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > LOG_MAX_BYTES:
                bak = SERVER_LOG.with_suffix(".log.1")
                if bak.exists():
                    bak.unlink()
                SERVER_LOG.rename(bak)
        except Exception:
            pass

    def _kill_locked(self) -> None:
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

    def _kill_port_owner(self) -> None:
        if os.name != "nt":
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, timeout=10, creationflags=flags,
            ).stdout
        except Exception:
            return
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
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10, creationflags=flags,
                )
            except Exception:
                pass
        t0 = time.time()
        while time.time() - t0 < 5 and not self._port_free():
            time.sleep(0.2)

    def _spawn_locked(self, cfg: Dict[str, Any], resolved_model: Path) -> None:
        if not SERVER_EXE.exists():
            raise RuntimeError(f"audiocpp_server.exe 不存在: {SERVER_EXE}")
        if not resolved_model.exists():
            raise RuntimeError(f"TTS 模型路径不存在: {resolved_model}")
        if not self._port_free():
            if self._healthy():
                self._ready = True
                return
            raise RuntimeError(f"端口 {SERVER_PORT} 被非本服务进程占用")

        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_config(cfg)
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

    def _prepare_cfg(
        self,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        task: str = "tts",
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], Path]:
        resolved = resolve_user_path(model_path)
        if resolved is None:
            raise AudioCppTtsRequestError("请先配置本地 TTS 模型文件或目录路径")
        fam = (family or "").strip()
        if not fam:
            raise AudioCppTtsRequestError("请先选择 TTS 模型家族 (family)")
        cfg = self._build_server_config(
            fam,
            resolved,
            compute_backend=compute_backend,
            task=task,
            session_options=session_options,
            load_options=load_options,
        )
        return cfg, resolved

    def _ensure_ready(
        self,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        task: str = "tts",
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg, resolved = self._prepare_cfg(
            family,
            model_path,
            compute_backend=compute_backend,
            task=task,
            session_options=session_options,
            load_options=load_options,
        )
        with self._lock:
            changed = self._write_config(cfg)
            if self._proc is not None and self._proc.poll() is None and self._ready and self._healthy():
                if not changed:
                    return
                self._kill_locked()
                self._spawn_locked(cfg, resolved)
                return
            if self._healthy():
                if not changed:
                    self._ready = True
                    self._proc = None
                    return
                self._kill_port_owner()
            self._kill_locked()
            self._spawn_locked(cfg, resolved)

    def restart(
        self,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        task: str = "tts",
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg, resolved = self._prepare_cfg(
            family,
            model_path,
            compute_backend=compute_backend,
            task=task,
            session_options=session_options,
            load_options=load_options,
        )
        with self._lock:
            self._kill_locked()
            if not self._port_free():
                self._kill_port_owner()
            self._spawn_locked(cfg, resolved)

    def release(self) -> Dict[str, Any]:
        with self._lock:
            proc_alive = self._proc is not None and self._proc.poll() is None
        if not proc_alive and self._port_free():
            return {"released": False, "reason": "not_running"}
        if not self._healthy():
            return {"released": False, "reason": "unhealthy"}
        try:
            r = httpx.post(
                f"{self._base_url()}/v1/tasks/unload_all_models",
                timeout=RELEASE_TIMEOUT,
                trust_env=False,
            )
            if r.status_code == 200:
                return {"released": True}
            return {"released": False, "reason": f"http_{r.status_code}"}
        except Exception as e:
            return {"released": False, "reason": str(e)[:160]}

    def close(self) -> None:
        with self._lock:
            self._kill_locked()

    @staticmethod
    def _build_payload(
        text: str,
        *,
        family: str,
        task: str = "",
        voice: str = "",
        voice_id: str = "",
        speed: Optional[float] = None,
        options: Optional[Dict[str, Any]] = None,
        response_format: str = "wav",
        stream: bool = False,
    ) -> Dict[str, Any]:
        spec = get_spec(family) or {}
        model_id = AudioCppTtsEngine._model_id_for_task(family, task)
        payload: Dict[str, Any] = {
            "model": model_id,
            "input": text,
            "response_format": response_format,
        }
        if stream:
            payload["stream_format"] = "audio"

        cleaned_opts = clean_options(options, spec.get("requestOptions"))

        v_ref = (voice or "").strip()
        v_id = (voice_id or "").strip()
        if v_id:
            if spec.get("supportsVoiceId") and "voice_id" not in cleaned_opts:
                cleaned_opts["voice_id"] = v_id
            elif not v_ref:
                payload["voice"] = v_id
        if v_ref:
            payload["voice"] = v_ref

        if speed is not None and float(speed) > 0:
            payload["speed"] = float(speed)

        if cleaned_opts:
            payload["options"] = cleaned_opts
        return payload

    def _post_wav(self, payload: Dict[str, Any]) -> bytes:
        try:
            r = httpx.post(
                f"{self._base_url()}/v1/audio/speech",
                json=payload,
                timeout=httpx.Timeout(SYNTH_TIMEOUT, connect=5.0),
                trust_env=False,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"audiocpp_server 请求失败: {e}") from e
        if r.status_code == 503 and "insufficient_memory" in r.text:
            raise AudioCppTtsInsufficientMemoryError(f"audiocpp_server {r.status_code}: {r.text[:240]}")
        if r.status_code >= 500:
            raise RuntimeError(f"audiocpp_server {r.status_code}: {r.text[:240]}")
        if r.status_code >= 400:
            raise AudioCppTtsRequestError(f"合成请求被拒 {r.status_code}: {r.text[:240]}")
        if not r.content:
            raise RuntimeError("audiocpp_server 返回空音频")
        return r.content

    def synthesize(
        self,
        text: str,
        *,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        task: str = "tts",
        voice: str = "",
        voice_id: str = "",
        speed: Optional[float] = None,
        options: Optional[Dict[str, Any]] = None,
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        self._ensure_ready(
            family,
            model_path,
            compute_backend=compute_backend,
            task=task,
            session_options=session_options,
            load_options=load_options,
        )
        payload = self._build_payload(
            text,
            family=family,
            task=task,
            voice=voice,
            voice_id=voice_id,
            speed=speed,
            options=options,
            response_format="wav",
            stream=False,
        )
        try:
            return self._post_wav(payload)
        except (AudioCppTtsRequestError, AudioCppTtsInsufficientMemoryError):
            raise
        except Exception as e:
            logger.warning("audiocpp_tts 合成失败(%s)，重启服务后重试一次", e)
            self.restart(
                family,
                model_path,
                compute_backend=compute_backend,
                task=task,
                session_options=session_options,
                load_options=load_options,
            )
            return self._post_wav(payload)

    async def stream(
        self,
        text: str,
        *,
        family: str,
        model_path: str,
        compute_backend: str = "vulkan",
        task: str = "tts",
        voice: str = "",
        voice_id: str = "",
        speed: Optional[float] = None,
        options: Optional[Dict[str, Any]] = None,
        session_options: Optional[Dict[str, Any]] = None,
        load_options: Optional[Dict[str, Any]] = None,
    ):
        """Stream raw s16le PCM bytes from audiocpp_server when the family supports streaming."""
        if not text or not text.strip():
            raise ValueError("text 不能为空")
        spec = get_spec(family)
        if spec is not None and "streaming" not in (spec.get("modes") or []):
            raise AudioCppTtsRequestError(f"{family} 不支持流式合成")
        await asyncio.to_thread(
            self._ensure_ready,
            family,
            model_path,
            compute_backend=compute_backend,
            task=task,
            session_options=session_options,
            load_options=load_options,
        )
        payload = self._build_payload(
            text,
            family=family,
            task=task,
            voice=voice,
            voice_id=voice_id,
            speed=speed,
            options=options,
            response_format="pcm",
            stream=True,
        )

        async def _once():
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(STREAM_READ_TIMEOUT, connect=5.0),
            ) as client:
                async with client.stream(
                    "POST", f"{self._base_url()}/v1/audio/speech", json=payload,
                ) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode("utf-8", "replace")[:300]
                        msg = f"audiocpp_server HTTP {r.status_code}: {detail}"
                        if r.status_code == 503 and "insufficient_memory" in detail:
                            raise AudioCppTtsInsufficientMemoryError(msg)
                        if 400 <= r.status_code < 500:
                            raise AudioCppTtsRequestError(msg)
                        raise RuntimeError(msg)
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            yield chunk

        yielded = False
        try:
            async for chunk in _once():
                yielded = True
                yield chunk
        except (AudioCppTtsRequestError, AudioCppTtsInsufficientMemoryError):
            raise
        except Exception as e:
            if yielded:
                raise
            logger.warning("audiocpp_tts 流式失败(%s)，重启服务后重试一次", e)
            await asyncio.to_thread(
                self.restart,
                family,
                model_path,
                compute_backend=compute_backend,
                task=task,
                session_options=session_options,
                load_options=load_options,
            )
            async for chunk in _once():
                yield chunk


_audiocpp_tts = AudioCppTtsEngine()
atexit.register(_audiocpp_tts.close)

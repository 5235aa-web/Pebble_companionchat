"""
STT engine registry - lazy loading, runtime switching, persistence.

- Engine list: built in. Adding one means appending to _BUILTIN_ENGINES.
- Lazy load: the engine instance is created on first use, because loading the
  model takes seconds and must not block startup. A failure is recorded and the
  caller falls back to the default engine.
- Persistence: stt_providers.json holds the active engine id.
- Thread safety: a lock guards loading so concurrent callers cannot initialise
  the same engine twice.
"""
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import STT_CUSTOM_PATH, STT_PROVIDERS_PATH, model_path, resolve_user_path

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = str(STT_PROVIDERS_PATH)
# Pre-CONFIG_DIR location: written next to this module, which is the PyInstaller
# temp directory once frozen, so the choice was lost on every exit.
LEGACY_CONFIG_FILE = os.path.join(BASE_DIR, "stt_providers.json")

DEFAULT_ENGINE_ID = "qwen3_asr"


def _make_qwen3_asr(override: Optional[Dict[str, str]] = None):
    from . import qwen3asr_stt_engine as mod
    ov = override or {}
    mod.set_override(ov.get("modelPath", ""), ov.get("computeBackend", ""))
    return mod.Qwen3AsrSTT()


def _make_confucius4(override: Optional[Dict[str, str]] = None):
    from . import confucius4_stt_engine as mod
    ov = override or {}
    mod.set_override(ov.get("modelPath", ""), ov.get("computeBackend", ""))
    return mod.Confucius4STT()


_BUILTIN_ENGINES = {
    "qwen3_asr": {
        "id": "qwen3_asr",
        "label": "Qwen3-ASR",
        "desc": "Qwen3-ASR 1.7B Q8 - 30 languages + 22 dialects - Vulkan",
        "family": "qwen3_asr",
        "default_rel": ("qwen3-asr", "qwen3-asr-1.7b-q8_0.gguf"),
        "factory": _make_qwen3_asr,
    },
    "confucius4_r2t2": {
        "id": "confucius4_r2t2",
        "label": "Confucius4-R2T2",
        "desc": "Confucius4-R2T2 Q8 - Chinese/English first - 30 languages - Vulkan",
        "family": "confucius4_r2t2",
        "default_rel": ("Confucius4-R2T2-GGUF", "r2t2-q8_0.gguf"),
        "factory": _make_confucius4,
    },
}


class STTRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._instances = {}
        self._errors = {}
        self._custom_engines: Dict[str, Dict[str, Any]] = {}
        self._overrides: Dict[str, Dict[str, str]] = {}
        self._load_custom_from_file()
        self._active_id = self._load_active_from_file()

    def _known_ids(self) -> set[str]:
        return set(_BUILTIN_ENGINES) | set(self._custom_engines)

    def _load_custom_from_file(self) -> None:
        self._custom_engines = {}
        self._overrides = {}
        try:
            if not STT_CUSTOM_PATH.is_file():
                return
            data = json.loads(STT_CUSTOM_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            raw_ov = data.get("overrides") or {}
            if isinstance(raw_ov, dict):
                for eid, ov in raw_ov.items():
                    if eid in _BUILTIN_ENGINES and isinstance(ov, dict):
                        self._overrides[eid] = {
                            "modelPath": str(ov.get("modelPath") or "").strip(),
                            "computeBackend": str(ov.get("computeBackend") or "").strip(),
                        }
            raw_eng = data.get("engines") or []
            if isinstance(raw_eng, list):
                for item in raw_eng:
                    if not isinstance(item, dict):
                        continue
                    eid = str(item.get("id") or "").strip()
                    if not eid or eid in _BUILTIN_ENGINES:
                        continue
                    kind = str(item.get("kind") or "audiocpp").strip()
                    if kind not in ("audiocpp", "openai_stt"):
                        continue
                    self._custom_engines[eid] = {
                        "id": eid,
                        "label": str(item.get("label") or eid).strip(),
                        "kind": kind,
                        "family": str(item.get("family") or "").strip(),
                        "modelPath": str(item.get("modelPath") or "").strip(),
                        "computeBackend": str(item.get("computeBackend") or "vulkan").strip() or "vulkan",
                        "baseUrl": str(item.get("baseUrl") or "").strip(),
                        "apiKey": str(item.get("apiKey") or "").strip(),
                        "model": str(item.get("model") or "whisper-1").strip() or "whisper-1",
                        "language": str(item.get("language") or "").strip(),
                        "requestOptions": item.get("requestOptions") if isinstance(item.get("requestOptions"), dict) else {},
                        "sessionOptions": item.get("sessionOptions") if isinstance(item.get("sessionOptions"), dict) else {},
                        "loadOptions": item.get("loadOptions") if isinstance(item.get("loadOptions"), dict) else {},
                    }
        except Exception as exc:
            logger.warning("stt custom config unreadable: %s", exc)

    def _save_custom_to_file(self) -> None:
        STT_CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engines": list(self._custom_engines.values()),
            "overrides": self._overrides,
        }
        tmp = str(STT_CUSTOM_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STT_CUSTOM_PATH)

    def _load_active_from_file(self) -> str:
        try:
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                active = str(data.get("active", "")).strip()
                if active in self._known_ids():
                    return active
        except Exception as e:
            logger.warning("stt config unreadable, falling back to default: %s", e)
        return DEFAULT_ENGINE_ID

    def _save_active_to_file(self) -> None:
        try:
            STT_PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"active": self._active_id}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            logger.warning("stt config not persisted: %s", e)

    # ---------- loading ----------
    def _build_engine_instance(self, eid: str):
        if eid in _BUILTIN_ENGINES:
            return _BUILTIN_ENGINES[eid]["factory"](self._overrides.get(eid))
        spec = self._custom_engines.get(eid)
        if spec is None:
            raise KeyError(f"unknown stt engine: {eid}")
        from .audiocpp_stt_engine import AudioCppSttEngine, OpenAICompatSttEngine

        if spec["kind"] == "openai_stt":
            return OpenAICompatSttEngine(
                engine_id=eid,
                label=spec["label"],
                base_url=spec["baseUrl"],
                api_key=spec.get("apiKey", ""),
                model=spec.get("model", "whisper-1"),
                language=spec.get("language", ""),
            )
        return AudioCppSttEngine(
            engine_id=eid,
            label=spec["label"],
            family=spec["family"],
            model_path=spec["modelPath"],
            compute_backend=spec.get("computeBackend", "vulkan"),
            language=spec.get("language", ""),
            request_options=spec.get("requestOptions") or {},
            session_options=spec.get("sessionOptions") or {},
            load_options=spec.get("loadOptions") or {},
        )

    def get_engine(self, engine_id: str | None = None):
        """Return a ready engine instance, creating it on first use."""
        eid = engine_id or self._active_id
        if eid not in self._known_ids():
            eid = DEFAULT_ENGINE_ID
        with self._lock:
            inst = self._instances.get(eid)
            if inst is not None:
                return inst
            try:
                inst = self._build_engine_instance(eid)
            except Exception as e:
                self._errors[eid] = str(e)
                logger.error("stt engine %s failed to load: %s", eid, e)
                if eid != DEFAULT_ENGINE_ID:
                    return self.get_engine(DEFAULT_ENGINE_ID)
                return None
            self._errors.pop(eid, None)
            self._instances[eid] = inst
            return inst

    def preload_async(self, engine_id: str) -> None:
        """Warm the engine in the background so the first recording is not slow."""

        def _run():
            try:
                self.get_engine(engine_id)
            except Exception as e:
                logger.warning("stt preload failed for %s: %s", engine_id, e)

        threading.Thread(target=_run, name=f"stt-preload-{engine_id}", daemon=True).start()

    # ---------- state ----------
    def set_active(self, engine_id: str) -> None:
        if engine_id not in self._known_ids():
            raise KeyError(f"unknown stt engine: {engine_id}")
        with self._lock:
            previous = self._active_id
            self._active_id = engine_id
            self._save_active_to_file()
            old = self._instances.pop(previous, None) if previous != engine_id else None
        # Outside the lock: stopping waits on the child process, and holding the
        # registry lock there would stall every concurrent transcription.
        if old is not None:
            try:
                verdict = old.stop()
            except Exception as e:
                logger.warning("stt: failed to stop %s: %s", previous, e)
                verdict = 'failed'
            if verdict == 'failed':
                # The process is still alive and we just dropped the only handle
                # to it. Put it back so the shutdown sweep can reach it again;
                # otherwise switching engines would orphan a server for good.
                with self._lock:
                    self._instances.setdefault(previous, old)

    @property
    def active_id(self) -> str:
        return self._active_id

    # ---------- unloading ----------
    def stop_engine(self, engine_id: str) -> str:
        """End one engine's server and free the VRAM it holds.

        Returns the engine's own verdict: 'stopped' / 'idle' / 'failed', plus
        'idle' when no instance was ever created. The instance is dropped from
        the cache either way - `loaded` in status() means "this process has an
        instance", and holding a stopped one would make the panel claim a model
        is up. Rebuilding is cheap: the constructor only checks that the binary
        and the weights exist.
        """
        with self._lock:
            inst = self._instances.pop(engine_id, None)
        if inst is None:
            return 'idle'
        try:
            verdict = inst.stop()
        except Exception as e:
            logger.warning("stt: failed to stop %s: %s", engine_id, e)
            verdict = 'failed'
        if verdict == 'failed':
            # The process survived, so keep its handle: dropping it would make
            # the next click report 'idle' while the server is still holding the
            # port, and nothing would ever reach it again.
            with self._lock:
                self._instances.setdefault(engine_id, inst)
        return verdict

    def stop_active(self) -> tuple[str, str]:
        """Stop whichever engine is currently selected. Returns (id, verdict)."""
        return self._active_id, self.stop_engine(self._active_id)

    def stop_all(self) -> list[str]:
        """Stop every engine instance this process created.

        Returns the ids that were actually running, so a caller can log the real
        work rather than every engine it looked at.
        """
        with self._lock:
            ids = list(self._instances)
        return [eid for eid in ids if self.stop_engine(eid) == 'stopped']

    def upsert_engine(self, payload: Dict[str, Any]) -> str:
        """Create/update a custom STT engine or override a built-in engine's path."""
        eid = str(payload.get("id") or "").strip()
        if not eid:
            import time
            eid = f"custom_{int(time.time() * 1000)}"

        if eid in _BUILTIN_ENGINES:
            mp = str(payload.get("modelPath") or "").strip()
            cb = str(payload.get("computeBackend") or "").strip().lower()
            if cb not in ("", "vulkan", "cpu"):
                cb = "vulkan"
            with self._lock:
                if mp or cb:
                    self._overrides[eid] = {"modelPath": mp, "computeBackend": cb}
                else:
                    self._overrides.pop(eid, None)
                self._save_custom_to_file()
                self._errors.pop(eid, None)
            self.stop_engine(eid)
            return eid

        kind = str(payload.get("kind") or "audiocpp").strip()
        if kind not in ("audiocpp", "openai_stt"):
            raise ValueError(f"unsupported stt kind: {kind}")
        label = str(payload.get("label") or eid).strip()
        family = str(payload.get("family") or "").strip()
        mp = str(payload.get("modelPath") or "").strip()
        cb = "cpu" if str(payload.get("computeBackend") or "").strip().lower() == "cpu" else "vulkan"
        base_url = str(payload.get("baseUrl") or "").strip()
        api_key = str(payload.get("apiKey") or "").strip()
        model = str(payload.get("model") or "whisper-1").strip() or "whisper-1"
        language = str(payload.get("language") or "").strip()

        if kind == "audiocpp" and (not family or not mp):
            raise ValueError("audio.cpp ASR 需要填写模型家族 (family) 与模型路径")
        if kind == "openai_stt" and not base_url:
            raise ValueError("HTTP ASR 需要填写服务地址 (baseUrl)")

        entry = {
            "id": eid,
            "label": label,
            "kind": kind,
            "family": family,
            "modelPath": mp,
            "computeBackend": cb,
            "baseUrl": base_url,
            "apiKey": api_key,
            "model": model,
            "language": language,
            "requestOptions": payload.get("requestOptions") if isinstance(payload.get("requestOptions"), dict) else {},
            "sessionOptions": payload.get("sessionOptions") if isinstance(payload.get("sessionOptions"), dict) else {},
            "loadOptions": payload.get("loadOptions") if isinstance(payload.get("loadOptions"), dict) else {},
        }
        with self._lock:
            self._custom_engines[eid] = entry
            self._save_custom_to_file()
            self._errors.pop(eid, None)
        self.stop_engine(eid)
        return eid

    def delete_engine(self, engine_id: str) -> None:
        if engine_id in _BUILTIN_ENGINES:
            with self._lock:
                self._overrides.pop(engine_id, None)
                self._save_custom_to_file()
                self._errors.pop(engine_id, None)
            self.stop_engine(engine_id)
            return
        if engine_id not in self._custom_engines:
            raise KeyError(f"unknown stt engine: {engine_id}")
        self.stop_engine(engine_id)
        with self._lock:
            self._custom_engines.pop(engine_id, None)
            self._errors.pop(engine_id, None)
            self._save_custom_to_file()
            if self._active_id == engine_id:
                self._active_id = DEFAULT_ENGINE_ID
                self._save_active_to_file()

    def status(self) -> dict:
        engines = []
        for eid, spec in _BUILTIN_ENGINES.items():
            ov = self._overrides.get(eid) or {}
            default_path = model_path(*spec["default_rel"])
            eff_path = resolve_user_path(ov.get("modelPath", "")) if ov.get("modelPath") else default_path
            engines.append(
                {
                    "id": eid,
                    "label": spec["label"],
                    "desc": spec["desc"],
                    "kind": "builtin",
                    "builtin": True,
                    "family": spec["family"],
                    "modelPath": ov.get("modelPath", ""),
                    "resolvedModelPath": str(eff_path) if eff_path else "",
                    "defaultModelPath": str(default_path),
                    "modelExists": bool(eff_path and eff_path.exists()),
                    "computeBackend": ov.get("computeBackend") or "vulkan",
                    "loaded": eid in self._instances,
                    "error": self._errors.get(eid),
                }
            )
        for eid, spec in self._custom_engines.items():
            if spec["kind"] == "openai_stt":
                desc = f"HTTP ({spec['baseUrl']}) · {spec['model']}"
                resolved = None
                exists = True
            else:
                resolved = resolve_user_path(spec["modelPath"])
                exists = bool(resolved and resolved.exists())
                desc = f"{spec['family']} · {spec['computeBackend']}"
            engines.append(
                {
                    "id": eid,
                    "label": spec["label"],
                    "desc": desc,
                    "kind": spec["kind"],
                    "builtin": False,
                    "family": spec.get("family", ""),
                    "modelPath": spec.get("modelPath", ""),
                    "resolvedModelPath": str(resolved) if resolved else "",
                    "modelExists": exists,
                    "computeBackend": spec.get("computeBackend", "vulkan"),
                    "baseUrl": spec.get("baseUrl", ""),
                    "apiKey": spec.get("apiKey", ""),
                    "model": spec.get("model", ""),
                    "language": spec.get("language", ""),
                    "requestOptions": spec.get("requestOptions") or {},
                    "sessionOptions": spec.get("sessionOptions") or {},
                    "loadOptions": spec.get("loadOptions") or {},
                    "loaded": eid in self._instances,
                    "error": self._errors.get(eid),
                }
            )
        return {"engines": engines, "active": self._active_id}


def _migrate_legacy_config() -> None:
    """Copy the pre-CONFIG_DIR file into the data root, once.

    Only a source checkout has the old file; a frozen build never will, so this
    is a no-op there. A failed copy is not fatal: the registry falls back to the
    default engine, which is exactly what used to happen on every restart.
    """
    if os.path.isfile(CONFIG_FILE) or not os.path.isfile(LEGACY_CONFIG_FILE):
        return
    try:
        STT_PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LEGACY_CONFIG_FILE, CONFIG_FILE)
        logger.info("stt config migrated to %s", CONFIG_FILE)
    except Exception as e:
        logger.warning("stt config migration failed: %s", e)


_migrate_legacy_config()
_registry = STTRegistry()


def get_registry() -> STTRegistry:
    return _registry

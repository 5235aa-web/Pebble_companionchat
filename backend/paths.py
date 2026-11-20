"""
Single source of truth for every path the backend reads or writes.

A packaged build runs from a temporary extraction directory, so user data can
never live next to the code: it would be destroyed on exit. Everything writable
goes under DATA_ROOT, which defaults to ``~/.pebblecompanion`` and can be moved
with the ``PEBBLE_DATA_ROOT`` environment variable.

Model weights are never bundled. MODELS_ROOT resolves in this order:

1. ``modelsRoot`` in ``<DATA_ROOT>/config/app.json`` (set from the UI)
2. the ``PEBBLE_MODELS_ROOT`` environment variable
3. ``<DATA_ROOT>/models``

Every consumer asks for a path through :func:`model_path` so a change made in
the UI takes effect the next time an engine starts, without touching code.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR_NAME = ".pebblecompanion"


def _is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _default_data_root() -> Path:
    override = os.getenv("PEBBLE_DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / APP_DIR_NAME


DATA_ROOT = _default_data_root()

# --- user data (writable, survives reinstall) ------------------------------
DB_PATH = DATA_ROOT / "companions.db"
UPLOAD_ROOT = DATA_ROOT / "uploads"
ATTACHMENT_DIR = UPLOAD_ROOT / "attachments"
AVATAR_DIR = UPLOAD_ROOT / "companion_avatars"
CONFIG_DIR = DATA_ROOT / "config"
LOG_DIR = DATA_ROOT / "logs"

# TTS reference audio is recorded by the user, so it is data, not a resource.
VOICE_DIR = DATA_ROOT / "voice"

# audiocpp_server writes one config and one log per engine at start-up.
ENGINE_STATE_DIR = DATA_ROOT / "audio-cpp"

# --- config files ----------------------------------------------------------
APP_CONFIG_PATH = CONFIG_DIR / "app.json"
LLM_CONFIG_PATH = CONFIG_DIR / "llm_providers.json"
LOCAL_LLM_CONFIG_PATH = CONFIG_DIR / "local_llm_models.json"
TTS_CONFIG_PATH = CONFIG_DIR / "tts.json"
# Where the voice panel's configs live. Kept in DATA_ROOT, not next to the code:
# a packaged build unpacks to a temp directory that is wiped on exit.
TTS_PROVIDERS_PATH = CONFIG_DIR / "tts_providers.json"
# Same reason: the selected STT engine has to survive a rebuild. It used to be
# written next to stt_registry.py, which is the temp directory when frozen.
STT_PROVIDERS_PATH = CONFIG_DIR / "stt_providers.json"
STT_CUSTOM_PATH = CONFIG_DIR / "stt_custom.json"

DEFAULT_MODELS_ROOT = DATA_ROOT / "models"


def ensure_dirs() -> None:
    """Create every writable directory. Safe to call more than once."""
    for directory in (
        DATA_ROOT,
        UPLOAD_ROOT,
        ATTACHMENT_DIR,
        AVATAR_DIR,
        CONFIG_DIR,
        LOG_DIR,
        VOICE_DIR,
        ENGINE_STATE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def load_app_config() -> Dict[str, Any]:
    """Read app.json, returning an empty dict when it is missing or broken."""
    try:
        with open(APP_CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_config(config: Dict[str, Any]) -> None:
    """Atomically replace app.json so a crash cannot leave it half written."""
    ensure_dirs()
    tmp_path = APP_CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, APP_CONFIG_PATH)


def models_root() -> Path:
    """Resolve the model directory. Re-read on every call so edits take effect."""
    configured = str(load_app_config().get("modelsRoot") or "").strip()
    if configured:
        return Path(configured).expanduser()

    from_env = os.getenv("PEBBLE_MODELS_ROOT", "").strip()
    if from_env:
        return Path(from_env).expanduser()

    return DEFAULT_MODELS_ROOT


def model_path(*parts: str) -> Path:
    """Build a path inside the model root, e.g. ``model_path('gemma', 'x.gguf')``."""
    return models_root().joinpath(*parts)


def resolve_user_path(raw: str) -> Optional[Path]:
    """Resolve a user-supplied model/runtime path.

    Empty strings return None. Relative paths are resolved against
    ``models_root()``, while absolute paths (or ``~``-prefixed ones) are used
    directly.
    """
    text = (raw or "").strip()
    if not text:
        return None
    p = Path(text).expanduser()
    if p.is_absolute():
        return p
    return models_root() / p


def bundle_root() -> Optional[Path]:
    """Directory holding read-only bundled resources (exe, DLLs, model specs)."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def audio_cpp_dir() -> Path:
    """Locate the audio.cpp runtime (audiocpp_server.exe + model_specs + voice).

    Frozen builds put it at <_MEIPASS>/audio-cpp. In source it lives inside
    backend/, one level below bundle_root(), so the bundled location has to be
    probed first and the sibling directory used as the fallback.
    """
    bundled = Path(bundle_root()) / "audio-cpp"
    if (bundled / "audiocpp_server.exe").exists():
        return bundled
    return Path(__file__).resolve().parent / "audio-cpp"


def _find_llama_in(base: Path) -> Optional[Path]:
    """Return a llama.cpp runtime sitting directly inside *base*, if any."""
    if not base or not base.exists():
        return None
    exact = base / "llama"
    if (exact / "llama-server.exe").exists():
        return exact
    for hit in sorted(base.glob("llama-*")):
        if (hit / "llama-server.exe").exists():
            return hit
    return None


def llama_dir() -> Path:
    """Locate the llama.cpp runtime directory.

    Precedence:
    1. ``LLAMA_SERVER_DIR`` environment variable
    2. ``llamaRoot`` in ``<DATA_ROOT>/config/app.json`` (set from the UI)
    3. Auto-discovery in ``models_root()``, its parent, ``DATA_ROOT``, ``bundle_root()``
    """
    env = os.getenv("LLAMA_SERVER_DIR", "").strip()
    if env:
        return Path(env).expanduser()

    configured = str(load_app_config().get("llamaRoot") or "").strip()
    if configured:
        target = Path(configured).expanduser()
        if target.is_file():
            return target.parent
        if (target / "llama-server.exe").exists():
            return target
        hit = _find_llama_in(target)
        if hit:
            return hit
        return target

    for base in (models_root(), models_root().parent, DATA_ROOT, bundle_root()):
        hit = _find_llama_in(base)
        if hit:
            return hit
    return bundle_root() / "llama"


# Called once at import so a fresh install can write before any request arrives.
ensure_dirs()

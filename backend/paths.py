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

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR_NAME = ".pebblecompanion"

# Entry points that ship to users. A process started from one of these is the
# real app, never a test run - even if some library pulled ``unittest`` into
# sys.modules. Without this veto a false positive would move the user's data
# into a temporary directory that gets deleted on exit.
_PRODUCTION_ENTRIES = frozenset({
    "run_backend.py",
    "pebble-backend.exe",
})
_PRODUCTION_MODULES = frozenset({
    "uvicorn",
    "fastapi",
})

_AUDIT_HOOK_INSTALLED = False
_AUDIT_GUARD_STATE = threading.local()


def _is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _real_data_root() -> Path:
    """The install the user actually runs: never a valid target for a test."""
    return Path.home() / APP_DIR_NAME


def _is_real_data_root(candidate: Path) -> bool:
    try:
        return candidate.resolve() == _real_data_root().resolve()
    except OSError:
        return False


def _is_under_real_data_root(candidate: Path) -> bool:
    try:
        real = _real_data_root().resolve()
        resolved = candidate.expanduser().resolve()
        return resolved == real or resolved.is_relative_to(real)
    except OSError:
        return False


def assert_safe_data_path(target: Path) -> Path:
    """Fail loudly if a test process attempts to target the real ~/.pebblecompanion."""
    if _looks_like_test_run() and _is_under_real_data_root(target):
        raise RuntimeError(
            f"refusing test write inside the real data root: {target}"
        )
    return target


def install_test_write_guard() -> bool:
    """Install a permanent PEP 578 audit hook blocking writes/deletes into real ~/.pebblecompanion."""
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return False
    if not _looks_like_test_run() and not os.environ.get("PEBBLE_DATA_ROOT", "").strip():
        return False

    real_prefix = os.path.normcase(os.path.realpath(str(_real_data_root())))
    write_flags = (
        getattr(os, "O_WRONLY", 1)
        | getattr(os, "O_RDWR", 2)
        | getattr(os, "O_CREAT", 0)
        | getattr(os, "O_TRUNC", 0)
        | getattr(os, "O_APPEND", 0)
    )
    write_events = ("os.remove", "os.unlink", "os.rmdir", "os.mkdir", "sqlite3.connect")
    rename_events = ("os.rename", "os.replace")

    def _is_real_path_str(raw: object) -> Optional[str]:
        if isinstance(raw, bytes):
            text = os.fsdecode(raw)
        elif isinstance(raw, str):
            text = raw
        else:
            return None
        s = text.strip()
        if not s or s == ":memory:" or s.startswith("file::memory:"):
            return None
        if s.startswith("file:"):
            s = s[5:].split("?", 1)[0]
            if os.name == "nt" and s.startswith("/") and len(s) > 2 and s[2] == ":":
                s = s[1:]
        resolved = os.path.normcase(os.path.realpath(os.path.abspath(s)))
        if resolved == real_prefix or resolved.startswith(real_prefix + os.sep):
            return resolved
        return None

    def _audit_guard(event: str, args: tuple) -> None:
        if getattr(_AUDIT_GUARD_STATE, "active", False):
            return
        if not _looks_like_test_run():
            return

        if event == "open":
            if len(args) < 3:
                return
            path_arg, mode_arg, flags_arg = args[0], args[1], args[2]
            is_write = (isinstance(mode_arg, str) and any(c in mode_arg for c in "wax+")) or (
                isinstance(flags_arg, int) and bool(flags_arg & write_flags)
            )
            if not is_write:
                return
            targets = (path_arg,)
        elif event in write_events:
            targets = (args[0],) if args else ()
        elif event in rename_events:
            targets = args[:2]
        else:
            return

        _AUDIT_GUARD_STATE.active = True
        try:
            for item in targets:
                hit = _is_real_path_str(item)
                if hit is not None:
                    raise PermissionError(
                        f"Blocked test {event} against real user data root: {hit}"
                    )
        finally:
            _AUDIT_GUARD_STATE.active = False

    sys.addaudithook(_audit_guard)
    _AUDIT_HOOK_INSTALLED = True
    return True


def _looks_like_test_run() -> bool:
    """True when this interpreter is running the test suite.

    The suite redirects the data root through ``PEBBLE_DATA_ROOT``, but that
    redirect lives in ``test/__init__.py`` and therefore only runs when the
    package is imported. A file started directly, a custom ``discover`` script,
    or an IDE runner skips it, and this module would then resolve to the real
    ``~/.pebblecompanion`` - where a destructive fixture once wiped the user's
    configuration.

    Detection is deliberately generous: a false positive only sends the run to
    a throwaway directory, while a false negative lets a test delete real data.
    The signals below cover every launcher seen in practice.
    """
    if os.getenv("PEBBLE_TESTING") == "1":
        return True
    # A frozen build is always the shipped app. Checking it before anything
    # else removes the one false-positive path that would relocate real data.
    if _is_frozen():
        return False
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_ADDOPTS"):
        return True
    if "pytest" in sys.modules:
        return True

    # ``python -m unittest ...`` and any script that passes those words through.
    argv = " ".join(sys.argv).lower()
    if "unittest" in argv or "pytest" in argv:
        return True

    main = sys.modules.get("__main__")
    if main is None:
        return False
    spec = getattr(main, "__spec__", None)
    if spec is not None:
        spec_root = str(getattr(spec, "name", "") or "").split(".")[0].lower()
        if spec_root == "unittest":
            return True
        if spec_root in _PRODUCTION_MODULES:
            return False

    main_file = getattr(main, "__file__", None) or ""
    if main_file:
        resolved = Path(main_file).resolve()
        name = resolved.name.lower()
        # PyCharm ships _jb_unittest_runner.py / _jb_pytest_runner.py.
        if "unittest_runner" in name or "pytest_runner" in name:
            return True
        # The shipped entry points are never test runs, even if a library
        # pulled unittest into sys.modules. Checked before that fallback so a
        # false positive cannot move a user's data into a temp directory.
        if name in _PRODUCTION_ENTRIES and resolved.parent.name != "test":
            return False
        if resolved.parent.name == "test" and resolved.stem.startswith("test_"):
            return True

    # Last resort: a runner that imported unittest without leaving a marker.
    return "unittest" in sys.modules


def _default_data_root() -> Path:
    override = os.getenv("PEBBLE_DATA_ROOT", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
        # An explicit override is honoured, except when a test run aims it at
        # the real install: that is the exact mistake this guard exists to
        # stop, so fail loudly instead of letting the fixture proceed.
        if _looks_like_test_run() and _is_real_data_root(root):
            raise RuntimeError(
                "refusing to run tests against the real data root "
                f"{root}; unset PEBBLE_DATA_ROOT so a temporary root is used"
            )
        return root
    if _looks_like_test_run():
        root = Path(tempfile.mkdtemp(prefix="pebble-test-"))
        os.environ["PEBBLE_DATA_ROOT"] = str(root)
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        return root
    return _real_data_root()


if _looks_like_test_run():
    install_test_write_guard()

DATA_ROOT = _default_data_root()

# --- user data (writable, survives reinstall) ------------------------------
DB_PATH = DATA_ROOT / "companions.db"
UPLOAD_ROOT = DATA_ROOT / "uploads"
ATTACHMENT_DIR = UPLOAD_ROOT / "attachments"
AVATAR_DIR = UPLOAD_ROOT / "companion_avatars"
COMPANION_ASSETS_DIR = UPLOAD_ROOT / "companion_assets"
TMP_UPLOAD_DIR = DATA_ROOT / "tmp_uploads"
CONFIG_DIR = DATA_ROOT / "config"
LOG_DIR = DATA_ROOT / "logs"
OUTPUT_CHAT_DIR = DATA_ROOT / "outputchat"

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
        COMPANION_ASSETS_DIR,
        TMP_UPLOAD_DIR,
        CONFIG_DIR,
        LOG_DIR,
        OUTPUT_CHAT_DIR,
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
    assert_safe_data_path(APP_CONFIG_PATH)
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


def audio_cpp_exe_name() -> str:
    """Platform executable filename for audio.cpp server."""
    return "audiocpp_server.exe" if os.name == "nt" else "audiocpp_server"


def audio_cpp_dir() -> Path:
    """Locate the audio.cpp runtime (audiocpp_server + model_specs + voice).

    Frozen builds put it at <_MEIPASS>/audio-cpp. In source it lives inside
    backend/, one level below bundle_root(), so the bundled location has to be
    probed first and the sibling directory used as the fallback.
    """
    exe_name = audio_cpp_exe_name()
    bundled = Path(bundle_root()) / "audio-cpp"
    if (bundled / exe_name).exists():
        return bundled
    custom = DATA_ROOT / "audio-cpp"
    if (custom / exe_name).exists():
        return custom
    return Path(__file__).resolve().parent / "audio-cpp"


def inochi2d_dir() -> Path:
    """Locate the external Inochi2D/See-through worker scripts and runtime sources.

    Precedence:
    1. ``<DATA_ROOT>/Inochi2D`` (user override for standalone/custom scripts)
    2. ``<_MEIPASS>/Inochi2D`` (bundled scripts + headless see-through in frozen builds)
    3. ``backend/Inochi2D`` in the source tree
    """
    custom = DATA_ROOT / "Inochi2D"
    if (custom / "run_decompose.py").is_file() and (custom / "see-through").is_dir():
        return custom
    bundled = Path(bundle_root()) / "Inochi2D"
    if (bundled / "run_decompose.py").is_file():
        return bundled
    return Path(__file__).resolve().parent / "Inochi2D"


def llama_server_exe_name() -> str:
    """Platform executable filename for llama-server."""
    return "llama-server.exe" if os.name == "nt" else "llama-server"


def ffmpeg_exe_name() -> str:
    """Platform executable filename for ffmpeg."""
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def _resolve_ffmpeg_candidate(candidate: Path) -> Optional[Path]:
    """Return an existing ffmpeg executable from a file or directory path."""
    exe_name = ffmpeg_exe_name()
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        for sub in (candidate / exe_name, candidate / "bin" / exe_name):
            if sub.is_file():
                return sub
    return None


_FFMPEG_USABLE_CACHE: Optional[tuple] = None


def invalidate_ffmpeg_cache() -> None:
    """Clear the cached FFmpeg usability probe result."""
    global _FFMPEG_USABLE_CACHE
    _FFMPEG_USABLE_CACHE = None


def is_ffmpeg_usable(force_refresh: bool = False) -> bool:
    """Return True when a resolved ffmpeg executable runs `ffmpeg -version` cleanly."""
    global _FFMPEG_USABLE_CACHE
    ff = ffmpeg_path()
    if ff is None or not ff.is_file():
        _FFMPEG_USABLE_CACHE = None
        return False

    key = str(ff)
    if not force_refresh and _FFMPEG_USABLE_CACHE is not None and _FFMPEG_USABLE_CACHE[0] == key:
        return bool(_FFMPEG_USABLE_CACHE[1])

    import subprocess

    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    ok = False
    try:
        proc = subprocess.run(
            [key, "-version"],
            capture_output=True,
            timeout=3,
            **kwargs,
        )
        ok = proc.returncode == 0
    except Exception:
        ok = False

    _FFMPEG_USABLE_CACHE = (key, ok)
    return ok


def ffmpeg_path() -> Optional[Path]:
    """Locate the ffmpeg executable, or None when unavailable.

    Precedence:
    1. ``PEBBLE_FFMPEG_PATH`` environment variable
    2. ``ffmpegPath`` in ``<DATA_ROOT>/config/app.json`` (set from the UI)
    3. Bundled/app bin directories (``<DATA_ROOT>/bin``, ``<bundle_root>/bin``)
    4. System ``PATH`` and common Windows package-manager install locations
    """
    env = os.getenv("PEBBLE_FFMPEG_PATH", "").strip()
    if env:
        hit = _resolve_ffmpeg_candidate(Path(env).expanduser())
        if hit:
            return hit

    configured = str(load_app_config().get("ffmpegPath") or "").strip()
    if configured:
        hit = _resolve_ffmpeg_candidate(Path(configured).expanduser())
        if hit:
            return hit

    exe_name = ffmpeg_exe_name()
    for candidate in (
        DATA_ROOT / "bin" / exe_name,
        bundle_root() / "bin" / exe_name,
        bundle_root() / exe_name,
    ):
        if candidate.is_file():
            return candidate

    which = shutil.which("ffmpeg")
    if which:
        p = Path(which)
        if p.is_file():
            return p

    if os.name == "nt":
        local_app = Path(os.getenv("LOCALAPPDATA", "") or Path.home() / "AppData" / "Local")
        program_data = Path(os.getenv("ProgramData", r"C:\ProgramData"))
        win_candidates = [
            local_app / "Microsoft" / "WinGet" / "Links" / exe_name,
            Path.home() / "scoop" / "shims" / exe_name,
            program_data / "chocolatey" / "bin" / exe_name,
            Path(r"C:\ffmpeg\bin") / exe_name,
        ]
        for cand in win_candidates:
            if cand.is_file():
                return cand
        winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            for hit in sorted(winget_pkgs.glob(f"*FFmpeg*/**/{exe_name}")):
                if hit.is_file():
                    return hit

    return None


def _find_llama_in(base: Path) -> Optional[Path]:
    """Return a llama.cpp runtime sitting directly inside *base*, if any."""
    if not base or not base.exists():
        return None
    exe_name = llama_server_exe_name()
    exact = base / "llama"
    if (exact / exe_name).exists():
        return exact
    for hit in sorted(base.glob("llama-*")):
        if (hit / exe_name).exists():
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
        if (target / llama_server_exe_name()).exists():
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

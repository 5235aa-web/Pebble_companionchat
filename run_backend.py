# -*- coding: utf-8 -*-
"""Entry point for the frozen backend.

`python -m uvicorn backend.main:app` is what the dev script runs, and PyInstaller
cannot reproduce it: there is no `-m` at runtime. This module does the same work
in one process - import the ASGI app, hand it to uvicorn.

Logging goes to a file rather than stderr. The shell spawns this process with
CREATE_NO_WINDOW and redirects both streams, so a traceback during import would
otherwise vanish and the only symptom would be a missing route.
"""
import atexit
import ctypes
import logging
import sys
from pathlib import Path


def _setup_logging() -> Path:
    """Mirror every log record into <data root>/logs/backend.log."""
    from backend.paths import LOG_DIR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "backend.log"

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # uvicorn configures its own loggers and stops propagation, so attach the
    # same handler to them directly or its output never reaches the file.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.propagate = False

    return path


def _shutdown_engines() -> None:
    """Kill every local model process this backend spawned.

    Source mode is why this exists: Ctrl+C ends the Python process and would
    otherwise leave audiocpp_server.exe and llama-server.exe running with the
    weights still resident. The engine list lives in backend.engine_shutdown so
    that this and the lifespan hook stop exactly the same set - maintaining a
    second list here is what let the STT engines fall through both.

    Every engine's close() is idempotent, so a half-started engine is harmless.
    """
    from backend.engine_shutdown import shutdown_all

    try:
        shutdown_all()
    except Exception:
        logging.getLogger("run_backend").warning("engine shutdown failed", exc_info=True)


def main() -> None:
    if sys.platform == "win32":
        # The backend sits in front of a GPU-bound LLM. Raising its priority
        # keeps token streaming steady while the webview repaints.
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080
        )

    log = logging.getLogger("run_backend")
    try:
        path = _setup_logging()
    except Exception:
        # Logging must never be the reason the backend fails to start.
        path = None
    log.info("backend starting, log file: %s", path)
    # Also registered here: an exception on the way up would skip the explicit
    # call at the end of main(). close() is idempotent, so both are harmless.
    atexit.register(_shutdown_engines)

    try:
        from backend.main import app
    except Exception:
        log.exception("importing backend.main failed")
        raise

    # The RPC socket is the only way the shell reaches the API. Record whether
    # it actually exists, so a 404 later is unambiguous.
    paths = [getattr(route, "path", "") for route in app.routes]
    log.info("app imported: %d routes, /ws registered: %s", len(paths), "/ws" in paths)

    import uvicorn

    # log_config=None keeps the logging setup above. uvicorn otherwise calls
    # dictConfig with its own handlers, silently replacing the file handler and
    # leaving backend.log with nothing but the import-time lines - which is why
    # a WebSocket connection could fail with no trace anywhere.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8002,
        log_level="info",
        log_config=None,
        access_log=True,
        # uvicorn answers a permessage-deflate request with a bare
        # "Sec-WebSocket-Extensions: permessage-deflate", dropping the
        # client_max_window_bits the client asked about. RFC 7692 requires the
        # server to echo a value, and WebView2 rejects the handshake over it:
        # the socket opens and closes within two milliseconds. Turning the
        # extension off removes the header entirely and the handshake is clean.
        ws_per_message_deflate=False,
    )

    # uvicorn.run returns on Ctrl+C; this is the last chance to release VRAM.
    _shutdown_engines()


if __name__ == "__main__":
    main()

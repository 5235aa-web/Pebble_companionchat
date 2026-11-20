# -*- coding: utf-8 -*-
"""One place that ends every local model process this backend started.

Two callers share it: run_backend.main() after uvicorn returns, and the FastAPI
lifespan when the server shuts down through the normal ASGI path - which is the
one `uvicorn --reload` uses. Each used to keep its own list and the STT engines
were on neither, so a restarted worker left audiocpp_server.exe holding its port
and its VRAM.

Every close() is idempotent and a failure is logged rather than raised: one
engine that cannot stop must not keep the others alive.
"""
import logging
import sys

logger = logging.getLogger('engine_shutdown')

# Module-level engine instances, by (label, module, attribute).
_MODULE_ENGINES = (
    ('omnivoice', 'omnivoice_engine', '_omnivoice'),
    ('dotstts', 'dotstts_engine', '_dotstts'),
    ('fishaudio', 'fishaudio_engine', '_fishaudio'),
    ('ming3tts', 'ming3tts_engine', '_ming3tts'),
    ('audiocpp_tts', 'audiocpp_tts_engine', '_audiocpp_tts'),
    ('llama', 'llama_server_engine', 'llama_server'),
)


def _close_module_engine(label: str, module_name: str, attr: str) -> None:
    package = __package__ or 'backend'
    # Only modules that were actually imported: importing one here would build
    # its engine instance for the first time just to close it, and a module that
    # never loaded cannot have started a process.
    module = sys.modules.get(f'{package}.{module_name}')
    if module is None:
        return
    try:
        getattr(module, attr).close()
    except Exception:
        logger.warning('shutdown %s failed', label, exc_info=True)


def shutdown_stt() -> list[str]:
    """End both STT servers; returns the ids that were actually running.

    Kept apart from the module engines on purpose. `main.py` runs this on every
    ASGI shutdown, which includes the worker swap behind `uvicorn --reload`;
    stopping the TTS and LLM engines there would make every backend edit reload
    8 GiB of LLM weights, for engines that reuse a healthy process instead of
    respawning it. Their exit path is `shutdown_all` plus their own atexit hooks.
    """
    try:
        from .stt_registry import get_registry

        stopped = get_registry().stop_all()
        if stopped:
            logger.info('shutdown: stopped STT engines %s', ', '.join(stopped))
        return stopped
    except Exception:
        logger.warning('shutdown STT failed', exc_info=True)
        return []


def shutdown_all() -> None:
    """End every local model process. Safe to call more than once."""
    for label, module_name, attr in _MODULE_ENGINES:
        _close_module_engine(label, module_name, attr)
    shutdown_stt()

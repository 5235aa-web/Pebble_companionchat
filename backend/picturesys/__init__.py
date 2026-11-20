"""
Picture Studio.

Self-contained image generation: its own config file, its own table, its own
storage folder, and its own prompt builder. Nothing outside this package needs
to know how a provider works; the chat and script hooks only call `maybe_auto`.
"""
from .routes import router

__all__ = ["router"]

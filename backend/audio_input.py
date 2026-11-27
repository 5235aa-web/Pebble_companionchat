"""Shared multimodal audio-input helpers for single and group chat.

Handles local STT pre-transcription into a temporary WAV file, OpenAI-style
`input_audio` message injection, and provider/model rejection caching with
seamless text-only fallback.
"""

import asyncio
import base64
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Providers/models that have already refused an input_audio block, keyed by
# (provider_type, model). Shared across single and group chat so a refusal
# learned in one view prevents wasted probe round-trips in the other.
AUDIO_REJECTED: set = set()

_AUDIO_REJECTION_HINTS = ("input_audio", "audio_url", "input audio")

# Local llama.cpp audio prefill is currently too slow for interactive turns;
# keep local models text-only unless PEBBLE_LLAMA_AUDIO is explicitly enabled.
LOCAL_AUDIO_DISABLED = os.getenv("PEBBLE_LLAMA_AUDIO", "").strip().lower() not in (
    "1",
    "true",
    "yes",
)

_AUDIO_MIME_MAP = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "pcm": "audio/L16",
}


def get_audio_mime(ext: str) -> str:
    """Return the MIME type for an audio file extension."""
    return _AUDIO_MIME_MAP.get((ext or "").lower().lstrip("."), "audio/wav")


def is_audio_rejection(chunk: Optional[Dict[str, Any]]) -> bool:
    """Return True when the provider refused the request because of audio input."""
    if not isinstance(chunk, dict) or chunk.get("type") != "error":
        return False
    text = str(chunk.get("message") or "").lower()
    if not text:
        return False
    if any(hint in text for hint in _AUDIO_REJECTION_HINTS):
        return True
    return "audio" in text and any(
        w in text for w in ("unknown variant", "unsupported", "invalid", "not support")
    )


async def first_or_none(stream):
    """Return the first item of an async iterator, or None if exhausted."""
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return None


def inject_audio_into_messages(
    messages: List[Dict[str, Any]],
    audio_file_path: str,
    prefix_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Inject *audio_file_path* as an OpenAI `input_audio` part on the last user turn.

    Returns a copied message list so the caller's text-only fallback list is
    never mutated in place. When *prefix_text* is supplied (e.g. a group-chat
    speaker label like `"You:"`), it is placed before the audio part without
    adding the STT transcript, keeping speaker attribution intact while letting
    audio-capable models hear tone and pauses directly.
    """
    if not audio_file_path or not os.path.exists(audio_file_path):
        return messages

    copied = [dict(m) for m in messages]
    last_user_idx = -1
    for i in range(len(copied) - 1, -1, -1):
        if copied[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx == -1:
        return copied

    with open(audio_file_path, "rb") as f:
        audio_bytes = f.read()
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    ext = os.path.splitext(audio_file_path)[1].lower().lstrip(".")
    mime = get_audio_mime(ext)
    audio_fmt = ext if ext in ("wav", "mp3", "pcm") else "wav"

    parts: List[Dict[str, Any]] = []
    if prefix_text:
        parts.append({"type": "text", "text": prefix_text})
    parts.append(
        {
            "type": "input_audio",
            "input_audio": {
                "data": f"data:{mime};base64,{audio_b64}",
                "format": audio_fmt,
            },
        }
    )

    copied[last_user_idx]["content"] = parts
    return copied


def prepare_audio_messages(
    messages: List[Dict[str, Any]],
    llm_config: Dict[str, Any],
    audio_file_path: Optional[str],
    prefix_text: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool, Tuple[str, str]]:
    """Prepare live messages and text-only fallback for an audio turn.

    Returns `(send_messages, text_fallback_messages, audio_sent, audio_key)`.
    """
    provider_type = str(llm_config.get("provider_type") or "")
    model = str(llm_config.get("model") or "")
    audio_key = (provider_type, model)
    text_messages = messages
    if not audio_file_path:
        return messages, text_messages, False, audio_key

    local_text_only = provider_type == "llama-cpp" and LOCAL_AUDIO_DISABLED
    if local_text_only:
        logger.info(
            "Audio skipped for local model %s/%s: text only until local audio "
            "prefill is fast enough (see PEBBLE_LLAMA_AUDIO)",
            *audio_key,
        )
        return messages, text_messages, False, audio_key

    if audio_key in AUDIO_REJECTED:
        logger.info("Audio skipped for %s/%s: refused earlier", *audio_key)
        return messages, text_messages, False, audio_key

    injected = inject_audio_into_messages(
        messages, audio_file_path, prefix_text=prefix_text
    )
    logger.info("Audio injected into last user message")
    return injected, text_messages, True, audio_key


async def transcribe_audio_to_temp_wav(
    stt_engine: Any,
    audio_bytes: bytes,
    sample_rate: int = 16000,
) -> Tuple[str, str]:
    """Persist *audio_bytes* to a temporary WAV file and transcribe via *stt_engine*.

    Always writes the temp file so the caller can forward raw audio to
    audio-capable LLMs; STT failures degrade gracefully to `"[语音消息]"`.
    The caller is responsible for unlinking the returned file path.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        loop = asyncio.get_running_loop()
        transcribed = await loop.run_in_executor(
            None, stt_engine.transcribe_bytes, audio_bytes, sample_rate
        )
        text = transcribed.strip() if transcribed else "[语音消息]"
    except Exception as exc:
        logger.error("STT transcription failed: %s", exc)
        text = "[语音消息]"
    return tmp_path, text

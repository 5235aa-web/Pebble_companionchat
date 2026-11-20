"""Realtime call bridge: browser PCM in, transcripts out.

audio.cpp's `/v1/audio/transcriptions/live` buffers 30 seconds before its first
delta and the window is not configurable - the server's own help text advertises
only `model=<id>`, so the `audio_chunk_duration_sec` we used to append was
ignored. A keyword that ends a turn therefore arrived half a minute late, which
made the whole keyword mechanism unusable.

This module no longer uses that endpoint. It keeps the utterance's PCM and
re-transcribes the accumulated audio through the ordinary offline endpoint every
CHUNK_SECONDS. Measured on this machine (Qwen3-ASR 1.7B Q8, Vulkan): 5 s of audio
in 293 ms, 7.7 s in 400 ms - RTF ~0.05, linear in length. Two seconds costs about
120 ms, so the transcript keeps up with the speaker and the keyword lands within
~2.2 s of being said.

The frontend protocol is unchanged: `start` / `stop` / `close` in, `ready` /
`partial` / `final` / `error` out.
"""
import array
import asyncio
import io
import json
import logging
import time
import wave
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .stt_registry import get_registry

logger = logging.getLogger(__name__)
# The root logger defaults to WARNING, so every logger.info in this module was
# silently dropped: the partial trace never reached the console during dev runs.
logger.setLevel(logging.INFO)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2

# How often the accumulated utterance is re-transcribed.
#
# 2 s keeps keyword latency near 2 s while the utterance is short. The cost grows
# linearly with the audio, so a 30 s utterance costs ~1.5 s per pass - still
# inside the interval, which is what keeps the loop from falling behind.
CHUNK_SECONDS = 2.0

# Commit the stabilized prefix once active PCM exceeds this duration, splitting
# at the quietest pause between COMMIT_MIN_S and COMMIT_MAX_S so long turns keep
# bounded transcription cost without truncating earlier words.
SEGMENT_THRESHOLD_SECONDS = 20.0
COMMIT_MIN_SECONDS = 14.0
COMMIT_MAX_SECONDS = 18.0
MAX_UTTERANCE_SECONDS = 180.0

# A single utterance is capped so a stuck client cannot hold a socket forever.
# The wait is only for the tail of one sentence, so it stays well under the
# frontend's own patience: a longer hold would block the next `start`.
SESSION_TIMEOUT_S = 30.0
READY_TIMEOUT_S = 240.0


def _find_quiet_split(pcm: bytes, min_byte: int, max_byte: int) -> int:
    """Return a 2-byte-aligned split offset near the lowest-energy 200ms window."""
    min_b = max(0, min_byte & ~1)
    max_b = min(len(pcm), max_byte & ~1)
    win = (SAMPLE_RATE // 5) * 2  # 200ms in bytes
    if max_b - min_b <= win:
        return max_b
    samples = array.array("h")
    samples.frombytes(pcm[min_b:max_b])
    win_samples = win // 2
    step_samples = max(1, win_samples // 4)
    best_idx = len(samples) - win_samples
    best_energy = None
    for idx in range(0, len(samples) - win_samples + 1, step_samples):
        # Stride by 4 samples for fast energy estimation (< 0.1 ms over 4s).
        energy = sum(abs(samples[j]) for j in range(idx, idx + win_samples, 4))
        if best_energy is None or energy < best_energy:
            best_energy = energy
            best_idx = idx
    center = min_b + (best_idx + win_samples // 2) * 2
    return center & ~1


def _to_wav(pcm: bytes) -> bytes:
    """Wrap s16le mono PCM in a WAV header. The offline endpoint wants a file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class CallSession:
    """One utterance: accumulate PCM, re-transcribe it on a timer."""

    def __init__(self, ws: WebSocket, engine: Any) -> None:
        self.ws = ws
        self.engine = engine
        self.pcm = bytearray()
        self.committed_parts: List[str] = []
        self.committed_bytes = 0
        self.accum = ""
        self.failed = ""
        self._send_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._busy = False
        # Clock origin for the partial trace: tells us how long after the
        # utterance opened text started arriving.
        self.t0 = time.time()

    # ---------- outbound ----------
    async def _send(self, payload: Dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await self.ws.send_json(payload)
            except Exception:  # noqa: BLE001 - socket already gone
                pass

    # ---------- lifecycle ----------
    async def open(self) -> None:
        self._loop_task = asyncio.create_task(self._transcribe_loop())

    async def feed(self, pcm: bytes) -> None:
        """Queue one PCM frame. Nothing is transcribed here."""
        max_total = int(MAX_UTTERANCE_SECONDS * BYTES_PER_SECOND)
        if self.committed_bytes + len(self.pcm) >= max_total:
            return
        self.pcm.extend(pcm)

    async def _maybe_commit_prefix(self) -> None:
        """Freeze the stabilized front segment when active PCM exceeds the rolling threshold."""
        threshold = int(SEGMENT_THRESHOLD_SECONDS * BYTES_PER_SECOND)
        if len(self.pcm) < threshold:
            return
        split = _find_quiet_split(
            bytes(self.pcm),
            int(COMMIT_MIN_SECONDS * BYTES_PER_SECOND),
            int(COMMIT_MAX_SECONDS * BYTES_PER_SECOND),
        )
        if split <= 0:
            return
        prefix_wav = _to_wav(bytes(self.pcm[:split]))
        prefix_text = await asyncio.to_thread(
            self.engine.transcribe_bytes, prefix_wav, SAMPLE_RATE
        )
        prefix_text = (prefix_text or "").strip()
        if prefix_text:
            self.committed_parts.append(prefix_text)
        del self.pcm[:split]
        self.committed_bytes += split

    async def _transcribe_once(self) -> None:
        """Transcribe active audio, commit oversized prefixes cleanly, and push partial."""
        if (not self.pcm and not self.committed_parts) or self._busy:
            return
        self._busy = True
        try:
            await self._maybe_commit_prefix()
            tail_text = ""
            if self.pcm:
                wav = _to_wav(bytes(self.pcm))
                tail_text = await asyncio.to_thread(
                    self.engine.transcribe_bytes, wav, SAMPLE_RATE
                )
                tail_text = (tail_text or "").strip()
            parts = [*self.committed_parts]
            if tail_text:
                parts.append(tail_text)
            text = " ".join(parts).strip()
            if text and text != self.accum:
                self.accum = text
                await self._send({"type": "partial", "text": text})
                logger.info(
                    "[call] partial +%.2fs audio=%.1fs len=%d %r",
                    time.time() - self.t0,
                    (self.committed_bytes + len(self.pcm)) / BYTES_PER_SECOND,
                    len(text),
                    text[-24:],
                )
        finally:
            self._busy = False

    async def _transcribe_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(CHUNK_SECONDS)
            if self._stopped:
                break
            try:
                await self._transcribe_once()
            except Exception as exc:  # noqa: BLE001 - never kill the call over it
                logger.warning("call: transcribe pass failed: %s", exc)

    async def finish(self) -> str:
        """Stop the timer, transcribe the tail once, emit the final transcript."""
        self._stopped = True
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(self._transcribe_once(), timeout=SESSION_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("call: final transcribe failed: %s", exc)
        text = self.accum
        if text:
            await self._send({"type": "final", "text": text})
        return text

    async def close(self) -> None:
        self._stopped = True
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _prepare_engine() -> Any:
    """Start audio.cpp and return the engine that can transcribe."""
    engine = get_registry().get_engine()
    if engine is None:
        raise RuntimeError("语音识别引擎不可用")
    await asyncio.to_thread(engine.ensure_ready)
    return engine


def install(app: FastAPI) -> None:
    """Mount the call bridge on /ws/call."""

    @app.websocket("/ws/call")
    async def _call(ws: WebSocket) -> None:
        await ws.accept()
        session: Optional[CallSession] = None

        async def stop_session() -> None:
            nonlocal session
            if session is None:
                return
            await session.finish()
            await session.close()
            session = None

        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                chunk = message.get("bytes")
                if chunk is not None:
                    if session is not None:
                        await session.feed(chunk)
                    continue

                raw = message.get("text")
                if not raw:
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = frame.get("type")
                if kind == "start":
                    await stop_session()
                    try:
                        engine = await _prepare_engine()
                    except Exception as exc:  # noqa: BLE001
                        await ws.send_json({"type": "error", "message": str(exc)})
                        continue
                    session = CallSession(ws, engine)
                    await session.open()
                    await ws.send_json({"type": "ready"})
                elif kind == "stop":
                    await stop_session()
                elif kind == "close":
                    await stop_session()
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("call bridge failed: %s", exc)
        finally:
            await stop_session()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

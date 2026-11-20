"""
tts_service.py — TTS 引擎集成
本地：OmniVoice / DotTTS / Fish Audio / Ming 3.0 TTS（audio.cpp 常驻子进程）
云端：MiniMax、ElevenLabs
"""
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response as FastAPIResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

logger = logging.getLogger("tts_service")

# ============================================================
# Data Models
# ============================================================

# Reference clip every cloning engine starts on. One name, mapped onto each
# engine's own field, so a fresh install speaks with a real voice instead of
# silently falling back to the engine's built-in default.
DEFAULT_VOICE_REF = "mayathesecond"


class TtsRequest(BaseModel):
    text: str
    provider: str  # omnivoice | dotstts | fishaudio | ming3tts | audiocpp_tts | openai_tts | minimax | elevenlabs
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    voice_name: Optional[str] = None
    voice_id: Optional[str] = None       # MiniMax / ElevenLabs / OpenAI-compat
    emotion: Optional[str] = None        # MiniMax
    speed: Optional[float] = None
    output_format: Optional[str] = None   # ElevenLabs / OpenAI-compat
    # Custom local model path & compute backend for local engines
    model_path: Optional[str] = None
    compute_backend: Optional[str] = None
    # Generic audio.cpp TTS
    audiocpp_family: Optional[str] = None
    audiocpp_task: Optional[str] = None
    audiocpp_options: Optional[Dict[str, Any]] = None
    sample_rate: Optional[int] = None
    speak_stage_directions: Optional[bool] = True
    # OmniVoice
    omni_voice: Optional[str] = None      # 克隆参考音频（voice/ 声音库名）
    omni_instruct: Optional[str] = None   # 演绎指令（voice design 属性串）
    omni_speed: Optional[float] = None
    omni_num_steps: Optional[int] = None
    # DotTTS
    dots_voice: Optional[str] = None
    dots_language: Optional[str] = None
    dots_num_steps: Optional[int] = None
    # Fish Audio S2 Pro
    fish_voice: Optional[str] = None
    fish_temperature: Optional[float] = None
    fish_top_p: Optional[float] = None
    fish_top_k: Optional[int] = None
    # Ming 3.0 TTS（三任务路由 + 26 项官方 request option）
    ming3_task: Optional[str] = None                     # tts | clon | vdes
    ming3_voice: Optional[str] = None                    # 克隆参考音频（voice/ 声音库名）
    ming3_options: Optional[Dict[str, Any]] = None       # template_name / language / instruct 等

    @field_validator(
        "omni_num_steps", "dots_num_steps", "fish_top_k",
        "omni_speed", "speed", "fish_temperature", "fish_top_p",
        "sample_rate",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        # The settings panel stores numbers as strings, so an untouched field
        # arrives as "". Pydantic then fails to parse it and the whole request
        # 422s over a field the engine would have ignored anyway.
        if isinstance(v, str) and not v.strip():
            return None
        return v


class TTSConfigRequest(BaseModel):
    configs: List[Dict[str, Any]]
    activeIndex: int = 0


class TtsReleaseRequest(BaseModel):
    keep: Optional[str] = None


class TtsUnloadRequest(BaseModel):
    """Which local engine to unload. Empty means all of them."""
    engine: str = ""   # 保留的 provider（切换后的目标引擎），其余本地 TTS 引擎释放



# ============================================================
# audio.cpp 本地引擎（OmniVoice / DotTTS / Fish Audio / Ming 3.0 TTS）
# 各自独立一个 audiocpp_server 常驻实例（端口 18244 / 18245 / 18246 / 18247），
# 模型懒加载、闲置 2 分钟由服务端自动卸载释放显存，生命周期在引擎模块内管理。
# ============================================================

from .omnivoice_engine import _omnivoice, set_override as omnivoice_set_override
from .dotstts_engine import _dotstts, set_override as dotstts_set_override
from .fishaudio_engine import _fishaudio, set_override as fishaudio_set_override
from .ming3tts_engine import (
    _ming3tts,
    set_override as ming3_set_override,
    set_reference_cache as ming3_set_reference_cache,
)
from .audiocpp_tts_engine import _audiocpp_tts
from .audio_specs import list_specs as list_audio_specs, stream_sample_rate as spec_stream_sample_rate
from .paths import VOICE_DIR as SHARED_VOICE_DIR, audio_cpp_dir


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def _apply_local_override(req: TtsRequest) -> None:
    mp = req.model_path or ""
    cb = req.compute_backend or ""
    if req.provider == "omnivoice":
        omnivoice_set_override(mp, cb)
    elif req.provider == "dotstts":
        dotstts_set_override(mp, cb)
    elif req.provider == "fishaudio":
        fishaudio_set_override(mp, cb)
    elif req.provider == "ming3tts":
        ming3_set_override(mp, cb)


async def _synthesize_omnivoice(req: TtsRequest) -> bytes:
    '''OmniVoice：克隆参考音频（omni_voice）+ voice design 属性串（omni_instruct）。'''
    _apply_local_override(req)
    return await asyncio.to_thread(
        _omnivoice.synthesize,
        req.text,
        voice=req.omni_voice or "",
        instruct=req.omni_instruct or "",
        speed=req.omni_speed or 0,
        num_steps=req.omni_num_steps or 0,
    )


async def _synthesize_dotstts(req: TtsRequest) -> bytes:
    '''DotTTS：克隆参考音频 + 可选语言锁定（空则自动判定）。'''
    _apply_local_override(req)
    return await asyncio.to_thread(
        _dotstts.synthesize,
        req.text,
        voice=req.dots_voice or "",
        language=req.dots_language or "",
        num_steps=req.dots_num_steps or 0,
    )


async def _synthesize_fishaudio(req: TtsRequest) -> bytes:
    '''Fish Audio S2 Pro：克隆参考音频 + 采样参数。'''
    _apply_local_override(req)
    return await asyncio.to_thread(
        _fishaudio.synthesize,
        req.text,
        voice=req.fish_voice or "",
        temperature=req.fish_temperature or 0,
        top_p=req.fish_top_p or 0,
        top_k=req.fish_top_k or 0,
    )


async def _synthesize_ming3tts(req: TtsRequest) -> bytes:
    '''Ming 3.0 TTS：三任务路由（tts / clon / vdes）+ 26 项官方 request option。'''
    _apply_local_override(req)
    return await asyncio.to_thread(
        _ming3tts.synthesize,
        req.text,
        task=req.ming3_task or "tts",
        voice=req.ming3_voice or "",
        options=req.ming3_options or {},
    )


async def _synthesize_audiocpp_tts(req: TtsRequest) -> bytes:
    '''Generic audio.cpp TTS: any family from model_specs + custom GGUF path.'''
    return await asyncio.to_thread(
        _audiocpp_tts.synthesize,
        req.text,
        family=req.audiocpp_family or "",
        model_path=req.model_path or "",
        compute_backend=req.compute_backend or "vulkan",
        task=req.audiocpp_task or "tts",
        voice=req.voice_name or req.voice_id or "",
        speed=req.speed,
        options=req.audiocpp_options or {},
    )


async def _synthesize_openai_tts(req: TtsRequest) -> tuple[bytes, str]:
    '''OpenAI-compatible /audio/speech endpoint (Kokoro-FastAPI, GPT-SoVITS wrapper, etc.).'''
    base = (req.base_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "OpenAI-compatible TTS requires base_url")
    url = base if base.endswith("/audio/speech") else f"{base}/audio/speech"
    fmt = (req.output_format or "wav").strip().lower() or "wav"
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if req.api_key and req.api_key.strip():
        headers["Authorization"] = f"Bearer {req.api_key.strip()}"
    payload: Dict[str, Any] = {
        "model": (req.model or "tts-1").strip() or "tts-1",
        "input": req.text,
        "voice": (req.voice_id or req.voice_name or "alloy").strip() or "alloy",
        "response_format": fmt,
    }
    if req.speed and float(req.speed) > 0:
        payload["speed"] = float(req.speed)
    trust_env = not any(h in url.lower() for h in _LOCAL_HOSTS)
    async with httpx.AsyncClient(timeout=120.0, trust_env=trust_env) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])
        mime_map = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "opus": "audio/ogg",
            "ogg": "audio/ogg",
            "flac": "audio/flac",
            "aac": "audio/aac",
            "pcm": "audio/L16",
        }
        return resp.content, mime_map.get(fmt, "application/octet-stream")


# ============================================================
# MiniMax (云端 TTS)
# ============================================================

async def _synthesize_minimax(req: TtsRequest) -> bytes:
    base = (req.base_url or "https://api.minimaxi.com/v1").rstrip("/")
    url = f"{base}/t2a_v2"
    headers = {
        "Authorization": f"Bearer {req.api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": req.model or "speech-2.8-turbo",
        "text": req.text,
        "stream": True,
        "output_format": "hex",
        "stream_options": {"exclude_aggregated_audio": True},
        "voice_setting": {
            "voice_id": req.voice_id or "female-shaonv",
            "emotion": req.emotion or "calm",
            "speed": req.speed or 1.0,
        },
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise HTTPException(status_code=resp.status_code, detail=text.decode())
            builder = bytearray()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data_str)
                        audio_hex = obj.get("data", {}).get("audio", "")
                        if audio_hex:
                            builder.extend(bytes.fromhex(audio_hex))
                    except json.JSONDecodeError:
                        continue
            return bytes(builder)


# ============================================================
# ElevenLabs (云端 TTS)
# ============================================================

async def _synthesize_elevenlabs(req: TtsRequest) -> tuple[bytes, str]:
    base = (req.base_url or "https://api.elevenlabs.io").rstrip("/")
    fmt = req.output_format or "mp3_44100_128"
    url = f"{base}/v1/text-to-speech/{req.voice_id}?output_format={fmt}"
    headers = {
        "xi-api-key": req.api_key.strip(),
        "Content-Type": "application/json",
    }
    payload = {
        "text": req.text,
        "model_id": req.model or "eleven_multilingual_v2",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        mime = (
            "audio/mpeg"
            if fmt.startswith("mp3")
            else "audio/ogg" if fmt.startswith("opus")
            else "application/octet-stream"
        )
        return resp.content, mime


# ============================================================
# Config Management
# ============================================================

from .paths import TTS_PROVIDERS_PATH

CONFIG_PATH = TTS_PROVIDERS_PATH

_stored_config: Dict[str, Any] = {}


def _load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load TTS config: {e}")
    return {}


def _save_config(config: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save TTS config: {e}")


_stored_config = _load_config()


# ============================================================
# Router
# ============================================================

router = APIRouter(tags=["tts"])


# 过滤 () 和 （）内的内容（如语气标签、舞台指示）；关闭舞台指示朗读时也过滤 *...*
_PAREN_FILTER = re.compile(r"[（(][^）)]*[）\)]")
_STAGE_DIR_FILTER = re.compile(r"\*[^*\n]*\*")


def _filter_tts_text(text: str, strip_stage_directions: bool = False) -> str:
    cleaned = _PAREN_FILTER.sub("", text)
    if strip_stage_directions:
        cleaned = _STAGE_DIR_FILTER.sub("", cleaned)
    return cleaned.strip()


@router.post("/tts")
async def text_to_speech(req: TtsRequest):
    orig_text = req.text
    req.text = _filter_tts_text(req.text, strip_stage_directions=(req.speak_stage_directions is False))
    if not req.text:
        raise HTTPException(400, f"No text to synthesize after filtering (orig: {orig_text[:50]})")

    if req.provider == "omnivoice":
        audio = await _synthesize_omnivoice(req)
        return FastAPIResponse(content=audio, media_type="audio/wav")

    elif req.provider == "dotstts":
        audio = await _synthesize_dotstts(req)
        return FastAPIResponse(content=audio, media_type="audio/wav")

    elif req.provider == "fishaudio":
        audio = await _synthesize_fishaudio(req)
        return FastAPIResponse(content=audio, media_type="audio/wav")

    elif req.provider == "ming3tts":
        audio = await _synthesize_ming3tts(req)
        return FastAPIResponse(content=audio, media_type="audio/wav")

    elif req.provider == "audiocpp_tts":
        audio = await _synthesize_audiocpp_tts(req)
        return FastAPIResponse(content=audio, media_type="audio/wav")

    elif req.provider == "openai_tts":
        audio, mime = await _synthesize_openai_tts(req)
        return FastAPIResponse(content=audio, media_type=mime)

    elif req.provider == "minimax":
        if not req.api_key:
            raise HTTPException(400, "MiniMax requires api_key")
        audio = await _synthesize_minimax(req)
        return FastAPIResponse(content=audio, media_type="audio/mpeg")

    elif req.provider == "elevenlabs":
        if not req.api_key or not req.voice_id:
            raise HTTPException(400, "ElevenLabs requires api_key and voice_id")
        audio, mime = await _synthesize_elevenlabs(req)
        return FastAPIResponse(content=audio, media_type=mime)

    else:
        raise HTTPException(400, f"Unknown provider: {req.provider}")


# Engines whose stream() emits s16le PCM, with the rate they emit it at.
# audiocpp_tts uses a default rate here and refines it per family / request.
STREAM_SAMPLE_RATES = {
    "omnivoice": 24000,
    "dotstts": 48000,
    "ming3tts": 44100,
    "audiocpp_tts": 24000,
}


def _resolve_stream_sample_rate(req: TtsRequest) -> Optional[int]:
    """Return the PCM sample rate for streaming, or None if streaming is unsupported."""
    if req.provider == "audiocpp_tts":
        family_sr = spec_stream_sample_rate(req.audiocpp_family or "")
        if family_sr is None:
            return None
        if req.sample_rate and int(req.sample_rate) > 0:
            return int(req.sample_rate)
        return family_sr
    return STREAM_SAMPLE_RATES.get(req.provider)


async def _aligned_pcm(chunks):
    """Buffer odd trailing bytes so every yielded PCM chunk aligns on 2-byte s16le samples."""
    carry = b""
    async for chunk in chunks:
        if not chunk:
            continue
        if carry:
            chunk = carry + chunk
            carry = b""
        if len(chunk) & 1:
            carry = chunk[-1:]
            chunk = chunk[:-1]
        if chunk:
            yield chunk


async def _stream_pcm(req: TtsRequest):
    """Yield raw s16le mono PCM as the engine produces it."""
    _apply_local_override(req)
    if req.provider == "omnivoice":
        async for chunk in _omnivoice.stream(
            req.text,
            voice=req.omni_voice or "",
            instruct=req.omni_instruct or "",
            speed=req.omni_speed or 0,
            num_steps=req.omni_num_steps or 0,
        ):
            yield chunk
    elif req.provider == "dotstts":
        async for chunk in _dotstts.stream(
            req.text,
            voice=req.dots_voice or "",
            language=req.dots_language or "",
            num_steps=req.dots_num_steps or 0,
        ):
            yield chunk
    elif req.provider == "audiocpp_tts":
        async for chunk in _audiocpp_tts.stream(
            req.text,
            family=req.audiocpp_family or "",
            model_path=req.model_path or "",
            compute_backend=req.compute_backend or "vulkan",
            task=req.audiocpp_task or "tts",
            voice=req.voice_name or req.voice_id or "",
            speed=req.speed,
            options=req.audiocpp_options or {},
        ):
            yield chunk
    else:
        async for chunk in _ming3tts.stream(
            req.text,
            task=req.ming3_task or "tts",
            voice=req.ming3_voice or "",
            options=req.ming3_options or {},
        ):
            yield chunk


@router.post("/tts/stream")
async def tts_stream(req: TtsRequest):
    """Synthesize as a raw PCM stream so playback can start on the first chunk.

    Only the engines with a real streaming API are served here; everything else
    (Fish Audio, MiniMax, ElevenLabs) must use /tts and accept the wait. The
    format lives in the headers rather than a container, because a container
    would need its length up front — the one thing streaming cannot provide.
    """
    sr = _resolve_stream_sample_rate(req)
    if sr is None:
        raise HTTPException(400, f"{req.provider} has no streaming API")

    req.text = _filter_tts_text(req.text, strip_stage_directions=(req.speak_stage_directions is False))
    if not req.text:
        raise HTTPException(400, "No text to synthesize after filtering")

    # Same rule as the blocking path: the engine about to run reclaims the VRAM
    # of the others, at the moment speech is actually requested.
    await _release_other_local_engines(req.provider)

    return StreamingResponse(
        _aligned_pcm(_stream_pcm(req)),
        media_type="audio/L16",
        headers={
            "X-Sample-Rate": str(sr),
            "X-Channels": "1",
            "X-Sample-Format": "s16le",
            "Cache-Control": "no-store",
        },
    )


@router.get("/tts/specs")
async def list_tts_specs():
    """List every TTS family supported by the bundled audio.cpp runtime."""
    return {"specs": list_audio_specs("tts")}


@router.get("/tts/voices")
async def list_voice_refs():
    """克隆参考音频列表：用户 voice 目录与内置 audio-cpp/voice 下的 wav。"""
    stems: set[str] = set()
    for voice_dir in (SHARED_VOICE_DIR, audio_cpp_dir() / "voice"):
        if voice_dir.is_dir():
            for p in voice_dir.glob("*.wav"):
                stems.add(p.stem)
    return {"voices": sorted(stems)}


@router.post("/tts/unload")
async def unload_tts_engine(req: TtsUnloadRequest):
    """Free the VRAM of one local engine, or every local engine."""
    engines = {
        "omnivoice": _omnivoice,
        "dotstts": _dotstts,
        "fishaudio": _fishaudio,
        "ming3tts": _ming3tts,
        "audiocpp_tts": _audiocpp_tts,
    }
    name = (req.engine or "").strip()
    if not name:
        targets = engines
    elif name in engines:
        targets = {name: engines[name]}
    else:
        # A cloud provider holds no VRAM. Treating it as "unload everything"
        # would silently kill every local engine when the user asks to release
        # the one on screen.
        targets = {}
    released = []
    for key, engine in targets.items():
        try:
            result = await asyncio.to_thread(engine.release)
            if isinstance(result, dict) and result.get("released"):
                released.append(key)
        except Exception as e:
            logger.warning("unload %s failed: %s", key, e)
    return {"released": released, "local": name in engines or not name}


async def _release_other_local_engines(keep: str) -> list[str]:
    """Unload every local engine except *keep*, freeing their VRAM.

    Uses audio.cpp's /v1/tasks/unload_all_models: the process stays up, only
    the weights go. A single engine failing is logged and skipped, never fatal
    — the caller is on its way to synthesizing something.
    """
    engines = {
        "omnivoice": _omnivoice,
        "dotstts": _dotstts,
        "fishaudio": _fishaudio,
        "ming3tts": _ming3tts,
        "audiocpp_tts": _audiocpp_tts,
    }
    released = []
    for name, engine in engines.items():
        if name == keep:
            continue
        try:
            result = await asyncio.to_thread(engine.release)
            if isinstance(result, dict) and result.get("released"):
                released.append(name)
        except Exception as e:
            logger.warning("release_others: %s 释放失败（忽略）: %s", name, e)
    return released


@router.post("/tts/release_others")
async def release_other_tts_engines(req: TtsReleaseRequest):
    """切换 TTS 引擎时释放其余本地引擎的模型显存。

    走 audio.cpp 官方的 /v1/tasks/unload_all_models：进程保留、模型卸载，
    下次请求自动重载。单个引擎失败只记日志，不阻断切换操作。
    云端引擎（minimax / elevenlabs）不在此列。
    """
    keep = (req.keep or "").strip()
    released = await _release_other_local_engines(keep)
    return {"released": released, "keep": keep}


@router.get("/tts/config")
async def get_tts_config():
    if _stored_config and _stored_config.get("configs"):
        return _stored_config
    # No configs is a legitimate state: the panel shows its empty prompt and the
    # user adds an engine with the + button. Returning a placeholder row made the
    # panel look like it held a broken engine literally named "TTS".
    return {"configs": [], "activeIndex": 0}


@router.post("/tts/config")
async def save_tts_config(request: TTSConfigRequest):
    global _stored_config
    _stored_config = {
        "configs": request.configs,
        "activeIndex": request.activeIndex,
    }
    _save_config(_stored_config)

    # Ming3 的 reference_cache 是会话级选项（不是请求参数）：写进引擎，
    # 下次 _ensure_ready 检测到配置内容变更会自动重启服务使其生效。
    try:
        configs = request.configs or []
        if configs:
            idx = min(max(request.activeIndex, 0), len(configs) - 1)
            active = configs[idx] or {}
            if active.get("provider") == "ming3tts":
                ming3_set_reference_cache(bool(active.get("ming3ReferenceCache", True)))
    except Exception as e:
        logger.warning("同步 Ming3 会话选项失败（忽略）: %s", e)

    return {"status": "success"}

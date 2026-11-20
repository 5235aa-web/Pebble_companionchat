# -*- coding: utf-8 -*-
"""Catalogue reader for audio.cpp model_specs/*.json.

audiocpp_server ships over 130 model-family JSON descriptors under
``audio-cpp/model_specs``. Reading them dynamically lets users pick any
supported TTS or ASR family without hardcoding every single model in Python.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import audio_cpp_dir

logger = logging.getLogger("audio_specs")

# Default sample rates for audio.cpp streaming TTS families where known.
STREAM_SAMPLE_RATES_BY_FAMILY: Dict[str, int] = {
    "omnivoice": 24000,
    "dots_tts": 48000,
    "ming3_tts": 44100,
    "fish_audio": 44100,
    "kokoro_tts": 24000,
    "qwen3_tts": 24000,
}
DEFAULT_STREAM_SAMPLE_RATE = 24000


def _specs_dir() -> Path:
    return audio_cpp_dir() / "model_specs"


@lru_cache(maxsize=1)
def _load_all_specs() -> Dict[str, Dict[str, Any]]:
    specs_dir = _specs_dir()
    out: Dict[str, Dict[str, Any]] = {}
    if not specs_dir.is_dir():
        return out
    for path in sorted(specs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("skip unreadable model spec %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        family = str(data.get("family") or path.stem).strip()
        if not family:
            continue
        req_opts: List[Dict[str, Any]] = []
        raw_req = ((data.get("options") or {}).get("request")) or []
        if isinstance(raw_req, list):
            for opt in raw_req:
                if not isinstance(opt, dict):
                    continue
                name = str(opt.get("name") or "").strip()
                if not name:
                    continue
                req_opts.append({
                    "name": name,
                    "type": str(opt.get("type") or "string"),
                    "description": str(opt.get("description") or ""),
                    "required": bool(opt.get("required", False)),
                    "default": opt.get("default"),
                    "values": opt.get("values") if isinstance(opt.get("values"), list) else None,
                    "min": opt.get("min"),
                    "max": opt.get("max"),
                })
        tasks = [str(t) for t in (data.get("tasks") or []) if str(t).strip()]
        modes = [str(m) for m in (data.get("modes") or []) if str(m).strip()]
        category = str(data.get("category") or "").strip().lower()
        sample_rate = STREAM_SAMPLE_RATES_BY_FAMILY.get(
            family,
            DEFAULT_STREAM_SAMPLE_RATE if "streaming" in modes else None,
        )
        out[family] = {
            "family": family,
            "displayName": str(data.get("display_name") or family),
            "description": str(data.get("description") or ""),
            "category": category,
            "status": str(data.get("status") or ""),
            "tasks": tasks,
            "modes": modes,
            "sampleRate": sample_rate,
            "languages": [str(l) for l in (data.get("languages") or [])[:12]],
            "capabilities": data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {},
            "requestOptions": req_opts,
        }
    return out


def clear_cache() -> None:
    _load_all_specs.cache_clear()


def get_spec(family: str) -> Optional[Dict[str, Any]]:
    return _load_all_specs().get((family or "").strip())


def stream_sample_rate(family: str) -> Optional[int]:
    """Return the streaming PCM sample rate for a TTS family, or None if offline-only."""
    fam = (family or "").strip()
    if not fam:
        return DEFAULT_STREAM_SAMPLE_RATE
    spec = get_spec(fam)
    if spec is not None and "streaming" not in (spec.get("modes") or []):
        return None
    return STREAM_SAMPLE_RATES_BY_FAMILY.get(fam, DEFAULT_STREAM_SAMPLE_RATE)


def list_specs(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return specs filtered by category ('tts' or 'asr'), sorted by displayName."""
    specs = list(_load_all_specs().values())
    if category:
        cat = category.strip().lower()
        specs = [
            s for s in specs
            if s["category"] == cat or cat in s["tasks"]
        ]
    # Keep supported models before community ones, then alphabetically.
    specs.sort(key=lambda s: (0 if s["status"] == "supported" else 1, s["displayName"].lower()))
    return specs

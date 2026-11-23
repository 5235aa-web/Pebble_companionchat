# -*- coding: utf-8 -*-
"""Catalogue reader and option normalizer for audio.cpp ``model_specs/*.json``.

audiocpp_server ships 130+ model-family JSON descriptors under
``audio-cpp/model_specs``. Reading all three option scopes (``request``,
``session``, ``load``), expanding ``preset`` enums, normalizing task names, and
exposing path/sample-rate metadata lets the generic TTS and ASR engines run any
user-installed model without per-model Python glue.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import audio_cpp_dir

logger = logging.getLogger("audio_specs")

# Official enum presets from audio.cpp src/framework/model_spec/options.cpp.
OPTION_PRESETS: Dict[str, List[str]] = {
    "best_of_n_language": ["auto", "en", "ja"],
    "perf_mode_flash_attention": ["off", "flash_attention"],
    "text_chunk_mode_full": ["word_budget", "tag_aware", "japanese", "endline"],
    "weight_type_codec_q8": ["native", "f32", "f16", "q8_0"],
    "weight_type_conv": ["native", "f32", "f16"],
    "weight_type_full": ["native", "f32", "f16", "bf16", "q8_0"],
}

# Mapping between model_specs task names and audiocpp_server / CLI 4-char task codes.
SPEC_TO_SERVER_TASK: Dict[str, str] = {
    "clone": "clon",
    "design": "vdes",
    "classify": "cls",
    "wake_word": "wake",
    "music": "gen",
    "sfx": "gen",
    "edit": "gen",
}
SERVER_TO_SPEC_TASK: Dict[str, str] = {
    "clon": "clone",
    "vdes": "design",
    "cls": "classify",
    "wake": "wake_word",
}

# Default output sample rates (Hz) for every streaming-capable TTS family.
# audiocpp_server streams raw s16le PCM without an X-Sample-Rate header, so
# every streaming family's native decoder rate must be listed here.
STREAM_SAMPLE_RATES_BY_FAMILY: Dict[str, int] = {
    "audio8_tts": 44100,
    "breeze_tts": 24000,
    "confucius4_tts": 22050,
    "dots_tts": 48000,
    "fish_audio": 44100,
    "fish_drama3": 44100,
    "gradium_phonon": 24000,
    "higgs_audio_tts": 24000,
    "index_tts2": 22050,
    "irodori_tts": 48000,
    "kokoro_tts": 24000,
    "kugelaudio": 24000,
    "lfm2_audio": 24000,
    "lfm3_audio": 24000,
    "m5_tts": 24000,
    "ming3_tts": 44100,
    "mira_tts": 48000,
    "moss_tts_nano": 48000,
    "neutts": 24000,
    "omnivoice": 24000,
    "personaplex": 24000,
    "plapre": 24000,
    "pocket_tts": 24000,
    "pocket_tts_small": 24000,
    "qwen3_tts": 24000,
    "soprano_tts": 32000,
    "sopro_tts": 24000,
    "supertonic": 44100,
    "vibevoice": 24000,
    "voxcpm1": 16000,
    "voxcpm2": 48000,
    "voxtral_tts": 24000,
    "zipvoice": 24000,
    "zonos2": 44100,
    "zonos3": 44100,
}
DEFAULT_STREAM_SAMPLE_RATE = 24000

_TTS_SERVER_TASK_ORDER = ("tts", "clon", "vdes")


def _specs_dir() -> Path:
    return audio_cpp_dir() / "model_specs"


def _parse_option_list(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for opt in raw:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name") or "").strip()
        if not name:
            continue
        preset = str(opt.get("preset") or "").strip() or None
        raw_values = opt.get("values")
        if isinstance(raw_values, list):
            values: Optional[List[Any]] = list(raw_values)
        elif preset and preset in OPTION_PRESETS:
            values = list(OPTION_PRESETS[preset])
        else:
            values = None
        out.append({
            "name": name,
            "type": str(opt.get("type") or "string"),
            "description": str(opt.get("description") or ""),
            "required": bool(opt.get("required", False)),
            "default": opt.get("default"),
            "values": values,
            "preset": preset,
            "min": opt.get("min"),
            "max": opt.get("max"),
        })
    return out


def _infer_path_kind(data: Dict[str, Any], session_opts: List[Dict[str, Any]]) -> str:
    """Infer whether a family expects a single GGUF file, a model folder, or either."""
    session_names = {o["name"] for o in session_opts}
    # Families whose session_options pick component GGUF filenames inside a folder
    # (e.g. yue2.model_gguf, lfm2_audio.model_gguf, auk.model_gguf) require a directory.
    if any(n.endswith("_gguf") for n in session_names):
        return "dir"

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        return "either"

    supports_file = False
    supports_dir = False
    for src in sources:
        if not isinstance(src, dict):
            continue
        fmt = str(src.get("format") or "").lower()
        roots = src.get("roots") or {}
        files = src.get("files") or {}
        tensors = src.get("tensors") or {}
        if fmt == "gguf":
            # A self-contained GGUF embeds its sidecars and stores tensors under $gguf.
            has_gguf_root = any(v == "$gguf" for v in roots.values())
            if has_gguf_root and not files and len(tensors) <= 2:
                supports_file = True
            else:
                # Some GGUF packages embed config/tokenizer inside the GGUF at runtime
                # (like omnivoice/ming3), while others require a folder layout.
                supports_file = True
                supports_dir = True
        else:
            supports_dir = True

    if supports_file and supports_dir:
        return "either"
    if supports_dir and not supports_file:
        return "dir"
    if supports_file:
        return "file"
    return "either"


def _compute_server_tasks(spec_tasks: List[str], category: str) -> List[str]:
    """Return ordered audiocpp_server task codes supported by this family."""
    mapped: List[str] = []
    for t in spec_tasks:
        code = SPEC_TO_SERVER_TASK.get(t, t)
        if code and code not in mapped:
            mapped.append(code)
    if category == "tts" or "tts" in mapped:
        tts_codes = [c for c in _TTS_SERVER_TASK_ORDER if c in mapped]
        if tts_codes:
            return tts_codes
        return ["tts"]
    if category == "asr" or "asr" in mapped:
        return [c for c in mapped if c in ("asr", "align")] or ["asr"]
    return mapped or ["tts"]


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
        options = data.get("options") or {}
        req_opts = _parse_option_list(options.get("request"))
        sess_opts = _parse_option_list(options.get("session"))
        load_opts = _parse_option_list(options.get("load"))

        tasks = [str(t).strip() for t in (data.get("tasks") or []) if str(t).strip()]
        modes = [str(m).strip() for m in (data.get("modes") or []) if str(m).strip()]
        category = str(data.get("category") or "").strip().lower()
        sample_rate = STREAM_SAMPLE_RATES_BY_FAMILY.get(
            family,
            DEFAULT_STREAM_SAMPLE_RATE if "streaming" in modes else None,
        )
        server_tasks = _compute_server_tasks(tasks, category)
        req_names = {o["name"] for o in req_opts}
        caps = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}

        out[family] = {
            "family": family,
            "displayName": str(data.get("display_name") or family),
            "description": str(data.get("description") or ""),
            "category": category,
            "status": str(data.get("status") or ""),
            "tasks": tasks,
            "serverTasks": server_tasks,
            "modes": modes,
            "sampleRate": sample_rate,
            "pathKind": _infer_path_kind(data, sess_opts),
            "supportsVoiceId": "voice_id" in req_names,
            "supportsReferenceText": "reference_text" in req_names,
            "languages": [str(l) for l in (data.get("languages") or [])],
            "capabilities": caps,
            "requestOptions": req_opts,
            "sessionOptions": sess_opts,
            "loadOptions": load_opts,
        }
    return out


def clear_cache() -> None:
    _load_all_specs.cache_clear()


def get_spec(family: str) -> Optional[Dict[str, Any]]:
    return _load_all_specs().get((family or "").strip())


def normalize_server_task(family: str, requested: str = "") -> str:
    """Resolve a requested task (e.g. 'clone'/'clon'/'design'/'vdes') to a valid server task code."""
    spec = get_spec(family)
    raw = (requested or "").strip().lower()
    if raw:
        mapped = SPEC_TO_SERVER_TASK.get(raw, raw)
    else:
        mapped = ""

    if spec is None:
        return mapped or "tts"

    supported = spec.get("serverTasks") or ["tts"]
    if mapped and mapped in supported:
        return mapped
    return supported[0]


def stream_sample_rate(family: str) -> Optional[int]:
    """Return the streaming PCM sample rate for a TTS family, or None if offline-only."""
    fam = (family or "").strip()
    if not fam:
        return DEFAULT_STREAM_SAMPLE_RATE
    spec = get_spec(fam)
    if spec is not None and "streaming" not in (spec.get("modes") or []):
        return None
    return STREAM_SAMPLE_RATES_BY_FAMILY.get(fam, DEFAULT_STREAM_SAMPLE_RATE)


def _coerce_option_value(value: Any, opt_type: str) -> Tuple[bool, Any]:
    """Coerce a raw UI/API option value to the spec's declared type."""
    if value is None:
        return False, None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return False, None

    if opt_type == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, (int, float)):
            return True, bool(value)
        if isinstance(value, str):
            low = value.lower()
            if low in ("true", "1", "yes", "on"):
                return True, True
            if low in ("false", "0", "no", "off"):
                return True, False
        return False, None

    if opt_type == "int":
        try:
            if isinstance(value, bool):
                return False, None
            return True, int(float(value)) if isinstance(value, str) and "." in value else int(value)
        except (TypeError, ValueError):
            return False, None

    if opt_type == "float":
        try:
            if isinstance(value, bool):
                return False, None
            return True, float(value)
        except (TypeError, ValueError):
            return False, None

    if opt_type in ("string_list", "path_list", "float_list"):
        if isinstance(value, list):
            items = [str(x).strip() for x in value if str(x).strip()]
        elif isinstance(value, str):
            items = [x.strip() for x in value.split(",") if x.strip()]
        else:
            return False, None
        if not items:
            return False, None
        if opt_type == "float_list":
            try:
                return True, [float(x) for x in items]
            except ValueError:
                return False, None
        return True, items

    # string / enum / path / audio_path
    s = str(value).strip()
    return (True, s) if s else (False, None)


def clean_options(
    raw: Optional[Dict[str, Any]],
    declared: Optional[List[Dict[str, Any]]],
    *,
    prefix_family: str = "",
) -> Dict[str, Any]:
    """Filter and coerce raw options against a spec's declared option list.

    When ``prefix_family`` is non-empty (used for ``models[].session_options``),
    local option names are prefixed as ``<family>.<name>`` per audio.cpp schema.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    by_name = {o["name"]: o for o in (declared or []) if isinstance(o, dict) and o.get("name")}
    out: Dict[str, Any] = {}
    fam_prefix = f"{prefix_family}." if prefix_family else ""

    for raw_key, val in raw.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        local_key = key[len(fam_prefix):] if fam_prefix and key.startswith(fam_prefix) else key
        if by_name and local_key not in by_name:
            continue
        opt_def = by_name.get(local_key) or {}
        ok, coerced = _coerce_option_value(val, str(opt_def.get("type") or "string"))
        if not ok:
            continue
        out_key = f"{fam_prefix}{local_key}" if fam_prefix and "." not in local_key else local_key
        out[out_key] = coerced
    return out


def list_specs(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return specs filtered by category ('tts' or 'asr'), sorted by displayName."""
    specs = list(_load_all_specs().values())
    if category:
        cat = category.strip().lower()
        specs = [
            s for s in specs
            if s["category"] == cat or cat in s["tasks"]
        ]
    # Keep supported models before community/experimental ones, then alphabetically.
    specs.sort(key=lambda s: (0 if s["status"] == "supported" else 1, s["displayName"].lower()))
    return specs

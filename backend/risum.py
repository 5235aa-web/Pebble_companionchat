"""risum.py - RisuAI `.risum` (RPack) module parser and companion importer.

Implements the official RisuAI RPack binary container format:
- Byte 0: magic `111` (0x6F)
- Byte 1: version `0`
- u32 LE length + RPack-substituted UTF-8 JSON (`{"type": "risuModule", "module": ...}`)
- Repeated asset blocks: mark `1` + u32 LE length + RPack-substituted asset bytes,
  terminated by mark `0`.

Also accepts plain JSON `module.json` (`{"type": "risuModule", ...}`) for unpacked
modules bundled inside `.charx` archives.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

from . import paths
from .card_presentation import (
    extract_background_css,
    extract_card_presentation,
    extract_display_regex,
    extract_input_regex,
    extract_output_regex,
    extract_prompt_regex,
    parse_default_variables,
)
from .charx import (
    classify_media_kind,
    infer_ext,
    infer_mime,
)
from .companion_db import (
    add_companion_assets,
    get_companion,
    get_companion_assets,
    update_companion,
)
from .rpack import (
    RPACK_MAGIC,
    RPACK_VERSION,
    RisumError,
    decode_rpack,
    encode_rpack,
    parse_risum_bytes,
)


def _safe_stem(name: str, fallback: str = "asset") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (name or "").strip()).strip(". ")
    return cleaned[:80] or fallback


def extract_risum_payload(raw: bytes) -> Dict[str, Any]:
    """Extract Display Regex rules, CSS, default variables, and asset blobs."""
    module, blobs = parse_risum_bytes(raw)

    synthetic_card = {
        "data": {
            "extensions": {
                "risuai": {
                    "customScripts": module.get("regex") if isinstance(module.get("regex"), list) else [],
                    "backgroundHTML": module.get("backgroundEmbedding") or "",
                    "defaultVariables": module.get("defaultVariables") or "",
                }
            }
        }
    }
    display_regex = extract_display_regex(synthetic_card)
    output_regex = extract_output_regex(synthetic_card)
    input_regex = extract_input_regex(synthetic_card)
    prompt_regex = extract_prompt_regex(synthetic_card)
    card_css = extract_background_css(synthetic_card)
    default_vars = parse_default_variables(synthetic_card)

    raw_assets = module.get("assets") if isinstance(module.get("assets"), list) else []
    asset_entries: List[Tuple[str, str, bytes]] = []
    for idx, blob in enumerate(blobs):
        if not blob:
            continue
        meta = raw_assets[idx] if idx < len(raw_assets) and isinstance(raw_assets[idx], (list, tuple)) else None
        a_name = str(meta[0] if meta and len(meta) >= 1 and meta[0] else f"module_asset_{idx}").strip()
        a_ext = str(meta[2] if meta and len(meta) >= 3 and meta[2] else "").strip().lstrip(".").lower()
        if not a_ext:
            a_ext = infer_ext(a_name, "bin")
            if a_ext == "bin":
                if blob.startswith(b"\x89PNG\r\n\x1a\n"):
                    a_ext = "png"
                elif blob.startswith(b"\xff\xd8\xff"):
                    a_ext = "jpg"
                elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                    a_ext = "webp"
        asset_entries.append((a_name, a_ext, blob))

    return {
        "name": str(module.get("name") or "Risu Module"),
        "display_regex": display_regex,
        "output_regex": output_regex,
        "input_regex": input_regex,
        "prompt_regex": prompt_regex,
        "card_css": card_css,
        "default_variables": default_vars,
        "assets": asset_entries,
    }


def _merge_rule_bucket(
    existing_raw: Any,
    incoming_raw: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """Append non-duplicate regex rules keyed by ``(pattern, flags, replace)``."""
    merged = list(existing_raw or [])
    seen: Set[Tuple[str, str, str]] = {
        (
            str(r.get("pattern") or ""),
            str(r.get("flags") or "g"),
            str(r.get("replace") or ""),
        )
        for r in merged
        if isinstance(r, dict)
    }
    added = 0
    for rule in incoming_raw or []:
        if not isinstance(rule, dict):
            continue
        sig = (
            str(rule.get("pattern") or ""),
            str(rule.get("flags") or "g"),
            str(rule.get("replace") or ""),
        )
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(rule)
        added += 1
    return merged, added


def merge_risum_into_companion(companion_id: str, raw: bytes) -> Dict[str, Any]:
    """Import a `.risum` module into an existing companion's `card_ui` & assets."""
    companion = get_companion(companion_id)
    if not companion:
        raise RisumError("Companion not found")

    parsed = extract_risum_payload(raw)
    current_ui = dict(companion.get("card_ui") or {})
    current_pres = extract_card_presentation(companion.get("raw_card_data", ""), current_ui)

    existing_display, added_disp = _merge_rule_bucket(
        current_pres.get("display_regex"), parsed.get("display_regex")
    )
    existing_output, added_out = _merge_rule_bucket(
        current_pres.get("output_regex"), parsed.get("output_regex")
    )
    existing_input, added_in = _merge_rule_bucket(
        current_pres.get("input_regex"), parsed.get("input_regex")
    )
    existing_prompt, added_prompt = _merge_rule_bucket(
        current_pres.get("prompt_regex"), parsed.get("prompt_regex")
    )
    added_regex = added_disp + added_out + added_in + added_prompt

    # Merge CSS if non-empty and not already contained.
    current_css = str(current_pres.get("card_css") or "").strip()
    module_css = str(parsed["card_css"] or "").strip()
    added_css = False
    if module_css and module_css not in current_css:
        current_css = f"{current_css}\n\n{module_css}".strip() if current_css else module_css
        added_css = True

    # Merge default variables without overwriting user overrides unless absent.
    merged_defaults = dict(current_pres.get("default_variables") or {})
    for k, v in (parsed["default_variables"] or {}).items():
        merged_defaults.setdefault(k, v)

    next_ui = dict(current_ui)
    next_ui["display_regex"] = existing_display
    if existing_output or "output_regex" in current_ui:
        next_ui["output_regex"] = existing_output
    if existing_input or "input_regex" in current_ui:
        next_ui["input_regex"] = existing_input
    if existing_prompt or "prompt_regex" in current_ui:
        next_ui["prompt_regex"] = existing_prompt
    next_ui["card_css"] = current_css
    if merged_defaults:
        next_ui["default_variables"] = merged_defaults
    update_companion(companion_id, {"card_ui": next_ui})

    # Store decoded module assets on disk and index in companion_assets.
    existing_assets = get_companion_assets(companion_id)
    existing_names = {str(a.get("name") or "").lower() for a in existing_assets}
    next_order = len(existing_assets)
    assets_root = paths.COMPANION_ASSETS_DIR / companion_id / "x-risu-asset"
    data_root_resolved = paths.DATA_ROOT.resolve()
    to_insert: List[Dict[str, Any]] = []

    for a_name, a_ext, blob in parsed["assets"]:
        if a_name.lower() in existing_names:
            continue
        existing_names.add(a_name.lower())
        assets_root.mkdir(parents=True, exist_ok=True)
        dest_path = assets_root / f"risum_{next_order:04d}_{_safe_stem(a_name)}.{a_ext}"
        dest_path.write_bytes(blob)
        rel_path = dest_path.resolve().relative_to(data_root_resolved).as_posix()
        to_insert.append({
            "asset_type": classify_media_kind(a_ext, "x-risu-asset"),
            "name": a_name,
            "ext": a_ext,
            "mime_type": infer_mime(a_ext),
            "rel_path": rel_path,
            "source_uri": f"embeded://assets/risum/{dest_path.name}",
            "size_bytes": len(blob),
            "sort_order": next_order,
        })
        next_order += 1

    if to_insert:
        add_companion_assets(companion_id, to_insert)

    updated = get_companion(companion_id)
    return {
        "status": "success",
        "module_name": parsed["name"],
        "added_regex": added_regex,
        "added_css": added_css,
        "added_assets": len(to_insert),
        "card_presentation": (updated or {}).get("card_presentation"),
    }

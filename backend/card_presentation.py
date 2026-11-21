"""Card presentation metadata extraction (CBS defaults, Display Regex, Card CSS).

Normalizes RisuAI (``extensions.risuai``) and SillyTavern (``extensions.regex_scripts``)
metadata from ``raw_card_data`` alongside optional per-companion overrides stored in
``companions.card_ui``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>([\s\S]*?)</style>", re.IGNORECASE)
_SLASH_REGEX_RE = re.compile(r"^/([\s\S]*)/([a-zA-Z]*)$")
_SUPPORTED_JS_FLAGS = frozenset("gimsuyd")


def _safe_json_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _card_extensions(raw_card: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    data = raw_card.get("data") if isinstance(raw_card.get("data"), dict) else raw_card
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    risu = ext.get("risuai") if isinstance(ext.get("risuai"), dict) else {}
    return ext, risu


def parse_default_variables(raw_card: Dict[str, Any]) -> Dict[str, str]:
    """Extract default CBS variables from RisuAI / Pebble card extensions."""
    ext, risu = _card_extensions(raw_card)
    out: Dict[str, str] = {}

    raw_vars = risu.get("defaultVariables")
    if isinstance(raw_vars, str):
        for line in raw_vars.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            k = key.strip()
            if k:
                out[k] = val.strip()
    elif isinstance(raw_vars, dict):
        for k, v in raw_vars.items():
            key = str(k).strip()
            if key:
                out[key] = "" if v is None else str(v)

    pebble_vars = ext.get("pebble_variables")
    if isinstance(pebble_vars, dict):
        for k, v in pebble_vars.items():
            key = str(k).strip()
            if key:
                out[key] = "" if v is None else str(v)

    return out


def _split_regex_literal(raw_pattern: str, fallback_flags: str = "g") -> Tuple[str, str]:
    text = str(raw_pattern or "")
    m = _SLASH_REGEX_RE.match(text)
    if m:
        pattern = m.group(1)
        flags = "".join(ch for ch in m.group(2) if ch in _SUPPORTED_JS_FLAGS)
        return pattern, flags or fallback_flags
    flags = "".join(ch for ch in str(fallback_flags or "g") if ch in _SUPPORTED_JS_FLAGS)
    return text, flags or "g"


def normalize_display_regex_list(rules: Any) -> List[Dict[str, Any]]:
    """Normalize arbitrary user/card display-regex descriptors into canonical dicts."""
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(rules):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "")
        if not pattern:
            continue
        raw_flags = str(item.get("flags") or "g")
        flags = "".join(ch for ch in raw_flags if ch in _SUPPORTED_JS_FLAGS) or "g"
        placement = item.get("placement")
        if isinstance(placement, list):
            clean_placement = [int(p) for p in placement if isinstance(p, (int, float)) or str(p).isdigit()]
        else:
            clean_placement = [1, 2]
        trim_strings = [str(s) for s in (item.get("trim_strings") or []) if isinstance(s, str) and s]
        out.append({
            "id": str(item.get("id") or f"rule_{idx}"),
            "name": str(item.get("name") or f"Rule {idx + 1}"),
            "pattern": pattern,
            "flags": flags,
            "replace": str(item.get("replace") or ""),
            "placement": clean_placement or [1, 2],
            "trim_strings": trim_strings,
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def extract_display_regex(raw_card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract display-only regex scripts from RisuAI and SillyTavern cards."""
    ext, risu = _card_extensions(raw_card)
    rules: List[Dict[str, Any]] = []

    # 1. RisuAI customScripts (type == 'editdisplay')
    custom_scripts = risu.get("customScripts")
    if isinstance(custom_scripts, list):
        for idx, item in enumerate(custom_scripts):
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "editdisplay":
                continue
            raw_in = str(item.get("in") or "")
            if not raw_in:
                continue
            flag = str(item.get("flag") or "g") if item.get("ableFlag", True) else "g"
            pattern, flags = _split_regex_literal(raw_in, flag)
            if not pattern:
                continue
            rules.append({
                "id": f"risu_{idx}",
                "name": str(item.get("comment") or f"Risu Display {idx + 1}"),
                "pattern": pattern,
                "flags": flags,
                "replace": str(item.get("out") or ""),
                "placement": [1, 2],
                "trim_strings": [],
                "enabled": True,
            })

    # 2. SillyTavern extensions.regex_scripts
    st_scripts = ext.get("regex_scripts")
    if isinstance(st_scripts, list):
        for idx, item in enumerate(st_scripts):
            if not isinstance(item, dict):
                continue
            if item.get("promptOnly") and not item.get("markdownOnly"):
                continue
            raw_find = str(item.get("findRegex") or "")
            if not raw_find:
                continue
            pattern, flags = _split_regex_literal(raw_find, "g")
            if not pattern:
                continue
            placement = item.get("placement")
            clean_placement = (
                [int(p) for p in placement if isinstance(p, (int, float)) or str(p).isdigit()]
                if isinstance(placement, list)
                else [2]
            )
            trim_strings = [str(s) for s in (item.get("trimStrings") or []) if isinstance(s, str) and s]
            rules.append({
                "id": str(item.get("id") or f"st_{idx}"),
                "name": str(item.get("scriptName") or f"ST Regex {idx + 1}"),
                "pattern": pattern,
                "flags": flags,
                "replace": str(item.get("replaceString") or ""),
                "placement": clean_placement or [2],
                "trim_strings": trim_strings,
                "enabled": not bool(item.get("disabled", False)),
            })

    return rules


def extract_background_css(raw_card: Dict[str, Any]) -> str:
    """Extract card-scoped CSS from RisuAI ``backgroundHTML`` or Pebble extensions."""
    ext, risu = _card_extensions(raw_card)
    chunks: List[str] = []

    bg = risu.get("backgroundHTML")
    if isinstance(bg, str) and bg.strip():
        styles = [m.group(1).strip() for m in _STYLE_BLOCK_RE.finditer(bg) if m.group(1).strip()]
        if styles:
            chunks.extend(styles)
        elif "<" not in bg and "{" in bg:
            chunks.append(bg.strip())

    custom_css = ext.get("pebble_card_css")
    if isinstance(custom_css, str) and custom_css.strip():
        chunks.append(custom_css.strip())

    return "\n\n".join(chunks)


_AUTO_FRAME_BRIDGES: Tuple[Dict[str, Any], ...] = (
    {
        "markers": (".twu-asset", ".twu-asset-inner", ".twu-asset-trigger", ".twu-asset-profile"),
        "rule": {
            "id": "auto_twu_frame",
            "name": "TWU Asset Frame (Auto)",
            "pattern": r"""<img=["']?([^"'>]+)["']?\s*>""",
            "flags": "gi",
            "replace": (
                '<div class="twu-asset twu-image-$1">'
                '<details class="twu-asset-inner">'
                '<summary class="twu-asset-trigger"><img src="$1" alt="$1"></summary>'
                '<div class="twu-asset-profile">'
                '<span class="twu-asset-profile-role"></span>'
                '<span class="twu-asset-profile-name"></span>'
                '<span class="twu-asset-profile-rule"></span>'
                '<span class="twu-asset-profile-line">'
                '<span class="twu-asset-profile-key twu-profile-key-courses"></span>'
                '<span class="twu-asset-profile-courses"></span>'
                '</span>'
                '<span class="twu-asset-profile-line">'
                '<span class="twu-asset-profile-key twu-profile-key-lab"></span>'
                '<span class="twu-asset-profile-lab"></span>'
                '</span>'
                '</div>'
                '</details>'
                '</div>'
            ),
            "placement": [1, 2],
            "trim_strings": [],
            "enabled": True,
        },
    },
)


def _apply_auto_frame_bridges(
    display_regex: List[Dict[str, Any]],
    card_css: str,
) -> List[Dict[str, Any]]:
    """Synthesize a fallback Display Regex when a card ships frame CSS without its regex."""
    if not card_css:
        return display_regex
    has_img_rule = any("<img" in str(r.get("pattern") or "").lower() for r in display_regex)
    if has_img_rule:
        return display_regex

    out = list(display_regex)
    for bridge in _AUTO_FRAME_BRIDGES:
        if all(marker in card_css for marker in bridge["markers"]):
            out.append(dict(bridge["rule"]))
            break
    return out


def extract_card_presentation(raw_card_data: Any, card_ui: Any = None) -> Dict[str, Any]:
    """Build normalized ``card_presentation`` payload from raw card + user overrides."""
    raw_card = _safe_json_obj(raw_card_data)
    overrides = _safe_json_obj(card_ui)

    default_vars = parse_default_variables(raw_card)
    if isinstance(overrides.get("default_variables"), dict):
        for k, v in overrides["default_variables"].items():
            key = str(k).strip()
            if key:
                default_vars[key] = "" if v is None else str(v)

    if "display_regex" in overrides and isinstance(overrides["display_regex"], list):
        display_regex = normalize_display_regex_list(overrides["display_regex"])
    else:
        display_regex = extract_display_regex(raw_card)

    if "card_css" in overrides and isinstance(overrides["card_css"], str):
        card_css = overrides["card_css"]
    else:
        card_css = extract_background_css(raw_card)

    display_regex = _apply_auto_frame_bridges(display_regex, card_css)

    return {
        "default_variables": default_vars,
        "display_regex": display_regex,
        "card_css": card_css,
    }

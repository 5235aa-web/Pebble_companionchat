"""card_hud.py - Single-chat Stage HUD & Media Cue protocol.

Independent from script_mode.py: keeps normal companion chat intact (full
character card, memory engine, global rules) while allowing cards—especially
multimedia CHARX cards—to maintain a live state HUD (mood, affection,
location, outfit, custom fields) and trigger stage expressions, scene images,
BGM, and one-shot SFX via a stripped trailing <<<STATE>>> block.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

HUD_STATE_START = "<<<STATE>>>"
HUD_STATE_END = "<<<END>>>"

CORE_HUD_KEYS = ("mood", "affection", "location", "outfit")
MEDIA_CUE_KEYS = ("expression", "scene", "bg", "bgm", "sfx")
_IGNORED_CONTROL_KEYS = {"scene_done", "scenedone", "scene_finished"}

_MAX_VISUAL_TAGS = 40
_MAX_AUDIO_TAGS = 20


def _coerce_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in ("true", "yes", "是"):
        return True
    if lowered in ("false", "no", "否"):
        return False
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def parse_hud_block(raw: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Parse a stripped <<<STATE>>> body into (hud_update, media_cue)."""
    hud: Dict[str, Any] = {}
    cue: Dict[str, str] = {}
    for line in raw.splitlines():
        cleaned = line.strip().lstrip("-").strip()
        if not cleaned or ":" not in cleaned:
            continue
        key, _, value = cleaned.partition(":")
        key = key.strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in _IGNORED_CONTROL_KEYS:
            continue
        if lowered in MEDIA_CUE_KEYS:
            val_str = value.strip()
            if val_str:
                canonical = "scene" if lowered == "bg" else lowered
                cue[canonical] = val_str
                # Persist current expression/scene/bgm on HUD, but keep sfx one-shot.
                if canonical != "sfx":
                    hud[canonical] = "" if val_str.lower() in ("none", "stop", "off") else val_str
            continue
        hud[key] = _coerce_value(value)
    return hud, cue


def merge_card_hud(current: Optional[Dict[str, Any]], update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge new HUD values into existing state without dropping unmentioned keys."""
    merged = dict(current or {})
    for key, value in (update or {}).items():
        if key == "sfx":
            continue
        merged[key] = value
    return merged


def _summarize_assets_for_prompt(assets: List[Dict]) -> Tuple[List[str], List[str]]:
    """Select compact, non-redundant visual and audio asset names for the prompt.

    Protects context budget on massive 2,000+ image CHARX cards by prioritizing
    explicit emotion/background/audio names and capping list length strictly.
    """
    emotions: List[str] = []
    other_images: List[str] = []
    audios: List[str] = []
    seen_v = set()
    seen_a = set()

    for asset in assets or []:
        mime = str(asset.get("mime_type") or "").lower()
        a_type = str(asset.get("asset_type") or "").lower()
        name = str(asset.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()

        if mime.startswith("audio/") or a_type in ("audio", "bgm", "sfx", "voice"):
            if key not in seen_a and len(audios) < _MAX_AUDIO_TAGS:
                seen_a.add(key)
                audios.append(name)
        elif mime.startswith("image/") or a_type in ("emotion", "icon", "background", "image"):
            if key in seen_v:
                continue
            seen_v.add(key)
            if a_type in ("emotion", "background"):
                if len(emotions) < _MAX_VISUAL_TAGS:
                    emotions.append(name)
            else:
                # Skip pure numeric placeholder names when better names exist.
                if not re.fullmatch(r"\d+", name) and len(other_images) < _MAX_VISUAL_TAGS:
                    other_images.append(name)

    visual = emotions if emotions else other_images
    if emotions and len(visual) < 16 and other_images:
        for item in other_images:
            if len(visual) >= _MAX_VISUAL_TAGS:
                break
            if item not in visual:
                visual.append(item)
    return visual[:_MAX_VISUAL_TAGS], audios[:_MAX_AUDIO_TAGS]


def _format_current_hud(hud: Optional[Dict[str, Any]], limit: int = 16) -> str:
    data = hud or {}
    keys = list(CORE_HUD_KEYS) + [
        k for k in data if k not in CORE_HUD_KEYS and k != "sfx"
    ]
    lines: List[str] = []
    for key in keys[:limit]:
        val = data.get(key, "")
        if isinstance(val, (list, tuple)):
            text = ", ".join(str(v) for v in val)
        elif isinstance(val, bool):
            text = "yes" if val else "no"
        else:
            text = str(val)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def build_card_hud_prompt(
    current_hud: Optional[Dict[str, Any]],
    assets: Optional[List[Dict]] = None,
) -> str:
    """Build the [Stage HUD] prompt block appended in normal single-chat mode."""
    visual_names, audio_names = _summarize_assets_for_prompt(assets or [])

    lines = [
        "[Stage HUD]",
        "Maintain the character's live state across turns.",
        "Current state:",
        _format_current_hud(current_hud),
    ]

    if visual_names:
        lines.append(
            "Available expression/scene images: " + ", ".join(visual_names)
        )
    if audio_names:
        lines.append(
            "Available BGM/SFX tracks: " + ", ".join(audio_names)
        )

    lines.extend([
        "",
        "At the very end of every reply, append a state block in this exact format:",
        HUD_STATE_START,
        "mood: <current emotion in a few words>",
        "affection: <integer 0-100>",
        "location: <current setting/place>",
        "outfit: <current clothing>",
    ])
    if visual_names:
        lines.append("expression: <best matching image name from above, or current mood>")
    if audio_names:
        lines.append("bgm: <track name to loop, 'none' to stop, or omit to keep>")
        lines.append("sfx: <optional one-shot sound effect name, or omit>")
    lines.append(HUD_STATE_END)
    lines.append(
        "Write normal prose/dialogue before the block; never mention the block in dialogue."
    )
    return "\n".join(lines)


class CardHudStreamFilter:
    """Strip the trailing <<<STATE>>>...<<<END>>> block from a token stream."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_state = False
        self._state_raw = ""

    def _partial_marker_len(self) -> int:
        limit = min(len(self._buf), len(HUD_STATE_START) - 1)
        for size in range(limit, 0, -1):
            if HUD_STATE_START.startswith(self._buf[-size:]):
                return size
        return 0

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: List[str] = []
        while True:
            if self._in_state:
                end = self._buf.find(HUD_STATE_END)
                if end < 0:
                    self._state_raw += self._buf
                    self._buf = ""
                    return "".join(out)
                self._state_raw += self._buf[:end]
                self._buf = self._buf[end + len(HUD_STATE_END):]
                self._in_state = False
                continue

            start = self._buf.find(HUD_STATE_START)
            if start >= 0:
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(HUD_STATE_START):]
                self._in_state = True
                continue

            hold = self._partial_marker_len()
            if hold:
                out.append(self._buf[:-hold])
                self._buf = self._buf[-hold:]
            else:
                out.append(self._buf)
                self._buf = ""
            return "".join(out)

    def finish(self) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
        """Flush tail and return (visible_tail, hud_update, media_cue)."""
        if self._in_state:
            self._state_raw += self._buf
            tail = ""
        else:
            tail = self._buf
        self._buf = ""
        hud_update, media_cue = (
            parse_hud_block(self._state_raw) if self._state_raw else ({}, {})
        )
        self._state_raw = ""
        return tail, hud_update, media_cue

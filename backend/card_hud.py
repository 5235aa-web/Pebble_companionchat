"""card_hud.py - Single-chat Stage HUD & Media Cue protocol.

Independent from script_mode.py: keeps normal companion chat intact (full
character card, memory engine, global rules) while allowing cards—especially
multimedia CHARX cards—to maintain a live state HUD (mood, affection,
location, outfit, custom fields) and trigger stage expressions, scene images,
BGM, and one-shot SFX via a stripped trailing <<<STATE>>> block.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

HUD_STATE_START = "<<<STATE>>>"
HUD_STATE_END = "<<<END>>>"

CORE_HUD_KEYS = ("mood", "affection", "location", "outfit")
MEDIA_CUE_KEYS = ("expression", "bgm", "sfx")
_IGNORED_CONTROL_KEYS = {"scene_done", "scenedone", "scene_finished", "scene", "bg"}
_THOUGHT_NULL_VALUES = frozenset({"none", "null", "off", "empty", "n/a", "-", "—", "无", "没有"})
_PLACEHOLDER_ICON_NAMES = frozenset({"iconx", "main", "avatar", "cover", "thumb", "thumbnail"})
_GENERIC_SCENE_RE = re.compile(r"^(?:\d+|(?:scene|bg|cg|map|item|ui|icon)[-_\s]*\d*)$", re.IGNORECASE)
_EMOTION_HINT_RE = re.compile(
    r"(?:_|^|\s)(?:smile|shy|blush|happy|sad|angry|annoyed|confused|curious|crying|"
    r"embarrassed|surprised|shocked|aroused|ahegao|warm|gentle|love|nervous|scared|"
    r"sleepy|tired|smug|teasing|laugh|wink|touched|relaxed|calm|neutral|normal|"
    r"defeat|attack|pleasure|upset|fear|pout|grin|serious|admiring|depressed|bored)\b",
    re.IGNORECASE,
)
_SIGNED_AFFECTION_RE = re.compile(r"^\s*([+-])\s*(\d+(?:\.\d+)?)\b")
_LEADING_AFFECTION_RE = re.compile(r"^\s*(?:[^\d+\-.]{0,18}?[:：=♥♡\s])?\s*(\d+(?:\.\d+)?)\b")

_MAX_VISUAL_TAGS = 40
_MAX_AUDIO_TAGS = 20


def _clamp_affection(val: float) -> int:
    return max(0, min(100, int(round(val))))


def parse_affection_value(raw: Any, current: Optional[Any] = None) -> Optional[int]:
    """Parse numeric, annotated (``17 (growing)``, ``65/100``), or ``+delta`` affection values."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return _clamp_affection(float(raw)) if math.isfinite(float(raw)) else None

    text = str(raw).strip()
    if not text:
        return None

    m_signed = _SIGNED_AFFECTION_RE.match(text)
    if m_signed:
        sign = -1.0 if m_signed.group(1) == "-" else 1.0
        delta = sign * float(m_signed.group(2))
        base = parse_affection_value(current) if current is not None else None
        if base is not None:
            return _clamp_affection(base + delta)
        return _clamp_affection(delta)

    m_num = _LEADING_AFFECTION_RE.match(text)
    if m_num:
        return _clamp_affection(float(m_num.group(1)))
    return None


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
                cue[lowered] = val_str
                # Persist current expression/bgm on HUD, but keep sfx one-shot.
                if lowered != "sfx":
                    hud[lowered] = "" if val_str.lower() in ("none", "stop", "off") else val_str
            continue
        if lowered == "thought":
            val_str = value.strip().strip("\"'")
            hud["thought"] = "" if val_str.lower() in _THOUGHT_NULL_VALUES else val_str
            continue
        if lowered == "affection":
            raw_aff = value.strip()
            m_signed = _SIGNED_AFFECTION_RE.match(raw_aff)
            if m_signed:
                hud["affection"] = f"{m_signed.group(1)}{m_signed.group(2)}"
            else:
                parsed_aff = parse_affection_value(raw_aff)
                if parsed_aff is not None:
                    hud["affection"] = parsed_aff
                elif raw_aff:
                    hud["affection"] = _coerce_value(raw_aff)
            continue
        hud[key] = _coerce_value(value)
    return hud, cue


def merge_card_hud(
    current: Optional[Dict[str, Any]],
    update: Optional[Dict[str, Any]],
    *,
    clear_stale_thought: bool = False,
) -> Dict[str, Any]:
    """Merge new HUD values into existing state without dropping unmentioned keys."""
    merged = dict(current or {})
    base_aff = parse_affection_value(merged.get("affection")) if "affection" in merged else None
    if base_aff is not None:
        merged["affection"] = base_aff
    upd = update or {}
    for key, value in upd.items():
        if key == "sfx":
            continue
        if key == "thought":
            text = str(value or "").strip()
            if not text or text.lower() in _THOUGHT_NULL_VALUES:
                merged.pop("thought", None)
            else:
                merged["thought"] = text
            continue
        if str(key).lower() == "affection":
            next_aff = parse_affection_value(value, current=base_aff)
            if next_aff is not None:
                merged["affection"] = next_aff
                base_aff = next_aff
            elif base_aff is not None:
                merged["affection"] = base_aff
            continue
        merged[key] = value
    if clear_stale_thought and "thought" not in upd:
        merged.pop("thought", None)
    return merged


def extract_var_updates(
    hud_update: Optional[Dict[str, Any]],
    pinned_vars: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Split ``var.<key>`` (or pinned CBS variable) mutations out of a parsed HUD update."""
    clean_hud: Dict[str, Any] = {}
    var_updates: Dict[str, str] = {}
    pinned_set = {str(k).strip() for k in (pinned_vars or ()) if str(k).strip()}
    pinned_lower = {k.lower(): k for k in pinned_set}
    reserved = set(CORE_HUD_KEYS) | {"expression", "scene", "bgm", "sfx"}

    for raw_k, raw_v in (hud_update or {}).items():
        k = str(raw_k).strip()
        if not k:
            continue
        low = k.lower()
        if low.startswith("var.") and len(k) > 4:
            var_key = k[4:].strip()
            if var_key:
                var_updates[var_key] = _format_var_value(raw_v)
            continue
        if low not in reserved:
            if k in pinned_set:
                var_updates[k] = _format_var_value(raw_v)
                continue
            if low in pinned_lower:
                var_updates[pinned_lower[low]] = _format_var_value(raw_v)
                continue
        clean_hud[k] = raw_v
    return clean_hud, var_updates


def _format_var_value(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val)


def _extract_name_tokens(char_name: str) -> List[str]:
    tokens = [
        tok.lower()
        for tok in re.split(r"[\s,·/|()\[\]<>\"'!?！？~]+", char_name or "")
        if len(tok.strip()) >= 2
    ]
    return list(dict.fromkeys(tokens))


def _image_relevance_score(name: str, char_tokens: Sequence[str]) -> int:
    low = name.lower()
    score = 0
    if char_tokens and any(tok in low for tok in char_tokens):
        score += 100
    if _EMOTION_HINT_RE.search(name):
        score += 30
    if not re.match(r"^(?:pikk|mob|bandit|slime|minotaur|inccubus|wyvern|kraken|troll|tentacles|goblins)[_\s]", low):
        score += 5
    return score


def _summarize_assets_for_prompt(
    assets: List[Dict],
    *,
    char_name: str = "",
) -> Tuple[List[str], List[str]]:
    """Select compact, non-redundant visual and audio asset names for the prompt.

    Protects context budget on massive 2,000+ image CHARX cards by prioritizing
    explicit emotion/portrait names (skipping generic placeholder icons),
    ranking main-character emotion candidates first, and capping list length strictly.
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
        elif mime.startswith("image/") or a_type in ("emotion", "icon", "portrait", "image"):
            if key in seen_v or a_type == "background":
                continue
            if a_type == "icon" and key in _PLACEHOLDER_ICON_NAMES:
                continue
            seen_v.add(key)
            if a_type in ("emotion", "portrait", "icon"):
                if len(emotions) < _MAX_VISUAL_TAGS:
                    emotions.append(name)
            elif not _GENERIC_SCENE_RE.fullmatch(key):
                other_images.append(name)

    if len(other_images) > _MAX_VISUAL_TAGS:
        char_tokens = _extract_name_tokens(char_name)
        other_images.sort(key=lambda item: -_image_relevance_score(item, char_tokens))

    other_images = other_images[:_MAX_VISUAL_TAGS]
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
        k for k in data if k not in CORE_HUD_KEYS and k not in ("sfx", "thought")
    ]
    lines: List[str] = []
    for key in keys[:limit]:
        val = data.get(key, "")
        if key == "affection":
            parsed = parse_affection_value(val)
            text = str(parsed if parsed is not None else 0)
        elif isinstance(val, (list, tuple)):
            text = ", ".join(str(v) for v in val)
        elif isinstance(val, bool):
            text = "yes" if val else "no"
        else:
            text = str(val)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def _collect_expression_roles(assets: Sequence[Dict]) -> List[str]:
    """Collect capped expression/portrait names for the [Stage HUD] prompt."""
    exprs: List[str] = []
    seen_e = set()
    for asset in assets or []:
        mime = str(asset.get("mime_type") or "").lower()
        a_type = str(asset.get("asset_type") or "").lower()
        name = str(asset.get("name") or "").strip()
        if not name or _GENERIC_SCENE_RE.fullmatch(name):
            continue
        if not (mime.startswith("image/") or a_type in ("emotion", "icon", "portrait", "image")):
            continue
        if a_type in ("emotion", "portrait", "icon"):
            key = name.lower()
            if a_type == "icon" and key in _PLACEHOLDER_ICON_NAMES:
                continue
            if key not in seen_e and len(exprs) < _MAX_VISUAL_TAGS:
                seen_e.add(key)
                exprs.append(name)
    return exprs


def build_card_hud_prompt(
    current_hud: Optional[Dict[str, Any]],
    assets: Optional[List[Dict]] = None,
    *,
    variables: Optional[Dict[str, Any]] = None,
    pinned_vars: Optional[Sequence[str]] = None,
    thought_enabled: bool = False,
    char_name: str = "",
    inline_media_managed: bool = False,
) -> str:
    """Build the [Stage HUD] prompt block appended in normal single-chat mode."""
    asset_list = list(assets or [])
    visual_names, audio_names = _summarize_assets_for_prompt(asset_list, char_name=char_name)
    expr_names = _collect_expression_roles(asset_list)
    # When a card already emits inline media tags (e.g. <pimg="...">) and lacks
    # dedicated portrait sprites, keep [Stage HUD] focused on state telemetry so it
    # never competes with the card's own visual protocol or dumps random asset lists.
    emit_expression_cue = bool(expr_names) or (bool(visual_names) and not inline_media_managed)
    pinned = [str(k).strip() for k in (pinned_vars or ()) if str(k).strip()]
    vars_map = variables or {}

    lines = [
        "[Stage HUD]",
        "Maintain the character's live state across turns.",
        "Current state:",
        _format_current_hud(current_hud),
    ]

    if pinned:
        var_lines = [f"{k}: {vars_map.get(k, '')}" for k in pinned[:12]]
        lines.append("Tracked story variables (CBS):")
        lines.extend(var_lines)

    if emit_expression_cue:
        if expr_names:
            lines.append("Available expression portraits: " + ", ".join(expr_names))
        elif visual_names:
            lines.append("Available expression images: " + ", ".join(visual_names))
    if audio_names:
        lines.append(
            "Available BGM/SFX tracks: " + ", ".join(audio_names)
        )

    lines.extend([
        "",
        "At the very end of every reply, append a state block in this exact format:",
        HUD_STATE_START,
        "mood: <current emotion in a few words>",
        "affection: <integer ONLY 0-100, cumulative score, NO '+' delta, NO text or notes>",
        "location: <current setting/place>",
        "outfit: <current clothing>",
    ])
    if emit_expression_cue:
        lines.append("expression: <best matching portrait/expression name from above, or current mood>")
    if audio_names:
        lines.append("bgm: <track name to loop, 'none' to stop, or omit to keep>")
        lines.append("sfx: <optional one-shot sound effect name, or omit>")
    if pinned:
        lines.append("var.<name>: <updated value for any tracked story variable that changed, or omit>")
    if thought_enabled:
        lines.append(
            "thought: <RARE & SELECTIVE — only on a deeply felt, tender, vulnerable, or unspoken-tension moment, "
            "write 1 brief unspoken inner thought (under 22 words) that contrasts with or deepens spoken words; "
            "omit or write 'none' on casual/ordinary turns>"
        )
    lines.append(HUD_STATE_END)
    if thought_enabled:
        lines.append(
            "Only include the 'thought:' line inside <<<STATE>>> sparingly when the moment truly feels "
            "emotionally charged, intimate, or pivotal—never on every reply, and never put 'thought:' in the reply text itself."
        )
    lines.append(
        "Keep updating any custom state fields listed under 'Current state' when they change."
    )
    lines.append(
        "Write normal prose/dialogue before the block; never mention the block in dialogue."
    )
    return "\n".join(lines)


_STATE_START_RE = re.compile(
    r"(?:(?<=^)|(?<=\n))[ \t]*```[a-zA-Z0-9_-]*[ \t]*\r?\n[ \t]*<<<\s*state\s*>>>"
    r"|<<<\s*state\s*>>>",
    re.IGNORECASE,
)
_STATE_END_RE = re.compile(r"<<<\s*(?:end|/\s*state)\s*>>>", re.IGNORECASE)
_CLOSING_FENCE_RE = re.compile(r"^[ \t]*\r?\n?[ \t]*```[ \t]*(?:\r?\n|$)")
_CLOSING_FENCE_PARTIAL_RE = re.compile(r"^[ \t]*\r?\n?[ \t]*(?:`{1,3}[ \t]*)?$")
_HOLD_TAIL_RE = re.compile(
    r"(?:"
    r"(?:(?<=^)|(?<=\n))[ \t]*`{1,3}[a-zA-Z0-9_-]*[ \t]*(?:\r?\n[ \t]*)?"
    r"(?:<(?:<(?:<(?:\s*(?:s(?:t(?:a(?:t(?:e(?:\s*>{0,2})?)?)?)?)?)?)?)?)?)?"
    r"|"
    r"<(?:<(?:<(?:\s*(?:s(?:t(?:a(?:t(?:e(?:\s*>{0,2})?)?)?)?)?)?)?)?)?"
    r")$",
    re.IGNORECASE,
)


class CardHudStreamFilter:
    """Strip the trailing <<<STATE>>>...<<<END>>> block from a token stream.

    Tolerates LLM format drift such as mixed case (`<<<state>>>`), inner spaces
    (`<<< STATE >>>`), `<<< /STATE >>>` closers, and Markdown code fences
    wrapping the state block without delaying normal Markdown code blocks.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_state = False
        self._closing_fence_pending = False
        self._state_raw = ""

    def _partial_marker_len(self) -> int:
        m = _HOLD_TAIL_RE.search(self._buf)
        if not m:
            return 0
        # Do not hold an empty match.
        return len(m.group(0))

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: List[str] = []
        while True:
            if self._closing_fence_pending:
                m_close = _CLOSING_FENCE_RE.match(self._buf)
                if m_close:
                    self._buf = self._buf[m_close.end():]
                    self._closing_fence_pending = False
                    continue
                if _CLOSING_FENCE_PARTIAL_RE.match(self._buf):
                    return "".join(out)
                self._closing_fence_pending = False

            if self._in_state:
                m_end = _STATE_END_RE.search(self._buf)
                if not m_end:
                    last_lt = self._buf.rfind("<")
                    if last_lt >= 0 and len(self._buf) - last_lt <= 24:
                        self._state_raw += self._buf[:last_lt]
                        self._buf = self._buf[last_lt:]
                    else:
                        self._state_raw += self._buf
                        self._buf = ""
                    return "".join(out)
                self._state_raw += self._buf[:m_end.start()]
                self._buf = self._buf[m_end.end():]
                self._in_state = False
                self._closing_fence_pending = True
                continue

            m_start = _STATE_START_RE.search(self._buf)
            if m_start:
                out.append(self._buf[:m_start.start()])
                self._buf = self._buf[m_start.end():]
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
        elif self._closing_fence_pending:
            m_close = _CLOSING_FENCE_RE.match(self._buf)
            if m_close:
                tail = self._buf[m_close.end():]
            elif _CLOSING_FENCE_PARTIAL_RE.match(self._buf):
                tail = ""
            else:
                tail = self._buf
        else:
            tail = self._buf
        self._buf = ""
        self._closing_fence_pending = False
        hud_update, media_cue = (
            parse_hud_block(self._state_raw) if self._state_raw else ({}, {})
        )
        self._state_raw = ""
        return tail, hud_update, media_cue

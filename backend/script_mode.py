"""
script_mode.py - Prompt construction and state-block handling for script mode.

Script mode is deliberately narrow: the model receives the character's name,
description and personality, the story premise, the current scene, and the
character HUD. Nothing else from the card, and nothing from the world book or
the memory system. A separate builder is used instead of build_system_prompt so
a later change to normal chat cannot leak extra sections into a scripted scene.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fenced block the model appends to every reply. Both markers are unlikely to
# appear in prose, and the stream filter keeps a partial marker in its buffer.
SCRIPT_STATE_START = "<<<STATE>>>"
SCRIPT_STATE_END = "<<<END>>>"

# Keys that drive the engine rather than the HUD display.
_CONTROL_KEYS = {
    "scene_done", "scene done", "scenedone", "scene_finished",
    "本幕完成", "场景完成", "本幕结束",
}


HUD_KEYS = ("mood", "affection", "location", "outfit")

# Prepended to a narration in script mode. A scripted narration is a one-shot
# cue: it shapes the next reply and then leaves the context, so this note has to
# carry the whole explanation on its own.
SCRIPT_DIRECTION_NOTE = (
    "This is not the user speaking, and it is not a user message. It is a "
    "third-person narration from the script, a voice-over that directs the "
    "scene. Your next reply must be based on it and answer it."
)


def _format_hud(hud: Dict[str, Any], limit: int = 20) -> str:
    data = hud or {}
    # The four core keys are always shown, even when empty: the model is asked
    # to keep them updated, and a missing line reads as "no such field".
    keys = list(HUD_KEYS) + [k for k in data if k not in HUD_KEYS]
    lines = []
    for key in keys[:limit]:
        value = data.get(key, "")
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            text = "yes" if value else "no"
        else:
            text = str(value)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def build_script_system_prompt(
    companion: Any,
    script: Dict,
    node: Optional[Dict],
    node_index: int,
    node_count: int,
    hud: Dict[str, Any],
    turns: int = 0,
    turns_done: int = 0,
) -> str:
    """Assemble the scripted system prompt.

    Only the fields below reach the model. In particular there is no world
    book, no memory block, no dialogue examples, no advanced prompt, no user
    info, no card system prompt, no post-history instructions and no depth
    prompt: a scripted scene is defined by the script, not by the card's chat
    scaffolding.
    """
    parts: List[str] = []

    name = (getattr(companion, "name", "") or "").strip()
    if name:
        parts.append(f"[Character]\nYou are {name}.")

    description = (getattr(companion, "description", "") or "").strip()
    if description:
        parts.append(f"[Description]\n{description}")

    personality = (getattr(companion, "personality", "") or "").strip()
    if personality:
        parts.append(f"[Personality]\n{personality}")

    parts.append(
        "[Script Mode]\n"
        "This is a scripted performance, not an open chat. The user wrote the "
        "story and directs it. The script below is the highest authority: it "
        "overrides any habit or preference of the character."
    )

    title = (script.get("title") or "").strip()
    if title:
        parts.append(f"[Script Title]\n{title}")

    premise = (script.get("premise") or "").strip()
    if premise:
        parts.append(f"[Story Premise]\n{premise}")

    if node:
        title = (node.get("title") or "").strip() or f"Scene {node_index + 1}"
        instruction = (node.get("instruction") or "").strip() or "(no extra instruction)"
        scene_block = f"[Current Scene {node_index + 1}/{node_count}]\n{title}\n{instruction}"
        if turns > 0:
            scene_block += (
                f"\n\nPlanned length: about {turns} exchanges. "
                f"Used: {turns_done}. Remaining: {max(0, turns - turns_done)}."
            )
        else:
            scene_block += f"\n\nPlanned length: open. Used: {turns_done}."
        parts.append(scene_block)
    else:
        parts.append(
            f"[Current Scene {node_index + 1}/{node_count}]\n"
            "No extra instruction for this scene. Advance from the premise alone."
        )

    parts.append(f"[Character State]\n{_format_hud(hud)}")

    if turns > 0:
        pace = (
            f"This scene is planned to run about {turns} exchanges; pace its "
            f"events so they land near that count. {max(0, turns - turns_done)} remain."
        )
    else:
        pace = "This scene has no fixed length; play it at a natural pace."

    parts.append(
        "[Rules]\n"
        "1. Play only the current scene. Do not resolve it in one reply, and do "
        "not start anything that belongs to a later scene.\n"
        "2. Never speak, act, think or decide for the user's character. Leave "
        "room for the user to answer.\n"
        "3. Stay in character: the personality above decides how lines are "
        "delivered, the script decides what happens.\n"
        "4. Write the scene in prose. No bullet lists, no headings, no "
        "meta-commentary about being an AI.\n"
        "5. When the events of this scene have actually played out, say so in "
        "the state block instead of rushing the story forward.\n"
        "6. End every reply with the state block, exactly in this shape:\n"
        f"{SCRIPT_STATE_START}\n"
        "mood: <current emotion>\n"
        "affection: <number>\n"
        "location: <where the character is>\n"
        "outfit: <what the character is wearing right now>\n"
        "scene_done: false\n"
        f"{SCRIPT_STATE_END}\n"
        "Keep the keys that already exist in [Character State] and update their "
        "values. Set scene_done to true only when this scene is finished.\n"
        f"7. {pace}\n"
        "8. The planned length is pacing guidance, not an ending. Never advance "
        "to the next scene yourself: the user decides when to move on. If the "
        "user keeps talking in this scene, keep playing it."
    )

    return "\n\n".join(parts)


# ==================== State block ====================

def _coerce(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in ("true", "yes", "是"):
        return True
    if lowered in ("false", "no", "否"):
        return False
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    except (TypeError, ValueError):
        pass
    return text


def parse_state_block(raw: str) -> Tuple[Dict[str, Any], bool]:
    """Parse the fenced block body into HUD values plus the scene-done flag."""
    hud: Dict[str, Any] = {}
    scene_done = False
    for line in raw.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in _CONTROL_KEYS or key in _CONTROL_KEYS:
            scene_done = bool(_coerce(value))
            continue
        hud[key] = _coerce(value)
    return hud, scene_done


class ScriptStreamFilter:
    """Strip the trailing state block from a token stream.

    The block can arrive split across chunks, so the tail of the buffer is held
    back while it could still be the beginning of a marker. The hold is at most
    the marker length, which keeps the visible stream effectively live.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_state = False
        self._state_raw = ""

    def _partial_marker_len(self) -> int:
        limit = min(len(self._buf), len(SCRIPT_STATE_START) - 1)
        for size in range(limit, 0, -1):
            if SCRIPT_STATE_START.startswith(self._buf[-size:]):
                return size
        return 0

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: List[str] = []
        while True:
            if self._in_state:
                end = self._buf.find(SCRIPT_STATE_END)
                if end < 0:
                    # Keep buffering; nothing inside a state block is shown.
                    self._state_raw += self._buf
                    self._buf = ""
                    return "".join(out)
                self._state_raw += self._buf[:end]
                self._buf = self._buf[end + len(SCRIPT_STATE_END):]
                self._in_state = False
                continue

            start = self._buf.find(SCRIPT_STATE_START)
            if start >= 0:
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(SCRIPT_STATE_START):]
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

    def finish(self) -> Tuple[str, Dict[str, Any], bool]:
        """Flush the tail and return (visible_text, hud, scene_done)."""
        if self._in_state:
            # Unclosed block: keep whatever body arrived, but never show it.
            self._state_raw += self._buf
            tail = ""
        else:
            tail = self._buf
        self._buf = ""
        hud, scene_done = parse_state_block(self._state_raw) if self._state_raw else ({}, False)
        self._state_raw = ""
        return tail, hud, scene_done


def strip_state_block(text: str) -> Tuple[str, Dict[str, Any], bool]:
    """Remove the state block from a complete reply, for stored history."""
    start = text.find(SCRIPT_STATE_START)
    if start < 0:
        return text, {}, False
    body_start = start + len(SCRIPT_STATE_START)
    end = text.find(SCRIPT_STATE_END, body_start)
    if end < 0:
        return text[:start].rstrip(), {}, False
    raw = text[body_start:end]
    hud, scene_done = parse_state_block(raw)
    clean = (text[:start] + text[end + len(SCRIPT_STATE_END):]).strip()
    return clean, hud, scene_done


def merge_hud(current: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Update values in place, keeping keys the model did not mention."""
    merged = dict(current or {})
    for key, value in (update or {}).items():
        merged[key] = value
    return merged


def dump_hud(hud: Dict[str, Any]) -> str:
    return json.dumps(hud or {}, ensure_ascii=False)

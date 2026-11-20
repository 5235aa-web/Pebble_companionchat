"""
Image prompt director.

This module is deliberately isolated from the chat and script prompt builders.
It never imports `companion.build_system_prompt`, never touches the world book,
never appends anything to a chat request. When a picture is triggered, this
module builds a fresh message array containing only what a visual director
needs, asks the text model for one JSON object, and returns it.

Keeping it separate is what stops image instructions from leaking into roleplay:
the model that writes the picture prompt is a different call with a different
system message, and its answer never reaches the conversation.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..llm_client import call_llm_stream
from ..paths import LLM_CONFIG_PATH

logger = logging.getLogger(__name__)

# Kept short on purpose: a long instruction set starts leaking into the style of
# the prompt and produces prose the image model cannot use.
DIRECTOR_SYSTEM = """You are a visual director writing prompts for a text-to-image model.

Return ONE JSON object and nothing else:
{"generate": true|false, "prompt": "...", "negative": "...", "reason": "..."}

Rules for "prompt":
- English only, 40-120 words, comma-separated visual phrases, no sentences.
- Describe subject, framing, pose, expression, lighting, colour, and setting.
- Keep the character's fixed appearance from the card exactly as given.
- Reflect the current outfit, location and mood when they are provided.
- Never mention names of real people, never write dialogue, never add text.

Rules for "negative": short comma-separated list of what to avoid.
A landscape, an object or an empty room is still a valid subject: when the user
asked for a picture explicitly, always return a non-empty prompt for it.
Set "generate" to false only when an automatic trigger found nothing visually
new to draw. A manual request is never a reason to return an empty prompt.
Do not wrap the JSON in markdown fences."""

# How much of the conversation the director sees. Enough to know the scene,
# small enough that the call stays cheap and fast.
TRANSCRIPT_TURNS = 12
MESSAGE_CLIP = 320
CARD_CLIP = 900

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DirectorError(Exception):
    pass


@dataclass
class PromptPlan:
    generate: bool
    prompt: str
    negative: str
    reason: str
    raw: str


def resolve_director_llm(preferred_id: str = "") -> Optional[Dict[str, Any]]:
    """Find a text model to write the image prompt.

    The panel-managed config wins; the project-root file is the development
    fallback. Only the first model of the chosen provider is used: a specific
    model name can still be pinned by editing that provider's list.
    """
    candidates = [LLM_CONFIG_PATH, _PROJECT_ROOT + os.sep + "llm_providers.json"]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

        providers = config.get("providers") or []
        if not isinstance(providers, list) or not providers:
            continue

        chosen = None
        if preferred_id:
            for entry in providers:
                if isinstance(entry, dict) and entry.get("id") == preferred_id:
                    chosen = entry
                    break
        if chosen is None:
            index = config.get("activeProviderIndex", 0)
            if not isinstance(index, int) or index < 0 or index >= len(providers):
                index = 0
            candidate = providers[index]
            chosen = candidate if isinstance(candidate, dict) else None
        if not chosen:
            continue

        models = chosen.get("models") or []
        model = str(models[0]) if isinstance(models, list) and models else ""
        if not model:
            continue
        return {
            "provider_type": str(chosen.get("provider") or "openai"),
            "api_key": str(chosen.get("apiKey") or ""),
            "base_url": str(chosen.get("baseUrl") or ""),
            "model": model,
            "temperature": chosen.get("temperature"),
            "top_p": chosen.get("topP"),
        }
    return None


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").replace("\r", "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def build_director_messages(
    companion: Dict[str, Any],
    transcript: List[Dict[str, str]],
    hud: Optional[Dict[str, Any]] = None,
    instruction: str = "",
    style: str = "",
    source: str = "chat",
) -> List[Dict[str, str]]:
    """Assemble the isolated message array.

    Only descriptive card fields are used. `system_prompt`, world book entries
    and post-history instructions are excluded on purpose: they shape the
    roleplay, and a picture prompt is not roleplay.
    """
    card_lines = [
        f"Character: {_clip(companion.get('name'), 120)}",
        f"Appearance and background: {_clip(companion.get('description'), CARD_CLIP)}",
        f"Personality: {_clip(companion.get('personality'), CARD_CLIP)}",
    ]
    scenario = _clip(companion.get("scenario"), CARD_CLIP)
    if scenario:
        card_lines.append(f"Setting: {scenario}")

    lines: List[str] = []
    lines.append("[character card]")
    lines.extend(line for line in card_lines if line.split(": ", 1)[-1])
    lines.append("")

    if hud:
        lines.append("[current scene state]")
        for key in ("mood", "location", "outfit", "affection"):
            value = hud.get(key)
            if value:
                lines.append(f"{key}: {_clip(value, 200)}")
        lines.append("")

    if transcript:
        lines.append(f"[last {len(transcript)} turns of the scene]")
        for entry in transcript:
            role = "Narrator" if entry.get("role") == "direction" else str(entry.get("role") or "User")
            lines.append(f"{role}: {_clip(entry.get('text'), MESSAGE_CLIP)}")
        lines.append("")

    if style:
        lines.append(f"[art direction]\n{_clip(style, 500)}\n")
    if instruction:
        lines.append(f"[user request]\n{_clip(instruction, 500)}\n")

    lines.append(f"[trigger]\nsource: {source}")
    lines.append("Write the JSON now.")

    return [
        {"role": "system", "content": DIRECTOR_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _fallback_prompt(
    companion: Dict[str, Any],
    instruction: str,
    style: str,
) -> str:
    """The prompt to use when the director refuses a manual request.

    A manual click is an explicit brief, so the instruction comes first. When it
    is empty the card's own description is the only honest subject: it keeps the
    result anchored to the character instead of inventing a scene.
    """
    parts: List[str] = []
    brief = (instruction or "").strip()
    if brief:
        parts.append(brief)
    else:
        for key in ("description", "personality", "scenario"):
            text = str((companion or {}).get(key) or "").strip()
            if text:
                parts.append(text[:300])
                break
    styling = (style or "").strip()
    if styling:
        parts.append(styling)
    parts.append("cinematic lighting, highly detailed, sharp focus")
    return ", ".join(part for part in parts if part)[:2000]


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model answer.

    Models wrap JSON in fences, prepend an apology, or add a trailing note. A
    brace scan survives all three without a second call.
    """
    raw = (text or "").strip()
    if not raw:
        raise DirectorError("导演模型没有返回内容")

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise DirectorError(f"导演模型返回的不是 JSON: {raw[:160]}")

    block = raw[start : end + 1]
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError:
        # Trailing commas are the one common defect worth repairing.
        repaired = block.replace(",}", "}").replace(",]", "]")
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise DirectorError(f"导演模型 JSON 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise DirectorError("导演模型返回的 JSON 不是对象")
    return parsed


async def draft_prompt(
    companion: Dict[str, Any],
    transcript: List[Dict[str, str]],
    hud: Optional[Dict[str, Any]] = None,
    instruction: str = "",
    style: str = "",
    source: str = "chat",
    preferred_provider_id: str = "",
    force: bool = False,
) -> PromptPlan:
    """Ask the text model for a picture prompt.

    `force` is used by the manual button: the user already decided they want an
    image, so a "nothing new to draw" verdict is overridden rather than leaving
    the click doing nothing.
    """
    llm = resolve_director_llm(preferred_provider_id)
    if not llm:
        raise DirectorError("没有可用的文本模型来生成图片提示词，请先在后台配置 LLM")

    messages = build_director_messages(
        companion=companion,
        transcript=transcript,
        hud=hud,
        instruction=instruction,
        style=style,
        source=source,
    )

    chunks: List[str] = []
    error = ""
    async for event in call_llm_stream(
        provider_type=llm["provider_type"],
        api_key=llm["api_key"],
        base_url=llm["base_url"],
        model=llm["model"],
        messages=messages,
        temperature=0.75,
        top_p=0.9,
        max_tokens=700,
        enable_thinking=False,
    ):
        kind = event.get("type")
        if kind == "content":
            chunks.append(str(event.get("content") or ""))
        elif kind == "error":
            error = str(event.get("message") or "LLM 调用失败")

    text = "".join(chunks).strip()
    if error and not text:
        raise DirectorError(error)

    parsed = _extract_json(text)
    prompt = str(parsed.get("prompt") or "").strip()
    negative = str(parsed.get("negative") or "").strip()
    reason = str(parsed.get("reason") or "").strip()
    generate = bool(parsed.get("generate", True))

    if force:
        generate = True
    if not prompt:
        if force:
            # The director declined a subject it considered uninteresting (a
            # pure landscape, an object) but the user pressed the button. Their
            # instruction is the brief, so draw it rather than failing a click.
            prompt = _fallback_prompt(companion, instruction, style)
        if not prompt:
            raise DirectorError("导演模型没有给出图片提示词")
    if len(prompt) > 2000:
        prompt = prompt[:2000]

    return PromptPlan(
        generate=generate,
        prompt=prompt,
        negative=negative[:400],
        reason=reason[:200],
        raw=text[:4000],
    )

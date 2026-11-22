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
from typing import Any, Dict, List, Optional, Tuple

from ..llm_client import call_llm_stream
from ..paths import LLM_CONFIG_PATH

logger = logging.getLogger(__name__)

# Kept focused on purpose: a long instruction set starts leaking into the style
# of the prompt and produces prose the image model cannot use.
_PROMPT_STYLE_ORDER = (
    "none",
    "auto",
    "tags",
    "anima",
    "qwen-anime",
    "qwen-real",
    "natural",
)

PROMPT_STYLES: Dict[str, Dict[str, str]] = {
    "none": {
        "label": "Empty (No style injection · Original)",
        "labelZh": "空（不注入风格指令 · 还原最初）",
        "rules": (
            "- English only, 40-120 words, comma-separated visual phrases, no sentences.\n"
            "- Describe subject, framing, pose, expression, lighting, colour, and setting."
        ),
        "negative": "",
    },
    "auto": {
        "label": "Auto (match model / workflow)",
        "labelZh": "自动匹配（按模型/工作流）",
        "rules": "",
        "negative": "",
    },
    "tags": {
        "label": "Tags (SDXL / Illustrious / Pony)",
        "labelZh": "标签式（SDXL / Illustrious / Pony）",
        "rules": (
            "- Style: SDXL / Illustrious tag-based prompt. Output comma-separated English Danbooru-style keywords and short phrases ONLY, never full sentences.\n"
            "- Order (highest weight first): quality prefix (e.g. masterpiece, best quality), subject count and identity (e.g. 1girl, solo), face and hair, body, clothing, pose and action, setting, lighting, camera and style.\n"
            "- Escape literal parentheses inside tags (e.g. tag_\\(name\\)); never mention resolution or aspect ratio.\n"
            "- For older subjects, use visual age tags early in the prompt (e.g. older woman, middle-aged, mature female, wrinkles, grey streaks) rather than bare numbers like \"58 years old\"."
        ),
        "negative": (
            "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digits, "
            "fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, "
            "signature, watermark, username, blurry"
        ),
    },
    "anima": {
        "label": "Anima (Tags + Natural Language)",
        "labelZh": "Anima 混合流（标签 + 自然语言）",
        "rules": (
            "- Style: Anima 2B anime prompt. English only; write tags in lowercase with spaces instead of underscores (only score_* tags keep underscores; prefer Gelbooru spelling).\n"
            "- Always start \"prompt\" with: masterpiece, best quality, score_7, followed by EXACTLY ONE rating tag: safe (non-sexual/clothed), nsfw (nudity/sexual), or explicit (hardcore). Never omit or output more than one rating tag.\n"
            "- Order: quality and rating prefix, character/identity, face and hair, body, clothing, pose, setting, lighting.\n"
            "- Tags and natural language may be mixed; when describing a scene or action, 1-2 concrete English sentences after the tags work better than stacking more tags.\n"
            "- When weighting tags, use stronger values like (tag:1.5) to (tag:2), never (tag:1.2). Never mention resolution or aspect ratio."
        ),
        "negative": (
            "worst quality, low quality, score_1, score_2, score_3, artist name, "
            "blurry, jpeg artifacts, chromatic aberration"
        ),
    },
    "qwen-anime": {
        "label": "Qwen-Image 2.1 · Anime (Natural Language)",
        "labelZh": "Qwen-Image 2.1 · 动漫（自然语言）",
        "rules": (
            "- Style: Qwen-Image 2.1 anime natural-language prompt. Write ONE flowing English paragraph (50-110 words) of complete sentences separated by periods, with commas inside sentences for details.\n"
            "- Always start \"prompt\" with the exact phrase: An anime illustration of \n"
            "- Strictly NO tag lists, NO bullet points, and NO weight syntax like (word:1.3).\n"
            "- Order: subject and appearance, action/pose, setting, light source and direction, framing, then style words.\n"
            "- Always include these style words near the end: flat colors, clean lineart, cel shading, anime key visual.\n"
            "- Do not use photographic words (photo, 35mm, realistic skin texture) unless explicitly requested. Never mention resolution or aspect ratio."
        ),
        "negative": (
            "low quality, low resolution, blurry, jpeg artifacts, washed-out colors, "
            "sloppy lines, messy line art, extra fingers, missing fingers, badly drawn hands, "
            "deformed anatomy, 3d render, photograph, photorealistic"
        ),
    },
    "qwen-real": {
        "label": "Qwen-Image 2.1 · Realism (Natural Language)",
        "labelZh": "Qwen-Image 2.1 · 写实（自然语言）",
        "rules": (
            "- Style: Qwen-Image 2.1 photorealistic natural-language prompt, written like an observer's caption of an already-finished photograph.\n"
            "- Write ONE flowing English paragraph (50-110 words) of complete sentences separated by periods. Strictly NO tag lists, NO bullet points, and NO weight syntax like (word:1.3).\n"
            "- Open with the photographic medium, e.g. An amateur photograph of / A candid phone photograph of / A studio portrait of.\n"
            "- Order: medium and subject, appearance (state age explicitly in the first sentence when relevant, especially if older), action, place with concrete detail, light source and direction, framing and camera, then texture words.\n"
            "- Close with physical realism words: sharp focus, natural skin texture. Never mention resolution or aspect ratio."
        ),
        "negative": (
            "artifacts, gpt-image, washed-out colors, low quality, low resolution, "
            "AI slop, deviantart, sloppy lines, rough sketch, blurry, indistinct, "
            "missing fingers, badly drawn hands, wrong number of fingers"
        ),
    },
    "natural": {
        "label": "General Natural Language (FLUX / Gemini / Cloud)",
        "labelZh": "通用自然语言（FLUX / Gemini / 云端）",
        "rules": (
            "- Style: General natural-language prompt. Write ONE clear, vivid English paragraph (45-120 words) of complete descriptive sentences.\n"
            "- Strictly NO Danbooru tag lists and NO weight syntax like (word:1.3).\n"
            "- Describe subject, appearance, pose/action, expression, setting details, lighting, colour palette, and camera framing. Never mention resolution or aspect ratio."
        ),
        "negative": (
            "lowres, bad anatomy, bad hands, extra digits, watermark, signature, "
            "text, jpeg artifacts, worst quality, blurry"
        ),
    },
}

DEFAULT_PROMPT_STYLE = "natural"


def list_prompt_styles() -> List[Dict[str, str]]:
    return [
        {
            "id": sid,
            "label": PROMPT_STYLES[sid]["label"],
            "labelZh": PROMPT_STYLES[sid]["labelZh"],
        }
        for sid in _PROMPT_STYLE_ORDER
        if sid in PROMPT_STYLES
    ]


def default_negative_for(style_id: str) -> str:
    meta = PROMPT_STYLES.get(style_id)
    return str((meta or {}).get("negative") or "")


def _infer_comfyui_style(
    workflow_id: str = "",
    workflow_data: Optional[Dict[str, Any]] = None,
) -> str:
    wf_lower = str(workflow_id or "").lower()
    if isinstance(workflow_data, dict) and workflow_data:
        has_qwen = False
        has_anima = "anima" in wf_lower
        hint_chunks: List[str] = [wf_lower]
        for node in workflow_data.values():
            if not isinstance(node, dict):
                continue
            ctype = str(node.get("class_type") or "")
            inputs = node.get("inputs") or {}
            if ctype in ("TextEncodeQwenImage21", "TextEncodeQwenImageEdit"):
                has_qwen = True
                hint_chunks.append(str(inputs.get("prompt") or "").lower())
                hint_chunks.append(str(inputs.get("text") or "").lower())
            elif ctype == "SaveImage":
                hint_chunks.append(str(inputs.get("filename_prefix") or "").lower())
            elif ctype in ("UNETLoader", "LoraLoader", "CLIPLoader"):
                for k in ("unet_name", "lora_name", "clip_name"):
                    if inputs.get(k):
                        hint_chunks.append(str(inputs[k]).lower())
            elif ctype == "KSampler":
                if inputs.get("sampler_name") == "er_sde":
                    has_anima = True
        joined = " ".join(hint_chunks)
        if has_qwen:
            if any(tok in joined for tok in ("anime", "cel shading", "clean lineart", "wutu")):
                return "qwen-anime"
            return "qwen-real"
        if has_anima or "anima" in joined:
            return "anima"
        return "tags"

    if "anima" in wf_lower:
        return "anima"
    if "wutu" in wf_lower:
        return "qwen-anime"
    if "noctq" in wf_lower or "qwen" in wf_lower:
        return "qwen-real"
    return "tags"


def resolve_prompt_style(
    configured_style: str = "auto",
    provider_id: str = "",
    workflow_id: str = "",
    workflow_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve user-selected prompt style (`auto` inspects provider/workflow)."""
    chosen = str(configured_style or "auto").strip()
    if chosen in PROMPT_STYLES and chosen != "auto":
        return chosen
    pid = str(provider_id or "").strip().lower()
    if pid == "comfyui":
        return _infer_comfyui_style(workflow_id, workflow_data)
    if pid == "aihorde":
        return "tags"
    if pid in ("siliconflow", "zhipu", "cloudflare", "gemini", "modelscope", "pollinations"):
        return "natural"
    return DEFAULT_PROMPT_STYLE


def build_director_system(style_id: str = DEFAULT_PROMPT_STYLE) -> str:
    """Build the director system prompt with dynamic style formatting rules."""
    resolved = style_id if style_id in PROMPT_STYLES and style_id != "auto" else DEFAULT_PROMPT_STYLE
    meta = PROMPT_STYLES[resolved]
    rules = meta["rules"]
    if resolved == "none":
        prompt_rules = f"""{rules}
- Keep the character's fixed appearance from the card accurately as given.
- Reflect the current outfit, location and mood when they are provided.
- Never mention names of real people, never write dialogue, never add text."""
        neg_rule = 'Rules for "negative": short comma-separated list of what to avoid.'
    else:
        neg_hint = meta.get("negative") or PROMPT_STYLES[DEFAULT_PROMPT_STYLE]["negative"]
        prompt_rules = f"""- English only: never leave Chinese characters or dialogue inside the prompt.
{rules}
- Keep the character's fixed appearance from the card accurate as given.
- Reflect any current outfit, location and mood from [current scene state] and the latest turns when provided.
- Never mention names of real people, never write dialogue, never add speech bubbles or text unless explicitly asked."""
        neg_rule = (
            'Rules for "negative": short comma-separated English list of what to avoid '
            f"for this model style (baseline: {neg_hint})."
        )
    return f"""You are a visual director writing prompts for a text-to-image model.

Return ONE JSON object and nothing else:
{{"generate": true|false, "prompt": "...", "negative": "...", "reason": "..."}}

Core directing rule:
- Ground every prompt in the character card: its fixed appearance, identity, and core traits form the indispensable baseline portrait and must always be preserved accurately.
- Focus the scene on the latest 4 messages: examine the character's most recent actions, posture, expression, emotion, physical state, outfit, and immediate surroundings so the image genuinely reflects what they are doing and experiencing right now.
- The character card defines who they are; the latest 4 messages drive what they are doing, feeling, and experiencing in the frame.

Rules for "prompt":
{prompt_rules}

{neg_rule}
A landscape, an object or an empty room is still a valid subject: when the user
asked for a picture explicitly, always return a non-empty prompt for it.
Set "generate" to false only when an automatic trigger found nothing visually
new to draw. A manual request is never a reason to return an empty prompt.
Do not wrap the JSON in markdown fences."""


DIRECTOR_SYSTEM = build_director_system(DEFAULT_PROMPT_STYLE)

# How much of the conversation the director sees. Enough to know the scene,
# small enough that the call stays cheap and fast.
TRANSCRIPT_TURNS = 4
MESSAGE_CLIP = 950
CARD_CLIP = 900
PERSONALITY_CLIP = 1200

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


def _parse_prompt_selection(selection: str) -> Tuple[str, str]:
    raw = str(selection or "").strip()
    if "::" in raw:
        pid, model = raw.split("::", 1)
        return pid.strip(), model.strip()
    return raw, ""


def _pick_model_from_provider(
    entry: Dict[str, Any],
    preferred_model: str = "",
    strict: bool = False,
    selection_label: str = "",
) -> str:
    models = [str(m).strip() for m in (entry.get("models") or []) if str(m).strip()]
    local_key = (
        str(entry.get("localModelKey") or "").strip()
        if str(entry.get("provider") or "") == "llama-cpp"
        else ""
    )
    if preferred_model:
        allowed = set(models)
        if local_key:
            allowed.add(local_key)
        if strict and allowed and preferred_model not in allowed:
            raise DirectorError(
                f"图片工作室指定的提示词模型「{preferred_model}」不在提供商模型列表中"
                f"（{selection_label or preferred_model}），请在「后台 → 生图」重新选择"
            )
        if not strict and allowed and preferred_model not in allowed:
            return local_key or (models[0] if models else "")
        return preferred_model
    if local_key:
        return local_key
    return models[0] if models else ""


def resolve_director_llm(
    preferred_id: str = "",
    chat_provider_id: str = "",
    chat_model: str = "",
) -> Optional[Dict[str, Any]]:
    """Find a text model to write the image prompt.

    Priority:
    1. Picture Studio's configured `preferred_id` (`provider_id::model` or
       legacy `provider_id`). When set, this is absolute and never falls back to
       whatever model the chat view currently has selected.
    2. Otherwise (empty `preferred_id`), follow the active chat selection
       (`chat_provider_id` / `chat_model`, or `activeProviderIndex` on disk).
    """
    candidates = [LLM_CONFIG_PATH, _PROJECT_ROOT + os.sep + "llm_providers.json"]
    config: Optional[Dict[str, Any]] = None
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        providers = loaded.get("providers") or []
        if isinstance(providers, list) and providers:
            config = loaded
            break
    if config is None:
        return None

    providers = [p for p in (config.get("providers") or []) if isinstance(p, dict)]
    if not providers:
        return None

    pref_raw = str(preferred_id or "").strip()
    if pref_raw:
        pref_pid, pref_model = _parse_prompt_selection(pref_raw)
        chosen: Optional[Dict[str, Any]] = None
        for entry in providers:
            if str(entry.get("id") or "").strip() == pref_pid:
                chosen = entry
                break
        if chosen is None and not pref_model:
            for entry in providers:
                entry_models = [str(m).strip() for m in (entry.get("models") or []) if str(m).strip()]
                if pref_pid in entry_models or pref_pid == str(entry.get("localModelKey") or "").strip():
                    chosen = entry
                    pref_model = pref_pid
                    break
        if chosen is None:
            raise DirectorError(
                f"图片工作室指定的提示词模型「{pref_raw}」不存在，请在「后台 → 生图」重新选择"
            )
        model = _pick_model_from_provider(
            chosen, preferred_model=pref_model, strict=True, selection_label=pref_raw
        )
        if not model:
            raise DirectorError("图片工作室指定的提示词提供商没有可用模型，请先在 LLM 设置里添加模型")
        return {
            "provider_type": str(chosen.get("provider") or "openai"),
            "api_key": str(chosen.get("apiKey") or ""),
            "base_url": str(chosen.get("baseUrl") or ""),
            "model": model,
            "temperature": chosen.get("temperature"),
            "top_p": chosen.get("topP"),
            "source": "picture_studio",
            "provider_id": str(chosen.get("id") or ""),
        }

    # Empty `preferred_id`: follow active chat provider/model.
    chosen = None
    chat_pid = str(chat_provider_id or "").strip()
    chat_m = str(chat_model or "").strip()
    if chat_pid:
        for entry in providers:
            if str(entry.get("id") or "").strip() == chat_pid:
                chosen = entry
                break
    if chosen is None and chat_m:
        for entry in providers:
            entry_models = [str(m).strip() for m in (entry.get("models") or []) if str(m).strip()]
            if chat_m in entry_models or chat_m == str(entry.get("localModelKey") or "").strip():
                chosen = entry
                break
    if chosen is None:
        index = config.get("activeProviderIndex", 0)
        if not isinstance(index, int) or index < 0 or index >= len(providers):
            index = 0
        chosen = providers[index]

    model = _pick_model_from_provider(chosen, preferred_model=chat_m, strict=False)
    if not model:
        return None
    return {
        "provider_type": str(chosen.get("provider") or "openai"),
        "api_key": str(chosen.get("apiKey") or ""),
        "base_url": str(chosen.get("baseUrl") or ""),
        "model": model,
        "temperature": chosen.get("temperature"),
        "top_p": chosen.get("topP"),
        "source": "chat_fallback",
        "provider_id": str(chosen.get("id") or ""),
    }


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
    prompt_style: str = DEFAULT_PROMPT_STYLE,
) -> List[Dict[str, str]]:
    """Assemble the isolated message array.

    Only descriptive card fields are used. `system_prompt`, world book entries
    and post-history instructions are excluded on purpose: they shape the
    roleplay, and a picture prompt is not roleplay.
    """
    card_lines = [
        f"Character: {_clip(companion.get('name'), 120)}",
        f"Appearance and background: {_clip(companion.get('description'), CARD_CLIP)}",
        f"Personality: {_clip(companion.get('personality'), PERSONALITY_CLIP)}",
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
        {"role": "system", "content": build_director_system(prompt_style)},
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
    prompt_style: str = DEFAULT_PROMPT_STYLE,
    chat_provider_id: str = "",
    chat_model: str = "",
) -> PromptPlan:
    """Ask the text model for a picture prompt.

    `force` is used by the manual button: the user already decided they want an
    image, so a "nothing new to draw" verdict is overridden rather than leaving
    the click doing nothing.
    """
    llm = resolve_director_llm(
        preferred_provider_id,
        chat_provider_id=chat_provider_id,
        chat_model=chat_model,
    )
    if not llm:
        raise DirectorError("没有可用的文本模型来生成图片提示词，请先在后台配置 LLM")
    logger.info(
        "picture director: using %s/%s (source=%s)",
        llm["provider_type"],
        llm["model"],
        llm.get("source") or "unknown",
    )

    messages = build_director_messages(
        companion=companion,
        transcript=transcript,
        hud=hud,
        instruction=instruction,
        style=style,
        source=source,
        prompt_style=prompt_style,
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

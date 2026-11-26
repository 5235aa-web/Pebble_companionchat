"""Character card translation service and resolution helpers.

Keeps original card fields strictly isolated from translations:
- Original columns (`name`, `description`, `personality`, etc.) are never overwritten.
- Only natural-language display fields (`description`, `personality`, `scenario`,
  `tags`, `first_mes`, `alternate_greetings`, `creator_notes`) are translated.
- Character name (`name`) and technical/instructional fields (`system_prompt`,
  `post_history_instructions`, `advanced_prompt`, `user_info`, `mes_example`,
  `card_ui`, `tts_config`) are never translated.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple

from .companion_db import get_companion, update_companion
from .llm_client import call_llm_stream, load_default_llm_config

SUPPORTED_LOCALES: Tuple[str, ...] = ("zh", "en")

# Strictly natural-language display fields; `name` and prompt-override fields
# (`system_prompt`, `post_history_instructions`, `advanced_prompt`, `user_info`,
# `mes_example`) are intentionally excluded.
DISPLAY_STRING_FIELDS: Tuple[str, ...] = (
    "description",
    "personality",
    "scenario",
    "first_mes",
    "creator_notes",
)
DISPLAY_LIST_FIELDS: Tuple[str, ...] = ("tags", "alternate_greetings")
DISPLAY_TRANSLATABLE_FIELDS: Tuple[str, ...] = DISPLAY_STRING_FIELDS + DISPLAY_LIST_FIELDS

_LOCALE_LABELS: Dict[str, str] = {
    "zh": "Simplified Chinese (简体中文)",
    "en": "English",
}

_FIELD_HINTS: Dict[str, str] = {
    "description": "character appearance, background, and profile description",
    "personality": "character personality traits and behavioral summary",
    "scenario": "scene setting and premise",
    "first_mes": "character's opening greeting message",
    "alternate_greetings": "an alternate opening greeting message",
    "creator_notes": "creator's notes about how to use the card",
    "tags": "short character card tags",
}


def normalize_locale(raw: Any) -> str:
    """Return `'zh'`, `'en'`, or `''` (original)."""
    val = str(raw or "").strip().lower()
    if val in SUPPORTED_LOCALES:
        return val
    return ""


def _clean_string_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) if x is not None else "" for x in raw]


def normalize_translations(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Sanitize a stored `translations` mapping into `{locale: {field: value}}`."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for loc in SUPPORTED_LOCALES:
        bucket = raw.get(loc)
        if not isinstance(bucket, dict):
            continue
        cleaned: Dict[str, Any] = {}
        for field in DISPLAY_STRING_FIELDS:
            val = bucket.get(field)
            if isinstance(val, str):
                cleaned[field] = val
        for field in DISPLAY_LIST_FIELDS:
            if isinstance(bucket.get(field), list):
                cleaned[field] = _clean_string_list(bucket[field])
        if isinstance(bucket.get("translated_at"), str) and bucket["translated_at"].strip():
            cleaned["translated_at"] = bucket["translated_at"].strip()
        if cleaned:
            out[loc] = cleaned
    return out


def resolve_companion_fields(
    data: Dict[str, Any],
    *,
    for_prompt: bool = False,
) -> Dict[str, Any]:
    """Return a shallow copy of *data* with active translation applied if enabled.

    When *for_prompt* is True and `data['prompt_use_original']` is truthy,
    original fields are preserved verbatim regardless of `active_locale`.
    Original `name` and non-display fields are always preserved.
    """
    if not isinstance(data, dict):
        return data
    if for_prompt and bool(data.get("prompt_use_original")):
        return dict(data)

    locale = normalize_locale(data.get("active_locale"))
    if not locale:
        return dict(data)

    translations = normalize_translations(data.get("translations"))
    bucket = translations.get(locale)
    if not bucket:
        return dict(data)

    resolved = dict(data)
    for field in DISPLAY_STRING_FIELDS:
        tr = bucket.get(field)
        if isinstance(tr, str) and tr.strip():
            resolved[field] = tr

    orig_tags = data.get("tags")
    tr_tags = bucket.get("tags")
    if isinstance(tr_tags, list):
        non_empty_tags = [t.strip() for t in tr_tags if isinstance(t, str) and t.strip()]
        if non_empty_tags or (isinstance(orig_tags, list) and len(orig_tags) == 0):
            resolved["tags"] = non_empty_tags

    orig_alts = data.get("alternate_greetings")
    tr_alts = bucket.get("alternate_greetings")
    if isinstance(tr_alts, list) and isinstance(orig_alts, list):
        merged_alts: List[str] = []
        for idx, orig_val in enumerate(orig_alts):
            tr_item = tr_alts[idx] if idx < len(tr_alts) else ""
            if isinstance(tr_item, str) and tr_item.strip():
                merged_alts.append(tr_item)
            else:
                merged_alts.append(orig_val)
        resolved["alternate_greetings"] = merged_alts
    elif isinstance(tr_alts, list):
        non_empty_alts = [a for a in tr_alts if isinstance(a, str) and a.strip()]
        if non_empty_alts:
            resolved["alternate_greetings"] = non_empty_alts

    return resolved


def _strip_echoed_wrapper(raw: str, original: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^\s*<TEXT_TO_TRANSLATE>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*</TEXT_TO_TRANSLATE>\s*$", "", text, flags=re.IGNORECASE)
    orig_stripped = (original or "").lstrip()
    if not orig_stripped.startswith("```"):
        fence = re.match(r"^```(?:[a-zA-Z0-9_-]+)?\s*\n([\s\S]*?)\n```\s*$", text)
        if fence:
            text = fence.group(1)
    return text.strip()


def _parse_json_string_list(raw: str, expected_len: int) -> List[str]:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, list):
                out = [str(item).strip() for item in parsed if item is not None]
                if out:
                    return out[:expected_len] if expected_len > 0 else out
        except (ValueError, TypeError):
            pass
    lines = [
        re.sub(r"^[-*•\d.)\s]+", "", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    return lines[:expected_len] if expected_len > 0 else lines


def _build_prose_messages(
    *,
    text: str,
    target_locale: str,
    field_name: str,
    char_name: str,
) -> List[Dict[str, str]]:
    lang_label = _LOCALE_LABELS[target_locale]
    field_hint = _FIELD_HINTS.get(field_name, field_name)
    system_prompt = (
        "You are an expert literary translator for interactive roleplay character cards.\n"
        f"Translate ONLY natural-language prose into {lang_label}.\n"
        "Strict preservation rules (NEVER violate these):\n"
        "1. Keep the character's original name unchanged (`" + (char_name or "{{char}}") + "`) whenever it appears as a proper name.\n"
        "2. NEVER translate, rename, or alter placeholders and CBS macros: `{{char}}`, `{{user}}`, `<user>`, `<char>`, `<START>`, "
        "`{{getvar::...}}`, `{{setvar::...}}`, `{{#when...}}`, `{{:else}}`, `{{/when}}`, `{{#if...}}`, `{{hud_...}}`, `{{assetlist}}`, etc.\n"
        "3. Preserve all HTML/XML tag names, attributes, inline CSS styles, Markdown syntax (`*action*`, `> quote`, `# heading`), "
        "structural brackets/delimiters (e.g. `<status>`, `</status>`, `【...】`, `[...]`), URLs, and `embedded://` asset paths. "
        "Translate only the human-readable text inside or around them.\n"
        "4. Preserve paragraph breaks, line structure, tone, and formatting.\n"
        "5. Output ONLY the translated text itself — no explanations, no commentary, no `<TEXT_TO_TRANSLATE>` tags, and no extra Markdown code fences."
    )
    user_prompt = (
        f"Target language: {lang_label}\n"
        f"Field: {field_name} ({field_hint})\n"
        f"Character original name (keep unchanged): {char_name or '{{char}}'}\n\n"
        "Translate the text inside <TEXT_TO_TRANSLATE>...</TEXT_TO_TRANSLATE>:\n"
        "<TEXT_TO_TRANSLATE>\n"
        f"{text}\n"
        "</TEXT_TO_TRANSLATE>"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_tags_messages(tags: Sequence[str], target_locale: str) -> List[Dict[str, str]]:
    lang_label = _LOCALE_LABELS[target_locale]
    system_prompt = (
        f"Translate the following JSON array of character card tags into {lang_label}.\n"
        "Keep proper nouns/abbreviations clear and natural, keep the exact same item count and order, "
        "and return ONLY a valid JSON array of strings."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(list(tags), ensure_ascii=False)},
    ]


async def _collect_translation_text(
    llm_config: Dict[str, Any],
    messages: List[Dict[str, str]],
    *,
    source_chars: int,
) -> str:
    if not llm_config or not llm_config.get("provider_type"):
        raise ValueError("No LLM model configured. Please configure an AI model in Backstage settings.")
    max_tokens = max(2048, min(8192, int(source_chars * 2.2) + 512))
    chunks: List[str] = []
    error_msg = ""
    async for ev in call_llm_stream(
        provider_type=str(llm_config["provider_type"]),
        api_key=str(llm_config.get("api_key") or ""),
        base_url=str(llm_config.get("base_url") or ""),
        model=str(llm_config.get("model") or ""),
        messages=messages,
        temperature=0.2,
        top_p=0.9,
        max_tokens=max_tokens,
        enable_thinking=False,
    ):
        kind = ev.get("type")
        if kind == "content":
            chunks.append(str(ev.get("content") or ""))
        elif kind == "error":
            error_msg = str(ev.get("message") or "LLM request failed")
            break
    if error_msg and not chunks:
        raise RuntimeError(error_msg)
    return "".join(chunks)


def _plan_translation_tasks(
    companion: Dict[str, Any],
    requested_fields: Optional[Sequence[str]],
) -> List[Tuple[str, Any]]:
    """Build ordered `(task_key, source_value)` steps for display-only fields."""
    req_set = {str(f).strip() for f in (requested_fields or []) if str(f).strip()}
    tasks: List[Tuple[str, Any]] = []

    for field in ("description", "personality", "scenario", "first_mes", "creator_notes"):
        if req_set and field not in req_set:
            continue
        val = str(companion.get(field) or "")
        if val.strip():
            tasks.append((field, val))

    if not req_set or "tags" in req_set:
        raw_tags = companion.get("tags") or []
        if isinstance(raw_tags, list):
            clean_tags = [str(t).strip() for t in raw_tags if isinstance(t, str) and t.strip()]
            if clean_tags:
                tasks.append(("tags", clean_tags))

    raw_alts = companion.get("alternate_greetings") or []
    if isinstance(raw_alts, list):
        for idx, alt in enumerate(raw_alts):
            key = f"alternate_greetings:{idx}"
            if req_set and "alternate_greetings" not in req_set and key not in req_set:
                continue
            text = str(alt or "")
            if text.strip():
                tasks.append((key, text))

    return tasks


async def iter_translate_companion_card(
    companion_id: str,
    *,
    target_locale: str,
    fields: Optional[Sequence[str]] = None,
    activate: bool = True,
    llm_config: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream per-field LLM translations for *companion_id* without touching original columns."""
    locale = normalize_locale(target_locale)
    if not locale:
        raise ValueError("Unsupported target locale; expected 'zh' or 'en'.")

    companion = get_companion(companion_id)
    if not companion:
        raise ValueError("Companion not found")

    cfg = llm_config or load_default_llm_config() or {}
    if not cfg.get("provider_type"):
        raise ValueError("No LLM model configured. Please configure an AI model in Backstage settings.")

    tasks = _plan_translation_tasks(companion, fields)
    translations = normalize_translations(companion.get("translations"))
    bucket = dict(translations.get(locale) or {})

    if not tasks:
        if activate:
            update_companion(companion_id, {"active_locale": locale})
        latest = get_companion(companion_id) or companion
        yield {
            "type": "done",
            "locale": locale,
            "done": 0,
            "total": 0,
            "translations": latest.get("translations", translations),
            "companion": latest,
        }
        return

    char_name = str(companion.get("name") or "").strip()
    total = len(tasks)
    orig_alts = companion.get("alternate_greetings") if isinstance(companion.get("alternate_greetings"), list) else []
    alt_list: List[str] = list(bucket.get("alternate_greetings") or [])
    if len(alt_list) < len(orig_alts):
        alt_list.extend([""] * (len(orig_alts) - len(alt_list)))

    yield {"type": "start", "locale": locale, "done": 0, "total": total}

    for idx, (task_key, source_val) in enumerate(tasks, start=1):
        yield {"type": "progress", "locale": locale, "done": idx - 1, "total": total, "field": task_key}

        if task_key == "tags":
            raw_reply = await _collect_translation_text(
                cfg,
                _build_tags_messages(source_val, locale),
                source_chars=sum(len(t) for t in source_val),
            )
            translated_tags = _parse_json_string_list(raw_reply, len(source_val))
            if translated_tags:
                bucket["tags"] = translated_tags
        elif task_key.startswith("alternate_greetings:"):
            alt_idx = int(task_key.split(":", 1)[1])
            raw_reply = await _collect_translation_text(
                cfg,
                _build_prose_messages(
                    text=source_val,
                    target_locale=locale,
                    field_name="alternate_greetings",
                    char_name=char_name,
                ),
                source_chars=len(source_val),
            )
            cleaned = _strip_echoed_wrapper(raw_reply, source_val)
            if cleaned:
                if alt_idx >= len(alt_list):
                    alt_list.extend([""] * (alt_idx + 1 - len(alt_list)))
                alt_list[alt_idx] = cleaned
                bucket["alternate_greetings"] = alt_list[: max(len(orig_alts), len(alt_list))]
        else:
            raw_reply = await _collect_translation_text(
                cfg,
                _build_prose_messages(
                    text=source_val,
                    target_locale=locale,
                    field_name=task_key,
                    char_name=char_name,
                ),
                source_chars=len(source_val),
            )
            cleaned = _strip_echoed_wrapper(raw_reply, source_val)
            if cleaned:
                bucket[task_key] = cleaned

        bucket["translated_at"] = datetime.now(timezone.utc).isoformat()
        translations[locale] = bucket
        update_companion(companion_id, {"translations": translations})

        yield {"type": "field_done", "locale": locale, "done": idx, "total": total, "field": task_key}

    if activate:
        update_companion(companion_id, {"active_locale": locale})

    latest = get_companion(companion_id) or companion
    yield {
        "type": "done",
        "locale": locale,
        "done": total,
        "total": total,
        "translations": latest.get("translations", translations),
        "companion": latest,
    }

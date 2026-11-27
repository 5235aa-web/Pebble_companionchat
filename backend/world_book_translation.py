"""Display-only LLM translation service for World Book (Lore) entries.

Design invariants:
- Original `comment` and `content` columns in `world_book_entries` are NEVER modified.
- `WorldBookEngine` continues reading and injecting ONLY original `comment`/`content` into LLM prompts.
- Translations live strictly in `world_book_entries.translations` as `{locale: {comment, content, translated_at}}`.
- Natural-language prose is translated while code blocks, regexes, HTML/CSS, `<pimg="...">`/`<img="...">`
  tags, asset keys, URLs, and CBS/Risu macros (`{{...}}`, `$1`, etc.) are preserved verbatim.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Set, Tuple

from .card_translation import (
    _LOCALE_LABELS,
    _collect_translation_text,
    _strip_echoed_wrapper,
    normalize_locale,
)
from .llm_client import load_default_llm_config
from .world_book_db import (
    get_entries,
    get_world_book,
    normalize_entry_translations,
    update_entry_translations,
)

_COMMENT_TAG_RE = re.compile(r"<WB_COMMENT>\s*([\s\S]*?)\s*</WB_COMMENT>", re.IGNORECASE)
_CONTENT_TAG_RE = re.compile(r"<WB_CONTENT>\s*([\s\S]*?)\s*</WB_CONTENT>", re.IGNORECASE)


def _build_entry_messages(
    *,
    comment: str,
    content: str,
    target_locale: str,
) -> List[Dict[str, str]]:
    """Build prompts asking the LLM to translate natural language in comment/content only."""
    lang_label = _LOCALE_LABELS[target_locale]
    has_comment = bool(comment.strip())
    has_content = bool(content.strip())

    output_parts: List[str] = []
    if has_comment:
        output_parts.append("<WB_COMMENT>...</WB_COMMENT>")
    if has_content:
        output_parts.append("<WB_CONTENT>...</WB_CONTENT>")
    output_spec = " and ".join(output_parts)

    system_prompt = (
        "You are an expert translator for interactive character-card World Book (Lorebook) entries.\n"
        f"Translate ONLY human-readable natural-language prose into {lang_label}.\n"
        "Strict preservation rules (NEVER violate these):\n"
        "1. NEVER translate, modify, escape, simplify, or omit:\n"
        "   - Fenced code blocks (```...```) and inline code (`...`)\n"
        "   - Regular expressions, patterns, flags, and backreferences (`$1`, `$2`, `(?:...)`, character classes)\n"
        "   - HTML/XML/custom tags, tag names, attributes, CSS selectors, style blocks, and class names "
        "(e.g., `<pimg=\"...\">`, `<img=\"...\">`, `<status>`, `<div class=\"...\">`)\n"
        "   - Exact image/asset keys, filenames, suffixes, path strings, and literal trigger/variable identifiers "
        "(keep quoted tokens, code identifiers, pipe/slash asset lists, `embedded://...`, and URLs verbatim)\n"
        "   - Template and CBS/Risu macros (`{{char}}`, `{{user}}`, `{{raw::...}}`, `{{getvar::...}}`, `{{#when...}}`, `${...}`)\n"
        "2. Preserve Markdown syntax, tables, headings, lists, indentation, and line structure.\n"
        "3. If a line or block is purely code, regex, asset key/list, or markup with no prose, keep it 100% verbatim.\n"
        f"4. Return ONLY {output_spec} containing the translated result — no commentary before or after the tags."
    )

    sections: List[str] = [f"Target language: {lang_label}", ""]
    if has_comment:
        sections.extend(
            [
                "Source entry title/comment:",
                "<WB_COMMENT>",
                comment,
                "</WB_COMMENT>",
                "",
            ]
        )
    if has_content:
        sections.extend(
            [
                "Source entry content:",
                "<WB_CONTENT>",
                content,
                "</WB_CONTENT>",
            ]
        )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(sections).strip()},
    ]


def _parse_entry_reply(
    raw_reply: str,
    *,
    orig_comment: str,
    orig_content: str,
) -> Tuple[str, str]:
    """Extract translated comment and content from `<WB_COMMENT>` / `<WB_CONTENT>` wrappers."""
    text = (raw_reply or "").strip()
    has_comment = bool(orig_comment.strip())
    has_content = bool(orig_content.strip())

    tr_comment = ""
    tr_content = ""

    m_comment = _COMMENT_TAG_RE.search(text)
    if m_comment:
        tr_comment = _strip_echoed_wrapper(m_comment.group(1), orig_comment)

    m_content = _CONTENT_TAG_RE.search(text)
    if m_content:
        tr_content = _strip_echoed_wrapper(m_content.group(1), orig_content)

    # Graceful fallbacks if a model omits one tag when only a single section was requested
    if has_comment and not has_content and not tr_comment:
        tr_comment = _strip_echoed_wrapper(text, orig_comment)
    elif has_content and not has_comment and not tr_content:
        tr_content = _strip_echoed_wrapper(text, orig_content)
    elif has_comment and has_content and not tr_content and not m_content:
        remainder = _COMMENT_TAG_RE.sub("", text).strip()
        if remainder:
            tr_content = _strip_echoed_wrapper(remainder, orig_content)

    return tr_comment, tr_content


def _entry_has_translation(entry: Dict[str, Any], locale: str) -> bool:
    """Return True when *entry* already holds a usable translation for *locale*."""
    translations = normalize_entry_translations(entry.get("translations"))
    bucket = translations.get(locale) or {}
    orig_comment = str(entry.get("comment") or "").strip()
    orig_content = str(entry.get("content") or "").strip()
    tr_comment = str(bucket.get("comment") or "").strip()
    tr_content = str(bucket.get("content") or "").strip()
    if orig_content and not tr_content:
        return False
    if orig_comment and not tr_comment:
        return False
    return bool(tr_comment or tr_content)


async def iter_translate_world_book(
    world_book_id: int,
    *,
    target_locale: str,
    entry_ids: Optional[Sequence[int]] = None,
    force: bool = False,
    llm_config: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream per-entry LLM translations into `world_book_entries.translations`."""
    locale = normalize_locale(target_locale)
    if not locale:
        raise ValueError("Unsupported target locale; expected 'zh' or 'en'.")

    book = get_world_book(world_book_id)
    if not book:
        raise ValueError("World book not found")

    cfg = llm_config or load_default_llm_config() or {}
    if not cfg.get("provider_type"):
        raise ValueError("No LLM model configured. Please configure an AI model in Backstage settings.")

    entries = get_entries(world_book_id)
    req_ids: Optional[Set[int]] = (
        {int(eid) for eid in entry_ids if eid is not None} if entry_ids is not None else None
    )

    tasks: List[Dict[str, Any]] = []
    skipped = 0
    for entry in entries:
        eid = int(entry["id"])
        if req_ids is not None and eid not in req_ids:
            continue
        orig_comment = str(entry.get("comment") or "")
        orig_content = str(entry.get("content") or "")
        if not orig_comment.strip() and not orig_content.strip():
            skipped += 1
            continue
        if not force and _entry_has_translation(entry, locale):
            skipped += 1
            continue
        tasks.append(entry)

    total = len(tasks)
    if total == 0:
        yield {
            "type": "done",
            "locale": locale,
            "done": 0,
            "total": 0,
            "skipped": skipped,
            "entries": entries,
        }
        return

    yield {
        "type": "start",
        "locale": locale,
        "done": 0,
        "total": total,
        "skipped": skipped,
    }

    for idx, entry in enumerate(tasks, start=1):
        eid = int(entry["id"])
        orig_comment = str(entry.get("comment") or "")
        orig_content = str(entry.get("content") or "")
        yield {
            "type": "progress",
            "locale": locale,
            "done": idx - 1,
            "total": total,
            "entry_id": eid,
            "comment": orig_comment,
        }

        messages = _build_entry_messages(
            comment=orig_comment,
            content=orig_content,
            target_locale=locale,
        )
        raw_reply = await _collect_translation_text(
            cfg,
            messages,
            source_chars=len(orig_comment) + len(orig_content),
            max_token_cap=16384,
        )
        tr_comment, tr_content = _parse_entry_reply(
            raw_reply,
            orig_comment=orig_comment,
            orig_content=orig_content,
        )

        translations = normalize_entry_translations(entry.get("translations"))
        bucket = dict(translations.get(locale) or {})
        if tr_comment:
            bucket["comment"] = tr_comment
        elif not orig_comment.strip():
            bucket["comment"] = ""
        if tr_content:
            bucket["content"] = tr_content
        elif not orig_content.strip():
            bucket["content"] = ""
        bucket["translated_at"] = datetime.now(timezone.utc).isoformat()
        translations[locale] = bucket

        update_entry_translations(eid, translations)
        yield {
            "type": "entry_done",
            "locale": locale,
            "done": idx,
            "total": total,
            "entry_id": eid,
            "translations": normalize_entry_translations(translations),
        }

    latest_entries = get_entries(world_book_id)
    yield {
        "type": "done",
        "locale": locale,
        "done": total,
        "total": total,
        "skipped": skipped,
        "entries": latest_entries,
    }

"""
scene_compaction.py - Scene-wise compaction for script mode.

Ordinary compaction merges every summary into one, because open chat only needs
the latest state. A playthrough is different: every segment stays readable on
its own, and a later run covers only what came after the previous card. This
module owns that rule, so the chat compaction path never has to grow a second
mode.

Compacted messages are only flagged. They keep their rows, stop rendering, and
stop feeding the model; the summary that replaces them is stored separately so
it can be placed back into the thread at the point it covers.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from .companion_db import get_messages_with_swipes, mark_messages_compacted
from .llm_client import call_llm_stream
from .script_db import add_scene_summary, list_scene_summaries, list_session_nodes
from .token_engine import count_text as count_text_tokens
from . import segmented_summary

logger = logging.getLogger(__name__)

# One summarizer request is capped per model family: a local server is far more
# sensitive to a long prompt than a cloud window, which today runs to a million
# tokens. A segment that does not fit is split into parts and the digests are
# merged, so length decides how many calls a compaction takes, never whether it
# works.
LOCAL_INPUT_TOKENS = 70_000
CLOUD_INPUT_TOKENS = 200_000
MAX_SUMMARY_CHARS = 1_700
# A part covers one slice of the segment, so it may carry less than the final
# summary: several of them are merged into it afterwards.
PART_SUMMARY_CHARS = 1_200
# The last exchange always stays verbatim, in the thread and in the context.
KEEP_RECENT = 2

# Exception text that means the model was never reached, as opposed to a model
# that answered and produced something unusable.
_NETWORK_HINTS = (
    "connect",
    "timeout",
    "timed out",
    "unreachable",
    "refused",
    "network",
    "getaddrinfo",
    "ssl",
)


def input_token_limit(llm_config: Optional[Dict]) -> int:
    """How much transcript one summarizer request may carry."""
    provider = str((llm_config or {}).get("provider_type") or "").lower()
    return LOCAL_INPUT_TOKENS if provider == "llama-cpp" else CLOUD_INPUT_TOKENS

SYSTEM_PROMPT = (
    "[Script Compaction]\n"
    "This is a scripted story, not an open chat. Summarize what has actually "
    "been played, scene by scene.\n\n"
    "Write in the third person, past tense. Follow the emotional line: what "
    "each person feels, how that feeling shifts, and what it costs them. Cover "
    "who was there, the time and place, the events, and the change in feeling. "
    "A dry list of facts is a failure.\n\n"
    "Label the parts:\n"
    "- Finished scenes: [Scene 1 to Scene 3: title / title / title]\n"
    "- The scene still running: [Scene 4: title]\n\n"
    "Rules:\n"
    "- Under 1700 characters in total.\n"
    "- No bullet lists, and no headings other than those labels.\n"
    "- Compact but alive: keep the emotional throughline."
)


def _scene_index(message: Dict, fallback: int) -> int:
    """Scene a message belongs to. Rows written before scene tagging fall back
    to the running scene."""
    meta = message.get("metadata") or {}
    if isinstance(meta, dict):
        value = meta.get("scene_index")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    return fallback


def _scene_label(index: int, titles: List[str], current_index: int) -> str:
    name = titles[index] if 0 <= index < len(titles) else ""
    head = f"[Scene {index + 1}{': ' + name if name else ''}]"
    kind = "finished" if index < current_index else "current"
    return f"{head} ({kind})"


def _transcript_lines(
    messages: List[Dict],
    titles: List[str],
    current_index: int,
    character_name: str,
) -> List[str]:
    """Flatten the segment into scene-labelled prose for the summarizer."""
    lines: List[str] = []
    last_scene = -1
    for msg in messages:
        scene = _scene_index(msg, current_index)
        if scene != last_scene:
            lines.append(_scene_label(scene, titles, current_index))
            last_scene = scene
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        who = character_name if msg.get("role") == "assistant" else "The user"
        lines.append(f"{who}: {text}")
    return lines


def _count_lines(lines: List[str]) -> int:
    return sum(count_text_tokens(line).tokens + 4 for line in lines)


def _trim_lines(lines: List[str], limit: int) -> List[str]:
    """Keep the newest lines and drop the oldest.

    A recap is read against what is happening now, so the recent past matters
    more than the opening. The marker records that the cut happened instead of
    letting the model believe the story started there.
    """
    total = 0
    kept: List[str] = []
    for line in reversed(lines):
        cost = count_text_tokens(line).tokens + 4
        if total + cost > limit:
            kept.append("(earlier turns omitted)")
            break
        total += cost
        kept.append(line)
    kept.reverse()
    return kept


def _clean_summary(text: str) -> str:
    body = (text or "").strip()
    # Models sometimes wrap the whole answer in a fence even when told not to.
    body = re.sub(r"^```[a-zA-Z]*\n", "", body)
    body = re.sub(r"\n```$", "", body).strip()
    if len(body) > MAX_SUMMARY_CHARS:
        body = body[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return body


PART_PROMPT = (
    "[Script Compaction · part {index} of {total}]\n"
    "This is one slice of a scripted playthrough that is too long to summarize in a "
    "single pass. Summarize only what happens inside this slice.\n\n"
    "Write in the third person, past tense. Follow the emotional line: what each person "
    "feels, how that feeling shifts, and what it costs them. Cover who was there, the "
    "time and place, the events, and the change in feeling. A dry list of facts is a "
    "failure.\n\n"
    "Rules:\n"
    "- Under {chars} characters.\n"
    "- No bullet lists, no headings.\n"
    "- Do not describe anything outside this slice, and do not invent an ending: later "
    "slices continue the same story."
)

MERGE_PROMPT = (
    "[Script Compaction · merge]\n"
    "The slices below summarize one continuous playthrough, in order. Merge them into a "
    "single narrative summary.\n\n"
    "Write in the third person, past tense. Keep the emotional line continuous: a reader "
    "must not be able to tell where one slice ends and the next begins. Drop repetition, "
    "and keep every event that changes the story.\n\n"
    "Rules:\n"
    "- Under {chars} characters in total.\n"
    "- No bullet lists, and no headings other than the scene labels.\n"
    "- Merge, do not concatenate."
)


def _line_tokens(text: str) -> int:
    return count_text_tokens(text).tokens


async def _summarize_with(system: str, user_text: str, llm_config: Dict) -> str:
    """One summarizer call with an explicit prompt."""
    out = ""
    async for chunk in call_llm_stream(
        provider_type=llm_config.get("provider_type", "openai"),
        api_key=llm_config.get("api_key", ""),
        base_url=llm_config.get("base_url", ""),
        model=llm_config.get("model", "gpt-4"),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        temperature=0.4,
    ):
        if chunk.get("type") == "content":
            out += chunk.get("content", "")
        elif chunk.get("type") == "error":
            raise RuntimeError(chunk.get("message") or "summary request failed")
    return out.strip()


async def _summarize(transcript: str, llm_config: Dict) -> str:
    text = await _summarize_with(
        SYSTEM_PROMPT, "Summarize the playthrough below.\n\n" + transcript, llm_config
    )
    return _clean_summary(text)


async def _summarize_part(part: str, index: int, total: int, llm_config: Dict) -> str:
    """Digest one slice of an over-long segment."""
    return await _summarize_with(
        PART_PROMPT.format(index=index, total=total, chars=PART_SUMMARY_CHARS),
        "Transcript slice:\n\n" + part,
        llm_config,
    )


async def _merge_group(group: List[str], llm_config: Dict) -> str:
    """Fold several slice digests into one narrative."""
    return await _summarize_with(
        MERGE_PROMPT.format(chars=MAX_SUMMARY_CHARS),
        "Slices to merge:\n\n" + "\n\n---\n\n".join(group),
        llm_config,
    )


async def compact_scene(
    companion_id: str,
    branch_id: str,
    session_id: int,
    script_id: int,
    current_index: int,
    character_name: str,
    llm_config: Optional[Dict] = None,
    allow_trim: bool = False,
) -> Dict[str, Any]:
    """Summarize everything between the previous card and the last exchange.

    Returns a result dict; `ok` is False when there is nothing eligible, and the
    caller surfaces `error` as is.
    """
    messages = get_messages_with_swipes(companion_id, branch_id)
    summaries = list_scene_summaries(companion_id, branch_id)
    last_covered = int(summaries[-1]["message_to_id"]) if summaries else 0

    pending = [m for m in messages if int(m.get("id") or 0) > last_covered]
    if len(pending) <= KEEP_RECENT:
        return {
            "ok": False,
            "error": "not_enough_messages",
            "kept": KEEP_RECENT,
            "pending": len(pending),
        }

    to_compact = pending[:-KEEP_RECENT]
    titles = [n.get("title") or "" for n in list_session_nodes(session_id)]
    lines = _transcript_lines(to_compact, titles, current_index, character_name)
    total_tokens = _count_lines(lines)

    cfg = llm_config or {}
    limit = input_token_limit(cfg)

    # Too long for one request no longer means dropping the opening: the segment
    # is split, each part digested on its own, then the digests merged. Length
    # decides how many calls a compaction takes, never whether it works.
    parts = segmented_summary.pack_parts(lines, limit, _line_tokens)

    trimmed = False
    if len(parts) == 1 and total_tokens > limit:
        if not allow_trim:
            return {
                "ok": False,
                "error": "needs_trim",
                "tokens": total_tokens,
                "limit": limit,
            }
        # One turn bigger than the whole window is the only case left where a
        # lossy pass is unavoidable, and only when the caller already agreed.
        lines = _trim_lines(lines, limit)
        parts = ["\n\n".join(lines)]
        trimmed = True

    if not parts or not any(part.strip() for part in parts):
        return {"ok": False, "error": "empty_transcript"}

    try:
        if len(parts) == 1:
            summary = await _summarize(parts[0], cfg)
        else:
            digests: List[str] = []
            for index, part in enumerate(parts, 1):
                digest = await _summarize_part(part, index, len(parts), cfg)
                if not digest:
                    logger.warning(
                        "Scene compaction: part %s/%s came back empty", index, len(parts)
                    )
                    return {"ok": False, "error": "empty_summary", "parts": len(parts)}
                digests.append(digest)
            merged = await segmented_summary.merge_digests(
                digests,
                lambda group: _merge_group(list(group), cfg),
                _line_tokens,
                limit,
                MAX_SUMMARY_CHARS,
            )
            if not merged:
                return {"ok": False, "error": "empty_summary", "parts": len(parts)}
            # The merged digests are still slices, not a summary: one more pass
            # turns them into the single narrative the thread will show.
            summary = await _summarize(merged, cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        logger.warning("Scene compaction failed: %s", exc)
        detail = str(exc)
        lowered = detail.lower()
        if any(hint in lowered for hint in _NETWORK_HINTS):
            return {"ok": False, "error": "llm_unavailable", "detail": detail}
        return {"ok": False, "error": "llm_error", "detail": detail}

    if not summary:
        return {"ok": False, "error": "empty_summary"}

    scenes = sorted({_scene_index(m, current_index) for m in to_compact})
    scene_from = scenes[0] + 1
    scene_to = scenes[-1] + 1
    scene_titles = " / ".join(
        titles[i] if 0 <= i < len(titles) and titles[i] else f"Scene {i + 1}"
        for i in scenes
    )

    summary_id = add_scene_summary(
        companion_id=companion_id,
        branch_id=branch_id,
        session_id=session_id,
        scene_from=scene_from,
        scene_to=scene_to,
        scene_titles=scene_titles,
        content=summary,
        message_from_id=int(to_compact[0].get("id") or 0),
        message_to_id=int(to_compact[-1].get("id") or 0),
    )
    compacted = mark_messages_compacted(
        companion_id,
        branch_id,
        [int(m["id"]) for m in to_compact if isinstance(m.get("id"), int)],
        f"scene_{summary_id}",
    )

    logger.info(
        "Scene compaction: scenes %s-%s, %s messages folded, summary #%s",
        scene_from,
        scene_to,
        compacted,
        summary_id,
    )
    return {
        "ok": True,
        "summary_id": summary_id,
        "summary": summary,
        "scene_from": scene_from,
        "scene_to": scene_to,
        "scene_titles": scene_titles,
        "compressed": compacted,
        "kept": KEEP_RECENT,
        "trimmed": trimmed,
        "parts": len(parts),
        "tokens": total_tokens,
    }

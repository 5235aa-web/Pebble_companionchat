"""Group rooms — compaction.

The same shape as the single-companion compactor: summarise the earlier part of
the transcript so the prompt stops growing, keep the recent tail verbatim, and
replay the summary as a system note. Long transcripts are summarised in parts and
then merged, so one overflow cannot swallow the middle of the evening.

What differs is the prompt. A group summary has to survive being read by three
people who were all there: it records who is in the room, how each of them stands
with the others, and where the mood turned — not a list of topics.
"""

import logging
from typing import Dict, List, Optional

from ..llm_client import call_llm_stream
from . import db
from .memory import _format_transcript

logger = logging.getLogger(__name__)

# Compact once the room passes this many messages, keeping a recent tail intact.
COMPACT_AFTER = 60
# Only the newest exchange stays verbatim: one line from the user, one reply.
# A room produces two rows per turn, so a tail of 2 is exactly the last turn —
# everything before it is what the summary has to carry. The same number is the
# threshold: a room with three uncovered messages is worth compacting, because
# the alternative is a room whose button refuses while its context keeps growing.
KEEP_TAIL = 2
# Leave it alone until this many new messages have piled up since the last pass.
MIN_NEW_SINCE_LAST = 2
# Below this the summary is not worth storing. Sized for the smallest useful
# case: compacting a single turn must still be able to produce a stored summary,
# or the button reports "too little" on a room that visibly has more than enough.
MIN_SUMMARY_CHARS = 80
# A slice larger than this is summarised in parts instead of one pass.
PART_CHARS = 12000
MAX_PARTS = 10
SUMMARY_CHARS = 3600

SUMMARY_PROMPT = """You are writing the memory of a group conversation so the people in it can carry on after the earlier turns are dropped.

The room, and how each person is labelled in the transcript:
{roster}

Write one summary in plain prose. It is read by people who were there, not by an outsider, so write it the way one of them would remember the evening: what was said, what it cost to say it, and what changed between them.

What has to survive:
- Who is in the room, and where each of them stands with the user and with each other. A silence counts: if someone stopped answering, that belongs in the memory.
- What actually happened, in the order it matters, including what was decided, promised, or deliberately avoided.
- The emotional line: what the mood was at the start, where it turned, and where it sits now. Name the turning point when there is one.
- Anything a later turn depends on — a name, an object, a place, a wound, a joke that would land differently if forgotten.

What to leave out:
- Play-by-play of small talk, greetings and repeated gestures.
- Feelings reduced to a list of adjectives. Show them through what was done and said.

Rules:
- Plain prose, three to six paragraphs. No bullet lists, no headings, no stage directions.
- Never invent events, and never write lines for the user.
- Refer to people by name; never write "the AI" or "a character".
- Under {chars} characters."""

PART_PROMPT = """You are summarising part {index} of {total} of a long group conversation. The parts will be merged into one memory afterwards.

Cover only this part. Keep who spoke to whom, what shifted between them, and how the room felt at the end of it.

- Plain prose, no bullet lists or headings.
- Write in the language the conversation is written in.
- Under {chars} characters.
- Do not resolve anything this part leaves open, and do not invent an ending."""

MERGE_PROMPT = """You are merging several part-summaries of one group conversation into the single memory these people will carry forward.

Merge, do not concatenate: keep the through-line of how they treat each other and how that changed, drop repetition, and keep every event later turns depend on. Keep the emotional texture instead of listing moods.

- Plain prose, no bullet lists or headings.
- Write in the language the summaries are written in.
- Under {chars} characters."""


def needs_compaction(session_id: int) -> bool:
    """True when the uncovered part of the room is long enough to be worth it.

    Kept for a pre-compaction hint in the UI. Turns no longer call it — the
    summariser runs from the panel button, the same way the single thread does.

    Everything here counts *messages*: `messages_compacted` is an id boundary,
    so it is compared against message ids, never subtracted from a row count.
    """
    covered = db.latest_compaction(session_id) or {}
    boundary = int(covered.get("messages_compacted") or 0)
    uncovered = [m for m in db.list_messages(session_id) if int(m["id"]) > boundary]
    if len(uncovered) <= KEEP_TAIL:
        return False
    return len(uncovered) - KEEP_TAIL >= MIN_NEW_SINCE_LAST


def _roster(session_id: int) -> str:
    names = [str(m.get("name") or "") for m in db.list_members(session_id)]
    names = [n for n in names if n]
    return ", ".join(names) if names else "the user alone"


async def _summarize_slice(text: str, llm_config: Dict, roster: str, chars: int) -> str:
    """One pass over a transcript slice, part-split if it is long."""
    if len(text) <= PART_CHARS:
        prompt = SUMMARY_PROMPT.format(roster=roster, chars=chars)
        return await _call(prompt, text, llm_config)

    chunks: List[str] = []
    step = PART_CHARS
    for i in range(0, len(text), step):
        if len(chunks) >= MAX_PARTS - 1 and i + step < len(text):
            # The slice is longer than MAX_PARTS can hold. Fold everything left
            # into one final part rather than dropping it: a part that is too
            # long degrades the summary, a part that is missing loses the
            # evening, and the second failure is silent.
            chunks.append(text[i:])
            logger.info("group summary: tail folded into final part (%s chars left)", len(text) - i)
            break
        chunks.append(text[i : i + step])

    parts: List[str] = []
    for i, chunk in enumerate(chunks):
        prompt = PART_PROMPT.format(
            index=i + 1, total=len(chunks), chars=max(MIN_SUMMARY_CHARS, PART_CHARS // 4)
        )
        part = await _call(prompt, chunk, llm_config)
        if part:
            parts.append(part)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]

    merged = await _call(MERGE_PROMPT.format(chars=chars), "\n\n".join(parts), llm_config)
    return merged or "\n\n".join(parts)


async def _call(system_prompt: str, content: str, llm_config: Dict) -> str:
    """Single LLM round trip that returns the visible text of the answer."""
    out: List[str] = []
    try:
        async for event in call_llm_stream(
            provider_type=llm_config.get("provider_type", ""),
            api_key=llm_config.get("api_key", ""),
            base_url=llm_config.get("base_url", ""),
            model=llm_config.get("model", ""),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.45,
            top_p=0.9,
            max_tokens=llm_config.get("max_tokens") or 1200,
        ):
            if event.get("type") == "content":
                out.append(event.get("content") or "")
            elif event.get("type") == "error":
                logger.warning("group summary failed: %s", event.get("message"))
    except Exception as exc:  # noqa: BLE001 - a failed summary must not break a turn
        logger.warning("group summary call failed: %s", exc)
    return "".join(out).strip()


class CompactError(Exception):
    """Why a compaction did not happen.

    Three different problems used to collapse into a bare `None`, so the route
    reported "nothing to compact" whether the room was too short, the model was
    down, or the summary came back empty. Each is its own reason now.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def compact(session_id: int, llm_config: Dict) -> Dict:
    """Summarise the uncovered part of a room and store the result.

    Raises `CompactError` with a reason rather than returning None.
    """
    covered = db.latest_compaction(session_id) or {}
    covered_to = int(covered.get("messages_compacted") or 0)

    history = db.list_messages(session_id)
    # Narration is a direction for one moment, already spent: it is not part of
    # the story being summarised, and it must not become the id boundary either.
    # A cue is the same: naming the next speaker adds nothing to the scene.
    spoken = [
        m for m in history if str(m.get("kind") or "") not in ("narration", "cue")
    ]
    fresh = [m for m in spoken if int(m["id"]) > covered_to]
    if len(fresh) <= KEEP_TAIL:
        raise CompactError("too_short")

    slice_ = fresh[:-KEEP_TAIL]
    if not slice_:
        raise CompactError("too_short")

    roster = _roster(session_id)
    members = db.list_members(session_id)
    text = _format_transcript(slice_, members)
    summary = await _summarize_slice(text, llm_config, roster, SUMMARY_CHARS)

    if not summary:
        logger.warning("group %s: model returned no summary text", session_id)
        raise CompactError("empty")
    if len(summary) < MIN_SUMMARY_CHARS:
        logger.warning("group %s summary too short (%s chars)", session_id, len(summary))
        raise CompactError("thin")

    covered_through = int(slice_[-1]["id"])
    db.save_compaction(session_id, summary, covered_through)
    logger.info("group %s compacted through message %s", session_id, covered_through)
    return {"summary": summary, "messages_compacted": covered_through}

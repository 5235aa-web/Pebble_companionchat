"""Split an over-long transcript into summarizable parts, then fold the digests.

Both compacters (a scripted playthrough and a chat branch) hit the same wall: one
summarizer request can only carry so much text. The alternatives were to drop the
opening or to fail outright, and neither is acceptable when the whole point of the
feature is to keep the story. This module packs the transcript into request-sized
parts, digests each part on its own, then merges the digests in fan-out batches
until a single summary remains.

Nothing here knows about prompts or providers: callers pass a `count` function, so
packing works with the project's provider-dispatched counter, and a `call`
coroutine, so each compacter keeps its own summarizer prompt and voice.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Sequence

logger = logging.getLogger(__name__)

# Each merge round folds this many digests into one, so the round count stays
# small even when the transcript split into dozens of parts.
MERGE_FANIN = 8
# More rounds than this would mean the digests alone exceed the model window.
MAX_MERGE_ROUNDS = 6


def _cap(text: str, max_chars: int) -> str:
    body = (text or "").strip()
    if max_chars > 0 and len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
    return body


def pack_parts(
    rows: Sequence[str],
    budget: int,
    count: Callable[[str], int],
    max_parts: int = 0,
) -> List[str]:
    """Greedily pack `rows` into parts of at most `budget` tokens.

    A boundary may fall inside a scene or a turn. That is acceptable because each
    part is digested on its own and the merge restores the through-line; what
    matters is that every row lands in exactly one part.

    `max_parts` caps how many parts are returned. When the transcript needs more,
    the newest parts are dropped and the caller must leave those messages
    uncompacted for a later run. Dropping from the tail keeps the oldest-first
    consumption order, so no message is ever marked as folded into a summary that
    never saw it.
    """
    budget = max(2_000, int(budget))
    parts: List[str] = []
    current: List[str] = []
    used = 0

    for row in rows:
        cost = count(row) + 8
        # A single row larger than the budget still gets its own part; splitting
        # mid-message would cut a sentence in half, and the summarizer copes.
        if current and used + cost > budget:
            parts.append("\n\n".join(current))
            current = []
            used = 0
        current.append(row)
        used += cost
    if current:
        parts.append("\n\n".join(current))

    if max_parts and len(parts) > max_parts:
        return parts[:max_parts]
    return parts


async def merge_digests(
    digests: Sequence[str],
    call: Callable[[Sequence[str]], Awaitable[str]],
    count: Callable[[str], int],
    budget: int,
    max_chars: int,
) -> str:
    """Fold part digests until they fit one request.

    `call` receives one group of digests and must return their merged text; the
    caller owns the prompt because the voice differs per compacter.

    Returns "" when nothing usable came back. A joined result that still exceeds
    the budget is returned as-is on the last round: the final summarizer call has
    its own size handling, and a partial record beats no record.
    """
    current = [d for d in digests if d]
    if not current:
        return ""
    if len(current) == 1:
        return _cap(current[0], max_chars)

    budget = max(2_000, int(budget))
    for round_index in range(MAX_MERGE_ROUNDS):
        joined = "\n\n---\n\n".join(current)
        if count(joined) <= budget:
            return _cap(joined, max_chars)

        merged: List[str] = []
        for start in range(0, len(current), MERGE_FANIN):
            group = current[start : start + MERGE_FANIN]
            if len(group) == 1:
                merged.append(group[0])
                continue
            try:
                text = await call(group)
            except Exception:  # noqa: BLE001 - a failed merge is reported, not raised
                logger.warning(
                    "segmented summary: merge round %s failed", round_index, exc_info=True
                )
                return ""
            if not text or not text.strip():
                logger.warning("segmented summary: merge round %s came back empty", round_index)
                return ""
            merged.append(text.strip())
        current = merged
        if len(current) == 1:
            break

    return _cap("\n\n---\n\n".join(current), max_chars)

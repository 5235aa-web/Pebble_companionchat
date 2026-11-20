"""Group rooms — shared memory.

One memory per room, not one per character. Everyone in the room experienced the
same things, so a per-character store would mean three extractions, three times
the storage, and a consistency problem the moment someone remembers something
another one does not.

Each memory still records who it is about, which is what makes "Rhea promised to
come back" useful later without needing separate stores.

Extraction runs on the room's own cursor: it never reads or writes the
single-companion memory tables.
"""

import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional

from ..llm_client import call_llm_stream
from . import db, embeddings, retriever

logger = logging.getLogger(__name__)

# Extract after this many new messages. A room produces lines three times as
# fast as a one-to-one thread, so the batch is larger than the single-companion
# cadence: 16 messages at most six entries caps density at 0.375 per message.
EXTRACT_EVERY = 16
MAX_BATCHES = 4
PROBE_CHARS = 600

# After this many consecutive provider failures, stop calling the model for a
# while. A room with a broken key would otherwise pay one failed request per
# turn, forever.
FAIL_STREAK_LIMIT = 3
FAIL_COOLDOWN_SECONDS = 600

_tasks: set = set()
# One extraction at a time per room. Two overlapping runs read the same cursor
# and mine the same batch twice.
_inflight: set = set()
_fail_streak: Dict[int, int] = {}
_cooldown_until: Dict[int, float] = {}

GROUP_EXTRACT_PROMPT = """You are keeping the shared memory of a group conversation.

Read the transcript and extract only what will still matter later: facts about
the people, decisions, promises, changes in how they feel about each other, and
events that will be referred to again.

Rules:
- One memory per entry, no lists or line breaks inside `content`.
- Start with who it is about when it matters ("Aslyn told the room ...").
- Skip small talk, passing moods, and anything that only makes sense right now.
- Do NOT repeat anything already covered in "Already remembered" below, even if
  phrased differently or in another language.
- If a new fact updates, resolves, or contradicts an existing memory (e.g. a
  promise fulfilled/broken, feelings changed), set "supersedes" to that old
  memory's numeric id (or opening words) so the outdated entry is retired.
  Omit "supersedes" otherwise.
- Give each memory 5-8 comma-separated trigger keywords in `keywords`. Include
  core words, colloquial synonyms, bilingual (Chinese + English) equivalents,
  and likely situational cues so future messages match even with different
  wording (e.g. for fear of thunder: "thunder,lightning,storm,rain,打雷,闪电,雷雨").
- At most 6 entries. Fewer is better than padded. Return [] if nothing new.
- Reply with a JSON array only, no prose:
  [{"content": "...", "keywords": "kw1,kw2,kw3,...", "about": "name or empty", "supersedes": "optional id or prefix"}]

Already remembered:
__EXISTING__

Transcript:
__TRANSCRIPT__"""


def _format_transcript(history: List[Dict], members: List[Dict]) -> str:
    names = {str(m.get("companion_id")): str(m.get("name") or "") for m in members}
    lines = []
    for msg in history:
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        sid = msg.get("speaker_id")
        kind = str(msg.get("kind") or "")
        if kind == "narration":
            # A stage direction is not something anyone said. Mining it would
            # turn "the lamp flickers" into a permanent fact about the room.
            continue
        if kind == "cue":
            # A card click that only names the next speaker. There is no fact in
            # "@Alice" to keep, and mining it would fill the room with memories
            # about who was called on instead of what happened.
            continue
        if sid == "user":
            who = "User"
        else:
            who = str(msg.get("speaker_name") or names.get(str(sid)) or "Someone")
        lines.append(f"{who}: {content}")
    return "\n".join(lines)


def _parse_json_array(raw: str) -> Optional[List[Dict]]:
    """Parsed entries, `[]` for an explicit empty result, `None` for a bad reply.

    The difference decides whether the cursor moves. `[]` means the model read
    the transcript and found nothing worth keeping; `None` means the reply was
    unusable and the same messages have to be retried later.
    """
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        keywords = item.get("keywords")
        if isinstance(keywords, list):
            keywords = ",".join(str(k) for k in keywords)
        out.append({
            "content": " ".join(content.split())[:400],
            "keywords": str(keywords or "").strip()[:300],
            "about": str(item.get("about") or "").strip()[:80],
            "supersedes": str(item.get("supersedes") or "").strip()[:80],
        })
    return out


async def _extract_batch(
    session_id: int,
    history: List[Dict],
    members: List[Dict],
    llm_config: Dict,
) -> Optional[int]:
    """Memories actually written, or `None` when the call itself failed.

    `None` is what keeps the cursor still. A provider error or an unparseable
    reply must not consume the batch, otherwise those messages are lost for
    good. Entries the room already holds are filtered before insertion, so the
    count reflects new rows rather than attempts.
    """
    transcript = _format_transcript(history, members)
    if not transcript.strip():
        return 0
    existing_rows = db.list_memories(session_id, limit=60)
    existing_lines = "\n".join(
        f"- [{int(m['id'])}] {m['content']}" for m in existing_rows
    ) or "(none yet)"
    prompt = (
        GROUP_EXTRACT_PROMPT
        .replace("__EXISTING__", existing_lines)
        .replace("__TRANSCRIPT__", transcript)
    )
    chunks: List[str] = []
    async for event in call_llm_stream(
        provider_type=str(llm_config.get("provider_type") or ""),
        api_key=str(llm_config.get("api_key") or ""),
        base_url=str(llm_config.get("base_url") or ""),
        model=str(llm_config.get("model") or ""),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        top_p=0.9,
        max_tokens=1100,
    ):
        if event.get("type") == "content":
            chunks.append(str(event.get("content") or ""))
        elif event.get("type") == "error":
            logger.warning("group memory extraction failed: %s", event.get("message"))
            return None
    parsed = _parse_json_array("".join(chunks))
    if parsed is None:
        logger.warning("group %s: memory extraction returned no usable JSON", session_id)
        return None
    items = parsed[:6]
    if not items:
        return 0
    known = db.memory_contents(session_id, [item["content"] for item in items])
    items = [item for item in items if item["content"] not in known]
    if not items:
        return 0

    # Retire superseded unpinned facts first so neither lexical nor optional
    # vector deduplication compares the update against the row it replaces.
    retired_ids: set = set()
    for item in items:
        target = item.get("supersedes") or ""
        if target and db.retire_superseded_memory(session_id, target):
            id_candidate = target.strip("[]# ")
            if id_candidate.isdigit():
                retired_ids.add(int(id_candidate))
            else:
                low = target.lower()
                for row in existing_rows:
                    if int(row.get("mark") or 0) == 0 and str(row["content"]).lower().startswith(low):
                        retired_ids.add(int(row["id"]))

    existing_contents = [
        str(m["content"])
        for m in existing_rows
        if int(m["id"]) not in retired_ids
    ]

    # Optional external `/v1/embeddings` pass when configured; None otherwise.
    vectors = await embeddings.encode([str(i["content"]) for i in items])
    existing_vectors = (
        [
            (int(m["id"]), m["embedding"])
            for m in db.list_memory_vectors(session_id)
            if int(m["id"]) not in retired_ids
        ]
        if vectors
        else []
    )
    stored = 0
    for idx, item in enumerate(items):
        if retriever.is_near_duplicate(item["content"], existing_contents):
            continue
        blob = embeddings.to_blob(vectors[idx]) if vectors and idx < len(vectors) else None
        if blob and existing_vectors:
            if embeddings.top_score(vectors[idx], existing_vectors) >= embeddings.DUPLICATE_SCORE:
                continue
        if db.add_memory(session_id, item["content"], item["keywords"], item["about"], blob):
            stored += 1
            existing_contents.append(item["content"])
            if blob:
                # Keep the batch self-consistent: a later entry must not squeak
                # through by being a near copy of one written a moment ago.
                existing_vectors.append((0, blob))
    dropped = db.trim_memories(session_id)
    if dropped:
        logger.info("group %s: trimmed %s memories past the ceiling", session_id, dropped)
    return stored


async def extract_memories(session_id: int, llm_config: Dict) -> int:
    """Mine the messages added since the last run. Returns memories written.

    The cursor lives in the database, not in this process. It used to be a
    module-level dict, so every restart reset it to zero and the next turn
    re-mined the tail of the room and wrote the same facts again. Each batch
    commits its cursor as soon as it succeeds: a crash costs at most one batch
    of work, never a whole conversation.
    """
    if not llm_config or not llm_config.get("model"):
        return 0
    if time.time() < _cooldown_until.get(session_id, 0.0):
        return 0
    members = db.list_members(session_id)
    cursor = db.get_memory_cursor(session_id)
    total = 0
    batches = 0
    while batches < MAX_BATCHES:
        batch = db.messages_after(session_id, cursor, EXTRACT_EVERY)
        if len(batch) < EXTRACT_EVERY:
            break
        stored = await _extract_batch(session_id, batch, members, llm_config)
        if stored is None:
            _note_failure(session_id)
            break
        _fail_streak.pop(session_id, None)
        cursor = int(batch[-1].get("id") or cursor)
        db.set_memory_cursor(session_id, cursor)
        total += stored
        batches += 1
    if total:
        logger.info("group %s: stored %s memories", session_id, total)
    return total


def _note_failure(session_id: int) -> None:
    """Count consecutive failures and pause the room once they pile up.

    Kept in memory on purpose: a restart means the user is probably fixing the
    provider, and a persisted cooldown would keep the room silent afterwards.
    """
    streak = _fail_streak.get(session_id, 0) + 1
    _fail_streak[session_id] = streak
    if streak < FAIL_STREAK_LIMIT:
        return
    _cooldown_until[session_id] = time.time() + FAIL_COOLDOWN_SECONDS
    logger.warning(
        "group %s: memory extraction paused for %ss after %s failures",
        session_id, FAIL_COOLDOWN_SECONDS, streak,
    )


def schedule_extraction(session_id: int, llm_config: Optional[Dict]) -> None:
    """Fire-and-forget extraction.

    The task holds a strong reference: asyncio only keeps weak ones, and a
    half-finished extraction would otherwise vanish mid-run. A request for a
    room that is already extracting is dropped rather than queued: extraction
    is "later is fine", and a queue would only grow during a fast exchange.
    """
    if not llm_config or session_id in _inflight:
        return
    try:
        task = asyncio.create_task(_run_extraction(session_id, dict(llm_config)))
    except RuntimeError:
        return
    _inflight.add(session_id)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_extraction(session_id: int, llm_config: Dict) -> None:
    """Wrapper that always releases the room's in-flight flag."""
    try:
        await extract_memories(session_id, llm_config)
    except Exception:  # noqa: BLE001 - extraction must never break a turn
        logger.warning("group %s: memory extraction crashed", session_id, exc_info=True)
    finally:
        _inflight.discard(session_id)


async def _backfill_vectors(session_id: int) -> None:
    """Give memories written before the model arrived a vector, once."""
    missing = db.list_memories_missing_vectors(session_id)
    if not missing:
        return
    vecs = await embeddings.encode([str(m["content"]) for m in missing])
    if not vecs:
        return
    for row, vec in zip(missing, vecs):
        db.set_memory_embedding(int(row["id"]), embeddings.to_blob(vec))
    logger.info("group %s: backfilled %s memory vectors", session_id, len(missing))


async def _vector_scores(session_id: int, probe: str) -> Optional[Dict[int, float]]:
    """Optional external `/v1/embeddings` cosine map, or None when unconfigured."""
    if not embeddings.available():
        return None
    await _backfill_vectors(session_id)
    rows = db.list_memory_vectors(session_id)
    if not rows:
        return None
    vecs = await embeddings.encode([probe])
    if not vecs:
        return None
    return embeddings.cosine_scores(
        vecs[0],
        [(int(r["id"]), r["embedding"]) for r in rows],
    )


async def build_memory_block(
    session_id: int,
    history: List[Dict],
    touch: bool = True,
    collect: Optional[List[Dict]] = None,
) -> Optional[str]:
    """What this moment reminds the room of, or None.

    The probe is the tail of the last few lines: a memory is "what this reminds
    us of", and looking too far back drags in everything.

    Uses zero-dependency hybrid BM25 + CJK n-gram + phrase-hit ranking by
    default, optionally fused with external `/v1/embeddings` cosine scores when
    configured.

    `touch` records the hits as used. Callers that only *estimate* the prompt —
    the context meter — pass False, otherwise opening the rail would mark every
    memory it happened to surface as if it had been spoken to.

    `collect` is an out-parameter, matching the single-thread side: when given,
    the hits that made it into the block are appended so the caller can show
    which memories were in play without searching a second time.
    """
    if not history:
        return None
    probe = " ".join(
        (m.get("content") or "")
        for m in history[-4:]
        if m.get("role") != "system" and str(m.get("kind") or "") != "cue"
    )[-PROBE_CHARS:]
    if not probe.strip():
        return None

    vector_scores = await _vector_scores(session_id, probe)
    hits = db.search_memories(
        session_id,
        probe,
        limit=6,
        max_required=2,
        vector_scores=vector_scores,
    )
    if not hits:
        return None

    if touch:
        db.touch_memories([int(m["id"]) for m in hits])
    if collect is not None:
        collect.extend(hits)
    return "\n".join(f"- {m['content']}" for m in hits)

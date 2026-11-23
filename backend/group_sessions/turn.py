"""Group rooms — who speaks, and the turn that follows.

Speaker selection is three layers, cheapest first:

1. **Address** — the name appears in the user's line, or they typed `@name`.
   Deterministic, and it is what a person expects when they say someone's name.
2. **Staleness** — otherwise the character who has gone longest without speaking
   gets the floor. Round-robin in order would read as a queue; longest-silent
   keeps the room feeling like a room.
3. **Fallback** — first member, used only when the room is brand new.

Interjection (a second character replying unprompted) is left for the next pass;
one speaker per turn keeps cost and confusion down, and the selection already
makes the room feel alive.
"""

import asyncio
from collections import Counter
import logging
import math
import re
from typing import AsyncGenerator, Dict, List, Optional

from ..llm_client import call_llm_stream
from ..memory_retriever import tokenize
from . import db, memory, prompt

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 4000

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# One lock per room. A local model is a single inference slot: two turns from the
# same room queue behind each other and the UI looks frozen, so the second one is
# refused rather than made to wait. The dict is keyed by room id and never grows
# past the number of rooms that have been talked in during this process.
_turn_locks: Dict[int, asyncio.Lock] = {}


def _lock_for(session_id: int) -> asyncio.Lock:
    lock = _turn_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _turn_locks[session_id] = lock
    return lock


def is_room_busy(session_id: int) -> bool:
    """Whether a turn is currently streaming in this room."""
    lock = _turn_locks.get(session_id)
    return bool(lock and lock.locked())


def _is_email_at(text: str, pos: int) -> bool:
    return pos > 0 and bool(re.match(r"[A-Za-z0-9._%+-]", text[pos - 1]))


def _has_prefix_boundary(tail: str, matched_len: int) -> bool:
    if matched_len >= len(tail):
        return True
    last_char = tail[matched_len - 1]
    next_char = tail[matched_len]
    if not _CJK.search(last_char) and re.match(r"[A-Za-z0-9]", next_char):
        return False
    return True


def _match_at_tail(tail: str, members: List[Dict]) -> Optional[Dict]:
    """Match a member at the start of text following `@`, longest name first.

    Matching prefixes by descending length handles both CJK phrases written
    without spaces (`@小美你觉得呢`) and multi-word Latin names (`@Grandma Evelyn`)
    without letting a shorter prefix steal the address.
    """
    if not tail:
        return None
    tail_low = tail.lower()

    full_candidates = sorted(
        (
            (str(m.get("name") or "").strip(), idx, m)
            for idx, m in enumerate(members)
            if str(m.get("name") or "").strip()
        ),
        key=lambda item: (-len(item[0]), item[1]),
    )
    for name, _, member in full_candidates:
        if tail_low.startswith(name.lower()) and _has_prefix_boundary(tail, len(name)):
            return member

    part_candidates = []
    for idx, m in enumerate(members):
        name = str(m.get("name") or "").strip()
        parts = name.split()
        if len(parts) <= 1:
            continue
        for part in parts:
            if len(part) >= 2 or _CJK.search(part):
                part_candidates.append((part, idx, m))
    part_candidates.sort(key=lambda item: (-len(item[0]), item[1]))
    for part, _, member in part_candidates:
        if tail_low.startswith(part.lower()) and _has_prefix_boundary(tail, len(part)):
            return member
    return None


def _find_at_addressed(members: List[Dict], text: str) -> Optional[Dict]:
    """Return the first member explicitly addressed with `@` in text order."""
    start = 0
    while True:
        pos = text.find("@", start)
        if pos < 0:
            return None
        start = pos + 1
        if _is_email_at(text, pos):
            continue
        tail = text[pos + 1 :].lstrip()
        matched = _match_at_tail(tail, members)
        if matched is not None:
            return matched


def _find_text_mentioned(members: List[Dict], text: str) -> Optional[Dict]:
    """Return the member mentioned earliest in `text` without an `@`.

    Ordering by position in the sentence rather than the roster order ensures
    "Bob, did you see Alice?" picks Bob instead of whichever name came first in
    the member list.
    """
    if not text:
        return None
    # Blank out email addresses while keeping character offsets intact so
    # neither "foo@lin.com" nor "lin@example.com" triggers member "Lin".
    scan = _EMAIL.sub(lambda m: " " * len(m.group(0)), text)
    hits = []
    for idx, member in enumerate(members):
        name = str(member.get("name") or "").strip()
        if not name:
            continue
        tokens = [(name, 0)]
        parts = name.split()
        if len(parts) > 1:
            for part in parts:
                if len(part) >= 2 or _CJK.search(part):
                    tokens.append((part, 1))
        for token, prio in tokens:
            if _CJK.search(token):
                pos = scan.find(token)
            else:
                pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
                match = re.search(pattern, scan, re.IGNORECASE)
                pos = match.start() if match else -1
            if pos >= 0:
                hits.append((pos, prio, -len(token), idx, member))
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return hits[0][4]


def _mentions(text: str, name: str) -> bool:
    """Does this line address the character by name?

    Latin names need word boundaries: a character called Lin must not fire on
    "listen", and Ann must not fire on "announce". CJK has no word boundaries,
    so those names fall back to a plain substring test.
    """
    name = (name or "").strip()
    if not name or not text:
        return False
    if _CJK.search(name):
        return name in text
    pattern = rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _answers_to(handle: str, name: str) -> bool:
    """`@evelyn` reaches "Grandma Evelyn" as well as an exact name match."""
    h = (handle or "").strip().lower()
    n = (name or "").strip().lower()
    if not h or not n:
        return False
    return h == n or h in n.split()


def _persona_token_weight(token: str) -> float:
    if token.startswith("c:"):
        # Single-char CJK feature ("c:人") is noisy; 2/3-grams carry real topics.
        return 0.35 if len(token) == 3 else (1.2 if len(token) >= 5 else 1.0)
    return 1.0


def _pick_by_persona_relevance(
    members: List[Dict],
    text: str,
    exclude_sid: str = "",
) -> Optional[Dict]:
    """Zero-LLM lexical router matching `text` against member persona fields.

    Only overrides rotation when one character clearly stands out above the
    rest; everyday small talk with weak/tied overlap returns None.
    """
    if len(members) <= 1 or not (text or "").strip():
        return None
    q_tokens = set(tokenize(text))
    if not q_tokens:
        return None

    doc_counts: List[Counter] = []
    df: Counter = Counter()
    for member in members:
        blob = " ".join(
            str(member.get(field) or "")
            for field in ("personality", "description", "scenario")
        )
        counts = Counter(tokenize(blob))
        doc_counts.append(counts)
        for tok in counts:
            df[tok] += 1

    total_docs = len(members)
    scored = []
    for member, counts in zip(members, doc_counts):
        sid = str(member.get("companion_id") or "")
        if exclude_sid and sid == exclude_sid:
            scored.append((0.0, member))
            continue
        score = 0.0
        for tok in q_tokens:
            tf = counts.get(tok, 0)
            if tf <= 0:
                continue
            idf = math.log(1.0 + (total_docs - df[tok] + 0.5) / (df[tok] + 0.5))
            tf_boost = 1.0 + 0.25 * min(tf - 1, 3)
            score += (0.8 + idf) * tf_boost * _persona_token_weight(tok)
        scored.append((score, member))

    ranked = sorted(scored, key=lambda item: item[0], reverse=True)
    best_score, best_member = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score < 1.2:
        return None
    if second_score > 0 and best_score < second_score * 1.35:
        return None
    return best_member


def pick_speaker(
    members: List[Dict],
    user_text: str,
    history: List[Dict],
    is_narration: bool = False,
) -> Optional[Dict]:
    """Choose who answers. Addressing wins; persona fit and continuity follow."""
    if not members:
        return None

    text = user_text or ""
    addressed = _find_at_addressed(members, text)
    if addressed is not None:
        return addressed

    mentioned = _find_text_mentioned(members, text)
    if mentioned is not None:
        return mentioned

    member_by_id = {str(m.get("companion_id")): m for m in members}
    spoken: Dict[str, int] = {}
    ai_turns: List[str] = []
    for idx, msg in enumerate(history):
        sid = str(msg.get("speaker_id") or "")
        if sid in member_by_id:
            spoken[sid] = idx
            ai_turns.append(sid)

    consec_sid = ""
    consec_count = 0
    if ai_turns:
        consec_sid = ai_turns[-1]
        for sid in reversed(ai_turns):
            if sid == consec_sid:
                consec_count += 1
            else:
                break

    stripped = text.strip()
    if not stripped and len(members) > 1 and history:
        # Relay / Continue right after a character spoke: if that character
        # addressed or mentioned another member, hand the mic straight to them.
        last_sid = str(history[-1].get("speaker_id") or "")
        if last_sid in member_by_id:
            others = [m for m in members if str(m.get("companion_id")) != last_sid]
            last_text = str(history[-1].get("content") or "")
            handoff = (
                _find_at_addressed(others, last_text)
                or _find_text_mentioned(others, last_text)
                or _pick_by_persona_relevance(others, last_text)
            )
            if handoff is not None:
                return handoff

    if stripped and not is_narration and len(members) > 1:
        fit = _pick_by_persona_relevance(
            members,
            stripped,
            exclude_sid=consec_sid if consec_count >= 2 else "",
        )
        if fit is not None:
            return fit

    dormant = [m for m in members if str(m.get("companion_id")) not in spoken]
    if dormant:
        return dormant[0]

    if len(members) > 1 and ai_turns and not is_narration and stripped:
        if consec_count < 2 and (stripped.endswith(("?", "？")) or len(stripped) <= 24):
            return member_by_id[consec_sid]

    return min(members, key=lambda m: spoken.get(str(m.get("companion_id")), -1))


async def generate_turn(
    session_id: int,
    llm_config: Dict,
    speaker: Dict,
    history: List[Dict],
    members: Optional[List[Dict]] = None,
    pending_message: Optional[Dict] = None,
    replace_message_id: Optional[int] = None,
) -> AsyncGenerator[Dict, None]:
    """Stream one character's reply, refusing to start while another runs.

    Yields:
        {"type": "speaker", "speaker": {...}} on the first tick,
        {"type": "content", "content": "..."} per delta,
        {"type": "error", "message": "..."} on failure or when the room is busy,
        {"type": "done", "message": {...}} with the stored row at the end.
    """
    lock = _lock_for(session_id)
    if lock.locked():
        # Refusing beats queueing: a second request would sit behind a model that
        # can only run one inference at a time, and the UI would look frozen for
        # however long the first one takes.
        yield {
            "type": "error",
            "message": "Someone is already answering in this room. Let them finish first.",
        }
        return

    await lock.acquire()
    try:
        async for event in _turn_stream(
            session_id,
            llm_config,
            speaker,
            history,
            members,
            pending_message,
            replace_message_id,
        ):
            yield event
    finally:
        lock.release()


async def _turn_stream(
    session_id: int,
    llm_config: Dict,
    speaker: Dict,
    history: List[Dict],
    members: Optional[List[Dict]] = None,
    pending_message: Optional[Dict] = None,
    replace_message_id: Optional[int] = None,
) -> AsyncGenerator[Dict, None]:
    """The body of a turn. Only ever entered while the room lock is held."""
    members = members or db.list_members(session_id)
    if not members:
        yield {"type": "error", "message": "This room has no members yet."}
        return
    if not llm_config or not llm_config.get("model"):
        yield {"type": "error", "message": "No model is configured."}
        return

    session = db.get_session(session_id)
    if not session:
        yield {"type": "error", "message": f"Room {session_id} not found."}
        return

    if replace_message_id:
        # Known gap, left for a later decision: the old reply is dropped before
        # the model is called, so a failed reroll (model error, empty answer,
        # user abort) loses it for good — the row is already gone, and the
        # client's rollback only touches its own list. A safer shape is to
        # generate first and update the row in place once text has arrived.
        db.delete_message(session_id, replace_message_id)
        history = [m for m in history if int(m.get("id") or 0) != int(replace_message_id)]

    user_row: Optional[Dict] = None
    if pending_message:
        user_row = db.add_message(
            session_id,
            pending_message["content"],
            role=pending_message.get("role", "user"),
            speaker_id=pending_message.get("speaker_id"),
            speaker_name=pending_message.get("speaker_name", ""),
            kind=pending_message.get("kind", "say"),
            attachments=pending_message.get("attachments"),
        )
        history = [*history, user_row]

    speaker_event: Dict = {
        "type": "speaker",
        "speaker": {
            "companion_id": speaker.get("companion_id"),
            "name": speaker.get("name"),
        },
    }
    if user_row is not None:
        speaker_event["user_message"] = user_row
    yield speaker_event

    memory_hits: List[Dict] = []
    memory_block = await memory.build_memory_block(session_id, history, collect=memory_hits)
    memories = [{"content": m["content"]} for m in memory_hits] or None
    compaction = db.latest_compaction(session_id)

    # The summary stands in for everything it covers. Without this cut the room
    # pays twice — once for the summary, once for the transcript it replaced —
    # and the context meter climbs after a compaction instead of dropping.
    boundary = int((compaction or {}).get("messages_compacted") or 0)
    visible = [m for m in history if int(m.get("id") or 0) > boundary] if boundary else history

    messages = prompt.build_messages(
        session, members, speaker, visible, memories=memories, compaction=compaction
    )

    chunks: List[str] = []
    async for event in call_llm_stream(
        provider_type=str(llm_config.get("provider_type") or ""),
        api_key=str(llm_config.get("api_key") or ""),
        base_url=str(llm_config.get("base_url") or ""),
        model=str(llm_config.get("model") or ""),
        messages=messages,
        temperature=float(llm_config.get("temperature") or 0.8),
        top_p=float(llm_config.get("top_p") or 0.92),
        max_tokens=llm_config.get("max_tokens"),
        top_k=llm_config.get("top_k"),
        min_p=llm_config.get("min_p"),
        repeat_penalty=llm_config.get("repeat_penalty"),
        frequency_penalty=llm_config.get("frequency_penalty"),
        presence_penalty=llm_config.get("presence_penalty"),
        enable_mtp=llm_config.get("enable_mtp"),
    ):
        kind = event.get("type")
        if kind == "content":
            delta = str(event.get("content") or "")
            if delta:
                chunks.append(delta)
                yield {"type": "content", "content": delta}
        elif kind == "error":
            yield {"type": "error", "message": str(event.get("message") or "LLM error")}
            return

    text = "".join(chunks).strip()
    if not text:
        yield {"type": "error", "message": "The model returned nothing."}
        return

    speaker_name = str(speaker.get("name") or "")
    # A model raised on cards often prefixes its own line with "Name:". Leaving
    # it in means the next turn sees the prefix twice and starts imitating it.
    text = re.sub(rf"^{re.escape(speaker_name)}\s*[:：]\s*", "", text) if speaker_name else text
    text = text[:MAX_REPLY_CHARS]

    row = db.add_message(
        session_id,
        text,
        role="assistant",
        speaker_id=str(speaker.get("companion_id") or ""),
        speaker_name=speaker_name,
        kind="say",
    )
    done_event: Dict = {
        "type": "done",
        "message": row,
        # What the room was thinking of while it answered. The shape matches the
        # single thread's in-play entries exactly, so the rail can draw them with
        # the same component and no branching in the template.
        "memories": [
            {
                "id": int(m["id"]),
                "label": str(m["content"]),
                "key": str(m.get("about") or ""),
                "weight": round(min(0.95, 0.55 + len(str(m["content"])) / 400), 2),
            }
            for m in memory_hits
        ],
    }
    if user_row is not None:
        done_event["user_message_id"] = int(user_row["id"])
    yield done_event

    # Compaction is user-driven, exactly like the single thread: the ceiling is
    # a button in the panel, not a background rule. Memory extraction still runs
    # on its own cursor — it is cheap and the room reads wrong without it.
    memory.schedule_extraction(session_id, llm_config)

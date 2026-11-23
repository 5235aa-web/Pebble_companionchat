"""Group rooms — HTTP surface.

Mounted at `/group`. Every route is scoped by a room id, and none of them touch
the single-companion endpoints: a room has its own messages, members and memory,
and deleting a room leaves companion threads alone.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db, embeddings, memory, prompt, turn
from ..attachment_parser import delete_image
from ..token_counter import estimate_tokens_messages

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/group", tags=["group"])


class SessionCreate(BaseModel):
    name: Optional[str] = None
    scene: str = ""
    user_note: str = ""
    members: Optional[List[str]] = None


class SessionPatch(BaseModel):
    name: Optional[str] = None
    scene: Optional[str] = None
    user_note: Optional[str] = None


class MemberAdd(BaseModel):
    companion_id: str


class FocusSet(BaseModel):
    companion_id: str


class MemoryPatch(BaseModel):
    content: Optional[str] = None
    keywords: Optional[str] = None
    about: Optional[str] = None
    # 0 plain, 1 pinned, 2 strong.
    mark: Optional[int] = None


class MessageIn(BaseModel):
    content: str
    kind: str = "say"


class StreamIn(BaseModel):
    message: Optional[str] = None
    kind: str = "say"
    # "narrator" means the user is directing the scene rather than speaking.
    sender: Optional[str] = None
    speaker_id: Optional[str] = None
    llm_config: Optional[Dict[str, Any]] = None
    # Images sent with this line. Stored beside the text and handed to the model
    # as multimodal parts while the line is still fresh.
    attachments: Optional[List[Dict[str, Any]]] = None


class RerollIn(BaseModel):
    message_id: int
    speaker_id: Optional[str] = None
    switch_speaker: bool = False
    llm_config: Optional[Dict[str, Any]] = None


def _require(session_id: int) -> Dict:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Room {session_id} not found")
    return session


def _sse(payload: Dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/sessions")
async def list_sessions():
    return {"sessions": db.list_sessions()}


@router.post("/sessions")
async def create_session(body: SessionCreate):
    # Unnamed rooms get group1, group2, … so the tab list reads as an ordered set
    # instead of a wall of "New room".
    name = (body.name or "").strip() or db.next_room_name()
    session = db.create_session(name, body.scene, body.user_note)
    added = []
    for companion_id in (body.members or [])[: db.MAX_MEMBERS]:
        result = db.add_member(int(session["id"]), companion_id)
        if result.get("ok"):
            added.append(companion_id)
    return {"session": db.get_session(int(session["id"])), "members": db.list_members(int(session["id"]))}


@router.get("/sessions/{session_id}")
async def get_session(session_id: int):
    session = _require(session_id)
    return {
        "session": session,
        "members": db.list_members(session_id),
        "messages": db.list_messages(session_id),
        "memories": db.list_memories(session_id, limit=50),
        "compaction": db.latest_compaction(session_id),
    }


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: int, body: SessionPatch):
    _require(session_id)
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    return {"session": db.update_session(session_id, **fields)}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    _require(session_id)
    db.delete_session(session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/members")
async def add_member(session_id: int, body: MemberAdd):
    _require(session_id)
    result = db.add_member(session_id, body.companion_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Could not add member")
    return {"members": db.list_members(session_id)}


@router.delete("/sessions/{session_id}/members/{companion_id}")
async def remove_member(session_id: int, companion_id: str):
    _require(session_id)
    db.remove_member(session_id, companion_id)
    return {"members": db.list_members(session_id)}


@router.post("/sessions/{session_id}/focus")
async def set_focus(session_id: int, body: FocusSet):
    _require(session_id)
    db.set_focus(session_id, body.companion_id)
    return {"members": db.list_members(session_id)}


@router.get("/sessions/{session_id}/memories")
async def list_memories(session_id: int):
    _require(session_id)
    return {"memories": db.list_memories(session_id)}


@router.get("/sessions/{session_id}/context")
async def context_stats(session_id: int, model: str = "", provider_type: str = ""):
    """How much of the window this room's next turn would occupy.

    Built from the same message list a turn sends, so the number matches what
    the model actually receives: system block, roster, memories, summary and
    the whole transcript. Any member works as the speaker here — the character
    section is the only part that varies, and it is one card wide.
    """
    session = _require(session_id)
    members = db.list_members(session_id)
    history = db.list_messages(session_id)
    if not members:
        return {"tokens": 0, "messages": len(history), "members": 0}

    memory_block = await memory.build_memory_block(session_id, history, touch=False)
    memories = [
        {"content": line[2:]}
        for line in (memory_block or "").splitlines()
        if line.startswith("- ")
    ]
    compaction = db.latest_compaction(session_id)
    # The same cut the turn itself makes: the meter must describe the window a
    # turn actually fills, not the one it would fill without a summary.
    boundary = int((compaction or {}).get("messages_compacted") or 0)
    visible = [m for m in history if int(m.get("id") or 0) > boundary] if boundary else history
    messages = prompt.build_messages(
        session,
        members,
        members[0],
        visible,
        memories=memories,
        compaction=compaction,
        include_images=False,
    )
    used = estimate_tokens_messages(
        messages,
        model_name=model or "gpt-4",
        provider_type=provider_type or None,
    )
    return {"tokens": used, "messages": len(history), "members": len(members)}


@router.get("/sessions/{session_id}/compactions")
async def list_compactions(session_id: int):
    """Every summary this room has written, oldest first."""
    _require(session_id)
    return {"compactions": db.list_compactions(session_id)}


@router.delete("/sessions/{session_id}/memories/{memory_id}")
async def delete_memory(session_id: int, memory_id: int):
    _require(session_id)
    db.delete_memory(session_id, memory_id)
    return {"ok": True}


@router.patch("/sessions/{session_id}/memories/{memory_id}")
async def patch_memory(session_id: int, memory_id: int, body: MemoryPatch):
    """Edit a memory. Changing the text re-encodes it, or retrieval would keep
    matching the wording that was replaced."""
    _require(session_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    if not db.update_memory(session_id, memory_id, **fields):
        raise HTTPException(status_code=404, detail="Memory not found.")
    if "content" in fields:
        vectors = await embeddings.encode([str(fields["content"])])
        db.set_memory_embedding(
            memory_id,
            embeddings.to_blob(vectors[0]) if vectors else None,
        )
    return {"ok": True}


@router.delete("/sessions/{session_id}/messages/{message_id}")
async def delete_message(session_id: int, message_id: int):
    """Remove one line from the transcript.

    Deliberately narrow: emptying the room is a separate, confirmed action. This
    drops only the row the user pointed at.
    """
    _require(session_id)
    db.delete_message(session_id, message_id)
    return {"ok": True}


@router.delete("/sessions/{session_id}/messages")
async def clear_messages(session_id: int, with_members: bool = False):
    """Empty the transcript.

    `with_members=true` also clears the cast, which is what the clear button in
    the composer asks for: the room goes back to an empty table. Memory and
    summaries survive either way.
    """
    _require(session_id)
    removed = db.clear_messages(session_id)
    if with_members:
        for member in db.list_members(session_id):
            db.remove_member(session_id, member["companion_id"])
    return {"ok": True, "removed": removed}


@router.get("/sessions/{session_id}/images")
async def session_images(session_id: int):
    """Images this room's messages reference, newest first."""
    _require(session_id)
    images = db.get_session_image_attachments(session_id)
    return {"images": images, "count": len(images)}


@router.delete("/sessions/{session_id}/images")
async def delete_session_image(session_id: int, url: str = Query(..., min_length=1)):
    """Remove one image from the room: the file, then every reference to it.

    The file goes first. A URL whose file is already gone still gets its
    references cleared, otherwise the album keeps a tile that opens into
    nothing. Clearing references last means a failed write leaves the row
    pointing at a file, which the user can simply delete again.
    """
    _require(session_id)
    file_deleted = delete_image(url)
    cleared = db.remove_session_image(session_id, url)
    if not file_deleted and cleared == 0:
        raise HTTPException(status_code=404, detail="Image not found")
    return {
        "ok": True,
        "url": url,
        "file_deleted": file_deleted,
        "cleared_messages": cleared,
    }


@router.get("/sessions/{session_id}/compacted")
async def compacted_messages(session_id: int):
    """The turns the newest summary covers, so the panel can show the source."""
    _require(session_id)
    return {"messages": db.compacted_messages(session_id)}


class CompactIn(BaseModel):
    llm_config: Optional[Dict[str, Any]] = None


@router.post("/sessions/{session_id}/compact")
async def compact_now(session_id: int, body: CompactIn):
    """Run one compaction on demand.

    Same trigger model as the single thread: nothing compacts in the background,
    the ceiling is the button. Returns the summary that was written, or 400 when
    the room is too short to be worth summarising.
    """
    _require(session_id)
    from .compact import CompactError, compact

    from ..llm_client import load_default_llm_config
    llm_config = body.llm_config or load_default_llm_config() or {}
    try:
        return await compact(session_id, llm_config)
    except CompactError as exc:
        # Three different problems, three different messages. They used to share
        # one "nothing to compact" line, which hid a dead model behind a room
        # that simply had not grown yet.
        detail = {
            "too_short": "Not enough uncovered messages to summarise yet.",
            "empty": "The model returned no text. Check that it is running.",
            "thin": "The summary came back too short to be worth storing.",
        }.get(exc.reason, "Nothing to compact yet.")
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/sessions/{session_id}/stream")
async def stream_turn(session_id: int, body: StreamIn):
    """Append the user's line, pick who answers, stream the reply."""
    session_row = _require(session_id)
    members = db.list_members(session_id)
    if not members:
        raise HTTPException(status_code=400, detail="This room has no members yet")

    from ..llm_client import load_default_llm_config
    llm_config = body.llm_config or {}
    if not llm_config.get("model"):
        llm_config = {**(load_default_llm_config() or {}), **llm_config}
        if not llm_config.get("model"):
            llm_config["model"] = (load_default_llm_config() or {}).get("model", "")
    if not llm_config.get("model"):
        raise HTTPException(status_code=400, detail="No model is configured.")

    if turn.is_room_busy(session_id):
        raise HTTPException(
            status_code=409,
            detail="Someone is already answering in this room. Let them finish first.",
        )

    attachments = [a for a in (body.attachments or []) if isinstance(a, dict)]
    has_text = bool(body.message and body.message.strip())
    is_narration = (body.sender or "") == "narrator"

    pending_message: Optional[Dict[str, Any]] = None
    if has_text or (attachments and not is_narration):
        if is_narration:
            # A stage direction. Stored so the transcript can draw it, but it is
            # never replayed as dialogue, never mined for memory and never folded
            # into a summary — it only steers the reply that follows it.
            pending_message = {
                "content": (body.message or "").strip(),
                "role": "user",
                "speaker_id": "narrator",
                "speaker_name": "Narration",
                "kind": "narration",
                "attachments": None,
            }
        else:
            # Label the user's line with the name the room knows them by, not a
            # generic "You": the transcript should read like a room where
            # everyone has a name. Falls back to "You" when no card declares one.
            speaker_name = prompt.user_display_name(session_row, members)
            if speaker_name == "the user":
                speaker_name = "You"
            pending_message = {
                "content": (body.message or "").strip(),
                "role": "user",
                "speaker_id": "user",
                "speaker_name": speaker_name,
                "kind": body.kind or "say",
                "attachments": attachments or None,
            }

    history = db.list_messages(session_id)

    if body.speaker_id:
        speaker = next(
            (m for m in members if str(m.get("companion_id")) == body.speaker_id), None
        )
        if not speaker:
            raise HTTPException(status_code=400, detail="Speaker is not in this room")
        if pending_message is None:
            # A card click names the next speaker and says nothing else. Store it
            # as a user line so the model sees who was called on, exactly as an
            # "@name" typed into the composer does. Without it the turn arrives
            # with no cue at all and the reply drifts off the scene.
            cue_name = prompt.user_display_name(session_row, members)
            if cue_name == "the user":
                cue_name = "You"
            pending_message = {
                "content": f"@{str(speaker.get('name') or '').strip()}",
                "role": "user",
                "speaker_id": "user",
                "speaker_name": cue_name,
                "kind": "cue",
                "attachments": None,
            }
    else:
        speaker = turn.pick_speaker(
            members,
            body.message or "",
            history,
            is_narration=is_narration,
        )
    if not speaker:
        raise HTTPException(status_code=400, detail="Could not choose a speaker")

    return _stream_response(
        session_id,
        llm_config,
        speaker,
        history,
        members,
        pending_message=pending_message,
    )


def _stream_response(
    session_id: int,
    llm_config: Dict[str, Any],
    speaker: Dict[str, Any],
    history: List[Dict[str, Any]],
    members: List[Dict[str, Any]],
    pending_message: Optional[Dict[str, Any]] = None,
    replace_message_id: Optional[int] = None,
) -> StreamingResponse:
    async def generate():
        try:
            async for event in turn.generate_turn(
                session_id,
                llm_config,
                speaker,
                history,
                members,
                pending_message=pending_message,
                replace_message_id=replace_message_id,
            ):
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - the stream must always close
            logger.exception("group stream failed")
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/reroll/stream")
async def reroll_turn(session_id: int, body: RerollIn):
    """Replace the latest assistant reply, optionally switching to another member."""
    _require(session_id)
    members = db.list_members(session_id)
    if not members:
        raise HTTPException(status_code=400, detail="This room has no members yet")

    from ..llm_client import load_default_llm_config
    llm_config = body.llm_config or {}
    if not llm_config.get("model"):
        llm_config = {**(load_default_llm_config() or {}), **llm_config}
        if not llm_config.get("model"):
            llm_config["model"] = (load_default_llm_config() or {}).get("model", "")
    if not llm_config.get("model"):
        raise HTTPException(status_code=400, detail="No model is configured.")

    if turn.is_room_busy(session_id):
        raise HTTPException(
            status_code=409,
            detail="Someone is already answering in this room. Let them finish first.",
        )

    history = db.list_messages(session_id)
    if not history or int(history[-1]["id"]) != body.message_id:
        raise HTTPException(status_code=400, detail="Only the latest reply can be regenerated")
    target = history[-1]
    if target.get("role") != "assistant":
        raise HTTPException(status_code=400, detail="Only a character reply can be regenerated")

    compaction = db.latest_compaction(session_id)
    boundary = int((compaction or {}).get("messages_compacted") or 0)
    if boundary and int(target["id"]) <= boundary:
        raise HTTPException(status_code=400, detail="Compacted messages cannot be regenerated")

    orig_sid = str(target.get("speaker_id") or "")
    prior = history[:-1]

    if body.speaker_id:
        speaker = next(
            (m for m in members if str(m.get("companion_id")) == body.speaker_id), None
        )
        if not speaker:
            raise HTTPException(status_code=400, detail="Speaker is not in this room")
    elif body.switch_speaker and len(members) > 1:
        others = [m for m in members if str(m.get("companion_id")) != orig_sid]
        speaker = turn.pick_speaker(others, "", prior) if others else None
    else:
        speaker = next(
            (m for m in members if str(m.get("companion_id")) == orig_sid), None
        ) or turn.pick_speaker(members, "", prior)

    if not speaker:
        raise HTTPException(status_code=400, detail="Could not choose a speaker")

    if str(speaker.get("companion_id") or "") != orig_sid:
        # A cue at the tail named whoever was called on before. Re-aim it at the
        # member answering now, or the new speaker reads "the user called on
        # Alice" and then has to reply as itself.
        tail = prior[-1] if prior else None
        name = str(speaker.get("name") or "").strip()
        if tail and name and str(tail.get("kind") or "") == "cue":
            cue = f"@{name}"
            db.update_message_content(session_id, int(tail["id"]), cue)
            # The same dict sits in `history`, so the prompt built from it sees
            # the new text without a second read.
            tail["content"] = cue

    return _stream_response(
        session_id,
        llm_config,
        speaker,
        history,
        members,
        replace_message_id=body.message_id,
    )

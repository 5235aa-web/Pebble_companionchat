"""
script_routes.py - HTTP surface for script mode.

A script is authored once and can be played by any character; a session binds
one script to one companion on its own branch, so the playthrough keeps its
messages, swipes and rewinds inside the existing chat machinery.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import script_db
from .companion_db import create_branch, delete_branch, get_companion
from .picturesys import service as picture_service
from .script_mode import merge_hud

logger = logging.getLogger(__name__)

router = APIRouter(tags=["script"])

# HUD keys that describe what the scene looks like. A change in any of them is
# the cue for a new picture; `affection` alone is not visible, so it is ignored.
_PICTURE_KEYS = ("outfit", "location", "mood")


def _trigger_picture(session: Dict[str, Any], reason: str) -> None:
    """Hand a scene change to Picture Studio.

    Cooldown, daily quota and the on/off flags live in the service, so this
    hook stays a single call. Errors are swallowed with a debug line: the HUD
    update has already succeeded and must not be reported as a failure.
    """
    try:
        picture_service.maybe_auto(
            companion_id=str(session.get("companion_id") or ""),
            branch_id=str(session.get("branch_id") or ""),
            source="script",
            session_id=int(session.get("id") or 0) or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("picture script trigger skipped (%s): %s", reason, exc)


class ScriptCreateRequest(BaseModel):
    title: str = ""
    premise: str = ""


class ScriptUpdateRequest(BaseModel):
    title: Optional[str] = None
    premise: Optional[str] = None


class NodeInput(BaseModel):
    title: str = ""
    instruction: str = ""
    # Planned exchanges for this scene. 0 means the scene has no fixed length.
    turns: int = Field(0, ge=0, le=200)


class NodesRequest(BaseModel):
    nodes: List[NodeInput] = Field(default_factory=list)


class StartRequest(BaseModel):
    companion_id: str
    hud: Optional[Dict[str, Any]] = None
    # The branch name is shown in the UI, so it follows the reader's language.
    language: str = "en"


class HudRequest(BaseModel):
    hud: Dict[str, Any] = Field(default_factory=dict)


class RewindRequest(BaseModel):
    index: int = Field(0, ge=0)
    hud: Optional[Dict[str, Any]] = None
    language: str = "en"


def _session_payload(session_id: int) -> Optional[Dict]:
    session = script_db.get_session(session_id)
    if not session:
        return None
    script = script_db.get_script(session["script_id"])
    nodes = script_db.list_session_nodes(session_id)
    if nodes:
        index = max(0, min(int(session["current_index"]), len(nodes) - 1))
        current = nodes[index]
    else:
        index = 0
        current = None
    return {
        "session": session,
        "script": script,
        "nodes": nodes,
        "current_index": index,
        "current_node": current,
        "node_count": len(nodes),
    }


# ==================== Script library ====================

@router.get("/scripts")
async def list_scripts_endpoint():
    return {"scripts": script_db.list_scripts()}


@router.post("/scripts")
async def create_script_endpoint(request: ScriptCreateRequest):
    script_id = script_db.create_script(request.title, request.premise)
    return {"script_id": script_id}


@router.get("/scripts/{script_id}")
async def get_script_endpoint(script_id: int):
    script = script_db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return {"script": script, "nodes": script_db.list_nodes(script_id)}


@router.put("/scripts/{script_id}")
async def update_script_endpoint(script_id: int, request: ScriptUpdateRequest):
    if not script_db.get_script(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    if not script_db.update_script(script_id, request.title, request.premise):
        raise HTTPException(status_code=400, detail="Nothing to update")
    return {"status": "success"}


@router.delete("/scripts/{script_id}")
async def delete_script_endpoint(script_id: int):
    if not script_db.delete_script(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    return {"status": "success"}


_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _short_title(title: Any, limit: int = 3) -> str:
    """Keep only enough of the title to tell scripts apart in the branch list.

    Chinese reads by character, so three characters; anything else reads by
    word, so three words.
    """
    text = str(title or "").strip()
    if not text:
        return ""
    if _CJK.search(text):
        return text[:limit]
    return " ".join(text.split()[:limit])


def _sig(title: Any, instruction: Any, turns: Any) -> tuple:
    """Identity of a locked scene; whitespace differences must not count."""
    return (
        str(title or "").strip(),
        str(instruction or "").strip(),
        int(turns or 0),
    )


@router.put("/scripts/{script_id}/nodes")
async def replace_nodes_endpoint(script_id: int, request: NodesRequest):
    if not script_db.get_script(script_id):
        raise HTTPException(status_code=404, detail="Script not found")

    # The shared script is a template: a playthrough plays its own copy, so
    # editing this one never reaches a story already in progress.
    nodes = script_db.replace_nodes(
        script_id,
        [{"title": n.title, "instruction": n.instruction, "turns": n.turns} for n in request.nodes],
    )
    return {"nodes": nodes}


@router.get("/script-sessions/{session_id}/nodes")
async def list_session_nodes_endpoint(session_id: int):
    if not script_db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"nodes": script_db.list_session_nodes(session_id)}


@router.put("/script-sessions/{session_id}/nodes")
async def replace_session_nodes_endpoint(session_id: int, request: NodesRequest):
    """Edit this playthrough's own copy. Played scenes stay frozen."""
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("status") != "playing":
        raise HTTPException(status_code=400, detail="This playthrough has ended")

    existing = script_db.list_session_nodes(session_id)
    locked = int(session.get("current_index") or 0) + 1
    if len(request.nodes) < locked:
        raise HTTPException(status_code=400, detail="Played scenes cannot be removed")
    for i in range(min(locked, len(existing))):
        incoming = request.nodes[i]
        if _sig(incoming.title, incoming.instruction, incoming.turns) != _sig(
            existing[i]["title"], existing[i]["instruction"], existing[i]["turns"]
        ):
            raise HTTPException(status_code=400, detail="Played scenes cannot be edited")

    nodes = script_db.replace_session_nodes(
        session_id,
        [{"title": n.title, "instruction": n.instruction, "turns": n.turns} for n in request.nodes],
    )
    return {"nodes": nodes}


# ==================== Playthroughs ====================

@router.post("/scripts/{script_id}/start")
async def start_script_endpoint(script_id: int, request: StartRequest):
    script = script_db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not get_companion(request.companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    nodes = script_db.list_nodes(script_id)
    if not nodes:
        raise HTTPException(status_code=400, detail="剧本还没有剧情节点")

    label = "剧本" if (request.language or "en").lower().startswith("zh") else "Script"
    title = _short_title(script.get("title"))
    branch_name = f"{label} · {title}" if title else label
    branch_id = create_branch(request.companion_id, branch_name)
    if not branch_id:
        raise HTTPException(status_code=500, detail="无法创建剧本分支")

    session_id = script_db.create_session(
        script_id,
        request.companion_id,
        branch_id,
        request.hud,
        0,
    )
    payload = _session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=500, detail="无法创建剧本会话")
    return payload


@router.get("/companions/{companion_id}/script-session")
async def active_session_endpoint(companion_id: str):
    session = script_db.get_active_session(companion_id)
    if not session:
        return {"session": None}
    payload = _session_payload(int(session["id"]))
    return payload or {"session": None}


@router.get("/companions/{companion_id}/script-sessions")
async def list_sessions_endpoint(companion_id: str):
    return {"sessions": script_db.list_sessions_for_companion(companion_id)}


@router.get("/script-sessions/{session_id}")
async def get_session_endpoint(session_id: int):
    payload = _session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    return payload


@router.post("/script-sessions/{session_id}/advance")
async def advance_session_endpoint(session_id: int):
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    nodes = script_db.list_session_nodes(session_id)
    if not nodes:
        raise HTTPException(status_code=400, detail="剧本还没有剧情节点")
    script_db.advance_session(session_id, len(nodes))
    payload = _session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    # A new scene is a new place and usually a new outfit, so it is the second
    # natural moment for a picture besides an explicit HUD change.
    _trigger_picture(session, "scene advance")
    return payload


@router.post("/script-sessions/{session_id}/finish")
async def finish_session_endpoint(session_id: int):
    if not script_db.set_session_status(session_id, "finished"):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "success"}


@router.put("/script-sessions/{session_id}/hud")
async def update_hud_endpoint(session_id: int, request: HudRequest):
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    before = session.get("hud") or {}
    merged = merge_hud(before, request.hud)
    if not script_db.update_session_hud(session_id, merged):
        raise HTTPException(status_code=400, detail="HUD update failed")

    changed = any(
        str(before.get(key) or "") != str(merged.get(key) or "") for key in _PICTURE_KEYS
    )
    if changed:
        _trigger_picture(session, "hud")
    return {"hud": merged}


@router.post("/script-sessions/{session_id}/rewind")
async def rewind_session_endpoint(session_id: int, request: RewindRequest):
    """Restart from an earlier scene on a fresh branch.

    The old session is marked abandoned rather than deleted: its branch keeps
    the messages that were already generated, so nothing the user saw is lost.
    """
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    script = script_db.get_script(session["script_id"])
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    nodes = script_db.list_session_nodes(session_id)
    if not nodes:
        raise HTTPException(status_code=400, detail="剧本还没有剧情节点")

    index = max(0, min(int(request.index), len(nodes) - 1))
    label = "剧本" if (request.language or "en").lower().startswith("zh") else "Script"
    scene_word = "第" if label == "剧本" else "Scene"
    suffix = f"{scene_word} {index + 1} 幕" if label == "剧本" else f"{scene_word} {index + 1}"
    title = _short_title(script["title"])
    prefix = f"{label} · {title}" if title else label
    branch_id = create_branch(
        session["companion_id"],
        f"{prefix} · {suffix}",
    )
    if not branch_id:
        raise HTTPException(status_code=500, detail="无法创建剧本分支")

    hud = request.hud if request.hud is not None else (session.get("hud") or {})
    new_id = script_db.create_session(
        session["script_id"],
        session["companion_id"],
        branch_id,
        hud,
        index,
        copy_from_session=session_id,
    )
    # The replayed run is replaced, not archived. Its branch could only be read
    # as plain chat afterwards, so keeping it spends space on something that can
    # never be resumed.
    script_db.clear_scene_summaries(session["companion_id"], session["branch_id"])
    delete_branch(session["companion_id"], session["branch_id"])
    script_db.delete_session(session_id)
    payload = _session_payload(new_id)
    if not payload:
        raise HTTPException(status_code=500, detail="无法创建剧本会话")
    return payload


# ==================== Scene compaction ====================

class CompactSceneRequest(BaseModel):
    llm_config: Optional[Dict[str, Any]] = None
    # Set only after the user accepted dropping the opening of a long segment.
    allow_trim: bool = False


@router.post("/script-sessions/{session_id}/compact")
async def compact_scene_endpoint(session_id: int, request: CompactSceneRequest):
    """Summarize the played segment. Segments never merge with each other."""
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("status") != "playing":
        raise HTTPException(status_code=400, detail="This playthrough is not running")

    companion = get_companion(session["companion_id"]) or {}
    from .scene_compaction import compact_scene

    return await compact_scene(
        companion_id=session["companion_id"],
        branch_id=session["branch_id"],
        session_id=session_id,
        script_id=int(session["script_id"]),
        current_index=int(session["current_index"]),
        character_name=(companion.get("name") or "").strip(),
        llm_config=request.llm_config,
        allow_trim=request.allow_trim,
    )


@router.get("/script-sessions/{session_id}/summaries")
async def list_scene_summaries_endpoint(session_id: int):
    session = script_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "summaries": script_db.list_scene_summaries(
            session["companion_id"], session["branch_id"]
        )
    }

"""
HTTP surface for Picture Studio.

Mounted under `/pictures`. The routes only translate request shapes: every rule
about providers, quotas and files lives in `service`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import comfyui as picture_comfyui
from . import config as picture_config
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pictures", tags=["picture-studio"])


class GenerateRequest(BaseModel):
    """A manual trigger. `instruction` is optional steering from the user."""

    companionId: str
    branchId: str
    instruction: str = ""
    messageId: Optional[int] = None
    sessionId: Optional[int] = None


def _fail(exc: service.PictureError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/config")
async def get_picture_config():
    return picture_config.public_config(picture_config.load_config())


@router.put("/config")
async def update_picture_config(payload: Dict[str, Any]):
    """Merge-save. Unknown keys are ignored by the loader rather than rejected:
    an older client must not be able to make the panel unsavable."""
    incoming = dict(payload or {})
    incoming.pop("catalogue", None)

    if "dailyLimit" in incoming:
        try:
            incoming["dailyLimit"] = max(0, min(int(incoming["dailyLimit"]), 1000))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="dailyLimit 必须是整数")
    if "cooldownSeconds" in incoming:
        try:
            incoming["cooldownSeconds"] = max(0, min(int(incoming["cooldownSeconds"]), 86400))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cooldownSeconds 必须是整数")
    # `fallbackChain` no longer exists. A panel built before the removal may
    # still send it, so it is dropped rather than rejected.
    incoming.pop("fallbackChain", None)

    saved = picture_config.save_config(incoming)
    return picture_config.public_config(saved)


@router.get("")
async def list_pictures(
    companion_id: str = Query(..., alias="companionId"),
    branch_id: str = Query(..., alias="branchId"),
    limit: int = Query(200, ge=1, le=500),
):
    rows = service.list_for_branch(companion_id, branch_id, limit)
    return {"pictures": rows, "count": len(rows)}


@router.get("/usage")
async def picture_usage(
    companion_id: str = Query("", alias="companionId"),
    branch_id: str = Query("", alias="branchId"),
):
    return service.usage(companion_id, branch_id)


@router.post("/generate")
async def generate_picture(request: GenerateRequest):
    try:
        picture_id = service.start_manual(
            companion_id=request.companionId,
            branch_id=request.branchId,
            instruction=request.instruction,
            message_id=request.messageId,
            session_id=request.sessionId,
        )
    except service.PictureError as exc:
        raise _fail(exc)
    return {"status": "queued", "id": picture_id}


@router.post("/{picture_id}/retry")
async def retry_picture(picture_id: int):
    try:
        new_id = service.retry(picture_id)
    except service.PictureError as exc:
        raise _fail(exc)
    return {"status": "queued", "id": new_id}


@router.post("/{picture_id}/cancel")
async def cancel_picture(picture_id: int):
    """Stop a queued or running job. Idempotent-safe: a finished job refuses."""
    try:
        return await service.cancel(picture_id)
    except service.PictureError as exc:
        raise _fail(exc)


@router.delete("/{picture_id}")
async def delete_picture(picture_id: int):
    try:
        return service.delete(picture_id)
    except service.PictureError as exc:
        raise _fail(exc)


# --- ComfyUI ---------------------------------------------------------------
#
# These live under their own prefix so they can never be mistaken for a picture
# id. Nothing here touches the ComfyUI installation: the only inputs are the
# instance URL and the workflow files stored inside this project's config.


class WorkflowImport(BaseModel):
    name: str
    workflow: Any


@router.get("/comfyui/status")
async def comfyui_status(baseUrl: str = Query("", alias="baseUrl")):
    """Probe the instance and list what it can offer.

    A stopped ComfyUI is a normal state, so this always answers 200 with
    `connected: false`; the panel renders that instead of an error toast.
    """
    cfg = picture_config.load_config()
    stored = (cfg.get("providers") or {}).get("comfyui") or {}
    url = baseUrl or str(stored.get("baseUrl") or "http://127.0.0.1:8188")
    key = str(stored.get("apiKey") or "")
    # trust_env=False: a local ComfyUI must not be routed through the system
    # proxy, which otherwise answers 503 for 127.0.0.1.
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        result = await picture_comfyui.probe(url, key, client)
    result["workflows"] = picture_comfyui.list_workflows()
    return result


@router.get("/comfyui/workflows")
async def comfyui_workflows():
    return {"items": picture_comfyui.list_workflows()}


@router.get("/comfyui/workflows/{name}")
async def comfyui_workflow_detail(name: str):
    """Raw graph, so the panel can show what an imported template contains."""
    try:
        return {"id": name, "workflow": picture_comfyui.load_workflow(name)}
    except picture_comfyui.ComfyUIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/comfyui/workflows")
async def comfyui_workflow_import(payload: WorkflowImport):
    try:
        saved = picture_comfyui.save_workflow(payload.name, payload.workflow)
    except picture_comfyui.ComfyUIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "saved", "item": saved, "items": picture_comfyui.list_workflows()}


@router.delete("/comfyui/workflows/{name}")
async def comfyui_workflow_delete(name: str):
    try:
        picture_comfyui.delete_workflow(name)
    except picture_comfyui.ComfyUIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "deleted", "items": picture_comfyui.list_workflows()}

"""Companion Chat V2 routes, mounted onto `api_routes.router` under `/companions`."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import anyio
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import token_engine
from .card_transfer import (
    EXPORT_CHUNK_BYTES,
    TransferError,
    cleanup_staged_export,
    prepare_charx_export,
    read_export_chunk,
)
from .charx import (
    MAX_CARD_UPLOAD_BYTES,
    MAX_CHARX_SINGLE_ASSET_BYTES,
    classify_media_kind,
    infer_ext,
    infer_mime,
)
from .chat_exporter import export_branch_chat
from .chat_importer import import_jsonl_branch, inspect_jsonl_chat
from .companion import Companion
from .companion_db import (
    add_companion_asset,
    delete_companion_asset,
    get_branch_hud,
    get_branch_variables,
    get_companion,
    get_companion_asset,
    get_companion_assets,
    is_last_active_message,
    reset_branch_variables,
    set_branch_variables,
    update_branch_hud,
    update_companion,
)
from .Inochi2D import service as inochi2d_service
from .llm_client import load_default_llm_config
from .paths import COMPANION_ASSETS_DIR, DATA_ROOT, TMP_UPLOAD_DIR
from .picturesys import service as picture_service
from .world_book_db import (
    add_entry,
    associate_companion_with_world_book,
    bulk_toggle_entries,
    count_character_book_entries,
    create_world_book,
    delete_entry,
    delete_world_book,
    export_world_book_to_json,
    get_all_world_books,
    get_companion_world_books,
    get_entries,
    get_entry,
    get_world_book,
    import_character_book_for_companion,
    import_world_book_from_json,
    normalize_entry_payload,
    remove_companion_world_book_association,
    update_entry,
    update_world_book,
)

router = APIRouter(tags=["multi-companion-v2"])

logger = logging.getLogger(__name__)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def record_usage_chunk(chunk: Any, companion_id: str, branch_id: str) -> None:
    """Persist provider-reported usage so the token panel shows measured values."""
    if isinstance(chunk, dict) and chunk.get("type") == "usage":
        token_engine.record_usage(companion_id, branch_id, chunk)


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


MAX_UPLOAD_BYTES = 50 * 1024 * 1024


async def _read_upload(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an upload in chunks, rejecting anything over the cap."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {max_bytes // (1024 * 1024)} MB 上限",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _stream_upload_to_tempfile(
    file: UploadFile,
    max_bytes: int = MAX_CARD_UPLOAD_BYTES,
    suffix: str = ".tmp",
) -> Path:
    """Stream an UploadFile directly to disk in 1 MB chunks without buffering in RAM."""
    in_dir = TMP_UPLOAD_DIR / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="upload_", suffix=suffix, dir=str(in_dir))
    os.close(fd)
    tmp_path = Path(raw_path)
    total = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {max_bytes // (1024 * 1024)} MB 上限",
                    )
                out.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ChatStreamRequest(BaseModel):
    message: str
    companion_id: str
    branch_id: str = "main"
    enable_thinking: bool = False
    llm_config: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    # Attachments travel with the message: images become multimodal parts,
    # documents are already extracted to text by /attachments/parse.
    attachments: Optional[List[Dict[str, Any]]] = None
    # "user" for a line the user's character says, "narrator" for authored
    # direction (scene, other people, time passing). Narration is stored with
    # its own message_type so the model can tell it apart from dialogue.
    sender: Optional[str] = "user"
    # When set, the turn runs inside a scripted playthrough: the script prompt
    # replaces the card prompt, and memory is neither injected nor extracted.
    script_session_id: Optional[int] = None


class ContinueStreamRequest(BaseModel):
    companion_id: str
    branch_id: str = "main"
    enable_thinking: bool = False
    llm_config: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9


class RegenerateStreamRequest(BaseModel):
    companion_id: str
    message_id: int
    branch_id: str = "main"
    enable_thinking: bool = False
    llm_config: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9


class ReeditStreamRequest(BaseModel):
    """改写一条用户消息，并让模型基于新问题重新回答。"""

    companion_id: str
    message_id: int
    content: str
    branch_id: str = "main"
    enable_thinking: bool = False
    llm_config: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9


@router.post("/chat/stream")
async def companion_chat_stream(request: ChatStreamRequest):
    """流式聊天端点。"""
    chat_logger = logging.getLogger("companion_chat")
    chat_logger.info(
        f"Received companion chat request: companion_id={request.companion_id}, message={request.message[:50]}"
    )

    companion_data = get_companion(request.companion_id)
    if not companion_data:
        chat_logger.error(f"Companion not found: {request.companion_id}")
        raise HTTPException(status_code=404, detail="Companion not found")
    chat_logger.info(f"Companion found: {companion_data['name']}")

    async def event_generator():
        try:
            companion = Companion(request.companion_id)
            chunk_count = 0
            async for chunk in companion.generate_response_stream(
                user_message=request.message,
                branch_id=request.branch_id,
                llm_config=request.llm_config,
                enable_thinking=request.enable_thinking,
                temperature=request.temperature,
                top_p=request.top_p,
                attachments=request.attachments,
                sender=request.sender or "user",
                script_session_id=request.script_session_id,
            ):
                chunk_count += 1
                chat_logger.debug(f"Yielding chunk #{chunk_count}: type={chunk.get('type')}")
                record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            chat_logger.info(f"Stream complete, yielded {chunk_count} chunks")

            # Picture Studio hook. Both chat and story mode run through maybe_auto,
            # letting cooldownSeconds and the director LLM decide whether to draw.
            # Swallows errors so a broken image provider never breaks a chat turn.
            source = "script" if request.script_session_id else "chat"
            try:
                picture_service.maybe_auto(
                    companion_id=request.companion_id,
                    branch_id=request.branch_id,
                    source=source,
                    session_id=request.script_session_id or None,
                )
            except Exception as exc:  # noqa: BLE001
                chat_logger.debug("picture auto trigger skipped: %s", exc)
            yield "data: [DONE]\n\n"
        except Exception as e:
            chat_logger.error(f"Stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return _sse_response(event_generator)


@router.post("/chat/continue/stream")
async def companion_chat_continue_stream(request: ContinueStreamRequest):
    """续写最后一条AI回复。流式SSE，与/chat/stream格式完全一致。"""
    cont_logger = logging.getLogger("companion_chat_continue")
    cont_logger.info(f"Received continue request: companion_id={request.companion_id}")

    companion_data = get_companion(request.companion_id)
    if not companion_data:
        raise HTTPException(status_code=404, detail="Companion not found")

    async def event_generator():
        try:
            companion = Companion(request.companion_id)
            async for chunk in companion.generate_continue_stream(
                branch_id=request.branch_id,
                llm_config=request.llm_config,
                enable_thinking=request.enable_thinking,
                temperature=request.temperature,
                top_p=request.top_p,
            ):
                record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            cont_logger.error(f"Continue stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return _sse_response(event_generator)


@router.post("/chat/regenerate/stream")
async def companion_chat_regenerate_stream(request: RegenerateStreamRequest):
    """重新生成指定AI消息。流式SSE，与/chat/stream格式完全一致。"""
    regen_logger = logging.getLogger("companion_chat_regenerate")
    regen_logger.info(
        f"Received regenerate request: companion_id={request.companion_id}, msg_id={request.message_id}"
    )

    companion_data = get_companion(request.companion_id)
    if not companion_data:
        raise HTTPException(status_code=404, detail="Companion not found")

    record_branch_usage = is_last_active_message(
        request.companion_id, request.branch_id, request.message_id
    )

    async def event_generator():
        try:
            companion = Companion(request.companion_id)
            async for chunk in companion.generate_regenerate_stream(
                message_id=request.message_id,
                branch_id=request.branch_id,
                llm_config=request.llm_config,
                enable_thinking=request.enable_thinking,
                temperature=request.temperature,
                top_p=request.top_p,
            ):
                if record_branch_usage:
                    record_usage_chunk(chunk, request.companion_id, request.branch_id)
                elif isinstance(chunk, dict) and chunk.get("type") == "usage":
                    token_engine.clear_recorded_usage(request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            regen_logger.error(f"Regenerate stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return _sse_response(event_generator)


@router.post("/chat/reedit/stream")
async def companion_chat_reedit_stream(request: ReeditStreamRequest):
    """改写一条用户消息并重新生成回复。"""
    companion_data = get_companion(request.companion_id)
    if not companion_data:
        raise HTTPException(status_code=404, detail="Companion not found")

    async def event_generator():
        try:
            companion = Companion(request.companion_id)
            async for chunk in companion.generate_reedit_stream(
                message_id=request.message_id,
                new_content=request.content,
                branch_id=request.branch_id,
                llm_config=request.llm_config,
                enable_thinking=request.enable_thinking,
                temperature=request.temperature,
                top_p=request.top_p,
            ):
                record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Reedit stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return _sse_response(event_generator)


class BranchExportRequest(BaseModel):
    """Export one branch conversation to DATA_ROOT/outputchat."""

    format: Literal["md", "json", "html", "jsonl"] = "md"
    include_compacted: bool = True


@router.post("/{companion_id}/branches/{branch_id}/export")
async def export_companion_branch(
    companion_id: str,
    branch_id: str,
    request: BranchExportRequest,
):
    """Export a single-companion branch to DATA_ROOT/outputchat."""
    try:
        return export_branch_chat(
            companion_id=companion_id,
            branch_id=branch_id,
            fmt=request.format,
            include_compacted=request.include_compacted,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class BranchImportJsonlRequest(BaseModel):
    """Import a SillyTavern / Pebble JSONL transcript into a new branch."""

    jsonl_text: str
    branch_name: Optional[str] = None
    filename: Optional[str] = None
    skip_system: bool = True


@router.post("/{companion_id}/branches/import-jsonl/preview")
async def preview_companion_jsonl_import(
    companion_id: str,
    request: BranchImportJsonlRequest,
):
    """Validate JSONL content and check character mismatch before creating a branch."""
    try:
        return inspect_jsonl_chat(
            companion_id=companion_id,
            jsonl_text=request.jsonl_text,
            skip_system=request.skip_system,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{companion_id}/branches/import-jsonl")
async def import_companion_jsonl_branch(
    companion_id: str,
    request: BranchImportJsonlRequest,
):
    """Create a new conversation branch from a SillyTavern / Pebble JSONL transcript."""
    try:
        return import_jsonl_branch(
            companion_id=companion_id,
            jsonl_text=request.jsonl_text,
            branch_name=request.branch_name,
            filename=request.filename,
            skip_system=request.skip_system,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class Live2DCueRequest(BaseModel):
    text: str = ""
    mood: str = ""
    expression: str = ""


@router.get("/{companion_id}/live2d/poses")
async def get_companion_live2d_poses(companion_id: str):
    """List multi-pose Live2D slots configured for a companion."""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return inochi2d_service.list_poses(companion_id)


@router.post("/{companion_id}/live2d/cue")
async def match_companion_live2d_cue(companion_id: str, request: Live2DCueRequest):
    """Match a chat/HUD cue against configured multi-pose Live2D slots."""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return inochi2d_service.match_pose_for_cue(
        companion_id=companion_id,
        text=request.text,
        mood=request.mood,
        expr_hint=request.expression,
    )


# ==================== World Book & Companion Association ====================

class WorldBookCreateRequest(BaseModel):
    name: str
    description: str = ""


class WorldBookUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class WorldBookEntryRequest(BaseModel):
    uid: int = 0
    comment: str = ""
    name: str = ""
    keys: List[str] = []
    secondary_keys: List[str] = []
    content: str = ""
    constant: bool = False
    selective: bool = True
    selective_logic: Optional[int] = None
    logic: Optional[str] = None
    order: int = 100
    position: Union[int, str] = 0
    depth: int = 4
    disabled: Optional[bool] = None
    enabled: Optional[bool] = None
    probability: Union[int, float] = 100
    use_probability: bool = True
    sticky: Optional[int] = None
    cooldown: Optional[int] = None
    delay: Optional[int] = None
    group: str = ""
    group_override: bool = False
    group_weight: int = 100
    category: str = "lore"


class WorldBookBulkToggleRequest(BaseModel):
    mode: Optional[str] = None
    entry_ids: Optional[List[int]] = None
    enabled: Optional[bool] = None


class WorldBookTranslateRequest(BaseModel):
    target_locale: str
    entry_ids: Optional[List[int]] = None
    force: bool = False
    companion_id: Optional[str] = None
    llm_config: Optional[Dict[str, Any]] = None


class CompanionWorldBookSyncRequest(BaseModel):
    force: bool = False


class CompanionWorldBookAssociationRequest(BaseModel):
    world_book_id: int
    is_primary: bool = False


@router.get("/world-books")
async def list_world_books():
    """获取所有世界书列表。"""
    books = get_all_world_books()
    return {"world_books": books}


@router.post("/world-books")
async def create_new_world_book(request: WorldBookCreateRequest):
    """创建新世界书。"""
    world_book_id = create_world_book(request.name, request.description)
    return {"status": "success", "world_book_id": world_book_id}


@router.get("/world-books/{world_book_id}")
async def get_world_book_detail(world_book_id: int):
    """获取世界书详情及其所有条目。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")

    entries = get_entries(world_book_id)
    return {
        "world_book": world_book,
        "entries": entries,
    }


@router.put("/world-books/{world_book_id}")
async def update_world_book_detail(world_book_id: int, request: WorldBookUpdateRequest):
    """更新世界书元数据。"""
    success = update_world_book(world_book_id, request.name, request.description)
    if not success:
        raise HTTPException(status_code=404, detail="World book not found")
    return {"status": "success"}


@router.delete("/world-books/{world_book_id}")
async def delete_world_book_endpoint(world_book_id: int):
    """删除世界书。"""
    success = delete_world_book(world_book_id)
    if not success:
        raise HTTPException(status_code=404, detail="World book not found")
    return {"status": "success"}


@router.post("/world-books/{world_book_id}/entries")
async def add_world_book_entry(world_book_id: int, request: WorldBookEntryRequest):
    """添加条目到世界书。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")

    entry_id = add_entry(world_book_id, normalize_entry_payload(request.model_dump()))
    return {"status": "success", "entry_id": entry_id}


@router.post("/world-books/{world_book_id}/entries/bulk-toggle")
async def bulk_toggle_world_book_entries(world_book_id: int, request: WorldBookBulkToggleRequest):
    """批量切换世界书条目的启用状态（支持仅启用媒体 / 全开 / 全关）。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")
    if request.mode not in (None, "media_only", "all_on", "all_off"):
        raise HTTPException(status_code=400, detail="Invalid bulk toggle mode")
    updated = bulk_toggle_entries(
        world_book_id,
        mode=request.mode,
        entry_ids=request.entry_ids,
        enabled=request.enabled,
    )
    return {"status": "success", "updated": updated, "entries": get_entries(world_book_id)}


@router.post("/world-books/{world_book_id}/translate")
async def translate_world_book_entries(world_book_id: int, request: WorldBookTranslateRequest):
    """Stream display-only translations for world book entries without altering originals."""
    from .world_book_translation import iter_translate_world_book

    if not get_world_book(world_book_id):
        raise HTTPException(status_code=404, detail="World book not found")

    llm_cfg: Dict[str, Any] = {}
    if request.companion_id:
        try:
            comp = Companion(request.companion_id)
            llm_cfg = comp._resolve_llm_config_full(request.llm_config) or {}
        except ValueError:
            llm_cfg = {}
    if not llm_cfg:
        base_cfg = load_default_llm_config() or {}
        if isinstance(request.llm_config, dict):
            llm_cfg = {**base_cfg, **{k: v for k, v in request.llm_config.items() if v}}
        else:
            llm_cfg = base_cfg

    async def event_stream():
        try:
            async for ev in iter_translate_world_book(
                world_book_id,
                target_locale=request.target_locale,
                entry_ids=request.entry_ids,
                force=request.force,
                llm_config=llm_cfg,
            ):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.error("World book translate failed for %s: %s", world_book_id, exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return _sse_response(event_stream)


@router.put("/world-books/{world_book_id}/entries/{entry_id}")
async def update_world_book_entry(world_book_id: int, entry_id: int, request: WorldBookEntryRequest):
    """更新条目。"""
    entry = get_entry(entry_id)
    if not entry or entry.get("id") != entry_id:
        raise HTTPException(status_code=404, detail="Entry not found")

    success = update_entry(entry_id, normalize_entry_payload(request.model_dump()))
    if not success:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"status": "success"}


@router.delete("/world-books/{world_book_id}/entries/{entry_id}")
async def delete_world_book_entry(world_book_id: int, entry_id: int):
    """删除条目。"""
    success = delete_entry(entry_id)
    if not success:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"status": "success"}


@router.post("/world-books/import")
async def import_world_book(file: UploadFile = File(...), name: Optional[str] = Form(None)):
    """导入世界书（支持JSON格式）。"""
    try:
        content = await _read_upload(file)
        data = json.loads(content.decode("utf-8"))

        world_book_id = import_world_book_from_json(data, name)
        return {"status": "success", "world_book_id": world_book_id}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.get("/world-books/{world_book_id}/export")
async def export_world_book(world_book_id: int):
    """导出世界书为JSON。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")

    data = export_world_book_to_json(world_book_id)
    return data


@router.get("/{companion_id}/world-books")
async def get_companion_world_books_list(companion_id: str):
    """获取AI伴侣关联的所有世界书及角色卡内置设定条目数。"""
    comp = get_companion(companion_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Companion not found")

    world_books = get_companion_world_books(companion_id)
    card_lore_count = count_character_book_entries(comp.get("raw_card_data"))
    return {"world_books": world_books, "card_lore_count": card_lore_count}


@router.post("/{companion_id}/world-books/sync-card")
async def sync_companion_card_world_book(
    companion_id: str,
    request: CompanionWorldBookSyncRequest = CompanionWorldBookSyncRequest(),
):
    """从角色卡内嵌 character_book 全量导入世界书（默认仅激活媒体资产指令条目）。"""
    comp = get_companion(companion_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Companion not found")

    assets = get_companion_assets(companion_id)
    asset_names = [str(a.get("name") or "").strip() for a in assets if str(a.get("name") or "").strip()]
    res = import_character_book_for_companion(
        companion_id,
        comp.get("raw_card_data"),
        char_name=comp.get("name") or "",
        asset_names=asset_names,
        skip_if_linked=not request.force,
    )
    book_id = res.get("world_book_id")
    world_book = get_world_book(book_id) if book_id else None
    entries = get_entries(book_id) if book_id else []
    return {
        "status": "success",
        "world_book": world_book,
        "entries": entries,
        **res,
    }


@router.post("/{companion_id}/world-books")
async def associate_companion_world_book(companion_id: str, request: CompanionWorldBookAssociationRequest):
    """关联AI伴侣与世界书。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")

    world_book = get_world_book(request.world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")

    success = associate_companion_with_world_book(companion_id, request.world_book_id, request.is_primary)
    return {"status": "success" if success else "failed"}


@router.delete("/{companion_id}/world-books/{world_book_id}")
async def remove_companion_world_book(companion_id: str, world_book_id: int):
    """移除AI伴侣与世界书的关联。"""
    success = remove_companion_world_book_association(companion_id, world_book_id)
    if not success:
        raise HTTPException(status_code=404, detail="Association not found")
    return {"status": "success"}


# ==================== Scene Rules Studio & Card Translation ====================

class SceneRulesUpdateRequest(BaseModel):
    display_regex: Optional[List[Dict[str, Any]]] = None
    output_regex: Optional[List[Dict[str, Any]]] = None
    input_regex: Optional[List[Dict[str, Any]]] = None
    prompt_regex: Optional[List[Dict[str, Any]]] = None
    scene_rules: Optional[List[Dict[str, Any]]] = None
    hud_pinned_vars: Optional[List[str]] = None
    hud_thought_enabled: Optional[bool] = None
    card_css: Optional[str] = None


class SceneRuleCompileRequest(BaseModel):
    instruction: str
    sample_text: str = ""
    preferred_mode: str = "auto"
    branch_id: str = "main"
    llm_config: Optional[Dict[str, Any]] = None


class SceneRuleExplainRequest(BaseModel):
    rules: List[Dict[str, Any]]
    language: str = "zh"
    llm_config: Optional[Dict[str, Any]] = None


class CompanionTranslateRequest(BaseModel):
    target_locale: str = "zh"
    fields: Optional[List[str]] = None
    activate: bool = True
    llm_config: Optional[Dict[str, Any]] = None


@router.put("/{companion_id}/scene-rules")
async def update_scene_rules(companion_id: str, request: SceneRulesUpdateRequest):
    """Merge Scene Rules & presentation overrides into ``card_ui`` without clobbering siblings."""
    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")

    next_ui = dict(companion.get("card_ui") or {})
    if request.display_regex is not None:
        next_ui["display_regex"] = request.display_regex
    if request.output_regex is not None:
        next_ui["output_regex"] = request.output_regex
    if request.input_regex is not None:
        next_ui["input_regex"] = request.input_regex
    if request.prompt_regex is not None:
        next_ui["prompt_regex"] = request.prompt_regex
    if request.scene_rules is not None:
        next_ui["scene_rules"] = request.scene_rules
    if request.hud_pinned_vars is not None:
        from .card_presentation import normalize_hud_pinned_vars

        next_ui["hud_pinned_vars"] = normalize_hud_pinned_vars(request.hud_pinned_vars)
    if request.hud_thought_enabled is not None:
        next_ui["hud_thought_enabled"] = bool(request.hud_thought_enabled)
    if request.card_css is not None:
        next_ui["card_css"] = request.card_css

    update_companion(companion_id, {"card_ui": next_ui})
    updated = get_companion(companion_id)
    return {
        "status": "success",
        "card_ui": (updated or {}).get("card_ui", next_ui),
        "card_presentation": (updated or {}).get("card_presentation"),
    }


@router.post("/{companion_id}/scene-rules/compile")
async def compile_companion_scene_rule(companion_id: str, request: SceneRuleCompileRequest):
    """Compile plain-language rule description into a validated Pebble rule."""
    from .memory_engine import resolve_user_name
    from .scene_rules_ai import compile_scene_rule

    try:
        comp = Companion(companion_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Companion not found")

    branch_key = request.branch_id or "main"
    vars_map = get_branch_variables(companion_id, branch_key).get("variables", {})
    hud_map = get_branch_hud(companion_id, branch_key).get("hud") or {}
    asset_names = [
        str(a.get("name") or "").strip()
        for a in get_companion_assets(companion_id)
        if str(a.get("name") or "").strip()
    ]
    llm_cfg = comp._resolve_llm_config_full(request.llm_config) or {}

    try:
        result = await compile_scene_rule(
            instruction=request.instruction,
            sample_text=request.sample_text,
            preferred_mode=request.preferred_mode,
            char_name=comp.name or companion_id,
            user_name=resolve_user_name(comp.user_info),
            variables=vars_map,
            hud=hud_map,
            asset_names=asset_names,
            llm_config=llm_cfg,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Scene rule compile failed for %s: %s", companion_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Compile failed: {str(exc)}")


@router.post("/{companion_id}/scene-rules/explain")
async def explain_companion_scene_rules(companion_id: str, request: SceneRuleExplainRequest):
    """Translate technical card rules into plain-language summaries."""
    from .scene_rules_ai import explain_scene_rules

    try:
        comp = Companion(companion_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Companion not found")

    llm_cfg = comp._resolve_llm_config_full(request.llm_config) or {}
    try:
        explanations = await explain_scene_rules(
            rules=request.rules,
            language=request.language,
            llm_config=llm_cfg,
        )
        return {"explanations": explanations}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Scene rule explain failed for %s: %s", companion_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Explain failed: {str(exc)}")


@router.post("/{companion_id}/translate")
async def translate_companion_card(companion_id: str, request: CompanionTranslateRequest):
    """Stream per-field LLM translations for display fields without mutating original card columns."""
    from .card_translation import iter_translate_companion_card

    try:
        comp = Companion(companion_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Companion not found")

    llm_cfg = comp._resolve_llm_config_full(request.llm_config) or {}

    async def event_stream():
        try:
            async for ev in iter_translate_companion_card(
                companion_id,
                target_locale=request.target_locale,
                fields=request.fields,
                activate=request.activate,
                llm_config=llm_cfg,
            ):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.error("Card translate failed for %s: %s", companion_id, exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return _sse_response(event_stream)


# ==================== Card Export & Multimedia Assets ====================

@router.get("/{companion_id}/export/json")
async def export_tavern_json(companion_id: str):
    """导出角色卡为JSON格式。"""
    from urllib.parse import quote
    from .tavern_exporter import export_to_json, get_export_filename

    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")

    try:
        json_content = export_to_json(companion)
        filename = get_export_filename(companion, "json")
        encoded_filename = quote(filename)
        return Response(
            content=json_content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
        )
    except Exception as e:
        import traceback

        print(f"[Export JSON] Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/{companion_id}/export/png")
async def export_tavern_png(companion_id: str):
    """导出角色卡为PNG格式（嵌入元数据）。"""
    from urllib.parse import quote
    from .tavern_exporter import export_to_png, get_export_filename

    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")

    try:
        png_bytes = export_to_png(companion)
        filename = get_export_filename(companion, "png")
        encoded_filename = quote(filename)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
        )
    except Exception as e:
        import traceback

        print(f"[Export PNG] Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/{companion_id}/export/charx")
async def export_tavern_charx(companion_id: str):
    """流式导出角色卡为 CHARX（.charx）容器包。"""
    from urllib.parse import quote
    from .tavern_exporter import export_to_charx_file, get_export_filename

    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")

    assets = get_companion_assets(companion_id)
    out_dir = TMP_UPLOAD_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="stream_", suffix=".charx", dir=str(out_dir))
    os.close(fd)
    dest_path = Path(raw_path)

    try:
        await anyio.to_thread.run_sync(export_to_charx_file, companion, assets, dest_path)
        file_size = dest_path.stat().st_size
        filename = get_export_filename(companion, "charx")
        encoded_filename = quote(filename)

        def _iter_file():
            with open(dest_path, "rb") as f:
                while True:
                    chunk = f.read(EXPORT_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            _iter_file(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                "Content-Length": str(file_size),
            },
            background=BackgroundTask(dest_path.unlink, missing_ok=True),
        )
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        logger.error("Export CHARX failed for %s: %s", companion_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.post("/{companion_id}/export/charx/prepare")
async def prepare_tavern_charx_export(companion_id: str):
    """在磁盘预生成 .charx 导出包，供分片下载（适配 Tauri / 进度条）。"""
    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")

    assets = get_companion_assets(companion_id)
    try:
        return await anyio.to_thread.run_sync(prepare_charx_export, companion, assets)
    except Exception as e:
        logger.error("Prepare CHARX export failed for %s: %s", companion_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/export/download/{export_id}/chunk")
async def download_staged_export_chunk(
    export_id: str,
    offset: int = Query(0, ge=0),
    length: int = Query(EXPORT_CHUNK_BYTES, ge=1),
):
    """读取预生成导出包的指定字节分片。"""
    try:
        data = await anyio.to_thread.run_sync(read_export_chunk, export_id, offset, length)
        return Response(content=data, media_type="application/octet-stream")
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.delete("/export/download/{export_id}")
async def delete_staged_export(export_id: str):
    """清理已完成或已取消的暂存导出包。"""
    try:
        removed = cleanup_staged_export(export_id)
        return {"status": "success", "removed": removed}
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/{companion_id}/assets")
async def list_companion_assets(companion_id: str, asset_type: Optional[str] = Query(None)):
    """列出角色关联的所有多媒体资产（图片、表情、背景、音乐、音效等）。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    assets = get_companion_assets(companion_id, asset_type=asset_type)
    return {"assets": assets, "count": len(assets)}


@router.post("/{companion_id}/assets")
async def upload_companion_asset(
    companion_id: str,
    file: UploadFile = File(...),
    asset_type: str = Form("other"),
    name: str = Form(""),
):
    """流式上传单个图片或音视频资产到角色资产库。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")

    orig_name = file.filename or "asset.bin"
    ext = infer_ext(orig_name, "bin")
    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _stream_upload_to_tempfile(
            file, max_bytes=MAX_CHARX_SINGLE_ASSET_BYTES, suffix=f".{ext}"
        )
        size_bytes = tmp_path.stat().st_size
        semantic_type = classify_media_kind(ext, asset_type)
        asset_name = (name or Path(orig_name).stem or "asset").strip()[:80]

        import uuid

        target_dir = COMPANION_ASSETS_DIR / companion_id / semantic_type
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = target_dir / f"{uuid.uuid4().hex[:10]}.{ext}"
        await anyio.to_thread.run_sync(shutil.move, str(tmp_path), str(dest_path))
        tmp_path = None

        rel_path = dest_path.resolve().relative_to(DATA_ROOT.resolve()).as_posix()
        asset_id = add_companion_asset(
            companion_id,
            {
                "asset_type": semantic_type,
                "name": asset_name,
                "ext": ext,
                "mime_type": infer_mime(ext),
                "rel_path": rel_path,
                "source_uri": f"embeded://assets/{semantic_type}/{dest_path.name}",
                "size_bytes": size_bytes,
                "sort_order": len(get_companion_assets(companion_id)),
            },
        )
        return {"status": "success", "asset": get_companion_asset(companion_id, asset_id)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Asset upload failed: {str(e)}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@router.delete("/{companion_id}/assets/{asset_id}")
async def remove_companion_asset(companion_id: str, asset_id: int):
    """删除角色的单个多媒体资产及其磁盘文件。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    deleted = delete_companion_asset(companion_id, asset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Asset not found")
    rel_path = str(deleted.get("rel_path") or "")
    if rel_path:
        try:
            assets_root = COMPANION_ASSETS_DIR.resolve()
            target = (DATA_ROOT.resolve() / rel_path).resolve()
            if target.is_relative_to(assets_root) and target.is_file():
                target.unlink()
        except Exception as exc:
            logger.warning("Failed to remove asset file %s: %s", rel_path, exc)
    return {"status": "success", "id": asset_id}


@router.post("/{companion_id}/avatar/from-asset/{asset_id}")
async def set_avatar_from_asset(companion_id: str, asset_id: int):
    """将角色资产库中的某张图片设为主头像。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    asset = get_companion_asset(companion_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not str(asset.get("mime_type") or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Selected asset is not an image")
    rel_path = str(asset.get("rel_path") or "")
    if not rel_path:
        raise HTTPException(status_code=400, detail="Asset has no stored file")
    update_companion(companion_id, {"avatar_path": rel_path})
    return {"status": "success", "avatar_path": rel_path}


# ==================== Card HUD & Branch Variables ====================

class CardHudUpdateRequest(BaseModel):
    hud: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    clear_enabled: bool = False


@router.get("/{companion_id}/branches/{branch_id}/hud")
async def get_companion_branch_hud(companion_id: str, branch_id: str):
    """获取单人对话分支的 HUD 状态与启用配置。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return get_branch_hud(companion_id, branch_id)


@router.put("/{companion_id}/branches/{branch_id}/hud")
async def put_companion_branch_hud(
    companion_id: str,
    branch_id: str,
    request: CardHudUpdateRequest,
):
    """更新单人对话分支的 HUD 状态或开关。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return update_branch_hud(
        companion_id,
        branch_id,
        hud=request.hud,
        enabled=request.enabled,
        clear_enabled=request.clear_enabled,
    )


@router.post("/{companion_id}/branches/{branch_id}/hud/reset")
async def reset_companion_branch_hud(companion_id: str, branch_id: str):
    """重置单人对话分支的 HUD 状态字段（保留开关设置）。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return update_branch_hud(companion_id, branch_id, hud={})


class BranchVariablesUpdateRequest(BaseModel):
    variables: Dict[str, Any] = Field(default_factory=dict)


@router.get("/{companion_id}/branches/{branch_id}/variables")
async def get_companion_branch_variables(companion_id: str, branch_id: str):
    """获取单人对话分支的 CBS 变量（含卡面默认值合并）。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return get_branch_variables(companion_id, branch_id)


@router.put("/{companion_id}/branches/{branch_id}/variables")
async def put_companion_branch_variables(
    companion_id: str,
    branch_id: str,
    request: BranchVariablesUpdateRequest,
):
    """更新单人对话分支的 CBS 变量。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    res = set_branch_variables(companion_id, branch_id, request.variables)
    token_engine.clear_recorded_usage(companion_id, branch_id)
    return res


@router.post("/{companion_id}/branches/{branch_id}/variables/reset")
async def reset_companion_branch_variables(companion_id: str, branch_id: str):
    """将分支 CBS 变量重置为角色卡默认值。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    res = reset_branch_variables(companion_id, branch_id)
    token_engine.clear_recorded_usage(companion_id, branch_id)
    return res

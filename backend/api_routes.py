"""
api_routes.py - 多AI伴侣独立API路由。
所有端点完全独立，不依赖现有系统的任何路由或逻辑。
通过 include_router 挂载到 api_server.py。
"""
import json
import os
import functools
import logging
import shutil
import tempfile
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

import anyio
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .companion_db import (
    get_companion, get_all_companions, delete_companion, update_companion,
    add_companion_asset, get_companion_assets, get_companion_asset, delete_companion_asset,
    get_branch_hud, update_branch_hud,
    get_branch_variables, set_branch_variables, reset_branch_variables,
    get_branches, create_branch, delete_branch, update_branch_name,
    get_messages, get_messages_with_swipes, delete_message, get_message_count, clear_branch_messages,
    save_message, mark_messages_compacted, get_compacted_messages,
    update_message_content,
    get_user_settings, update_user_settings, replace_branch_messages, get_branch_stats,
    set_message_swipe_id, get_last_assistant_message, search_messages,
    is_last_active_message,
    # 新增：压缩历史和消息重要性
    save_compaction_history, get_compaction_history, get_compaction_stats,
    set_message_importance, get_important_messages,
    get_summary_messages, get_latest_summary, get_compaction_version,
    # 图片记录面板：分支图片聚合与引用清除
    get_branch_image_attachments, remove_attachment_reference,
)
from .companion import Companion
from .script_db import get_session_by_branch
from .picturesys import service as picture_service
from .paths import AVATAR_DIR, COMPANION_ASSETS_DIR, DATA_ROOT, LLM_CONFIG_PATH, TMP_UPLOAD_DIR
from .llm_client import load_default_llm_config
from .tavern_importer import import_card_file, import_from_bytes
from .risum import RisumError, merge_risum_into_companion
from .charx import (
    CharxError,
    MAX_CARD_UPLOAD_BYTES,
    MAX_CHARX_SINGLE_ASSET_BYTES,
    classify_media_kind,
    infer_ext,
    infer_mime,
)
from .card_transfer import (
    EXPORT_CHUNK_BYTES,
    TransferError,
    abort_staged_upload,
    append_staged_chunk,
    cleanup_staged_export,
    commit_staged_upload,
    init_staged_upload,
    prepare_charx_export,
    read_export_chunk,
)
from .token_counter import get_token_status, acount_full_context, estimate_tokens_messages, get_context_window
from . import token_engine
from .compaction import compact_conversation, CompactionResult, CompactionConfig, get_summary_preview, is_summary_message
from .memory_db import (
    MAX_CHARACTER_MEMORIES,
    MAX_MEMORIES,
    clear_memories,
    delete_memory,
    list_memories,
    update_memory,
)
from .attachment_parser import (
    build_attachment,
    AttachmentError,
    delete_image,
    format_documents_for_prompt,
    resolve_image_path,
    supported_extensions,
)
from .world_book_db import (    create_world_book, get_world_book, get_all_world_books, update_world_book, delete_world_book,
    add_entry, update_entry, delete_entry, get_entries, get_entry,
    associate_companion_with_world_book, remove_companion_world_book_association,
    get_companion_world_books, get_companion_primary_world_book,
    export_world_book_to_json, import_world_book_from_json
)

router = APIRouter(prefix="/companions", tags=["multi-companion"])

logger = logging.getLogger(__name__)

# Upload limits. The cap is enforced while reading rather than after: a declared
# Content-Length can be missing or wrong, and reading first would put the whole
# file in memory before its size is known.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENT_FILES = 10

_attachment_slots: Optional[asyncio.Semaphore] = None


def _attachment_semaphore() -> asyncio.Semaphore:
    """Created on first use so it binds to the running loop, not import time."""
    global _attachment_slots
    if _attachment_slots is None:
        _attachment_slots = asyncio.Semaphore(MAX_ATTACHMENT_FILES)
    return _attachment_slots


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


def _record_usage_chunk(chunk: Any, companion_id: str, branch_id: str) -> None:
    """Persist provider-reported usage so the token panel shows measured values."""
    if isinstance(chunk, dict) and chunk.get("type") == "usage":
        token_engine.record_usage(companion_id, branch_id, chunk)


@router.post("/attachments/parse")
async def parse_attachment_endpoint(file: UploadFile = File(...)):
    """
    解析上传的附件。

    图片保存到 uploads/attachments 并返回静态 URL 与像素尺寸；
    Office / PDF / 纯文本在本地解析成正文返回，由聊天请求内联进用户消息。
    """
    filename = file.filename or "attachment"
    # One user can drop a batch of files; the semaphore keeps a large selection
    # from holding more than MAX_ATTACHMENT_FILES bodies in memory at once.
    async with _attachment_semaphore():
        data = await _read_upload(file)

        try:
            return build_attachment(filename, data)
        except AttachmentError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Attachment parse failed for {filename}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"附件解析失败: {e}")


@router.get("/attachments/supported")
async def supported_attachments():
    """前端据此渲染可接受的文件类型。"""
    return supported_extensions()


@router.get("/{companion_id}/branches/{branch_id}/images")
async def list_branch_images(companion_id: str, branch_id: str):
    """列出该分支消息引用过的图片附件，供图片记录面板使用。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    images = get_branch_image_attachments(companion_id, branch_id)
    return {"images": images, "count": len(images)}


@router.delete("/{companion_id}/branches/{branch_id}/images")
async def delete_branch_image_endpoint(
    companion_id: str,
    branch_id: str,
    url: str = Query(..., min_length=1, description="图片附件的静态 URL"),
):
    """
    彻底删除一张图片附件：磁盘文件 + 所有消息里的 metadata 引用。

    先删文件再清引用。文件已不存在时仍然清引用，否则面板会一直列出一个点不开的
    破图；反过来若清引用失败，下次请求还能列出该 url，用户再点一次删除即可自愈。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")

    file_deleted = delete_image(url)
    cleared = remove_attachment_reference(url)

    if not file_deleted and cleared == 0:
        raise HTTPException(status_code=404, detail="图片不存在或已被删除")

    logger.info(
        f"Attachment removed: url={url} file_deleted={file_deleted} messages_updated={cleared}"
    )
    return {
        "status": "success",
        "url": url,
        "file_deleted": file_deleted,
        "cleared_messages": cleared,
    }


def _resolve_provider_max_context(model: str) -> int:
    """
    Look up the context window configured for the provider serving `model`.

    The provider is matched by model name first so a model that belongs to a
    non-active provider still gets its own window; otherwise the active
    provider's value applies. 0 means the built-in table decides.
    """
    try:
        config_path = LLM_CONFIG_PATH
        if not config_path.exists():
            return 0
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        providers = config.get("providers", [])
        for provider in providers:
            if model and model in (provider.get("models") or []):
                return int(provider.get("maxContext") or 0)
        # A local weights key never appears in provider.models: its window comes
        # from the launch args recorded in MODEL_REGISTRY.
        if model:
            try:
                from .llama_server_engine import MODEL_REGISTRY
                spec = MODEL_REGISTRY.get(model)
                if spec and spec.get("ctx"):
                    return int(spec["ctx"])
            except Exception:
                pass
        active_idx = config.get("activeProviderIndex", 0)
        if providers and 0 <= active_idx < len(providers):
            return int(providers[active_idx].get("maxContext") or 0)
    except Exception:
        return 0
    return 0


# ==================== Pydantic Models ====================

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


class SwipeRequest(BaseModel):
    companion_id: str
    message_id: int
    branch_id: str = "main"
    swipe_id: int = 0


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


class CompanionUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    personality: Optional[str] = None
    scenario: Optional[str] = None
    first_mes: Optional[str] = None
    mes_example: Optional[str] = None
    advanced_prompt: Optional[str] = None
    user_info: Optional[str] = None
    creator_notes: Optional[str] = None
    system_prompt: Optional[str] = None
    post_history_instructions: Optional[str] = None
    creator: Optional[str] = None
    character_version: Optional[str] = None
    tags: Optional[List[str]] = None
    alternate_greetings: Optional[List[str]] = None
    avatar_path: Optional[str] = None
    raw_card_data: Optional[str] = None
    depth_prompt: Optional[Dict[str, Any]] = None
    llm_provider_id: Optional[str] = None
    tts_config: Optional[Dict[str, Any]] = None
    card_ui: Optional[Dict[str, Any]] = None
    pinned: Optional[bool] = None


class BranchRenameRequest(BaseModel):
    branch_name: str


class BranchCreateRequest(BaseModel):
    branch_name: str = "新分支"
    fork_from_message_id: Optional[int] = None


class CompactRequest(BaseModel):
    companion_id: str
    branch_id: str = "main"
    llm_config: Optional[Dict[str, Any]] = None
    # Bounds are enforced here because the split logic treats preserve_recent as
    # a slice offset: 0 or a negative value silently inverts the split.
    preserve_recent: int = Field(5, ge=1, le=200)
    max_summary_tokens: int = Field(2000, ge=100, le=32000)
    preserve_important: bool = True
    max_important_messages: int = Field(20, ge=0, le=200)


class MessageImportanceRequest(BaseModel):
    companion_id: str
    branch_id: str = "main"
    message_id: int
    is_important: bool


# ==================== World Book Models ====================

class WorldBookCreateRequest(BaseModel):
    name: str
    description: str = ""


class WorldBookUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class WorldBookEntryRequest(BaseModel):
    comment: str = ""
    keys: List[str] = []
    secondary_keys: List[str] = []
    content: str = ""
    constant: bool = False
    selective: bool = True
    selective_logic: int = 0
    order: int = 100
    position: int = 0
    depth: int = 4
    disabled: bool = False
    probability: int = 100
    use_probability: bool = True
    sticky: Optional[int] = None
    cooldown: Optional[int] = None
    delay: Optional[int] = None
    group: str = ""
    group_override: bool = False
    group_weight: int = 100


class CompanionWorldBookAssociationRequest(BaseModel):
    world_book_id: int
    is_primary: bool = False


# ==================== World Book (MUST be before /{companion_id}) ====================

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
    """获取世界书详情及所有条目。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        raise HTTPException(status_code=404, detail="World book not found")
    
    entries = get_entries(world_book_id)
    return {
        "world_book": world_book,
        "entries": entries
    }


@router.put("/world-books/{world_book_id}")
async def update_world_book_detail(world_book_id: int, request: WorldBookUpdateRequest):
    """更新世界书信息。"""
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
    
    entry_id = add_entry(world_book_id, request.model_dump())
    return {"status": "success", "entry_id": entry_id}


@router.put("/world-books/{world_book_id}/entries/{entry_id}")
async def update_world_book_entry(world_book_id: int, entry_id: int, request: WorldBookEntryRequest):
    """更新条目。"""
    entry = get_entry(entry_id)
    if not entry or entry.get('id') != entry_id:
        raise HTTPException(status_code=404, detail="Entry not found")
    
    success = update_entry(entry_id, request.model_dump())
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
        data = json.loads(content.decode('utf-8'))
        
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


# ==================== Companion CRUD ====================

@router.get("/list")
async def list_companions():
    """获取所有AI伴侣列表。"""
    companions = get_all_companions()
    return {"companions": companions}


@router.get("/{companion_id}")
async def get_companion_detail(companion_id: str):
    """获取单个AI伴侣详情。"""
    data = get_companion(companion_id)
    if not data:
        raise HTTPException(status_code=404, detail="Companion not found")
    return {"companion": data}


@router.post("/{companion_id}")
async def create_companion_detail(companion_id: str, request: CompanionUpdateRequest):
    """创建新的AI伴侣。"""
    from .companion_db import create_companion
    fields = request.dict(exclude_none=True)
    success = create_companion(companion_id, fields)
    if not success:
        raise HTTPException(status_code=409, detail="Companion already exists")
    return {"status": "success", "companion_id": companion_id}


@router.put("/{companion_id}")
async def update_companion_detail(companion_id: str, request: CompanionUpdateRequest):
    """更新AI伴侣配置。"""
    fields = request.dict(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    success = update_companion(companion_id, fields)
    if not success:
        raise HTTPException(status_code=404, detail="Companion not found")
    return {"status": "success", "message": "Companion updated"}


@router.delete("/{companion_id}")
async def delete_companion_detail(companion_id: str):
    """删除AI伴侣及其所有聊天记录和分支。"""
    success = delete_companion(companion_id)
    if not success:
        raise HTTPException(status_code=404, detail="Companion not found")
    return {"status": "success", "message": "Companion deleted"}


# ==================== Card Import ====================

class StagedUploadInitRequest(BaseModel):
    filename: str
    total_size: int = 0
    file_type: str = ""


class StagedUploadChunkRequest(BaseModel):
    offset: int
    data_b64: str


@router.post("/import/upload/init")
async def init_card_upload(request: StagedUploadInitRequest):
    """初始化分片上传会话（支持大体积 .charx/.png/.json，兼容 Tauri WebSocket 桥接）。"""
    try:
        return init_staged_upload(request.filename, request.total_size, request.file_type)
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/import/upload/{upload_id}/chunk")
async def upload_card_chunk(upload_id: str, request: StagedUploadChunkRequest):
    """写入单个分片到磁盘暂存区。"""
    try:
        return await anyio.to_thread.run_sync(
            append_staged_chunk, upload_id, request.offset, request.data_b64
        )
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/import/upload/{upload_id}/commit")
async def commit_card_upload(upload_id: str):
    """完成分片上传并从磁盘流式导入角色卡。"""
    try:
        companion_id = await anyio.to_thread.run_sync(commit_staged_upload, upload_id)
        return {"status": "success", "companion_id": companion_id}
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        logger.error("Staged card commit failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.delete("/import/upload/{upload_id}")
async def abort_card_upload(upload_id: str):
    """取消并清理分片上传暂存文件。"""
    try:
        removed = abort_staged_upload(upload_id)
        return {"status": "success", "removed": removed}
    except TransferError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/import/json")
async def import_tavern_json(file: UploadFile = File(...)):
    """导入 JSON 角色卡。"""
    if not file.filename or not file.filename.lower().endswith('.json'):
        raise HTTPException(status_code=400, detail="File must be a .json file")

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _stream_upload_to_tempfile(file, max_bytes=MAX_CARD_UPLOAD_BYTES, suffix='.json')
        companion_id = await anyio.to_thread.run_sync(import_card_file, str(tmp_path), 'json')
        return {"status": "success", "companion_id": companion_id}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@router.post("/import/png")
async def import_tavern_png(file: UploadFile = File(...)):
    """导入 PNG 角色卡（从元数据提取）。"""
    if not file.filename or not file.filename.lower().endswith('.png'):
        raise HTTPException(status_code=400, detail="File must be a .png file")

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _stream_upload_to_tempfile(file, max_bytes=MAX_CARD_UPLOAD_BYTES, suffix='.png')
        companion_id = await anyio.to_thread.run_sync(import_card_file, str(tmp_path), 'png')
        return {"status": "success", "companion_id": companion_id}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@router.post("/import/charx")
async def import_tavern_charx(file: UploadFile = File(...)):
    """流式导入 CHARX（.charx / .zip）角色卡及其图片/音视频资产。"""
    fname = (file.filename or "").lower()
    if not fname.endswith(('.charx', '.zip')):
        raise HTTPException(status_code=400, detail="File must be a .charx or .zip file")

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _stream_upload_to_tempfile(file, max_bytes=MAX_CARD_UPLOAD_BYTES, suffix='.charx')
        companion_id = await anyio.to_thread.run_sync(import_card_file, str(tmp_path), 'charx')
        return {"status": "success", "companion_id": companion_id}
    except HTTPException:
        raise
    except (CharxError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Import CHARX failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


class RisumBase64ImportRequest(BaseModel):
    filename: str = "module.risum"
    data_b64: str


@router.post("/{companion_id}/import/risum")
async def import_risum_for_companion(companion_id: str, file: UploadFile = File(...)):
    """导入 RisuAI `.risum`（或 `module.json`）模块并合并至指定角色卡。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    try:
        raw = await file.read()
        if len(raw) > MAX_CARD_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Module file too large")
        return await anyio.to_thread.run_sync(merge_risum_into_companion, companion_id, raw)
    except HTTPException:
        raise
    except RisumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Import .risum failed for %s: %s", companion_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import .risum failed: {str(e)}")


@router.post("/{companion_id}/import/risum/json")
async def import_risum_base64_for_companion(
    companion_id: str,
    request: RisumBase64ImportRequest,
):
    """通过 JSON base64 导入 `.risum` 模块（兼容 Tauri WebSocket 桥接）。"""
    import base64

    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    try:
        raw = base64.b64decode(request.data_b64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}")
    try:
        return await anyio.to_thread.run_sync(merge_risum_into_companion, companion_id, raw)
    except RisumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Import .risum (json) failed for %s: %s", companion_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import .risum failed: {str(e)}")


# ==================== Card Export ====================

@router.get("/{companion_id}/export/json")
async def export_tavern_json(companion_id: str):
    """导出角色卡为JSON格式。"""
    from .tavern_exporter import export_to_json, get_export_filename
    from urllib.parse import quote
    
    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")
    
    try:
        json_content = export_to_json(companion)
        filename = get_export_filename(companion, 'json')
        encoded_filename = quote(filename)
        return Response(
            content=json_content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
        )
    except Exception as e:
        import traceback
        print(f"[Export JSON] Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/{companion_id}/export/png")
async def export_tavern_png(companion_id: str):
    """导出角色卡为PNG格式（嵌入元数据）。"""
    from .tavern_exporter import export_to_png, get_export_filename
    from urllib.parse import quote
    
    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")
    
    try:
        png_bytes = export_to_png(companion)
        filename = get_export_filename(companion, 'png')
        encoded_filename = quote(filename)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
        )
    except Exception as e:
        import traceback
        print(f"[Export PNG] Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/{companion_id}/export/charx")
async def export_tavern_charx(companion_id: str):
    """流式导出角色卡为 CHARX（.charx）容器包。"""
    from .tavern_exporter import export_to_charx_file, get_export_filename
    from urllib.parse import quote

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
        filename = get_export_filename(companion, 'charx')
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


# ==================== Companion Multimedia Assets ====================

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
        asset_id = add_companion_asset(companion_id, {
            "asset_type": semantic_type,
            "name": asset_name,
            "ext": ext,
            "mime_type": infer_mime(ext),
            "rel_path": rel_path,
            "source_uri": f"embeded://assets/{semantic_type}/{dest_path.name}",
            "size_bytes": size_bytes,
            "sort_order": len(get_companion_assets(companion_id)),
        })
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


# ==================== Branch Management ====================

@router.get("/{companion_id}/branches")
async def get_companion_branches(companion_id: str):
    """获取指定AI的所有对话分支。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    branches = get_branches(companion_id)
    return {"branches": branches}


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


@router.post("/{companion_id}/branches")
async def create_companion_branch(companion_id: str, request: BranchCreateRequest):
    """创建新对话分支。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    branch_id = create_branch(
        companion_id,
        request.branch_name,
        request.fork_from_message_id
    )
    if not branch_id:
        raise HTTPException(status_code=500, detail="Failed to create branch")
    return {"status": "success", "branch_id": branch_id}


@router.put("/{companion_id}/branches/{branch_id}")
async def rename_companion_branch(companion_id: str, branch_id: str, request: BranchRenameRequest):
    """Rename a branch. The main branch is renameable but not deletable."""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    ok = update_branch_name(companion_id, branch_id, request.branch_name)
    if not ok:
        raise HTTPException(status_code=404, detail="Branch not found")
    return {"status": "success", "branch_id": branch_id, "branch_name": request.branch_name}


@router.delete("/{companion_id}/branches/{branch_id}")
async def delete_companion_branch(companion_id: str, branch_id: str):
    """删除指定分支及其消息。"""
    if branch_id == "main":
        raise HTTPException(status_code=400, detail="Cannot delete main branch")
    success = delete_branch(companion_id, branch_id)
    if not success:
        raise HTTPException(status_code=404, detail="Branch not found")
    return {"status": "success", "message": "Branch deleted"}


# ==================== Message Management ====================

@router.get("/{companion_id}/branches/{branch_id}/messages")
async def get_branch_messages(companion_id: str, branch_id: str):
    """获取指定分支的完整聊天记录。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    msgs = get_messages(companion_id, branch_id)
    count = get_message_count(companion_id, branch_id)
    return {"messages": msgs, "count": count}


@router.get("/{companion_id}/branches/{branch_id}/compacted-messages")
async def get_branch_compacted_messages(companion_id: str, branch_id: str):
    """
    获取被压缩的历史消息，按压缩批次分组。

    这些消息不再进入上下文，也不在聊天流中渲染，只在此面板中按需展示。
    每个批次对应一次压缩操作。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    batches = get_compacted_messages(companion_id, branch_id)
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "batches": batches,
        "total_messages": sum(b.get("message_count", 0) for b in batches),
    }


@router.delete("/{companion_id}/branches/{branch_id}/messages/{message_id}")
async def delete_branch_message(companion_id: str, branch_id: str, message_id: int):
    """删除指定消息。"""
    success = delete_message(companion_id, branch_id, message_id)
    if not success:
        raise HTTPException(status_code=404, detail="Message not found")
    token_engine.clear_recorded_usage(companion_id, branch_id)
    return {"status": "success", "message": "Message deleted"}


@router.delete("/{companion_id}/branches/{branch_id}/messages")
async def clear_branch_messages_endpoint(companion_id: str, branch_id: str):
    """清空指定分支的所有消息。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    deleted_count = clear_branch_messages(companion_id, branch_id)
    from .script_db import clear_scene_summaries, reset_session_progress

    # A playthrough on this branch loses its cards too, and goes back to the
    # first scene: an empty thread sitting on scene four would contradict itself.
    clear_scene_summaries(companion_id, branch_id)
    reset_session_progress(companion_id, branch_id)
    token_engine.clear_recorded_usage(companion_id, branch_id)
    return {"status": "success", "message": f"已清空 {deleted_count} 条消息", "deleted_count": deleted_count}


@router.get("/{companion_id}/branches/{branch_id}/messages/search")
async def search_branch_messages(
    companion_id: str,
    branch_id: str,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="最大返回条数"),
):
    """在分支消息内容中搜索关键词。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    msgs = search_messages(companion_id, branch_id, q, limit)
    return {"messages": msgs, "count": len(msgs)}


# ==================== Token & Compaction ====================

@router.get("/{companion_id}/branches/{branch_id}/tokens")
async def get_branch_token_stats(
    companion_id: str,
    branch_id: str,
    model: str = "",
    provider_type: str = "",
    base_url: str = "",
    max_context: int = 0,
):
    """
    Token accounting for a branch.

    Counts the full request payload: the character-card system prompt plus the
    branch history, using the most precise counter available for the provider.
    `exact` reports whether the number is measured or estimated.
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    messages = get_messages(companion_id, branch_id)
    
    if not model or not provider_type or not base_url:
        default_config = load_default_llm_config() or {}
        model = model or default_config.get("model") or "gpt-4"
        provider_type = provider_type or default_config.get("provider_type") or ""
        base_url = base_url or default_config.get("base_url") or ""
        api_key = default_config.get("api_key") or ""
    else:
        api_key = ""
    
    companion = Companion(companion_id)
    script_session = get_session_by_branch(companion_id, branch_id)
    if script_session:
        # A playthrough is measured with the prompt it actually sends, not the
        # card's open-chat prompt.
        system_prompt, messages = companion.script_request_parts(
            messages, int(script_session["id"])
        )
    else:
        system_prompt = companion.build_system_prompt(messages)
    
    breakdown = await acount_full_context(
        system_prompt=system_prompt,
        messages=messages,
        model_name=model,
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
    )
    # A provider-reported count covers the whole request, chat template included,
    # so it beats anything reconstructed locally. Prefer total_tokens (or
    # prompt + completion) because the reply that just finished is already saved
    # in history and keeps the top context meter aligned with the reply footer's
    # TOTAL figure.
    recorded = token_engine.get_recorded_usage(companion_id, branch_id)
    measured = bool(
        recorded and (recorded["total_tokens"] > 0 or recorded["prompt_tokens"] > 0)
    )

    if measured:
        token_count = (
            recorded["total_tokens"]
            if recorded["total_tokens"] > 0
            else recorded["prompt_tokens"] + recorded["completion_tokens"]
        )
        exact = True
        method = "provider usage"
        system_tokens = breakdown["system_tokens"]
        history_tokens = max(token_count - system_tokens, 0)
        measured_at = recorded["recorded_at"]
    else:
        token_count = breakdown["total_tokens"]
        exact = breakdown["exact"]
        method = breakdown["method"]
        system_tokens = breakdown["system_tokens"]
        history_tokens = breakdown["history_tokens"]
        measured_at = None
    
    # The window is a per-provider setting; when the caller does not pass one,
    # match the provider that actually serves this model.
    if not max_context:
        max_context = _resolve_provider_max_context(model)
    
    status = get_token_status(
        token_count,
        model,
        system_tokens=system_tokens,
        history_tokens=history_tokens,
        exact=exact,
        method=method,
        context_window_override=max_context or None,
    )
    
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "model": model,
        "provider_type": provider_type,
        "message_count": len(messages),
        "token_count": token_count,
        "system_tokens": system_tokens,
        "history_tokens": history_tokens,
        "exact": exact,
        "method": method,
        "measured": measured,
        "measured_at": measured_at,
        "context_window": status["limit"],
        "threshold": status["threshold"],
        "percentage": status["percentage"],
        "should_compact": status["should_compact"],
        "status": status["status"],
    }


_compact_locks: Dict[str, asyncio.Lock] = {}


def _compact_lock(companion_id: str, branch_id: str) -> asyncio.Lock:
    key = f"{companion_id}:{branch_id}"
    lock = _compact_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _compact_locks[key] = lock
    return lock


def _serialized_compaction(func):
    """Run one compaction per branch at a time.

    Two clicks in quick succession would otherwise both read the same history
    and store two summary rows for the same range.
    """
    @functools.wraps(func)
    async def wrapper(companion_id: str, branch_id: str, request: CompactRequest):
        # Explicit signature: FastAPI resolves parameters from the wrapper, so
        # it must not depend on __wrapped__ introspection.
        async with _compact_lock(str(companion_id), str(branch_id)):
            return await func(companion_id, branch_id, request)
    return wrapper


@router.post("/{companion_id}/branches/{branch_id}/compact")
@_serialized_compaction
async def compact_branch_endpoint(
    companion_id: str, 
    branch_id: str, 
    request: CompactRequest
):
    """
    压缩指定分支的对话历史。
    将早期的消息压缩为摘要，保留最近的消息和重要消息。
    
    新特性：
    - 摘要消息带有唯一标识 (message_type='compaction_summary')
    - 支持保留重要消息（is_important=True的消息）
    - 压缩历史记录追踪
    - 摘要长度限制
    - 增量压缩（识别已有摘要）
    """
    companion = get_companion(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="Companion not found")
    character_name = (companion.get("name") or "").strip()
    
    # Swipes and timestamps must travel with the messages, otherwise the
    # write-back would blank every alternate reply and reset created_at.
    messages = get_messages_with_swipes(companion_id, branch_id)
    
    if len(messages) <= request.preserve_recent + 5:
        return {
            "success": False,
            "error": "消息数太少，无需压缩",
            "messages_before": len(messages),
            "messages_after": len(messages),
        }
    
    # 构建压缩配置
    config = CompactionConfig(
        preserve_recent=request.preserve_recent,
        max_summary_tokens=request.max_summary_tokens,
        preserve_important=request.preserve_important,
        max_important_messages=request.max_important_messages,
    )
    
    # 执行压缩
    # 前端只传保留参数，不带 LLM 配置。这里必须兜底到用户当前选中的 provider：
    # 以前是 `request.llm_config or {}`，空 base_url 让每一次摘要请求都报
    # "Request URL is missing an 'http://' or 'https://' protocol."，压缩从未成功过。
    llm_config = request.llm_config or load_default_llm_config() or {}
    result = await compact_conversation(
        messages=[{
            "role": msg["role"], 
            "content": msg["content"],
            "id": msg.get("id"),
            "message_type": msg.get("message_type", "chat"),
            "metadata": msg.get("metadata", {}),
            "is_important": msg.get("is_important", False),
            "swipes": msg.get("swipes"),
            "swipe_id": msg.get("swipe_id", 0),
            "created_at": msg.get("created_at"),
        } for msg in messages],
        llm_config=llm_config,
        config=config,
        companion_id=companion_id,
        branch_id=branch_id,
        character_name=character_name,
    )
    
    if result.success:
        summary_message = next(
            (m for m in result.preserved_messages if m.get("message_type") == "compaction_summary"),
            None,
        )
        if not summary_message:
            return {
                "success": False,
                "error": "压缩结果缺少摘要消息，未写入数据库",
                "messages_before": result.messages_before,
                "messages_after": result.messages_after,
            }
        
        # Only the newest summary stays visible as a chat card, so superseded
        # summary rows are removed instead of being archived.
        for summary_id in result.replaced_summary_ids:
            delete_message(companion_id, branch_id, summary_id)
        
        # Conversation messages are flagged, not deleted: the branch keeps its
        # history for review, while the runtime context shrinks.
        batch_id = f"cmp_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        mark_messages_compacted(
            companion_id, branch_id, result.compacted_message_ids, batch_id
        )
        
        save_message(
            companion_id,
            branch_id,
            summary_message.get("role", "system"),
            summary_message["content"],
            message_type="compaction_summary",
            metadata=summary_message.get("metadata") or {},
        )
        
        # The branch changed size, so any cached measurement is now wrong.
        token_engine.clear_recorded_usage(companion_id, branch_id)
        
        # 保存压缩历史
        save_compaction_history(
            companion_id=companion_id,
            branch_id=branch_id,
            messages_before=result.messages_before,
            messages_after=result.messages_after,
            messages_compacted=result.messages_compacted,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            tokens_saved=result.tokens_saved,
            summary_preview=get_summary_preview(result.summary),
            summary_full=result.summary,
            compaction_version=result.compaction_version,
            preserve_recent=request.preserve_recent,
            llm_model=llm_config.get("model", ""),
        )
    
    return {
        "success": result.success,
        "summary": result.summary if result.success else None,
        "messages_before": result.messages_before,
        "messages_after": result.messages_after,
        "messages_compacted": result.messages_compacted,
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
        "tokens_saved": result.tokens_saved,
        "important_preserved": result.important_preserved,
        "compaction_version": result.compaction_version,
        "is_incremental": result.is_incremental,
        "error": result.error,
    }


# ==================== Memories ====================

class MemoryUpdateRequest(BaseModel):
    """改记忆时只允许动这两项：正文和检索关键词。"""

    content: Optional[str] = None
    keys: Optional[List[str]] = None


@router.get("/{companion_id}/branches/{branch_id}/memories")
async def list_branch_memories(companion_id: str, branch_id: str):
    """该分支自动提取出的记忆，最新的在前。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    return {
        "memories": list_memories(companion_id, branch_id),
        "limit": MAX_MEMORIES + MAX_CHARACTER_MEMORIES,
    }


@router.patch("/{companion_id}/branches/{branch_id}/memories/{memory_id}")
async def update_branch_memory(
    companion_id: str,
    branch_id: str,
    memory_id: int,
    request: MemoryUpdateRequest,
):
    """就地改一条记忆的正文或关键词。

    提取的措辞常常差一点，删了重来要等下一次提取才有机会，而那时对话已经过去了。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    if request.content is None and request.keys is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if not update_memory(
        companion_id, branch_id, memory_id,
        content=request.content,
        keys=request.keys,
    ):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "success"}


@router.delete("/{companion_id}/branches/{branch_id}/memories/{memory_id}")
async def delete_branch_memory(companion_id: str, branch_id: str, memory_id: int):
    """删掉一条记错的记忆。用户能纠正，是这套机制可信的前提。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    if not delete_memory(companion_id, branch_id, memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "success"}


@router.delete("/{companion_id}/branches/{branch_id}/memories")
async def clear_branch_memories(companion_id: str, branch_id: str):
    """清空该分支的全部记忆。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    removed = clear_memories(companion_id, branch_id)
    return {"status": "success", "removed": removed}


# ==================== Compaction History ====================

@router.get("/{companion_id}/branches/{branch_id}/compaction-history")
async def get_branch_compaction_history(
    companion_id: str, 
    branch_id: str,
    limit: int = 10
):
    """
    获取分支的压缩历史记录。
    
    返回：
    - 压缩次数
    - 每次压缩的时间、节省的token、摘要预览等
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    history = get_compaction_history(companion_id, branch_id, limit)
    stats = get_compaction_stats(companion_id, branch_id)

    # The panel reads `message_count` / `summary` / `created_at`; the table stores
    # `messages_compacted` / `summary_preview` / `compacted_at`. Mapping here keeps
    # the schema out of the UI, which previously rendered blank rows because every
    # field it looked for was missing.
    records = [
        {
            "id": r.get("id"),
            "branch_id": r.get("branch_id"),
            "message_count": r.get("messages_compacted") or 0,
            "tokens_before": r.get("tokens_before") or 0,
            "tokens_after": r.get("tokens_after") or 0,
            "tokens_saved": r.get("tokens_saved") or 0,
            "summary": r.get("summary_preview") or "",
            "created_at": r.get("compacted_at"),
            "compaction_version": r.get("compaction_version"),
            "llm_model": r.get("llm_model"),
        }
        for r in history
    ]

    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "history": records,
        "stats": stats,
    }


@router.get("/{companion_id}/branches/{branch_id}/compaction-stats")
async def get_branch_compaction_stats(companion_id: str, branch_id: str):
    """
    获取分支的压缩统计信息。
    
    返回：
    - 总压缩次数
    - 总节省token
    - 最后压缩时间
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    stats = get_compaction_stats(companion_id, branch_id)
    version = get_compaction_version(companion_id, branch_id)
    
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "compaction_version": version,
        **stats,
    }


# ==================== Message Importance ====================

class MessageUpdateRequest(BaseModel):
    """Partial update for a stored message.

    Both fields are optional: the star toggle only sends is_important, the
    inline editor only sends content.
    """

    content: Optional[str] = None
    is_important: Optional[bool] = None


@router.put("/{companion_id}/branches/{branch_id}/messages/{message_id}")
async def update_branch_message(
    companion_id: str,
    branch_id: str,
    message_id: int,
    request: MessageUpdateRequest,
):
    """更新单条消息的内容或重要性标记。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")

    changed = False
    if request.content is not None:
        if not update_message_content(companion_id, branch_id, message_id, request.content):
            raise HTTPException(status_code=404, detail="Message not found")
        token_engine.clear_recorded_usage(companion_id, branch_id)
        changed = True
    if request.is_important is not None:
        if not set_message_importance(
            companion_id, branch_id, message_id, request.is_important
        ):
            raise HTTPException(status_code=404, detail="Message not found")
        changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "success", "message_id": message_id}



@router.put("/{companion_id}/branches/{branch_id}/messages/{message_id}/importance")
async def set_message_importance_endpoint(
    companion_id: str,
    branch_id: str,
    message_id: int,
    request: MessageImportanceRequest
):
    """
    设置消息的重要性标记。
    重要消息在压缩时会被保留，不会被压缩。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    success = set_message_importance(
        companion_id, 
        branch_id, 
        message_id, 
        request.is_important
    )
    
    if not success:
        raise HTTPException(status_code=404, detail="Message not found")
    
    return {
        "status": "success",
        "message_id": message_id,
        "is_important": request.is_important,
    }


@router.get("/{companion_id}/branches/{branch_id}/important-messages")
async def get_branch_important_messages(companion_id: str, branch_id: str):
    """
    获取分支中所有标记为重要的消息。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    messages = get_important_messages(companion_id, branch_id)
    
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "important_messages": messages,
        "count": len(messages),
    }


# ==================== Summary Information ====================

@router.get("/{companion_id}/branches/{branch_id}/summaries")
async def get_branch_summaries(companion_id: str, branch_id: str):
    """
    获取分支中所有的摘要消息。
    
    返回所有历史摘要，按时间正序排列。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    summaries = get_summary_messages(companion_id, branch_id)
    
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "summaries": summaries,
        "count": len(summaries),
    }


@router.get("/{companion_id}/branches/{branch_id}/latest-summary")
async def get_branch_latest_summary(companion_id: str, branch_id: str):
    """
    获取分支中最新的摘要消息。
    """
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    summary = get_latest_summary(companion_id, branch_id)
    
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "has_summary": summary is not None,
        "summary": summary,
    }

@router.post("/chat/stream")
async def companion_chat_stream(request: ChatStreamRequest):
    """
    流式聊天端点。
    完全独立的SSE流，不依赖现有/chat/stream。
    """
    import logging
    logger = logging.getLogger("companion_chat")
    logger.info(f"Received companion chat request: companion_id={request.companion_id}, message={request.message[:50]}")
    
    # 验证companion存在
    companion_data = get_companion(request.companion_id)
    if not companion_data:
        logger.error(f"Companion not found: {request.companion_id}")
        raise HTTPException(status_code=404, detail="Companion not found")
    logger.info(f"Companion found: {companion_data['name']}")
    
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
                logger.debug(f"Yielding chunk #{chunk_count}: type={chunk.get('type')}")
                _record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            logger.info(f"Stream complete, yielded {chunk_count} chunks")
            # Picture Studio hook. Only plain chat: script branches are driven by
            # their HUD, and firing here as well would double up on the same turn.
            # The call is fire-and-forget and swallows its own errors, so a broken
            # image provider can never turn a finished reply into a stream error.
            if not request.script_session_id:
                try:
                    picture_service.maybe_auto(
                        companion_id=request.companion_id,
                        branch_id=request.branch_id,
                        source="chat",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("picture auto trigger skipped: %s", exc)
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
    
    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== Chat Audio (Streaming + STT) ====================

def _get_stt_engine():
    """Resolve the active STT engine through the registry (lazy loaded)."""
    from .stt_registry import get_registry
    return get_registry().get_engine()


def _get_audio_mime(ext: str) -> str:
    """根据文件扩展名返回音频的 MIME 类型。"""
    mime_map = {
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'flac': 'audio/flac',
        'm4a': 'audio/mp4',
        'ogg': 'audio/ogg',
        'pcm': 'audio/L16',
    }
    return mime_map.get(ext, 'audio/wav')


class STTConfigRequest(BaseModel):
    active: str


class STTEnginePayload(BaseModel):
    id: str = ""
    label: str = ""
    kind: str = "audiocpp"  # audiocpp | openai_stt | builtin
    family: str = ""
    modelPath: str = ""
    computeBackend: str = "vulkan"
    baseUrl: str = ""
    apiKey: str = ""
    model: str = "whisper-1"
    language: str = ""


@router.get("/stt/specs")
async def get_stt_specs():
    """List ASR families supported by the bundled audio.cpp runtime."""
    from .audio_specs import list_specs
    return {"specs": list_specs("asr")}


@router.get("/stt/config")
async def get_stt_config():
    """List STT engines with their load state and the active one."""
    from .stt_registry import get_registry
    return get_registry().status()


@router.post("/stt/config")
async def save_stt_config(request: STTConfigRequest):
    """Switch the active STT engine.

    The engine is not warmed here: ASR stays unloaded until the first
    transcription actually needs it, so an idle app holds no GPU memory.
    """
    from .stt_registry import get_registry
    reg = get_registry()
    try:
        reg.set_active(request.active)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "active": request.active}


@router.post("/stt/engines")
async def upsert_stt_engine(request: STTEnginePayload):
    """Create or update a custom STT engine (or override a built-in engine's path)."""
    from .stt_registry import get_registry
    reg = get_registry()
    try:
        eid = await asyncio.to_thread(reg.upsert_engine, request.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "id": eid, **reg.status()}


@router.delete("/stt/engines/{engine_id}")
async def delete_stt_engine(engine_id: str):
    """Delete a custom STT engine (or clear a built-in engine's path override)."""
    from .stt_registry import get_registry
    reg = get_registry()
    try:
        await asyncio.to_thread(reg.delete_engine, engine_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "success", **reg.status()}


@router.post("/stt/stop")
async def stop_stt_engine():
    """End the active STT server and free its VRAM, now.

    audio.cpp's idle timer already unloads the weights after two minutes, but it
    leaves the process holding its port, and a process this backend did not
    spawn survives every later stop(). This endpoint is the manual button: the
    server is ended outright and the instance dropped, so the next transcription
    starts from a clean spawn instead of inheriting a stale one.
    """
    from .stt_registry import get_registry

    reg = get_registry()
    try:
        # Blocking: the health probe plus the port sweep wait on a child process.
        engine, result = await asyncio.to_thread(reg.stop_active)
    except Exception as e:
        logger.warning("stt stop failed: %s", e)
        raise HTTPException(status_code=500, detail=f"STT stop failed: {e}")
    return {"engine": engine, "result": result, "active": reg.active_id}


@router.post("/stt")
async def transcribe_audio(
    file: UploadFile = File(...),
    sample_rate: int = Form(16000),
):
    """Transcribe a WAV upload with the active engine."""
    engine = _get_stt_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="STT model not loaded")
    audio_bytes = await _read_upload(file)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")
    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, engine.transcribe_bytes, audio_bytes, sample_rate
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    return {"text": (text or "").strip()}


async def _audio_chat_stream(
    audio_bytes: bytes,
    companion_id: str,
    branch_id: str,
    enable_thinking_bool: bool,
    llm_config_dict: dict,
    temperature_float: float,
    top_p_float: float,
    sender: str = "user",
):
    """Transcribe the clip, then stream the reply.

    Shared by the multipart and JSON entry points so the two cannot drift.
    """
    import logging
    logger = logging.getLogger("companion_chat_audio")

    stt_engine = _get_stt_engine()
    if stt_engine is None:
        raise HTTPException(status_code=503, detail="STT model not loaded")

    companion_data = get_companion(companion_id)
    if not companion_data:
        raise HTTPException(status_code=404, detail="Companion not found")

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        loop = asyncio.get_event_loop()
        transcribed = await loop.run_in_executor(None, stt_engine.transcribe_bytes, audio_bytes, 16000)
        user_message_text = transcribed.strip() if transcribed else "[语音消息]"
    except Exception as e:
        logger.error(f"STT transcription failed: {e}")
        user_message_text = "[语音消息]"

    logger.info(
        f"Companion audio chat: id={companion_id}, branch={branch_id}, stt={user_message_text[:50]}"
    )

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'stt_transcript', 'content': user_message_text}, ensure_ascii=False)}\n\n"

            companion = Companion(companion_id)
            async for chunk in companion.generate_response_stream(
                user_message=user_message_text,
                branch_id=branch_id,
                llm_config=llm_config_dict if llm_config_dict else None,
                enable_thinking=enable_thinking_bool,
                audio_file_path=tmp_path,
                temperature=temperature_float,
                top_p=top_p_float,
                sender=sender,
                is_voice=True,
            ):
                _record_usage_chunk(chunk, companion_id, branch_id)
                # Same framing as every other stream endpoint: the generator
                # hands back dicts, and only the SSE writer turns them into
                # bytes. Yielding the dict itself reached StreamingResponse,
                # which then called .encode() on it.
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class AudioStreamJsonRequest(BaseModel):
    """Same payload as the multipart endpoint, with the clip base64-encoded.

    The socket bridge only carries JSON frames, so the shell cannot post a
    FormData here; it sends the bytes as base64 in the body instead.
    """

    audio_b64: str
    companion_id: str
    branch_id: str = "main"
    enable_thinking: bool = False
    llm_config: dict = Field(default_factory=dict)
    temperature: float = 0.7
    top_p: float = 0.9
    sender: str = "user"


@router.post("/chat/audio/stream-json")
async def companion_chat_audio_stream_json(request: AudioStreamJsonRequest):
    import base64 as _b64

    try:
        audio_bytes = _b64.b64decode(request.audio_b64, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audio payload")

    return await _audio_chat_stream(
        audio_bytes=audio_bytes,
        companion_id=request.companion_id,
        branch_id=request.branch_id,
        enable_thinking_bool=bool(request.enable_thinking),
        llm_config_dict=request.llm_config or {},
        temperature_float=float(request.temperature),
        top_p_float=float(request.top_p),
        sender=request.sender,
    )


@router.post("/chat/audio/stream")
async def companion_chat_audio_stream(
    file: UploadFile = File(...),
    companion_id: str = Form(...),
    branch_id: str = Form("main"),
    enable_thinking: str = Form("false"),
    llm_config: str = Form("{}"),
    temperature: str = Form("0.7"),
    top_p: str = Form("0.9"),
    sender: str = Form("user"),
):
    """音频流式聊天端点（multipart）。

    接收音频文件，STT 转录文字存入历史，同时将原始音频传给多模态 LLM，
    流式返回回复。打包后的 shell 无法提交 FormData，走 /chat/audio/stream-json。
    """
    audio_bytes = await _read_upload(file)

    try:
        llm_config_dict = json.loads(llm_config) if llm_config else {}
    except json.JSONDecodeError:
        llm_config_dict = {}

    return await _audio_chat_stream(
        audio_bytes=audio_bytes,
        companion_id=companion_id,
        branch_id=branch_id,
        enable_thinking_bool=enable_thinking.lower() in ("true", "1", "yes"),
        llm_config_dict=llm_config_dict,
        temperature_float=float(temperature) if temperature else 0.7,
        top_p_float=float(top_p) if top_p else 0.9,
        sender=sender,
    )


# ==================== Chat Continue (Streaming) ====================

@router.post("/chat/continue/stream")
async def companion_chat_continue_stream(request: ContinueStreamRequest):
    """
    续写最后一条AI回复。
    流式SSE，与/chat/stream格式完全一致。
    """
    import logging
    logger = logging.getLogger("companion_chat_continue")
    logger.info(f"Received continue request: companion_id={request.companion_id}")

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
                _record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Continue stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== Chat Regenerate (Streaming) ====================

@router.post("/chat/regenerate/stream")
async def companion_chat_regenerate_stream(request: RegenerateStreamRequest):
    """
    重新生成指定AI消息。
    流式SSE，与/chat/stream格式完全一致。
    """
    import logging
    logger = logging.getLogger("companion_chat_regenerate")
    logger.info(f"Received regenerate request: companion_id={request.companion_id}, msg_id={request.message_id}")

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
                    _record_usage_chunk(chunk, request.companion_id, request.branch_id)
                elif isinstance(chunk, dict) and chunk.get("type") == "usage":
                    token_engine.clear_recorded_usage(request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Regenerate stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/chat/reedit/stream")
async def companion_chat_reedit_stream(request: ReeditStreamRequest):
    """改写一条用户消息并重新生成回复。

    改写会作废该消息之后的整段对话，所以后端先删掉那些消息再生成，
    前端收到 done 后重新拉取整个分支即可，不需要自己推算删了多少条。
    """
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
                _record_usage_chunk(chunk, request.companion_id, request.branch_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Reedit stream error: {e}", exc_info=True)
            error_chunk = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== Swipe Navigation ====================

@router.put("/chat/swipe")
async def swipe_message(request: SwipeRequest):
    """切换消息的swipe版本。"""
    result = set_message_swipe_id(
        request.companion_id,
        request.branch_id,
        request.message_id,
        request.swipe_id,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Message not found or invalid swipe_id")
    gen = result.get("generation") if result.get("is_last") else None
    if isinstance(gen, dict) and gen.get("tokens_source") == "usage":
        prompt = int(gen.get("prompt_tokens") or 0)
        completion = int(gen.get("completion_tokens") or 0)
        ctx_total = int(gen.get("context_tokens") or gen.get("total_tokens") or 0)
        if ctx_total <= 0 and prompt > 0:
            ctx_total = prompt + completion
        if ctx_total > 0 or prompt > 0:
            token_engine.record_usage(
                request.companion_id,
                request.branch_id,
                {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": ctx_total,
                },
            )
        else:
            token_engine.clear_recorded_usage(request.companion_id, request.branch_id)
    else:
        token_engine.clear_recorded_usage(request.companion_id, request.branch_id)
    return {"status": "success", "content": result["content"], "swipe_id": result["swipe_id"], "swipes": result["swipes"]}


@router.get("/{companion_id}/branches/{branch_id}/messages-with-swipes")
async def get_branch_messages_with_swipes(companion_id: str, branch_id: str):
    """获取指定分支的完整聊天记录（包含swipe信息）。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    msgs = get_messages_with_swipes(companion_id, branch_id)
    return {"messages": msgs}


# ==================== User Settings (MUST be before /{companion_id} routes) ====================

@router.get("/user/settings")
async def get_user_setting():
    """获取用户设置。"""
    settings = get_user_settings()
    return {"settings": settings}


@router.put("/user/settings")
async def update_user_setting(name: Optional[str] = None, avatar_path: Optional[str] = None):
    """更新用户设置。"""
    success = update_user_settings(name, avatar_path)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update settings")
    return {"status": "success"}


@router.post("/user/avatar")
async def upload_user_avatar(file: UploadFile = File(...)):
    """上传用户头像。"""
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        content = await _read_upload(file)
        
        import uuid
        upload_dir = AVATAR_DIR
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        ext = file.filename.split('.')[-1] if '.' in file.filename else 'png'
        filename = f"user_avatar_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = upload_dir / filename
        
        with open(filepath, 'wb') as f:
            f.write(content)
        
        avatar_url = f"uploads/companion_avatars/{filename}"
        update_user_settings(avatar_path=avatar_url)
        
        return {"status": "success", "avatar_path": avatar_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


# ==================== Companion Avatar Upload ====================

@router.post("/{companion_id}/avatar")
async def upload_companion_avatar(companion_id: str, file: UploadFile = File(...)):
    """上传AI伴侣头像。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")

    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        content = await _read_upload(file)

        import uuid
        upload_dir = AVATAR_DIR
        upload_dir.mkdir(parents=True, exist_ok=True)

        ext = file.filename.split('.')[-1] if '.' in file.filename else 'png'
        filename = f"companion_{companion_id[:8]}_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = upload_dir / filename

        with open(filepath, 'wb') as f:
            f.write(content)

        avatar_url = f"uploads/companion_avatars/{filename}"
        update_companion(companion_id, {"avatar_path": avatar_url})

        return {"status": "success", "avatar_path": avatar_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


# ==================== Companion - World Book Association ====================

@router.get("/{companion_id}/world-books")
async def get_companion_world_books_list(companion_id: str):
    """获取AI伴侣关联的所有世界书。"""
    if not get_companion(companion_id):
        raise HTTPException(status_code=404, detail="Companion not found")
    
    world_books = get_companion_world_books(companion_id)
    return {"world_books": world_books}


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

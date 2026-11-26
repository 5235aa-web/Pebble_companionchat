"""Companion Chat V2 routes, mounted onto `api_routes.router` under `/companions`."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import token_engine
from .chat_exporter import export_branch_chat
from .chat_importer import import_jsonl_branch, inspect_jsonl_chat
from .companion import Companion
from .companion_db import get_companion, is_last_active_message
from .picturesys import service as picture_service

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

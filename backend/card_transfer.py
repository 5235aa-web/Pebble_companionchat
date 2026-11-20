"""card_transfer.py - Staged, disk-backed large-file upload & export manager.

Designed so multi-hundred-megabyte CHARX archives transfer reliably over both:
1. Standard HTTP multipart / streaming responses (browser mode), and
2. Chunked JSON / binary calls over the `/ws` ASGI bridge (Tauri desktop shell),
   keeping per-frame sizes well under WebSocket limits and RAM usage flat.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import paths
from .charx import MAX_CARD_UPLOAD_BYTES, CharxError
from .tavern_exporter import export_to_charx_file, get_export_filename
from .tavern_importer import import_card_file

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 256 * 1024     # 256 KB raw (~342 KB base64 JSON frame)
EXPORT_CHUNK_BYTES = 256 * 1024     # 256 KB raw (~3 WS frames of 96 KB)
MAX_EXPORT_SLICE_BYTES = 2 * 1024 * 1024
_STALE_UPLOAD_AGE_SEC = 2 * 3600
_STALE_EXPORT_AGE_SEC = 3600
_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TransferError(ValueError):
    """Client-visible transfer validation error (maps to HTTP 400/404/413)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _validate_hex_id(token: str, label: str = "id") -> str:
    if not isinstance(token, str) or not _HEX_ID_RE.match(token):
        raise TransferError(f"Invalid {label}", status_code=400)
    return token


def _in_root() -> Path:
    root = paths.TMP_UPLOAD_DIR / "in"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _out_root() -> Path:
    root = paths.TMP_UPLOAD_DIR / "out"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_stale(root: Path, max_age_sec: int) -> None:
    """Best-effort sweep for abandoned staging files/directories."""
    now = time.time()
    try:
        for entry in root.iterdir():
            try:
                if now - entry.stat().st_mtime <= max_age_sec:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _infer_card_type(filename: str, explicit_type: str = "") -> str:
    kind = (explicit_type or "").strip().lower().lstrip(".")
    if not kind and filename and "." in filename:
        kind = filename.rsplit(".", 1)[-1].lower()
    if kind in ("charx", "zip"):
        return "charx"
    if kind in ("png", "json"):
        return kind
    raise TransferError("Unsupported card format; expected .charx, .zip, .png, or .json", status_code=400)


# ==================== Chunked Upload ====================

def init_staged_upload(filename: str, total_size: int = 0, file_type: str = "") -> Dict:
    """Create a disk-backed staging slot for chunked card upload."""
    _cleanup_stale(_in_root(), _STALE_UPLOAD_AGE_SEC)

    if total_size < 0:
        raise TransferError("Invalid total_size", status_code=400)
    if total_size > MAX_CARD_UPLOAD_BYTES:
        raise TransferError(
            f"File exceeds {MAX_CARD_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            status_code=413,
        )

    kind = _infer_card_type(filename, file_type)
    upload_id = uuid.uuid4().hex
    slot_dir = _in_root() / upload_id
    slot_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "upload_id": upload_id,
        "filename": Path(filename or f"card.{kind}").name,
        "file_type": kind,
        "total_size": int(total_size),
        "received_bytes": 0,
        "created_at": time.time(),
    }
    (slot_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (slot_dir / "payload.part").write_bytes(b"")

    return {
        "upload_id": upload_id,
        "file_type": kind,
        "chunk_size": UPLOAD_CHUNK_BYTES,
        "max_bytes": MAX_CARD_UPLOAD_BYTES,
    }


def append_staged_chunk(upload_id: str, offset: int, data_b64: str) -> Dict:
    """Append one base64-encoded chunk to a staged upload file on disk."""
    _validate_hex_id(upload_id, "upload_id")
    slot_dir = _in_root() / upload_id
    meta_path = slot_dir / "meta.json"
    part_path = slot_dir / "payload.part"
    if not meta_path.is_file() or not part_path.is_file():
        raise TransferError("Upload session not found", status_code=404)

    if not isinstance(data_b64, str) or not data_b64:
        raise TransferError("Empty chunk data", status_code=400)
    if len(data_b64) > UPLOAD_CHUNK_BYTES * 2:
        raise TransferError("Chunk exceeds maximum size", status_code=413)

    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        raise TransferError(f"Invalid base64 chunk: {exc}", status_code=400) from exc

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    received = int(meta.get("received_bytes", 0))
    declared_total = int(meta.get("total_size", 0))

    if offset < 0:
        raise TransferError("Negative chunk offset", status_code=400)

    # Idempotent retry of an already-committed slice.
    if offset + len(raw) <= received:
        return {"upload_id": upload_id, "received_bytes": received}

    if offset != received:
        raise TransferError(
            f"Unexpected chunk offset {offset} (expected {received})",
            status_code=409,
        )

    new_received = received + len(raw)
    if new_received > MAX_CARD_UPLOAD_BYTES:
        raise TransferError(
            f"File exceeds {MAX_CARD_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            status_code=413,
        )
    if declared_total > 0 and new_received > declared_total:
        raise TransferError("Uploaded bytes exceed declared total_size", status_code=400)

    with open(part_path, "ab") as f:
        f.write(raw)

    meta["received_bytes"] = new_received
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return {"upload_id": upload_id, "received_bytes": new_received}


def commit_staged_upload(upload_id: str) -> str:
    """Finalize a staged upload, import the card from disk, and clean up."""
    _validate_hex_id(upload_id, "upload_id")
    slot_dir = _in_root() / upload_id
    meta_path = slot_dir / "meta.json"
    part_path = slot_dir / "payload.part"
    if not meta_path.is_file() or not part_path.is_file():
        raise TransferError("Upload session not found", status_code=404)

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        declared_total = int(meta.get("total_size", 0))
        actual_size = part_path.stat().st_size
        if actual_size == 0:
            raise TransferError("Uploaded file is empty", status_code=400)
        if declared_total > 0 and actual_size != declared_total:
            raise TransferError(
                f"Incomplete upload: received {actual_size} of {declared_total} bytes",
                status_code=400,
            )
        return import_card_file(str(part_path), meta.get("file_type"))
    except CharxError as exc:
        raise TransferError(str(exc), status_code=400) from exc
    except ValueError as exc:
        raise TransferError(str(exc), status_code=400) from exc
    finally:
        shutil.rmtree(slot_dir, ignore_errors=True)


def abort_staged_upload(upload_id: str) -> bool:
    """Cancel and delete a staged upload directory."""
    _validate_hex_id(upload_id, "upload_id")
    slot_dir = _in_root() / upload_id
    if not slot_dir.exists():
        return False
    shutil.rmtree(slot_dir, ignore_errors=True)
    return True


# ==================== Staged Chunked Export ====================

def prepare_charx_export(companion_data: Dict, assets: List[Dict]) -> Dict:
    """Build a `.charx` archive in staging storage for chunked or streaming download."""
    _cleanup_stale(_out_root(), _STALE_EXPORT_AGE_SEC)

    export_id = uuid.uuid4().hex
    dest_path = _out_root() / f"{export_id}.charx"
    export_to_charx_file(companion_data, assets, dest_path)

    total_size = dest_path.stat().st_size
    filename = get_export_filename(companion_data, "charx")
    meta = {
        "export_id": export_id,
        "filename": filename,
        "total_size": total_size,
        "created_at": time.time(),
    }
    (_out_root() / f"{export_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    return {
        "export_id": export_id,
        "filename": filename,
        "total_size": total_size,
        "chunk_size": EXPORT_CHUNK_BYTES,
    }


def read_export_chunk(export_id: str, offset: int, length: int = EXPORT_CHUNK_BYTES) -> bytes:
    """Read a bounded byte slice from a staged export file."""
    _validate_hex_id(export_id, "export_id")
    if offset < 0:
        raise TransferError("Negative offset", status_code=400)
    if length <= 0 or length > MAX_EXPORT_SLICE_BYTES:
        raise TransferError(f"Chunk length must be between 1 and {MAX_EXPORT_SLICE_BYTES}", status_code=400)

    archive_path = _out_root() / f"{export_id}.charx"
    if not archive_path.is_file():
        raise TransferError("Export file not found or expired", status_code=404)

    total_size = archive_path.stat().st_size
    if offset >= total_size:
        return b""

    with open(archive_path, "rb") as f:
        f.seek(offset)
        return f.read(length)


def cleanup_staged_export(export_id: str) -> bool:
    """Delete a staged export archive and its sidecar metadata."""
    _validate_hex_id(export_id, "export_id")
    archive_path = _out_root() / f"{export_id}.charx"
    meta_path = _out_root() / f"{export_id}.meta.json"
    existed = archive_path.exists() or meta_path.exists()
    archive_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return existed

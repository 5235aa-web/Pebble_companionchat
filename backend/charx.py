"""charx.py - CHARX (Character Card V3 ZIP container) parser & builder.

Handles:
- Streaming extraction of .charx / .zip / charx-jpeg polyglot archives to disk
- Anti-Zip-Slip path validation and decompression bomb limits
- CCv3 `data.assets` (embeded://, embedded://, __asset:, data:...;base64,...)
- RisuAI `extensions.risuai` (`emotions`, `additionalAssets`) and fallback media
- Streaming .charx archive creation with ZIP_STORED for pre-compressed media
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from . import paths
from .rpack import RisumError, parse_risum_bytes

logger = logging.getLogger(__name__)

# Default 1 GB card upload ceiling (configurable via PEBBLE_MAX_CARD_BYTES).
_DEFAULT_MAX_CARD_BYTES = 1024 * 1024 * 1024
MAX_CARD_UPLOAD_BYTES = max(
    1024 * 1024,
    int(os.getenv("PEBBLE_MAX_CARD_BYTES", str(_DEFAULT_MAX_CARD_BYTES)) or _DEFAULT_MAX_CARD_BYTES),
)

MAX_CHARX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB uncompressed safety cap
MAX_CHARX_SINGLE_ASSET_BYTES = 256 * 1024 * 1024    # 256 MB per asset
MAX_CHARX_ASSETS_COUNT = 10000
MAX_CARD_JSON_BYTES = 32 * 1024 * 1024              # 32 MB for card.json
_STREAM_CHUNK = 64 * 1024

IMAGE_EXTS: Set[str] = {
    "png", "jpg", "jpeg", "webp", "gif", "avif", "bmp", "svg", "apng", "ico",
}
AUDIO_EXTS: Set[str] = {
    "mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba",
}
VIDEO_EXTS: Set[str] = {
    "mp4", "webm", "mov", "mkv", "m4v",
}

# Pre-compressed formats stored without re-deflating to avoid burning CPU.
COMPRESSED_MEDIA_EXTS: Set[str] = {
    "png", "jpg", "jpeg", "webp", "avif", "gif", "apng",
    "mp3", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba",
    "mp4", "webm", "mov", "mkv", "m4v",
}

MIME_BY_EXT: Dict[str, str] = {
    "png": "image/png",
    "apng": "image/apng",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "opus": "audio/opus",
    "weba": "audio/webm",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "m4v": "video/mp4",
    "json": "application/json",
    "txt": "text/plain",
}

EXT_BY_MIME: Dict[str, str] = {
    "image/png": "png",
    "image/apng": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/opus": "opus",
    "audio/webm": "weba",
    "video/mp4": "mp4",
    "video/webm": "webm",
}


class CharxError(ValueError):
    """Raised when a CHARX archive is malformed, unsafe, or oversized."""


def infer_ext(name_or_uri: str, fallback: str = "bin") -> str:
    """Extract a clean lowercase extension without a leading dot."""
    clean = str(name_or_uri or "").split("?", 1)[0].split("#", 1)[0].strip()
    if "." in clean:
        ext = clean.rsplit(".", 1)[-1].lower()
        ext = re.sub(r"[^a-z0-9]", "", ext)
        if ext:
            return ext
    return fallback


def infer_mime(ext_or_name: str) -> str:
    """Return a best-effort MIME type from an extension or filename."""
    ext = ext_or_name.lower().lstrip(".")
    if "." in ext:
        ext = infer_ext(ext)
    return MIME_BY_EXT.get(ext, "application/octet-stream")


def classify_media_kind(ext: str, hint_type: str = "") -> str:
    """Classify an asset into image/audio/video/other, preserving semantic hints."""
    ext_l = ext.lower().lstrip(".")
    hint_l = (hint_type or "").lower().strip()
    if hint_l in ("icon", "emotion", "background", "user_icon", "portrait"):
        return hint_l
    if hint_l in ("audio", "bgm", "sfx", "voice"):
        return hint_l
    if ext_l in AUDIO_EXTS:
        return "audio"
    if ext_l in VIDEO_EXTS:
        return "video"
    if ext_l in IMAGE_EXTS:
        return "image"
    return hint_l or "other"


def _safe_filename_segment(value: str, fallback: str = "asset") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (value or "").strip()).strip(". ")
    return cleaned[:80] or fallback


def sanitize_rel_zip_path(raw: str) -> Optional[str]:
    """Normalize an archive path and reject traversal attempts."""
    if not raw or "\x00" in raw:
        return None
    norm = raw.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    if not norm or re.match(r"^[A-Za-z]:", norm):
        return None
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def is_zip_container(path: Path) -> bool:
    """Return True if *path* is readable by zipfile (including charx-jpeg polyglots)."""
    try:
        if zipfile.is_zipfile(path):
            return True
    except OSError:
        return False
    return False


def _copy_zip_member_to_file(
    zf: zipfile.ZipFile,
    member_name: str,
    dest_path: Path,
    remaining_budget: int,
    max_single: int = MAX_CHARX_SINGLE_ASSET_BYTES,
) -> int:
    """Stream one ZIP member to *dest_path* while enforcing size caps."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zf.open(member_name, "r") as src, open(dest_path, "wb") as dst:
        while True:
            chunk = src.read(_STREAM_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_single:
                raise CharxError(
                    f"CHARX asset '{member_name}' exceeds single-file limit ({max_single // (1024 * 1024)} MB)"
                )
            if written > remaining_budget:
                raise CharxError("CHARX extracted size exceeds safety limit")
            dst.write(chunk)
    return written


def _read_bounded_member_bytes(zf: zipfile.ZipFile, member_name: str, limit: int) -> bytes:
    """Read a small text/JSON ZIP member without trusting ZipInfo.file_size."""
    chunks: List[bytes] = []
    total = 0
    with zf.open(member_name, "r") as src:
        while True:
            chunk = src.read(_STREAM_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise CharxError(f"CHARX member '{member_name}' exceeds {limit // (1024 * 1024)} MB limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _resolve_zip_member(uri_or_path: str, members_exact: Set[str], members_lower: Dict[str, str]) -> Optional[str]:
    """Map embeded://, embedded://, __asset:, or relative paths to an archive member."""
    if not isinstance(uri_or_path, str):
        return None
    raw = uri_or_path.strip()
    if not raw or raw.startswith(("http://", "https://", "data:", "ccdefault:")):
        return None

    for prefix in ("embeded://", "embedded://", "__asset:"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break

    raw = unquote(raw)
    rel = sanitize_rel_zip_path(raw)
    if not rel:
        return None

    candidates = [rel]
    if not rel.startswith("assets/"):
        candidates.append(f"assets/{rel}")

    for cand in candidates:
        if cand in members_exact:
            return cand
        hit = members_lower.get(cand.lower())
        if hit:
            return hit
    return None


def _decode_data_url(data_url: str, max_bytes: int = MAX_CHARX_SINGLE_ASSET_BYTES) -> Optional[Tuple[bytes, str]]:
    """Decode a base64 data:... URL into (bytes, ext)."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, payload = data_url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header.lower():
        return None
    mime = header[5:].split(";", 1)[0].strip().lower()
    ext = EXT_BY_MIME.get(mime) or infer_ext(mime.replace("/", "."), "bin")
    if len(payload) * 3 // 4 > max_bytes:
        raise CharxError("Embedded data URL asset exceeds size limit")
    try:
        blob = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if not blob or len(blob) > max_bytes:
        return None
    return blob, ext


def _extract_polyglot_header_image(charx_path: Path, dest_dir: Path) -> Optional[Tuple[Path, str, int]]:
    """If a charx-jpeg / PNG-ZIP polyglot starts with a valid image before PK\\x03\\x04, extract it."""
    try:
        with open(charx_path, "rb") as f:
            head = f.read(16)
            if head.startswith(b"\xff\xd8\xff"):
                ext = "jpg"
            elif head.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = "png"
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                ext = "webp"
            else:
                return None

            f.seek(0)
            # Scan in chunks for the first ZIP local file header signature (PK\x03\x04)
            # bounded to 32 MB so a huge file without an early header doesn't scan forever.
            pk_offset = -1
            scanned = 0
            tail = b""
            while scanned < 32 * 1024 * 1024:
                buf = f.read(256 * 1024)
                if not buf:
                    break
                joined = tail + buf
                idx = joined.find(b"PK\x03\x04")
                if idx != -1:
                    pk_offset = scanned - len(tail) + idx
                    break
                scanned += len(buf)
                tail = joined[-3:]

            if pk_offset <= 0:
                return None

            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / f"main.{ext}"
            f.seek(0)
            remaining = pk_offset
            with open(out_path, "wb") as out:
                while remaining > 0:
                    chunk = f.read(min(_STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            return out_path, ext, pk_offset
    except OSError:
        return None


def _pick_avatar_rel_path(assets: List[Dict]) -> Optional[str]:
    """Select the best portrait/avatar relative path from extracted assets."""
    image_assets = []
    for a in assets:
        if not a.get("rel_path"):
            continue
        ext = str(a.get("ext") or "").strip().lower().lstrip(".") or infer_ext(a["rel_path"], "")
        if ext in IMAGE_EXTS:
            image_assets.append(a)
    if not image_assets:
        return None

    for a in image_assets:
        if a.get("asset_type") == "icon" and str(a.get("name") or "").lower() == "main":
            return a["rel_path"]
    for a in image_assets:
        if a.get("asset_type") == "icon":
            return a["rel_path"]
    for a in image_assets:
        if any(tok in str(a.get("name") or "").lower() for tok in ("main", "avatar", "icon", "portrait")):
            return a["rel_path"]
    return image_assets[0]["rel_path"]


def extract_charx_package(
    charx_path: Path,
    companion_id: str,
) -> Tuple[Dict, List[Dict], Optional[str]]:
    """Extract `card.json` and multimedia assets from a CHARX archive.

    Returns:
        (raw_card_dict, extracted_assets_metadata_list, avatar_rel_path)
    """
    charx_path = Path(charx_path)
    if not charx_path.is_file():
        raise CharxError("CHARX file does not exist")

    assets_root = paths.COMPANION_ASSETS_DIR / companion_id
    data_root_resolved = paths.DATA_ROOT.resolve()
    assets_root.mkdir(parents=True, exist_ok=True)

    extracted_assets: List[Dict] = []
    remaining_budget = MAX_CHARX_EXTRACTED_BYTES

    try:
        with zipfile.ZipFile(charx_path, "r") as zf:
            members_exact: Set[str] = set()
            members_lower: Dict[str, str] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                safe_rel = sanitize_rel_zip_path(info.filename)
                if not safe_rel:
                    raise CharxError(f"Unsafe archive entry path: {info.filename!r}")
                members_exact.add(info.filename)
                members_lower.setdefault(safe_rel.lower(), info.filename)

            card_member = members_lower.get("card.json")
            if not card_member:
                raise CharxError("CHARX archive is missing card.json at root")

            card_bytes = _read_bounded_member_bytes(zf, card_member, MAX_CARD_JSON_BYTES)
            try:
                raw_card = json.loads(card_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CharxError(f"Invalid card.json: {exc}") from exc
            if not isinstance(raw_card, dict):
                raise CharxError("card.json must be a JSON object")

            data_block = raw_card.get("data") if isinstance(raw_card.get("data"), dict) else raw_card
            ext_block = data_block.get("extensions") if isinstance(data_block.get("extensions"), dict) else {}
            risu_block = ext_block.get("risuai") if isinstance(ext_block.get("risuai"), dict) else {}

            # Collect candidate asset descriptors in priority order:
            # 1) CCv3 data.assets
            # 2) RisuAI extensions.risuai.emotions / additionalAssets
            # 3) Any leftover media files inside assets/
            candidates: List[Tuple[str, str, str, str]] = []  # (type, name, uri_or_member, ext_hint)

            raw_assets = data_block.get("assets")
            if isinstance(raw_assets, list):
                for idx, entry in enumerate(raw_assets):
                    if not isinstance(entry, dict):
                        continue
                    uri = str(entry.get("uri") or "").strip()
                    if not uri:
                        continue
                    a_type = str(entry.get("type") or "other").strip() or "other"
                    a_name = str(entry.get("name") or f"asset_{idx}").strip()
                    a_ext = str(entry.get("ext") or "").strip().lstrip(".").lower()
                    candidates.append((a_type, a_name, uri, a_ext))

            emotions = risu_block.get("emotions")
            if isinstance(emotions, list):
                for item in emotions:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        candidates.append(("emotion", str(item[0] or "emotion"), str(item[1] or ""), ""))

            additional = risu_block.get("additionalAssets")
            if isinstance(additional, list):
                for item in additional:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        ext_hint = str(item[2]) if len(item) >= 3 and item[2] else ""
                        candidates.append(("x-risu-asset", str(item[0] or "asset"), str(item[1] or ""), ext_hint))

            consumed_members: Set[str] = set()
            seen_data_urls: Set[str] = set()

            def _store_member(
                a_type: str,
                a_name: str,
                member_name: str,
                source_uri: str,
                ext_hint: str,
            ) -> None:
                nonlocal remaining_budget
                if len(extracted_assets) >= MAX_CHARX_ASSETS_COUNT:
                    return
                if member_name in consumed_members:
                    return
                consumed_members.add(member_name)

                ext = (ext_hint or infer_ext(member_name, "bin")).lower()
                kind_dir = _safe_filename_segment(a_type.lower(), "other")
                base_stem = _safe_filename_segment(a_name or Path(member_name).stem, "asset")
                idx_prefix = f"{len(extracted_assets):04d}"
                disk_path = assets_root / kind_dir / f"{idx_prefix}_{base_stem}.{ext}"

                size_written = _copy_zip_member_to_file(
                    zf, member_name, disk_path, remaining_budget, MAX_CHARX_SINGLE_ASSET_BYTES
                )
                remaining_budget -= size_written
                rel_path = disk_path.resolve().relative_to(data_root_resolved).as_posix()
                semantic_type = classify_media_kind(ext, a_type)
                extracted_assets.append({
                    "asset_type": semantic_type,
                    "name": a_name or Path(member_name).stem,
                    "ext": ext,
                    "mime_type": infer_mime(ext),
                    "rel_path": rel_path,
                    "source_uri": source_uri or f"embeded://{sanitize_rel_zip_path(member_name) or member_name}",
                    "size_bytes": size_written,
                    "sort_order": len(extracted_assets),
                })

            for a_type, a_name, uri, ext_hint in candidates:
                if len(extracted_assets) >= MAX_CHARX_ASSETS_COUNT:
                    break
                member = _resolve_zip_member(uri, members_exact, members_lower)
                if member:
                    _store_member(a_type, a_name, member, uri, ext_hint)
                    continue
                if uri.startswith("data:") and uri not in seen_data_urls:
                    seen_data_urls.add(uri)
                    decoded = _decode_data_url(uri)
                    if decoded:
                        blob, data_ext = decoded
                        if len(blob) > remaining_budget:
                            raise CharxError("CHARX extracted size exceeds safety limit")
                        ext = (ext_hint or data_ext or "bin").lower()
                        kind_dir = _safe_filename_segment(a_type.lower(), "other")
                        base_stem = _safe_filename_segment(a_name, "asset")
                        disk_path = assets_root / kind_dir / f"{len(extracted_assets):04d}_{base_stem}.{ext}"
                        disk_path.parent.mkdir(parents=True, exist_ok=True)
                        disk_path.write_bytes(blob)
                        remaining_budget -= len(blob)
                        rel_path = disk_path.resolve().relative_to(data_root_resolved).as_posix()
                        extracted_assets.append({
                            "asset_type": classify_media_kind(ext, a_type),
                            "name": a_name,
                            "ext": ext,
                            "mime_type": infer_mime(ext),
                            "rel_path": rel_path,
                            "source_uri": "",
                            "size_bytes": len(blob),
                            "sort_order": len(extracted_assets),
                        })

            # Pick up any unreferenced media files bundled inside the archive (e.g. assets/... or x_meta/...).
            for info in zf.infolist():
                if len(extracted_assets) >= MAX_CHARX_ASSETS_COUNT:
                    break
                if info.is_dir() or info.filename in consumed_members or info.filename == card_member:
                    continue
                safe_rel = sanitize_rel_zip_path(info.filename)
                if not safe_rel:
                    continue
                ext = infer_ext(safe_rel, "")
                if ext not in IMAGE_EXTS and ext not in AUDIO_EXTS and ext not in VIDEO_EXTS:
                    continue
                inferred_type = "other"
                parts_lower = [p.lower() for p in safe_rel.split("/")]
                for hint in ("icon", "emotion", "emotions", "background", "audio", "bgm", "sfx", "image", "images"):
                    if hint in parts_lower:
                        inferred_type = hint.rstrip("s") if hint in ("emotions", "images") else hint
                        break
                _store_member(inferred_type, Path(safe_rel).stem, info.filename, f"embeded://{safe_rel}", ext)

            # Auto-import any embedded RisuAI module (`*.risum` or `module.json`) inside the CHARX archive.
            for info in zf.infolist():
                if info.is_dir() or info.filename == card_member:
                    continue
                safe_rel = sanitize_rel_zip_path(info.filename)
                if not safe_rel:
                    continue
                low_name = Path(safe_rel).name.lower()
                if not (low_name.endswith(".risum") or low_name == "module.json"):
                    continue
                try:
                    mod_bytes = _read_bounded_member_bytes(zf, info.filename, MAX_CARD_JSON_BYTES if low_name == "module.json" else MAX_CHARX_SINGLE_ASSET_BYTES)
                    mod_dict, mod_blobs = parse_risum_bytes(mod_bytes)
                except (CharxError, RisumError):
                    continue

                ext_target = data_block.setdefault("extensions", {})
                if not isinstance(ext_target, dict):
                    ext_target = {}
                    data_block["extensions"] = ext_target
                risu_target = ext_target.setdefault("risuai", {})
                if not isinstance(risu_target, dict):
                    risu_target = {}
                    ext_target["risuai"] = risu_target

                mod_regex = mod_dict.get("regex")
                if isinstance(mod_regex, list) and mod_regex:
                    existing_scripts = risu_target.setdefault("customScripts", [])
                    if isinstance(existing_scripts, list):
                        existing_scripts.extend(mod_regex)

                mod_bg = mod_dict.get("backgroundEmbedding")
                if isinstance(mod_bg, str) and mod_bg.strip():
                    prev_bg = str(risu_target.get("backgroundHTML") or "")
                    if mod_bg.strip() not in prev_bg:
                        risu_target["backgroundHTML"] = f"{prev_bg}\n{mod_bg}".strip() if prev_bg else mod_bg

                mod_vars = mod_dict.get("defaultVariables")
                if isinstance(mod_vars, str) and mod_vars.strip():
                    prev_vars = str(risu_target.get("defaultVariables") or "")
                    risu_target["defaultVariables"] = f"{prev_vars}\n{mod_vars}".strip() if prev_vars else mod_vars

                raw_mod_assets = mod_dict.get("assets") if isinstance(mod_dict.get("assets"), list) else []
                existing_asset_names = {str(a.get("name") or "").lower() for a in extracted_assets}
                for m_idx, blob in enumerate(mod_blobs):
                    if len(extracted_assets) >= MAX_CHARX_ASSETS_COUNT or not blob:
                        break
                    if len(blob) > remaining_budget:
                        raise CharxError("CHARX extracted size exceeds safety limit")
                    meta = raw_mod_assets[m_idx] if m_idx < len(raw_mod_assets) and isinstance(raw_mod_assets[m_idx], (list, tuple)) else None
                    m_name = str(meta[0] if meta and len(meta) >= 1 and meta[0] else f"module_asset_{m_idx}").strip()
                    if m_name.lower() in existing_asset_names:
                        continue
                    existing_asset_names.add(m_name.lower())
                    m_ext = str(meta[2] if meta and len(meta) >= 3 and meta[2] else "").strip().lstrip(".").lower() or infer_ext(m_name, "bin")
                    disk_path = assets_root / "x-risu-asset" / f"{len(extracted_assets):04d}_{_safe_filename_segment(m_name)}.{m_ext}"
                    disk_path.parent.mkdir(parents=True, exist_ok=True)
                    disk_path.write_bytes(blob)
                    remaining_budget -= len(blob)
                    rel_path = disk_path.resolve().relative_to(data_root_resolved).as_posix()
                    extracted_assets.append({
                        "asset_type": classify_media_kind(m_ext, "x-risu-asset"),
                        "name": m_name,
                        "ext": m_ext,
                        "mime_type": infer_mime(m_ext),
                        "rel_path": rel_path,
                        "source_uri": f"embeded://assets/risum/{disk_path.name}",
                        "size_bytes": len(blob),
                        "sort_order": len(extracted_assets),
                    })

        # Polyglot charx-jpeg / PNG+ZIP fallback if no icon/main image was inside the ZIP.
        avatar_rel = _pick_avatar_rel_path(extracted_assets)
        if not avatar_rel:
            poly = _extract_polyglot_header_image(charx_path, assets_root / "icon")
            if poly:
                poly_path, poly_ext, poly_size = poly
                rel_path = poly_path.resolve().relative_to(data_root_resolved).as_posix()
                extracted_assets.insert(0, {
                    "asset_type": "icon",
                    "name": "main",
                    "ext": poly_ext,
                    "mime_type": infer_mime(poly_ext),
                    "rel_path": rel_path,
                    "source_uri": "",
                    "size_bytes": poly_size,
                    "sort_order": 0,
                })
                avatar_rel = rel_path

        return raw_card, extracted_assets, avatar_rel

    except zipfile.BadZipFile as exc:
        shutil.rmtree(assets_root, ignore_errors=True)
        raise CharxError(f"Not a valid CHARX/ZIP file: {exc}") from exc
    except Exception:
        shutil.rmtree(assets_root, ignore_errors=True)
        raise


def _resolve_local_file(rel_or_abs: str) -> Optional[Path]:
    """Resolve a stored relative path (uploads/...) against DATA_ROOT."""
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p if p.is_file() else None
    candidate = (paths.DATA_ROOT / rel_or_abs).resolve()
    if candidate.is_file():
        return candidate
    return None


def build_charx_archive(
    companion_data: Dict,
    assets: List[Dict],
    card_payload_builder: Callable[[Dict], Dict],
    dest_path: Path,
) -> Path:
    """Stream-build a standard CCv3 `.charx` ZIP file at *dest_path*."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    card_obj = card_payload_builder(companion_data)
    data_block = card_obj.setdefault("data", {})
    card_obj["spec"] = "chara_card_v3"
    card_obj["spec_version"] = "3.0"

    manifest: List[Dict] = []
    files_to_pack: List[Tuple[Path, str, int]] = []  # (disk_path, arcname, compress_type)
    used_arcnames: Set[str] = set()

    def _add_file(disk_path: Path, asset_type: str, name: str, ext: str) -> None:
        ext_l = (ext or infer_ext(disk_path.name, "bin")).lower().lstrip(".")
        type_seg = _safe_filename_segment(asset_type or "other", "other")
        stem = _safe_filename_segment(name or disk_path.stem, "asset")
        arcname = f"assets/{type_seg}/{stem}.{ext_l}"
        counter = 1
        while arcname in used_arcnames:
            arcname = f"assets/{type_seg}/{stem}_{counter}.{ext_l}"
            counter += 1
        used_arcnames.add(arcname)

        compress = zipfile.ZIP_STORED if ext_l in COMPRESSED_MEDIA_EXTS else zipfile.ZIP_DEFLATED
        files_to_pack.append((disk_path, arcname, compress))
        manifest.append({
            "type": asset_type or "other",
            "name": name or disk_path.stem,
            "uri": f"embeded://{arcname}",
            "ext": ext_l,
        })

    avatar_rel = str(companion_data.get("avatar_path") or "")
    has_main_icon = False

    for asset in assets:
        disk_path = _resolve_local_file(str(asset.get("rel_path") or ""))
        if not disk_path:
            continue
        a_type = str(asset.get("asset_type") or "other")
        a_name = str(asset.get("name") or disk_path.stem)
        a_ext = str(asset.get("ext") or infer_ext(disk_path.name, "bin"))
        if a_type == "icon" and a_name.lower() == "main":
            has_main_icon = True
        if avatar_rel and str(asset.get("rel_path") or "") == avatar_rel and a_type == "icon":
            has_main_icon = True
        _add_file(disk_path, a_type, a_name, a_ext)

    if avatar_rel and not has_main_icon:
        avatar_file = _resolve_local_file(avatar_rel)
        if avatar_file:
            _add_file(avatar_file, "icon", "main", infer_ext(avatar_file.name, "png"))

    data_block["assets"] = manifest

    try:
        with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as zf:
            card_json = json.dumps(card_obj, ensure_ascii=False, indent=2).encode("utf-8")
            zf.writestr("card.json", card_json, compress_type=zipfile.ZIP_DEFLATED)
            for disk_path, arcname, compress in files_to_pack:
                zf.write(disk_path, arcname=arcname, compress_type=compress)
        os.replace(tmp_path, dest_path)
        return dest_path
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

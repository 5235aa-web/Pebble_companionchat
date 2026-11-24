"""
attachment_parser.py - Turn uploaded files into chat-ready payloads.

Images are stored under uploads/attachments and described by a static URL plus
their pixel size. Videos are stored under uploads/attachments along with a
poster frame, sampled keyframes, optional duration/resolution, and optional
audio transcript (extracted on the frontend or via local FFmpeg fallback).
Documents are parsed locally into plain text that gets folded into the user
message, so the model receives the content instead of a filename.

Every format is handled with what is already installed: python-docx, openpyxl,
pypdf and Pillow. Powerpoint has no local dependency, so the OOXML container is
read directly with zipfile and the slide text is pulled out of the XML.
"""
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hard limits. A document longer than this is truncated rather than rejected,
# because a long contract is still useful up to the point it gets cut.
MAX_TEXT_CHARS = 20000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_FRAMES = 180
MAX_VIDEO_AUDIO_SECONDS = 180
# Spreadsheet cells can explode combinatorially on a wide sheet.
MAX_SHEET_ROWS = 500
MAX_SHEET_COLS = 40

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
VIDEO_EXTS = {"mp4", "m4v", "webm", "mov", "mkv", "avi", "flv", "wmv", "ts"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "log", "yaml", "yml", "xml", "html", "py", "js", "ts", "java", "c", "cpp", "h", "rs", "go", "sql"}
DOC_EXTS = {"docx", "xlsx", "xlsm", "pptx", "pdf"} | TEXT_EXTS

MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "flv": "video/x-flv",
    "wmv": "video/x-ms-wmv",
    "ts": "video/mp2t",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

from .paths import (
    ATTACHMENT_DIR,
    UPLOAD_ROOT,
    ffmpeg_path,
    invalidate_ffmpeg_cache,
    is_ffmpeg_usable,
    load_app_config,
)


class AttachmentError(ValueError):
    """Raised when an uploaded file cannot be used as an attachment."""


class FfmpegFallbackError(AttachmentError):
    """Raised when FFmpeg is absent/unusable and browser frames were not supplied."""


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def classify(filename: str) -> Optional[str]:
    """Return 'image', 'video', 'document', or None when the type is not supported."""
    ext = _ext(filename)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in DOC_EXTS:
        return "document"
    return None


def supported_extensions() -> Dict[str, Any]:
    return {
        "image": sorted(IMAGE_EXTS),
        "video": sorted(VIDEO_EXTS),
        "document": sorted(DOC_EXTS),
        "ffmpegAvailable": is_ffmpeg_usable(),
        "videoAutoStt": bool(load_app_config().get("videoAutoStt", False)),
    }


# ==================== Image ====================

def _image_size(data: bytes) -> tuple:
    """Read the pixel size, validating the bytes really are an image."""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - Pillow is a declared dependency
        logger.warning("Pillow unavailable, image size will be unknown: %s", e)
        return 0, 0

    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception as e:
        raise AttachmentError(f"无法识别的图片文件: {e}") from e


def save_attachment_bytes(ext: str, data: bytes, prefix: str = "") -> str:
    """Write attachment bytes under uploads/attachments and return the static URL."""
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    clean_ext = (ext or "bin").lower().lstrip(".") or "bin"
    stored_name = f"{prefix}{uuid.uuid4().hex}.{clean_ext}"
    target = ATTACHMENT_DIR / stored_name
    target.write_bytes(data)
    return f"/uploads/attachments/{stored_name}"


def save_image(filename: str, data: bytes) -> str:
    """Write the image under uploads/attachments and return its static URL."""
    return save_attachment_bytes(_ext(filename) or "png", data)


def resolve_image_path(url: str) -> Optional[Path]:
    """
    Map a stored attachment URL back to a file on disk.

    Only files inside ATTACHMENT_DIR are accepted, so a crafted URL cannot read
    outside the uploads folder.
    """
    if not url or not url.startswith("/uploads/attachments/"):
        return None

    name = url[len("/uploads/attachments/"):]
    if not name or "/" in name or "\\" in name or ".." in name:
        return None

    candidate = (ATTACHMENT_DIR / name).resolve()
    try:
        candidate.relative_to(ATTACHMENT_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def delete_image(url: str) -> bool:
    """
    Delete a stored attachment file from disk.

    Returns True only when a file was actually removed. A missing file is not an
    error: the caller's goal is "this file no longer exists", and a repeated
    delete must stay idempotent.
    """
    path = resolve_image_path(url)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        logger.warning(f"Failed to delete attachment {url}: {e}")
        return False


def delete_video_assets(attachment: Dict) -> int:
    """Delete derived poster and keyframe files owned by a video attachment."""
    urls: List[str] = []
    poster = str(attachment.get("poster_url") or "")
    if poster:
        urls.append(poster)
    for frame in attachment.get("frames") or []:
        if isinstance(frame, dict) and frame.get("url"):
            urls.append(str(frame["url"]))
    removed = 0
    for u in dict.fromkeys(urls):
        if delete_image(u):
            removed += 1
    return removed


# ==================== Office documents ====================

def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as e:
        raise AttachmentError("服务端缺少 python-docx，无法解析 .docx") from e

    document = docx.Document(io.BytesIO(data))
    blocks: List[str] = []

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if text:
            blocks.append(text)

    # Tables carry most of the meaning in exported documents, keep them as
    # pipe-separated rows rather than dropping them.
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or "").strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))

    return "\n".join(blocks)


def _extract_xlsx(data: bytes) -> str:
    try:
        import openpyxl
    except ImportError as e:
        raise AttachmentError("服务端缺少 openpyxl，无法解析 .xlsx") from e

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    blocks: List[str] = []
    try:
        for sheet in workbook.worksheets:
            blocks.append(f"[Sheet: {sheet.title}]")
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                if row_index >= MAX_SHEET_ROWS:
                    blocks.append("... (表格行数过多，已截断)")
                    break
                cells = ["" if v is None else str(v) for v in row[:MAX_SHEET_COLS]]
                if any(c.strip() for c in cells):
                    blocks.append(" | ".join(cells))
    finally:
        workbook.close()

    return "\n".join(blocks)


_PPT_TEXT_PATTERN = re.compile(r"<a:t>(.*?)</a:t>", re.DOTALL)


def _extract_pptx(data: bytes) -> str:
    """
    Pull slide text straight out of the OOXML container.

    python-pptx is not installed, and every slide's text lives in <a:t> tags
    inside ppt/slides/slideN.xml, so the archive is read directly.
    """
    blocks: List[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            slide_names = sorted(
                (n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", Path(n).stem).group(1)),
            )
            for index, name in enumerate(slide_names, start=1):
                xml = archive.read(name).decode("utf-8", errors="ignore")
                texts = [t.strip() for t in _PPT_TEXT_PATTERN.findall(xml)]
                texts = [t for t in texts if t]
                if texts:
                    blocks.append(f"[Slide {index}]")
                    blocks.extend(texts)
    except zipfile.BadZipFile as e:
        raise AttachmentError("无法解析 .pptx 文件，文件可能已损坏") from e

    return "\n".join(blocks)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise AttachmentError("服务端缺少 pypdf，无法解析 .pdf") from e

    reader = PdfReader(io.BytesIO(data))
    blocks: List[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:
            logger.warning("Failed to extract page %s: %s", index, e)
            continue
        if text:
            blocks.append(f"[Page {index}]")
            blocks.append(text)

    return "\n".join(blocks)


def _extract_plain_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_document_text(filename: str, data: bytes) -> str:
    """Extract plain text from a supported document."""
    ext = _ext(filename)

    if ext == "docx":
        return _extract_docx(data)
    if ext in ("xlsx", "xlsm"):
        return _extract_xlsx(data)
    if ext == "pptx":
        return _extract_pptx(data)
    if ext == "pdf":
        return _extract_pdf(data)
    if ext == "json":
        try:
            return json.dumps(json.loads(_extract_plain_text(data)), ensure_ascii=False, indent=2)
        except Exception:
            return _extract_plain_text(data)
    if ext in TEXT_EXTS:
        return _extract_plain_text(data)

    raise AttachmentError(f"不支持的文件类型: .{ext}")


# ==================== Video ====================

_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", re.IGNORECASE | re.DOTALL)
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RES_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})(?:[\s,\[])")


def _subprocess_kwargs() -> Dict:
    kwargs: Dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def probe_video_ffmpeg(ff: Path, video_path: Path) -> Tuple[float, int, int]:
    """Read (duration_sec, width, height) from `ffmpeg -i` stderr output."""
    try:
        proc = subprocess.run(
            [str(ff), "-hide_banner", "-i", str(video_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15,
            **_subprocess_kwargs(),
        )
    except Exception as e:
        logger.warning("ffmpeg probe failed for %s: %s", video_path, e)
        return 0.0, 0, 0

    stderr = proc.stderr or ""
    duration = 0.0
    dm = _DURATION_RE.search(stderr)
    if dm:
        hours, mins, secs = int(dm.group(1)), int(dm.group(2)), float(dm.group(3))
        duration = round(hours * 3600 + mins * 60 + secs, 2)

    width, height = 0, 0
    rm = _VIDEO_RES_RE.search(stderr)
    if rm:
        width, height = int(rm.group(1)), int(rm.group(2))
    return duration, width, height


def _sample_timestamps(duration: float, max_frames: int = MAX_VIDEO_FRAMES) -> List[float]:
    """Pick dense sequential timestamps for continuous video understanding."""
    if duration <= 0.4:
        return [0.0]
    if duration <= 10.0:
        target = int(round(duration * 4.0))
    elif duration <= 80.0:
        target = 40 + int(round((duration - 10.0) * 2.0))
    else:
        target = max_frames
    count = max(2, min(max_frames, target))
    margin = min(0.15, duration * 0.02)
    start = margin
    end = max(start, duration - margin)
    if count == 1 or end <= start:
        return [round(start, 2)]
    step = (end - start) / (count - 1)
    return [round(start + i * step, 2) for i in range(count)]


def _extract_frames_seek(
    ff: Path,
    video_path: Path,
    timestamps: List[float],
    tmp_root: Path,
) -> List[Dict]:
    """Fallback per-timestamp seeking when single-pass fps extraction fails."""
    frames: List[Dict] = []
    for idx, ts in enumerate(timestamps):
        out_file = tmp_root / f"seek_{idx:04d}.jpg"
        cmd = [
            str(ff),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{ts:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(768,iw)':-2",
            "-q:v",
            "4",
            str(out_file),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=20, **_subprocess_kwargs())
        except Exception as e:
            logger.warning("ffmpeg frame extract failed at %.2fs: %s", ts, e)
            continue
        if out_file.is_file() and out_file.stat().st_size > 0:
            url = save_attachment_bytes("jpg", out_file.read_bytes(), prefix="vf_")
            frames.append({"time": ts, "url": url})
    return frames


def extract_frames_ffmpeg(
    ff: Path,
    video_path: Path,
    duration: float,
    max_frames: int = MAX_VIDEO_FRAMES,
) -> List[Dict]:
    """Extract dense sequential JPEG frames from `video_path` in one FFmpeg pass."""
    timestamps = _sample_timestamps(duration, max_frames=max_frames)
    target_count = len(timestamps)
    with tempfile.TemporaryDirectory(prefix="pebble_vframes_") as tmpdir:
        tmp_root = Path(tmpdir)
        if duration > 0 and target_count > 1:
            fps_val = max(0.1, min(8.0, target_count / max(duration, 0.1)))
            cmd = [
                str(ff),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                f"fps={fps_val:.6f},scale='min(768,iw)':-2",
                "-frames:v",
                str(target_count),
                "-q:v",
                "4",
                str(tmp_root / "frame_%04d.jpg"),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60, **_subprocess_kwargs())
            except Exception as e:
                logger.warning("ffmpeg single-pass stream extract failed for %s: %s", video_path, e)

            emitted = sorted(
                (p for p in tmp_root.glob("frame_*.jpg") if p.is_file() and p.stat().st_size > 0)
            )[:max_frames]
            if emitted:
                total = len(emitted)
                step = duration / total
                frames: List[Dict] = []
                for idx, path in enumerate(emitted):
                    ts = round(min(duration, (idx + 0.5) * step), 2)
                    url = save_attachment_bytes("jpg", path.read_bytes(), prefix="vf_")
                    frames.append({"time": ts, "url": url})
                return frames

        return _extract_frames_seek(ff, video_path, timestamps, tmp_root)


def extract_audio_wav_ffmpeg(ff: Path, video_path: Path) -> Optional[bytes]:
    """Extract up to MAX_VIDEO_AUDIO_SECONDS of 16 kHz mono WAV audio via FFmpeg."""
    with tempfile.TemporaryDirectory(prefix="pebble_vaudio_") as tmpdir:
        out_wav = Path(tmpdir) / "audio.wav"
        cmd = [
            str(ff),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-t",
            str(MAX_VIDEO_AUDIO_SECONDS),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(out_wav),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, **_subprocess_kwargs())
        except Exception as e:
            logger.warning("ffmpeg audio extract failed for %s: %s", video_path, e)
            return None
        if out_wav.is_file() and out_wav.stat().st_size > 44:
            return out_wav.read_bytes()
    return None


def _parse_frontend_frames(frames_json: str) -> List[Dict]:
    """Decode and persist frontend-sampled data URLs as JPEG/WebP attachments."""
    if not frames_json:
        return []
    try:
        items = json.loads(frames_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []

    saved: List[Dict] = []
    for item in items[:MAX_VIDEO_FRAMES]:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("data_url") or "").strip()
        m = _DATA_URL_RE.match(raw_url)
        if not m:
            continue
        ext = "jpg" if m.group(1).lower() in ("jpg", "jpeg") else m.group(1).lower()
        try:
            raw_bytes = base64.b64decode(m.group(2), validate=True)
        except Exception:
            continue
        if not raw_bytes or len(raw_bytes) > MAX_IMAGE_BYTES:
            continue
        try:
            _image_size(raw_bytes)
        except AttachmentError:
            continue
        ts = round(float(item.get("time") or 0.0), 2)
        url = save_attachment_bytes(ext, raw_bytes, prefix="vf_")
        saved.append({"time": ts, "url": url})
    return saved


def _format_ts(seconds: float, *, subsecond: bool = False) -> str:
    sec_val = max(0.0, float(seconds or 0.0))
    if subsecond:
        whole = int(sec_val)
        tenths = int(round((sec_val - whole) * 10))
        if tenths >= 10:
            whole += 1
            tenths = 0
        m, s = divmod(whole, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}.{tenths}"
        return f"{m:02d}:{s:02d}.{tenths}"
    sec = max(0, int(round(sec_val)))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_video_attachment(
    filename: str,
    data: bytes,
    *,
    frames_json: str = "",
    duration: float = 0.0,
    width: int = 0,
    height: int = 0,
    wav_bytes: Optional[bytes] = None,
    transcript: str = "",
    enable_stt: bool = False,
    stt_fn: Optional[Callable[[bytes], str]] = None,
) -> Dict:
    """Persist a video upload plus sampled frames and optional audio transcript."""
    if len(data) > MAX_VIDEO_BYTES:
        raise AttachmentError(f"视频超过 {MAX_VIDEO_BYTES // (1024 * 1024)}MB 限制")

    ext = _ext(filename) or "mp4"
    mime = MIME_BY_EXT.get(ext, "video/mp4")
    video_url = save_attachment_bytes(ext, data)
    video_path = resolve_image_path(video_url)

    frames = _parse_frontend_frames(frames_json)
    dur = max(0.0, float(duration or 0.0))
    w = max(0, int(width or 0))
    h = max(0, int(height or 0))

    ff: Optional[Path] = ffmpeg_path() if is_ffmpeg_usable() else None
    needs_ffmpeg_frames = len(frames) == 0
    needs_ffmpeg_audio = bool(enable_stt) and not (transcript or "").strip() and not wav_bytes

    if ff is not None and video_path is not None and (needs_ffmpeg_frames or needs_ffmpeg_audio or dur <= 0):
        if dur <= 0 or w <= 0 or h <= 0:
            p_dur, p_w, p_h = probe_video_ffmpeg(ff, video_path)
            if dur <= 0:
                dur = p_dur
            if w <= 0:
                w = p_w
            if h <= 0:
                h = p_h
        if needs_ffmpeg_frames:
            frames = extract_frames_ffmpeg(ff, video_path, dur)
        if needs_ffmpeg_audio:
            wav_bytes = extract_audio_wav_ffmpeg(ff, video_path)

    if not frames:
        delete_image(video_url)
        if ff is not None:
            invalidate_ffmpeg_cache()
        if not frames_json:
            raise FfmpegFallbackError(
                "FFmpeg 不可用或未能从视频抽帧，需要降级到前端抽帧。"
            )
        if ff is None:
            raise AttachmentError(
                "浏览器无法解码该视频格式，且本机未检测到可用 FFmpeg。请转为 MP4(H.264) 或在「后台设置 -> 路径」中配置 ffmpeg.exe。"
            )
        raise AttachmentError("未能从视频中抽取到有效画面，文件可能已损坏或格式不受支持。")

    final_transcript = (transcript or "").strip() if enable_stt else ""
    if enable_stt and not final_transcript and wav_bytes and stt_fn is not None:
        try:
            final_transcript = (stt_fn(wav_bytes) or "").strip()
        except Exception as e:
            logger.info("Video audio transcription skipped: %s", e)

    poster_url = frames[0]["url"] if frames else ""

    return {
        "id": uuid.uuid4().hex,
        "kind": "video",
        "name": Path(filename).name,
        "mime": mime,
        "size": len(data),
        "url": video_url,
        "poster_url": poster_url,
        "width": w,
        "height": h,
        "duration": round(dur, 2),
        "frames": frames,
        "transcript": final_transcript,
        "stt_enabled": bool(enable_stt),
    }


def transcribe_stored_video(
    video_url: str,
    *,
    wav_bytes: Optional[bytes] = None,
    stt_fn: Optional[Callable[[bytes], str]] = None,
) -> str:
    """Run on-demand STT against an already uploaded video attachment."""
    video_path = resolve_image_path(video_url)
    if video_path is None:
        raise AttachmentError("视频文件不存在或已被移除")

    raw_wav = wav_bytes
    if not raw_wav:
        ff = ffmpeg_path() if is_ffmpeg_usable() else None
        if ff is None:
            raise FfmpegFallbackError("本机未检测到可用 FFmpeg，需由前端提取音轨")
        raw_wav = extract_audio_wav_ffmpeg(ff, video_path)
        if not raw_wav:
            return ""

    if stt_fn is None:
        return ""
    return (stt_fn(raw_wav) or "").strip()


def format_video_for_prompt(item: Dict) -> str:
    """Render structured video metadata, frame timeline, and transcript for the LLM."""
    name = item.get("name") or "video"
    meta_parts: List[str] = []
    dur = float(item.get("duration") or 0.0)
    if dur > 0:
        meta_parts.append(f"时长 {dur:.1f}s")
    w = int(item.get("width") or 0)
    h = int(item.get("height") or 0)
    if w > 0 and h > 0:
        meta_parts.append(f"{w}x{h}")
    header = f"[视频附件: {name}" + (f" ({', '.join(meta_parts)})" if meta_parts else "") + "]"

    lines = [header]
    frames = [f for f in (item.get("frames") or []) if isinstance(f, dict) and f.get("url")]
    if frames:
        raw_times = [float(f.get("time") or 0.0) for f in frames]
        has_subsecond_gap = any((b - a) < 0.95 for a, b in zip(raw_times, raw_times[1:]))
        if len(frames) <= 12:
            ts_list = ", ".join(_format_ts(t, subsecond=has_subsecond_gap) for t in raw_times)
            lines.append(
                f"已按时间顺序连续抽取 {len(frames)} 帧视频画面（时间点: {ts_list}）。"
                "请将随后按顺序排列的画面视为连续视频片段来理解动作与变化。"
            )
        else:
            start_ts = _format_ts(raw_times[0], subsecond=has_subsecond_gap)
            end_ts = _format_ts(raw_times[-1], subsecond=has_subsecond_gap)
            span = max(0.01, raw_times[-1] - raw_times[0])
            avg_step = span / max(1, len(frames) - 1)
            lines.append(
                f"已按时间顺序连续采样 {len(frames)} 帧视频画面流"
                f"（时间范围 {start_ts} ~ {end_ts}，约每 {avg_step:.2f}s 一帧）。"
                "请将随后按顺序排列的全部画面视为连续播放的动态视频流来理解动作、神态与场景变化，"
                "不要将其拆解为彼此孤立的静态图片。"
            )

    transcript = str(item.get("transcript") or "").strip()
    if transcript:
        lines.append(f"[视频语音转录]: {transcript}")
    else:
        lines.append("（未附带音频转录，请仅依据画面内容回应，不要猜测视频中的对白或旁白。）")

    return "\n".join(lines)


# ==================== Entry point ====================

def build_attachment(
    filename: str,
    data: bytes,
    *,
    frames_json: str = "",
    duration: float = 0.0,
    width: int = 0,
    height: int = 0,
    wav_bytes: Optional[bytes] = None,
    transcript: str = "",
    enable_stt: bool = False,
    stt_fn: Optional[Callable[[bytes], str]] = None,
) -> Dict:
    """
    Turn one uploaded file into the descriptor the chat request carries.

    Images come back with a URL and pixel size; videos come back with a URL,
    poster/keyframe URLs, metadata, and optional transcript; documents come back
    with the extracted text already truncated to MAX_TEXT_CHARS.
    """
    if not data:
        raise AttachmentError("文件内容为空")

    kind = classify(filename)
    if kind is None:
        raise AttachmentError(f"不支持的文件类型: {filename}")

    ext = _ext(filename)
    mime = MIME_BY_EXT.get(ext, "application/octet-stream")

    if kind == "image":
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 限制")
        w, h = _image_size(data)
        url = save_image(filename, data)
        return {
            "id": uuid.uuid4().hex,
            "kind": "image",
            "name": Path(filename).name,
            "mime": mime,
            "size": len(data),
            "url": url,
            "width": w,
            "height": h,
        }

    if kind == "video":
        return build_video_attachment(
            filename,
            data,
            frames_json=frames_json,
            duration=duration,
            width=width,
            height=height,
            wav_bytes=wav_bytes,
            transcript=transcript,
            enable_stt=enable_stt,
            stt_fn=stt_fn,
        )

    if len(data) > MAX_DOC_BYTES:
        raise AttachmentError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)}MB 限制")

    text = (extract_document_text(filename, data) or "").strip()
    if not text:
        raise AttachmentError("未能从文档中提取到文字内容")

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    return {
        "id": uuid.uuid4().hex,
        "kind": "document",
        "name": Path(filename).name,
        "mime": mime,
        "size": len(data),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def format_documents_for_prompt(attachments: List[Dict]) -> str:
    """
    Render document attachments as a block for the live request.

    The text is injected into the current turn only. The stored message keeps a
    placeholder, so the same document is not re-sent on every later turn.
    """
    blocks: List[str] = []
    for item in attachments or []:
        if item.get("kind") != "document":
            continue
        name = item.get("name") or "document"
        text = item.get("text") or ""
        suffix = "\n...(内容过长，已截断)" if item.get("truncated") else ""
        blocks.append(f"[附件文档: {name}]\n{text}{suffix}")

    return "\n\n".join(blocks)

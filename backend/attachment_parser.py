"""
attachment_parser.py - Turn uploaded files into chat-ready payloads.

Images are stored under uploads/attachments and described by a static URL plus
their pixel size. Documents are parsed locally into plain text that gets folded
into the user message, so the model receives the content instead of a filename.

Every format is handled with what is already installed: python-docx, openpyxl,
pypdf and Pillow. Powerpoint has no local dependency, so the OOXML container is
read directly with zipfile and the slide text is pulled out of the XML.
"""
import io
import json
import logging
import re
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Hard limits. A document longer than this is truncated rather than rejected,
# because a long contract is still useful up to the point it gets cut.
MAX_TEXT_CHARS = 20000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES = 20 * 1024 * 1024
# Spreadsheet cells can explode combinatorially on a wide sheet.
MAX_SHEET_ROWS = 500
MAX_SHEET_COLS = 40

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "log", "yaml", "yml", "xml", "html", "py", "js", "ts", "java", "c", "cpp", "h", "rs", "go", "sql"}
DOC_EXTS = {"docx", "xlsx", "xlsm", "pptx", "pdf"} | TEXT_EXTS

MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

from .paths import ATTACHMENT_DIR, UPLOAD_ROOT


class AttachmentError(ValueError):
    """Raised when an uploaded file cannot be used as an attachment."""


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def classify(filename: str) -> Optional[str]:
    """Return 'image', 'document', or None when the type is not supported."""
    ext = _ext(filename)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOC_EXTS:
        return "document"
    return None


def supported_extensions() -> Dict[str, List[str]]:
    return {"image": sorted(IMAGE_EXTS), "document": sorted(DOC_EXTS)}


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


def save_image(filename: str, data: bytes) -> str:
    """Write the image under uploads/attachments and return its static URL."""
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    ext = _ext(filename) or "png"
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    target = ATTACHMENT_DIR / stored_name
    target.write_bytes(data)
    return f"/uploads/attachments/{stored_name}"


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
    Delete a stored image from disk.

    Returns True only when a file was actually removed. A missing file is not an
    error: the caller's goal is "this image no longer exists", and a repeated
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


# ==================== Entry point ====================

def build_attachment(filename: str, data: bytes) -> Dict:
    """
    Turn one uploaded file into the descriptor the chat request carries.

    Images come back with a URL and pixel size; documents come back with the
    extracted text already truncated to MAX_TEXT_CHARS.
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
        width, height = _image_size(data)
        url = save_image(filename, data)
        return {
            "id": uuid.uuid4().hex,
            "kind": "image",
            "name": Path(filename).name,
            "mime": mime,
            "size": len(data),
            "url": url,
            "width": width,
            "height": height,
        }

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

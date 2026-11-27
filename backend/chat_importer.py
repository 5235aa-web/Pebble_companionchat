"""Import SillyTavern / Pebble JSONL conversations into a new companion branch."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .companion_db import create_imported_branch, get_companion

MAX_JSONL_BYTES = 50 * 1024 * 1024

_AT_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*@\s*(\d{1,2})h\s*(\d{1,2})m(?:\s*(\d{1,2})s)?",
    re.IGNORECASE,
)
_FILENAME_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%B %d, %Y %I:%M%p",
    "%B %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M:%S%p",
    "%B %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M%p",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y %I:%M:%S%p",
    "%b %d, %Y %I:%M:%S %p",
)


def _parse_send_date(raw: Any) -> Optional[str]:
    """Normalize SillyTavern or ISO timestamps into SQLite UTC/local YYYY-MM-DD HH:MM:SS."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            return None
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return None

    text = str(raw).strip()
    if not text:
        return None

    # SillyTavern "@Xh Ym Zs" timestamps, e.g. "2024-11-26 @15h 15m 10s 123ms"
    m_at = _AT_DATE_RE.match(text)
    if m_at:
        date_part, hh, mm, ss = m_at.group(1), int(m_at.group(2)), int(m_at.group(3)), int(m_at.group(4) or 0)
        try:
            dt = datetime.strptime(f"{date_part} {hh:02d}:{mm:02d}:{ss:02d}", "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    # ISO 8601 (handles Z / timezone offsets)
    iso_cand = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_cand)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    # Clean common ordinal suffixes like "November 26th, 2024" -> "November 26, 2024"
    normalized = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    return None


def _is_header_record(obj: Dict[str, Any]) -> bool:
    """Detect SillyTavern JSONL metadata header on the first record."""
    if isinstance(obj.get("mes"), str) or isinstance(obj.get("swipes"), list):
        return False
    return any(
        k in obj
        for k in ("user_name", "character_name", "create_date", "chat_metadata", "chat_id")
    )


def _flatten_variables(raw_vars: Any) -> Dict[str, str]:
    """Convert ST chat_metadata.variables into flat string key/value pairs for card_vars."""
    if not isinstance(raw_vars, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw_vars.items():
        key = str(k).strip()
        if not key or v is None:
            continue
        if isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        else:
            out[key] = str(v)
    return out


def _derive_branch_name(branch_name: Optional[str], filename: Optional[str]) -> str:
    explicit = (branch_name or "").strip()
    if explicit:
        return explicit[:80]
    if filename:
        stem = Path(filename.strip()).stem
        cleaned = _FILENAME_INVALID_RE.sub(" ", stem)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        if cleaned:
            return cleaned[:60]
    return f"导入对话 {datetime.now().strftime('%m-%d %H:%M')}"


def _normalize_swipes(raw_mes: Any, raw_swipes: Any, raw_swipe_id: Any) -> Tuple[str, List[str], int]:
    """Align mes/swipes/swipe_id into consistent Pebble message fields."""
    mes_text = "" if raw_mes is None else str(raw_mes)

    swipes: List[str] = []
    if isinstance(raw_swipes, list):
        swipes = ["" if item is None else str(item) for item in raw_swipes]

    if not swipes:
        swipes = [mes_text]

    try:
        swipe_id = int(raw_swipe_id or 0)
    except (TypeError, ValueError):
        swipe_id = 0

    if swipe_id < 0 or swipe_id >= len(swipes):
        swipe_id = 0

    if mes_text != "":
        if swipes[swipe_id] != mes_text:
            swipes[swipe_id] = mes_text
        content = mes_text
    else:
        content = swipes[swipe_id]

    return content, swipes, swipe_id


def parse_jsonl_chat(
    jsonl_text: str,
    *,
    skip_system: bool = True,
) -> Dict[str, Any]:
    """Parse SillyTavern or Pebble-exported JSONL conversation text."""
    raw = (jsonl_text or "").lstrip("\ufeff")
    if not raw.strip():
        raise ValueError("JSONL content is empty")
    if len(raw.encode("utf-8")) > MAX_JSONL_BYTES:
        raise ValueError("JSONL file exceeds the 50 MB size limit")

    records: List[Dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at line {line_no}: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Line {line_no} is not a JSON object")
        records.append(parsed)

    if not records:
        raise ValueError("JSONL content has no valid records")

    header: Dict[str, Any] = {}
    msg_records = records
    if _is_header_record(records[0]):
        header = records[0]
        msg_records = records[1:]

    chat_meta = header.get("chat_metadata") if isinstance(header.get("chat_metadata"), dict) else {}
    variables = _flatten_variables(chat_meta.get("variables"))
    latest_msg_vars: Dict[str, str] = {}

    parsed_messages: List[Dict[str, Any]] = []
    skipped_system = 0
    skipped_empty = 0
    swipe_total = 0

    for row in msg_records:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        pebble_type = str(extra.get("pebble_message_type") or "").strip()
        is_system = bool(row.get("is_system")) or bool(extra.get("isSmallSys"))
        is_user = bool(row.get("is_user"))

        # Handle Pebble round-trip special records before ST system skip rule.
        if pebble_type == "compaction_summary":
            raw_mes = "" if row.get("mes") is None else str(row.get("mes"))
            if not raw_mes.strip():
                skipped_empty += 1
                continue
            created_at = _parse_send_date(row.get("send_date"))
            meta: Dict[str, Any] = {
                "summary": raw_mes,
                "messages_compacted": int(extra.get("messages_compacted") or 0),
                "imported_from": "jsonl",
            }
            if row.get("send_date"):
                meta["st_send_date"] = str(row.get("send_date"))
            parsed_messages.append(
                {
                    "role": "system",
                    "content": raw_mes,
                    "swipes": [raw_mes],
                    "swipe_id": 0,
                    "message_type": "compaction_summary",
                    "metadata": meta,
                    "is_important": False,
                    "is_compacted": False,
                    "created_at": created_at,
                }
            )
            continue

        if pebble_type == "narration":
            content, swipes, swipe_id = _normalize_swipes(
                row.get("mes"),
                row.get("swipes"),
                row.get("swipe_id"),
            )
            if not content and all(not s for s in swipes):
                skipped_empty += 1
                continue
            swipe_total += len(swipes)
            created_at = _parse_send_date(row.get("send_date"))
            meta = {"imported_from": "jsonl"}
            if row.get("send_date"):
                meta["st_send_date"] = str(row.get("send_date"))
            parsed_messages.append(
                {
                    "role": "user",
                    "content": content,
                    "swipes": swipes,
                    "swipe_id": swipe_id,
                    "message_type": "narration",
                    "metadata": meta,
                    "is_important": bool(extra.get("is_important")),
                    "is_compacted": bool(extra.get("is_compacted")),
                    "created_at": created_at,
                }
            )
            continue

        if is_system and skip_system:
            skipped_system += 1
            continue

        content, swipes, swipe_id = _normalize_swipes(
            row.get("mes"),
            row.get("swipes"),
            row.get("swipe_id"),
        )
        if not content and all(not s for s in swipes):
            skipped_empty += 1
            continue

        # Capture fallback per-message variables (used by some ST extensions/cards).
        row_vars = row.get("variables")
        if isinstance(row_vars, dict):
            latest_msg_vars = _flatten_variables(row_vars)
        elif isinstance(row_vars, list) and 0 <= swipe_id < len(row_vars) and isinstance(row_vars[swipe_id], dict):
            latest_msg_vars = _flatten_variables(row_vars[swipe_id])

        swipe_total += len(swipes)
        created_at = _parse_send_date(row.get("send_date"))
        meta = {"imported_from": "jsonl"}
        if pebble_type == "greeting":
            meta["is_greeting"] = True
        if row.get("name"):
            meta["st_name"] = str(row.get("name"))
        if row.get("send_date"):
            meta["st_send_date"] = str(row.get("send_date"))

        if is_system:
            # Only reached when skip_system=False: map to narration so frontend renders cleanly.
            role = "user"
            message_type = "narration"
        elif is_user:
            role = "user"
            message_type = "chat"
        else:
            role = "assistant"
            message_type = "chat"

        parsed_messages.append(
            {
                "role": role,
                "content": content,
                "swipes": swipes,
                "swipe_id": swipe_id,
                "message_type": message_type,
                "metadata": meta,
                "is_important": bool(extra.get("is_important")),
                "is_compacted": bool(extra.get("is_compacted")),
                "created_at": created_at,
            }
        )

    if not parsed_messages:
        raise ValueError("No importable chat messages found in JSONL")

    if not variables and latest_msg_vars:
        variables = latest_msg_vars

    return {
        "source_character_name": str(header.get("character_name") or "").strip(),
        "source_user_name": str(header.get("user_name") or "").strip(),
        "create_date": str(header.get("create_date") or "").strip(),
        "variables": variables,
        "messages": parsed_messages,
        "imported_count": len(parsed_messages),
        "skipped_system_count": skipped_system,
        "skipped_empty_count": skipped_empty,
        "swipe_total_count": swipe_total,
    }


def _has_character_mismatch(source_char: str, target_char: str) -> bool:
    src = source_char.strip().lower()
    dst = target_char.strip().lower()
    return bool(src and dst and src != dst)


def inspect_jsonl_chat(
    companion_id: str,
    jsonl_text: str,
    *,
    skip_system: bool = True,
) -> Dict[str, Any]:
    """Validate a JSONL chat payload and report metadata before branch creation."""
    companion = get_companion(companion_id)
    if not companion:
        raise LookupError(f"Companion not found: {companion_id}")

    parsed = parse_jsonl_chat(jsonl_text, skip_system=skip_system)
    target_name = str(companion.get("name") or companion_id)
    src_name = parsed["source_character_name"]

    return {
        "status": "ok",
        "companion_id": companion_id,
        "companion_name": target_name,
        "source_character_name": src_name,
        "source_user_name": parsed["source_user_name"],
        "character_mismatch": _has_character_mismatch(src_name, target_name),
        "message_count": parsed["imported_count"],
        "skipped_system_count": parsed["skipped_system_count"],
        "skipped_empty_count": parsed["skipped_empty_count"],
        "swipe_total_count": parsed["swipe_total_count"],
        "variables_count": len(parsed["variables"]),
    }


def import_jsonl_branch(
    companion_id: str,
    jsonl_text: str,
    *,
    branch_name: Optional[str] = None,
    filename: Optional[str] = None,
    skip_system: bool = True,
) -> Dict[str, Any]:
    """Import a JSONL conversation into a newly created branch for *companion_id*."""
    companion = get_companion(companion_id)
    if not companion:
        raise LookupError(f"Companion not found: {companion_id}")

    parsed = parse_jsonl_chat(jsonl_text, skip_system=skip_system)
    resolved_branch_name = _derive_branch_name(branch_name, filename)
    variables = parsed["variables"]

    branch_id = create_imported_branch(
        companion_id=companion_id,
        branch_name=resolved_branch_name,
        messages=parsed["messages"],
        variables=variables or None,
    )
    if not branch_id:
        raise RuntimeError("Failed to create imported conversation branch")

    target_name = str(companion.get("name") or companion_id)
    src_name = parsed["source_character_name"]

    return {
        "status": "success",
        "companion_id": companion_id,
        "companion_name": target_name,
        "branch_id": branch_id,
        "branch_name": resolved_branch_name,
        "source_character_name": src_name,
        "source_user_name": parsed["source_user_name"],
        "character_mismatch": _has_character_mismatch(src_name, target_name),
        "imported_count": parsed["imported_count"],
        "skipped_system_count": parsed["skipped_system_count"],
        "skipped_empty_count": parsed["skipped_empty_count"],
        "swipe_total_count": parsed["swipe_total_count"],
        "variables_imported": len(variables),
        "imported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }

"""
Picture Studio storage.

Rows live in the same SQLite file as the rest of the companion data, but in
their own table with their own lifecycle. Nothing here reads or writes the chat
message metadata, which is what keeps generated pictures and user attachments
from deleting each other.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from ..paths import DB_PATH

logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False

# Only these columns may be written by update_job. Building SQL from arbitrary
# keys would let a typo corrupt a row or, worse, inject a statement.
_WRITABLE = {
    "status",
    "source",
    "instruction",
    "prompt",
    "prompt_raw",
    "negative_prompt",
    "provider",
    "model",
    "seed",
    "url",
    "width",
    "height",
    "size",
    "error",
    "message_id",
    "session_id",
    "progress",
    "provider_job_id",
}

PUBLIC_COLUMNS = (
    "id, companion_id, branch_id, source, status, instruction, prompt, "
    "negative_prompt, provider, model, seed, url, width, height, size, error, "
    "message_id, session_id, progress, created_at"
)


def _has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='generated_pictures'"
    ).fetchone()
    return bool(row) and row[0] == 1


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS generated_pictures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL DEFAULT 'main',
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'pending',
            -- What the user typed. Kept apart from `prompt` so a retry feeds the
            -- original request back to the director, not its own last output.
            instruction TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            prompt_raw TEXT NOT NULL DEFAULT '',
            negative_prompt TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            seed INTEGER DEFAULT NULL,
            url TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            -- 0-99 while a local runner reports sampling steps; 100 once the
            -- picture is on disk. Cloud providers leave it at 0.
            progress INTEGER NOT NULL DEFAULT 0,
            -- Upstream task id (ComfyUI prompt_id). Needed to interrupt a job
            -- that is still running on the other side.
            provider_job_id TEXT NOT NULL DEFAULT '',
            message_id INTEGER DEFAULT NULL,
            session_id INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_genpic_branch
            ON generated_pictures(companion_id, branch_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_genpic_status
            ON generated_pictures(status);
        CREATE INDEX IF NOT EXISTS idx_genpic_created
            ON generated_pictures(created_at);
        """
    )
    # Additive migration: a table created before `instruction` existed gains it
    # in place, so an install that already ran a build keeps its rows.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(generated_pictures)")}
    if "instruction" not in columns:
        conn.execute(
            "ALTER TABLE generated_pictures ADD COLUMN instruction TEXT NOT NULL DEFAULT ''"
        )
    if "progress" not in columns:
        conn.execute(
            "ALTER TABLE generated_pictures ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
        )
    if "provider_job_id" not in columns:
        conn.execute(
            "ALTER TABLE generated_pictures ADD COLUMN provider_job_id TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    global _schema_ready
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _schema_ready or not _has_schema(conn):
        with _schema_lock:
            if not _schema_ready or not _has_schema(conn):
                _init_tables(conn)
                _schema_ready = True
    return conn


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def create_job(
    companion_id: str,
    branch_id: str,
    source: str,
    message_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO generated_pictures
               (companion_id, branch_id, source, status, message_id, session_id)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (companion_id, branch_id, source, message_id, session_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_job(picture_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in _WRITABLE}
    if not allowed:
        return
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    values = list(allowed.values()) + [picture_id]
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE generated_pictures SET {assignments}, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def update_progress(picture_id: int, percent: int) -> None:
    """Progress-only write, guarded to running rows.

    Called once per second at most (see service._progress_writer). The guard
    keeps a message that arrives after the job finished from touching the row.
    """
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE generated_pictures SET progress = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'running'",
            (max(0, min(int(percent), 100)), picture_id),
        )
        conn.commit()
    finally:
        conn.close()


def claim_cancel(picture_id: int) -> Optional[Dict[str, Any]]:
    """Flip a pending/running row to failed, or report that it was too late.

    The WHERE clause carries the race: a job that finished between the read and
    the write keeps its picture instead of being marked cancelled. Returns the
    previous row when the cancel won, None when it did not.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT provider, provider_job_id, status FROM generated_pictures WHERE id = ?",
            (picture_id,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE generated_pictures SET status = 'failed', error = ?, progress = 0, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status IN ('pending', 'running')",
            ("已取消", picture_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return dict(row)
    finally:
        conn.close()


def get_job(picture_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM generated_pictures WHERE id = ?",
            (picture_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_jobs(companion_id: str, branch_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Newest first. Pending rows are included so the stage can show progress."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM generated_pictures "
            "WHERE companion_id = ? AND branch_id = ? ORDER BY id DESC LIMIT ?",
            (companion_id, branch_id, max(1, min(limit, 500))),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def latest_done(companion_id: str, branch_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM generated_pictures "
            "WHERE companion_id = ? AND branch_id = ? AND status = 'done' "
            "ORDER BY id DESC LIMIT 1",
            (companion_id, branch_id),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def delete_job(picture_id: int) -> Optional[Dict[str, Any]]:
    """Remove a row and return it, so the caller can unlink its file."""
    job = get_job(picture_id)
    if not job:
        return None
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM generated_pictures WHERE id = ?", (picture_id,))
        conn.commit()
    finally:
        conn.close()
    return job


def count_since(sql: str, params: tuple) -> int:
    conn = _get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def count_today() -> int:
    """Images finished today, UTC, matching the timestamp SQLite writes."""
    return count_since(
        "SELECT COUNT(*) FROM generated_pictures "
        "WHERE status = 'done' AND date(created_at) = date('now')",
        (),
    )


def count_done(companion_id: str, branch_id: str) -> int:
    return count_since(
        "SELECT COUNT(*) FROM generated_pictures "
        "WHERE companion_id = ? AND branch_id = ? AND status = 'done'",
        (companion_id, branch_id),
    )


def seconds_since_last(companion_id: str, branch_id: str) -> Optional[int]:
    """Seconds since the newest automatic attempt on this branch.

    Any status counts: a failed automatic attempt still means the cooldown was
    consumed, otherwise a broken provider would be retried on every turn.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT CAST(strftime('%s','now') AS INTEGER) - "
            "CAST(strftime('%s', created_at) AS INTEGER) AS age "
            "FROM generated_pictures "
            "WHERE companion_id = ? AND branch_id = ? AND source != 'manual' "
            "ORDER BY id DESC LIMIT 1",
            (companion_id, branch_id),
        ).fetchone()
        if not row or row["age"] is None:
            return None
        return max(0, int(row["age"]))
    finally:
        conn.close()


def has_running(companion_id: str, branch_id: str) -> bool:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM generated_pictures "
            "WHERE companion_id = ? AND branch_id = ? AND status IN ('pending','running')",
            (companion_id, branch_id),
        ).fetchone()
        return bool(row) and int(row[0]) > 0
    finally:
        conn.close()


def fail_stale_jobs() -> int:
    """Close rows left mid-flight by a previous process.

    A task cannot survive the process that owns it, so a pending row after a
    restart would otherwise sit on the stage as "generating" forever.
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE generated_pictures SET status = 'failed', "
            "error = '任务中断：应用已重启', updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('pending','running')"
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()

"""Group rooms — storage.

A deliberately separate set of tables from the single-companion ones. A group
message can never show up in a companion thread, and a companion's memory can
never be pulled into a room. The only thing shared is the `companions` table,
read to build each speaker's prompt.

The tables live in the same SQLite file as everything else, which keeps backups
and the WAL setup unchanged, but nothing joins across the boundary: every query
here filters by `session_id`, and no single-companion query knows these tables
exist.
"""

import json
import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from ..companion_db import _get_conn

logger = logging.getLogger(__name__)

_ready = False
_lock = threading.Lock()

MAX_MEMBERS = 3

# Per-room ceiling. Extraction never stops on its own, so growth has to be
# bounded somewhere; the least recently useful rows are the ones dropped.
MAX_MEMORIES = 40

_DDL = """
CREATE TABLE IF NOT EXISTS group_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'New room',
    scene TEXT NOT NULL DEFAULT '',
    -- Extra context about the user, appended to each card's own user_info when
    -- the prompt is built. Kept here so it does not have to be re-typed into
    -- every card that joins a room.
    user_note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    companion_id TEXT NOT NULL,
    turn_order INTEGER NOT NULL DEFAULT 0,
    focus INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, companion_id),
    FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_group_members_session ON group_members(session_id);

CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    -- companion_id for a character line, 'user' for the user, NULL for narration.
    speaker_id TEXT,
    -- Snapshot of the name at the time it was said: renaming a card later must
    -- not rewrite what the room already remembers hearing.
    speaker_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'assistant',
    kind TEXT NOT NULL DEFAULT 'say',
    content TEXT NOT NULL,
    -- JSON array of {kind, name, url, mime, size, width, height}. Images live on
    -- disk; this is only the reference the transcript and the album read.
    attachments TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_group_messages_session ON group_messages(session_id, id);

CREATE TABLE IF NOT EXISTS group_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    -- 0 plain, 1 pinned, 2 strong. Pinned survives trimming; strong is also
    -- pulled into every prompt whether or not it clears the score floor.
    mark INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_group_memories_session ON group_memories(session_id);

CREATE TABLE IF NOT EXISTS group_memory_cursors (
    session_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    messages_compacted INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_group_compactions_session ON group_compactions(session_id, id);
"""


def _init_group_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _migrate_embeddings(conn)
    _migrate_memory_cursors(conn)
    _migrate_attachments(conn)
    _migrate_memory_mark(conn)
    conn.commit()


def _migrate_memory_mark(conn: sqlite3.Connection) -> None:
    """Add the pin column to rooms created before hearts existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(group_memories)").fetchall()}
    if "mark" not in cols:
        conn.execute(
            "ALTER TABLE group_memories ADD COLUMN mark INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_attachments(conn: sqlite3.Connection) -> None:
    """Add the attachment column to rooms created before images were supported.

    SQLite has no "add column if not exists", so the column list is the check,
    the same shape as the embedding migration next door.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(group_messages)").fetchall()}
    if "attachments" not in cols:
        conn.execute(
            "ALTER TABLE group_messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_memory_cursors(conn: sqlite3.Connection) -> None:
    """Seed a cursor for rooms that predate the table.

    `INSERT OR IGNORE` makes this run once per room: a room that has a cursor
    keeps it, a new room gets zero so its first message is mined. Without the
    seed the first extraction after this change would treat every historical
    message as new and write the same memories a second time.
    """
    conn.execute(
        "INSERT OR IGNORE INTO group_memory_cursors (session_id, last_message_id) "
        "SELECT s.id, COALESCE(MAX(m.id), 0) "
        "FROM group_sessions s "
        "LEFT JOIN group_messages m ON m.session_id = s.id "
        "GROUP BY s.id"
    )


def _migrate_embeddings(conn: sqlite3.Connection) -> None:
    """Add the columns that arrived after the first release.

    SQLite has no "add column if not exists", so the column list is the check.
    Rows written before the change keep NULL vectors and a zero use count; both
    are filled in on first use rather than by a backfill pass at startup.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(group_memories)").fetchall()}
    if "embedding" not in cols:
        conn.execute("ALTER TABLE group_memories ADD COLUMN embedding BLOB")
    if "use_count" not in cols:
        conn.execute("ALTER TABLE group_memories ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")


def _conn() -> sqlite3.Connection:
    """Connection with the group schema guaranteed.

    `_get_conn` already opens the shared database; this adds the group tables on
    first use and never touches the single-companion schema.
    """
    global _ready
    conn = _get_conn()
    if not _ready:
        with _lock:
            if not _ready:
                _init_group_tables(conn)
                _rename_default_rooms(conn)
                _ready = True
    return conn


def _rename_default_rooms(conn: sqlite3.Connection) -> None:
    """Give rooms created before auto-naming a `group{n}` name.

    Everything built by the first version of this module is called "New room",
    which makes the room column read as a stack of duplicates. Runs once, on the
    first connection after the change.
    """
    rows = conn.execute(
        "SELECT id FROM group_sessions WHERE name = 'New room' ORDER BY id"
    ).fetchall()
    if not rows:
        return
    taken = {
        str(r["name"]) for r in conn.execute("SELECT name FROM group_sessions").fetchall()
    }
    n = 1
    for row in rows:
        while f"group{n}" in taken:
            n += 1
        name = f"group{n}"
        conn.execute("UPDATE group_sessions SET name = ? WHERE id = ?", (name, row["id"]))
        taken.add(name)
    conn.commit()


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict]:
    return dict(row) if row is not None else None


# ==================== sessions ====================

def create_session(name: str, scene: str = "", user_note: str = "") -> Dict:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO group_sessions (name, scene, user_note) VALUES (?, ?, ?)",
            ((name or "New room").strip(), scene or "", user_note or ""),
        )
        session_id = int(cur.lastrowid)
        # A new room starts at zero so its first message is mined. The
        # migration's MAX(id) seed must never apply to a room created here.
        conn.execute(
            "INSERT OR IGNORE INTO group_memory_cursors (session_id, last_message_id) "
            "VALUES (?, 0)",
            (session_id,),
        )
        conn.commit()
        return get_session(session_id) or {}
    finally:
        conn.close()


def get_session(session_id: int) -> Optional[Dict]:
    conn = _conn()
    try:
        return _row(conn.execute(
            "SELECT * FROM group_sessions WHERE id = ?", (session_id,)
        ).fetchone())
    finally:
        conn.close()


def next_room_name() -> str:
    """`group1`, `group2`, … — the lowest number not already taken."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT name FROM group_sessions").fetchall()
        taken = {str(r["name"]) for r in rows}
        n = 1
        while f"group{n}" in taken:
            n += 1
        return f"group{n}"
    finally:
        conn.close()


def list_sessions() -> List[Dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM group_members m WHERE m.session_id = s.id) AS member_count, "
            "(SELECT COUNT(*) FROM group_messages g WHERE g.session_id = s.id) AS message_count "
            "FROM group_sessions s ORDER BY s.updated_at DESC, s.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_session(session_id: int, **fields) -> Optional[Dict]:
    """Patch the fields given. Unknown keys are ignored rather than raising."""
    allowed = {"name", "scene", "user_note"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not patch:
        return get_session(session_id)
    sets = ", ".join(f"{k} = ?" for k in patch)
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE group_sessions SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*patch.values(), session_id),
        )
        conn.commit()
        return get_session(session_id)
    finally:
        conn.close()


def delete_session(session_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM group_sessions WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def touch_session(session_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE group_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ==================== members ====================

def list_members(session_id: int) -> List[Dict]:
    """Members with the card fields the prompt needs, in speaking order."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT m.companion_id, m.turn_order, m.focus, "
            "c.name, c.personality, c.description, c.user_info, c.scenario "
            "FROM group_members m "
            "LEFT JOIN companions c ON c.id = m.companion_id "
            "WHERE m.session_id = ? ORDER BY m.turn_order, m.id",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            # A card deleted from the cast leaves the membership row behind so
            # the room keeps its shape; the prompt just gets a placeholder name.
            item["name"] = item.get("name") or item.get("companion_id") or "Unknown"
            out.append(item)
        return out
    finally:
        conn.close()


def add_member(session_id: int, companion_id: str) -> Dict:
    """Add a character. Refuses past MAX_MEMBERS; re-adding is a no-op."""
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT id FROM group_members WHERE session_id = ? AND companion_id = ?",
            (session_id, companion_id),
        ).fetchone()
        if existing:
            return {"ok": True, "already": True}
        row = conn.execute(
            """
            SELECT c.card_format,
                   (SELECT COUNT(*) FROM companion_assets WHERE companion_id = c.id) AS asset_count
            FROM companions c
            WHERE c.id = ?
            """,
            (companion_id,),
        ).fetchone()
        if row is not None:
            fmt = str(row["card_format"] or "").lower()
            if fmt == "charx" or (not fmt and int(row["asset_count"] or 0) > 0):
                return {"ok": False, "error": "charx cards cannot be invited to group chat."}
        count = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if count >= MAX_MEMBERS:
            return {"ok": False, "error": f"A room holds at most {MAX_MEMBERS} characters."}
        order = conn.execute(
            "SELECT COALESCE(MAX(turn_order), -1) + 1 FROM group_members WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO group_members (session_id, companion_id, turn_order) VALUES (?, ?, ?)",
            (session_id, companion_id, order),
        )
        conn.commit()
        touch_session(session_id)
        return {"ok": True}
    finally:
        conn.close()


def remove_member(session_id: int, companion_id: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM group_members WHERE session_id = ? AND companion_id = ?",
            (session_id, companion_id),
        )
        conn.commit()
        touch_session(session_id)
    finally:
        conn.close()


def set_focus(session_id: int, companion_id: str) -> None:
    """Exactly one member carries focus, which is who the view centres on."""
    conn = _conn()
    try:
        conn.execute("UPDATE group_members SET focus = 0 WHERE session_id = ?", (session_id,))
        conn.execute(
            "UPDATE group_members SET focus = 1 WHERE session_id = ? AND companion_id = ?",
            (session_id, companion_id),
        )
        conn.commit()
    finally:
        conn.close()


# ==================== messages ====================

def _message_row(row: sqlite3.Row) -> Dict:
    """A message with `attachments` decoded from its stored JSON."""
    out = dict(row)
    raw = out.get("attachments")
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        parsed = []
    out["attachments"] = parsed if isinstance(parsed, list) else []
    return out


def add_message(
    session_id: int,
    content: str,
    role: str = "assistant",
    speaker_id: Optional[str] = None,
    speaker_name: str = "",
    kind: str = "say",
    attachments: Optional[List[Dict]] = None,
) -> Dict:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO group_messages "
            "(session_id, speaker_id, speaker_name, role, kind, content, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                speaker_id,
                speaker_name or "",
                role,
                kind,
                content,
                json.dumps(attachments or [], ensure_ascii=False),
            ),
        )
        conn.commit()
        touch_session(session_id)
        row = conn.execute(
            "SELECT * FROM group_messages WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _message_row(row)
    finally:
        conn.close()


def list_messages(session_id: int, limit: int = 200) -> List[Dict]:
    """Newest `limit` messages, returned oldest-first."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM group_messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (session_id, limit),
        ).fetchall()
        return [_message_row(r) for r in rows]
    finally:
        conn.close()


def messages_after(session_id: int, after_id: int, limit: int = 12) -> List[Dict]:
    """Oldest `limit` messages newer than `after_id`.

    Memory extraction walks forward from its stored cursor instead of reading a
    fixed tail window: ids only grow (`AUTOINCREMENT`), so a cursor that lags by
    more than a window still sees every message once, in order.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM group_messages WHERE session_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (session_id, int(after_id), int(limit)),
        ).fetchall()
        return [_message_row(r) for r in rows]
    finally:
        conn.close()


def message_count(session_id: int) -> int:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM group_messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def delete_messages_after(session_id: int, message_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM group_messages WHERE session_id = ? AND id > ?",
            (session_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_message(session_id: int, message_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM group_messages WHERE session_id = ? AND id = ?",
            (session_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_message_content(session_id: int, message_id: int, content: str) -> None:
    """Rewrite one line in place.

    Used when a reroll hands the turn to another member: the cue that named the
    previous speaker has to name the new one instead.
    """
    conn = _conn()
    try:
        conn.execute(
            "UPDATE group_messages SET content = ? WHERE session_id = ? AND id = ?",
            (content, session_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def clear_messages(session_id: int) -> int:
    """Empty a room's transcript.

    Members, memory and summaries are kept: clearing is "start this
    conversation over", not "delete the room". Returns how many rows went.
    """
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM group_messages WHERE session_id = ?", (session_id,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def get_session_image_attachments(session_id: int) -> List[Dict]:
    """Every image or video this room's messages reference, newest first, deduped by url.

    Compaction never deletes rows, so the album keeps listing media from
    messages a summary already covers. Without that the files would linger on
    disk with nothing pointing at them.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM group_messages WHERE session_id = ? AND attachments != '[]' "
            "ORDER BY id DESC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    seen = set()
    out: List[Dict] = []
    for row in rows:
        for att in _message_row(row)["attachments"]:
            if not isinstance(att, dict):
                continue
            kind = att.get("kind")
            if kind not in ("image", "video"):
                continue
            url = str(att.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            entry: Dict = {
                "kind": kind,
                "url": url,
                "name": att.get("name") or ("video" if kind == "video" else "image"),
                "mime": att.get("mime") or "",
                "size": att.get("size") or 0,
                "width": att.get("width") or 0,
                "height": att.get("height") or 0,
                "message_id": int(row["id"]),
                "created_at": row["created_at"],
            }
            if kind == "video":
                entry["poster_url"] = att.get("poster_url") or ""
                entry["duration"] = att.get("duration") or 0
                entry["frames"] = att.get("frames") or []
                entry["transcript"] = att.get("transcript") or ""
            out.append(entry)
    return out


def get_session_attachments_by_url(session_id: int, url: str) -> List[Dict]:
    """Return attachment descriptors matching *url* inside this room."""
    if not url:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT attachments FROM group_messages "
            "WHERE session_id = ? AND attachments != '[]'",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    found: List[Dict] = []
    for row in rows:
        try:
            items = json.loads(row["attachments"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("url") or "") == url:
                found.append(item)
    return found


def remove_session_image(session_id: int, url: str) -> int:
    """Drop one image reference from every message in the room.

    Returns how many messages changed. The file is removed by the route, not
    here: the database only holds URLs, and a failed unlink must not leave a
    row pointing at a file the album can no longer open.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, attachments FROM group_messages "
            "WHERE session_id = ? AND attachments != '[]'",
            (session_id,),
        ).fetchall()
        updated = 0
        for row in rows:
            try:
                items = json.loads(row["attachments"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(items, list):
                continue
            kept = [
                item for item in items
                if not (isinstance(item, dict) and str(item.get("url") or "") == url)
            ]
            if len(kept) == len(items):
                continue
            conn.execute(
                "UPDATE group_messages SET attachments = ? WHERE id = ?",
                (json.dumps(kept, ensure_ascii=False), row["id"]),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def compacted_messages(session_id: int) -> List[Dict]:
    """The turns already folded into the newest summary, oldest first.

    `messages_compacted` stores the **id** the summary reached, not a row count.
    Ids are auto-increment on a table shared by every room, so they skip when a
    message is deleted; reading the value as a count would slice the wrong
    range. The boundary is an upper bound on id, nothing else.

    Compaction never deletes rows — this is what "view source" reads.
    """
    latest = latest_compaction(session_id)
    if not latest:
        return []
    boundary = int(latest.get("messages_compacted") or 0)
    if boundary <= 0:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM group_messages WHERE session_id = ? AND id <= ? ORDER BY id ASC",
            (session_id, boundary),
        ).fetchall()
        return [_message_row(r) for r in rows]
    finally:
        conn.close()


# ==================== memories ====================

def get_memory_cursor(session_id: int) -> int:
    """Last message id this room's memory extraction has consumed."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT last_message_id FROM group_memory_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["last_message_id"]) if row else 0
    finally:
        conn.close()


def set_memory_cursor(session_id: int, message_id: int) -> None:
    """Advance the cursor, never rewind it.

    Extraction batches can overlap after a crash; `MAX` keeps the stored value
    monotonic so an out-of-order write cannot make the room mine the same
    messages twice.
    """
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO group_memory_cursors (session_id, last_message_id, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "last_message_id = MAX(last_message_id, excluded.last_message_id), "
            "updated_at = CURRENT_TIMESTAMP",
            (session_id, int(message_id)),
        )
        conn.commit()
    finally:
        conn.close()


def memory_contents(session_id: int, contents: List[str]) -> set:
    """Which of these exact strings the room already remembers.

    One query for the whole batch. Used to filter an extraction result before
    it reaches `add_memory`, which is idempotent on its own but cannot tell the
    caller what it skipped.
    """
    values = [c for c in dict.fromkeys(contents) if c]
    if not values:
        return set()
    placeholders = ",".join("?" for _ in values)
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT content FROM group_memories WHERE session_id = ? "
            f"AND content IN ({placeholders})",
            (session_id, *values),
        ).fetchall()
        return {str(r["content"]) for r in rows}
    finally:
        conn.close()


def add_memory(
    session_id: int,
    content: str,
    keywords: str = "",
    about: str = "",
    embedding: Optional[bytes] = None,
) -> int:
    """Insert one memory, or return 0 when the room already has that exact text.

    The same sentence arrives again whenever extraction re-reads a message it
    has already mined. Callers count real inserts, so an idempotent write is
    what makes "6 stored" mean six new rows instead of six attempts.
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO group_memories (session_id, content, keywords, about, embedding) "
            "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM group_memories WHERE session_id = ? AND content = ?)",
            (session_id, content, keywords or "", about or "", embedding, session_id, content),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return 0
        return int(cur.lastrowid)
    finally:
        conn.close()


def trim_memories(session_id: int, limit: int = MAX_MEMORIES) -> int:
    """Drop the least recently useful memories beyond `limit`. Returns how many.

    Keeps the newest `limit` rows by last use, then by creation, and deletes the
    rest. Ordered by when the room last needed a memory rather than when it was
    written: a fact from last week that came up today outlives one from
    yesterday that never did. Unused rows fall back to their creation time.

    Pinned rows are exempt and do not count against `limit`: a heart is the
    user saying this one stays, so trimming must never be the thing that
    removes it.
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM group_memories WHERE session_id = ? AND mark = 0 AND id NOT IN ("
            "SELECT id FROM group_memories WHERE session_id = ? AND mark = 0 "
            "ORDER BY COALESCE(last_used_at, created_at) DESC, id DESC LIMIT ?)",
            (session_id, session_id, limit),
        )
        conn.commit()
        return int(cur.rowcount) if cur.rowcount > 0 else 0
    finally:
        conn.close()


def list_memory_vectors(session_id: int) -> List[Dict]:
    """`(id, embedding)` for every memory in a room that already has a vector."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, embedding FROM group_memories "
            "WHERE session_id = ? AND embedding IS NOT NULL",
            (session_id,),
        ).fetchall()
        return [{"id": int(r["id"]), "embedding": r["embedding"]} for r in rows]
    finally:
        conn.close()


def list_memories_missing_vectors(session_id: int, limit: int = 200) -> List[Dict]:
    """Memories written before the model existed, oldest first."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, content FROM group_memories "
            "WHERE session_id = ? AND embedding IS NULL ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_memory_embedding(memory_id: int, embedding: Optional[bytes]) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE group_memories SET embedding = ? WHERE id = ?",
            (embedding, memory_id),
        )
        conn.commit()
    finally:
        conn.close()


def retire_superseded_memory(session_id: int, target: object) -> int:
    """Delete an unpinned memory replaced by newer information.

    Accepts either an integer/numeric id (optionally wrapped as `[12]` or `#12`)
    or the opening words of the old memory's content. User-pinned rows
    (`mark > 0`) are never retired automatically.
    """
    raw = str(target or "").strip()
    if not raw:
        return 0
    id_candidate = raw.strip("[]# ")
    conn = _conn()
    try:
        if id_candidate.isdigit():
            cur = conn.execute(
                "DELETE FROM group_memories WHERE session_id = ? AND id = ? AND mark = 0",
                (session_id, int(id_candidate)),
            )
        else:
            if len(raw) < 4:
                return 0
            prefix = (
                raw[:60]
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            cur = conn.execute(
                "DELETE FROM group_memories WHERE session_id = ? AND mark = 0 "
                "AND content LIKE ? ESCAPE '\\'",
                (session_id, prefix + "%"),
            )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def update_memory(session_id: int, memory_id: int, **fields) -> bool:
    """Edit a memory in place. Only the three text columns are writable.

    The vector is not touched here: it describes the content, so whoever changes
    the content has to re-encode it or retrieval will keep matching the old text.
    """
    allowed = {
        k: v for k, v in fields.items() if k in ("content", "keywords", "about", "mark")
    }
    if "mark" in allowed:
        allowed["mark"] = max(0, min(2, int(allowed["mark"])))
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE group_memories SET {sets} WHERE session_id = ? AND id = ?",
            (*allowed.values(), session_id, memory_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# Columns safe to hand back to the API. `embedding` is deliberately absent: it
# is a float32 BLOB, and FastAPI's JSON encoder turns bytes into text with
# `bytes.decode()`, which raises on any byte that is not valid UTF-8 — one
# memory in a room was enough to 500 the whole room payload.
_MEMORY_COLS = "id, session_id, content, keywords, about, created_at, last_used_at, use_count, mark"


def memories_by_ids(memory_ids: List[int], session_id: int) -> List[Dict]:
    """Fetch memories by id in the order given, dropping ids from other rooms."""
    if not memory_ids:
        return []
    placeholders = ",".join("?" for _ in memory_ids)
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {_MEMORY_COLS} FROM group_memories WHERE session_id = ? AND id IN ({placeholders})",
            (session_id, *memory_ids),
        ).fetchall()
    finally:
        conn.close()
    by_id = {int(r["id"]): dict(r) for r in rows}
    return [by_id[i] for i in memory_ids if i in by_id]


def list_marked_memories(session_id: int, mark: int = 2) -> List[Dict]:
    """Memories carrying a heart. `mark=2` are the ones every prompt prefers."""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {_MEMORY_COLS} FROM group_memories "
            "WHERE session_id = ? AND mark = ? ORDER BY id ASC",
            (session_id, int(mark)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_memories(session_id: int, limit: int = 200) -> List[Dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {_MEMORY_COLS} FROM group_memories WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_memories(
    session_id: int,
    probe: str,
    limit: int = 6,
    max_required: int = 2,
    vector_scores: Optional[Dict[int, float]] = None,
) -> List[Dict]:
    """Rank memories using zero-dependency hybrid BM25 + CJK n-gram scoring.

    When optional external `vector_scores` are supplied, lexical and vector
    signals are fused together; otherwise pure lexical + phrase + subject
    scoring runs in-memory in under a millisecond.
    """
    if not (probe or "").strip():
        return []
    from . import retriever

    rows = list_memories(session_id, limit=400)
    if not rows:
        return []
    return retriever.rank_memories(
        probe,
        rows,
        limit=limit,
        max_required=max_required,
        vector_scores=vector_scores,
    )


def touch_memories(memory_ids: List[int]) -> None:
    """Record that these memories made it into a reply: when, and how often."""
    if not memory_ids:
        return
    conn = _conn()
    try:
        conn.executemany(
            "UPDATE group_memories SET last_used_at = CURRENT_TIMESTAMP, "
            "use_count = use_count + 1 WHERE id = ?",
            [(mid,) for mid in memory_ids],
        )
        conn.commit()
    finally:
        conn.close()


def delete_memory(session_id: int, memory_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM group_memories WHERE session_id = ? AND id = ?",
            (session_id, memory_id),
        )
        conn.commit()
    finally:
        conn.close()


# ==================== compaction ====================

def save_compaction(session_id: int, summary: str, messages_compacted: int) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO group_compactions (session_id, summary, messages_compacted) "
            "VALUES (?, ?, ?)",
            (session_id, summary, messages_compacted),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _covered_count(conn: sqlite3.Connection, session_id: int, boundary: int) -> int:
    """How many rows a summary really covers, derived from its id boundary.

    The stored value is an id, not a count, so anything that displays "N
    messages" has to ask the table rather than trust the number.
    """
    if boundary <= 0:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM group_messages WHERE session_id = ? AND id <= ?",
        (session_id, boundary),
    ).fetchone()
    return int(row[0]) if row else 0


def latest_compaction(session_id: int) -> Optional[Dict]:
    conn = _conn()
    try:
        row = _row(conn.execute(
            "SELECT * FROM group_compactions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone())
        if row:
            row["covered_count"] = _covered_count(
                conn, session_id, int(row.get("messages_compacted") or 0)
            )
        return row
    finally:
        conn.close()


def list_compactions(session_id: int) -> List[Dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM group_compactions WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["covered_count"] = _covered_count(
                conn, session_id, int(r.get("messages_compacted") or 0)
            )
        return out
    finally:
        conn.close()

"""
script_db.py - Storage for script mode.

A script is a list of scenes written by the user. It belongs to nobody: the
same script can be played by any character, and editing it later does not
touch the sessions already running. A session is one playthrough, bound to a
companion and a branch, and holds the cursor plus the character HUD.

The tables live in companions.db and are created by companion_db._init_tables.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from .companion_db import _get_conn

logger = logging.getLogger(__name__)

# The HUD starts with the four keys the script prompt asks the model to keep
# updated. Empty values are deliberate: the model fills them in as it plays.
DEFAULT_HUD = {"mood": "", "affection": 0, "location": "", "outfit": ""}


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# ==================== Scripts ====================

def list_scripts() -> List[Dict]:
    """Every script with its scene count, newest edit first."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT s.id, s.title, s.premise, s.created_at, s.updated_at,
                      (SELECT COUNT(*) FROM script_nodes n WHERE n.script_id = s.id) AS node_count
               FROM scripts s
               ORDER BY s.updated_at DESC, s.id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_script(script_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, title, premise, created_at, updated_at FROM scripts WHERE id = ?",
            (script_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_script(title: str, premise: str = "") -> int:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO scripts (title, premise) VALUES (?, ?)",
            ((title or "").strip() or "未命名剧本", premise or ""),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def update_script(script_id: int, title: Optional[str], premise: Optional[str]) -> bool:
    fields = []
    values: List[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append((title or "").strip() or "未命名剧本")
    if premise is not None:
        fields.append("premise = ?")
        values.append(premise or "")
    if not fields:
        return False
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(script_id)
    conn = _get_conn()
    try:
        cursor = conn.execute(
            f"UPDATE scripts SET {', '.join(fields)} WHERE id = ?", values
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def delete_script(script_id: int) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


# ==================== Nodes ====================

def list_nodes(script_id: int) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, script_id, order_index, title, instruction, turns
               FROM script_nodes WHERE script_id = ? ORDER BY order_index, id""",
            (script_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_node(node_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, script_id, order_index, title, instruction
               FROM script_nodes WHERE id = ?""",
            (node_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def replace_nodes(script_id: int, nodes: List[Dict]) -> List[Dict]:
    """Rewrite the whole scene list in one transaction.

    The editor sends the full list, so a delete-and-insert keeps ordering and
    ids consistent without diffing. Sessions reference scenes by index, not by
    id, so replacing rows never breaks a running playthrough.
    """
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM script_nodes WHERE script_id = ?", (script_id,))
        for index, node in enumerate(nodes):
            conn.execute(
                """INSERT INTO script_nodes (script_id, order_index, title, instruction, turns)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    script_id,
                    index,
                    (node.get("title") or "").strip() or f"第 {index + 1} 幕",
                    node.get("instruction") or "",
                    _as_int(node.get("turns")),
                ),
            )
        conn.execute(
            "UPDATE scripts SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (script_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return list_nodes(script_id)


# ==================== Sessions ====================

def create_session(
    script_id: int,
    companion_id: str,
    branch_id: str,
    hud: Optional[Dict[str, Any]] = None,
    start_index: int = 0,
    copy_from_session: Optional[int] = None,
) -> int:
    """Open a playthrough and take a copy of the scenes it will play.

    The copy is what the playthrough reads from that moment on, so edits to the
    shared script by anyone else cannot rewrite a story already in progress.
    A replay copies the session it restarts from, keeping the retold story on the
    same scenes as the original.
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO script_sessions
               (script_id, companion_id, branch_id, current_index, scene_done, turns_done, hud, status)
               VALUES (?, ?, ?, ?, 0, 0, ?, 'playing')""",
            (
                script_id,
                companion_id,
                branch_id,
                max(0, int(start_index)),
                json.dumps(hud if hud is not None else dict(DEFAULT_HUD), ensure_ascii=False),
            ),
        )
        session_id = int(cursor.lastrowid)

        rows = []
        if copy_from_session:
            rows = conn.execute(
                """SELECT order_index, title, instruction, turns FROM script_session_nodes
                   WHERE session_id = ? ORDER BY order_index, id""",
                (copy_from_session,),
            ).fetchall()
        if not rows:
            rows = conn.execute(
                """SELECT order_index, title, instruction, turns FROM script_nodes
                   WHERE script_id = ? ORDER BY order_index, id""",
                (script_id,),
            ).fetchall()
        for row in rows:
            conn.execute(
                """INSERT INTO script_session_nodes
                   (session_id, order_index, title, instruction, turns)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, row["order_index"], row["title"], row["instruction"], row["turns"]),
            )
        conn.commit()
        return session_id
    finally:
        conn.close()


def list_session_nodes(session_id: int) -> List[Dict]:
    """The scenes this playthrough plays. Older sessions are copied on first read."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, session_id, order_index, title, instruction, turns
               FROM script_session_nodes WHERE session_id = ?
               ORDER BY order_index, id""",
            (session_id,),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]
    finally:
        conn.close()
    return _snapshot_session_from_script(session_id)


def _snapshot_session_from_script(session_id: int) -> List[Dict]:
    """Fill in a copy for a playthrough that predates the copy table."""
    conn = _get_conn()
    try:
        session = conn.execute(
            "SELECT script_id FROM script_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            return []
        rows = conn.execute(
            """SELECT order_index, title, instruction, turns FROM script_nodes
               WHERE script_id = ? ORDER BY order_index, id""",
            (session["script_id"],),
        ).fetchall()
        for row in rows:
            conn.execute(
                """INSERT INTO script_session_nodes
                   (session_id, order_index, title, instruction, turns)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, row["order_index"], row["title"], row["instruction"], row["turns"]),
            )
        conn.commit()
    finally:
        conn.close()
    return list_session_nodes(session_id)


def replace_session_nodes(session_id: int, nodes: List[Dict]) -> List[Dict]:
    """Rewrite one playthrough's scenes. The shared script is not touched."""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM script_session_nodes WHERE session_id = ?", (session_id,)
        )
        for index, node in enumerate(nodes):
            conn.execute(
                """INSERT INTO script_session_nodes
                   (session_id, order_index, title, instruction, turns)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    index,
                    (node.get("title") or "").strip() or f"第 {index + 1} 幕",
                    node.get("instruction") or "",
                    _as_int(node.get("turns")),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return list_session_nodes(session_id)


def _row_to_session(row) -> Dict:
    data = dict(row)
    raw = data.get("hud") or "{}"
    try:
        parsed = json.loads(raw)
        data["hud"] = parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        data["hud"] = {}
    data["scene_done"] = bool(data.get("scene_done"))
    data["turns_done"] = _as_int(data.get("turns_done"))
    return data


def get_session(session_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, script_id, companion_id, branch_id, current_index,
                      scene_done, turns_done, hud, status, created_at, updated_at
               FROM script_sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def get_active_session(companion_id: str) -> Optional[Dict]:
    """The playthrough currently running for a companion, if any."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, script_id, companion_id, branch_id, current_index,
                      scene_done, turns_done, hud, status, created_at, updated_at
               FROM script_sessions
               WHERE companion_id = ? AND status = 'playing'
               ORDER BY id DESC LIMIT 1""",
            (companion_id,),
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def update_session_hud(session_id: int, hud: Dict[str, Any]) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE script_sessions
               SET hud = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (json.dumps(hud or {}, ensure_ascii=False), session_id),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def set_scene_done(session_id: int, done: bool) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE script_sessions
               SET scene_done = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (1 if done else 0, session_id),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def increment_turn(session_id: int) -> int:
    """Count one finished exchange in the current scene, returning the new count."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE script_sessions
               SET turns_done = turns_done + 1, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (session_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT turns_done FROM script_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _as_int(row["turns_done"]) if row else 0
    finally:
        conn.close()


def advance_session(session_id: int, node_count: int) -> Optional[Dict]:
    """Move to the next scene. The last scene closes the playthrough."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT current_index, status FROM script_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        current = int(row["current_index"] or 0)
        next_index = current + 1
        if next_index >= node_count:
            conn.execute(
                """UPDATE script_sessions
                   SET status = 'finished', scene_done = 0, turns_done = 0,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (session_id,),
            )
        else:
            conn.execute(
                """UPDATE script_sessions
                   SET current_index = ?, scene_done = 0, turns_done = 0,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (next_index, session_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id)


def set_session_status(session_id: int, status: str) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE script_sessions
               SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, session_id),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def delete_session(session_id: int) -> bool:
    """Remove a playthrough. Its scene copy cascades with the row."""
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM script_sessions WHERE id = ?", (session_id,))
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def get_session_by_branch(companion_id: str, branch_id: str) -> Optional[Dict]:
    """The running playthrough on a branch, if that branch is a script."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, script_id, companion_id, branch_id, current_index,
                      scene_done, turns_done, hud, status, created_at, updated_at
               FROM script_sessions
               WHERE companion_id = ? AND branch_id = ? AND status = 'playing'
               ORDER BY id DESC LIMIT 1""",
            (companion_id, branch_id),
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def list_sessions_for_companion(companion_id: str) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, script_id, companion_id, branch_id, current_index,
                      scene_done, turns_done, hud, status, created_at, updated_at
               FROM script_sessions WHERE companion_id = ?
               ORDER BY id DESC""",
            (companion_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]
    finally:
        conn.close()


# ==================== Scene summaries ====================

def add_scene_summary(
    companion_id: str,
    branch_id: str,
    session_id: int,
    scene_from: int,
    scene_to: int,
    scene_titles: str,
    content: str,
    message_from_id: int,
    message_to_id: int,
) -> int:
    """Store one segment summary. Segments never merge with each other."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO script_scene_summaries
               (companion_id, branch_id, session_id, scene_from, scene_to,
                scene_titles, content, message_from_id, message_to_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                companion_id,
                branch_id,
                session_id,
                int(scene_from),
                int(scene_to),
                scene_titles,
                content,
                int(message_from_id),
                int(message_to_id),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def list_scene_summaries(companion_id: str, branch_id: str) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, companion_id, branch_id, session_id, scene_from,
                      scene_to, scene_titles, content, message_from_id,
                      message_to_id, created_at
               FROM script_scene_summaries
               WHERE companion_id = ? AND branch_id = ?
               ORDER BY id""",
            (companion_id, branch_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def clear_scene_summaries(companion_id: str, branch_id: str) -> int:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM script_scene_summaries WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def reset_session_progress(companion_id: str, branch_id: str) -> bool:
    """Send a playthrough back to its first scene, used when the thread is wiped."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE script_sessions
               SET current_index = 0, scene_done = 0, turns_done = 0,
                   updated_at = CURRENT_TIMESTAMP
               WHERE companion_id = ? AND branch_id = ? AND status = 'playing'""",
            (companion_id, branch_id),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()

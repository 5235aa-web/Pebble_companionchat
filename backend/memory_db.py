import json
import logging
import re
from typing import Dict, List, Optional

from .companion_db import _get_conn

logger = logging.getLogger(__name__)

# 单个角色+分支最多保留的记忆条数。超出后由 _evict 淘汰最不重要的一批。
# branch(75) + character(25) 合计每条时间线最多可见 100 条记忆。
MAX_MEMORIES = 75

# 一次注入最多带多少条，以及最多占多少字符（粗略折算 token）。
INJECT_LIMIT = 5
INJECT_CHAR_BUDGET = 1600

VALID_KINDS = {"fact", "event", "preference", "relationship"}

# character: who they are, shared by every branch of this companion.
# branch: what happened on this timeline, visible only there.
VALID_SCOPES = {"character", "branch"}

# 跨分支共享的记忆单独封顶，否则支线一多，分支级淘汰会把角色的底色挤掉。
MAX_CHARACTER_MEMORIES = 25


def _row_to_memory(row) -> Dict:
    data = dict(row)
    try:
        data["keys"] = json.loads(data.get("keys") or "[]")
    except (TypeError, ValueError):
        data["keys"] = []
    try:
        data["source_message_ids"] = json.loads(data.get("source_message_ids") or "[]")
    except (TypeError, ValueError):
        data["source_message_ids"] = []
    return data


def _retire_superseded(
    conn,
    companion_id: str,
    branch_id: str,
    target: object,
) -> int:
    """Retire a superseded branch memory by id or opening prefix.

    Strictly scoped to `scope = 'branch'` on this `(companion_id, branch_id)`
    so automatic extraction on one timeline can never delete a character-wide
    memory shared by other branches.
    """
    raw = str(target or "").strip()
    if not raw:
        return 0
    id_candidate = raw.strip("[]# ")
    if id_candidate.isdigit():
        cur = conn.execute(
            """DELETE FROM companion_memories
               WHERE companion_id = ? AND branch_id = ?
                 AND scope = 'branch' AND id = ?""",
            (companion_id, branch_id, int(id_candidate)),
        )
        return int(cur.rowcount or 0)
    if len(raw) < 4:
        return 0
    prefix = (
        raw[:60]
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    cur = conn.execute(
        """DELETE FROM companion_memories
           WHERE companion_id = ? AND branch_id = ?
             AND scope = 'branch' AND content LIKE ? ESCAPE '\\'""",
        (companion_id, branch_id, prefix + "%"),
    )
    return int(cur.rowcount or 0)


def add_memories(
    companion_id: str,
    branch_id: str,
    items: List[Dict],
    source_message_ids: Optional[List[int]] = None,
) -> int:
    """插入一批已提取的记忆，返回实际写入条数。

    逐条校验而不是整批拒绝：一条格式不对不该拖垮同一批里其他有效的记忆。
    """
    if not items:
        return 0

    sources = json.dumps(source_message_ids or [], ensure_ascii=False)
    conn = _get_conn()
    written = 0
    try:
        for item in items:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            kind = str(item.get("kind") or "fact").strip().lower()
            if kind not in VALID_KINDS:
                kind = "fact"
            scope = str(item.get("scope") or "branch").strip().lower()
            if scope not in VALID_SCOPES:
                scope = "branch"
            keys = item.get("keys")
            if not isinstance(keys, list):
                keys = []
            keys = [str(k).strip() for k in keys if str(k).strip()][:8]
            try:
                importance = int(item.get("importance", 2))
            except (TypeError, ValueError):
                importance = 2
            importance = min(3, max(1, importance))

            supersedes = str(item.get("supersedes") or "").strip()
            if supersedes:
                _retire_superseded(conn, companion_id, branch_id, supersedes)

            conn.execute(
                """INSERT INTO companion_memories
                   (companion_id, branch_id, content, kind, scope, keys, importance, source_message_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    companion_id,
                    branch_id,
                    content,
                    kind,
                    scope,
                    json.dumps(keys, ensure_ascii=False),
                    importance,
                    sources,
                ),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()

    if written:
        _evict(companion_id, branch_id)
    return written


def _evict(companion_id: str, branch_id: str) -> int:
    """把超出上限的部分淘汰掉。

    排序键是"重要性升序、使用次数升序、最后使用时间升序"，也就是最不重要、
    最少被用到、最久没被碰过的先走。没有引入时间衰减曲线：曲线要调参，而
    这三个稳定可解释的键已经够用。

    两个作用域分别封顶。character 级的记忆属于整个角色，不按分支计算，否则
    支线一多，分支级的淘汰就会把"她是谁"这类底色挤掉。
    """
    return _evict_scope(companion_id, branch_id, "branch", MAX_MEMORIES) + _evict_scope(
        companion_id, None, "character", MAX_CHARACTER_MEMORIES
    )


def _evict_scope(
    companion_id: str,
    branch_id: Optional[str],
    scope: str,
    cap: int,
) -> int:
    """淘汰一个作用域里超出上限的部分。branch_id 为 None 时只看 scope。"""
    conn = _get_conn()
    try:
        if branch_id is None:
            where = "companion_id = ? AND scope = ?"
            args: tuple = (companion_id, scope)
        else:
            where = "companion_id = ? AND branch_id = ? AND scope = ?"
            args = (companion_id, branch_id, scope)

        total = conn.execute(
            f"SELECT COUNT(*) FROM companion_memories WHERE {where}", args
        ).fetchone()[0]
        extra = total - cap
        if extra <= 0:
            return 0
        cursor = conn.execute(
            f"""DELETE FROM companion_memories WHERE id IN (
                   SELECT id FROM companion_memories
                   WHERE {where}
                   ORDER BY importance ASC,
                            use_count ASC,
                            COALESCE(last_used_at, '') ASC,
                            id ASC
                   LIMIT ?
               )""",
            (*args, extra),
        )
        conn.commit()
        return cursor.rowcount or 0
    finally:
        conn.close()


def list_memories(companion_id: str, branch_id: str) -> List[Dict]:
    """面板用：全部记忆，最新的在前。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, content, kind, scope, keys, importance, created_at,
                      last_used_at, use_count, source_message_ids
               FROM companion_memories
               WHERE companion_id = ?
                 AND (branch_id = ? OR scope = 'character')
               ORDER BY id DESC""",
            (companion_id, branch_id),
        ).fetchall()
        return [_row_to_memory(r) for r in rows]
    finally:
        conn.close()


def _key_hit(key: str, probe: str) -> bool:
    """关键词是否出现在这段对话里。

    A plain substring test breaks English: "run" matches "brunch", "art"
    matches "start" and "party". Requiring a non-letter before the keyword
    keeps the useful part — "run" still finds "running" — while dropping the
    false hits inside unrelated words.

    CJK has no word boundaries, so those keywords stay plain substring.
    """
    key = str(key or "").strip().lower()
    if not key:
        return False
    if not key.isascii():
        return key in probe
    # A single ASCII letter matches inside almost every word ("me" in "time"),
    # so it is noise rather than a signal.
    if len(key) < 2:
        return False
    return re.search(r"(?<![a-z])" + re.escape(key), probe, re.IGNORECASE) is not None


def search_memories(
    companion_id: str,
    branch_id: str,
    text: str,
    limit: int = INJECT_LIMIT,
    char_budget: int = INJECT_CHAR_BUDGET,
) -> List[Dict]:
    """挑出与 *text* 相关、值得注入的记忆。

    使用零依赖混合打分（关键词短语 + 正文 BM25 + 中文 N-gram），结合
    importance 加权与 use_count 温和轮换，既能从正文召回，又不会被单次
    字面巧合带偏。
    """
    if not (text or "").strip():
        return []

    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, content, kind, scope, keys, importance, created_at,
                      last_used_at, use_count
               FROM companion_memories
               WHERE companion_id = ?
                 AND (branch_id = ? OR scope = 'character')""",
            (companion_id, branch_id),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    from . import memory_retriever

    mems = [_row_to_memory(r) for r in rows]
    return memory_retriever.rank_memories(
        text,
        mems,
        limit=limit,
        max_required=0,
        char_budget=char_budget,
    )


def touch_memories(memory_ids: List[int]) -> None:
    """记下这些记忆被注入了，供下一轮排序轮换使用。"""
    if not memory_ids:
        return
    conn = _get_conn()
    try:
        placeholders = ",".join("?" * len(memory_ids))
        conn.execute(
            f"""UPDATE companion_memories
                SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})""",
            memory_ids,
        )
        conn.commit()
    finally:
        conn.close()


def update_memory(
    companion_id: str,
    branch_id: str,
    memory_id: int,
    content: Optional[str] = None,
    keys: Optional[List[str]] = None,
) -> bool:
    """改一条记忆的正文或关键词。

    提取的措辞经常差一点，删了重来要等下一次提取才有机会，而那时对话已经
    过去了。就地改的成本低得多。

    作用域与删除一致：跨分支的记忆在任何分支都能改。
    """
    sets = []
    values: List = []
    if content is not None:
        text = content.strip()
        if not text:
            return False
        sets.append("content = ?")
        values.append(text)
    if keys is not None:
        clean = [str(k).strip() for k in keys if str(k).strip()][:8]
        sets.append("keys = ?")
        values.append(json.dumps(clean, ensure_ascii=False))
    if not sets:
        return False

    values.extend([memory_id, companion_id, branch_id])
    conn = _get_conn()
    try:
        cursor = conn.execute(
            f"""UPDATE companion_memories SET {', '.join(sets)}
                WHERE id = ? AND companion_id = ?
                  AND (branch_id = ? OR scope = 'character')""",
            values,
        )
        conn.commit()
        return (cursor.rowcount or 0) > 0
    finally:
        conn.close()


def delete_memory(companion_id: str, branch_id: str, memory_id: int) -> bool:
    """删掉一条记忆。

    作用域限定在 (companion_id, branch_id)：只按 id 删会让一个角色的记忆
    能被另一个角色的请求删掉。
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """DELETE FROM companion_memories
               WHERE id = ? AND companion_id = ?
                 AND (branch_id = ? OR scope = 'character')""",
            (memory_id, companion_id, branch_id),
        )
        conn.commit()
        return (cursor.rowcount or 0) > 0
    finally:
        conn.close()


def clear_memories(companion_id: str, branch_id: str, scope: Optional[str] = None) -> int:
    """清空记忆。scope 为 None 时连跨分支的角色级记忆一起清掉。"""
    conn = _get_conn()
    try:
        if scope in VALID_SCOPES:
            cursor = conn.execute(
                "DELETE FROM companion_memories WHERE companion_id = ? AND branch_id = ? AND scope = ?",
                (companion_id, branch_id, scope),
            )
        else:
            # Everything this character can currently see: this branch plus the
            # character-wide set.
            cursor = conn.execute(
                """DELETE FROM companion_memories
                   WHERE companion_id = ? AND (branch_id = ? OR scope = 'character')""",
                (companion_id, branch_id),
            )
        conn.commit()
        return cursor.rowcount or 0
    finally:
        conn.close()


def count_memories(companion_id: str, branch_id: str) -> int:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM companion_memories WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        ).fetchone()[0]
    finally:
        conn.close()


def get_cursor(companion_id: str, branch_id: str) -> int:
    """最后一条已被提取扫描过的消息 id；0 表示从未提取。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT memory_cursor FROM companion_branches WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def set_cursor(companion_id: str, branch_id: str, message_id: int) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE companion_branches SET memory_cursor = ?
               WHERE companion_id = ? AND branch_id = ?""",
            (int(message_id), companion_id, branch_id),
        )
        conn.commit()
    finally:
        conn.close()

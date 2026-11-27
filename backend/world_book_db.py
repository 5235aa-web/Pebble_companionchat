import json
import logging
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 复用companion数据库路径
from . import paths
from .paths import DB_PATH

_FOLDER_KEY_PREFIX = "\uf000folder:"
_VALID_CATEGORIES = frozenset({"media", "lore", "rpg"})

_STANDARD_MEDIA_SYNTAX_RE = re.compile(
    r"""<img\s*=|<img\b[^>]*\bsrc\s*=|\{\{\s*(?:img|image|asset|raw|audio|bgm|video)\s*::|(?:embeded|embedded)://""",
    re.IGNORECASE,
)
_MEDIA_HEADER_RE = re.compile(
    r"""(?:visual\s+asset|asset\s+tagging|asset\s+output|image\s+tag|media\s+tag"""
    r"""|에셋\s*출력|이미지\s*출력|어셋\s*출력|표정\s*출력|视觉资产|立绘|插图|表情标签|音效指令)""",
    re.IGNORECASE,
)
_MEDIA_BODY_HINT_RE = re.compile(
    r"""(?:exact\s+key|extension-free|asset\s+name|output\s+the\s+tag|before\s+the\s+paragraph"""
    r"""|태그|키|출력|标签)""",
    re.IGNORECASE,
)
_RPG_SIGNAL_RE = re.compile(
    r"""(?:상태창|에로스탯|경지\s*강화|퀘스트|거래\s*시세|경제|상점|스탯|호감도\s*수치|동전|은화|금화|전투\s*규칙"""
    r"""|\b(?:status\s*(?:window|bar|panel|data)|ero\s*stat|economy|transaction\s*protocol"""
    r"""|price\s*benchmarks?|quest|mastery|lv\.\s*\d+|hit\s*points|mana\s*points"""
    r"""|copper\s*coin|gold\s*coin|currency|inventory|combat\s*system)\b"""
    r"""|状态栏|属性面板|数值面板|经济系统|物价|任务系统|等级突破|货币|战斗系统)""",
    re.IGNORECASE,
)


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    paths.assert_safe_data_path(DB_PATH)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_world_book_tables(conn)
    return conn


def _init_world_book_tables(conn: sqlite3.Connection) -> None:
    """初始化世界书相关表"""
    conn.executescript("""
        -- 世界书定义表
        CREATE TABLE IF NOT EXISTS world_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- 世界书条目表
        CREATE TABLE IF NOT EXISTS world_book_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_book_id INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            comment TEXT DEFAULT '',
            keys TEXT DEFAULT '[]',
            secondary_keys TEXT DEFAULT '[]',
            content TEXT DEFAULT '',
            constant BOOLEAN DEFAULT 0,
            selective BOOLEAN DEFAULT 1,
            selective_logic INTEGER DEFAULT 0,
            "order" INTEGER DEFAULT 100,
            position INTEGER DEFAULT 0,
            depth INTEGER DEFAULT 4,
            disabled BOOLEAN DEFAULT 0,
            probability INTEGER DEFAULT 100,
            use_probability BOOLEAN DEFAULT 1,
            sticky INTEGER,
            cooldown INTEGER,
            delay INTEGER,
            exclude_recursion BOOLEAN DEFAULT 0,
            prevent_recursion BOOLEAN DEFAULT 0,
            delay_until_recursion INTEGER DEFAULT 0,
            group_name TEXT DEFAULT '',
            group_override BOOLEAN DEFAULT 0,
            group_weight INTEGER DEFAULT 100,
            case_sensitive BOOLEAN,
            match_whole_words BOOLEAN,
            use_group_scoring BOOLEAN,
            scan_depth INTEGER,
            outlet_name TEXT DEFAULT '',
            triggers TEXT DEFAULT '[]',
            ignore_budget BOOLEAN DEFAULT 0,
            category TEXT DEFAULT 'lore',
            translations TEXT DEFAULT '{}',
            FOREIGN KEY (world_book_id) REFERENCES world_books(id) ON DELETE CASCADE
        );
        
        -- AI伴侣与世界书的关联
        CREATE TABLE IF NOT EXISTS companion_world_books (
            companion_id TEXT NOT NULL,
            world_book_id INTEGER NOT NULL,
            is_primary BOOLEAN DEFAULT 0,
            PRIMARY KEY (companion_id, world_book_id),
            FOREIGN KEY (world_book_id) REFERENCES world_books(id) ON DELETE CASCADE
        );
        
        -- 索引
        CREATE INDEX IF NOT EXISTS idx_wb_entries_book_id ON world_book_entries(world_book_id);
        CREATE INDEX IF NOT EXISTS idx_wb_entries_uid ON world_book_entries(world_book_id, uid);
        CREATE INDEX IF NOT EXISTS idx_companion_wb ON companion_world_books(companion_id);
    """)
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(world_book_entries)").fetchall()
    }
    if "category" not in cols:
        conn.execute("ALTER TABLE world_book_entries ADD COLUMN category TEXT DEFAULT 'lore'")
    if "translations" not in cols:
        conn.execute("ALTER TABLE world_book_entries ADD COLUMN translations TEXT DEFAULT '{}'")
    conn.commit()


# ==================== World Book CRUD ====================

def create_world_book(name: str, description: str = "") -> int:
    """创建新世界书，返回世界书ID"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO world_books (name, description) VALUES (?, ?)",
            (name, description)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_world_book(world_book_id: int) -> Optional[Dict]:
    """获取单个世界书详情"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM world_books WHERE id = ?",
            (world_book_id,)
        ).fetchone()
        
        if not row:
            return None
        
        return dict(row)
    finally:
        conn.close()


def get_all_world_books() -> List[Dict]:
    """获取所有世界书列表"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM world_books ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_world_book(world_book_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    """更新世界书信息"""
    conn = _get_conn()
    try:
        updates = []
        values = []
        
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        
        if not updates:
            return False
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(world_book_id)
        
        cursor = conn.execute(
            f"UPDATE world_books SET {', '.join(updates)} WHERE id = ?",
            values
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_world_book(world_book_id: int) -> bool:
    """删除世界书及其所有条目"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM world_books WHERE id = ?",
            (world_book_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ==================== Entry CRUD ====================

def _normalize_position(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if v in (0, 1, 4) else 0
    s = str(raw or "").strip().lower()
    mapping = {
        "0": 0,
        "before_char": 0,
        "before": 0,
        "1": 1,
        "after_char": 1,
        "after": 1,
        "4": 4,
        "at_depth": 4,
        "depth": 4,
    }
    return mapping.get(s, 0)


def _normalize_probability(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return 100
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return 100
    if 0.0 < val <= 1.0 and isinstance(raw, float):
        val *= 100.0
    return max(0, min(100, int(round(val))))


_TRANSLATION_LOCALES = ("zh", "en")


def _normalize_category(raw: Any) -> str:
    s = str(raw or "lore").strip().lower()
    return s if s in _VALID_CATEGORIES else "lore"


def normalize_entry_translations(raw: Any) -> Dict[str, Dict[str, str]]:
    """Sanitize a world-book entry's display-only `translations` map."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}

    out: Dict[str, Dict[str, str]] = {}
    for loc in _TRANSLATION_LOCALES:
        bucket = raw.get(loc)
        if not isinstance(bucket, dict):
            continue
        cleaned: Dict[str, str] = {}
        has_text = False
        for key in ("comment", "content"):
            val = bucket.get(key)
            if isinstance(val, str):
                cleaned[key] = val
                if val.strip():
                    has_text = True
        ts = bucket.get("translated_at")
        if isinstance(ts, str) and ts.strip():
            cleaned["translated_at"] = ts.strip()
        if has_text:
            out[loc] = cleaned
    return out


def normalize_entry_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize both backend-native and frontend-shaped world-book entry payloads."""
    out = dict(raw or {})
    if not str(out.get("comment") or "").strip() and str(out.get("name") or "").strip():
        out["comment"] = str(out["name"]).strip()
    if "enabled" in out and out.get("disabled") is None:
        out["disabled"] = not bool(out["enabled"])
    if "logic" in out and out.get("selective_logic") is None:
        out["selective_logic"] = 3 if str(out["logic"]).strip().upper() == "AND" else 0
    if isinstance(out.get("selective_logic"), str):
        logic_map = {"and_any": 0, "not_all": 1, "not_any": 2, "and_all": 3, "or": 0, "and": 3}
        out["selective_logic"] = logic_map.get(out["selective_logic"].strip().lower(), 0)
    out["position"] = _normalize_position(out.get("position", 0))
    out["probability"] = _normalize_probability(out.get("probability", 100))
    try:
        out["depth"] = max(0, int(out.get("depth") if out.get("depth") is not None else 4))
    except (ValueError, TypeError):
        out["depth"] = 4
    out["category"] = _normalize_category(out.get("category"))
    return out


def _entry_to_db(entry: Dict) -> Dict:
    """将条目字典转换为数据库格式"""
    norm = normalize_entry_payload(entry)
    return {
        'uid': norm.get('uid', 0),
        'comment': norm.get('comment', ''),
        'keys': json.dumps(norm.get('keys', []), ensure_ascii=False),
        'secondary_keys': json.dumps(norm.get('secondary_keys', []), ensure_ascii=False),
        'content': norm.get('content', ''),
        'constant': 1 if norm.get('constant', False) else 0,
        'selective': 1 if norm.get('selective', True) else 0,
        'selective_logic': norm.get('selective_logic', 0),
        'order': norm.get('order', 100),
        'position': norm.get('position', 0),
        'depth': norm.get('depth', 4),
        'disabled': 1 if norm.get('disabled', False) else 0,
        'probability': norm.get('probability', 100),
        'use_probability': 1 if norm.get('use_probability', True) else 0,
        'sticky': norm.get('sticky'),
        'cooldown': norm.get('cooldown'),
        'delay': norm.get('delay'),
        'exclude_recursion': 1 if norm.get('exclude_recursion', False) else 0,
        'prevent_recursion': 1 if norm.get('prevent_recursion', False) else 0,
        'delay_until_recursion': norm.get('delay_until_recursion', 0),
        'group_name': norm.get('group', ''),
        'group_override': 1 if norm.get('group_override', False) else 0,
        'group_weight': norm.get('group_weight', 100),
        'case_sensitive': 1 if norm.get('case_sensitive') else 0 if norm.get('case_sensitive') is not None else None,
        'match_whole_words': 1 if norm.get('match_whole_words') else 0 if norm.get('match_whole_words') is not None else None,
        'use_group_scoring': 1 if norm.get('use_group_scoring') else 0 if norm.get('use_group_scoring') is not None else None,
        'scan_depth': norm.get('scan_depth'),
        'outlet_name': norm.get('outlet_name', ''),
        'triggers': json.dumps(norm.get('triggers', []), ensure_ascii=False),
        'ignore_budget': 1 if norm.get('ignore_budget', False) else 0,
        'category': norm.get('category', 'lore'),
        'translations': json.dumps(normalize_entry_translations(norm.get('translations')), ensure_ascii=False),
    }


def _entry_from_db(row: sqlite3.Row) -> Dict:
    """将数据库行转换为条目字典"""
    keys = row.keys()
    cat = row['category'] if 'category' in keys else 'lore'
    raw_translations = row['translations'] if 'translations' in keys else '{}'
    return {
        'id': row['id'],
        'uid': row['uid'],
        'comment': row['comment'],
        'keys': json.loads(row['keys']),
        'secondary_keys': json.loads(row['secondary_keys']),
        'content': row['content'],
        'constant': bool(row['constant']),
        'selective': bool(row['selective']),
        'selective_logic': row['selective_logic'],
        'order': row['order'],
        'position': row['position'],
        'depth': row['depth'],
        'disabled': bool(row['disabled']),
        'probability': row['probability'],
        'use_probability': bool(row['use_probability']),
        'sticky': row['sticky'],
        'cooldown': row['cooldown'],
        'delay': row['delay'],
        'exclude_recursion': bool(row['exclude_recursion']),
        'prevent_recursion': bool(row['prevent_recursion']),
        'delay_until_recursion': row['delay_until_recursion'],
        'group': row['group_name'],
        'group_override': bool(row['group_override']),
        'group_weight': row['group_weight'],
        'case_sensitive': bool(row['case_sensitive']) if row['case_sensitive'] is not None else None,
        'match_whole_words': bool(row['match_whole_words']) if row['match_whole_words'] is not None else None,
        'use_group_scoring': bool(row['use_group_scoring']) if row['use_group_scoring'] is not None else None,
        'scan_depth': row['scan_depth'],
        'outlet_name': row['outlet_name'],
        'triggers': json.loads(row['triggers']),
        'ignore_budget': bool(row['ignore_budget']),
        'category': _normalize_category(cat),
        'translations': normalize_entry_translations(raw_translations),
    }


def _insert_entry_row(conn: sqlite3.Connection, world_book_id: int, entry: Dict) -> int:
    data = _entry_to_db(entry)
    cursor = conn.execute("""
        INSERT INTO world_book_entries 
        (world_book_id, uid, comment, keys, secondary_keys, content, constant, selective, 
        selective_logic, "order", position, depth, disabled, probability, use_probability,
        sticky, cooldown, delay, exclude_recursion, prevent_recursion, delay_until_recursion,
        group_name, group_override, group_weight, case_sensitive, match_whole_words,
        use_group_scoring, scan_depth, outlet_name, triggers, ignore_budget, category, translations)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        world_book_id, data['uid'], data['comment'], data['keys'], data['secondary_keys'],
        data['content'], data['constant'], data['selective'], data['selective_logic'],
        data['order'], data['position'], data['depth'], data['disabled'], data['probability'],
        data['use_probability'], data['sticky'], data['cooldown'], data['delay'],
        data['exclude_recursion'], data['prevent_recursion'], data['delay_until_recursion'],
        data['group_name'], data['group_override'], data['group_weight'], data['case_sensitive'],
        data['match_whole_words'], data['use_group_scoring'], data['scan_depth'],
        data['outlet_name'], data['triggers'], data['ignore_budget'], data['category'],
        data['translations']
    ))
    return cursor.lastrowid


def add_entry(world_book_id: int, entry: Dict) -> int:
    """添加条目到世界书"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(uid) as max_uid FROM world_book_entries WHERE world_book_id = ?",
            (world_book_id,)
        ).fetchone()
        next_uid = (row['max_uid'] or 0) + 1
        entry_copy = dict(entry)
        entry_copy['uid'] = next_uid
        entry_id = _insert_entry_row(conn, world_book_id, entry_copy)
        conn.execute(
            "UPDATE world_books SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (world_book_id,)
        )
        conn.commit()
        return entry_id
    finally:
        conn.close()


def add_entries_bulk(world_book_id: int, entries: Sequence[Dict]) -> int:
    """Insert multiple entries into *world_book_id* inside one transaction."""
    if not entries:
        return 0
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(uid) as max_uid FROM world_book_entries WHERE world_book_id = ?",
            (world_book_id,),
        ).fetchone()
        next_uid = (row['max_uid'] or 0) + 1
        count = 0
        for item in entries:
            copy = dict(item)
            copy['uid'] = next_uid
            next_uid += 1
            _insert_entry_row(conn, world_book_id, copy)
            count += 1
        conn.execute(
            "UPDATE world_books SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (world_book_id,),
        )
        conn.commit()
        return count
    finally:
        conn.close()


def update_entry(entry_id: int, entry: Dict) -> bool:
    """更新条目"""
    conn = _get_conn()
    try:
        data = _entry_to_db(entry)
        
        cursor = conn.execute("""
            UPDATE world_book_entries SET
            comment = ?, keys = ?, secondary_keys = ?, content = ?, constant = ?,
            selective = ?, selective_logic = ?, "order" = ?, position = ?, depth = ?,
            disabled = ?, probability = ?, use_probability = ?, sticky = ?, cooldown = ?,
            delay = ?, exclude_recursion = ?, prevent_recursion = ?, delay_until_recursion = ?,
            group_name = ?, group_override = ?, group_weight = ?, case_sensitive = ?,
            match_whole_words = ?, use_group_scoring = ?, scan_depth = ?, outlet_name = ?,
            triggers = ?, ignore_budget = ?, category = ?
            WHERE id = ?
        """, (
            data['comment'], data['keys'], data['secondary_keys'], data['content'], data['constant'],
            data['selective'], data['selective_logic'], data['order'], data['position'], data['depth'],
            data['disabled'], data['probability'], data['use_probability'], data['sticky'], data['cooldown'],
            data['delay'], data['exclude_recursion'], data['prevent_recursion'], data['delay_until_recursion'],
            data['group_name'], data['group_override'], data['group_weight'], data['case_sensitive'],
            data['match_whole_words'], data['use_group_scoring'], data['scan_depth'], data['outlet_name'],
            data['triggers'], data['ignore_budget'], data['category'], entry_id
        ))
        
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def bulk_toggle_entries(
    world_book_id: int,
    *,
    mode: Optional[str] = None,
    entry_ids: Optional[Sequence[int]] = None,
    enabled: Optional[bool] = None,
) -> int:
    """Enable/disable entries in *world_book_id* by preset mode or explicit entry IDs."""
    conn = _get_conn()
    try:
        updated = 0
        if mode == "media_only":
            cur = conn.execute(
                "UPDATE world_book_entries SET disabled = CASE WHEN category = 'media' THEN 0 ELSE 1 END WHERE world_book_id = ?",
                (world_book_id,),
            )
            updated = cur.rowcount
        elif mode == "all_on":
            cur = conn.execute(
                "UPDATE world_book_entries SET disabled = 0 WHERE world_book_id = ?",
                (world_book_id,),
            )
            updated = cur.rowcount
        elif mode == "all_off":
            cur = conn.execute(
                "UPDATE world_book_entries SET disabled = 1 WHERE world_book_id = ?",
                (world_book_id,),
            )
            updated = cur.rowcount
        elif entry_ids is not None and enabled is not None:
            clean_ids = [int(eid) for eid in entry_ids if eid is not None]
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                cur = conn.execute(
                    f"UPDATE world_book_entries SET disabled = ? WHERE world_book_id = ? AND id IN ({placeholders})",
                    [0 if enabled else 1, world_book_id, *clean_ids],
                )
                updated = cur.rowcount
        if updated:
            conn.execute(
                "UPDATE world_books SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (world_book_id,),
            )
        conn.commit()
        return updated
    finally:
        conn.close()


def update_entry_translations(entry_id: int, translations: Any) -> bool:
    """Update only the display `translations` column for *entry_id*, leaving original fields untouched."""
    normalized = json.dumps(normalize_entry_translations(translations), ensure_ascii=False)
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE world_book_entries SET translations = ? WHERE id = ?",
            (normalized, entry_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_entry(entry_id: int) -> bool:
    """删除条目"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM world_book_entries WHERE id = ?",
            (entry_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_entries(world_book_id: int) -> List[Dict]:
    """获取世界书的所有条目"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM world_book_entries WHERE world_book_id = ? ORDER BY uid",
            (world_book_id,)
        ).fetchall()
        return [_entry_from_db(row) for row in rows]
    finally:
        conn.close()


def get_entry(entry_id: int) -> Optional[Dict]:
    """获取单个条目"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM world_book_entries WHERE id = ?",
            (entry_id,)
        ).fetchone()
        
        if not row:
            return None
        
        return _entry_from_db(row)
    finally:
        conn.close()


# ==================== Companion - World Book Association ====================

def associate_companion_with_world_book(companion_id: str, world_book_id: int, is_primary: bool = False) -> bool:
    """关联AI伴侣与世界书"""
    conn = _get_conn()
    try:
        # 如果设为主世界书，先取消其他的主世界书
        if is_primary:
            conn.execute(
                "UPDATE companion_world_books SET is_primary = 0 WHERE companion_id = ?",
                (companion_id,)
            )
        
        conn.execute(
            """INSERT OR REPLACE INTO companion_world_books (companion_id, world_book_id, is_primary)
            VALUES (?, ?, ?)""",
            (companion_id, world_book_id, 1 if is_primary else 0)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_companion_world_book_association(companion_id: str, world_book_id: int) -> bool:
    """移除AI伴侣与世界书的关联"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM companion_world_books WHERE companion_id = ? AND world_book_id = ?",
            (companion_id, world_book_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_companion_world_books(companion_id: str) -> List[Dict]:
    """获取AI伴侣关联的所有世界书"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT wb.*, cwb.is_primary 
            FROM world_books wb
            JOIN companion_world_books cwb ON wb.id = cwb.world_book_id
            WHERE cwb.companion_id = ?
            ORDER BY cwb.is_primary DESC, wb.updated_at DESC
        """, (companion_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_companion_primary_world_book(companion_id: str) -> Optional[Dict]:
    """获取AI伴侣的主世界书"""
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT wb.* FROM world_books wb
            JOIN companion_world_books cwb ON wb.id = cwb.world_book_id
            WHERE cwb.companion_id = ? AND cwb.is_primary = 1
            LIMIT 1
        """, (companion_id,)).fetchone()
        
        if not row:
            return None
        
        return dict(row)
    finally:
        conn.close()


# ==================== Import/Export ====================

def export_world_book_to_json(world_book_id: int) -> Dict:
    """导出世界书为 JSON 格式。"""
    world_book = get_world_book(world_book_id)
    if not world_book:
        return {}
    
    entries = get_entries(world_book_id)
    
    # 转换为通用条目格式
    entries_dict = {}
    for entry in entries:
        entries_dict[str(entry['uid'])] = entry
    
    return {
        'name': world_book['name'],
        'description': world_book['description'],
        'entries': entries_dict
    }


def import_world_book_from_json(data: Dict, name: Optional[str] = None) -> int:
    """从 JSON 导入世界书。"""
    # 处理不同格式
    if 'entries' not in data:
        # 可能是条目列表格式
        entries_data = data if isinstance(data, list) else []
        book_name = name or 'Imported World Book'
        description = ''
    else:
        entries_data = list(data.get('entries', {}).values())
        book_name = name or data.get('name', 'Imported World Book')
        description = data.get('description', '')
    
    # 创建世界书
    world_book_id = create_world_book(book_name, description)
    
    # 导入条目
    for entry_data in entries_data:
        # 格式转换（兼容 key/keys 字段）
        if 'key' in entry_data and 'keys' not in entry_data:
            entry_data['keys'] = entry_data.pop('key')
        if 'keysecondary' in entry_data and 'secondary_keys' not in entry_data:
            entry_data['secondary_keys'] = entry_data.pop('keysecondary')
        
        # 处理selectiveLogic字符串值
        if isinstance(entry_data.get('selective_logic'), str):
            logic_map = {'and_any': 0, 'not_all': 1, 'not_any': 2, 'and_all': 3}
            entry_data['selective_logic'] = logic_map.get(entry_data['selective_logic'], 0)
        
        add_entry(world_book_id, entry_data)
    
    return world_book_id


# ==================== Character Card Lorebook Import & Triage ====================

def _parse_raw_card_obj(raw_card: Any) -> Dict[str, Any]:
    if isinstance(raw_card, dict):
        return raw_card
    if isinstance(raw_card, str) and raw_card.strip():
        try:
            parsed = json.loads(raw_card)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _clean_key_list(raw_keys: Any) -> List[str]:
    if isinstance(raw_keys, str):
        raw_list = [k.strip() for k in raw_keys.split(",")]
    elif isinstance(raw_keys, list):
        raw_list = [str(k or "").strip() for k in raw_keys]
    else:
        return []
    return [k for k in raw_list if k and not k.startswith(_FOLDER_KEY_PREFIX)]


def _raw_entries_from_book(raw_book: Any) -> Tuple[str, str, List[Dict[str, Any]]]:
    if not isinstance(raw_book, dict):
        return "", "", []
    book_name = str(raw_book.get("name") or "").strip()
    book_desc = str(raw_book.get("description") or "").strip()
    raw_entries = raw_book.get("entries")
    items: List[Dict[str, Any]] = []
    if isinstance(raw_entries, list):
        items = [e for e in raw_entries if isinstance(e, dict)]
    elif isinstance(raw_entries, dict):
        items = [e for e in raw_entries.values() if isinstance(e, dict)]
    return book_name, book_desc, items


def classify_character_book_entry(
    entry: Dict[str, Any],
    *,
    protected_html: Iterable[str] = (),
    protected_macros: Iterable[str] = (),
    asset_names: Sequence[str] = (),
) -> str:
    """Classify a card lorebook entry as ``'media'``, ``'rpg'``, or ``'lore'``."""
    comment = str(entry.get("comment") or entry.get("name") or "").strip()
    content = str(entry.get("content") or "").strip()
    if not content:
        return "lore"

    if _STANDARD_MEDIA_SYNTAX_RE.search(content):
        return "media"

    p_html = sorted({str(t).strip().lower() for t in protected_html if str(t).strip()})
    if p_html:
        alt_h = "|".join(re.escape(t) for t in p_html)
        if re.search(rf"""<(?:{alt_h})(?:\s*=|\b[^>]*\bsrc\s*=)""", content, re.IGNORECASE):
            return "media"

    p_macros = sorted({str(m).strip().lower() for m in protected_macros if str(m).strip()})
    if p_macros:
        alt_m = "|".join(re.escape(m) for m in p_macros)
        if re.search(rf"""\{{\{{\s*(?:{alt_m})\s*::""", content, re.IGNORECASE):
            return "media"

    if asset_names:
        low_content = content.lower()
        hits = 0
        seen_names = set()
        for name in asset_names:
            clean = str(name or "").strip()
            low = clean.lower()
            # Only treat composite/suffixed asset keys (e.g. `이벨린_shy`, `scene 10`) as
            # evidence of an asset registry; single words like `shop` or `lucian` appear in prose.
            is_composite_key = ("_" in clean) or bool(re.search(r"\s+\d+$", clean))
            if not is_composite_key or len(clean) < 5 or low in seen_names:
                continue
            seen_names.add(low)
            if low in low_content:
                hits += 1
                if hits >= 2 and not _RPG_SIGNAL_RE.search(comment):
                    return "media"

    if _MEDIA_HEADER_RE.search(comment) and (
        _MEDIA_BODY_HINT_RE.search(content) or not _RPG_SIGNAL_RE.search(comment)
    ):
        return "media"

    if _RPG_SIGNAL_RE.search(comment) or _RPG_SIGNAL_RE.search(content):
        return "rpg"

    return "lore"


def count_character_book_entries(raw_card: Any) -> int:
    """Return the count of non-empty entries in ``character_book``."""
    card = _parse_raw_card_obj(raw_card)
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    _, _, items = _raw_entries_from_book(data.get("character_book"))
    return sum(1 for item in items if str(item.get("content") or "").strip())


def extract_character_book_entries(
    raw_card: Any,
    *,
    char_name: str = "",
    asset_names: Sequence[str] = (),
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Normalize ``character_book`` entries and set default activation by category.

    All non-empty entries are extracted so the user can toggle any of them on
    demand in the Lore panel; only ``'media'`` entries (unless explicitly disabled
    in the source card) default to active (``disabled=False``).
    """
    from .card_presentation import extract_card_presentation, extract_protected_asset_tags

    card = _parse_raw_card_obj(raw_card)
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    book_name, book_desc, items = _raw_entries_from_book(data.get("character_book"))
    if not items:
        return book_name, book_desc, []

    pres = extract_card_presentation(card)
    all_rules = [
        *(pres.get("display_regex") or []),
        *(pres.get("output_regex") or []),
        *(pres.get("prompt_regex") or []),
    ]
    protected_html, protected_macros, _, _ = extract_protected_asset_tags(all_rules)

    effective_name = book_name or (f"{char_name} - Lore" if char_name else "Character Lore")
    normalized: List[Dict[str, Any]] = []
    for idx, raw_item in enumerate(items):
        content = str(raw_item.get("content") or "")
        if not content.strip():
            continue

        keys = _clean_key_list(raw_item.get("keys") if "keys" in raw_item else raw_item.get("key"))
        secondary_keys = _clean_key_list(
            raw_item.get("secondary_keys") if "secondary_keys" in raw_item else raw_item.get("keysecondary")
        )
        comment = str(raw_item.get("comment") or raw_item.get("name") or "").strip()
        if not comment:
            first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
            comment = first_line[:60] if first_line else f"Entry {idx + 1}"

        ext = raw_item.get("extensions") if isinstance(raw_item.get("extensions"), dict) else {}
        category = classify_character_book_entry(
            {"comment": comment, "content": content},
            protected_html=protected_html,
            protected_macros=protected_macros,
            asset_names=asset_names,
        )

        orig_disabled = (
            raw_item.get("enabled") is False
            or bool(raw_item.get("disabled"))
            or bool(raw_item.get("disable"))
        )
        is_disabled = True if orig_disabled else (category != "media")

        constant = bool(raw_item.get("constant", False))
        if category == "media" and not keys:
            constant = True

        selective_raw = raw_item.get("selective")
        selective = bool(selective_raw) if selective_raw is not None else bool(secondary_keys)

        order_raw = raw_item.get("insertion_order", raw_item.get("order", 100))
        try:
            order_val = int(order_raw) if order_raw is not None else 100
        except (ValueError, TypeError):
            order_val = 100

        pos_raw = raw_item.get("position")
        if pos_raw is None:
            pos_raw = ext.get("position", 0)
        depth_raw = raw_item.get("depth")
        if depth_raw is None:
            depth_raw = ext.get("depth", 4)

        prob_raw = raw_item.get("probability")
        if prob_raw is None:
            prob_raw = ext.get("probability", 100)
        use_prob_raw = raw_item.get("use_probability", raw_item.get("useProbability", True))

        normalized.append({
            "comment": comment,
            "keys": keys,
            "secondary_keys": secondary_keys,
            "content": content,
            "constant": constant,
            "selective": selective,
            "selective_logic": raw_item.get("selective_logic", raw_item.get("selectiveLogic", 0)),
            "order": order_val,
            "position": _normalize_position(pos_raw),
            "depth": depth_raw if depth_raw is not None else 4,
            "disabled": is_disabled,
            "probability": _normalize_probability(prob_raw),
            "use_probability": bool(use_prob_raw if use_prob_raw is not None else True),
            "case_sensitive": raw_item.get("case_sensitive", raw_item.get("caseSensitive")),
            "match_whole_words": raw_item.get("match_whole_words", raw_item.get("matchWholeWords")),
            "scan_depth": raw_item.get("scan_depth", raw_item.get("scanDepth")),
            "group": str(raw_item.get("group") or ""),
            "group_weight": int(raw_item.get("group_weight") or 100),
            "category": category,
        })

    return effective_name, book_desc, normalized


def import_character_book_for_companion(
    companion_id: str,
    raw_card: Any,
    *,
    char_name: str = "",
    asset_names: Sequence[str] = (),
    skip_if_linked: bool = True,
) -> Dict[str, Any]:
    """Import a card's embedded ``character_book`` into ``world_books`` for *companion_id*.

    All non-empty entries are persisted; only ``'media'`` entries default to
    enabled while ``'lore'`` and ``'rpg'`` entries start disabled so users opt in
    per entry.
    """
    existing = get_companion_world_books(companion_id)
    if skip_if_linked and existing:
        primary_id = int(existing[0]["id"])
        return {
            "world_book_id": primary_id,
            "imported_count": 0,
            "media_enabled_count": sum(
                1 for e in get_entries(primary_id) if not e.get("disabled") and e.get("category") == "media"
            ),
            "already_linked": True,
        }

    book_name, book_desc, entries = extract_character_book_entries(
        raw_card,
        char_name=char_name,
        asset_names=asset_names,
    )
    if not entries:
        return {
            "world_book_id": None,
            "imported_count": 0,
            "media_enabled_count": 0,
            "already_linked": False,
        }

    if existing and not skip_if_linked:
        world_book_id = int(existing[0]["id"])
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM world_book_entries WHERE world_book_id = ?", (world_book_id,))
            conn.execute(
                "UPDATE world_books SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (book_name, book_desc, world_book_id),
            )
            conn.commit()
        finally:
            conn.close()
        inserted = add_entries_bulk(world_book_id, entries)
        associate_companion_with_world_book(companion_id, world_book_id, is_primary=True)
    else:
        world_book_id = create_world_book(book_name, book_desc)
        inserted = add_entries_bulk(world_book_id, entries)
        associate_companion_with_world_book(companion_id, world_book_id, is_primary=True)
    media_enabled = sum(1 for e in entries if e.get("category") == "media" and not e.get("disabled"))
    return {
        "world_book_id": world_book_id,
        "imported_count": inserted,
        "media_enabled_count": media_enabled,
        "already_linked": False,
    }

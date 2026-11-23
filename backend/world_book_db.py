import sqlite3
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# 复用companion数据库路径
from .paths import DB_PATH


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
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

def _entry_to_db(entry: Dict) -> Dict:
    """将条目字典转换为数据库格式"""
    return {
        'uid': entry.get('uid', 0),
        'comment': entry.get('comment', ''),
        'keys': json.dumps(entry.get('keys', []), ensure_ascii=False),
        'secondary_keys': json.dumps(entry.get('secondary_keys', []), ensure_ascii=False),
        'content': entry.get('content', ''),
        'constant': 1 if entry.get('constant', False) else 0,
        'selective': 1 if entry.get('selective', True) else 0,
        'selective_logic': entry.get('selective_logic', 0),
        'order': entry.get('order', 100),
        'position': entry.get('position', 0),
        'depth': entry.get('depth', 4),
        'disabled': 1 if entry.get('disabled', False) else 0,
        'probability': entry.get('probability', 100),
        'use_probability': 1 if entry.get('use_probability', True) else 0,
        'sticky': entry.get('sticky'),
        'cooldown': entry.get('cooldown'),
        'delay': entry.get('delay'),
        'exclude_recursion': 1 if entry.get('exclude_recursion', False) else 0,
        'prevent_recursion': 1 if entry.get('prevent_recursion', False) else 0,
        'delay_until_recursion': entry.get('delay_until_recursion', 0),
        'group_name': entry.get('group', ''),
        'group_override': 1 if entry.get('group_override', False) else 0,
        'group_weight': entry.get('group_weight', 100),
        'case_sensitive': 1 if entry.get('case_sensitive') else 0 if entry.get('case_sensitive') is not None else None,
        'match_whole_words': 1 if entry.get('match_whole_words') else 0 if entry.get('match_whole_words') is not None else None,
        'use_group_scoring': 1 if entry.get('use_group_scoring') else 0 if entry.get('use_group_scoring') is not None else None,
        'scan_depth': entry.get('scan_depth'),
        'outlet_name': entry.get('outlet_name', ''),
        'triggers': json.dumps(entry.get('triggers', []), ensure_ascii=False),
        'ignore_budget': 1 if entry.get('ignore_budget', False) else 0,
    }


def _entry_from_db(row: sqlite3.Row) -> Dict:
    """将数据库行转换为条目字典"""
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
    }


def add_entry(world_book_id: int, entry: Dict) -> int:
    """添加条目到世界书"""
    conn = _get_conn()
    try:
        # 获取当前最大uid
        row = conn.execute(
            "SELECT MAX(uid) as max_uid FROM world_book_entries WHERE world_book_id = ?",
            (world_book_id,)
        ).fetchone()
        next_uid = (row['max_uid'] or 0) + 1
        entry['uid'] = next_uid
        
        data = _entry_to_db(entry)
        
        cursor = conn.execute("""
            INSERT INTO world_book_entries 
            (world_book_id, uid, comment, keys, secondary_keys, content, constant, selective, 
            selective_logic, "order", position, depth, disabled, probability, use_probability,
            sticky, cooldown, delay, exclude_recursion, prevent_recursion, delay_until_recursion,
            group_name, group_override, group_weight, case_sensitive, match_whole_words,
            use_group_scoring, scan_depth, outlet_name, triggers, ignore_budget)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            world_book_id, data['uid'], data['comment'], data['keys'], data['secondary_keys'],
            data['content'], data['constant'], data['selective'], data['selective_logic'],
            data['order'], data['position'], data['depth'], data['disabled'], data['probability'],
            data['use_probability'], data['sticky'], data['cooldown'], data['delay'],
            data['exclude_recursion'], data['prevent_recursion'], data['delay_until_recursion'],
            data['group_name'], data['group_override'], data['group_weight'], data['case_sensitive'],
            data['match_whole_words'], data['use_group_scoring'], data['scan_depth'],
            data['outlet_name'], data['triggers'], data['ignore_budget']
        ))
        
        # 更新世界书的更新时间
        conn.execute(
            "UPDATE world_books SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (world_book_id,)
        )
        
        conn.commit()
        return cursor.lastrowid
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
            triggers = ?, ignore_budget = ?
            WHERE id = ?
        """, (
            data['comment'], data['keys'], data['secondary_keys'], data['content'], data['constant'],
            data['selective'], data['selective_logic'], data['order'], data['position'], data['depth'],
            data['disabled'], data['probability'], data['use_probability'], data['sticky'], data['cooldown'],
            data['delay'], data['exclude_recursion'], data['prevent_recursion'], data['delay_until_recursion'],
            data['group_name'], data['group_override'], data['group_weight'], data['case_sensitive'],
            data['match_whole_words'], data['use_group_scoring'], data['scan_depth'], data['outlet_name'],
            data['triggers'], data['ignore_budget'], entry_id
        ))
        
        conn.commit()
        return cursor.rowcount > 0
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

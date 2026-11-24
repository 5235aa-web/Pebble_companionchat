import sqlite3
import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 独立数据库路径（与 pebble.db 完全分离）
from . import paths
from .card_presentation import extract_card_presentation
from .paths import DB_PATH


_schema_lock = threading.Lock()
_schema_ready = False


def _has_schema(conn: sqlite3.Connection) -> bool:
    """Cheap probe: are the core tables there?

    All core tables are checked so an existing database that predates new
    tables still gets its schema pass once, instead of silently missing them.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('companions', 'scripts', 'companion_assets')"
    ).fetchone()
    return bool(row) and row[0] == 3


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，首次使用时初始化表。"""
    global _schema_ready
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # The DDL script and its migrations are idempotent but not free: running
    # them on every connection put a full schema pass in front of every query.
    # The cheap probe below keeps that off the hot path while still recovering
    # when the database file is replaced under a running process.
    if not _schema_ready or not _has_schema(conn):
        with _schema_lock:
            if not _schema_ready or not _has_schema(conn):
                _init_tables(conn)
                _schema_ready = True
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    """初始化所有表（幂等，仅首次创建）。"""
    conn.executescript("""
        -- AI伴侣定义表
        CREATE TABLE IF NOT EXISTS companions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Unknown',
            description TEXT DEFAULT '',
            personality TEXT DEFAULT '',
            scenario TEXT DEFAULT '',
            first_mes TEXT DEFAULT '',
            mes_example TEXT DEFAULT '',
            advanced_prompt TEXT DEFAULT '',
            user_info TEXT DEFAULT '',
            creator_notes TEXT DEFAULT '',
            system_prompt TEXT DEFAULT '',
            post_history_instructions TEXT DEFAULT '',
            creator TEXT DEFAULT '',
            character_version TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            alternate_greetings TEXT DEFAULT '[]',
            depth_prompt TEXT DEFAULT '{}',
            avatar_path TEXT DEFAULT '',
            llm_provider_id TEXT DEFAULT '',
            tts_config TEXT DEFAULT '{}',
            raw_card_data TEXT DEFAULT '',
            card_format TEXT DEFAULT '',
            card_ui TEXT DEFAULT '{}',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 对话分支表
        CREATE TABLE IF NOT EXISTS companion_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            branch_name TEXT DEFAULT '新分支',
            parent_branch_id TEXT DEFAULT '',
            fork_from_message_id INTEGER DEFAULT NULL,
            token_usage TEXT DEFAULT '',
            card_vars TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(companion_id, branch_id),
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );

        -- 角色记忆表：由对话自动提取，按角色与分支隔离。
        -- keys 是检索用的关键词（JSON 数组），importance 1-3 决定注入优先级。
        CREATE TABLE IF NOT EXISTS companion_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL DEFAULT 'main',
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            -- 'character' memories hold who they are and are shared across
            -- branches; 'branch' memories hold what happened on this timeline.
            scope TEXT NOT NULL DEFAULT 'branch',
            keys TEXT NOT NULL DEFAULT '[]',
            importance INTEGER NOT NULL DEFAULT 2,
            source_message_ids TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            use_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_memories_owner
            ON companion_memories(companion_id, branch_id);
        CREATE INDEX IF NOT EXISTS idx_memories_used
            ON companion_memories(companion_id, branch_id, last_used_at);

        -- 聊天记录表（独立于现有chat_logs）
        CREATE TABLE IF NOT EXISTS companion_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL DEFAULT 'main',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            swipes TEXT DEFAULT '',
            swipe_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );

        -- 剧本库：与角色无关。一份剧本可被任意角色使用，也可反复编辑。
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            premise TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 剧情节点：线性顺序，order_index 决定推进次序。
        CREATE TABLE IF NOT EXISTS script_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id INTEGER NOT NULL,
            order_index INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL DEFAULT '',
            turns INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (script_id) REFERENCES scripts(id) ON DELETE CASCADE
        );

        -- 一次游玩：绑定剧本、角色、分支；current_index 指向正在演的那一幕。
        CREATE TABLE IF NOT EXISTS script_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id INTEGER NOT NULL,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            current_index INTEGER NOT NULL DEFAULT 0,
            scene_done INTEGER NOT NULL DEFAULT 0,
            turns_done INTEGER NOT NULL DEFAULT 0,
            hud TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'playing',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (script_id) REFERENCES scripts(id) ON DELETE CASCADE,
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_script_nodes_script
            ON script_nodes(script_id, order_index);
        CREATE INDEX IF NOT EXISTS idx_script_sessions_companion
            ON script_sessions(companion_id, status);

        -- 每场演出拿的是开演那一刻的剧本副本（影子剧本）。共享剧本随后
        -- 怎么改都不影响已经在演的剧情，其他角色也一样。
        CREATE TABLE IF NOT EXISTS script_session_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            order_index INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL DEFAULT '',
            turns INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES script_sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_session_nodes_session
            ON script_session_nodes(session_id, order_index);

        -- 剧本分段摘要：每段独立保留，不合并、不进压缩面板。
        -- message_from_id/message_to_id 标出覆盖区间，前端据此原位插入卡片。
        CREATE TABLE IF NOT EXISTS script_scene_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            scene_from INTEGER NOT NULL DEFAULT 0,
            scene_to INTEGER NOT NULL DEFAULT 0,
            scene_titles TEXT DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            message_from_id INTEGER DEFAULT 0,
            message_to_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_scene_summaries_branch
            ON script_scene_summaries(companion_id, branch_id, id);

        -- 索引优化
        CREATE INDEX IF NOT EXISTS idx_messages_companion_branch
            ON companion_messages(companion_id, branch_id);
        CREATE INDEX IF NOT EXISTS idx_messages_created
            ON companion_messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_branches_companion
            ON companion_branches(companion_id);

        -- 压缩历史表
        CREATE TABLE IF NOT EXISTS compaction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            branch_id TEXT NOT NULL DEFAULT 'main',
            compacted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            messages_before INTEGER NOT NULL,
            messages_after INTEGER NOT NULL,
            messages_compacted INTEGER NOT NULL,
            tokens_before INTEGER NOT NULL,
            tokens_after INTEGER NOT NULL,
            tokens_saved INTEGER NOT NULL,
            summary_preview TEXT DEFAULT '',
            summary_full TEXT DEFAULT '',
            compaction_version INTEGER DEFAULT 1,
            preserve_recent INTEGER DEFAULT 10,
            llm_model TEXT DEFAULT '',
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );

        -- 压缩历史索引
        CREATE INDEX IF NOT EXISTS idx_compaction_history_companion_branch
            ON compaction_history(companion_id, branch_id);
        CREATE INDEX IF NOT EXISTS idx_compaction_history_time
            ON compaction_history(compacted_at);

        -- 角色多媒体资产表（CHARX / 手动上传的图片、表情、背景、音乐、音效等）
        CREATE TABLE IF NOT EXISTS companion_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            companion_id TEXT NOT NULL,
            asset_type TEXT NOT NULL DEFAULT 'other',
            name TEXT NOT NULL DEFAULT '',
            ext TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            rel_path TEXT NOT NULL DEFAULT '',
            source_uri TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (companion_id) REFERENCES companions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_companion_assets_owner
            ON companion_assets(companion_id, asset_type, sort_order);

        -- 用户设置表（单行：只存本机用户身份）
        CREATE TABLE IF NOT EXISTS user_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT DEFAULT 'You',
            avatar_path TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 迁移：主分支不再存名字，改由界面按当前语言渲染。
    # Only the exact placeholder is cleared, so a name the user typed is kept.
    try:
        conn.execute(
            "UPDATE companion_branches SET branch_name = '' "
            "WHERE branch_id = 'main' AND branch_name = '主对话'"
        )
    except sqlite3.Error:
        pass

    # 迁移：为旧表添加 swipes 和 swipe_id 列
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(companion_messages)").fetchall()]
        if 'swipes' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN swipes TEXT DEFAULT ''")
        if 'swipe_id' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN swipe_id INTEGER DEFAULT 0")
        # 新增字段：消息类型和元数据
        if 'message_type' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN message_type TEXT DEFAULT 'chat'")
        if 'metadata' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN metadata TEXT DEFAULT '{}'")
        if 'is_important' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN is_important INTEGER DEFAULT 0")
        # Compaction no longer deletes messages: they are flagged and grouped
        # into a batch so the UI can still show them on demand.
        if 'is_compacted' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN is_compacted INTEGER DEFAULT 0")
        if 'compaction_batch_id' not in columns:
            conn.execute("ALTER TABLE companion_messages ADD COLUMN compaction_batch_id TEXT DEFAULT ''")
    except Exception as e:
        logger.warning(f"Migration warning (can be ignored if table is new): {e}")

    # 迁移：为 companions 表添加 advanced_prompt 列
    try:
        companion_columns = [row[1] for row in conn.execute("PRAGMA table_info(companions)").fetchall()]
        if 'advanced_prompt' not in companion_columns:
            conn.execute("ALTER TABLE companions ADD COLUMN advanced_prompt TEXT DEFAULT ''")
        if 'user_info' not in companion_columns:
            conn.execute("ALTER TABLE companions ADD COLUMN user_info TEXT DEFAULT ''")
        if 'pinned' not in companion_columns:
            conn.execute("ALTER TABLE companions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if 'card_ui' not in companion_columns:
            conn.execute("ALTER TABLE companions ADD COLUMN card_ui TEXT DEFAULT '{}'")
        if 'card_format' not in companion_columns:
            conn.execute("ALTER TABLE companions ADD COLUMN card_format TEXT DEFAULT ''")
            # Backfill existing CHARX imports from extracted assets or CCv3 embedded asset URIs.
            conn.execute("""
                UPDATE companions
                SET card_format = 'charx'
                WHERE card_format = ''
                  AND (
                    EXISTS (SELECT 1 FROM companion_assets WHERE companion_id = companions.id)
                    OR avatar_path LIKE 'companion_assets/%'
                    OR raw_card_data LIKE '%embeded://%'
                    OR raw_card_data LIKE '%embedded://%'
                  )
            """)
    except Exception as e:
        logger.warning(f"Migration warning for companions table: {e}")

    # 迁移：分支记录记忆提取游标（最后一条已扫描的消息 id）与单人对话 HUD 状态
    try:
        branch_columns = [row[1] for row in conn.execute("PRAGMA table_info(companion_branches)").fetchall()]
        if 'memory_cursor' not in branch_columns:
            conn.execute("ALTER TABLE companion_branches ADD COLUMN memory_cursor INTEGER NOT NULL DEFAULT 0")
        if 'card_hud' not in branch_columns:
            conn.execute("ALTER TABLE companion_branches ADD COLUMN card_hud TEXT DEFAULT '{}'")
        if 'card_hud_enabled' not in branch_columns:
            conn.execute("ALTER TABLE companion_branches ADD COLUMN card_hud_enabled INTEGER DEFAULT NULL")
        if 'token_usage' not in branch_columns:
            conn.execute("ALTER TABLE companion_branches ADD COLUMN token_usage TEXT DEFAULT ''")
        if 'card_vars' not in branch_columns:
            conn.execute("ALTER TABLE companion_branches ADD COLUMN card_vars TEXT DEFAULT ''")
    except Exception as e:
        logger.warning(f"Migration warning for companion_branches table: {e}")

    # 迁移：记忆作用域（character = 跨分支共享，branch = 本分支独有）
    try:
        memory_columns = [row[1] for row in conn.execute("PRAGMA table_info(companion_memories)").fetchall()]
        if 'scope' not in memory_columns:
            conn.execute("ALTER TABLE companion_memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'branch'")
    except Exception as e:
        logger.warning(f"Migration warning for companion_memories table: {e}")

    # 迁移：剧本节点计划轮数（0 表示不限）
    try:
        node_columns = [row[1] for row in conn.execute("PRAGMA table_info(script_nodes)").fetchall()]
        if 'turns' not in node_columns:
            conn.execute("ALTER TABLE script_nodes ADD COLUMN turns INTEGER NOT NULL DEFAULT 0")
    except Exception as e:
        logger.warning(f"Migration warning for script_nodes table: {e}")

    # 迁移：当前幕已用轮数，推进时归零
    try:
        session_columns = [row[1] for row in conn.execute("PRAGMA table_info(script_sessions)").fetchall()]
        if 'turns_done' not in session_columns:
            conn.execute("ALTER TABLE script_sessions ADD COLUMN turns_done INTEGER NOT NULL DEFAULT 0")
    except Exception as e:
        logger.warning(f"Migration warning for script_sessions table: {e}")

    conn.commit()


# ==================== Companion CRUD ====================

def create_companion(
    companion_id: str,
    fields: Dict,
    avatar_path: Optional[str] = None,
    raw_card_data: str = "",
    card_format: str = "",
) -> bool:
    """创建新的AI伴侣。"""
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO companions (
                id, name, description, personality, scenario, first_mes,
                mes_example, advanced_prompt, user_info, creator_notes, system_prompt, post_history_instructions,
                creator, character_version, tags, alternate_greetings, depth_prompt,
                avatar_path, raw_card_data, card_format, tts_config
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            companion_id,
            fields.get('name', 'Unknown'),
            fields.get('description', ''),
            fields.get('personality', ''),
            fields.get('scenario', ''),
            fields.get('first_mes', ''),
            fields.get('mes_example', ''),
            fields.get('advanced_prompt', ''),
            fields.get('user_info', ''),
            fields.get('creator_notes', ''),
            fields.get('system_prompt', ''),
            fields.get('post_history_instructions', ''),
            fields.get('creator', ''),
            fields.get('character_version', ''),
            json.dumps(fields.get('tags', []), ensure_ascii=False),
            json.dumps(fields.get('alternate_greetings', []), ensure_ascii=False),
            json.dumps(fields.get('depth_prompt', {}), ensure_ascii=False),
            avatar_path or '',
            raw_card_data,
            card_format or '',
            json.dumps(fields.get('tts_config', {}), ensure_ascii=False),
        ))
        # 自动创建main分支
        # The name is left empty on purpose: the UI renders its own label for
        # the main branch in the active language, so storing text here would
        # freeze it to whichever language created the companion.
        conn.execute("""
            INSERT INTO companion_branches (companion_id, branch_id, branch_name)
            VALUES (?, 'main', '')
        """, (companion_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_companion(companion_id: str) -> Optional[Dict]:
    """获取单个AI伴侣详情。"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM companions WHERE id = ?", (companion_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        # 解析JSON字段
        data['tags'] = json.loads(data.get('tags', '[]'))
        data['alternate_greetings'] = json.loads(data.get('alternate_greetings', '[]'))
        data['depth_prompt'] = json.loads(data.get('depth_prompt', '{}'))
        data['tts_config'] = json.loads(data.get('tts_config', '{}'))
        try:
            data['card_ui'] = json.loads(data.get('card_ui') or '{}')
        except (ValueError, TypeError):
            data['card_ui'] = {}
        data['card_presentation'] = extract_card_presentation(
            data.get('raw_card_data', ''), data['card_ui']
        )
        return data
    finally:
        conn.close()


def get_all_companions() -> List[Dict]:
    """获取所有AI伴侣列表（含该角色最新一条聊天消息预览）。"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT c.id, c.name, c.description, c.personality, c.scenario,
                   c.first_mes, c.mes_example, c.creator_notes, c.system_prompt,
                   c.post_history_instructions, c.creator, c.character_version,
                   c.tags, c.alternate_greetings, c.depth_prompt, c.avatar_path,
                   c.llm_provider_id, c.tts_config, c.created_at, c.updated_at,
                   c.advanced_prompt, c.user_info, c.pinned, c.card_format,
                   (SELECT COUNT(*) FROM companion_assets
                    WHERE companion_id = c.id) AS asset_count,
                   (SELECT content FROM companion_messages
                    WHERE companion_id = c.id
                    ORDER BY id DESC LIMIT 1) AS last_message
            FROM companions c
            -- Pinned first, then newest; both keep a stable order for the cast
            -- drawer and for the pendant strip, which simply takes the head.
            ORDER BY c.pinned DESC, c.created_at DESC
        """).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data['tags'] = json.loads(data.get('tags', '[]'))
            data['alternate_greetings'] = json.loads(data.get('alternate_greetings', '[]'))
            data['tts_config'] = json.loads(data.get('tts_config', '{}'))
            result.append(data)
        return result
    finally:
        conn.close()


def delete_companion(companion_id: str) -> bool:
    """删除AI伴侣及其所有聊天记录、分支与磁盘资产。"""
    conn = _get_conn()
    avatar_rel = ""
    try:
        row = conn.execute("SELECT avatar_path FROM companions WHERE id = ?", (companion_id,)).fetchone()
        if row:
            avatar_rel = str(row["avatar_path"] or "")
        conn.execute("DELETE FROM companion_messages WHERE companion_id = ?", (companion_id,))
        conn.execute("DELETE FROM companion_branches WHERE companion_id = ?", (companion_id,))
        conn.execute("DELETE FROM companion_assets WHERE companion_id = ?", (companion_id,))
        cursor = conn.execute("DELETE FROM companions WHERE id = ?", (companion_id,))
        conn.commit()
        deleted = cursor.rowcount > 0
    finally:
        conn.close()

    if deleted:
        # Clean up disk assets belonging to this companion without risking anything outside DATA_ROOT.
        try:
            assets_root = paths.COMPANION_ASSETS_DIR.resolve()
            comp_dir = (assets_root / companion_id).resolve()
            if comp_dir.is_relative_to(assets_root) and comp_dir != assets_root and comp_dir.exists():
                shutil.rmtree(comp_dir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Failed to clean companion assets dir for %s: %s", companion_id, exc)
        if avatar_rel and avatar_rel.startswith("uploads/companion_avatars/"):
            try:
                avatars_root = paths.AVATAR_DIR.resolve()
                avatar_file = (paths.DATA_ROOT.resolve() / avatar_rel).resolve()
                if avatar_file.is_relative_to(avatars_root) and avatar_file.is_file():
                    avatar_file.unlink()
            except Exception as exc:
                logger.warning("Failed to clean companion avatar %s: %s", avatar_rel, exc)
    return deleted


def update_companion(companion_id: str, fields: Dict) -> bool:
    """更新AI伴侣字段（部分更新）。"""
    conn = _get_conn()
    allowed = {
        'name', 'description', 'personality', 'scenario', 'first_mes',
        'mes_example', 'advanced_prompt', 'user_info', 'creator_notes', 'system_prompt', 'post_history_instructions',
        'creator', 'character_version', 'tags', 'alternate_greetings',
        'depth_prompt', 'avatar_path', 'llm_provider_id', 'tts_config',
        'pinned', 'card_ui',
    }
    set_parts = []
    values = []
    for key, val in fields.items():
        if key in allowed:
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            elif key == 'pinned':
                # Stored as INTEGER; keep the coercion explicit so a bool never
                # lands in the column as the literal string "True".
                val = 1 if val else 0
            set_parts.append(f"{key} = ?")
            values.append(val)
    if not set_parts:
        return False
    set_parts.append("updated_at = CURRENT_TIMESTAMP")
    values.append(companion_id)
    try:
        conn.execute(f"UPDATE companions SET {', '.join(set_parts)} WHERE id = ?", values)
        conn.commit()
        return True
    finally:
        conn.close()


# ==================== Companion Assets CRUD ====================

def add_companion_assets(companion_id: str, assets: List[Dict]) -> List[int]:
    """批量写入角色多媒体资产记录，返回新插入的 id 列表。"""
    if not assets:
        return []
    conn = _get_conn()
    ids: List[int] = []
    try:
        for idx, item in enumerate(assets):
            cur = conn.execute(
                """
                INSERT INTO companion_assets (
                    companion_id, asset_type, name, ext, mime_type,
                    rel_path, source_uri, size_bytes, sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    companion_id,
                    str(item.get('asset_type') or 'other'),
                    str(item.get('name') or ''),
                    str(item.get('ext') or ''),
                    str(item.get('mime_type') or ''),
                    str(item.get('rel_path') or ''),
                    str(item.get('source_uri') or ''),
                    int(item.get('size_bytes') or 0),
                    int(item.get('sort_order', idx)),
                ),
            )
            if cur.lastrowid is not None:
                ids.append(int(cur.lastrowid))
        conn.commit()
        return ids
    finally:
        conn.close()


def add_companion_asset(companion_id: str, asset: Dict) -> int:
    """写入单条角色多媒体资产记录。"""
    ids = add_companion_assets(companion_id, [asset])
    return ids[0] if ids else 0


def get_companion_assets(companion_id: str, asset_type: Optional[str] = None) -> List[Dict]:
    """按顺序获取指定角色的多媒体资产列表。"""
    conn = _get_conn()
    try:
        if asset_type:
            rows = conn.execute(
                """
                SELECT * FROM companion_assets
                WHERE companion_id = ? AND asset_type = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (companion_id, asset_type),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM companion_assets
                WHERE companion_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (companion_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_companion_asset(companion_id: str, asset_id: int) -> Optional[Dict]:
    """获取单条角色资产记录。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM companion_assets WHERE id = ? AND companion_id = ?",
            (asset_id, companion_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_companion_asset(companion_id: str, asset_id: int) -> Optional[Dict]:
    """删除单条角色资产记录并返回被删除的行（供调用方清理磁盘文件）。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM companion_assets WHERE id = ? AND companion_id = ?",
            (asset_id, companion_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "DELETE FROM companion_assets WHERE id = ? AND companion_id = ?",
            (asset_id, companion_id),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


# ==================== Branch CRUD ====================

def create_branch(companion_id: str, branch_name: str = "新分支", fork_from_message_id: Optional[int] = None) -> Optional[str]:
    """创建新对话分支，返回branch_id。"""
    import uuid
    conn = _get_conn()
    try:
        branch_id = f"branch_{uuid.uuid4().hex[:8]}"
        conn.execute("""
            INSERT INTO companion_branches (companion_id, branch_id, branch_name, fork_from_message_id)
            VALUES (?, ?, ?, ?)
        """, (companion_id, branch_id, branch_name, fork_from_message_id))
        
        # 如果指定了分叉点，复制分叉点之前的消息
        if fork_from_message_id:
            rows = conn.execute(
                "SELECT role, content FROM companion_messages WHERE companion_id = ? AND id <= ? ORDER BY id",
                (companion_id, fork_from_message_id)
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO companion_messages (companion_id, branch_id, role, content) VALUES (?, ?, ?, ?)",
                    (companion_id, branch_id, row['role'], row['content'])
                )
        
        conn.commit()
        return branch_id
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def update_branch_name(companion_id: str, branch_id: str, branch_name: str) -> bool:
    """Rename a branch. The main branch can be renamed but never deleted."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE companion_branches SET branch_name = ? WHERE companion_id = ? AND branch_id = ?",
            (branch_name, companion_id, branch_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_branches(companion_id: str) -> List[Dict]:
    """获取指定AI的所有对话分支，附带每个分支的消息条数。

    条数用子查询而不是 JOIN + GROUP BY：分支只有几条到几十条，子查询的开销
    可以忽略，而 GROUP BY 要求把 b.* 的每一列都写进分组条件，改表结构时容易漏。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT b.*,
                      (SELECT COUNT(*) FROM companion_messages m
                       WHERE m.companion_id = b.companion_id
                         AND m.branch_id = b.branch_id) AS message_count
               FROM companion_branches b
               WHERE b.companion_id = ?
               ORDER BY b.created_at""",
            (companion_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_branch_hud(companion_id: str, branch_id: str = "main") -> Dict:
    """Read single-chat card HUD state & enablement for one branch."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT card_hud, card_hud_enabled FROM companion_branches WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        ).fetchone()
        asset_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM companion_assets WHERE companion_id = ?",
            (companion_id,),
        ).fetchone()
        has_assets = bool(asset_row and int(asset_row["cnt"] or 0) > 0)

        raw_hud = (row["card_hud"] if row else "{}") or "{}"
        try:
            hud = json.loads(raw_hud)
            if not isinstance(hud, dict):
                hud = {}
        except (ValueError, TypeError):
            hud = {}

        raw_enabled = row["card_hud_enabled"] if row else None
        enabled: Optional[bool] = None if raw_enabled is None else bool(raw_enabled)
        effective_enabled = has_assets if enabled is None else enabled
        return {
            "hud": hud,
            "enabled": enabled,
            "effective_enabled": effective_enabled,
            "has_assets": has_assets,
        }
    finally:
        conn.close()


def update_branch_hud(
    companion_id: str,
    branch_id: str = "main",
    hud: Optional[Dict] = None,
    enabled: Optional[bool] = None,
    *,
    clear_enabled: bool = False,
) -> Dict:
    """Update branch card HUD values and/or explicit enablement flag."""
    conn = _get_conn()
    try:
        sets = []
        vals: List = []
        if hud is not None:
            sets.append("card_hud = ?")
            vals.append(json.dumps(hud, ensure_ascii=False))
        if clear_enabled:
            sets.append("card_hud_enabled = NULL")
        elif enabled is not None:
            sets.append("card_hud_enabled = ?")
            vals.append(1 if enabled else 0)
        if sets:
            vals.extend([companion_id, branch_id])
            conn.execute(
                f"UPDATE companion_branches SET {', '.join(sets)} WHERE companion_id = ? AND branch_id = ?",
                vals,
            )
            conn.commit()
    finally:
        conn.close()
    return get_branch_hud(companion_id, branch_id)


def get_branch_token_usage(companion_id: str, branch_id: str = "main") -> Optional[Dict]:
    """Return persisted provider token usage for *branch_id*, or None if cleared/absent.

    When ``token_usage`` has never been populated (e.g. branches created before
    persistence existed), fall back to the active swipe's ``generation`` record
    on the trailing active assistant message so pre-existing threads stay aligned
    with their reply footer.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT token_usage FROM companion_branches WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        ).fetchone()
        raw = (row["token_usage"] if row else "") or ""
        if raw:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                if data.get("cleared"):
                    return None
                prompt = int(data.get("prompt_tokens") or 0)
                completion = int(data.get("completion_tokens") or 0)
                total = int(data.get("total_tokens") or 0)
                if total > 0 or prompt > 0:
                    return {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": total,
                        "recorded_at": float(data.get("recorded_at") or 0),
                    }
            return None

        last = conn.execute(
            """
            SELECT role, swipe_id, metadata
            FROM companion_messages
            WHERE companion_id = ? AND branch_id = ? AND is_compacted = 0
            ORDER BY id DESC LIMIT 1
            """,
            (companion_id, branch_id),
        ).fetchone()
        if not last or last["role"] != "assistant":
            return None
        meta = _load_metadata(last["metadata"])
        slots = meta.get("generation")
        swipe_id = int(last["swipe_id"] or 0)
        if not isinstance(slots, list) or not (0 <= swipe_id < len(slots)):
            return None
        gen = slots[swipe_id]
        if not isinstance(gen, dict) or gen.get("tokens_source") != "usage":
            return None
        prompt = int(gen.get("prompt_tokens") or 0)
        completion = int(gen.get("completion_tokens") or 0)
        total = int(gen.get("context_tokens") or gen.get("total_tokens") or 0)
        if total <= 0 and prompt > 0:
            total = prompt + completion
        if total <= 0 and prompt <= 0:
            return None
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "recorded_at": 0.0,
        }
    finally:
        conn.close()


def set_branch_token_usage(
    companion_id: str,
    branch_id: str,
    usage: Optional[Dict],
) -> None:
    """Persist measured provider usage on the branch row (or mark it cleared)."""
    payload = (
        json.dumps({"cleared": True}, ensure_ascii=False)
        if usage is None
        else json.dumps(usage, ensure_ascii=False)
    )
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE companion_branches SET token_usage = ? WHERE companion_id = ? AND branch_id = ?",
            (payload, companion_id, branch_id),
        )
        conn.commit()
    finally:
        conn.close()


def is_last_active_message(companion_id: str, branch_id: str, message_id: int) -> bool:
    """Return True if *message_id* is the trailing non-compacted message on *branch_id*."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT id FROM companion_messages
            WHERE companion_id = ? AND branch_id = ? AND is_compacted = 0
            ORDER BY id DESC LIMIT 1
            """,
            (companion_id, branch_id),
        ).fetchone()
        return bool(row and int(row["id"]) == int(message_id))
    finally:
        conn.close()


def _load_card_defaults_for_companion(conn: sqlite3.Connection, companion_id: str) -> Dict[str, str]:
    row = conn.execute(
        "SELECT raw_card_data, card_ui FROM companions WHERE id = ?",
        (companion_id,),
    ).fetchone()
    if not row:
        return {}
    pres = extract_card_presentation(row["raw_card_data"], row["card_ui"])
    return dict(pres.get("default_variables") or {})


def get_branch_variables(companion_id: str, branch_id: str = "main") -> Dict[str, Any]:
    """Return card default variables and resolved active variables for *branch_id*."""
    conn = _get_conn()
    try:
        defaults = _load_card_defaults_for_companion(conn, companion_id)
        row = conn.execute(
            "SELECT card_vars FROM companion_branches WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        ).fetchone()
        raw_vars = (row["card_vars"] if row else "") or ""
        merged = dict(defaults)
        if raw_vars:
            try:
                stored = json.loads(raw_vars)
            except (ValueError, TypeError):
                stored = None
            if isinstance(stored, dict):
                # When stored dict explicitly exists, merge over defaults so new
                # card defaults still show up while user overrides win.
                for k, v in stored.items():
                    key = str(k).strip()
                    if key:
                        merged[key] = "" if v is None else str(v)
        return {"defaults": defaults, "variables": merged}
    finally:
        conn.close()


def set_branch_variables(
    companion_id: str,
    branch_id: str,
    variables: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist branch-scoped CBS variables and return the updated state."""
    clean = {
        str(k).strip(): ("" if v is None else str(v))
        for k, v in (variables or {}).items()
        if str(k).strip()
    }
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE companion_branches SET card_vars = ? WHERE companion_id = ? AND branch_id = ?",
            (json.dumps(clean, ensure_ascii=False), companion_id, branch_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_branch_variables(companion_id, branch_id)


def reset_branch_variables(companion_id: str, branch_id: str = "main") -> Dict[str, Any]:
    """Clear branch variable overrides so defaults from the card apply again."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE companion_branches SET card_vars = '' WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_branch_variables(companion_id, branch_id)


def delete_branch(companion_id: str, branch_id: str) -> bool:
    """删除指定分支及其消息。"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM companion_messages WHERE companion_id = ? AND branch_id = ?", (companion_id, branch_id))
        cursor = conn.execute("DELETE FROM companion_branches WHERE companion_id = ? AND branch_id = ?", (companion_id, branch_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ==================== Message CRUD ====================

def save_message(
    companion_id: str, 
    branch_id: str, 
    role: str, 
    content: str,
    message_type: str = "chat",
    metadata: Optional[Dict] = None,
    is_important: bool = False,
) -> int:
    """
    保存一条消息，返回消息ID。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        role: 消息角色 (user/assistant/system)
        content: 消息内容
        message_type: 消息类型 (chat/compaction_summary/system_instruction)
        metadata: 元数据字典
        is_important: 是否为重要消息（不会被压缩）
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO companion_messages 
               (companion_id, branch_id, role, content, message_type, metadata, is_important) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (companion_id, branch_id, role, content, message_type, 
             json.dumps(metadata or {}, ensure_ascii=False), 
             1 if is_important else 0)
        )
        conn.commit()
        result = cursor.lastrowid
        return result if result is not None else 0
    finally:
        conn.close()


def get_messages(companion_id: str, branch_id: str = "main", include_swipes: bool = False, include_compacted: bool = False) -> List[Dict]:
    """
    获取指定分支的聊天上下文。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        include_swipes: 是否包含swipe版本数据
        include_compacted: 是否包含已被压缩的消息。默认排除，因为它们
            不再参与上下文，也不应进入聊天渲染。
    
    Returns:
        消息列表，每条消息包含 id, role, content, message_type, metadata, is_important 等字段
    """
    conn = _get_conn()
    try:
        compacted_filter = "" if include_compacted else " AND is_compacted = 0"
        if include_swipes:
            rows = conn.execute(
                f"""SELECT id, role, content, swipes, swipe_id, created_at, 
                          message_type, metadata, is_important 
                   FROM companion_messages 
                   WHERE companion_id = ? AND branch_id = ?{compacted_filter}
                   ORDER BY id""",
                (companion_id, branch_id)
            ).fetchall()
            result = []
            for row in rows:
                data = dict(row)
                if data.get('swipes'):
                    data['swipes'] = json.loads(data['swipes'])
                else:
                    data['swipes'] = [data['content']]
                # 解析 metadata JSON
                if data.get('metadata'):
                    data['metadata'] = json.loads(data['metadata'])
                else:
                    data['metadata'] = {}
                result.append(data)
            return result
        else:
            rows = conn.execute(
                f"""SELECT id, role, content, created_at, 
                          message_type, metadata, is_important 
                   FROM companion_messages 
                   WHERE companion_id = ? AND branch_id = ?{compacted_filter}
                   ORDER BY id""",
                (companion_id, branch_id)
            ).fetchall()
            result = []
            for row in rows:
                data = dict(row)
                # 解析 metadata JSON
                if data.get('metadata'):
                    data['metadata'] = json.loads(data['metadata'])
                else:
                    data['metadata'] = {}
                result.append(data)
            return result
    finally:
        conn.close()


def get_message_count(companion_id: str, branch_id: str = "main") -> int:
    """获取指定分支的可见消息数量（不含已压缩的消息）。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM companion_messages WHERE companion_id = ? AND branch_id = ? AND is_compacted = 0",
            (companion_id, branch_id)
        ).fetchone()
        return row['cnt'] if row else 0
    finally:
        conn.close()


def delete_message(companion_id: str, branch_id: str, message_id: int) -> bool:
    """删除指定消息。"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM companion_messages WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (companion_id, branch_id, message_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_messages_after(companion_id: str, branch_id: str, message_id: int) -> int:
    """删除某条消息之后的所有消息（不含它本身）。返回删除条数。

    编辑并重发时需要它：改过的消息之后的问答都属于旧时间线，留着会让模型
    看到互相矛盾的两段对话。
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM companion_messages "
            "WHERE companion_id = ? AND branch_id = ? AND id > ?",
            (companion_id, branch_id, message_id),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def search_messages(companion_id: str, branch_id: str, query: str, limit: int = 20) -> List[Dict]:
    """
    在指定分支的消息内容中搜索关键词（不区分大小写）。
    
    Returns:
        匹配的消息列表，每条包含 id, role, content, created_at
    """
    conn = _get_conn()
    try:
        like_pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT id, role, content, created_at
               FROM companion_messages
               WHERE companion_id = ? AND branch_id = ?
                 AND content LIKE ?
               ORDER BY id
               LIMIT ?""",
            (companion_id, branch_id, like_pattern, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_branch_image_attachments(companion_id: str, branch_id: str = "main") -> List[Dict]:
    """
    汇总该分支引用过的图片与视频附件，按消息从新到旧排列。

    已压缩的消息也在范围内：压缩只折叠上下文，媒体文件仍然留在磁盘上，
    记录面板若把它们排除掉，这些文件就成了无法删除的孤儿。

    按发送先后顺序去重：同一分支内首次出现的同名媒体进入画廊，
    后续再次发送的同名文件不再重复收录（通用剪贴板名称 image.png 等则校验尺寸与大小）。
    """
    from .attachment_parser import collect_gallery_media

    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, metadata, created_at FROM companion_messages
               WHERE companion_id = ? AND branch_id = ?
                 AND (metadata LIKE ? OR metadata LIKE ?)
               ORDER BY id ASC""",
            (companion_id, branch_id, '%"image"%', '%"video"%')
        ).fetchall()
    finally:
        conn.close()

    batches = []
    for row in rows:
        raw = row["metadata"]
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            continue
        batches.append((row["id"], row["created_at"], meta.get("attachments") or []))
    return collect_gallery_media(batches)


def get_attachments_by_url(url: str) -> List[Dict]:
    """Return all attachment dicts matching *url* across companion messages."""
    if not url:
        return []
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT metadata FROM companion_messages WHERE metadata LIKE ?",
            (f"%{url}%",),
        ).fetchall()
    finally:
        conn.close()
    found: List[Dict] = []
    for row in rows:
        raw = row["metadata"]
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for att in meta.get("attachments") or []:
            if isinstance(att, dict) and att.get("url") == url:
                found.append(att)
    return found


def remove_attachment_reference(url: str) -> int:
    """
    删除所有分支、所有角色中指向该 url 的 metadata 引用，返回更新的消息条数。

    正文里的 `[图片附件: 名称]` 标记保持不动：那是当时对话的一部分，
    只有 metadata 才是活引用。清掉引用后，记录面板就不会再列出已经删掉的文件。
    """
    if not url:
        return 0

    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, metadata FROM companion_messages WHERE metadata LIKE ?",
            (f"%{url}%",)
        ).fetchall()

        updated = 0
        for row in rows:
            raw = row["metadata"]
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except (TypeError, ValueError):
                continue
            attachments = meta.get("attachments")
            if not isinstance(attachments, list):
                continue

            kept = [
                a for a in attachments
                if not (isinstance(a, dict) and a.get("url") == url)
            ]
            if len(kept) == len(attachments):
                continue

            if kept:
                meta["attachments"] = kept
            else:
                meta.pop("attachments", None)

            conn.execute(
                "UPDATE companion_messages SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), row["id"])
            )
            updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()


def clear_branch_messages(companion_id: str, branch_id: str) -> int:
    """清空指定分支的所有消息，返回删除的消息数量。"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM companion_messages WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def replace_branch_messages(companion_id: str, branch_id: str, messages: List[Dict]) -> bool:
    """
    替换分支的所有消息（用于压缩后更新）。
    先清空，再插入新消息。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        messages: 新消息列表，每条消息可包含 role, content, message_type, metadata, is_important
    
    Returns:
        是否成功
    """
    conn = _get_conn()
    try:
        # 先清空
        conn.execute(
            "DELETE FROM companion_messages WHERE companion_id = ? AND branch_id = ?",
            (companion_id, branch_id)
        )
        
        # 插入新消息
        for msg in messages:
            swipes = msg.get("swipes")
            if isinstance(swipes, list):
                swipes_blob = json.dumps(swipes, ensure_ascii=False)
            elif isinstance(swipes, str):
                swipes_blob = swipes
            else:
                swipes_blob = ""
            
            # Preserve the original timestamp so a compaction does not collapse
            # the whole timeline onto the moment it ran.
            created_at = msg.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            conn.execute(
                """INSERT INTO companion_messages 
                   (companion_id, branch_id, role, content, swipes, swipe_id,
                    message_type, metadata, is_important, created_at) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (companion_id, branch_id, 
                 msg.get("role", ""), 
                 msg.get("content", ""),
                 swipes_blob,
                 int(msg.get("swipe_id") or 0),
                 msg.get("message_type", "chat"),
                 json.dumps(msg.get("metadata", {}), ensure_ascii=False),
                 1 if msg.get("is_important", False) else 0,
                 created_at)
            )
        
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to replace messages: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def save_message_with_swipes(
    companion_id: str, 
    branch_id: str, 
    role: str, 
    content: str, 
    swipes: List[str] = None, 
    swipe_id: int = 0,
    message_type: str = "chat",
    metadata: Optional[Dict] = None,
    is_important: bool = False,
) -> int:
    """
    保存一条带swipe版本的消息，返回消息ID。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        role: 消息角色
        content: 消息内容
        swipes: swipe版本列表
        swipe_id: 当前swipe索引
        message_type: 消息类型
        metadata: 元数据字典
        is_important: 是否为重要消息
    """
    if swipes is None:
        swipes = [content]
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO companion_messages 
               (companion_id, branch_id, role, content, swipes, swipe_id, message_type, metadata, is_important) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (companion_id, branch_id, role, content, 
             json.dumps(swipes, ensure_ascii=False), swipe_id,
             message_type, json.dumps(metadata or {}, ensure_ascii=False),
             1 if is_important else 0)
        )
        conn.commit()
        result = cursor.lastrowid
        return result if result is not None else 0
    finally:
        conn.close()


def update_message_swipes(companion_id: str, branch_id: str, message_id: int, content: str, swipes: List[str], swipe_id: int) -> bool:
    """更新消息的swipe版本数据。"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE companion_messages SET content = ?, swipes = ?, swipe_id = ? WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (content, json.dumps(swipes, ensure_ascii=False), swipe_id, companion_id, branch_id, message_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def add_swipe_to_message(
    companion_id: str,
    branch_id: str,
    message_id: int,
    new_content: str,
    generation: Optional[Dict] = None,
) -> Optional[Dict]:
    """为消息添加一个新的swipe版本，并切换到该版本。返回更新后的消息数据。

    *generation* is stored at the new variant's index, so each regenerated
    reply keeps its own model, duration and token count.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT content, swipes, swipe_id, metadata FROM companion_messages WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (companion_id, branch_id, message_id)
        ).fetchone()
        if not row:
            return None
        
        swipes = json.loads(row['swipes']) if row['swipes'] else [row['content']]
        swipes.append(new_content)
        new_swipe_id = len(swipes) - 1

        meta = _metadata_with_generation(_load_metadata(row['metadata']), new_swipe_id, generation)
        
        conn.execute(
            "UPDATE companion_messages SET content = ?, swipes = ?, swipe_id = ?, metadata = ? "
            "WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (new_content, json.dumps(swipes, ensure_ascii=False), new_swipe_id,
             json.dumps(meta, ensure_ascii=False), companion_id, branch_id, message_id)
        )
        conn.commit()
        return {"swipes": swipes, "swipe_id": new_swipe_id, "content": new_content}
    finally:
        conn.close()


def _load_metadata(raw) -> Dict:
    """Read the metadata column, which is JSON text and may be absent or broken."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_with_generation(
    metadata: Optional[Dict],
    swipe_id: int,
    generation: Optional[Dict],
) -> Dict:
    """Place *generation* at the slot of *swipe_id*, keeping the other swipes.

    One message can hold several variants, each produced by a different call.
    A single record on the message would be wrong as soon as the user swipes, so
    the records live in a list indexed exactly like `swipes`.
    """
    meta = dict(metadata or {})
    if not generation:
        return meta
    slots = meta.get("generation")
    slots = list(slots) if isinstance(slots, list) else []
    while len(slots) <= swipe_id:
        slots.append({})
    slots[swipe_id] = generation
    meta["generation"] = slots
    return meta


def update_message_content(
    companion_id: str,
    branch_id: str,
    message_id: int,
    content: str,
    generation: Optional[Dict] = None,
) -> bool:
    """更新消息的内容（用于续写追加内容）。同时更新当前swipe的内容。

    *generation* overwrites the record of the swipe being extended; the caller
    merges the counters first, because only it knows the previous record.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT swipes, swipe_id, metadata FROM companion_messages WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (companion_id, branch_id, message_id)
        ).fetchone()
        if not row:
            return False
        
        swipes = json.loads(row['swipes']) if row['swipes'] else []
        swipe_id = row['swipe_id'] or 0
        if swipes and swipe_id < len(swipes):
            swipes[swipe_id] = content

        meta = _metadata_with_generation(_load_metadata(row['metadata']), swipe_id, generation)
        
        cursor = conn.execute(
            "UPDATE companion_messages SET content = ?, swipes = ?, metadata = ? WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (content, json.dumps(swipes, ensure_ascii=False), json.dumps(meta, ensure_ascii=False),
             companion_id, branch_id, message_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def set_message_swipe_id(companion_id: str, branch_id: str, message_id: int, swipe_id: int) -> Optional[Dict]:
    """切换消息的当前swipe版本。返回更新后的消息数据。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT content, swipes, swipe_id, metadata FROM companion_messages WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (companion_id, branch_id, message_id)
        ).fetchone()
        if not row:
            return None
        
        swipes = json.loads(row['swipes']) if row['swipes'] else [row['content']]
        if swipe_id < 0 or swipe_id >= len(swipes):
            return None
        
        new_content = swipes[swipe_id]
        conn.execute(
            "UPDATE companion_messages SET content = ?, swipe_id = ? WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (new_content, swipe_id, companion_id, branch_id, message_id)
        )
        last_row = conn.execute(
            """
            SELECT id FROM companion_messages
            WHERE companion_id = ? AND branch_id = ? AND is_compacted = 0
            ORDER BY id DESC LIMIT 1
            """,
            (companion_id, branch_id),
        ).fetchone()
        conn.commit()
        meta = _load_metadata(row['metadata'])
        slots = meta.get('generation')
        generation = slots[swipe_id] if isinstance(slots, list) and 0 <= swipe_id < len(slots) else None
        return {
            "swipes": swipes,
            "swipe_id": swipe_id,
            "content": new_content,
            "is_last": bool(last_row and int(last_row["id"]) == int(message_id)),
            "generation": generation if isinstance(generation, dict) else None,
        }
    finally:
        conn.close()


def get_messages_with_swipes(companion_id: str, branch_id: str = "main", include_compacted: bool = False) -> List[Dict]:
    """
    获取指定分支的聊天上下文（包含swipe信息）。
    
    Returns:
        消息列表，包含 message_type, metadata, is_important 字段
    """
    conn = _get_conn()
    try:
        compacted_filter = "" if include_compacted else " AND is_compacted = 0"
        rows = conn.execute(
            f"""SELECT id, role, content, swipes, swipe_id, created_at, 
                      message_type, metadata, is_important 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ?{compacted_filter}
               ORDER BY id""",
            (companion_id, branch_id)
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            # 解析swipes JSON
            if data.get('swipes'):
                data['swipes'] = json.loads(data['swipes'])
            else:
                data['swipes'] = [data['content']]
            # 解析 metadata JSON
            if data.get('metadata'):
                data['metadata'] = json.loads(data['metadata'])
            else:
                data['metadata'] = {}
            result.append(data)
        return result
    finally:
        conn.close()


def get_last_assistant_message(companion_id: str, branch_id: str = "main") -> Optional[Dict]:
    """
    获取分支中最后一条AI消息。
    
    Returns:
        消息字典，包含 message_type, metadata, is_important 字段
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, role, content, swipes, swipe_id, created_at, 
                      message_type, metadata, is_important 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND role = 'assistant' 
               ORDER BY id DESC LIMIT 1""",
            (companion_id, branch_id)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        if data.get('swipes'):
            data['swipes'] = json.loads(data['swipes'])
        else:
            data['swipes'] = [data['content']]
        # 解析 metadata JSON
        if data.get('metadata'):
            data['metadata'] = json.loads(data['metadata'])
        else:
            data['metadata'] = {}
        return data
    finally:
        conn.close()


def get_branch_stats(companion_id: str, branch_id: str = "main") -> Dict:
    """获取分支的统计信息（消息数、token数等）。"""
    messages = get_messages(companion_id, branch_id)
    
    total_chars = sum(len(msg.get("content", "")) for msg in messages)
    
    return {
        "message_count": len(messages),
        "total_chars": total_chars,
        "messages": messages,
    }


# ==================== User Settings CRUD ====================
# 使用现有的 conversations.db user_profiles 表

def get_user_settings() -> Dict:
    """获取用户设置。从 companions.db 的 user_settings 表读取。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT name, avatar_path FROM user_settings WHERE id = 1"
        ).fetchone()
        if row:
            return {
                'name': row['name'] or 'You',
                'avatar_path': row['avatar_path'] or ''
            }
        return {'name': 'You', 'avatar_path': ''}
    finally:
        conn.close()


def update_user_settings(name: Optional[str] = None, avatar_path: Optional[str] = None) -> bool:
    """更新用户设置。更新 companions.db 的 user_settings 表。"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (id, name, avatar_path) VALUES (1, 'You', '')"
        )
        if name is not None:
            conn.execute(
                "UPDATE user_settings SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (name,)
            )
        if avatar_path is not None:
            conn.execute(
                "UPDATE user_settings SET avatar_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (avatar_path,)
            )
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] update_user_settings error: {e}")
        return False
    finally:
        conn.close()
    return True


# ==================== Compaction History CRUD ====================

def save_compaction_history(
    companion_id: str,
    branch_id: str,
    messages_before: int,
    messages_after: int,
    messages_compacted: int,
    tokens_before: int,
    tokens_after: int,
    tokens_saved: int,
    summary_preview: str,
    summary_full: str = "",
    compaction_version: int = 1,
    preserve_recent: int = 10,
    llm_model: str = "",
) -> int:
    """
    保存压缩历史记录。
    
    Returns:
        历史记录ID
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO compaction_history 
               (companion_id, branch_id, messages_before, messages_after, messages_compacted,
                tokens_before, tokens_after, tokens_saved, summary_preview, summary_full,
                compaction_version, preserve_recent, llm_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (companion_id, branch_id, messages_before, messages_after, messages_compacted,
             tokens_before, tokens_after, tokens_saved, summary_preview, summary_full,
             compaction_version, preserve_recent, llm_model)
        )
        conn.commit()
        result = cursor.lastrowid
        return result if result is not None else 0
    finally:
        conn.close()


def get_compaction_history(
    companion_id: str, 
    branch_id: str = "main", 
    limit: int = 10
) -> List[Dict]:
    """
    获取压缩历史记录。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        limit: 返回记录数量限制
    
    Returns:
        压缩历史记录列表
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM compaction_history 
               WHERE companion_id = ? AND branch_id = ? 
               ORDER BY compacted_at DESC 
               LIMIT ?""",
            (companion_id, branch_id, limit)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_compaction_stats(companion_id: str, branch_id: str = "main") -> Dict:
    """
    获取压缩统计信息。
    
    Returns:
        包含总压缩次数、总节省token等统计信息
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT 
                COUNT(*) as total_compactions,
                SUM(messages_compacted) as total_messages_compacted,
                SUM(tokens_saved) as total_tokens_saved,
                MAX(compacted_at) as last_compaction_time
               FROM compaction_history 
               WHERE companion_id = ? AND branch_id = ?""",
            (companion_id, branch_id)
        ).fetchone()
        
        if not row:
            return {
                "total_compactions": 0,
                "total_messages_compacted": 0,
                "total_tokens_saved": 0,
                "last_compaction_time": None,
            }
        
        return dict(row)
    finally:
        conn.close()


# ==================== Message Importance ====================

def set_message_importance(
    companion_id: str, 
    branch_id: str, 
    message_id: int, 
    is_important: bool
) -> bool:
    """
    设置消息的重要性标记。
    重要消息在压缩时会被保留，不会被压缩。
    
    Args:
        companion_id: AI伴侣ID
        branch_id: 分支ID
        message_id: 消息ID
        is_important: 是否重要
    
    Returns:
        是否成功
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE companion_messages SET is_important = ? WHERE companion_id = ? AND branch_id = ? AND id = ?",
            (1 if is_important else 0, companion_id, branch_id, message_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_important_messages(companion_id: str, branch_id: str = "main") -> List[Dict]:
    """
    获取分支中所有标记为重要的消息。
    
    Returns:
        重要消息列表
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, role, content, created_at, message_type, metadata 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND is_important = 1 
               ORDER BY id""",
            (companion_id, branch_id)
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            if data.get('metadata'):
                data['metadata'] = json.loads(data['metadata'])
            else:
                data['metadata'] = {}
            result.append(data)
        return result
    finally:
        conn.close()


# ==================== Summary Message Helpers ====================

def get_summary_messages(companion_id: str, branch_id: str = "main") -> List[Dict]:
    """
    获取分支中所有的摘要消息。
    
    Returns:
        摘要消息列表，按时间正序排列
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, role, content, created_at, metadata 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND message_type = 'compaction_summary' 
               ORDER BY id""",
            (companion_id, branch_id)
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            if data.get('metadata'):
                data['metadata'] = json.loads(data['metadata'])
            else:
                data['metadata'] = {}
            result.append(data)
        return result
    finally:
        conn.close()


def get_latest_summary(companion_id: str, branch_id: str = "main") -> Optional[Dict]:
    """
    获取分支中最新的摘要消息。
    
    Returns:
        最新的摘要消息，如果没有则返回None
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, role, content, created_at, metadata 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND message_type = 'compaction_summary' 
               ORDER BY id DESC LIMIT 1""",
            (companion_id, branch_id)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        if data.get('metadata'):
            data['metadata'] = json.loads(data['metadata'])
        else:
            data['metadata'] = {}
        return data
    finally:
        conn.close()


def get_compaction_version(companion_id: str, branch_id: str = "main") -> int:
    """
    获取当前压缩版本号。
    通过统计摘要消息数量来确定。
    
    Returns:
        当前压缩版本号（0表示未压缩过）
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) as count 
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND message_type = 'compaction_summary'""",
            (companion_id, branch_id)
        ).fetchone()
        return row['count'] if row else 0
    finally:
        conn.close()


# ==================== Compaction retention ====================

def mark_messages_compacted(
    companion_id: str,
    branch_id: str,
    message_ids: List[int],
    batch_id: str,
) -> int:
    """
    Flag messages as compacted instead of deleting them.

    A compacted message stops feeding the model and stops rendering in the chat,
    but it stays in the table so the history panel can show what was folded away.
    Returns the number of rows updated.
    """
    if not message_ids:
        return 0
    
    conn = _get_conn()
    try:
        unique_ids = [int(mid) for mid in message_ids]
        total = 0
        # SQLite caps bound variables per statement, so a long branch has to be
        # flagged in batches; one oversized UPDATE raises and the caller sees
        # zero rows marked.
        for start in range(0, len(unique_ids), 500):
            batch = unique_ids[start:start + 500]
            placeholders = ",".join(["?"] * len(batch))
            cursor = conn.execute(
                f"""UPDATE companion_messages
                    SET is_compacted = 1, compaction_batch_id = ?
                    WHERE companion_id = ? AND branch_id = ? AND id IN ({placeholders})""",
                [batch_id, companion_id, branch_id] + batch
            )
            total += cursor.rowcount or 0
        conn.commit()
        return total
    except Exception as e:
        logger.error(f"Failed to mark messages compacted: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()


def get_compacted_messages(companion_id: str, branch_id: str = "main") -> List[Dict]:
    """
    Return compacted messages grouped by compaction batch, newest batch first.

    Each group carries the batch id, the timestamps of its messages and the
    full message list, which is what the history panel renders.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, role, content, created_at, message_type, metadata,
                      compaction_batch_id
               FROM companion_messages 
               WHERE companion_id = ? AND branch_id = ? AND is_compacted = 1
                 AND COALESCE(compaction_batch_id, '') NOT LIKE 'scene_%'
               ORDER BY id""",
            (companion_id, branch_id)
        ).fetchall()
    finally:
        conn.close()
    
    batches: Dict[str, Dict] = {}
    order: List[str] = []
    
    for row in rows:
        data = dict(row)
        metadata_raw = data.get("metadata")
        if metadata_raw:
            try:
                data["metadata"] = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                data["metadata"] = {}
        else:
            data["metadata"] = {}
        
        batch_id = data.pop("compaction_batch_id", "") or "legacy"
        if batch_id not in batches:
            batches[batch_id] = {
                "batch_id": batch_id,
                "message_count": 0,
                "started_at": data.get("created_at"),
                "ended_at": data.get("created_at"),
                "messages": [],
            }
            order.append(batch_id)
        
        group = batches[batch_id]
        group["messages"].append(data)
        group["message_count"] = len(group["messages"])
        group["ended_at"] = data.get("created_at")
    
    # Newest batch first: the most recent compaction is what users look for.
    return [batches[bid] for bid in reversed(order)]

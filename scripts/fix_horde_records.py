"""
fix_horde_records.py — 修复 Horde Studio 格式角色卡的历史导入记录。

背景
----
旧版导入器（tavern_importer.py）不认识 Horde Studio 的
`horde-studio-virtual-human` 格式（.horde_human.json），导致：
  1. 所有字段解析为空（name 变成 'Unknown'）；
  2. 整个导出文件（可达数百 MB）被原样存入 raw_card_data，
     使 companions.db 剧烈膨胀，拖慢列表/详情接口。

本脚本对已入库的记录执行与新版导入器一致的修复：
  - 重新解析 raw_card_data 并填充空字段；
  - 从数据内嵌的 profilePhoto/basePhoto 提取头像文件；
  - 将 raw_card_data 裁剪为轻量版本（媒体 base64 替换为占位标记）；
  - 可选执行 VACUUM 收缩数据库文件。

字段覆盖策略（保护用户数据）
  - name 仅在当前为 'Unknown' 或空时覆盖；
  - 文本字段仅在当前为空时填充；
  - avatar_path 与 raw_card_data 始终更新（修复目标本身）。

用法
----
  python scripts/fix_horde_records.py --dry-run              # 只分析，不写入
  python scripts/fix_horde_records.py --apply                # 执行修复（自动备份）
  python scripts/fix_horde_records.py --apply --vacuum       # 修复并收缩数据库

注意：运行前请确认后端服务已停止（避免数据库锁竞争）。
"""
import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# 允许从项目根导入 backend 包
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.tavern_importer import (  # noqa: E402
    HORDE_FORMAT_TAG,
    _extract_horde_avatar,
    _normalize_horde_human,
    _safe_raw_card_dump,
)

DB_PATH = ROOT / "companions.db"

# 需要填充的文本字段（仅当当前为空时）
FILL_IF_EMPTY_FIELDS = (
    "description", "personality", "scenario", "first_mes", "mes_example",
    "advanced_prompt", "user_info", "creator_notes", "system_prompt",
    "post_history_instructions", "creator", "character_version",
)

# JSON 序列化字段（仅当当前为空值时填充）
JSON_FIELDS = ("tags", "alternate_greetings", "depth_prompt")


def _reconfigure_stdout():
    """Windows 控制台下尽量使用 UTF-8 输出。"""
    try:
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _probe_avatar(raw: dict):
    """dry-run 用：检查头像是否可提取（不解码、不写文件）。"""
    c = raw.get("companion") or {}
    url = c.get("profilePhoto") or c.get("basePhoto") or ""
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    try:
        header, b64 = url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header:
        return None
    approx_bytes = len(b64) * 3 // 4
    return f"{header.split(';')[0].replace('data:', '')} (~{approx_bytes / 1024 / 1024:.1f} MB)"


def _empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "[]", "{}")
    return False


def scan_candidates(conn: sqlite3.Connection):
    """单次全表扫描，返回疑似 horde 记录列表：[(record_dict, raw_str)]。"""
    cursor = conn.execute("SELECT rowid, * FROM companions")
    columns = [d[0] for d in cursor.description]
    candidates = []
    for row in cursor.fetchall():
        record = dict(zip(columns, row))
        raw_str = record.get("raw_card_data") or ""
        if not raw_str:
            continue
        # 高效探测：只需检查前 2000 字符
        head = raw_str[:2000] if len(raw_str) > 2000 else raw_str
        if HORDE_FORMAT_TAG in head:
            candidates.append((record, raw_str))
    return candidates


def process_record(record: dict, raw_str: str, apply_mode: bool):
    """处理单条记录。返回 (summary_dict, update_dict)。"""
    cid = record["id"]
    name = record.get("name")
    raw_len = len(raw_str)
    print(f"\n--- 记录: {cid} (rowid={record['rowid']}) ---")
    print(f"  当前 name      : {name!r}")
    print(f"  当前 raw 体积  : {raw_len:,} 字符 (~{raw_len / 1024 / 1024:.1f} MB)")

    t0 = time.time()
    raw = json.loads(raw_str)
    print(f"  解析耗时       : {time.time() - t0:.1f}s")

    if raw.get("_format") != HORDE_FORMAT_TAG:
        print("  跳过：不是 horde 格式")
        return None, None

    normalized = _normalize_horde_human(raw)
    new_dump = _safe_raw_card_dump(raw)

    print(f"  解析结果 name  : {normalized.get('name')!r}")
    for key in ("description", "personality", "scenario", "mes_example",
                "system_prompt", "advanced_prompt", "user_info"):
        value = normalized.get(key) or ""
        print(f"    {key:<28}: {len(value):,} 字符")
    print(f"  裁剪后 raw 体积: {len(new_dump):,} 字符 (~{len(new_dump) / 1024:.0f} KB)")
    avatar_probe = _probe_avatar(raw)
    print(f"  头像           : {avatar_probe or '不可提取'}")

    update = {}
    if (not record.get("name")) or record.get("name") == "Unknown":
        if normalized.get("name") and normalized["name"] != "Unknown":
            update["name"] = normalized["name"]

    for key in FILL_IF_EMPTY_FIELDS:
        if _empty(record.get(key)) and normalized.get(key):
            update[key] = normalized[key]

    for key in JSON_FIELDS:
        if _empty(record.get(key)) and normalized.get(key):
            update[key] = json.dumps(normalized[key], ensure_ascii=False)

    if apply_mode:
        avatar_path = _extract_horde_avatar(raw, cid)
        if avatar_path:
            update["avatar_path"] = avatar_path
            print(f"  头像已保存     : {avatar_path}")

    update["raw_card_data"] = new_dump

    summary = {
        "id": cid,
        "name": normalized.get("name"),
        "fields_filled": [k for k in update if k != "raw_card_data"],
        "raw_before": raw_len,
        "raw_after": len(new_dump),
    }
    return summary, update


def apply_update(conn: sqlite3.Connection, rowid: int, update: dict):
    """按 rowid 执行 UPDATE。"""
    keys = list(update.keys())
    set_clause = ", ".join(f"{k} = ?" for k in keys)
    values = [update[k] for k in keys] + [rowid]
    conn.execute(f"UPDATE companions SET {set_clause} WHERE rowid = ?", values)


def backup_database() -> Path:
    """备份数据库文件（先 checkpoint WAL）。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.with_name(f"{DB_PATH.name}.bak_{stamp}")
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="修复 Horde 格式角色卡的历史导入记录")
    parser.add_argument("--dry-run", action="store_true", help="只分析，不写入")
    parser.add_argument("--apply", action="store_true", help="执行修复")
    parser.add_argument("--vacuum", action="store_true", help="修复后 VACUUM 收缩数据库")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.error("请指定 --dry-run 或 --apply")

    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        sys.exit(1)

    print(f"数据库: {DB_PATH}")
    print(f"当前大小: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"模式: {'DRY-RUN（不写入）' if args.dry_run else 'APPLY（写入）'}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        candidates = scan_candidates(conn)
        if not candidates:
            print("\n未发现 horde 格式的导入记录。")
            return

        print(f"\n发现 {len(candidates)} 条疑似 horde 记录：")
        for record, raw_str in candidates:
            print(f"  - {record['id']} | name={record.get('name')!r} | raw={len(raw_str) / 1024 / 1024:.1f} MB")

        if args.apply:
            backup_path = backup_database()
            print(f"\n已备份数据库: {backup_path}")

        summaries = []
        for record, raw_str in candidates:
            summary, update = process_record(record, raw_str, args.apply)
            if summary is None:
                continue
            summaries.append(summary)
            if args.apply and update:
                apply_update(conn, record["rowid"], update)

        if args.apply:
            conn.commit()
            print("\n已写入修复结果。")
            total_before = sum(s["raw_before"] for s in summaries)
            total_after = sum(s["raw_after"] for s in summaries)
            print(f"raw_card_data 总量: {total_before / 1024 / 1024:.1f} MB -> {total_after / 1024:.0f} KB")

            if args.vacuum:
                print("\n执行 VACUUM（可能需要一些时间）...")
                conn.execute("VACUUM")
                conn.commit()
                print(f"VACUUM 完成。数据库大小: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            print("\n[DRY-RUN] 未写入任何数据。确认无误后使用 --apply 执行修复。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

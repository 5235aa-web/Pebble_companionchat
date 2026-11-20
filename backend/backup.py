"""
backup.py - 数据库快照

整个产品就装在一个 SQLite 文件里：角色卡、所有分支、每条消息、每条记忆。
任何一份都没有第二个副本。

快照走 sqlite3 自带的 backup API，而不是复制文件。数据库运行在 WAL 模式，
磁盘上那个 .db 本身并不是一个完整的库，直接复制会漏掉还在写前日志里、
尚未合并回主文件的内容。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from .paths import DATA_ROOT, DB_PATH

logger = logging.getLogger(__name__)

BACKUP_DIR = DATA_ROOT / "backups"
PREFIX = "companions-"
SUFFIX = ".db"

# 保留多少份。每次启动写一份、同时清掉更旧的，正常使用下覆盖最近一周左右。
KEEP = 7


def _existing() -> List[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob(f"{PREFIX}*{SUFFIX}"), key=lambda p: p.name)


def prune(keep: int = KEEP) -> int:
    """删掉超出保留份数的旧快照，返回删除数量。"""
    files = _existing()
    if len(files) <= keep:
        return 0
    removed = 0
    for path in files[: len(files) - keep]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("could not remove old backup %s: %s", path, exc)
    return removed


def snapshot() -> Optional[Path]:
    """写一份数据库快照并清理旧文件。

    备份是保险，不是功能：任何一步失败都只记日志，绝不能让应用起不来。
    """
    if not DB_PATH.exists():
        return None

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = BACKUP_DIR / f"{PREFIX}{stamp}{SUFFIX}"

        src = sqlite3.connect(str(DB_PATH))
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        dropped = prune()
        logger.info(
            "database snapshot written to %s (%d old backups removed)", target, dropped
        )
        return target
    except Exception as exc:  # noqa: BLE001 - never block startup on a backup
        logger.warning("database snapshot failed: %s", exc)
        return None

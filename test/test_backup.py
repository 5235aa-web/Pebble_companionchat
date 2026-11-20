# -*- coding: utf-8 -*-
"""Database snapshots: the one file the whole product lives in."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import backup


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-bak-'))
        self._db = backup.DB_PATH
        self._dir = backup.BACKUP_DIR
        backup.DB_PATH = self.tmp / 'companions.db'
        backup.BACKUP_DIR = self.tmp / 'backups'

    def tearDown(self):
        backup.DB_PATH = self._db
        backup.BACKUP_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_db(self, rows=1):
        conn = sqlite3.connect(str(backup.DB_PATH))
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        conn.executemany('INSERT INTO t (v) VALUES (?)', [(f'v{i}',) for i in range(rows)])
        conn.commit()
        conn.close()

    def test_missing_database_yields_no_snapshot(self):
        self.assertIsNone(backup.snapshot())

    def test_snapshot_creates_a_copy(self):
        self._make_db(rows=3)
        target = backup.snapshot()
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        conn = sqlite3.connect(str(target))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM t').fetchone()[0], 3)
        finally:
            conn.close()

    def test_snapshot_dir_is_created(self):
        self._make_db()
        backup.snapshot()
        self.assertTrue(backup.BACKUP_DIR.is_dir())


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-prune-'))
        self._dir = backup.BACKUP_DIR
        backup.BACKUP_DIR = self.tmp
        self.files = []
        for i in range(5):
            path = self.tmp / f'{backup.PREFIX}2026010{i}-000000{backup.SUFFIX}'
            path.write_text('x', encoding='utf-8')
            self.files.append(path)

    def tearDown(self):
        backup.BACKUP_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keeps_everything_under_the_limit(self):
        self.assertEqual(backup.prune(keep=10), 0)
        self.assertEqual(len(list(self.tmp.iterdir())), 5)

    def test_removes_the_oldest_first(self):
        removed = backup.prune(keep=2)
        self.assertEqual(removed, 3)
        remaining = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(remaining, [self.files[3].name, self.files[4].name])

    def test_missing_directory_is_not_an_error(self):
        backup.BACKUP_DIR = self.tmp / 'absent'
        self.assertEqual(backup.prune(), 0)


if __name__ == '__main__':
    unittest.main()

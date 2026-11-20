# -*- coding: utf-8 -*-
"""Companion / branch / message persistence against a throwaway database."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion_db, world_book_db


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-db-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)


class CompanionTest(DbCase):
    def test_create_and_read_back(self):
        self.assertTrue(companion_db.create_companion('c1', {'name': 'Aria', 'description': 'd'}))
        loaded = companion_db.get_companion('c1')
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded['name'], 'Aria')

    def test_duplicate_id_is_rejected(self):
        companion_db.create_companion('c1', {'name': 'Aria'})
        self.assertFalse(companion_db.create_companion('c1', {'name': 'Other'}))

    def test_missing_companion_reads_as_none(self):
        self.assertIsNone(companion_db.get_companion('nope'))

    def test_create_makes_a_main_branch(self):
        companion_db.create_companion('c1', {'name': 'Aria'})
        branches = companion_db.get_branches('c1')
        self.assertTrue(any(b['branch_id'] == 'main' for b in branches))

    def test_created_companion_is_listed(self):
        companion_db.create_companion('c1', {'name': 'Aria'})
        self.assertIn('c1', [c['id'] for c in companion_db.get_all_companions()])

    def test_update_changes_the_field(self):
        companion_db.create_companion('c1', {'name': 'Aria'})
        self.assertTrue(companion_db.update_companion('c1', {'name': 'Renamed'}))
        self.assertEqual(companion_db.get_companion('c1')['name'], 'Renamed')

    def test_delete_removes_the_row(self):
        companion_db.create_companion('c1', {'name': 'Aria'})
        self.assertTrue(companion_db.delete_companion('c1'))
        self.assertIsNone(companion_db.get_companion('c1'))


class BranchTest(DbCase):
    def setUp(self):
        super().setUp()
        companion_db.create_companion('c1', {'name': 'Aria'})

    def test_create_branch_returns_an_id(self):
        branch_id = companion_db.create_branch('c1', 'side')
        self.assertIsNotNone(branch_id)
        self.assertIn(branch_id, [b['branch_id'] for b in companion_db.get_branches('c1')])

    def test_rename_branch(self):
        branch_id = companion_db.create_branch('c1', 'side')
        self.assertTrue(companion_db.update_branch_name('c1', branch_id, 'renamed'))
        names = {b['branch_id']: b['branch_name'] for b in companion_db.get_branches('c1')}
        self.assertEqual(names[branch_id], 'renamed')

    def test_delete_branch(self):
        branch_id = companion_db.create_branch('c1', 'side')
        self.assertTrue(companion_db.delete_branch('c1', branch_id))
        self.assertNotIn(branch_id, [b['branch_id'] for b in companion_db.get_branches('c1')])


class MessageTest(DbCase):
    def setUp(self):
        super().setUp()
        companion_db.create_companion('c1', {'name': 'Aria'})

    def test_save_and_read(self):
        companion_db.save_message('c1', 'main', 'user', 'hello')
        messages = companion_db.get_messages('c1', 'main')
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['content'], 'hello')

    def test_message_count_tracks_saves(self):
        companion_db.save_message('c1', 'main', 'user', 'a')
        companion_db.save_message('c1', 'main', 'assistant', 'b')
        self.assertEqual(companion_db.get_message_count('c1', 'main'), 2)

    def test_search_finds_a_substring(self):
        companion_db.save_message('c1', 'main', 'user', 'the dragon sleeps')
        companion_db.save_message('c1', 'main', 'user', 'unrelated line')
        hits = companion_db.search_messages('c1', 'main', 'dragon')
        self.assertEqual(len(hits), 1)
        self.assertIn('dragon', hits[0]['content'])

    def test_delete_message_removes_it(self):
        message_id = companion_db.save_message('c1', 'main', 'user', 'hello')
        self.assertTrue(companion_db.delete_message('c1', 'main', message_id))
        self.assertEqual(companion_db.get_message_count('c1', 'main'), 0)

    def test_clear_branch_empties_it(self):
        companion_db.save_message('c1', 'main', 'user', 'a')
        companion_db.save_message('c1', 'main', 'assistant', 'b')
        companion_db.clear_branch_messages('c1', 'main')
        self.assertEqual(companion_db.get_message_count('c1', 'main'), 0)

    def test_branches_are_isolated(self):
        other = companion_db.create_branch('c1', 'side')
        companion_db.save_message('c1', 'main', 'user', 'in main')
        companion_db.save_message('c1', other, 'user', 'in side')
        self.assertEqual(companion_db.get_message_count('c1', 'main'), 1)
        self.assertEqual(companion_db.get_messages('c1', 'main')[0]['content'], 'in main')

    def test_compacted_messages_leave_the_default_view(self):
        message_id = companion_db.save_message('c1', 'main', 'user', 'old turn')
        companion_db.mark_messages_compacted('c1', 'main', [message_id], 'batch-1')
        self.assertEqual(companion_db.get_message_count('c1', 'main'), 0)
        self.assertEqual(len(companion_db.get_compacted_messages('c1', 'main')), 1)


if __name__ == '__main__':
    unittest.main()

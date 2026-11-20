# -*- coding: utf-8 -*-
"""World book storage: books, entries, companion links, JSON round trip."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion_db, world_book_db


def _entry(**over):
    data = {'comment': 'e', 'keys': ['dragon'], 'content': 'dragons live here'}
    data.update(over)
    return data


class WorldBookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-wb-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        companion_db.create_companion('c1', {'name': 'Aria'})
        self.book_id = world_book_db.create_world_book('Lore', 'setting notes')

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)


class BookTest(WorldBookCase):
    def test_create_and_read_back(self):
        book = world_book_db.get_world_book(self.book_id)
        self.assertIsNotNone(book)
        self.assertEqual(book['name'], 'Lore')

    def test_created_book_is_listed(self):
        self.assertIn(self.book_id, [b['id'] for b in world_book_db.get_all_world_books()])

    def test_rename(self):
        self.assertTrue(world_book_db.update_world_book(self.book_id, name='Renamed'))
        self.assertEqual(world_book_db.get_world_book(self.book_id)['name'], 'Renamed')

    def test_delete_removes_it(self):
        self.assertTrue(world_book_db.delete_world_book(self.book_id))
        self.assertIsNone(world_book_db.get_world_book(self.book_id))

    def test_missing_book_reads_as_none(self):
        self.assertIsNone(world_book_db.get_world_book(99999))


class EntryTest(WorldBookCase):
    def test_add_and_read_back(self):
        entry_id = world_book_db.add_entry(self.book_id, _entry())
        loaded = world_book_db.get_entry(entry_id)
        self.assertEqual(loaded['content'], 'dragons live here')
        self.assertEqual(loaded['keys'], ['dragon'])

    def test_uid_is_assigned_incrementally(self):
        first = world_book_db.add_entry(self.book_id, _entry())
        second = world_book_db.add_entry(self.book_id, _entry())
        self.assertEqual(world_book_db.get_entry(first)['uid'], 1)
        self.assertEqual(world_book_db.get_entry(second)['uid'], 2)

    def test_entries_are_listed_per_book(self):
        world_book_db.add_entry(self.book_id, _entry())
        other_book = world_book_db.create_world_book('Other')
        world_book_db.add_entry(other_book, _entry())
        self.assertEqual(len(world_book_db.get_entries(self.book_id)), 1)
        self.assertEqual(len(world_book_db.get_entries(other_book)), 1)

    def test_update_changes_the_content(self):
        entry_id = world_book_db.add_entry(self.book_id, _entry())
        updated = _entry(content='rewritten')
        self.assertTrue(world_book_db.update_entry(entry_id, updated))
        self.assertEqual(world_book_db.get_entry(entry_id)['content'], 'rewritten')

    def test_delete_removes_the_entry(self):
        entry_id = world_book_db.add_entry(self.book_id, _entry())
        self.assertTrue(world_book_db.delete_entry(entry_id))
        self.assertIsNone(world_book_db.get_entry(entry_id))

    def test_missing_entry_reads_as_none(self):
        self.assertIsNone(world_book_db.get_entry(424242))


class CompanionLinkTest(WorldBookCase):
    def test_association_is_listed(self):
        self.assertTrue(world_book_db.associate_companion_with_world_book('c1', self.book_id))
        ids = [b['id'] for b in world_book_db.get_companion_world_books('c1')]
        self.assertIn(self.book_id, ids)

    def test_association_is_idempotent(self):
        world_book_db.associate_companion_with_world_book('c1', self.book_id)
        world_book_db.associate_companion_with_world_book('c1', self.book_id)
        ids = [b['id'] for b in world_book_db.get_companion_world_books('c1')]
        self.assertEqual(ids.count(self.book_id), 1)

    def test_primary_book_is_returned(self):
        world_book_db.associate_companion_with_world_book('c1', self.book_id, is_primary=True)
        primary = world_book_db.get_companion_primary_world_book('c1')
        self.assertIsNotNone(primary)
        self.assertEqual(primary['id'], self.book_id)

    def test_no_primary_book_reads_as_none(self):
        self.assertIsNone(world_book_db.get_companion_primary_world_book('c1'))

    def test_association_can_be_removed(self):
        world_book_db.associate_companion_with_world_book('c1', self.book_id)
        self.assertTrue(world_book_db.remove_companion_world_book_association('c1', self.book_id))
        self.assertEqual(world_book_db.get_companion_world_books('c1'), [])


class JsonRoundTripTest(WorldBookCase):
    def test_export_carries_the_entries(self):
        world_book_db.add_entry(self.book_id, _entry())
        exported = world_book_db.export_world_book_to_json(self.book_id)
        self.assertEqual(exported['name'], 'Lore')
        self.assertEqual(len(exported['entries']), 1)

    def test_import_recreates_the_book(self):
        world_book_db.add_entry(self.book_id, _entry())
        exported = world_book_db.export_world_book_to_json(self.book_id)
        cloned_id = world_book_db.import_world_book_from_json(exported, name='Clone')
        self.assertNotEqual(cloned_id, self.book_id)
        self.assertEqual(world_book_db.get_world_book(cloned_id)['name'], 'Clone')
        self.assertEqual(len(world_book_db.get_entries(cloned_id)), 1)


if __name__ == '__main__':
    unittest.main()

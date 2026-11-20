# -*- coding: utf-8 -*-
"""Character card normalisation (Tavern V1/V2/V3) and the import pipeline."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion_db, tavern_importer, world_book_db


class NormalizeTest(unittest.TestCase):
    def test_v2_card_reads_the_data_block(self):
        raw = {
            'spec': 'chara_card_v2',
            'data': {
                'name': 'Aria',
                'description': 'a bard',
                'first_mes': 'hello',
                'tags': ['music'],
            },
        }
        normalized = tavern_importer._normalize_card_data(raw)
        self.assertEqual(normalized['name'], 'Aria')
        self.assertEqual(normalized['description'], 'a bard')
        self.assertEqual(normalized['first_mes'], 'hello')
        self.assertEqual(normalized['tags'], ['music'])

    def test_v2_extensions_are_picked_up(self):
        raw = {'data': {'name': 'Aria', 'extensions': {'advanced_prompt': 'be brief'}}}
        self.assertEqual(tavern_importer._normalize_card_data(raw)['advanced_prompt'], 'be brief')

    def test_v1_card_reads_the_top_level(self):
        normalized = tavern_importer._normalize_card_data({'name': 'Old', 'description': 'v1 card'})
        self.assertEqual(normalized['name'], 'Old')
        self.assertEqual(normalized['description'], 'v1 card')

    def test_missing_name_falls_back_to_unknown(self):
        self.assertEqual(tavern_importer._normalize_card_data({})['name'], 'Unknown')

    def test_missing_fields_default_to_empty(self):
        normalized = tavern_importer._normalize_card_data({'name': 'A'})
        for field in ('description', 'personality', 'scenario', 'first_mes'):
            self.assertEqual(normalized[field], '', field)


class RawDumpTest(unittest.TestCase):
    def test_long_strings_are_replaced_with_a_note(self):
        stripped = tavern_importer._strip_oversized_strings({'avatar': 'x' * 200_000})
        self.assertIn('stripped', stripped['avatar'])
        self.assertLess(len(stripped['avatar']), 100)

    def test_nested_structures_are_walked(self):
        stripped = tavern_importer._strip_oversized_strings({'a': [{'b': 'y' * 200_000}]})
        self.assertIn('stripped', stripped['a'][0]['b'])

    def test_short_strings_survive(self):
        self.assertEqual(tavern_importer._strip_oversized_strings({'a': 'keep'}), {'a': 'keep'})

    def test_dump_is_json_and_bounded(self):
        dump = tavern_importer._safe_raw_card_dump({'data': {'name': 'A', 'avatar': 'x' * 200_000}})
        payload = json.loads(dump)
        self.assertEqual(payload['data']['name'], 'A')
        self.assertLess(len(dump), 10_000)


class ImportPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-card-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_json_creates_a_companion(self):
        card = {'data': {'name': 'Aria', 'description': 'a bard', 'first_mes': 'hi'}}
        path = self.tmp / 'card.json'
        path.write_text(json.dumps(card), encoding='utf-8')

        companion_id = tavern_importer.import_json(str(path))

        self.assertTrue(companion_id)
        stored = companion_db.get_companion(companion_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored['name'], 'Aria')
        self.assertEqual(stored['first_mes'], 'hi')

    def test_imported_companion_gets_a_main_branch(self):
        path = self.tmp / 'card.json'
        path.write_text(json.dumps({'name': 'Solo'}), encoding='utf-8')

        companion_id = tavern_importer.import_json(str(path))

        branches = companion_db.get_branches(companion_id)
        self.assertTrue(any(b['branch_id'] == 'main' for b in branches))

    def test_raw_card_data_is_stored(self):
        path = self.tmp / 'card.json'
        path.write_text(json.dumps({'name': 'Solo', 'personality': 'quiet'}), encoding='utf-8')

        companion_id = tavern_importer.import_json(str(path))
        stored = companion_db.get_companion(companion_id)
        self.assertIn('Solo', stored['raw_card_data'])


if __name__ == '__main__':
    unittest.main()

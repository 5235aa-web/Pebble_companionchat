# -*- coding: utf-8 -*-
"""Per-message generation footprint: record shape, merging, per-swipe storage."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion, companion_db, world_book_db

USAGE = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
CONFIG = {'model': 'gemma-4-12b-heretic', 'provider_type': 'llama-cpp'}


class GenerationRecordTest(unittest.TestCase):
    def test_provider_usage_is_preferred(self):
        record = companion._generation_record(CONFIG, 1234.6, 210.4, USAGE, 'ignored')
        self.assertEqual(record['tokens_source'], 'usage')
        self.assertEqual(record['completion_tokens'], 20)
        self.assertEqual(record['prompt_tokens'], 100)
        self.assertEqual(record['total_tokens'], 120)

    def test_missing_usage_is_counted_locally(self):
        record = companion._generation_record(CONFIG, 500, None, None, 'hello world again')
        # measured when a tokenizer file or llama.cpp /tokenize answers, an
        # estimate only when both are unavailable.
        self.assertIn(record['tokens_source'], ('measured', 'estimated'))
        self.assertGreater(record['completion_tokens'], 0)
        self.assertIsNone(record['prompt_tokens'])

    def test_llama_tokenize_is_marked_measured(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        exact = SimpleNamespace(tokens=42, exact=True, method='llama.cpp:/tokenize')
        with patch.object(companion, 'count_text_tokens', return_value=exact):
            record = companion._generation_record(CONFIG, 10, None, None, 'hello')
        self.assertEqual(record['tokens_source'], 'measured')
        self.assertEqual(record['completion_tokens'], 42)
        self.assertEqual(record['total_tokens'], 42)

    def test_foreign_tokenizer_is_still_an_estimate(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        # tiktoken calls itself exact, but cl100k is not Gemma's vocabulary.
        foreign = SimpleNamespace(tokens=7, exact=True, method='tiktoken:cl100k_base')
        with patch.object(companion, 'count_text_tokens', return_value=foreign):
            record = companion._generation_record(CONFIG, 10, None, None, 'hello')
        self.assertEqual(record['tokens_source'], 'estimated')
        self.assertEqual(record['completion_tokens'], 7)

    def test_counter_failure_does_not_lose_the_record(self):
        from unittest.mock import patch

        with patch.object(companion, 'count_text_tokens', side_effect=RuntimeError('down')):
            record = companion._generation_record(CONFIG, 10, None, None, 'hello')
        self.assertEqual(record['tokens_source'], 'none')
        self.assertIsNone(record['completion_tokens'])
        self.assertEqual(record['model'], 'gemma-4-12b-heretic')

    def test_zero_completion_does_not_count_as_usage(self):
        record = companion._generation_record(CONFIG, 500, None, {'completion_tokens': 0}, 'text')
        self.assertNotEqual(record['tokens_source'], 'usage')

    def test_empty_text_has_no_tokens(self):
        record = companion._generation_record(CONFIG, 10, None, None, '')
        self.assertEqual(record['tokens_source'], 'none')
        self.assertIsNone(record['completion_tokens'])

    def test_timings_are_rounded_ints(self):
        record = companion._generation_record(CONFIG, 1234.6, 210.4, USAGE, 'x')
        self.assertEqual(record['elapsed_ms'], 1235)
        self.assertEqual(record['ttft_ms'], 210)

    def test_absent_ttft_stays_none(self):
        self.assertIsNone(companion._generation_record(CONFIG, 10, None, USAGE, 'x')['ttft_ms'])

    def test_model_and_provider_come_from_the_config(self):
        record = companion._generation_record(CONFIG, 10, 1, USAGE, 'x')
        self.assertEqual(record['model'], 'gemma-4-12b-heretic')
        self.assertEqual(record['provider'], 'llama-cpp')

    def test_missing_config_does_not_raise(self):
        record = companion._generation_record(None, 10, 1, USAGE, 'x')
        self.assertEqual(record['model'], '')
        self.assertEqual(record['status'], 'ok')


class MergeGenerationTest(unittest.TestCase):
    def test_without_a_previous_record_the_addition_stands(self):
        addition = {'elapsed_ms': 50, 'completion_tokens': 5}
        self.assertEqual(companion._merge_generation(None, addition), addition)

    def test_counters_accumulate(self):
        previous = {'elapsed_ms': 100, 'completion_tokens': 10, 'total_tokens': 20, 'tokens_source': 'usage'}
        addition = {'elapsed_ms': 50, 'completion_tokens': 5, 'total_tokens': 10, 'tokens_source': 'usage'}
        merged = companion._merge_generation(previous, addition)
        self.assertEqual(merged['elapsed_ms'], 150)
        self.assertEqual(merged['completion_tokens'], 15)
        self.assertEqual(merged['total_tokens'], 30)

    def test_estimated_sticks_once_seen(self):
        previous = {'elapsed_ms': 1, 'tokens_source': 'estimated'}
        addition = {'elapsed_ms': 1, 'completion_tokens': 3, 'tokens_source': 'usage'}
        self.assertEqual(companion._merge_generation(previous, addition)['tokens_source'], 'estimated')

    def test_non_dict_previous_is_ignored(self):
        addition = {'elapsed_ms': 7}
        self.assertEqual(companion._merge_generation('broken', addition), addition)


class MetadataSlotTest(unittest.TestCase):
    def test_records_are_placed_by_swipe_index(self):
        meta = companion_db._metadata_with_generation({}, 1, {'model': 'b'})
        self.assertEqual(meta['generation'], [{}, {'model': 'b'}])

    def test_other_metadata_keys_survive(self):
        meta = companion_db._metadata_with_generation({'attachments': [{'kind': 'image'}]}, 0, {'model': 'a'})
        self.assertIn('attachments', meta)
        self.assertEqual(meta['generation'][0]['model'], 'a')

    def test_without_a_record_nothing_is_added(self):
        meta = companion_db._metadata_with_generation({'x': 1}, 0, None)
        self.assertNotIn('generation', meta)


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-gen-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        companion_db.create_companion('c1', {'name': 'Aria'})

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _first(self, message_id):
        messages = companion_db.get_messages_with_swipes('c1', 'main')
        for message in messages:
            if message['id'] == message_id:
                return message
        self.fail('message not found')

    def test_new_reply_keeps_its_record(self):
        generation = {'model': 'a', 'elapsed_ms': 100}
        message_id = companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'hi', ['hi'], 0, metadata={'generation': [generation]},
        )
        self.assertEqual(self._first(message_id)['metadata']['generation'], [generation])

    def test_regenerated_variant_gets_its_own_slot(self):
        first = {'model': 'a', 'elapsed_ms': 100}
        second = {'model': 'b', 'elapsed_ms': 200}
        message_id = companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'one', ['one'], 0, metadata={'generation': [first]},
        )
        result = companion_db.add_swipe_to_message('c1', 'main', message_id, 'two', generation=second)
        self.assertEqual(result['swipe_id'], 1)
        stored = self._first(message_id)['metadata']['generation']
        self.assertEqual(stored, [first, second])

    def test_continuation_replaces_the_slot_it_extends(self):
        message_id = companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'one', ['one'], 0,
            metadata={'generation': [{'model': 'a', 'elapsed_ms': 100}]},
        )
        merged = {'model': 'a', 'elapsed_ms': 250}
        companion_db.update_message_content('c1', 'main', message_id, 'one two', generation=merged)
        stored = self._first(message_id)['metadata']['generation']
        self.assertEqual(stored, [merged])
        self.assertEqual(self._first(message_id)['content'], 'one two')

    def test_switching_variant_does_not_touch_the_records(self):
        first = {'model': 'a', 'elapsed_ms': 100}
        second = {'model': 'b', 'elapsed_ms': 200}
        message_id = companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'one', ['one'], 0, metadata={'generation': [first]},
        )
        companion_db.add_swipe_to_message('c1', 'main', message_id, 'two', generation=second)
        companion_db.set_message_swipe_id('c1', 'main', message_id, 0)
        stored = self._first(message_id)['metadata']['generation']
        self.assertEqual(stored, [first, second])


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""Token counting facade and the context-window helpers.

The counters themselves live in token_engine; what is pinned down here is the
contract callers rely on: empty input costs nothing, a truncation really truncates
to the requested budget, and an unknown model still gets sane defaults.
"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import api_routes, companion_db, token_counter, token_engine


class EstimateTest(unittest.TestCase):
    def test_empty_text_costs_nothing(self):
        self.assertEqual(token_counter.estimate_tokens_text(''), 0)

    def test_short_text_costs_a_few_tokens(self):
        count = token_counter.estimate_tokens_text('hello world')
        self.assertGreater(count, 0)
        self.assertLess(count, 10)

    def test_longer_text_costs_more(self):
        short = token_counter.estimate_tokens_text('hello world')
        long = token_counter.estimate_tokens_text('hello world ' * 50)
        self.assertGreater(long, short)

    def test_empty_message_list_costs_next_to_nothing(self):
        # The chat template still costs a few tokens even with no messages.
        self.assertLess(token_counter.estimate_tokens_messages([]), 10)

    def test_one_message_costs_something(self):
        count = token_counter.estimate_tokens_messages([{'role': 'user', 'content': 'hi'}])
        self.assertGreater(count, 0)


class TruncateTest(unittest.TestCase):
    def test_text_within_budget_is_untouched(self):
        text = 'short'
        self.assertEqual(token_counter.truncate_to_tokens(text, 100), text)

    def test_long_text_is_cut_to_the_budget(self):
        text = 'word ' * 400
        trimmed = token_counter.truncate_to_tokens(text, 10)
        self.assertLess(len(trimmed), len(text))
        self.assertLessEqual(token_counter.estimate_tokens_text(trimmed), 10)

    def test_zero_budget_yields_empty_text(self):
        self.assertEqual(token_counter.truncate_to_tokens('word ' * 50, 0), '')


class WindowTest(unittest.TestCase):
    def test_known_model_has_a_positive_window(self):
        self.assertGreater(token_counter.get_context_window('gpt-4'), 0)

    def test_unknown_model_falls_back_to_a_default(self):
        self.assertGreater(token_counter.get_context_window('definitely-not-a-model'), 0)

    def test_threshold_is_below_the_window(self):
        window = token_counter.get_context_window('gpt-4')
        self.assertLessEqual(token_counter.get_compaction_threshold('gpt-4'), window)


class ShouldCompactTest(unittest.TestCase):
    def test_fresh_room_does_not_compact(self):
        self.assertFalse(token_counter.should_compact(0, 'gpt-4'))

    def test_full_window_compacts(self):
        self.assertTrue(token_counter.should_compact(10 ** 9, 'gpt-4'))


class DescribeTest(unittest.TestCase):
    def test_description_names_a_method_and_precision(self):
        info = token_counter.describe_counter('gpt-4')
        self.assertIn('method', info)
        self.assertIn('exact', info)
        self.assertTrue(str(info['method']))


class BranchTokenStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-tokens-'))
        self._db = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        companion_db.create_companion('c1', {'name': 'Aria'})
        token_engine.clear_recorded_usage('c1', 'main')

    def tearDown(self):
        token_engine.clear_recorded_usage('c1', 'main')
        companion_db.DB_PATH = self._db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_branch_tokens_align_with_recorded_total_tokens(self):
        token_engine.record_usage('c1', 'main', {
            'prompt_tokens': 1200,
            'completion_tokens': 340,
            'total_tokens': 1540,
        })
        res = asyncio.run(api_routes.get_branch_token_stats('c1', 'main'))
        self.assertEqual(res['token_count'], 1540)
        self.assertEqual(res['method'], 'provider usage')
        self.assertTrue(res['exact'])

    def test_branch_tokens_sum_prompt_and_completion_when_total_omitted(self):
        token_engine.record_usage('c1', 'main', {
            'prompt_tokens': 800,
            'completion_tokens': 250,
            'total_tokens': 0,
        })
        res = asyncio.run(api_routes.get_branch_token_stats('c1', 'main'))
        self.assertEqual(res['token_count'], 1050)

    def test_branch_mutations_clear_recorded_usage(self):
        msg_id = companion_db.save_message('c1', 'main', 'user', 'hello')
        token_engine.record_usage('c1', 'main', {
            'prompt_tokens': 500,
            'completion_tokens': 100,
            'total_tokens': 600,
        })
        asyncio.run(api_routes.delete_branch_message('c1', 'main', msg_id))
        self.assertIsNone(token_engine.get_recorded_usage('c1', 'main'))

    def test_recorded_usage_survives_memory_cache_clear(self):
        token_engine.record_usage('c1', 'main', {
            'prompt_tokens': 900,
            'completion_tokens': 200,
            'total_tokens': 1100,
        })
        token_engine._USAGE_CACHE.clear()
        res = asyncio.run(api_routes.get_branch_token_stats('c1', 'main'))
        self.assertEqual(res['token_count'], 1100)
        self.assertEqual(res['method'], 'provider usage')

    def test_legacy_trailing_assistant_generation_backfills_usage(self):
        # Simulate a pre-migration branch whose token_usage column is still ''.
        conn = companion_db._get_conn()
        try:
            conn.execute(
                "UPDATE companion_branches SET token_usage = '' WHERE companion_id = 'c1' AND branch_id = 'main'"
            )
            conn.commit()
        finally:
            conn.close()
        gen = {
            'prompt_tokens': 3634,
            'completion_tokens': 406,
            'total_tokens': 4040,
            'tokens_source': 'usage',
        }
        companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'reply', ['reply'], 0,
            metadata={'generation': [gen]},
        )
        token_engine._USAGE_CACHE.clear()
        res = asyncio.run(api_routes.get_branch_token_stats('c1', 'main'))
        self.assertEqual(res['token_count'], 4040)
        self.assertEqual(res['method'], 'provider usage')

    def test_swipe_on_trailing_message_syncs_usage(self):
        g0 = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120, 'tokens_source': 'usage'}
        g1 = {'prompt_tokens': 100, 'completion_tokens': 45, 'total_tokens': 145, 'tokens_source': 'usage'}
        msg_id = companion_db.save_message_with_swipes(
            'c1', 'main', 'assistant', 'first', ['first'], 0,
            metadata={'generation': [g0]},
        )
        companion_db.add_swipe_to_message('c1', 'main', msg_id, 'second', generation=g1)
        asyncio.run(api_routes.swipe_message(api_routes.SwipeRequest(
            companion_id='c1', branch_id='main', message_id=msg_id, swipe_id=0,
        )))
        res = asyncio.run(api_routes.get_branch_token_stats('c1', 'main'))
        self.assertEqual(res['token_count'], 120)


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""Scene-wise compaction: segment cards, the two-turn floor, isolation."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion_db, scene_compaction, script_db


async def fake_summarize(transcript, llm_config):
    """Stand-in for the model call: the test asserts on bookkeeping, not prose."""
    return f'RECAP scenes={transcript.count("[Scene ")} chars={len(transcript)}'


class CleanSummaryTest(unittest.TestCase):
    def test_long_summary_is_clipped(self):
        cleaned = scene_compaction._clean_summary('x' * (scene_compaction.MAX_SUMMARY_CHARS + 900))
        self.assertEqual(len(cleaned), scene_compaction.MAX_SUMMARY_CHARS)

    def test_code_fence_is_removed(self):
        self.assertEqual(scene_compaction._clean_summary('```\nhello\n```'), 'hello')


class CompactionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-scene-'))
        self._db_path = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        self._summarize = scene_compaction._summarize
        scene_compaction._summarize = fake_summarize

        self.script_id = script_db.create_script('The Rain Letter', 'A rainy night.')
        script_db.replace_nodes(
            self.script_id,
            [
                {'title': 'One', 'instruction': 'meet', 'turns': 3},
                {'title': 'Two', 'instruction': 'argue', 'turns': 3},
                {'title': 'Three', 'instruction': 'resolve', 'turns': 3},
            ],
        )
        companion_db.create_companion(
            'c1', {'name': 'Elias', 'description': 'shop', 'personality': 'quiet'}
        )
        self.branch_id = companion_db.create_branch('c1', 'Script')
        self.session_id = script_db.create_session(
            self.script_id, 'c1', self.branch_id
        )

    def tearDown(self):
        scene_compaction._summarize = self._summarize
        companion_db.DB_PATH = self._db_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_turns(self, count, start_scene=0):
        for i in range(count):
            role = 'user' if i % 2 == 0 else 'assistant'
            companion_db.save_message(
                'c1',
                self.branch_id,
                role,
                f'message {start_scene}-{i}',
                metadata={'scene_index': start_scene},
            )

    def compact(self, current_index=0, allow_trim=False):
        return asyncio.run(
            scene_compaction.compact_scene(
                'c1',
                self.branch_id,
                self.session_id,
                self.script_id,
                current_index,
                'Elias',
                {},
                allow_trim=allow_trim,
            )
        )


class FloorTest(CompactionCase):
    def test_two_turns_are_not_enough(self):
        self.write_turns(2)
        result = self.compact()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'not_enough_messages')

    def test_nothing_is_marked_when_the_floor_blocks_it(self):
        self.write_turns(2)
        self.compact()
        self.assertEqual(len(companion_db.get_messages('c1', self.branch_id)), 2)
        self.assertEqual(script_db.list_scene_summaries('c1', self.branch_id), [])


class FirstRunTest(CompactionCase):
    def test_keeps_the_last_two_turns(self):
        self.write_turns(8)
        result = self.compact()
        self.assertTrue(result['ok'])
        self.assertEqual(result['compressed'], 6)
        remaining = companion_db.get_messages('c1', self.branch_id)
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[-1]['content'], 'message 0-7')

    def test_writes_one_card_with_the_covered_range(self):
        self.write_turns(8)
        self.compact(current_index=1)
        cards = script_db.list_scene_summaries('c1', self.branch_id)
        self.assertEqual(len(cards), 1)
        # Every message was written under scene 0, so the card covers scene 1.
        self.assertEqual(cards[0]['scene_from'], 1)
        self.assertEqual(cards[0]['scene_to'], 1)
        self.assertTrue(cards[0]['message_to_id'] > 0)

    def test_compacted_messages_stay_in_the_table(self):
        self.write_turns(8)
        self.compact()
        all_rows = companion_db.get_messages('c1', self.branch_id, include_compacted=True)
        self.assertEqual(len(all_rows), 8)


class SecondRunTest(CompactionCase):
    def test_second_run_only_covers_what_came_after(self):
        self.write_turns(8)
        self.compact()
        self.write_turns(6, start_scene=2)
        result = self.compact(current_index=2)
        self.assertTrue(result['ok'])
        cards = script_db.list_scene_summaries('c1', self.branch_id)
        self.assertEqual(len(cards), 2)
        self.assertIn('RECAP', cards[0]['content'])
        # The second card starts after the first one ended; the two turns the
        # first run kept are folded into it, which is the point of the rule.
        self.assertGreater(cards[1]['message_to_id'], cards[0]['message_to_id'])

    def test_old_card_is_never_merged_away(self):
        self.write_turns(8)
        self.compact()
        first = script_db.list_scene_summaries('c1', self.branch_id)[0]
        self.write_turns(6, start_scene=2)
        self.compact(current_index=2)
        cards = script_db.list_scene_summaries('c1', self.branch_id)
        self.assertEqual(cards[0]['id'], first['id'])
        self.assertEqual(cards[0]['content'], first['content'])


class IsolationTest(CompactionCase):
    def test_scene_batches_stay_out_of_the_compaction_panel(self):
        self.write_turns(8)
        self.compact()
        self.assertEqual(companion_db.get_compacted_messages('c1', self.branch_id), [])

    def test_clear_removes_cards_and_resets_progress(self):
        self.write_turns(8)
        self.compact(current_index=1)
        script_db.increment_turn(self.session_id)
        script_db.clear_scene_summaries('c1', self.branch_id)
        script_db.reset_session_progress('c1', self.branch_id)
        session = script_db.get_session(self.session_id)
        self.assertEqual(script_db.list_scene_summaries('c1', self.branch_id), [])
        self.assertEqual(session['current_index'], 0)
        self.assertEqual(session['turns_done'], 0)
        self.assertFalse(session['scene_done'])


class TrimTest(CompactionCase):
    def with_limit(self, limit):
        original = scene_compaction.input_token_limit
        scene_compaction.input_token_limit = lambda cfg: limit
        self.addCleanup(setattr, scene_compaction, 'input_token_limit', original)

    def test_over_the_limit_asks_before_sending(self):
        self.write_turns(8)
        self.with_limit(10)
        result = self.compact()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'needs_trim')
        self.assertGreater(result['tokens'], 10)
        # Nothing was marked and no card was written: the model was never called.
        self.assertEqual(script_db.list_scene_summaries('c1', self.branch_id), [])
        self.assertEqual(len(companion_db.get_messages('c1', self.branch_id)), 8)

    def test_trim_runs_only_after_approval(self):
        self.write_turns(8)
        self.with_limit(10)
        result = self.compact(allow_trim=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['trimmed'])

    def test_local_limit_is_lower_than_cloud(self):
        self.assertEqual(
            scene_compaction.input_token_limit({'provider_type': 'llama-cpp'}),
            scene_compaction.LOCAL_INPUT_TOKENS,
        )
        self.assertEqual(
            scene_compaction.input_token_limit({'provider_type': 'openai'}),
            scene_compaction.CLOUD_INPUT_TOKENS,
        )
        self.assertLess(
            scene_compaction.LOCAL_INPUT_TOKENS,
            scene_compaction.CLOUD_INPUT_TOKENS,
        )


if __name__ == '__main__':
    unittest.main()

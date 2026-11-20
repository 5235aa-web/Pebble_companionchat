# -*- coding: utf-8 -*-
"""Character memory storage: scoping, keyword search, eviction, cursor."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import companion_db, memory_db, memory_engine, world_book_db


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-mem-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        companion_db.create_companion('c1', {'name': 'Aria'})

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)


class AddTest(MemoryCase):
    def test_items_are_written(self):
        written = memory_db.add_memories('c1', 'main', [
            {'content': 'likes tea', 'kind': 'preference'},
            {'content': 'met at the harbour', 'kind': 'event'},
        ])
        self.assertEqual(written, 2)
        self.assertEqual(memory_db.count_memories('c1', 'main'), 2)

    def test_blank_content_is_skipped(self):
        written = memory_db.add_memories('c1', 'main', [{'content': '   '}, {'content': 'ok'}])
        self.assertEqual(written, 1)

    def test_unknown_kind_falls_back_to_fact(self):
        memory_db.add_memories('c1', 'main', [{'content': 'x', 'kind': 'nonsense'}])
        self.assertEqual(memory_db.list_memories('c1', 'main')[0]['kind'], 'fact')

    def test_unknown_scope_falls_back_to_branch(self):
        memory_db.add_memories('c1', 'main', [{'content': 'x', 'scope': 'galaxy'}])
        self.assertEqual(memory_db.list_memories('c1', 'main')[0]['scope'], 'branch')

    def test_no_items_writes_nothing(self):
        self.assertEqual(memory_db.add_memories('c1', 'main', []), 0)


class ScopeTest(MemoryCase):
    def test_branch_memory_is_invisible_from_another_branch(self):
        other = companion_db.create_branch('c1', 'side')
        memory_db.add_memories('c1', 'main', [{'content': 'main only', 'scope': 'branch'}])
        self.assertEqual(memory_db.count_memories('c1', other), 0)

    def test_character_memory_is_visible_everywhere(self):
        other = companion_db.create_branch('c1', 'side')
        memory_db.add_memories('c1', 'main', [{'content': 'always true', 'scope': 'character'}])
        # count_memories answers "how full is this branch", so it stays branch-local;
        # the panel and the injector read through list_memories, which widens to
        # character-scoped rows.
        visible = [m['content'] for m in memory_db.list_memories('c1', other)]
        self.assertEqual(visible, ['always true'])


class SearchTest(MemoryCase):
    def test_keyword_hit_is_returned(self):
        memory_db.add_memories('c1', 'main', [
            {'content': 'owns a red bicycle', 'keys': ['bicycle']},
            {'content': 'afraid of thunder', 'keys': ['thunder']},
        ])
        hits = memory_db.search_memories('c1', 'main', 'where is the bicycle')
        self.assertEqual(len(hits), 1)
        self.assertIn('bicycle', hits[0]['content'])

    def test_no_match_returns_nothing(self):
        memory_db.add_memories('c1', 'main', [{'content': 'owns a bicycle', 'keys': ['bicycle']}])
        self.assertEqual(memory_db.search_memories('c1', 'main', 'quantum physics'), [])

    def test_blank_probe_returns_nothing(self):
        memory_db.add_memories('c1', 'main', [{'content': 'x', 'keys': ['x']}])
        self.assertEqual(memory_db.search_memories('c1', 'main', '   '), [])

    def test_english_key_does_not_match_inside_a_word(self):
        self.assertFalse(memory_db._key_hit('run', 'brunch'))
        self.assertTrue(memory_db._key_hit('run', 'running'))

    def test_single_ascii_letter_is_ignored(self):
        self.assertFalse(memory_db._key_hit('a', 'a cat'))

    def test_cjk_keys_use_a_plain_substring(self):
        self.assertTrue(memory_db._key_hit('海边', '她住在海边'))


class EditTest(MemoryCase):
    def setUp(self):
        super().setUp()
        memory_db.add_memories('c1', 'main', [{'content': 'original', 'keys': ['k']}])
        self.memory_id = memory_db.list_memories('c1', 'main')[0]['id']

    def test_content_can_be_rewritten(self):
        self.assertTrue(memory_db.update_memory('c1', 'main', self.memory_id, content='rewritten'))
        self.assertEqual(memory_db.list_memories('c1', 'main')[0]['content'], 'rewritten')

    def test_blank_content_is_refused(self):
        self.assertFalse(memory_db.update_memory('c1', 'main', self.memory_id, content='  '))

    def test_no_fields_is_refused(self):
        self.assertFalse(memory_db.update_memory('c1', 'main', self.memory_id))

    def test_delete_removes_the_row(self):
        self.assertTrue(memory_db.delete_memory('c1', 'main', self.memory_id))
        self.assertEqual(memory_db.count_memories('c1', 'main'), 0)

    def test_clear_all(self):
        memory_db.add_memories('c1', 'main', [{'content': 'second'}])
        self.assertEqual(memory_db.clear_memories('c1', 'main'), 2)
        self.assertEqual(memory_db.count_memories('c1', 'main'), 0)


class EvictionTest(MemoryCase):
    def test_count_is_capped(self):
        items = [{'content': f'memory {i}', 'importance': 1} for i in range(memory_db.MAX_MEMORIES + 15)]
        memory_db.add_memories('c1', 'main', items)
        self.assertLessEqual(memory_db.count_memories('c1', 'main'), memory_db.MAX_MEMORIES)

    def test_combined_branch_and_character_cap_is_100(self):
        self.assertEqual(memory_db.MAX_MEMORIES + memory_db.MAX_CHARACTER_MEMORIES, 100)
        branch_items = [{'content': f'branch fact {i}', 'scope': 'branch'} for i in range(90)]
        char_items = [{'content': f'char fact {i}', 'scope': 'character'} for i in range(40)]
        memory_db.add_memories('c1', 'main', branch_items + char_items)
        self.assertEqual(len(memory_db.list_memories('c1', 'main')), 100)


class HybridSearchAndEngineTest(MemoryCase):
    def test_content_is_searched_even_without_keys(self):
        memory_db.add_memories('c1', 'main', [
            {'content': 'Aria found a brass compass in the attic', 'keys': []},
            {'content': 'Aria dislikes bitter coffee', 'keys': ['coffee']},
        ])
        hits = memory_db.search_memories('c1', 'main', 'Where did she find that brass compass?')
        self.assertEqual(len(hits), 1)
        self.assertIn('compass', hits[0]['content'])

    def test_cjk_content_and_stemming_recall(self):
        memory_db.add_memories('c1', 'main', [
            {'content': 'Aria promised to return before winter', 'keys': ['promise', 'return']},
            {'content': '林夏在阁楼里藏了一把旧口琴', 'keys': []},
        ])
        self.assertEqual(
            len(memory_db.search_memories('c1', 'main', 'She was promising she would be returning soon')),
            1,
        )
        cjk_hits = memory_db.search_memories('c1', 'main', '阁楼里那把旧口琴还在吗')
        self.assertEqual(len(cjk_hits), 1)
        self.assertIn('旧口琴', cjk_hits[0]['content'])

    def test_relevance_beats_stale_weak_tie_and_importance_breaks_ties(self):
        memory_db.add_memories('c1', 'main', [
            {'content': 'Aria mentioned an umbrella once', 'keys': ['umbrella'], 'importance': 1},
            {'content': 'Aria kept the umbrella from the harbor storm', 'keys': ['umbrella', 'harbor', 'storm'], 'importance': 3},
        ])
        strong_id = next(
            m['id'] for m in memory_db.list_memories('c1', 'main') if 'harbor storm' in m['content']
        )
        # Even after being used a few times, the strong multi-term match still
        # beats a weak single-term tie with use_count == 0.
        for _ in range(3):
            memory_db.touch_memories([strong_id])

        hits = memory_db.search_memories('c1', 'main', 'Remember the harbor storm and that umbrella?')
        self.assertEqual([m['id'] for m in hits], [strong_id])

        # Equal relevance: higher importance ranks first.
        memory_db.clear_memories('c1', 'main')
        memory_db.add_memories('c1', 'main', [
            {'content': 'Aria walked on the pier in rain', 'keys': ['pier', 'rain'], 'importance': 1},
            {'content': 'Aria promised on the pier in rain', 'keys': ['pier', 'rain'], 'importance': 3},
        ])
        tie_hits = memory_db.search_memories('c1', 'main', 'pier in the rain')
        self.assertEqual(len(tie_hits), 2)
        self.assertEqual(tie_hits[0]['importance'], 3)

    def test_supersedes_protects_character_scope_and_invalid_json_preserves_cursor(self):
        other = companion_db.create_branch('c1', 'side')
        memory_db.add_memories('c1', 'main', [
            {'content': 'Aria is angry at Alex', 'scope': 'branch', 'keys': ['angry']},
            {'content': 'Aria loves jasmine tea', 'scope': 'character', 'keys': ['tea']},
        ])
        all_rows = memory_db.list_memories('c1', 'main')
        branch_id = next(m['id'] for m in all_rows if m['scope'] == 'branch')
        char_id = next(m['id'] for m in all_rows if m['scope'] == 'character')

        history = [
            {'id': i + 1, 'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'Line {i} {{braces}} ok'}
            for i in range(memory_engine.EXTRACT_EVERY)
        ]

        async def bad_stream(**kwargs):
            yield {'type': 'content', 'content': 'not valid json at all'}

        with patch('backend.memory_engine.call_llm_stream', side_effect=bad_stream):
            res_bad = asyncio.run(
                memory_engine.extract_memories('c1', 'main', history, {'model': 'm'}, 'Aria', 'Alex')
            )
        self.assertEqual(res_bad, 0)
        self.assertEqual(memory_db.get_cursor('c1', 'main'), 0)

        async def good_stream(**kwargs):
            payload = [
                {
                    'content': 'Aria forgave Alex and made up',
                    'kind': 'relationship',
                    'scope': 'branch',
                    'keys': ['forgive', 'made up', '原谅', '和好'],
                    'importance': 3,
                    'supersedes': str(branch_id),
                },
                {
                    'content': 'Aria hates jasmine tea now',
                    'kind': 'preference',
                    'scope': 'branch',
                    'keys': ['tea', '茶'],
                    'importance': 2,
                    'supersedes': str(char_id),
                },
            ]
            yield {'type': 'content', 'content': json.dumps(payload, ensure_ascii=False)}

        with patch('backend.memory_engine.call_llm_stream', side_effect=good_stream):
            written = asyncio.run(
                memory_engine.extract_memories('c1', 'main', history, {'model': 'm'}, 'Aria', 'Alex')
            )
        self.assertEqual(written, 2)
        self.assertEqual(memory_db.get_cursor('c1', 'main'), memory_engine.EXTRACT_EVERY)
        main_ids = {m['id'] for m in memory_db.list_memories('c1', 'main')}
        self.assertNotIn(branch_id, main_ids)
        self.assertIn(char_id, main_ids)
        self.assertIn('Aria loves jasmine tea', [m['content'] for m in memory_db.list_memories('c1', other)])

    def test_select_existing_pulls_relevant_older_memories_past_40(self):
        items = [{'content': f'Filler memory number {i} about clouds', 'keys': ['clouds']} for i in range(60)]
        items.insert(0, {'content': 'Aria hid a silver locket under the floorboards', 'keys': ['locket', 'floorboards']})
        memory_db.add_memories('c1', 'main', items)
        all_rows = memory_db.list_memories('c1', 'main')
        selected = memory_engine._select_existing_for_prompt(
            all_rows, 'Alex: Did you check under the floorboards for the silver locket?', limit=45
        )
        self.assertTrue(any('silver locket' in m['content'] for m in selected))


class CursorTest(MemoryCase):
    def test_missing_cursor_reads_as_zero(self):
        self.assertEqual(memory_db.get_cursor('c1', 'main'), 0)

    def test_cursor_round_trip(self):
        memory_db.set_cursor('c1', 'main', 42)
        self.assertEqual(memory_db.get_cursor('c1', 'main'), 42)


if __name__ == '__main__':
    unittest.main()

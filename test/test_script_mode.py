# -*- coding: utf-8 -*-
"""Script mode: library, scenes, playthroughs, lock rules, prompt isolation."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from fastapi import HTTPException

from backend import companion_db, script_db, script_mode
from backend.companion import Companion
from backend.script_routes import (
    NodeInput,
    NodesRequest,
    RewindRequest,
    StartRequest,
    replace_nodes_endpoint,
    replace_session_nodes_endpoint,
    rewind_session_endpoint,
    start_script_endpoint,
)


class ScriptCase(unittest.TestCase):
    """A throwaway database per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-script-'))
        self._db_path = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'

    def tearDown(self):
        companion_db.DB_PATH = self._db_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_script(self, scenes=3, turns=4):
        script_id = script_db.create_script('The Rain Letter', 'A rainy night.')
        script_db.replace_nodes(
            script_id,
            [
                {'title': f'Scene {i + 1}', 'instruction': f'beat {i + 1}', 'turns': turns}
                for i in range(scenes)
            ],
        )
        return script_id

    def make_session(self, script_id, companion_id='c1'):
        if not companion_db.get_companion(companion_id):
            companion_db.create_companion(
                companion_id,
                {'name': 'Elias', 'description': 'shop', 'personality': 'quiet'},
            )
        branch_id = companion_db.create_branch(companion_id, 'Script')
        session_id = script_db.create_session(script_id, companion_id, branch_id)
        return session_id, branch_id

    def payload(self, titles, turns=4):
        return NodesRequest(
            nodes=[
                NodeInput(title=t, instruction=f'beat {i + 1}', turns=turns)
                for i, t in enumerate(titles)
            ]
        )


class LibraryTest(ScriptCase):
    def test_create_and_list(self):
        script_id = self.make_script(scenes=2)
        self.assertGreater(script_id, 0)
        listed = script_db.list_scripts()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['title'], 'The Rain Letter')
        self.assertEqual(listed[0]['node_count'], 2)

    def test_update_changes_title_and_premise(self):
        script_id = self.make_script()
        self.assertTrue(script_db.update_script(script_id, 'New title', 'New premise'))
        loaded = script_db.get_script(script_id)
        self.assertEqual(loaded['title'], 'New title')
        self.assertEqual(loaded['premise'], 'New premise')

    def test_delete_removes_scenes(self):
        script_id = self.make_script(scenes=3)
        self.assertTrue(script_db.delete_script(script_id))
        self.assertEqual(script_db.list_nodes(script_id), [])


class SceneListTest(ScriptCase):
    def test_order_and_turn_budget_are_kept(self):
        script_id = script_db.create_script('S', '')
        nodes = script_db.replace_nodes(
            script_id,
            [
                {'title': 'One', 'instruction': 'a', 'turns': 6},
                {'title': 'Two', 'instruction': 'b', 'turns': 0},
            ],
        )
        self.assertEqual([n['title'] for n in nodes], ['One', 'Two'])
        self.assertEqual([n['turns'] for n in nodes], [6, 0])

    def test_blank_title_gets_a_default(self):
        script_id = script_db.create_script('S', '')
        nodes = script_db.replace_nodes(script_id, [{'title': '  ', 'instruction': 'a'}])
        self.assertEqual(nodes[0]['title'], '第 1 幕')


class PlaythroughTest(ScriptCase):
    def test_session_starts_at_first_scene_with_default_hud(self):
        script_id = self.make_script()
        session_id, _ = self.make_session(script_id)
        session = script_db.get_session(session_id)
        self.assertEqual(session['current_index'], 0)
        self.assertEqual(session['turns_done'], 0)
        self.assertFalse(session['scene_done'])
        self.assertEqual(set(session['hud']), {'mood', 'affection', 'location', 'outfit'})

    def test_turn_counter_resets_on_advance(self):
        script_id = self.make_script()
        session_id, _ = self.make_session(script_id)
        script_db.increment_turn(session_id)
        script_db.increment_turn(session_id)
        self.assertEqual(script_db.get_session(session_id)['turns_done'], 2)
        script_db.advance_session(session_id, 3)
        session = script_db.get_session(session_id)
        self.assertEqual(session['current_index'], 1)
        self.assertEqual(session['turns_done'], 0)

    def test_last_scene_finishes_the_playthrough(self):
        script_id = self.make_script(scenes=1)
        session_id, _ = self.make_session(script_id)
        session = script_db.advance_session(session_id, 1)
        self.assertEqual(session['status'], 'finished')
        self.assertIsNone(script_db.get_active_session('c1'))

    def test_finished_session_is_not_active(self):
        script_id = self.make_script()
        session_id, _ = self.make_session(script_id)
        script_db.set_session_status(session_id, 'finished')
        self.assertIsNone(script_db.get_active_session('c1'))

    def test_session_lookup_by_branch(self):
        script_id = self.make_script()
        session_id, branch_id = self.make_session(script_id)
        found = script_db.get_session_by_branch('c1', branch_id)
        self.assertIsNotNone(found)
        self.assertEqual(found['id'], session_id)
        self.assertIsNone(script_db.get_session_by_branch('c1', 'nope'))


class CopyTest(ScriptCase):
    """Every playthrough plays its own copy; sharing the script touches nothing."""

    def test_start_takes_a_copy_of_the_scenes(self):
        script_id = self.make_script(scenes=3)
        session_id, _ = self.make_session(script_id)
        self.assertEqual(len(script_db.list_session_nodes(session_id)), 3)

    def test_editing_the_shared_script_does_not_reach_a_playthrough(self):
        script_id = self.make_script(scenes=3)
        session_id, _ = self.make_session(script_id)
        script_db.replace_nodes(
            script_id,
            [{'title': 'Rewritten', 'instruction': 'x', 'turns': 1}],
        )
        titles = [n['title'] for n in script_db.list_session_nodes(session_id)]
        self.assertEqual(titles, ['Scene 1', 'Scene 2', 'Scene 3'])

    def test_two_playthroughs_keep_separate_copies(self):
        script_id = self.make_script(scenes=3)
        first, _ = self.make_session(script_id)
        companion_db.create_companion(
            'c2', {'name': 'Mira', 'description': 'sailor', 'personality': 'loud'}
        )
        second = script_db.create_session(
            script_id, 'c2', companion_db.create_branch('c2', 'Script')
        )
        script_db.replace_session_nodes(
            second, [{'title': 'Only mine', 'instruction': 'a', 'turns': 1}]
        )
        self.assertEqual(script_db.list_session_nodes(first)[0]['title'], 'Scene 1')

    def test_shared_script_is_not_locked_by_a_running_playthrough(self):
        script_id = self.make_script(scenes=3)
        self.make_session(script_id)
        nodes = asyncio.run(
            replace_nodes_endpoint(script_id, self.payload(['A', 'B']))
        )['nodes']
        self.assertEqual(len(nodes), 2)

    def test_played_scenes_of_a_copy_are_frozen(self):
        script_id = self.make_script(scenes=3)
        session_id, _ = self.make_session(script_id)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                replace_session_nodes_endpoint(
                    session_id, self.payload(['Changed', 'Scene 2', 'Scene 3'])
                )
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_copy_can_append_after_the_cursor(self):
        script_id = self.make_script(scenes=2)
        session_id, _ = self.make_session(script_id)
        nodes = asyncio.run(
            replace_session_nodes_endpoint(
                session_id, self.payload(['Scene 1', 'Scene 2', 'Scene 3'])
            )
        )['nodes']
        self.assertEqual(len(nodes), 3)

    def test_replay_removes_the_old_branch_and_its_messages(self):
        script_id = self.make_script(scenes=3)
        session_id, branch_id = self.make_session(script_id)
        companion_db.save_message('c1', branch_id, 'user', 'hello')
        companion_db.save_message('c1', branch_id, 'assistant', 'hi')

        payload = asyncio.run(
            rewind_session_endpoint(session_id, RewindRequest(index=0))
        )

        self.assertIsNone(script_db.get_session(session_id))
        self.assertNotIn(
            branch_id,
            [b['branch_id'] for b in companion_db.get_branches('c1')],
        )
        self.assertEqual(companion_db.get_messages('c1', branch_id), [])
        self.assertNotEqual(payload['session']['branch_id'], branch_id)

    def test_replay_copies_the_previous_playthrough(self):
        script_id = self.make_script(scenes=2)
        first, _ = self.make_session(script_id)
        script_db.replace_session_nodes(
            first,
            [
                {'title': 'Kept', 'instruction': 'a', 'turns': 2},
                {'title': 'Also kept', 'instruction': 'b', 'turns': 0},
            ],
        )
        second = script_db.create_session(
            script_id,
            'c1',
            companion_db.create_branch('c1', 'Replay'),
            copy_from_session=first,
        )
        self.assertEqual(
            [n['title'] for n in script_db.list_session_nodes(second)],
            ['Kept', 'Also kept'],
        )


class FakeCompanion:
    """Only the fields the script prompt is allowed to read, plus bait."""

    id = 'c1'
    name = 'Elias'
    description = 'Runs a bookshop.'
    personality = 'Quiet and exact.'
    creator_notes = 'SECRET AUTHOR NOTE'
    mes_example = 'EXAMPLE DIALOGUE'
    advanced_prompt = 'ADVANCED'
    user_info = 'USER INFO'
    system_prompt = 'CARD SYSTEM PROMPT'
    scenario = 'SCENARIO'
    post_history_instructions = 'POST HISTORY'
    depth_prompt: ClassVar[dict] = {'text': 'DEPTH PROMPT'}


class PromptTest(ScriptCase):
    def build(self, turns=4):
        return script_mode.build_script_system_prompt(
            FakeCompanion(),
            {'title': 'The Rain Letter', 'premise': 'A rainy night.'},
            {'title': 'Scene 2', 'instruction': 'They argue.'},
            1,
            3,
            {'mood': 'calm', 'affection': 2, 'location': 'shop', 'outfit': 'grey coat'},
            turns,
            2,
        )

    def test_only_card_basics_reach_the_model(self):
        prompt = self.build()
        self.assertIn('Elias', prompt)
        self.assertIn('Runs a bookshop.', prompt)
        self.assertIn('Quiet and exact.', prompt)
        for bait in (
            'SECRET AUTHOR NOTE',
            'EXAMPLE DIALOGUE',
            'ADVANCED',
            'USER INFO',
            'CARD SYSTEM PROMPT',
            'SCENARIO',
            'POST HISTORY',
            'DEPTH PROMPT',
        ):
            self.assertNotIn(bait, prompt)

    def test_title_premise_and_turn_budget(self):
        prompt = self.build(turns=6)
        self.assertIn('[Script Title]\nThe Rain Letter', prompt)
        self.assertIn('[Story Premise]\nA rainy night.', prompt)
        self.assertIn('Planned length: about 6 exchanges. Used: 2. Remaining: 4.', prompt)
        self.assertIn('outfit: grey coat', prompt)

    def test_rules_forbid_self_advancing(self):
        prompt = self.build()
        self.assertIn('7. This scene is planned to run', prompt)
        self.assertIn('Never advance', prompt)
        self.assertIn('scene_done: false', prompt)

    def test_open_length_when_turns_is_zero(self):
        prompt = self.build(turns=0)
        self.assertIn('Planned length: open.', prompt)


class StateBlockTest(unittest.TestCase):
    def test_block_split_across_chunks_is_stripped(self):
        stream = script_mode.ScriptStreamFilter()
        parts = ['Hello', ' world<<<ST', 'ATE>>>\nmood: calm\nscene_done: true\n<<<END>>>']
        visible = ''.join(stream.feed(p) for p in parts)
        tail, hud, done = stream.finish()
        self.assertEqual((visible + tail).strip(), 'Hello world')
        self.assertEqual(hud, {'mood': 'calm'})
        self.assertTrue(done)

    def test_plain_reply_passes_through(self):
        stream = script_mode.ScriptStreamFilter()
        visible = stream.feed('only body')
        tail, hud, done = stream.finish()
        self.assertEqual(visible + tail, 'only body')
        self.assertEqual(hud, {})
        self.assertFalse(done)

    def test_unclosed_block_never_leaks(self):
        stream = script_mode.ScriptStreamFilter()
        visible = stream.feed('body<<<STATE>>>\nmood: tense')
        tail, hud, _done = stream.finish()
        self.assertEqual(visible + tail, 'body')
        self.assertEqual(hud, {'mood': 'tense'})

    def test_strip_on_stored_text(self):
        clean, hud, done = script_mode.strip_state_block(
            'reply\n<<<STATE>>>\naffection: 12\n<<<END>>>'
        )
        self.assertEqual(clean, 'reply')
        self.assertEqual(hud, {'affection': 12})
        self.assertFalse(done)


class ContextTest(ScriptCase):
    def test_old_narration_leaves_the_context(self):
        script_id = self.make_script(scenes=1)
        session_id, branch_id = self.make_session(script_id)
        companion_db.save_message('c1', branch_id, 'user', 'Rain at the door.', message_type='narration')
        companion_db.save_message('c1', branch_id, 'user', 'Hello.')
        companion_db.save_message('c1', branch_id, 'assistant', 'You came.')
        companion_db.save_message('c1', branch_id, 'user', 'The lamp goes out.', message_type='narration')

        history = companion_db.get_messages('c1', branch_id)
        _, replayed = Companion.script_request_parts(FakeCompanion(), history, session_id)
        joined = '\n'.join(m['content'] for m in replayed)

        self.assertNotIn('Rain at the door.', joined)
        self.assertIn('The lamp goes out.', joined)
        self.assertIn('[Scene Direction]', joined)
        self.assertIn('Hello.', joined)
        self.assertNotIn('[Narration]', joined)

    def test_segment_summary_comes_first(self):
        script_id = self.make_script(scenes=2)
        session_id, branch_id = self.make_session(script_id)
        first = companion_db.save_message('c1', branch_id, 'user', 'hello')
        last = companion_db.save_message('c1', branch_id, 'assistant', 'hi')
        script_db.add_scene_summary(
            'c1', branch_id, session_id, 1, 1, 'Scene 1', 'They met in the rain.', first, last
        )

        history = companion_db.get_messages('c1', branch_id)
        _, replayed = Companion.script_request_parts(FakeCompanion(), history, session_id)
        self.assertIn('[Story So Far', replayed[0]['content'])
        self.assertIn('They met in the rain.', replayed[0]['content'])


class BranchLabelTest(ScriptCase):
    def start_and_read_branch(self, language, title=None):
        script_id = self.make_script(scenes=1)
        if title is not None:
            script_db.update_script(script_id, title, 'A rainy night.')
        companion_db.create_companion(
            'c1', {'name': 'Elias', 'description': 'shop', 'personality': 'quiet'}
        )
        payload = asyncio.run(
            start_script_endpoint(
                script_id, StartRequest(companion_id='c1', language=language)
            )
        )
        branch_id = payload['session']['branch_id']
        names = {b['branch_id']: b['branch_name'] for b in companion_db.get_branches('c1')}
        return names[branch_id]

    def test_english_branch_name(self):
        self.assertEqual(self.start_and_read_branch('en'), 'Script · The Rain Letter')

    def test_chinese_branch_name(self):
        self.assertEqual(self.start_and_read_branch('zh'), '剧本 · The Rain Letter')

    def test_long_english_title_keeps_three_words(self):
        name = self.start_and_read_branch('en', 'A Very Long Wandering Tale')
        self.assertEqual(name, 'Script · A Very Long')

    def test_long_chinese_title_keeps_three_characters(self):
        name = self.start_and_read_branch('zh', '雨夜来信长篇故事')
        self.assertEqual(name, '剧本 · 雨夜来')


if __name__ == '__main__':
    unittest.main()

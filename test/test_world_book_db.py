# -*- coding: utf-8 -*-
"""World book storage: books, entries, companion links, JSON round trip."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class CharacterBookImportAndTriageTest(WorldBookCase):
    def test_imports_all_entries_with_only_media_enabled_by_default(self):
        from backend import companion_db, world_book_engine

        companion_db.DB_PATH = world_book_db.DB_PATH
        companion_db.create_companion('c_charx', {'name': 'Evelyn'})
        raw_card = {
            'spec': 'chara_card_v3',
            'data': {
                'name': 'Evelyn',
                'extensions': {
                    'risuai': {
                        'customScripts': [
                            {
                                'type': 'editdisplay',
                                'in': '<pimg="(.+?)">',
                                'out': '<img src="{{raw::$1}}">',
                            }
                        ]
                    }
                },
                'character_book': {
                    'entries': [
                        {
                            'comment': 'Folder Divider',
                            'keys': ['\uf000folder:abc'],
                            'content': '   ',
                            'constant': True,
                            'enabled': True,
                        },
                        {
                            'comment': '에셋 출력',
                            'keys': [''],
                            'content': 'Write <pimg="exact key"> before the paragraph. Example: <pimg="이벨린_shy">',
                            'constant': True,
                            'enabled': True,
                            'position': None,
                        },
                        {
                            'comment': '상태창 경제 상점',
                            'keys': [''],
                            'content': '[STATUS, ECONOMY, AND TRANSACTION PROTOCOL] Copper coin prices...',
                            'constant': True,
                            'enabled': True,
                        },
                        {
                            'comment': '이벨린 노크티스',
                            'keys': ['이벨린'],
                            'content': 'Evelyn lives in a hidden cave.',
                            'constant': True,
                            'enabled': True,
                        },
                    ]
                },
            },
        }
        res = world_book_db.import_character_book_for_companion(
            'c_charx',
            raw_card,
            char_name='Evelyn',
            asset_names=['이벨린_shy', 'scene 10'],
        )
        self.assertEqual(res['imported_count'], 3)
        self.assertEqual(res['media_enabled_count'], 1)

        entries = world_book_db.get_entries(res['world_book_id'])
        by_comment = {e['comment']: e for e in entries}
        self.assertEqual(by_comment['에셋 출력']['category'], 'media')
        self.assertFalse(by_comment['에셋 출력']['disabled'])
        self.assertEqual(by_comment['상태창 경제 상점']['category'], 'rpg')
        self.assertTrue(by_comment['상태창 경제 상점']['disabled'])
        self.assertEqual(by_comment['이벨린 노크티스']['category'], 'lore')
        self.assertTrue(by_comment['이벨린 노크티스']['disabled'])

        # Active prompt injection includes ONLY the enabled media entry, not RPG/lore.
        active = world_book_engine.get_active_world_book_content('c_charx', [{'role': 'user', 'content': '이벨린'}])
        self.assertIn('<pimg="exact key">', active)
        self.assertNotIn('TRANSACTION PROTOCOL', active)
        self.assertNotIn('hidden cave', active)

        # User toggles an individual lore entry on -> it immediately enters context.
        lore_entry = dict(by_comment['이벨린 노크티스'])
        lore_entry['disabled'] = False
        world_book_db.update_entry(lore_entry['id'], lore_entry)
        active2 = world_book_engine.get_active_world_book_content('c_charx', [{'role': 'user', 'content': '이벨린'}])
        self.assertIn('hidden cave', active2)

        # Bulk toggle modes: all_off and media_only
        world_book_db.bulk_toggle_entries(res['world_book_id'], mode='all_off')
        self.assertIsNone(world_book_engine.get_active_world_book_content('c_charx', [{'role': 'user', 'content': '이벨린'}]))
        world_book_db.bulk_toggle_entries(res['world_book_id'], mode='media_only')
        active3 = world_book_engine.get_active_world_book_content('c_charx', [{'role': 'user', 'content': '이벨린'}])
        self.assertIn('<pimg="exact key">', active3)
        self.assertNotIn('hidden cave', active3)

        # Re-sync with skip_if_linked=False replaces entries in-place without duplicating books
        res2 = world_book_db.import_character_book_for_companion(
            'c_charx',
            raw_card,
            char_name='Evelyn',
            asset_names=['이벨린_shy', 'scene 10'],
            skip_if_linked=False,
        )
        self.assertEqual(res2['world_book_id'], res['world_book_id'])
        self.assertEqual(len(world_book_db.get_companion_world_books('c_charx')), 1)


class WorldBookTranslationTest(WorldBookCase):
    def test_translation_isolation_and_prompt_keeps_original(self):
        from backend import world_book_engine, world_book_translation

        orig_comment = '에셋 출력 규칙'
        orig_content = (
            '이미지를 출력할 때 <pimg="1_kiss"> 를 사용하세요.\n'
            '정규식: `(<pimg="(?:[^"<>\\r\\n]+?))` / 매크로: {{raw::test}}'
        )
        eid = world_book_db.add_entry(
            self.book_id,
            _entry(comment=orig_comment, content=orig_content, constant=True, disabled=False),
        )
        world_book_db.associate_companion_with_world_book('c1', self.book_id, True)

        seen_calls = []

        async def fake_collect(llm_cfg, messages, *, source_chars, max_token_cap=8192):
            seen_calls.append((llm_cfg, messages, max_token_cap))
            return (
                '<WB_COMMENT>资产输出规则</WB_COMMENT>\n'
                '<WB_CONTENT>输出图像时请使用 <pimg="1_kiss">。\n'
                '正则：`(<pimg="(?:[^"<>\\r\\n]+?))` / 宏：{{raw::test}}</WB_CONTENT>'
            )

        async def run():
            events = []
            with patch.object(world_book_translation, '_collect_translation_text', side_effect=fake_collect):
                async for ev in world_book_translation.iter_translate_world_book(
                    self.book_id,
                    target_locale='zh',
                    llm_config={'provider_type': 'openai', 'model': 'chat-model-x'},
                ):
                    events.append(ev)
            return events

        events = asyncio.run(run())
        self.assertEqual([e['type'] for e in events], ['start', 'progress', 'entry_done', 'done'])
        self.assertEqual(len(seen_calls), 1)
        self.assertEqual(seen_calls[0][0].get('model'), 'chat-model-x')
        self.assertEqual(seen_calls[0][2], 16384)

        entry = world_book_db.get_entry(eid)
        self.assertEqual(entry['comment'], orig_comment)
        self.assertEqual(entry['content'], orig_content)
        self.assertEqual(entry['translations']['zh']['comment'], '资产输出规则')
        self.assertIn('<pimg="1_kiss">', entry['translations']['zh']['content'])
        self.assertIn('{{raw::test}}', entry['translations']['zh']['content'])

        # Editing entry toggle/keys must preserve stored translations without mutating originals.
        entry_copy = dict(entry)
        entry_copy.pop('translations', None)
        entry_copy['keys'] = ['updated_kw']
        world_book_db.update_entry(eid, entry_copy)
        after = world_book_db.get_entry(eid)
        self.assertEqual(after['translations']['zh']['comment'], '资产输出规则')

        # Active prompt sent to LLM must always use the original untranslated text.
        active = world_book_engine.get_active_world_book_content('c1', [{'role': 'user', 'content': 'hi'}])
        self.assertIn('이미지를 출력할 때 <pimg="1_kiss">', active)
        self.assertNotIn('资产输出规则', active)
        self.assertNotIn('输出图像时请使用', active)

    def test_translation_skips_existing_and_supports_single_entry_retranslate(self):
        from backend import world_book_translation

        e1 = world_book_db.add_entry(
            self.book_id,
            _entry(comment='First entry', content='Alpha prose'),
        )
        world_book_db.update_entry_translations(
            e1,
            {'zh': {'comment': '第一条', 'content': '甲段正文'}},
        )
        e2 = world_book_db.add_entry(
            self.book_id,
            _entry(comment='Second entry', content='Beta prose'),
        )

        called = []

        async def fake_collect(llm_cfg, messages, *, source_chars, max_token_cap=8192):
            called.append(messages[1]['content'])
            return '<WB_COMMENT>第二条</WB_COMMENT>\n<WB_CONTENT>乙段正文</WB_CONTENT>'

        async def run(**kwargs):
            out = []
            with patch.object(world_book_translation, '_collect_translation_text', side_effect=fake_collect):
                async for ev in world_book_translation.iter_translate_world_book(
                    self.book_id,
                    target_locale='zh',
                    llm_config={'provider_type': 'openai', 'model': 'm'},
                    **kwargs,
                ):
                    out.append(ev)
            return out

        evs1 = asyncio.run(run(force=False))
        self.assertEqual(evs1[-1]['total'], 1)
        self.assertEqual(evs1[-1]['skipped'], 1)
        self.assertEqual(len(called), 1)
        self.assertIn('Beta prose', called[0])
        self.assertEqual(world_book_db.get_entry(e2)['translations']['zh']['comment'], '第二条')

        # Second pass skips both already-translated entries.
        called.clear()
        evs2 = asyncio.run(run(force=False))
        self.assertEqual(evs2[-1]['total'], 0)
        self.assertEqual(evs2[-1]['skipped'], 2)
        self.assertEqual(len(called), 0)

        # Single-entry forced retranslate updates only e1.
        called.clear()
        evs3 = asyncio.run(run(entry_ids=[e1], force=True))
        self.assertEqual(evs3[-1]['total'], 1)
        self.assertEqual(len(called), 1)
        self.assertIn('Alpha prose', called[0])


class WorldBookDepthInjectionTest(WorldBookCase):
    def test_true_depth_injection_and_media_post_history_reminder(self):
        from backend import companion

        companion_db.update_companion(
            'c1',
            {
                'card_ui': {
                    'display_regex': [
                        {
                            'id': 'pimg',
                            'enabled': True,
                            'pattern': '<pimg="(.+?)">',
                            'replace': '<img src="{{raw::$1}}">',
                        }
                    ]
                }
            },
        )
        lore_id = world_book_db.add_entry(
            self.book_id,
            _entry(comment='World Lore', content='Castle history.', keys=[], constant=True, position=0, category='lore'),
        )
        media_id = world_book_db.add_entry(
            self.book_id,
            _entry(comment='Visual Asset Protocol', content='Emit <pimg="Evelyn_shy"> carefully.', keys=[], constant=True, position=0, category='media'),
        )
        world_book_db.associate_companion_with_world_book('c1', self.book_id, True)

        history = [
            {'role': 'user', 'content': 'msg1', 'message_type': 'chat'},
            {'role': 'assistant', 'content': 'reply1', 'message_type': 'chat'},
            {'role': 'user', 'content': 'msg2', 'message_type': 'chat'},
        ]

        # When media entry is in system (position=0), media sorts after lore and a concise
        # Visual Asset Reminder appears in trailing [Post-History Instructions] without duplicating content.
        msgs = companion.Companion('c1')._build_messages(history)
        sys_text = msgs[0]['content']
        self.assertLess(sys_text.index('Castle history.'), sys_text.index('Emit <pimg="Evelyn_shy">'))
        self.assertEqual(msgs[-1]['role'], 'system')
        self.assertIn('[Post-History Instructions]', msgs[-1]['content'])
        self.assertIn('Visual Asset Reminder:', msgs[-1]['content'])
        self.assertIn('<pimg="exact key">', msgs[-1]['content'])

        # Move media entry to true depth=1 (position=4), plus add a depth=2 lore note.
        media_entry = world_book_db.get_entry(media_id)
        media_entry['position'] = 4
        media_entry['depth'] = 1
        world_book_db.update_entry(media_id, media_entry)

        lore_entry = world_book_db.get_entry(lore_id)
        lore_entry['position'] = 4
        lore_entry['depth'] = 2
        world_book_db.update_entry(lore_id, lore_entry)

        msgs2 = companion.Companion('c1')._build_messages(history)
        self.assertNotIn('Emit <pimg="Evelyn_shy">', msgs2[0]['content'])
        self.assertNotIn('Castle history.', msgs2[0]['content'])
        # Since media is already at shallow depth=1 <= 2, no duplicate Post-History reminder is needed.
        self.assertFalse(any('Visual Asset Reminder:' in m['content'] for m in msgs2))

        contents = [m['content'] for m in msgs2]
        # history has 3 items (msg1, reply1, msg2):
        # depth=2 sits right before reply1 (last 2 messages); depth=1 sits right before msg2 (last 1 message).
        idx_lore = next(i for i, c in enumerate(contents) if '[World Info · Depth 2]' in c)
        idx_reply1 = contents.index('reply1')
        idx_media = next(i for i, c in enumerate(contents) if '[World Info · Depth 1]' in c)
        idx_msg2 = contents.index('msg2')
        self.assertEqual(idx_lore + 1, idx_reply1)
        self.assertEqual(idx_media + 1, idx_msg2)


if __name__ == '__main__':
    unittest.main()

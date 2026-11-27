# -*- coding: utf-8 -*-
"""World book activation: keyword matching, selective logic, probability."""

import unittest

from backend.world_book_engine import (
    SELECTIVE_LOGIC_AND_ALL,
    SELECTIVE_LOGIC_AND_ANY,
    SELECTIVE_LOGIC_NOT_ALL,
    SELECTIVE_LOGIC_NOT_ANY,
    WorldBookEngine,
)


def _entry(**over):
    data = {'id': 1, 'uid': 1, 'comment': 'e', 'keys': ['dragon'], 'content': 'dragons live here'}
    data.update(over)
    return data


def _engine(entries, budget=1000):
    return WorldBookEngine(entries, budget=budget)


class MatchKeysTest(unittest.TestCase):
    def setUp(self):
        self.engine = _engine([])

    def test_no_keys_never_matches(self):
        self.assertFalse(self.engine.match_keys('dragon', []))

    def test_plain_key_matches_case_insensitively(self):
        self.assertTrue(self.engine.match_keys('A Dragon appears', ['dragon']))

    def test_case_sensitive_misses_a_different_case(self):
        self.assertFalse(self.engine.match_keys('A Dragon appears', ['dragon'], case_sensitive=True))

    def test_whole_word_rejects_a_substring(self):
        self.assertFalse(self.engine.match_keys('dragons', ['dragon'], match_whole_words=True))

    def test_whole_word_accepts_the_exact_word(self):
        self.assertTrue(self.engine.match_keys('a dragon here', ['dragon'], match_whole_words=True))

    def test_regex_key_is_honoured(self):
        self.assertTrue(self.engine.match_keys('wyrm and drake', ['/wyrms?/']))

    def test_invalid_regex_is_ignored_not_raised(self):
        self.assertFalse(self.engine.match_keys('anything', ['/[unclosed/']))


class SelectiveLogicTest(unittest.TestCase):
    def setUp(self):
        self.engine = _engine([])

    def test_no_secondary_keys_passes(self):
        self.assertTrue(self.engine.check_selective_logic('text', [], SELECTIVE_LOGIC_AND_ANY))

    def test_and_any_needs_one_hit(self):
        self.assertTrue(self.engine.check_selective_logic('red', ['red', 'blue'], SELECTIVE_LOGIC_AND_ANY))
        self.assertFalse(self.engine.check_selective_logic('green', ['red', 'blue'], SELECTIVE_LOGIC_AND_ANY))

    def test_not_all_needs_one_miss(self):
        self.assertTrue(self.engine.check_selective_logic('red', ['red', 'blue'], SELECTIVE_LOGIC_NOT_ALL))
        self.assertFalse(self.engine.check_selective_logic('red blue', ['red', 'blue'], SELECTIVE_LOGIC_NOT_ALL))

    def test_not_any_needs_a_full_miss(self):
        self.assertTrue(self.engine.check_selective_logic('green', ['red', 'blue'], SELECTIVE_LOGIC_NOT_ANY))
        self.assertFalse(self.engine.check_selective_logic('red', ['red', 'blue'], SELECTIVE_LOGIC_NOT_ANY))

    def test_and_all_needs_every_hit(self):
        self.assertTrue(self.engine.check_selective_logic('red blue', ['red', 'blue'], SELECTIVE_LOGIC_AND_ALL))
        self.assertFalse(self.engine.check_selective_logic('red', ['red', 'blue'], SELECTIVE_LOGIC_AND_ALL))


class ProbabilityTest(unittest.TestCase):
    def setUp(self):
        self.engine = _engine([])

    def test_zero_never_fires(self):
        self.assertFalse(self.engine.check_probability(0))

    def test_hundred_always_fires(self):
        self.assertTrue(self.engine.check_probability(100))


class EvaluateTest(unittest.TestCase):
    def test_no_entries_activates_nothing(self):
        self.assertEqual(_engine([]).evaluate([{'role': 'user', 'content': 'hi'}]), [])

    def test_keyword_hit_activates_the_entry(self):
        activated = _engine([_entry()]).evaluate([{'role': 'user', 'content': 'a dragon'}])
        self.assertEqual(len(activated), 1)
        self.assertEqual(activated[0].entry.id, 1)

    def test_keyword_miss_activates_nothing(self):
        self.assertEqual(_engine([_entry()]).evaluate([{'role': 'user', 'content': 'a kitten'}]), [])

    def test_constant_entry_ignores_keywords(self):
        activated = _engine([_entry(keys=[], constant=True)]).evaluate(
            [{'role': 'user', 'content': 'unrelated'}]
        )
        self.assertEqual(len(activated), 1)

    def test_disabled_entry_is_skipped(self):
        self.assertEqual(
            _engine([_entry(disabled=True)]).evaluate([{'role': 'user', 'content': 'a dragon'}]),
            [],
        )


class PlanAndDepthTest(unittest.TestCase):
    def test_scan_depth_does_not_leak_to_following_entries(self):
        msgs = [
            {'role': 'user', 'content': 'old_keyword appears here'},
            {'role': 'assistant', 'content': 'middle turn'},
            {'role': 'user', 'content': 'latest turn'},
        ]
        entries = [
            _entry(id=1, uid=1, comment='short', keys=['missing'], scan_depth=1),
            _entry(id=2, uid=2, comment='default', keys=['old_keyword'], scan_depth=None, content='found old'),
        ]
        activated = _engine(entries).evaluate(msgs, scan_depth=10)
        self.assertEqual([a.entry.comment for a in activated], ['default'])

    def test_media_entries_sort_after_lore_inside_same_position(self):
        entries = [
            _entry(id=1, uid=1, comment='media', keys=[], constant=True, position=0, order=200, category='media', content='MEDIA_RULE'),
            _entry(id=2, uid=2, comment='lore', keys=[], constant=True, position=0, order=100, category='lore', content='LORE_TEXT'),
        ]
        engine = _engine(entries)
        plan = engine.build_plan(engine.evaluate([]))
        self.assertIsNotNone(plan.system_before)
        self.assertLess(plan.system_before.index('LORE_TEXT'), plan.system_before.index('MEDIA_RULE'))
        self.assertTrue(plan.has_media)
        self.assertFalse(plan.has_shallow_media)

    def test_position_4_builds_depth_entries_and_shallow_media_flag(self):
        entries = [
            _entry(id=1, uid=1, comment='m', keys=[], constant=True, position=4, depth=2, category='media', content='DEPTH_MEDIA'),
            _entry(id=2, uid=2, comment='l', keys=[], constant=True, position=1, category='lore', content='AFTER_LORE'),
        ]
        engine = _engine(entries)
        plan = engine.build_plan(engine.evaluate([]))
        self.assertIsNone(plan.system_before)
        self.assertIn('AFTER_LORE', plan.system_after)
        self.assertEqual(len(plan.depth_entries), 1)
        self.assertEqual(plan.depth_entries[0]['depth'], 2)
        self.assertEqual(plan.depth_entries[0]['content'], 'DEPTH_MEDIA')
        self.assertTrue(plan.has_shallow_media)


if __name__ == '__main__':
    unittest.main()

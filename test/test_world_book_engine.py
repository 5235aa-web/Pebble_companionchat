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


if __name__ == '__main__':
    unittest.main()

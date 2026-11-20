# -*- coding: utf-8 -*-
"""Stage directions are stripped before synthesis.

`_filter_tts_text` is the blunt version used for every engine: anything inside
brackets is a direction, not something to read aloud.
"""
import unittest

from backend.tts_service import _filter_tts_text


class LocalFilterTest(unittest.TestCase):
    def test_brackets_are_removed(self):
        self.assertEqual(_filter_tts_text('she smiled (laughs) and left'), 'she smiled  and left')

    def test_full_width_brackets_are_removed(self):
        self.assertEqual(_filter_tts_text('她笑了（笑声）然后走开'), '她笑了然后走开')

    def test_text_without_brackets_is_unchanged(self):
        self.assertEqual(_filter_tts_text('plain line'), 'plain line')

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(_filter_tts_text('  padded  '), 'padded')

    def test_a_line_that_is_only_a_stage_direction_becomes_empty(self):
        self.assertEqual(_filter_tts_text('(sighs)'), '')


if __name__ == '__main__':
    unittest.main()

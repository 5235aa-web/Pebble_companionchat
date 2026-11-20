# -*- coding: utf-8 -*-
"""Compaction helpers that need no LLM: markers, previews, message splitting."""

import unittest

from backend import compaction
from backend.compaction import CompactionConfig


def _msg(role='user', content='text', **over):
    data = {'role': role, 'content': content}
    data.update(over)
    return data


class SummaryDetectionTest(unittest.TestCase):
    def test_structured_summary_is_recognised(self):
        self.assertTrue(compaction.is_summary_message(_msg(message_type='compaction_summary')))

    def test_marker_in_content_is_recognised(self):
        text = f'{compaction.SUMMARY_MARKER_START}: v1]\n\nbody\n\n---'
        self.assertTrue(compaction.is_summary_message(_msg(content=text)))

    def test_plain_chat_is_not_a_summary(self):
        self.assertFalse(compaction.is_summary_message(_msg()))


class SummaryExtractionTest(unittest.TestCase):
    def test_body_is_pulled_out_of_the_marker_block(self):
        text = f'{compaction.SUMMARY_MARKER_START}: v1]\n\nthe body\n\n---\nold turns'
        self.assertEqual(compaction._extract_summary_content(text), 'the body')

    def test_structured_metadata_wins_over_the_marker(self):
        wrapped = _msg(
            message_type='compaction_summary',
            content='ignored',
            metadata={'summary': 'from metadata'},
        )
        self.assertEqual(compaction._find_existing_summary([wrapped]), 'from metadata')

    def test_no_summary_returns_none(self):
        self.assertIsNone(compaction._find_existing_summary([_msg()]))


class PreviewTest(unittest.TestCase):
    def test_short_summary_is_returned_as_is(self):
        self.assertEqual(compaction.get_summary_preview('short', 100), 'short')

    def test_long_summary_is_cut_with_an_ellipsis(self):
        preview = compaction.get_summary_preview('a' * 50, 10)
        self.assertEqual(len(preview), 13)
        self.assertTrue(preview.endswith('...'))
        self.assertTrue(preview.startswith('a' * 10))


class VersionTest(unittest.TestCase):
    def test_starts_at_one(self):
        self.assertEqual(compaction._get_next_compaction_version([_msg()]), 1)

    def test_increments_past_the_highest_seen(self):
        messages = [
            _msg(message_type='compaction_summary', metadata={'compaction_version': 1}),
            _msg(message_type='compaction_summary', metadata={'compaction_version': 3}),
            _msg(),
        ]
        self.assertEqual(compaction._get_next_compaction_version(messages), 4)


class SplitTest(unittest.TestCase):
    def test_recent_tail_is_preserved_verbatim(self):
        config = CompactionConfig(preserve_recent=3)
        messages = [_msg(content=f'm{i}') for i in range(10)]
        to_compact, to_preserve, important = compaction._split_messages_for_compaction(messages, config)
        self.assertEqual([m['content'] for m in to_preserve], ['m7', 'm8', 'm9'])
        self.assertEqual([m['content'] for m in to_compact], ['m0', 'm1', 'm2', 'm3', 'm4', 'm5', 'm6'])
        self.assertEqual(important, [])

    def test_previous_summary_always_goes_to_the_compact_side(self):
        config = CompactionConfig(preserve_recent=3)
        summary = _msg(message_type='compaction_summary', content='old')
        messages = [summary] + [_msg(content=f'm{i}') for i in range(5)]
        to_compact, to_preserve, _ = compaction._split_messages_for_compaction(messages, config)
        self.assertIn(summary, to_compact)
        self.assertNotIn(summary, to_preserve)

    def test_important_message_outside_the_tail_is_kept_separately(self):
        config = CompactionConfig(preserve_recent=2)
        flagged = _msg(content='remember this', is_important=True)
        messages = [flagged] + [_msg(content=f'm{i}') for i in range(5)]
        to_compact, to_preserve, important = compaction._split_messages_for_compaction(messages, config)
        self.assertIn(flagged, important)
        self.assertNotIn(flagged, to_compact)
        self.assertNotIn(flagged, to_preserve)

    def test_short_history_keeps_a_tail_to_preserve(self):
        # With a window of 10 and three messages, the split still holds the
        # newest one back: a branch must never be folded down to nothing.
        config = CompactionConfig(preserve_recent=10)
        messages = [_msg(content=f'm{i}') for i in range(3)]
        to_compact, to_preserve, _ = compaction._split_messages_for_compaction(messages, config)
        self.assertEqual([m['content'] for m in to_compact], ['m0'])
        self.assertEqual([m['content'] for m in to_preserve], ['m1', 'm2'])


if __name__ == '__main__':
    unittest.main()

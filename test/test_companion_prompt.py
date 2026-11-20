# -*- coding: utf-8 -*-
"""Character prompt assembly and the provider helpers around it."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import companion, companion_db, world_book_db


class LocalUrlTest(unittest.TestCase):
    def test_loopback_names_are_local(self):
        for url in ('http://localhost:11434', 'http://127.0.0.1:8080', 'http://0.0.0.0:8000'):
            self.assertTrue(companion._is_local_base_url(url), url)

    def test_remote_hosts_are_not_local(self):
        self.assertFalse(companion._is_local_base_url('https://api.openai.com/v1'))

    def test_missing_url_is_not_local(self):
        self.assertFalse(companion._is_local_base_url(None))
        self.assertFalse(companion._is_local_base_url(''))


class AudioRejectionTest(unittest.TestCase):
    """A refused input_audio block has to be recognised so the retry can drop it.

    Nothing decides up front whether a provider takes audio: the first request
    carries it, and a refusal teaches the client to send text only. Getting this
    predicate wrong either loses the turn (refusal treated as a normal error) or
    burns a round trip on every message.
    """

    def test_deepseek_refusal_is_recognised(self):
        chunk = {
            'type': 'error',
            'message': (
                'HTTP 422: {"error":{"message":"Failed to deserialize the JSON body into '
                'the target type: messages[31]: unknown variant `input_audio`, expected '
                'one of `text`, `image_url`, `file`"}}'
            ),
        }
        self.assertTrue(companion._is_audio_rejection(chunk))

    def test_other_wording_is_recognised(self):
        for message in (
            'unsupported content type: input_audio',
            'invalid content: audio block not supported',
            'the model does not support audio input',
        ):
            self.assertTrue(
                companion._is_audio_rejection({'type': 'error', 'message': message}), message
            )

    def test_unrelated_errors_are_left_alone(self):
        for message in (
            'HTTP 401: invalid api key',
            'unknown variant `foo`, expected one of `text`',
            'context length exceeded',
            '',
        ):
            self.assertFalse(
                companion._is_audio_rejection({'type': 'error', 'message': message}), message
            )

    def test_only_error_chunks_count(self):
        self.assertFalse(companion._is_audio_rejection(None))
        self.assertFalse(companion._is_audio_rejection({'type': 'content', 'content': 'hi'}))

    def test_strip_keeps_the_transcript(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': '你好'},
                {'type': 'input_audio', 'input_audio': {'data': 'x'}},
            ]},
        ]
        out = companion._strip_audio_parts(messages)
        self.assertEqual(out[0]['content'], 'sys')
        self.assertEqual(out[1]['content'], [{'type': 'text', 'text': '你好'}])

    def test_strip_leaves_a_placeholder_when_nothing_remains(self):
        messages = [{'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {}}]}]
        out = companion._strip_audio_parts(messages)
        self.assertEqual(out[0]['content'][0]['type'], 'text')
        self.assertTrue(out[0]['content'][0]['text'])


class InPlayPayloadTest(unittest.TestCase):
    def test_shape(self):
        payload = companion._in_play_payload([
            {'id': 1, 'content': 'likes tea', 'importance': 2, 'keys': ['tea']},
        ])
        self.assertEqual(len(payload), 1)
        entry = payload[0]
        self.assertEqual(entry['id'], 1)
        self.assertEqual(entry['key'], 'tea')
        self.assertEqual(entry['label'], 'likes tea')

    def test_label_is_truncated(self):
        payload = companion._in_play_payload([{'id': 1, 'content': 'x' * 200}])
        self.assertEqual(len(payload[0]['label']), 60)

    def test_importance_is_clamped(self):
        high = companion._in_play_payload([{'id': 1, 'content': 'a', 'importance': 99}])
        low = companion._in_play_payload([{'id': 2, 'content': 'b', 'importance': 0}])
        self.assertEqual(high[0]['weight'], 1.0)
        self.assertLess(low[0]['weight'], 1.0)

    def test_garbage_importance_falls_back(self):
        payload = companion._in_play_payload([{'id': 1, 'content': 'a', 'importance': 'high'}])
        self.assertGreater(payload[0]['weight'], 0)

    def test_missing_keys_yields_an_empty_key(self):
        self.assertEqual(companion._in_play_payload([{'content': 'a'}])[0]['key'], '')


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-prompt-'))
        self._companion = companion_db.DB_PATH
        self._world = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / 'companions.db'
        world_book_db.DB_PATH = companion_db.DB_PATH
        companion_db.create_companion('c1', {
            'name': 'Aria',
            'description': 'a wandering bard',
            'personality': 'warm, teasing',
            'scenario': 'a harbour tavern',
        })

    def tearDown(self):
        companion_db.DB_PATH = self._companion
        world_book_db.DB_PATH = self._world
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_card_fields_are_tagged(self):
        prompt = companion.Companion('c1').build_system_prompt()
        self.assertIn('[Character]', prompt)
        self.assertIn('Aria', prompt)
        self.assertIn('[Description]', prompt)
        self.assertIn('a wandering bard', prompt)
        self.assertIn('[Personality]', prompt)
        self.assertIn('[Scenario]', prompt)

    def test_empty_card_fields_are_omitted(self):
        companion_db.create_companion('c2', {'name': 'Bare'})
        prompt = companion.Companion('c2').build_system_prompt()
        self.assertIn('[Character]', prompt)
        self.assertNotIn('[Description]', prompt)
        self.assertNotIn('[Personality]', prompt)

    def test_memory_out_starts_empty(self):
        memory_out = []
        companion.Companion('c1').build_system_prompt(memory_out=memory_out)
        self.assertEqual(memory_out, [])

    def test_narration_prompt_is_concise_and_tag_free(self):
        c = companion.Companion('c1')
        self.assertNotIn('third-person voice-over', c.build_system_prompt([]))

        history = [
            {'role': 'user', 'content': 'Hi', 'message_type': 'chat'},
            {'role': 'user', 'content': 'Rain taps on the window.', 'message_type': 'narration'},
        ]
        prompt = c.build_system_prompt(history)
        self.assertIn('third-person voice-over', prompt)
        self.assertIn('Your next reply must be based on that narration message.', prompt)
        self.assertNotIn('[Narration]', prompt)
        self.assertNotIn('[/Narration]', prompt)
        self.assertNotIn('For example', prompt)

        messages = c._build_messages(history)
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertEqual(messages[-1]['content'], 'Narration: Rain taps on the window.')
        self.assertNotIn('[Narration]', messages[-1]['content'])


if __name__ == '__main__':
    unittest.main()

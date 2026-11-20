# -*- coding: utf-8 -*-
"""Provider whitelist, config loading and the stream-cleaning patterns."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import llm_client


class ProviderWhitelistTest(unittest.TestCase):
    def test_expected_providers_are_present(self):
        for name in ('openai', 'anthropic', 'gemini', 'llama-cpp'):
            self.assertIn(name, llm_client.SUPPORTED_PROVIDERS)

    def test_list_has_no_duplicates(self):
        names = llm_client.SUPPORTED_PROVIDERS
        self.assertEqual(len(names), len(set(names)))


class LoadDefaultConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-llm-'))
        self._cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(llm_client.load_default_llm_config())

    def test_broken_file_returns_none(self):
        Path('llm_providers.json').write_text('{bad', encoding='utf-8')
        self.assertIsNone(llm_client.load_default_llm_config())

    def test_active_provider_is_returned(self):
        payload = {
            'providers': [
                {'provider': 'openai', 'apiKey': 'sk-a', 'models': ['gpt-4']},
                {'provider': 'deepseek', 'apiKey': 'sk-b', 'models': ['deepseek-chat']},
            ],
            'activeProviderIndex': 1,
        }
        Path('llm_providers.json').write_text(json.dumps(payload), encoding='utf-8')
        loaded = llm_client.load_default_llm_config()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get('provider_type'), 'deepseek')
        self.assertEqual(loaded.get('api_key'), 'sk-b')
        self.assertEqual(loaded.get('model'), 'deepseek-chat')

    def test_empty_provider_list_returns_none(self):
        Path('llm_providers.json').write_text('{"providers": []}', encoding='utf-8')
        self.assertIsNone(llm_client.load_default_llm_config())


class StreamCleanupPatternTest(unittest.TestCase):
    def test_reasoning_tag_is_extracted(self):
        match = llm_client.THINK_TAG_PATTERN.search('a <think>plan</think> b')
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), 'plan')

    def test_reasoning_tag_is_case_insensitive(self):
        self.assertIsNotNone(llm_client.THINK_TAG_PATTERN.search('<Think>x</Think>'))

    def test_end_of_turn_tokens_are_matched(self):
        self.assertTrue(llm_client.EOT_TOKEN_PATTERN.search('done<|eot_id|>'))
        self.assertTrue(llm_client.EOS_TOKEN_PATTERN.search('done</s>'))
        self.assertIsNone(llm_client.EOT_TOKEN_PATTERN.search('plain text'))

    def test_needs_check_marker_is_matched(self):
        match = llm_client.NEEDS_CHECK_PATTERN.search('text [NEEDS CHECK: x] tail')
        self.assertIsNotNone(match)
        self.assertIn('NEEDS CHECK', match.group(0))


if __name__ == '__main__':
    unittest.main()

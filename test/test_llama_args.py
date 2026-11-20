# -*- coding: utf-8 -*-
"""llama-server argument building and device parsing.

Both are pure functions: no subprocess, no weights, no GPU.
"""

import unittest

from backend import llama_server_engine as llama


SAMPLE_DEVICES = """
Available devices:
  Vulkan0: AMD Radeon RX 9070 GRE (12224 MiB, 11348 MiB free)
  Vulkan1: AMD Radeon(TM) Graphics (7993 MiB, 7594 MiB free)
"""


class BoolFlagTest(unittest.TestCase):
    def test_true_is_on(self):
        self.assertEqual(llama._bool_flag(True), 'on')

    def test_false_is_off(self):
        self.assertEqual(llama._bool_flag(False), 'off')


class ParseDevicesTest(unittest.TestCase):
    def test_two_devices_are_parsed(self):
        devices = llama._parse_devices(SAMPLE_DEVICES)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['id'], 'Vulkan0')
        self.assertEqual(devices[0]['vram_mib'], '12224')
        self.assertIn('Graphics', devices[1]['name'])

    def test_output_without_devices_is_empty(self):
        self.assertEqual(llama._parse_devices('no devices here'), [])

    def test_partial_lines_are_skipped(self):
        self.assertEqual(llama._parse_devices('  Vulkan0: broken'), [])


class DescribeModelsTest(unittest.TestCase):
    def test_every_registry_entry_is_described(self):
        described = llama.describe_models()
        self.assertEqual(len(described), len(llama.MODEL_REGISTRY))
        for item in described:
            self.assertIn('key', item)
            self.assertIn('label', item)
            self.assertIn('available', item)
            self.assertGreater(item['ctx'], 0)

    def test_registry_keys_are_all_listed(self):
        keys = {item['key'] for item in llama.describe_models()}
        self.assertEqual(keys, set(llama.MODEL_REGISTRY))


class BuildArgsTest(unittest.TestCase):
    def test_alias_matches_the_model_key(self):
        args = llama.build_args(llama.DEFAULT_MODEL_KEY)
        self.assertIn('--alias', args)
        self.assertEqual(args[args.index('--alias') + 1], llama.DEFAULT_MODEL_KEY)

    def test_weights_and_context_are_pinned(self):
        args = llama.build_args(llama.DEFAULT_MODEL_KEY)
        self.assertIn('-m', args)
        self.assertIn('-c', args)

    def test_flash_attention_is_on(self):
        args = llama.build_args(llama.DEFAULT_MODEL_KEY)
        self.assertIn('-fa', args)
        self.assertEqual(args[args.index('-fa') + 1], 'on')

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(Exception):
            llama.build_args('definitely-not-a-model')

    def test_draft_head_is_only_added_when_the_file_exists(self):
        for key, spec in llama.MODEL_REGISTRY.items():
            args = llama.build_args(key, enable_mtp=True)
            added = '--model-draft' in args
            expected = bool(spec.get('mtp')) and llama.Path(spec['mtp']).exists()
            self.assertEqual(added, expected, key)


if __name__ == '__main__':
    unittest.main()

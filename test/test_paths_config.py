# -*- coding: utf-8 -*-
"""Where the STT engine choice is stored, and how an old file is picked up.

A frozen backend runs from a temp directory, so anything written next to the
code is gone on exit. This is the regression that made the packaged build forget
which engine the user picked.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import paths, stt_registry


class PathContractTest(unittest.TestCase):
    def test_config_sits_in_the_config_dir(self):
        self.assertEqual(paths.STT_PROVIDERS_PATH.parent, paths.CONFIG_DIR)
        self.assertEqual(paths.STT_PROVIDERS_PATH.name, 'stt_providers.json')

    def test_config_is_absolute_and_under_the_data_root(self):
        self.assertTrue(os.path.isabs(str(paths.STT_PROVIDERS_PATH)))
        self.assertTrue(
            str(paths.STT_PROVIDERS_PATH).startswith(str(paths.DATA_ROOT)),
        )

    def test_dead_constant_is_gone(self):
        """STT_CONFIG_PATH pointed at config/stt.json and was never read."""
        self.assertFalse(hasattr(paths, 'STT_CONFIG_PATH'))

    def test_registry_uses_the_shared_constant(self):
        self.assertEqual(stt_registry.CONFIG_FILE, str(paths.STT_PROVIDERS_PATH))

    def test_legacy_path_is_next_to_the_module(self):
        """It only exists in a source checkout, which is what migration relies on."""
        self.assertEqual(
            Path(stt_registry.LEGACY_CONFIG_FILE).parent,
            Path(stt_registry.__file__).resolve().parent,
        )


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.reg = stt_registry.STTRegistry()
        self.reg._active_id = 'qwen3_asr'

    def test_save_recreates_a_missing_config_dir(self):
        """A fresh install has no config directory; the save has to create it.

        The directory under test is a throwaway one. Deleting the shared
        ``paths.CONFIG_DIR`` is what destroyed a real install once: run this
        file directly and the data root is the user's own.
        """
        root = Path(tempfile.mkdtemp(prefix='pebble-config-'))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        target = root / 'config' / 'stt_providers.json'
        original_file = stt_registry.CONFIG_FILE
        original_path = stt_registry.STT_PROVIDERS_PATH
        try:
            stt_registry.CONFIG_FILE = str(target)
            stt_registry.STT_PROVIDERS_PATH = target
            self.assertFalse(target.parent.exists())
            self.reg._active_id = 'confucius4_r2t2'
            self.reg._save_active_to_file()
            self.assertTrue(target.is_file())
            with open(target, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), {'active': 'confucius4_r2t2'})
        finally:
            stt_registry.CONFIG_FILE = original_file
            stt_registry.STT_PROVIDERS_PATH = original_path

    def test_save_is_atomic_at_the_file_level(self):
        """The temp file must not survive a successful save."""
        self.reg._save_active_to_file()
        leftovers = list(paths.CONFIG_DIR.glob('stt_providers.json.tmp'))
        self.assertEqual(leftovers, [])

    def test_a_new_registry_reads_the_saved_choice(self):
        self.reg._active_id = 'confucius4_r2t2'
        self.reg._save_active_to_file()
        self.assertEqual(stt_registry.STTRegistry().active_id, 'confucius4_r2t2')

    def test_a_broken_file_falls_back_to_the_default(self):
        paths.STT_PROVIDERS_PATH.write_text('{not json', encoding='utf-8')
        self.assertEqual(
            stt_registry.STTRegistry().active_id, stt_registry.DEFAULT_ENGINE_ID,
        )

    def test_an_unknown_engine_falls_back_to_the_default(self):
        paths.STT_PROVIDERS_PATH.write_text(
            json.dumps({'active': 'some_removed_engine'}), encoding='utf-8',
        )
        self.assertEqual(
            stt_registry.STTRegistry().active_id, stt_registry.DEFAULT_ENGINE_ID,
        )


class MigrationTest(unittest.TestCase):
    def setUp(self):
        paths.STT_PROVIDERS_PATH.unlink(missing_ok=True)

    def tearDown(self):
        paths.STT_PROVIDERS_PATH.unlink(missing_ok=True)

    def test_legacy_file_is_copied_when_the_new_one_is_absent(self):
        legacy = Path(stt_registry.LEGACY_CONFIG_FILE)
        if not legacy.is_file():
            self.skipTest('no legacy file in this checkout')
        stt_registry._migrate_legacy_config()
        self.assertTrue(paths.STT_PROVIDERS_PATH.is_file())
        with open(legacy, encoding='utf-8') as handle:
            expected = json.load(handle)
        with open(paths.STT_PROVIDERS_PATH, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), expected)

    def test_existing_file_is_never_overwritten(self):
        paths.STT_PROVIDERS_PATH.write_text(
            json.dumps({'active': 'qwen3_asr'}), encoding='utf-8',
        )
        stt_registry._migrate_legacy_config()
        with open(paths.STT_PROVIDERS_PATH, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), {'active': 'qwen3_asr'})

    def test_missing_legacy_file_is_not_an_error(self):
        original = stt_registry.LEGACY_CONFIG_FILE
        try:
            stt_registry.LEGACY_CONFIG_FILE = os.path.join(
                str(paths.DATA_ROOT), 'no-such-legacy.json',
            )
            stt_registry._migrate_legacy_config()
            self.assertFalse(paths.STT_PROVIDERS_PATH.exists())
        finally:
            stt_registry.LEGACY_CONFIG_FILE = original


if __name__ == '__main__':
    unittest.main()

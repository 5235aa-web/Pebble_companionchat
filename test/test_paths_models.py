# -*- coding: utf-8 -*-
"""Model root resolution and the atomic write behind app.json.

A wrong root here points every engine at a missing weight file, so the
precedence order (UI setting > environment > default) is worth pinning down.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import paths


class ModelsRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-models-'))
        self._app_config = paths.APP_CONFIG_PATH
        self._env = os.environ.pop('PEBBLE_MODELS_ROOT', None)
        paths.APP_CONFIG_PATH = self.tmp / 'app.json'

    def tearDown(self):
        paths.APP_CONFIG_PATH = self._app_config
        os.environ.pop('PEBBLE_MODELS_ROOT', None)
        if self._env is not None:
            os.environ['PEBBLE_MODELS_ROOT'] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_when_nothing_is_configured(self):
        self.assertEqual(paths.models_root(), paths.DEFAULT_MODELS_ROOT)

    def test_environment_variable_is_used(self):
        os.environ['PEBBLE_MODELS_ROOT'] = str(self.tmp / 'from-env')
        self.assertEqual(paths.models_root(), self.tmp / 'from-env')

    def test_config_file_beats_the_environment(self):
        paths.save_app_config({'modelsRoot': str(self.tmp / 'from-config')})
        os.environ['PEBBLE_MODELS_ROOT'] = str(self.tmp / 'from-env')
        self.assertEqual(paths.models_root(), self.tmp / 'from-config')

    def test_blank_config_falls_through_to_the_environment(self):
        paths.save_app_config({'modelsRoot': '   '})
        os.environ['PEBBLE_MODELS_ROOT'] = str(self.tmp / 'from-env')
        self.assertEqual(paths.models_root(), self.tmp / 'from-env')

    def test_model_path_joins_under_the_root(self):
        paths.save_app_config({'modelsRoot': str(self.tmp / 'm')})
        self.assertEqual(paths.model_path('sub', 'w.gguf'), self.tmp / 'm' / 'sub' / 'w.gguf')


class AppConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pebble-appcfg-'))
        self._app_config = paths.APP_CONFIG_PATH
        paths.APP_CONFIG_PATH = self.tmp / 'app.json'

    def tearDown(self):
        paths.APP_CONFIG_PATH = self._app_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_reads_as_an_empty_dict(self):
        self.assertEqual(paths.load_app_config(), {})

    def test_round_trip(self):
        payload = {'modelsRoot': 'D:/models', 'llamaRoot': 'D:/llama'}
        paths.save_app_config(payload)
        self.assertEqual(paths.load_app_config(), payload)

    def test_broken_json_reads_as_an_empty_dict(self):
        paths.APP_CONFIG_PATH.write_text('{not json', encoding='utf-8')
        self.assertEqual(paths.load_app_config(), {})

    def test_non_dict_payload_reads_as_an_empty_dict(self):
        paths.APP_CONFIG_PATH.write_text('[1, 2, 3]', encoding='utf-8')
        self.assertEqual(paths.load_app_config(), {})

    def test_write_leaves_no_temporary_file(self):
        paths.save_app_config({'a': 1})
        leftovers = [p.name for p in self.tmp.iterdir() if p.name != 'app.json']
        self.assertEqual(leftovers, [])

    def test_write_replaces_contents_completely(self):
        paths.save_app_config({'a': 1, 'b': 2})
        paths.save_app_config({'a': 1})
        self.assertEqual(paths.load_app_config(), {'a': 1})


class EnsureDirsTest(unittest.TestCase):
    def test_is_idempotent_and_creates_every_directory(self):
        paths.ensure_dirs()
        paths.ensure_dirs()
        for directory in (
            paths.DATA_ROOT,
            paths.UPLOAD_ROOT,
            paths.ATTACHMENT_DIR,
            paths.AVATAR_DIR,
            paths.CONFIG_DIR,
            paths.LOG_DIR,
            paths.VOICE_DIR,
            paths.ENGINE_STATE_DIR,
        ):
            self.assertTrue(directory.is_dir(), str(directory))


if __name__ == '__main__':
    unittest.main()

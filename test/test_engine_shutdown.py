# -*- coding: utf-8 -*-
"""The two shutdown entry points.

`shutdown_stt` is the one the ASGI lifespan calls; `shutdown_all` is the one the
packaged backend calls. They must not be the same list: stopping the TTS and LLM
engines on every `--reload` would reload gigabytes of weights.
"""
import sys
import types
import unittest

from backend import engine_shutdown, stt_registry

FAKE_MODULE = 'backend._fake_engine_for_test'


class FakeEngine:
    def __init__(self, verdict='stopped'):
        self.verdict = verdict

    def stop(self):
        return self.verdict


class ShutdownSttTest(unittest.TestCase):
    def setUp(self):
        self.reg = stt_registry.get_registry()
        self.reg._instances.clear()

    def tearDown(self):
        self.reg._instances.clear()

    def test_nothing_loaded_returns_empty(self):
        self.assertEqual(engine_shutdown.shutdown_stt(), [])

    def test_returns_only_engines_that_stopped(self):
        self.reg._instances['qwen3_asr'] = FakeEngine('stopped')
        self.reg._instances['confucius4_r2t2'] = FakeEngine('failed')
        self.assertEqual(engine_shutdown.shutdown_stt(), ['qwen3_asr'])
        self.assertIn('confucius4_r2t2', self.reg._instances)

    def test_shutdown_all_is_safe_with_no_engines_loaded(self):
        engine_shutdown.shutdown_all()

    def test_shutdown_stt_is_idempotent(self):
        self.reg._instances['qwen3_asr'] = FakeEngine('stopped')
        self.assertEqual(engine_shutdown.shutdown_stt(), ['qwen3_asr'])
        self.assertEqual(engine_shutdown.shutdown_stt(), [])


class ModuleEngineTest(unittest.TestCase):
    """Only modules that were actually imported are touched."""

    def tearDown(self):
        sys.modules.pop(FAKE_MODULE, None)

    def test_imported_module_is_closed(self):
        calls = []
        module = types.ModuleType(FAKE_MODULE)
        module._fake = types.SimpleNamespace(close=lambda: calls.append(1))
        sys.modules[FAKE_MODULE] = module
        engine_shutdown._close_module_engine('fake', '_fake_engine_for_test', '_fake')
        self.assertEqual(calls, [1])

    def test_missing_module_is_skipped_silently(self):
        sys.modules.pop(FAKE_MODULE, None)
        # Must not raise, and must not import the module to close it.
        engine_shutdown._close_module_engine('fake', '_fake_engine_for_test', '_fake')
        self.assertNotIn(FAKE_MODULE, sys.modules)

    def test_close_that_raises_does_not_stop_the_sweep(self):
        def boom():
            raise RuntimeError('boom')

        module = types.ModuleType(FAKE_MODULE)
        module._fake = types.SimpleNamespace(close=boom)
        sys.modules[FAKE_MODULE] = module
        engine_shutdown._close_module_engine('fake', '_fake_engine_for_test', '_fake')
        self.assertIn(FAKE_MODULE, sys.modules)


class ModuleListTest(unittest.TestCase):
    """The list is the single source of truth, so it has to be complete."""

    def test_every_local_module_engine_is_listed(self):
        listed = {name for _, name, _ in engine_shutdown._MODULE_ENGINES}
        for expected in ('omnivoice_engine', 'dotstts_engine', 'fishaudio_engine',
                         'ming3tts_engine', 'llama_server_engine'):
            self.assertIn(expected, listed)

    def test_stt_engines_are_not_in_the_module_list(self):
        """They live behind the registry, which owns their instances."""
        listed = {name for _, name, _ in engine_shutdown._MODULE_ENGINES}
        self.assertNotIn('qwen3asr_stt_engine', listed)
        self.assertNotIn('confucius4_stt_engine', listed)

    def test_shutdown_inochi2d_is_safe_and_calls_loaded_service(self):
        from backend.Inochi2D import service as inochi_service

        calls = []
        orig = inochi_service.shutdown_workers
        try:
            inochi_service.shutdown_workers = lambda: calls.append('killed') or 1
            self.assertEqual(engine_shutdown.shutdown_inochi2d(), 1)
            self.assertEqual(calls, ['killed'])
        finally:
            inochi_service.shutdown_workers = orig


if __name__ == '__main__':
    unittest.main()

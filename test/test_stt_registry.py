# -*- coding: utf-8 -*-
"""The STT registry's unloading surface.

The point of these tests is that the registry never claims an engine is gone
while it still holds the port: a failed stop keeps the instance reachable.
"""
import json
import os
import unittest

from backend import stt_registry


class FakeEngine:
    """Stands in for an STT engine; records how often stop() was called."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def stop(self):
        self.calls += 1
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class RegistryStopTest(unittest.TestCase):
    def setUp(self):
        # Start from a known engine choice: earlier tests in the same process
        # legitimately write the active id to the shared temp data root.
        self.reg = stt_registry.STTRegistry()
        self.reg._instances.clear()
        self.reg._active_id = 'qwen3_asr'

    def test_empty_registry_reports_idle(self):
        self.assertEqual(self.reg.stop_engine('qwen3_asr'), 'idle')
        self.assertEqual(self.reg.stop_all(), [])
        self.assertEqual(self.reg.stop_active(), ('qwen3_asr', 'idle'))

    def test_stopped_engine_is_dropped_from_the_cache(self):
        engine = FakeEngine('stopped')
        self.reg._instances['qwen3_asr'] = engine
        self.assertEqual(self.reg.stop_engine('qwen3_asr'), 'stopped')
        self.assertEqual(engine.calls, 1)
        self.assertNotIn('qwen3_asr', self.reg._instances)

    def test_idle_engine_is_dropped_from_the_cache(self):
        engine = FakeEngine('idle')
        self.reg._instances['qwen3_asr'] = engine
        self.assertEqual(self.reg.stop_engine('qwen3_asr'), 'idle')
        self.assertNotIn('qwen3_asr', self.reg._instances)

    def test_failed_engine_keeps_its_instance(self):
        """Otherwise the process outlives its only handle and can never be ended."""
        engine = FakeEngine('failed')
        self.reg._instances['qwen3_asr'] = engine
        self.assertEqual(self.reg.stop_engine('qwen3_asr'), 'failed')
        self.assertIn('qwen3_asr', self.reg._instances)
        self.assertIs(self.reg._instances['qwen3_asr'], engine)

    def test_raising_engine_is_reported_as_failed_and_kept(self):
        engine = FakeEngine(RuntimeError('boom'))
        self.reg._instances['qwen3_asr'] = engine
        self.assertEqual(self.reg.stop_engine('qwen3_asr'), 'failed')
        self.assertIn('qwen3_asr', self.reg._instances)

    def test_stop_all_lists_only_what_stopped(self):
        self.reg._instances['qwen3_asr'] = FakeEngine('stopped')
        self.reg._instances['confucius4_r2t2'] = FakeEngine('failed')
        self.assertEqual(self.reg.stop_all(), ['qwen3_asr'])
        self.assertEqual(list(self.reg._instances), ['confucius4_r2t2'])

    def test_stop_active_returns_the_engine_it_tried(self):
        self.reg._active_id = 'confucius4_r2t2'
        engine = FakeEngine('stopped')
        self.reg._instances['confucius4_r2t2'] = engine
        self.assertEqual(self.reg.stop_active(), ('confucius4_r2t2', 'stopped'))
        self.assertEqual(engine.calls, 1)


class RegistrySetActiveTest(unittest.TestCase):
    """Switching engines stops the previous one, and must not orphan it."""

    def setUp(self):
        self.reg = stt_registry.STTRegistry()
        self.reg._instances.clear()
        self.reg._active_id = 'qwen3_asr'

    def test_switch_stops_the_previous_engine(self):
        old = FakeEngine('stopped')
        self.reg._instances['qwen3_asr'] = old
        self.reg.set_active('confucius4_r2t2')
        self.assertEqual(old.calls, 1)
        self.assertEqual(self.reg.active_id, 'confucius4_r2t2')
        self.assertNotIn('qwen3_asr', self.reg._instances)

    def test_switch_keeps_a_previous_engine_that_would_not_stop(self):
        old = FakeEngine('failed')
        self.reg._instances['qwen3_asr'] = old
        self.reg.set_active('confucius4_r2t2')
        self.assertEqual(self.reg.active_id, 'confucius4_r2t2')
        self.assertIs(self.reg._instances.get('qwen3_asr'), old)

    def test_switching_to_the_active_engine_changes_nothing(self):
        old = FakeEngine('stopped')
        self.reg._instances['qwen3_asr'] = old
        self.reg.set_active('qwen3_asr')
        self.assertEqual(old.calls, 0)
        self.assertIn('qwen3_asr', self.reg._instances)

    def test_unknown_engine_is_rejected(self):
        with self.assertRaises(KeyError):
            self.reg.set_active('nope')

    def test_switch_is_persisted(self):
        self.reg.set_active('confucius4_r2t2')
        path = stt_registry.CONFIG_FILE
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), {'active': 'confucius4_r2t2'})

    def test_status_reports_load_state_per_engine(self):
        self.reg._instances['qwen3_asr'] = FakeEngine('stopped')
        status = self.reg.status()
        loaded = {e['id']: e['loaded'] for e in status['engines']}
        self.assertTrue(loaded['qwen3_asr'])
        self.assertFalse(loaded['confucius4_r2t2'])
        self.assertEqual(status['active'], 'qwen3_asr')


if __name__ == '__main__':
    unittest.main()

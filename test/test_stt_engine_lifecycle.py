# -*- coding: utf-8 -*-
"""Lifecycle verdicts of the two STT engines.

The contract these tests pin down: every stop path reports what actually
happened, and a process that refuses to die keeps its handle so a later attempt
can still reach it.
"""
import unittest

from . import _helpers


class KillLockedTest(unittest.TestCase):
    """_kill_locked only ever touches the child this process spawned."""

    def test_no_child_reports_idle(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._ready = True
                self.assertEqual(inst._kill_locked(), 'idle')
                self.assertIsNone(inst._proc)
                self.assertFalse(inst._ready)

    def test_dead_child_reports_stopped_and_drops_the_handle(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                proc = _helpers.DeadProc()
                inst._proc = proc
                self.assertEqual(inst._kill_locked(), 'stopped')
                self.assertIsNone(inst._proc)
                self.assertFalse(proc.terminated)

    def test_live_child_is_terminated(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                proc = _helpers.LiveProc()
                inst._proc = proc
                self.assertEqual(inst._kill_locked(), 'stopped')
                self.assertTrue(proc.terminated)
                self.assertIsNone(inst._proc)

    def test_stubborn_child_reports_failed_and_keeps_the_handle(self):
        """A survivor must stay reachable, or every later stop lies about it."""
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                proc = _helpers.StubbornProc()
                inst._proc = proc
                self.assertEqual(inst._kill_locked(), 'failed')
                self.assertIs(inst._proc, proc)
                self.assertTrue(proc.terminated)
                self.assertTrue(proc.killed)
                # A second attempt still sees the same process, not 'idle'.
                self.assertEqual(inst._kill_locked(), 'failed')
                self.assertIs(inst._proc, proc)

    def test_broken_handle_reports_failed_instead_of_raising(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = 'not-a-process'
                self.assertEqual(inst._kill_locked(), 'failed')


class ReleaseLockedTest(unittest.TestCase):
    """_release_locked ends whatever holds the port, spawned here or not."""

    def test_nothing_listening_reports_idle(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                self.assertEqual(inst._release_locked(), 'idle')

    def test_own_child_is_enough(self):
        """Once our own child is gone there is nothing left to sweep."""
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = _helpers.LiveProc()
                swept = []
                inst._healthy = lambda: True
                inst._kill_port_owner = lambda: swept.append(1) or True
                self.assertEqual(inst._release_locked(), 'stopped')
                self.assertEqual(swept, [])

    def test_orphan_is_swept_by_port(self):
        """The reused-instance case: no handle, but a server answers /health."""
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                swept = []
                inst._healthy = lambda: True
                inst._kill_port_owner = lambda: swept.append(1) or True
                self.assertEqual(inst._release_locked(), 'stopped')
                self.assertEqual(swept, [1])

    def test_unsweepable_orphan_reports_failed(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._healthy = lambda: True
                inst._kill_port_owner = lambda: False
                self.assertEqual(inst._release_locked(), 'failed')

    def test_stubborn_child_is_not_reported_as_released(self):
        """terminate and kill both failed, and no port answers: still 'failed'."""
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = _helpers.StubbornProc()
                self.assertEqual(inst._release_locked(), 'failed')
                self.assertIsNotNone(inst._proc)

    def test_stubborn_child_is_swept_when_the_port_still_answers(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = _helpers.StubbornProc()
                inst._healthy = lambda: True
                inst._kill_port_owner = lambda: True
                self.assertEqual(inst._release_locked(), 'stopped')


class StopTest(unittest.TestCase):
    """stop() is the public surface and must be safe to call twice."""

    def test_stop_is_idempotent(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = _helpers.LiveProc()
                self.assertEqual(inst.stop(), 'stopped')
                self.assertEqual(inst.stop(), 'idle')

    def test_close_ignores_the_verdict(self):
        for cls in _helpers.stt_engines():
            with self.subTest(engine=cls.__name__):
                inst = _helpers.engine_instance(cls)
                inst._proc = _helpers.LiveProc()
                self.assertIsNone(inst.close())
                self.assertIsNone(inst._proc)


if __name__ == '__main__':
    unittest.main()

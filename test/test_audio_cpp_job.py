# -*- coding: utf-8 -*-
"""The kill-on-exit job object.

A real child is spawned here on purpose: the whole point of the module is that
the OS, not Python, ends the process, and only a real handle proves that.
"""
import os
import subprocess
import sys
import unittest

from . import _helpers
from backend import audio_cpp_job


def _adopt_real_child():
    """Spawn a sleeping child and put it in the job. Caller must reap it."""
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        adopted = audio_cpp_job.adopt(proc)
    except Exception:
        proc.kill()
        proc.wait(timeout=10)
        raise
    return proc, adopted


class JobHandleTest(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'job objects are Windows-only')
    def test_handle_is_created_once(self):
        lib = audio_cpp_job._lib()
        first = audio_cpp_job._job_handle(lib)
        second = audio_cpp_job._job_handle(lib)
        self.assertTrue(first)
        self.assertEqual(first, second)

    @unittest.skipUnless(os.name == 'nt', 'job objects are Windows-only')
    def test_this_process_is_a_member_so_children_inherit(self):
        # Adoption alone only covers a graceful exit. A backend killed through
        # the terminal never runs atexit, and its engines outlived it twice:
        # audiocpp on 18244 and llama-server on 8080. Membership is the fix -
        # Windows joins every child of a member to the job at creation time.
        lib = audio_cpp_job._lib()
        self.assertTrue(audio_cpp_job._job_handle(lib))
        self.assertTrue(audio_cpp_job._self_bound)

    def test_adopt_off_windows_is_a_no_op(self):
        original = os.name
        try:
            os.name = 'posix'
            self.assertFalse(audio_cpp_job.adopt(_helpers.LiveProc()))
        finally:
            os.name = original


class AdoptRejectionTest(unittest.TestCase):
    """Every rejection path has to answer False, never raise."""

    def test_none(self):
        self.assertFalse(audio_cpp_job.adopt(None))

    def test_already_dead_child(self):
        self.assertFalse(audio_cpp_job.adopt(_helpers.DeadProc()))

    def test_child_whose_pid_does_not_exist(self):
        # The stand-ins carry a pid no process can hold, so OpenProcess fails;
        # that must come back as False rather than an exception.
        self.assertFalse(audio_cpp_job.adopt(_helpers.LiveProc()))


@unittest.skipUnless(os.name == 'nt', 'job objects are Windows-only')
class AdoptRealChildTest(unittest.TestCase):
    def test_real_child_is_adopted(self):
        proc, adopted = _adopt_real_child()
        try:
            self.assertTrue(adopted)
            # Membership, not the return value, is what keeps the child from
            # outliving us: a child spawned before the job existed is not
            # inherited, and calling that adopted is how audiocpp survived.
            self.assertTrue(audio_cpp_job.is_member(proc.pid))
            # Adopting the same child twice is a no-op, not an error.
            self.assertTrue(audio_cpp_job.adopt(proc))
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_dead_child_after_the_fact_is_rejected(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait(timeout=30)
        self.assertFalse(audio_cpp_job.adopt(proc))


if __name__ == '__main__':
    unittest.main()

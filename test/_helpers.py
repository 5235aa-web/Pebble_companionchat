# -*- coding: utf-8 -*-
"""Fixtures shared by the backend tests.

Nothing here starts an audio.cpp process, loads a model, or touches
``~/.pebblecompanion``: the engine lifecycle is exercised through stand-in
process objects, and the data root is redirected to a throwaway directory
before ``backend.paths`` is imported.
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# backend.paths calls ensure_dirs() at import time, so this has to run first.
# setdefault keeps whatever run_all.py already chose.
os.environ.setdefault('PEBBLE_DATA_ROOT', tempfile.mkdtemp(prefix='pebble-test-'))

# A pid no real process can hold, used by the stand-ins below. Keeping it far
# above the usual allocation range matters: OpenProcess on a live pid would put
# someone else's process into our job object.
_BOGUS_PID = 0x7FFF0000


class DeadProc:
    """A child that has already exited."""

    def __init__(self, returncode=0):
        self.pid = _BOGUS_PID
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode


class LiveProc:
    """A child that exits as soon as it is terminated."""

    def __init__(self):
        self.pid = _BOGUS_PID
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class StubbornProc:
    """A child that survives both terminate and kill."""

    def __init__(self):
        self.pid = _BOGUS_PID
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired('stubborn', timeout or 5)


def engine_instance(cls):
    """Build an STT engine without running its constructor.

    Both constructors demand the server binary and the weights on disk; the
    lifecycle code under test does not, and a test must never load a model.
    ``_healthy`` and ``_kill_port_owner`` are stubbed out so no test opens a
    socket or runs taskkill; individual tests override them as needed.
    """
    inst = object.__new__(cls)
    inst._lock = threading.RLock()
    inst._proc = None
    inst._ready = False
    inst._healthy = lambda: False
    inst._kill_port_owner = lambda: True
    return inst


def stt_engines():
    """Both engine classes, for tests that must hold for either."""
    from backend.confucius4_stt_engine import Confucius4STT
    from backend.qwen3asr_stt_engine import Qwen3AsrSTT

    return (Qwen3AsrSTT, Confucius4STT)

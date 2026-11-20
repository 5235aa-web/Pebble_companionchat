"""Tests for the backend. Run `python test/run_all.py`.

Importing this package redirects the data root before anything else can read it.
`backend.paths` builds its tree at import time and the STT registry writes into
it, so every entry point - run_all.py, `python -m unittest test.test_x` - has to
pass through here first or a test run would touch the real ~/.pebblecompanion.
"""
import atexit
import os
import shutil
import tempfile

if not os.environ.get('PEBBLE_DATA_ROOT'):
    _DATA_ROOT = tempfile.mkdtemp(prefix='pebble-test-')
    os.environ['PEBBLE_DATA_ROOT'] = _DATA_ROOT
    atexit.register(shutil.rmtree, _DATA_ROOT, ignore_errors=True)

# -*- coding: utf-8 -*-
"""Run every backend test: `python test/run_all.py`.

Standard library only: the project has no test framework in requirements, and a
test run must not need one installed.
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# backend.paths builds its directory tree the moment it is imported, and the STT
# registry migrates a file into it. A throwaway root keeps the run away from the
# real ~/.pebblecompanion.
DATA_ROOT = tempfile.mkdtemp(prefix='pebble-test-')
os.environ['PEBBLE_DATA_ROOT'] = DATA_ROOT


def main() -> int:
    start = Path(__file__).resolve().parent
    suite = unittest.TestLoader().discover(str(start), top_level_dir=str(ROOT))
    count = suite.countTestCases()
    if not count:
        print('no tests found under', start)
        return 1

    print(f'{count} tests, data root {DATA_ROOT}')
    print('-' * 72)
    # Several tests deliberately drive the failure paths, and the backend logs
    # those with tracebacks. Left alone they bury the test output.
    logging.disable(logging.ERROR)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('-' * 72)
    print(
        f'run {result.testsRun}  failures {len(result.failures)}  '
        f'errors {len(result.errors)}  skipped {len(result.skipped)}'
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    code = main()
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    sys.exit(code)

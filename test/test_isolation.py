# -*- coding: utf-8 -*-
"""No test may touch the real ``~/.pebblecompanion``.

A destructive fixture once ran with ``paths.CONFIG_DIR`` pointing at the real
install and deleted the user's LLM and TTS providers. The guards in
``backend.paths`` now make that impossible, and these tests turn the contract
into something that fails on every run, so a future fixture cannot quietly
reintroduce it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend import paths

TEST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_DIR.parent
REAL_ROOT = Path.home() / '.pebblecompanion'


def _probe_source() -> str:
    return (
        'import sys\n'
        f'sys.path.insert(0, r"{PROJECT_ROOT}")\n'
        'from backend import paths\n'
        'print(paths.DATA_ROOT)\n'
    )


class DataRootIsolationTest(unittest.TestCase):
    def test_the_guard_recognises_this_process_as_a_test_run(self):
        """If this ever fails, the redirect below is not running at all."""
        self.assertTrue(paths._looks_like_test_run())

    def test_data_root_is_not_the_real_install(self):
        self.assertNotEqual(
            Path(paths.DATA_ROOT).resolve(), REAL_ROOT.resolve(),
            'tests are running against the real data root',
        )

    def test_data_root_lives_under_the_system_temp_dir(self):
        temp = Path(tempfile.gettempdir()).resolve()
        root = Path(paths.DATA_ROOT).resolve()
        self.assertEqual(
            root.parent, temp,
            f'{root} is not a throwaway directory',
        )

    def test_the_env_override_is_set_for_child_processes(self):
        self.assertTrue(
            os.environ.get('PEBBLE_DATA_ROOT'),
            'child processes would fall back to the real root',
        )

    def test_config_dir_is_inside_the_throwaway_root(self):
        root = Path(paths.DATA_ROOT).resolve()
        config = Path(paths.CONFIG_DIR).resolve()
        self.assertTrue(
            config.is_relative_to(root),
            f'CONFIG_DIR escaped the throwaway root: {config}',
        )


class NoTestHardcodesTheRealRootTest(unittest.TestCase):
    """A literal path would bypass every guard above."""

    def test_no_test_file_mentions_the_real_install(self):
        offenders = []
        for path in sorted(TEST_DIR.glob('test_*.py')):
            if path.name == Path(__file__).name:
                continue  # this file names the real root on purpose
            text = path.read_text(encoding='utf-8', errors='replace')
            for marker in ('.pebblecompanion', 'Path.home()', 'expanduser'):
                if marker in text:
                    offenders.append(f'{path.name}: {marker}')
        self.assertEqual(offenders, [])


class LauncherIsolationTest(unittest.TestCase):
    """The launcher that caused the incident, reproduced in a real child."""

    def _run_probe(self, name: str, env=None):
        probe = TEST_DIR / name
        probe.write_text(_probe_source(), encoding='utf-8')
        self.addCleanup(probe.unlink, missing_ok=True)
        return subprocess.run(
            [sys.executable, str(probe)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=120, env=env,
        )

    def test_a_test_file_started_directly_gets_a_temp_root(self):
        result = self._run_probe('test_zz_direct_run_probe.py')
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        printed = Path(result.stdout.strip().splitlines()[-1]).resolve()
        self.assertNotEqual(printed, REAL_ROOT.resolve())
        self.assertEqual(printed.parent, Path(tempfile.gettempdir()).resolve())

    def test_an_override_pointing_at_the_real_root_is_refused(self):
        env = dict(os.environ)
        env['PEBBLE_DATA_ROOT'] = str(REAL_ROOT)
        result = self._run_probe('test_zz_override_probe.py', env=env)
        self.assertNotEqual(
            result.returncode, 0,
            'the guard let a real data root through to a test run',
        )
        self.assertIn('refusing', result.stderr.lower())


class ProductionEntryIsNotATestRunTest(unittest.TestCase):
    """The guard must not fire for the shipped entry point.

    A false positive would move a user's data into a temporary directory that
    is deleted on exit, which is its own kind of data loss.
    """

    def test_run_backend_is_treated_as_production(self):
        self.assertIn('run_backend.py', paths._PRODUCTION_ENTRIES)

    def test_the_packaged_entry_point_is_treated_as_production(self):
        self.assertIn('pebble-backend.exe', paths._PRODUCTION_ENTRIES)

    def test_a_process_started_from_run_backend_keeps_its_configured_root(self):
        """The guard must not fire for the shipped entry point.

        A false positive would move a user's data into a temporary directory
        that is deleted on exit. The override below keeps this check away from
        the real install while still exercising production detection.
        """
        sandbox = Path(tempfile.mkdtemp(prefix='pebble-prod-'))
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        entry = sandbox / 'run_backend.py'
        entry.write_text(_probe_source(), encoding='utf-8')
        fake_root = sandbox / 'data'
        env = dict(os.environ)
        env['PEBBLE_DATA_ROOT'] = str(fake_root)

        result = subprocess.run(
            [sys.executable, str(entry)],
            cwd=str(sandbox), capture_output=True, text=True,
            timeout=120, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        printed = Path(result.stdout.strip().splitlines()[-1]).resolve()
        self.assertEqual(
            printed, fake_root.resolve(),
            'production detection moved the data root',
        )


if __name__ == '__main__':
    unittest.main()

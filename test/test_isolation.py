# -*- coding: utf-8 -*-
"""No test may touch the real ``~/.pebblecompanion``.

A destructive fixture once ran with ``paths.CONFIG_DIR`` pointing at the real
install and deleted the user's LLM and TTS providers. The guards in
``backend.paths`` now make that impossible, and these tests turn the contract
into something that fails on every run, so a future fixture cannot quietly
reintroduce it.
"""
import ast
import os
import shutil
import sqlite3
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


_HOME_ENV_KEYS = frozenset({'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'})


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                ids.add(id(node.body[0].value))
    return ids


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left), _const_str(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return ''.join(parts) if parts else None
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == 'environ'
        and isinstance(node.value, ast.Name)
        and node.value.id == 'os'
    )


class NoTestHardcodesTheRealRootTest(unittest.TestCase):
    """AST semantic check so literal, split, or env tricks cannot reach the real root."""

    def test_no_test_file_mentions_the_real_install(self):
        offenders: list[str] = []
        for path in sorted(TEST_DIR.glob('*.py')):
            if path.name == Path(__file__).name:
                continue  # this file names the real root on purpose
            source = path.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(source, filename=str(path))
            doc_ids = _docstring_node_ids(tree)

            for node in ast.walk(tree):
                if id(node) in doc_ids:
                    continue
                if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)):
                    text = _const_str(node)
                    if text and 'pebblecompanion' in text.lower():
                        offenders.append(f'{path.name}:{getattr(node, "lineno", "?")}: pebblecompanion literal')
                if isinstance(node, ast.Attribute) and node.attr in ('expanduser', 'home'):
                    if node.attr == 'expanduser' or (
                        isinstance(node.value, ast.Name) and node.value.id == 'Path'
                    ):
                        offenders.append(f'{path.name}:{node.lineno}: .{node.attr}')
                if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                    key = _const_str(node.slice)
                    if key in _HOME_ENV_KEYS:
                        offenders.append(f'{path.name}:{node.lineno}: os.environ[{key!r}]')
                if isinstance(node, ast.Call):
                    func = node.func
                    # os.getenv('HOME') / os.environ.get('HOME')
                    if isinstance(func, ast.Attribute) and node.args:
                        arg0 = _const_str(node.args[0])
                        if arg0 in _HOME_ENV_KEYS and (
                            (func.attr == 'getenv' and isinstance(func.value, ast.Name) and func.value.id == 'os')
                            or (func.attr == 'get' and _is_os_environ(func.value))
                        ):
                            offenders.append(f'{path.name}:{node.lineno}: env lookup {arg0!r}')
                    # importlib.reload(paths)
                    if (
                        (isinstance(func, ast.Attribute) and func.attr == 'reload')
                        or (isinstance(func, ast.Name) and func.id == 'reload')
                    ) and node.args:
                        target_arg = node.args[0]
                        if (isinstance(target_arg, ast.Name) and target_arg.id == 'paths') or (
                            isinstance(target_arg, ast.Attribute) and target_arg.attr == 'paths'
                        ):
                            offenders.append(f'{path.name}:{node.lineno}: reload(paths)')
                    # subprocess.*(..., env={...}) dropping PEBBLE_DATA_ROOT
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == 'subprocess'
                        and func.attr in ('run', 'Popen', 'call', 'check_call', 'check_output')
                    ):
                        for kw in node.keywords:
                            if kw.arg == 'env' and isinstance(kw.value, ast.Dict):
                                has_unpack = any(k is None for k in kw.value.keys)
                                keys = {_const_str(k) for k in kw.value.keys if k is not None}
                                if not has_unpack and 'PEBBLE_DATA_ROOT' not in keys:
                                    offenders.append(
                                        f'{path.name}:{node.lineno}: subprocess env dict without PEBBLE_DATA_ROOT'
                                    )
        self.assertEqual(offenders, [])


class RuntimeWriteGuardTest(unittest.TestCase):
    """PEP 578 audit hook & assert_safe_data_path stop any write reaching REAL_ROOT."""

    def test_audit_hook_is_active_during_tests(self):
        self.assertTrue(paths._AUDIT_HOOK_INSTALLED)

    def test_audit_hook_blocks_file_writes_under_real_root(self):
        probe = REAL_ROOT / 'config' / '__pebble_guard_probe__.json'
        with self.assertRaises(PermissionError):
            open(probe, 'w', encoding='utf-8')
        self.assertFalse(probe.exists())

    def test_audit_hook_blocks_sqlite_connect_to_real_root(self):
        probe_db = REAL_ROOT / '__pebble_guard_probe__.db'
        with self.assertRaises(PermissionError):
            sqlite3.connect(str(probe_db))

    def test_audit_hook_blocks_mkdir_and_unlink_under_real_root(self):
        with self.assertRaises(PermissionError):
            os.mkdir(REAL_ROOT / '__pebble_guard_probe_dir__')
        with self.assertRaises(PermissionError):
            os.unlink(REAL_ROOT / '__pebble_nonexistent_probe__')

    def test_audit_hook_allows_memory_sqlite_and_temp_root_writes(self):
        conn = sqlite3.connect(':memory:')
        conn.close()
        ok_probe = Path(paths.DATA_ROOT) / '__pebble_temp_probe__.txt'
        ok_probe.write_text('ok', encoding='utf-8')
        ok_probe.unlink()

    def test_assert_safe_data_path_rejects_real_root(self):
        with self.assertRaises(RuntimeError):
            paths.assert_safe_data_path(REAL_ROOT / 'config' / 'app.json')

    def test_save_app_config_refuses_real_root_target(self):
        orig = paths.APP_CONFIG_PATH
        try:
            paths.APP_CONFIG_PATH = REAL_ROOT / 'config' / 'app.json'
            with self.assertRaises(RuntimeError):
                paths.save_app_config({'test': True})
        finally:
            paths.APP_CONFIG_PATH = orig


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


    def test_uvicorn_module_entry_is_not_mistaken_for_a_test_run(self):
        self.assertIn('uvicorn', paths._PRODUCTION_MODULES)
        sandbox = Path(tempfile.mkdtemp(prefix='pebble-uvicorn-'))
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        fake_root = sandbox / 'data'
        env = dict(os.environ)
        env['PEBBLE_DATA_ROOT'] = str(fake_root)
        script = (
            'import importlib.machinery, sys, unittest\n'
            f'sys.path.insert(0, r"{PROJECT_ROOT}")\n'
            'sys.modules["__main__"].__spec__ = importlib.machinery.ModuleSpec("uvicorn.__main__", None)\n'
            'from backend import paths\n'
            'print(paths._looks_like_test_run(), paths.DATA_ROOT)\n'
        )
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=str(sandbox), capture_output=True, text=True,
            timeout=120, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        last_line = result.stdout.strip().splitlines()[-1]
        is_test, printed_root = last_line.split(' ', 1)
        self.assertEqual(is_test, 'False')
        self.assertEqual(Path(printed_root).resolve(), fake_root.resolve())


if __name__ == '__main__':
    unittest.main()

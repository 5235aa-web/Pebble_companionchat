# -*- coding: utf-8 -*-
"""The HTTP surface, checked without importing the app.

Importing backend.main builds every engine singleton, which is far too much
machinery for a test that only wants to know whether a route still exists. So the
source is parsed instead: a deleted or renamed decorator shows up here without a
single import side effect.
"""

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / 'backend'
ROUTES = BACKEND / 'api_routes.py'
MAIN = BACKEND / 'main.py'
WS_CALL = BACKEND / 'ws_call.py'

VERBS = {'get', 'post', 'put', 'delete', 'patch'}


def _decorated_routes(path: Path):
    """Every (verb, path) pair declared with @router.<verb>("...") in *path*."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr not in VERBS:
                continue
            if not decorator.args:
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((func.attr, first.value))
    return found


class RouteSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.routes = _decorated_routes(ROUTES)

    def test_routes_are_declared(self):
        self.assertGreater(len(self.routes), 30)

    def test_stop_endpoint_exists(self):
        self.assertIn(('post', '/stt/stop'), self.routes)

    def test_stt_config_endpoints_exist(self):
        self.assertIn(('get', '/stt/config'), self.routes)
        self.assertIn(('post', '/stt/config'), self.routes)

    def test_transcription_endpoint_exists(self):
        self.assertIn(('post', '/stt'), self.routes)

    def test_audio_chat_stream_exists(self):
        self.assertIn(('post', '/chat/audio/stream'), self.routes)

    def test_no_duplicate_route_declarations(self):
        self.assertEqual(len(self.routes), len(set(self.routes)))

    def test_every_path_starts_with_a_slash(self):
        for verb, path in self.routes:
            with self.subTest(route=f'{verb} {path}'):
                self.assertTrue(path.startswith('/'))


class RouterWiringTest(unittest.TestCase):
    def test_both_routers_are_mounted(self):
        source = MAIN.read_text(encoding='utf-8')
        self.assertGreaterEqual(source.count('include_router'), 2)

    def test_static_uploads_are_mounted(self):
        source = MAIN.read_text(encoding='utf-8')
        self.assertIn('StaticFiles', source)

    def test_lifespan_stops_the_stt_engines(self):
        source = MAIN.read_text(encoding='utf-8')
        self.assertIn('_shutdown_stt', source)

    def test_realtime_call_route_is_declared(self):
        self.assertIn('/ws/call', WS_CALL.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

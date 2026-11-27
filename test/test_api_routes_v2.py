# -*- coding: utf-8 -*-
"""Tests for api_routesV2 mounting and story-mode auto-picture trigger."""

import asyncio
import unittest
from unittest import mock

import _helpers  # noqa: F401 - ensures sys.path & temp PEBBLE_DATA_ROOT
from backend import api_routes, api_routesV2


async def _collect_stream(resp) -> list:
    chunks = []
    async for item in resp.body_iterator:
        chunks.append(item)
    return chunks


class ApiRoutesV2MountTest(unittest.TestCase):
    def test_v2_routes_mounted_under_companions(self):
        registered = {
            (tuple(r.methods), r.path)
            for r in api_routes.router.routes
            if hasattr(r, "methods") and hasattr(r, "path")
        }
        for path in (
            "/companions/chat/stream",
            "/companions/chat/continue/stream",
            "/companions/chat/regenerate/stream",
            "/companions/chat/reedit/stream",
            "/companions/world-books",
            "/companions/world-books/{world_book_id}/translate",
            "/companions/{companion_id}/world-books",
            "/companions/{companion_id}/scene-rules",
            "/companions/{companion_id}/translate",
            "/companions/{companion_id}/export/charx",
            "/companions/{companion_id}/assets",
            "/companions/{companion_id}/branches/{branch_id}/hud",
            "/companions/{companion_id}/branches/{branch_id}/variables",
        ):
            with self.subTest(path=path):
                method = "GET" if path in ("/companions/world-books", "/companions/{companion_id}/export/charx") else "PUT" if path.endswith("/scene-rules") else "POST"
                if path in ("/companions/{companion_id}/world-books", "/companions/{companion_id}/assets", "/companions/{companion_id}/branches/{branch_id}/hud", "/companions/{companion_id}/branches/{branch_id}/variables"):
                    method = "GET"
                self.assertIn(((method,), path), registered)

    def test_backwards_compat_reexports(self):
        self.assertIs(api_routes.companion_chat_stream, api_routesV2.companion_chat_stream)
        self.assertIs(api_routes.ChatStreamRequest, api_routesV2.ChatStreamRequest)
        self.assertIs(api_routes.update_scene_rules, api_routesV2.update_scene_rules)
        self.assertIs(api_routes.SceneRulesUpdateRequest, api_routesV2.SceneRulesUpdateRequest)
        self.assertIs(api_routes.put_companion_branch_variables, api_routesV2.put_companion_branch_variables)
        self.assertIs(api_routes.BranchVariablesUpdateRequest, api_routesV2.BranchVariablesUpdateRequest)
        self.assertIs(api_routes.reset_companion_branch_variables, api_routesV2.reset_companion_branch_variables)


class StoryModeAutoPictureTest(unittest.TestCase):
    def _run_chat_stream(self, script_session_id):
        req = api_routesV2.ChatStreamRequest(
            message="hello",
            companion_id="c1",
            branch_id="b1",
            script_session_id=script_session_id,
        )

        async def fake_stream(**_kwargs):
            yield {"type": "content", "content": "hi"}
            yield {"type": "done"}

        with (
            mock.patch.object(api_routesV2, "get_companion", return_value={"name": "Test"}),
            mock.patch.object(api_routesV2, "Companion") as mock_comp,
            mock.patch.object(api_routesV2.picture_service, "maybe_auto") as mock_maybe,
        ):
            mock_comp.return_value.generate_response_stream = fake_stream
            resp = asyncio.run(api_routesV2.companion_chat_stream(req))
            asyncio.run(_collect_stream(resp))
            return mock_maybe

    def test_story_mode_triggers_script_source(self):
        mock_maybe = self._run_chat_stream(script_session_id=42)
        mock_maybe.assert_called_once_with(
            companion_id="c1",
            branch_id="b1",
            source="script",
            session_id=42,
        )

    def test_plain_chat_and_zero_session_trigger_chat_source(self):
        for sid in (None, 0):
            with self.subTest(script_session_id=sid):
                mock_maybe = self._run_chat_stream(script_session_id=sid)
                mock_maybe.assert_called_once_with(
                    companion_id="c1",
                    branch_id="b1",
                    source="chat",
                    session_id=None,
                )


if __name__ == "__main__":
    unittest.main()

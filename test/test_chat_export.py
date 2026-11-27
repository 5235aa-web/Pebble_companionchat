# -*- coding: utf-8 -*-
"""Tests for single-companion branch chat export (md, json, html, jsonl)."""

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path

from fastapi import HTTPException

import _helpers  # noqa: F401 - ensures sys.path & temp PEBBLE_DATA_ROOT
from backend import api_routes, api_routesV2, paths
from backend.chat_exporter import _next_output_path, export_branch_chat
from backend.chat_importer import import_jsonl_branch
from backend.companion_db import (
    add_companion_asset,
    create_branch,
    create_companion,
    save_message,
    set_branch_variables,
)
from backend.script_db import add_scene_summary


class ChatExportTest(unittest.TestCase):
    def setUp(self):
        self.cid = "exp_comp_1"
        raw_card = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Luna",
                "extensions": {
                    "risuai": {
                        "defaultVariables": "hp=100\n",
                        "backgroundHTML": "<style>.status-box{border:1px solid {border};padding:8px;max-width:{limit};}</style>",
                    },
                    "regex_scripts": [
                        {
                            "id": "st_status",
                            "scriptName": "StatusCard",
                            "findRegex": r"/<status>(.*?)<\/status>/gs",
                            "replaceString": '<div class="status-box"><style>.status-box span{color:gold;}</style><span>$1</span></div>',
                            "placement": [2],
                            "disabled": False,
                        },
                        {
                            "id": "st_doc_img",
                            "scriptName": "DocShellImg",
                            "findRegex": r'/<img="([^"]+)">/g',
                            "replaceString": (
                                '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
                                '<meta charset="UTF-8">\n<meta name="asset">\n<title>어셋</title>\n</head>\n'
                                '<table class="asset-table"><tr><td><img src="{{raw::$1}}" alt="$1"></td></tr></table>\n</html>'
                            ),
                            "placement": [2],
                            "disabled": False,
                        },
                        {
                            "id": "st_border",
                            "scriptName": "BorderToken",
                            "findRegex": r"/\{border\}/g",
                            "replaceString": "rgba(143,130,188,0.4)",
                            "placement": [2],
                            "disabled": False,
                        },
                        {
                            "id": "st_limit",
                            "scriptName": "LimitToken",
                            "findRegex": r"/\{limit\}/g",
                            "replaceString": "480px",
                            "placement": [2],
                            "disabled": False,
                        },
                        {
                            "id": "st_heart_voice",
                            "scriptName": "HeartVoice",
                            "findRegex": r"/【心声：([^】]*)】/g",
                            "replaceString": '<span class="hv" style="opacity:0.8">$1</span>',
                            "placement": [2],
                            "disabled": False,
                        },
                    ],
                },
            },
        }
        created = create_companion(
            self.cid,
            {"name": "Luna", "scenario": "Moonlit library"},
            raw_card_data=json.dumps(raw_card, ensure_ascii=False),
        )
        self.assertTrue(created)
        self.branch_id = create_branch(self.cid, "Route: A/B")
        set_branch_variables(self.cid, self.branch_id, {"hp": "88"})

        # Embed a tiny 1x1 PNG asset to verify HTML data-URI inlining.
        asset_rel = "companion_assets/test_exp_badge.png"
        asset_file = paths.DATA_ROOT / "uploads" / asset_rel
        asset_file.parent.mkdir(parents=True, exist_ok=True)
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
            b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        asset_file.write_bytes(tiny_png)
        add_companion_asset(
            self.cid,
            {
                "asset_type": "image",
                "name": "badge.png",
                "ext": "png",
                "mime_type": "image/png",
                "rel_path": asset_rel,
                "size_bytes": len(tiny_png),
            },
        )

        # Insert 1 compacted user message, 1 compaction summary, 1 narration, 1 AI status-bar message.
        self.m1 = save_message(self.cid, self.branch_id, "user", "Hi {{char}}, check status.")
        self.m2 = save_message(
            self.cid,
            self.branch_id,
            "system",
            "[摘要] 之前打过招呼",
            message_type="compaction_summary",
            metadata={"summary": "用户向Luna打了招呼。", "messages_compacted": 1, "compaction_version": 1},
        )
        self.m3 = save_message(
            self.cid,
            self.branch_id,
            "user",
            "图书馆里安静极了。",
            message_type="narration",
        )
        self.m4 = save_message(
            self.cid,
            self.branch_id,
            "assistant",
            '(微笑)\n你好，{{user}}。\n【心声：真期待】\n她低声说：【心声：别走】\n<!-- hidden -->\n<img="badge.png">\n<img src={{raw::badge.png}}>\n<img src="{{raw::missing_asset}}">\n<status>HP: {{getvar::hp}}</status>',
            is_important=True,
        )
        conn = sqlite3.connect(str(paths.DB_PATH))
        try:
            conn.execute(
                "UPDATE companion_messages SET is_compacted = 1, compaction_batch_id = 'b1' WHERE id = ?",
                (self.m1,),
            )
            conn.execute(
                "UPDATE companion_messages SET swipes = ?, swipe_id = 0 WHERE id = ?",
                (
                    json.dumps(
                        [
                            '(微笑)\n你好，{{user}}。\n【心声：真期待】\n她低声说：【心声：别走】\n<!-- hidden -->\n<img="badge.png">\n<img src={{raw::badge.png}}>\n<img src="{{raw::missing_asset}}">\n<status>HP: {{getvar::hp}}</status>',
                            "另一个候选回复",
                        ],
                        ensure_ascii=False,
                    ),
                    self.m4,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        add_scene_summary(
            self.cid,
            self.branch_id,
            session_id=1,
            scene_from=1,
            scene_to=1,
            scene_titles="初遇",
            content="两人在图书馆相遇。",
            message_from_id=self.m1,
            message_to_id=self.m4,
        )

    def tearDown(self):
        conn = sqlite3.connect(str(paths.DB_PATH))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM companions WHERE id = ?", (self.cid,))
            conn.execute(
                "DELETE FROM script_scene_summaries WHERE companion_id = ?",
                (self.cid,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_route_mounted_on_companions(self):
        registered = {
            (tuple(r.methods), r.path)
            for r in api_routes.router.routes
            if hasattr(r, "methods") and hasattr(r, "path")
        }
        self.assertIn((("POST",), "/companions/{companion_id}/branches/{branch_id}/export"), registered)

    def test_markdown_export_strips_status_html_and_toggles_compacted(self):
        res_full = export_branch_chat(self.cid, self.branch_id, "md", include_compacted=True)
        out_path = Path(res_full["file_path"])
        expected_dir = paths.OUTPUT_CHAT_DIR / "Luna"
        self.assertTrue(out_path.is_file())
        self.assertEqual(out_path.parent, expected_dir)
        self.assertEqual(res_full["output_dir"], str(expected_dir))
        text_full = out_path.read_text(encoding="utf-8")
        self.assertIn("Hi Luna, check status.", text_full)
        self.assertIn("已压缩", text_full)
        self.assertIn("HP: 88", text_full)
        self.assertNotIn("<style>", text_full)
        self.assertNotIn("<div class=\"status-box\">", text_full)
        self.assertIn("剧本分段摘要", text_full)

        res_vis = export_branch_chat(self.cid, self.branch_id, "md", include_compacted=False)
        text_vis = Path(res_vis["file_path"]).read_text(encoding="utf-8")
        self.assertNotIn("Hi Luna, check status.", text_vis)
        self.assertIn("用户向Luna打了招呼。", text_vis)

    def test_html_export_restores_status_bar_styles_and_embeds_image(self):
        res = export_branch_chat(self.cid, self.branch_id, "html", include_compacted=True)
        self.assertEqual(Path(res["file_path"]).parent, paths.OUTPUT_CHAT_DIR / "Luna")
        html_text = Path(res["file_path"]).read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn('<div class="status-box">', html_text)
        self.assertIn("HP: 88", html_text)
        self.assertIn("data:image/png;base64,", html_text)
        self.assertNotIn('<img src="<img', html_text)
        self.assertNotIn("&lt;!DOCTYPE html&gt;", html_text)
        self.assertNotIn("&lt;span class=", html_text)
        self.assertIn('<span class="hv" style="opacity:0.8">真期待</span>', html_text)
        self.assertIn('<span class="hv" style="opacity:0.8">别走</span>', html_text)
        self.assertIn("[图片: missing_asset]", html_text)
        self.assertNotIn("<!-- hidden -->", html_text)
        self.assertNotIn('name="asset"', html_text)
        self.assertIn("rgba(143,130,188,0.4)", html_text)
        self.assertIn("480px", html_text)
        self.assertIn("shadowrootmode=\"open\"", html_text)
        self.assertIn("剧本分段摘要", html_text)

    def test_pebble_json_export_is_lossless(self):
        res = export_branch_chat(self.cid, self.branch_id, "json", include_compacted=True)
        self.assertEqual(Path(res["file_path"]).parent, paths.OUTPUT_CHAT_DIR / "Luna")
        data = json.loads(Path(res["file_path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["format"], "pebble_chat_export")
        self.assertEqual(data["branch"]["variables"]["hp"], "88")
        self.assertEqual(len(data["messages"]), 4)
        self.assertTrue(data["messages"][0]["is_compacted"])
        self.assertEqual(len(data["messages"][3]["swipes"]), 2)
        self.assertEqual(len(data["script_scene_summaries"]), 1)

    def test_sillytavern_jsonl_export_structure(self):
        res = export_branch_chat(self.cid, self.branch_id, "jsonl", include_compacted=False)
        self.assertEqual(Path(res["file_path"]).parent, paths.OUTPUT_CHAT_DIR / "Luna")
        raw_lines = Path(res["file_path"]).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(raw_lines), 4)  # 1 header + 3 visible messages
        header = json.loads(raw_lines[0])
        self.assertEqual(header["character_name"], "Luna")
        last_msg = json.loads(raw_lines[-1])
        self.assertFalse(last_msg["is_user"])
        self.assertEqual(len(last_msg["swipes"]), 2)

    def test_endpoint_error_handling(self):
        req = api_routesV2.BranchExportRequest(format="md", include_compacted=True)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(api_routesV2.export_companion_branch("missing_comp", "main", req))
        self.assertEqual(ctx.exception.status_code, 404)

        with self.assertRaises(HTTPException) as ctx2:
            asyncio.run(api_routesV2.export_companion_branch(self.cid, "missing_branch", req))
        self.assertEqual(ctx2.exception.status_code, 404)

    def test_character_folder_sanitization_and_reserved_names(self):
        sanitized = _next_output_path('Alice / "Wonder" .. ', "branch/1", "md")
        self.assertEqual(sanitized.parent.parent, paths.OUTPUT_CHAT_DIR)
        self.assertEqual(sanitized.parent.name, "Alice _ _Wonder_")
        self.assertTrue(sanitized.name.startswith("Alice _ _Wonder__branch_1_"))

        reserved = _next_output_path("CON", "main", "json")
        self.assertEqual(reserved.parent.parent, paths.OUTPUT_CHAT_DIR)
        self.assertEqual(reserved.parent.name, "CON_")

        dots_only = _next_output_path("...", "main", "html")
        self.assertEqual(dots_only.parent, paths.OUTPUT_CHAT_DIR / "companion")

    def test_first_message_greeting_exported_without_duplication(self):
        gid = "exp_comp_greet"
        self.assertTrue(
            create_companion(
                gid,
                {
                    "name": "Iris",
                    "first_mes": "Welcome, {{user}}. *Iris adjusts her glasses.*",
                    "alternate_greetings": ["Alt greeting for {{user}}."],
                },
            )
        )
        try:
            save_message(gid, "main", "user", "Hello Iris!")
            save_message(gid, "main", "assistant", "Glad you came.")

            md_res = export_branch_chat(gid, "main", "md", include_compacted=True)
            self.assertEqual(md_res["message_count"], 3)
            md_text = Path(md_res["file_path"]).read_text(encoding="utf-8")
            self.assertIn("开场白", md_text)
            self.assertLess(
                md_text.index("Welcome, You."),
                md_text.index("Hello Iris!"),
            )

            html_res = export_branch_chat(gid, "main", "html", include_compacted=True)
            html_text = Path(html_res["file_path"]).read_text(encoding="utf-8")
            self.assertIn('<span class="badge">开场白</span>', html_text)
            self.assertLess(
                html_text.index("Welcome, You."),
                html_text.index("Hello Iris!"),
            )

            json_res = export_branch_chat(gid, "main", "json", include_compacted=True)
            json_data = json.loads(Path(json_res["file_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(json_data["messages"]), 3)
            self.assertEqual(json_data["messages"][0]["id"], 0)
            self.assertEqual(
                json_data["messages"][0]["content"],
                "Welcome, {{user}}. *Iris adjusts her glasses.*",
            )
            self.assertEqual(
                json_data["messages"][0]["swipes"],
                [
                    "Welcome, {{user}}. *Iris adjusts her glasses.*",
                    "Alt greeting for {{user}}.",
                ],
            )
            self.assertTrue(json_data["messages"][0]["metadata"].get("is_greeting"))

            jsonl_res = export_branch_chat(gid, "main", "jsonl", include_compacted=True)
            jsonl_text = Path(jsonl_res["file_path"]).read_text(encoding="utf-8")
            jsonl_lines = jsonl_text.strip().splitlines()
            self.assertEqual(len(jsonl_lines), 4)  # 1 header + greeting + 2 messages
            greeting_row = json.loads(jsonl_lines[1])
            self.assertFalse(greeting_row["is_user"])
            self.assertEqual(greeting_row["extra"]["pebble_message_type"], "greeting")

            # Re-importing the exported JSONL and exporting again must not duplicate the greeting.
            imp = import_jsonl_branch(gid, jsonl_text, branch_name="Reimported")
            self.assertEqual(imp["imported_count"], 3)
            re_export = export_branch_chat(gid, imp["branch_id"], "json", include_compacted=True)
            re_data = json.loads(Path(re_export["file_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(re_data["messages"]), 3)

            # Empty branch with no user turns yet still exports the opening greeting.
            empty_branch = create_branch(gid, "Fresh Start")
            empty_res = export_branch_chat(gid, empty_branch, "md", include_compacted=False)
            self.assertEqual(empty_res["message_count"], 1)
            self.assertIn("Welcome, You.", Path(empty_res["file_path"]).read_text(encoding="utf-8"))
        finally:
            conn = sqlite3.connect(str(paths.DB_PATH))
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("DELETE FROM companions WHERE id = ?", (gid,))
                conn.commit()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

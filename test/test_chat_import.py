# -*- coding: utf-8 -*-
"""Tests for SillyTavern / Pebble JSONL single-companion branch import."""

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path

from fastapi import HTTPException

import _helpers  # noqa: F401 - ensures sys.path & temp PEBBLE_DATA_ROOT
from backend import api_routes, api_routesV2, paths
from backend.chat_exporter import export_branch_chat
from backend.chat_importer import import_jsonl_branch, inspect_jsonl_chat
from backend.companion_db import (
    create_branch,
    create_companion,
    get_branch_export_messages,
    get_branch_variables,
    get_branches,
    get_messages,
    save_message,
    save_message_with_swipes,
)


class ChatImportTest(unittest.TestCase):
    def setUp(self):
        self.cid = "imp_comp_1"
        created = create_companion(
            self.cid,
            {"name": "Motoko", "scenario": "Sector 9"},
        )
        self.assertTrue(created)
        self.exported_files = []

    def tearDown(self):
        for p in self.exported_files:
            Path(p).unlink(missing_ok=True)
        conn = sqlite3.connect(str(paths.DB_PATH))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM companions WHERE id = ?", (self.cid,))
            conn.commit()
        finally:
            conn.close()

    def test_sillytavern_jsonl_import_skips_system_and_keeps_swipes_and_vars(self):
        st_lines = [
            json.dumps(
                {
                    "user_name": "Batou",
                    "character_name": "Motoko",
                    "create_date": "2024-11-26 @15h 15m 10s",
                    "chat_metadata": {
                        "variables": {
                            "trust": 85,
                            "location": "Rooftop",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "name": "System",
                    "is_user": False,
                    "is_system": True,
                    "send_date": "2024-11-26 @15h 15m 10s",
                    "mes": "[Author's Note: keep tone cyberpunk]",
                }
            ),
            json.dumps(
                {
                    "name": "Motoko",
                    "is_user": False,
                    "is_system": False,
                    "send_date": "November 26, 2024 3:15pm",
                    "mes": "Dive ready, variant two.",
                    "swipes": ["Dive ready, variant one.", "Dive ready, variant two."],
                    "swipe_id": 1,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "name": "Batou",
                    "is_user": True,
                    "is_system": False,
                    "send_date": "2024-11-26T15:18:05Z",
                    "mes": "Copy that, Major.",
                }
            ),
            json.dumps(
                {
                    "name": "System",
                    "is_user": False,
                    "is_system": False,
                    "send_date": "2024-11-26 @15h 19m 00s",
                    "mes": "Paused timer",
                    "extra": {"isSmallSys": True},
                }
            ),
        ]
        # Include UTF-8 BOM and blank lines to verify tolerant parsing.
        payload = "\ufeff" + "\n\n".join(st_lines) + "\n"

        preview = inspect_jsonl_chat(self.cid, payload)
        self.assertFalse(preview["character_mismatch"])
        self.assertEqual(preview["message_count"], 2)
        self.assertEqual(preview["skipped_system_count"], 2)
        self.assertEqual(preview["variables_count"], 2)

        res = import_jsonl_branch(
            self.cid,
            payload,
            filename="Motoko - 2024-11-26.jsonl",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["branch_name"], "Motoko - 2024-11-26")
        self.assertEqual(res["imported_count"], 2)
        self.assertEqual(res["skipped_system_count"], 2)
        self.assertEqual(res["swipe_total_count"], 3)
        self.assertEqual(res["variables_imported"], 2)

        branches = get_branches(self.cid)
        bid = res["branch_id"]
        self.assertTrue(any(b["branch_id"] == bid and b["branch_name"] == "Motoko - 2024-11-26" for b in branches))

        msgs = get_messages(self.cid, bid, include_swipes=True)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertEqual(msgs[0]["content"], "Dive ready, variant two.")
        self.assertEqual(msgs[0]["swipe_id"], 1)
        self.assertEqual(msgs[0]["swipes"], ["Dive ready, variant one.", "Dive ready, variant two."])
        self.assertEqual(msgs[0]["created_at"], "2024-11-26 15:15:00")

        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "Copy that, Major.")

        vars_info = get_branch_variables(self.cid, bid)
        self.assertEqual(vars_info["variables"].get("trust"), "85")
        self.assertEqual(vars_info["variables"].get("location"), "Rooftop")

    def test_headerless_jsonl_and_swipe_bounds_recovery(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "name": "Motoko",
                        "is_user": False,
                        "is_system": False,
                        "mes": "Edited line wins.",
                        "swipes": ["Original"],
                        "swipe_id": 99,
                    }
                ),
                json.dumps(
                    {
                        "name": "User",
                        "is_user": True,
                        "mes": "Understood.",
                    }
                ),
            ]
        )

        res = import_jsonl_branch(self.cid, raw, branch_name="Headerless Run")
        self.assertEqual(res["imported_count"], 2)
        self.assertFalse(res["character_mismatch"])

        msgs = get_messages(self.cid, res["branch_id"], include_swipes=True)
        self.assertEqual(msgs[0]["swipe_id"], 0)
        self.assertEqual(msgs[0]["swipes"], ["Edited line wins."])
        self.assertEqual(msgs[0]["content"], "Edited line wins.")
        self.assertEqual(msgs[1]["role"], "user")

    def test_pebble_jsonl_roundtrip_preserves_narration_and_summary(self):
        src_branch = create_branch(self.cid, "Source Route")
        save_message(self.cid, src_branch, "user", "Rain fell on the harbor.", message_type="narration", is_important=True)
        save_message_with_swipes(
            self.cid,
            src_branch,
            "assistant",
            "Choice B",
            swipes=["Choice A", "Choice B"],
            swipe_id=1,
        )
        save_message(
            self.cid,
            src_branch,
            "system",
            "[COMPACTION SUMMARY]\nThey inspected the docks.",
            message_type="compaction_summary",
            metadata={"summary": "They inspected the docks.", "messages_compacted": 4},
        )

        exported = export_branch_chat(self.cid, src_branch, fmt="jsonl", include_compacted=True)
        self.exported_files.append(exported["file_path"])
        jsonl_text = Path(exported["file_path"]).read_text(encoding="utf-8")

        imported = import_jsonl_branch(self.cid, jsonl_text, branch_name="Roundtrip Branch")
        self.assertEqual(imported["imported_count"], 3)
        self.assertEqual(imported["skipped_system_count"], 0)

        restored = get_branch_export_messages(self.cid, imported["branch_id"], include_compacted=True)
        self.assertEqual(len(restored), 3)
        self.assertEqual(restored[0]["message_type"], "narration")
        self.assertTrue(restored[0]["is_important"])
        self.assertEqual(restored[1]["swipes"], ["Choice A", "Choice B"])
        self.assertEqual(restored[1]["swipe_id"], 1)
        self.assertEqual(restored[2]["message_type"], "compaction_summary")
        self.assertEqual(restored[2]["metadata"]["summary"], "They inspected the docks.")

    def test_routes_mismatch_preview_and_validation_errors(self):
        route_paths = {getattr(r, "path", "") for r in api_routes.router.routes}
        self.assertIn("/companions/{companion_id}/branches/import-jsonl/preview", route_paths)
        self.assertIn("/companions/{companion_id}/branches/import-jsonl", route_paths)

        other_jsonl = "\n".join(
            [
                json.dumps({"character_name": "Kusanagi Alt", "user_name": "You"}),
                json.dumps({"is_user": False, "mes": "Hello from another card."}),
            ]
        )
        preview = asyncio.run(
            api_routesV2.preview_companion_jsonl_import(
                self.cid,
                api_routesV2.BranchImportJsonlRequest(jsonl_text=other_jsonl),
            )
        )
        self.assertTrue(preview["character_mismatch"])
        self.assertEqual(preview["source_character_name"], "Kusanagi Alt")

        imported = asyncio.run(
            api_routesV2.import_companion_jsonl_branch(
                self.cid,
                api_routesV2.BranchImportJsonlRequest(
                    jsonl_text=other_jsonl,
                    filename="alt_chat.jsonl",
                ),
            )
        )
        self.assertEqual(imported["branch_name"], "alt_chat")
        self.assertEqual(imported["imported_count"], 1)

        with self.assertRaises(HTTPException) as bad_json:
            asyncio.run(
                api_routesV2.import_companion_jsonl_branch(
                    self.cid,
                    api_routesV2.BranchImportJsonlRequest(jsonl_text='{"mes":"ok"}\n{bad'),
                )
            )
        self.assertEqual(bad_json.exception.status_code, 400)
        self.assertIn("line 2", bad_json.exception.detail)

        with self.assertRaises(HTTPException) as only_sys:
            asyncio.run(
                api_routesV2.import_companion_jsonl_branch(
                    self.cid,
                    api_routesV2.BranchImportJsonlRequest(
                        jsonl_text=json.dumps({"is_system": True, "mes": "hidden"})
                    ),
                )
            )
        self.assertEqual(only_sys.exception.status_code, 400)

        with self.assertRaises(HTTPException) as missing_comp:
            asyncio.run(
                api_routesV2.preview_companion_jsonl_import(
                    "missing_companion",
                    api_routesV2.BranchImportJsonlRequest(jsonl_text=other_jsonl),
                )
            )
        self.assertEqual(missing_comp.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

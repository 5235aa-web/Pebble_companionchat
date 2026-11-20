# -*- coding: utf-8 -*-
"""Single-chat Stage HUD protocol, stream filter, and branch persistence tests."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import card_hud, companion, companion_db, world_book_db


class StreamFilterTest(unittest.TestCase):
    def test_strips_split_state_block_and_extracts_cues(self):
        flt = card_hud.CardHudStreamFilter()
        chunks = [
            "She smiles softly.",
            " <<<STA",
            "TE>>>\nmood: happy\naffection: 72\nlocation: balcony\n",
            "outfit: evening coat\nexpression: smile\nbgm: rain_theme\nsfx: chime\n<<<END>>>",
        ]
        visible = "".join(flt.feed(c) for c in chunks)
        tail, hud_update, media_cue = flt.finish()
        visible += tail

        self.assertEqual(visible.strip(), "She smiles softly.")
        self.assertEqual(hud_update["mood"], "happy")
        self.assertEqual(hud_update["affection"], 72)
        self.assertEqual(hud_update["location"], "balcony")
        self.assertEqual(hud_update["outfit"], "evening coat")
        self.assertEqual(hud_update["expression"], "smile")
        self.assertEqual(hud_update["bgm"], "rain_theme")
        # One-shot sfx must appear in media_cue, not persist in hud_update.
        self.assertNotIn("sfx", hud_update)
        self.assertEqual(media_cue["sfx"], "chime")
        self.assertEqual(media_cue["bgm"], "rain_theme")
        self.assertEqual(media_cue["expression"], "smile")

    def test_plain_reply_passes_through_unchanged(self):
        flt = card_hud.CardHudStreamFilter()
        visible = flt.feed("Hello there!")
        tail, hud_update, media_cue = flt.finish()
        self.assertEqual(visible + tail, "Hello there!")
        self.assertEqual(hud_update, {})
        self.assertEqual(media_cue, {})


class PromptBudgetTest(unittest.TestCase):
    def test_massive_asset_list_is_capped_for_prompt(self):
        assets = [
            {"asset_type": "emotion", "name": f"expr_{i}", "mime_type": "image/png"}
            for i in range(2500)
        ] + [
            {"asset_type": "bgm", "name": f"track_{i}", "mime_type": "audio/mpeg"}
            for i in range(50)
        ]
        prompt = card_hud.build_card_hud_prompt({"mood": "calm"}, assets)
        self.assertIn("[Stage HUD]", prompt)
        self.assertIn("expr_0", prompt)
        self.assertNotIn("expr_500", prompt)
        self.assertIn("track_0", prompt)
        self.assertNotIn("track_49", prompt)


class BranchHudDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-hud-"))
        self._orig_db = companion_db.DB_PATH
        self._orig_wb = world_book_db.DB_PATH
        companion_db.DB_PATH = self.tmp / "companions.db"
        world_book_db.DB_PATH = companion_db.DB_PATH

    def tearDown(self):
        companion_db.DB_PATH = self._orig_db
        world_book_db.DB_PATH = self._orig_wb
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_auto_enablement_follows_assets_and_explicit_toggle(self):
        companion_db.create_companion("c_plain", {"name": "Plain"})
        st_plain = companion_db.get_branch_hud("c_plain", "main")
        self.assertFalse(st_plain["effective_enabled"])
        self.assertIsNone(st_plain["enabled"])

        # Turning it on explicitly enables it even without assets.
        st_on = companion_db.update_branch_hud("c_plain", "main", enabled=True)
        self.assertTrue(st_on["effective_enabled"])
        self.assertTrue(st_on["enabled"])

        # A companion with imported assets enables HUD automatically.
        companion_db.create_companion("c_rich", {"name": "Rich"})
        companion_db.add_companion_asset("c_rich", {
            "asset_type": "emotion",
            "name": "happy",
            "ext": "png",
            "mime_type": "image/png",
            "rel_path": "uploads/companion_assets/c_rich/happy.png",
        })
        st_rich = companion_db.get_branch_hud("c_rich", "main")
        self.assertTrue(st_rich["effective_enabled"])
        self.assertTrue(st_rich["has_assets"])

        # Plain prompt without include_card_hud stays untouched.
        plain_prompt = companion.Companion("c_plain").build_system_prompt()
        self.assertNotIn("[Stage HUD]", plain_prompt)
        hud_prompt = companion.Companion("c_rich").build_system_prompt(include_card_hud=True)
        self.assertIn("[Stage HUD]", hud_prompt)
        self.assertIn("happy", hud_prompt)


if __name__ == "__main__":
    unittest.main()

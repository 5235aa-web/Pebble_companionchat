# -*- coding: utf-8 -*-
"""Single-chat Stage HUD protocol, stream filter, and branch persistence tests."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_tolerates_case_spaces_split_end_and_code_fences(self):
        flt = card_hud.CardHudStreamFilter()
        payload = (
            "Normal prose.\n"
            "```python\nprint('keep me')\n```\n"
            "Ending line.\n"
            "```yaml\n"
            "<<< sTaTe >>>\n"
            "mood: playful\n"
            "affection: 88\n"
            "<<< /STATE >>>\n"
            "```"
        )
        # Feed 2 chars at a time to stress every marker/fence boundary.
        visible = "".join(flt.feed(payload[i : i + 2]) for i in range(0, len(payload), 2))
        tail, hud_update, _ = flt.finish()
        visible += tail

        self.assertEqual(
            visible.strip(),
            "Normal prose.\n```python\nprint('keep me')\n```\nEnding line.",
        )
        self.assertEqual(hud_update["mood"], "playful")
        self.assertEqual(hud_update["affection"], 88)


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

    def test_continue_stream_filters_state_and_emits_card_hud(self):
        companion_db.create_companion("c_cont", {"name": "Iris"})
        companion_db.update_branch_hud("c_cont", "main", enabled=True, hud={"mood": "calm"})
        companion_db.save_message_with_swipes("c_cont", "main", "user", "Go on.", [], 0)
        msg_id = companion_db.save_message_with_swipes(
            "c_cont", "main", "assistant", "She paused near the door.", ["She paused near the door."], 0
        )

        captured_messages = []

        async def fake_stream(**kwargs):
            captured_messages.extend(kwargs["messages"])
            for part in [
                {"type": "content", "content": " Then she smiled."},
                {"type": "content", "content": "\n<<<STA"},
                {"type": "content", "content": "TE>>>\nmood: warm\naffection: 65\n<<<END>>>"},
            ]:
                yield part

        async def run():
            events = []
            with mock.patch.object(companion, "call_llm_stream", side_effect=fake_stream):
                comp = companion.Companion("c_cont")
                async for ev in comp.generate_continue_stream(
                    branch_id="main",
                    llm_config={
                        "provider_type": "openai",
                        "api_key": "k",
                        "base_url": "http://localhost:1234/v1",
                        "model": "test-model",
                    },
                ):
                    events.append(ev)
            return events

        events = asyncio.run(run())
        content_text = "".join(e["content"] for e in events if e["type"] == "content")
        self.assertEqual(content_text.strip(), "Then she smiled.")

        hud_events = [e for e in events if e["type"] == "card_hud"]
        self.assertEqual(len(hud_events), 1)
        self.assertEqual(hud_events[0]["hud"]["mood"], "warm")
        self.assertEqual(hud_events[0]["hud"]["affection"], 65)

        done_events = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0]["message_id"], msg_id)
        self.assertNotIn("<<<STATE>>>", done_events[0]["final_content"])

        # System prompt includes Stage HUD instructions when enabled on the branch.
        self.assertIn("[Stage HUD]", captured_messages[0]["content"])

        # Stored message appends only the visible continuation prose.
        msgs = companion_db.get_messages("c_cont", "main")
        self.assertEqual(msgs[-1]["content"].strip(), "She paused near the door. Then she smiled.")
        self.assertNotIn("<<<STATE>>>", msgs[-1]["content"])

        # Branch HUD persisted the update.
        branch_hud = companion_db.get_branch_hud("c_cont", "main")
        self.assertEqual(branch_hud["hud"]["mood"], "warm")
        self.assertEqual(branch_hud["hud"]["affection"], 65)

    def test_dual_visual_roles_and_pinned_cbs_var_bidirectional_sync(self):
        companion_db.create_companion("c_dual", {"name": "Iris"})
        companion_db.update_companion(
            "c_dual",
            {"card_ui": {"hud_pinned_vars": ["stamina", "trust_stage"]}},
        )
        companion_db.add_companion_asset("c_dual", {
            "asset_type": "emotion",
            "name": "gentle_smile",
            "ext": "png",
            "mime_type": "image/png",
            "rel_path": "uploads/companion_assets/c_dual/gentle_smile.png",
        })
        companion_db.add_companion_asset("c_dual", {
            "asset_type": "background",
            "name": "rainy_cafe",
            "ext": "png",
            "mime_type": "image/png",
            "rel_path": "uploads/companion_assets/c_dual/rainy_cafe.png",
        })
        companion_db.update_branch_hud("c_dual", "main", enabled=True, hud={"mood": "calm"})
        companion_db.set_branch_variables("c_dual", "main", {"stamina": "80", "trust_stage": "1"})

        comp = companion.Companion("c_dual")
        sys_prompt = comp.build_system_prompt(include_card_hud=True)
        self.assertIn("Available expression portraits: gentle_smile", sys_prompt)
        self.assertNotIn("Available background scenes", sys_prompt)
        self.assertNotIn("rainy_cafe", sys_prompt)
        self.assertNotIn("scene:", sys_prompt)
        self.assertIn("Tracked story variables (CBS):", sys_prompt)
        self.assertIn("stamina: 80", sys_prompt)
        self.assertIn("var.<name>:", sys_prompt)
        self.assertIn("Keep updating any custom state fields", sys_prompt)

        flt = card_hud.CardHudStreamFilter()
        flt.feed("It is pouring outside.<<<STATE>>>\nexpression: gentle_smile\nscene: rainy_cafe\nvar.stamina: 75\ntrust_stage: 2\n<<<END>>>")
        tail, ev = comp._finalize_card_hud("main", flt, {"mood": "calm"})
        self.assertEqual(tail, "")
        self.assertEqual(ev["media_cue"]["expression"], "gentle_smile")
        self.assertNotIn("scene", ev["media_cue"])
        self.assertNotIn("scene", ev["hud"])
        self.assertEqual(ev["variables"]["stamina"], "75")
        self.assertEqual(ev["variables"]["trust_stage"], "2")
        # Pinned CBS variables stay stored in branch variables, not duplicated in card_hud.
        self.assertNotIn("var.stamina", ev["hud"])
        self.assertNotIn("trust_stage", ev["hud"])
        persisted_vars = companion_db.get_branch_variables("c_dual", "main")["variables"]
        self.assertEqual(persisted_vars["stamina"], "75")
        self.assertEqual(persisted_vars["trust_stage"], "2")

    def test_selective_thought_prompt_and_transient_clearing(self):
        companion_db.create_companion("c_thought", {"name": "Sora"})
        companion_db.update_branch_hud("c_thought", "main", enabled=True, hud={"mood": "tender"})

        # Default disabled: no thought field instruction in prompt.
        prompt_off = companion.Companion("c_thought").build_system_prompt(include_card_hud=True)
        self.assertNotIn("thought:", prompt_off)

        # Enabled via card_ui: prompt guides rare/selective usage strictly inside <<<STATE>>>.
        companion_db.update_companion("c_thought", {"card_ui": {"hud_thought_enabled": True}})
        comp = companion.Companion("c_thought")
        prompt_on = comp.build_system_prompt(include_card_hud=True)
        self.assertIn("inside <<<STATE>>> sparingly", prompt_on)
        self.assertIn("never on every reply", prompt_on)

        # End-to-end stream_reply check: thought is stripped from visible reply & DB message, delivered only in card_hud.
        async def fake_stream(**kwargs):
            for part in [
                {"type": "content", "content": "She looked away softly."},
                {"type": "content", "content": "\n<<<STATE>>>\nmood: shy\nthought: 真希望时间停在这一刻。\n<<<END>>>"},
            ]:
                yield part

        async def run_stream():
            evs = []
            with mock.patch.object(companion, "call_llm_stream", side_effect=fake_stream):
                async for ev in comp.generate_response_stream(
                    "Sit closer.",
                    branch_id="main",
                    llm_config={
                        "provider_type": "openai",
                        "api_key": "k",
                        "base_url": "http://localhost:1234/v1",
                        "model": "test-model",
                    },
                ):
                    evs.append(ev)
            return evs

        stream_evs = asyncio.run(run_stream())
        visible_text = "".join(e["content"] for e in stream_evs if e["type"] == "content").strip()
        self.assertEqual(visible_text, "She looked away softly.")
        self.assertNotIn("thought", visible_text)
        self.assertNotIn("真希望时间停在这一刻", visible_text)
        saved_msgs = companion_db.get_messages("c_thought", "main")
        self.assertEqual(saved_msgs[-1]["content"].strip(), "She looked away softly.")
        hud_evs = [e for e in stream_evs if e["type"] == "card_hud"]
        self.assertEqual(len(hud_evs), 1)
        self.assertEqual(hud_evs[0]["hud"]["thought"], "真希望时间停在这一刻。")

        # Turn 1 (intimate moment): thought appears cleanly.
        f1 = card_hud.CardHudStreamFilter()
        f1.feed("<<<STATE>>>\nmood: shy\nthought: 刚才那一秒心跳好像漏了一拍。\n<<<END>>>")
        _, ev1 = comp._finalize_card_hud("main", f1, {"mood": "tender"})
        self.assertEqual(ev1["hud"]["thought"], "刚才那一秒心跳好像漏了一拍。")

        # Turn 2 (ordinary casual reply omitting thought): stale thought is cleared from HUD & DB.
        f2 = card_hud.CardHudStreamFilter()
        f2.feed("<<<STATE>>>\nmood: calm\n<<<END>>>")
        _, ev2 = comp._finalize_card_hud("main", f2, ev1["hud"])
        self.assertNotIn("thought", ev2["hud"])
        self.assertNotIn("thought", companion_db.get_branch_hud("c_thought", "main")["hud"])

    def test_affection_normalization_heals_annotated_deltas_and_legacy_db_rows(self):
        hud_upd, _ = card_hud.parse_hud_block("affection: 17 (growing)\nmood: shy")
        self.assertEqual(hud_upd["affection"], 17)

        hud_frac, _ = card_hud.parse_hud_block("affection: 65/100")
        self.assertEqual(hud_frac["affection"], 65)

        hud_delta, _ = card_hud.parse_hud_block('affection: +15 from the moment of being caught')
        self.assertEqual(hud_delta["affection"], "+15")

        merged = card_hud.merge_card_hud({"affection": "17 (growing)"}, hud_delta)
        self.assertEqual(merged["affection"], 32)

        merged_drop = card_hud.merge_card_hud({"affection": 12}, {"affection": "-20"})
        self.assertEqual(merged_drop["affection"], 0)

        # Legacy dirty string stored directly in SQLite heals on read via get_branch_hud.
        companion_db.create_companion("c_legacy_aff", {"name": "Evelyn"})
        companion_db.update_branch_hud("c_legacy_aff", "main", hud={"affection": "17 (growing)", "mood": "warm"})
        state = companion_db.get_branch_hud("c_legacy_aff", "main")
        self.assertEqual(state["hud"]["affection"], 17)

    def test_multi_char_asset_filtering_and_inline_media_decoupling(self):
        companion_db.create_companion("c_inline_card", {"name": "Evelyn"})
        companion_db.update_companion(
            "c_inline_card",
            {
                "card_ui": {
                    "display_regex": [
                        {
                            "id": "pimg",
                            "enabled": True,
                            "pattern": '<pimg="(.+?)">',
                            "replace": '<img src="{{raw::$1}}">',
                        }
                    ]
                }
            },
        )
        companion_db.add_companion_asset("c_inline_card", {
            "asset_type": "icon",
            "name": "iconx",
            "ext": "png",
            "mime_type": "image/png",
            "rel_path": "uploads/companion_assets/c_inline_card/iconx.png",
        })
        for i in range(10):
            companion_db.add_companion_asset("c_inline_card", {
                "asset_type": "image",
                "name": f"scene {i + 1}",
                "ext": "png",
                "mime_type": "image/png",
                "rel_path": f"uploads/companion_assets/c_inline_card/scene_{i + 1}.png",
            })
        for i in range(45):
            companion_db.add_companion_asset("c_inline_card", {
                "asset_type": "image",
                "name": f"lucian_pose_{i}",
                "ext": "webp",
                "mime_type": "image/webp",
                "rel_path": f"uploads/companion_assets/c_inline_card/lucian_{i}.webp",
            })
        companion_db.add_companion_asset("c_inline_card", {
            "asset_type": "image",
            "name": "Evelyn_shy",
            "ext": "webp",
            "mime_type": "image/webp",
            "rel_path": "uploads/companion_assets/c_inline_card/evelyn_shy.webp",
        })

        # Ranking prioritizes main character assets and skips placeholder iconx / generic scene N.
        assets = companion_db.get_companion_assets("c_inline_card")
        visual, _ = card_hud._summarize_assets_for_prompt(assets, char_name="Evelyn")
        self.assertIn("Evelyn_shy", visual)
        self.assertNotIn("iconx", visual)
        self.assertNotIn("scene 1", visual)

        # Inline media card without dedicated emotion/portrait assets runs lightweight HUD telemetry:
        # keeps affection/mood/location/outfit without fighting <pimg> via expression lists.
        prompt = companion.Companion("c_inline_card").build_system_prompt(include_card_hud=True)
        self.assertIn("[Stage HUD]", prompt)
        self.assertIn("affection: <integer ONLY 0-100", prompt)
        self.assertNotIn("Available expression images", prompt)
        self.assertNotIn("expression:", prompt)
        self.assertNotIn("lucian_pose_0", prompt)


if __name__ == "__main__":
    unittest.main()

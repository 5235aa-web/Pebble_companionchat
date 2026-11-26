# -*- coding: utf-8 -*-
"""Tests for isolated character card translation across single and group chat."""

import asyncio
import json
import unittest
from unittest.mock import patch

import test._helpers  # noqa: F401 - redirects PEBBLE_DATA_ROOT before backend imports

from backend import card_translation, companion, companion_db
from backend.group_sessions import db as group_db
from backend.group_sessions import prompt as group_prompt
from backend.group_sessions import turn as group_turn


class CardTranslationTest(unittest.TestCase):
    def setUp(self):
        self.cid = f"tr-test-{id(self)}"
        self.other_cid = f"tr-other-{id(self)}"
        companion_db.delete_companion(self.cid)
        companion_db.delete_companion(self.other_cid)
        ok = companion_db.create_companion(
            self.cid,
            {
                "name": "Evelyn",
                "description": "A lighthouse keeper on a misty coast.",
                "personality": "Calm and observant.",
                "scenario": "Rain taps against the lantern glass.",
                "first_mes": "Welcome to the tower, {{user}}.",
                "alternate_greetings": ["Tea is still warm.", ""],
                "tags": ["mystery", "coastal"],
                "creator_notes": "Best played slowly.",
                "system_prompt": "Stay concise in character.",
                "post_history_instructions": "Never speak for {{user}}.",
                "advanced_prompt": "Atmospheric prose.",
                "user_info": "Traveler",
                "mes_example": "<START>\n{{user}}: Hi\n{{char}}: Hello.",
            },
        )
        self.assertTrue(ok)

    def tearDown(self):
        companion_db.delete_companion(self.cid)
        companion_db.delete_companion(self.other_cid)

    def test_translate_isolates_original_and_keeps_name_and_system_fields(self):
        seen_prompts = []

        async def fake_llm_stream(**kwargs):
            msgs = kwargs.get("messages") or []
            user_msg = msgs[-1]["content"] if msgs else ""
            seen_prompts.append(user_msg)
            if user_msg.startswith("["):
                yield {"type": "content", "content": json.dumps(["悬疑", "海岸"], ensure_ascii=False)}
            elif "description" in user_msg:
                yield {"type": "content", "content": "一位驻守雾海灯塔的守灯人。"}
            elif "personality" in user_msg:
                yield {"type": "content", "content": "沉稳而敏锐。"}
            elif "scenario" in user_msg:
                yield {"type": "content", "content": "细雨轻敲着灯笼玻璃。"}
            elif "first_mes" in user_msg:
                yield {"type": "content", "content": "欢迎来到灯塔，{{user}}。"}
            elif "creator_notes" in user_msg:
                yield {"type": "content", "content": "适合慢节奏体验。"}
            elif "alternate_greetings" in user_msg:
                yield {"type": "content", "content": "茶还是温的。"}
            else:
                yield {"type": "content", "content": "译文"}

        async def run():
            events = []
            with patch.object(card_translation, "call_llm_stream", side_effect=fake_llm_stream):
                async for ev in card_translation.iter_translate_companion_card(
                    self.cid,
                    target_locale="zh",
                    activate=True,
                    llm_config={"provider_type": "openai", "model": "mock"},
                ):
                    events.append(ev)
            return events

        events = asyncio.run(run())
        self.assertEqual(events[-1]["type"], "done")

        stored = companion_db.get_companion(self.cid)
        self.assertIsNotNone(stored)
        # Original columns must remain untouched.
        self.assertEqual(stored["name"], "Evelyn")
        self.assertEqual(stored["description"], "A lighthouse keeper on a misty coast.")
        self.assertEqual(stored["personality"], "Calm and observant.")
        self.assertEqual(stored["scenario"], "Rain taps against the lantern glass.")
        self.assertEqual(stored["first_mes"], "Welcome to the tower, {{user}}.")
        self.assertEqual(stored["tags"], ["mystery", "coastal"])
        self.assertEqual(stored["creator_notes"], "Best played slowly.")
        self.assertEqual(stored["system_prompt"], "Stay concise in character.")
        self.assertEqual(stored["post_history_instructions"], "Never speak for {{user}}.")
        self.assertEqual(stored["advanced_prompt"], "Atmospheric prose.")
        self.assertEqual(stored["mes_example"], "<START>\n{{user}}: Hi\n{{char}}: Hello.")

        # Only display fields should be translated; system/name fields excluded.
        self.assertEqual(stored["active_locale"], "zh")
        self.assertFalse(stored["prompt_use_original"])
        zh_bucket = stored["translations"]["zh"]
        self.assertNotIn("name", zh_bucket)
        self.assertNotIn("system_prompt", zh_bucket)
        self.assertNotIn("post_history_instructions", zh_bucket)
        self.assertNotIn("advanced_prompt", zh_bucket)
        self.assertNotIn("mes_example", zh_bucket)
        self.assertEqual(zh_bucket["description"], "一位驻守雾海灯塔的守灯人。")
        self.assertEqual(zh_bucket["first_mes"], "欢迎来到灯塔，{{user}}。")
        self.assertEqual(zh_bucket["tags"], ["悬疑", "海岸"])

    def test_single_and_group_prompts_respect_locale_and_prompt_use_original(self):
        companion_db.update_companion(
            self.cid,
            {
                "translations": {
                    "zh": {
                        "description": "驻守雾海灯塔的守灯人。",
                        "personality": "沉稳而敏锐。",
                        "scenario": "细雨轻敲着灯笼玻璃。",
                        "first_mes": "欢迎来到灯塔。",
                    }
                },
                "active_locale": "zh",
                "prompt_use_original": False,
            },
        )
        companion_db.create_companion(
            self.other_cid,
            {
                "name": "Iris",
                "description": "A botanist cataloging coastal moss.",
                "personality": "Curious and brisk.",
                "scenario": "Sorting specimens by the dock.",
            },
        )

        # Single chat prompt uses zh display fields while keeping original name & system prompt.
        comp = companion.Companion(self.cid)
        sys_prompt = comp.build_system_prompt([])
        self.assertIn("You are Evelyn.", sys_prompt)
        self.assertIn("驻守雾海灯塔的守灯人。", sys_prompt)
        self.assertIn("沉稳而敏锐。", sys_prompt)
        self.assertIn("Stay concise in character.", sys_prompt)
        self.assertNotIn("A lighthouse keeper on a misty coast.", sys_prompt)

        # Flipping prompt_use_original=True keeps prompt in original English.
        companion_db.update_companion(self.cid, {"prompt_use_original": True})
        comp_orig_prompt = companion.Companion(self.cid)
        sys_orig = comp_orig_prompt.build_system_prompt([])
        self.assertIn("A lighthouse keeper on a misty coast.", sys_orig)
        self.assertNotIn("驻守雾海灯塔的守灯人。", sys_orig)

        # Group chat prompt & lexical router respect active_locale and prompt_use_original.
        companion_db.update_companion(self.cid, {"prompt_use_original": False})
        session = group_db.create_session(name="Harbor", scene="", user_note="")
        try:
            group_db.add_member(session["id"], self.cid)
            group_db.add_member(session["id"], self.other_cid)
            members = group_db.list_members(session["id"])
            speaker = next(m for m in members if m["companion_id"] == self.cid)

            g_prompt = group_prompt.build_system_prompt(session, members, speaker)
            self.assertIn("You are Evelyn.", g_prompt)
            self.assertIn("驻守雾海灯塔的守灯人。", g_prompt)
            self.assertNotIn("A lighthouse keeper on a misty coast.", g_prompt)

            picked = group_turn._pick_by_persona_relevance(members, "灯塔 雾海 守灯人")
            self.assertIsNotNone(picked)
            self.assertEqual(picked["companion_id"], self.cid)

            # With prompt_use_original=True, group prompt switches back to original text.
            companion_db.update_companion(self.cid, {"prompt_use_original": True})
            members_orig = group_db.list_members(session["id"])
            speaker_orig = next(m for m in members_orig if m["companion_id"] == self.cid)
            g_prompt_orig = group_prompt.build_system_prompt(session, members_orig, speaker_orig)
            self.assertIn("A lighthouse keeper on a misty coast.", g_prompt_orig)
            self.assertNotIn("驻守雾海灯塔的守灯人。", g_prompt_orig)
        finally:
            group_db.delete_session(session["id"])


if __name__ == "__main__":
    unittest.main()

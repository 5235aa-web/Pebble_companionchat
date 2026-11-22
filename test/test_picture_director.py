# -*- coding: utf-8 -*-
"""Tests for Picture Studio director prompt limits and transcript preconditions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.picturesys import config as picture_config
from backend.picturesys import prompt as picture_prompt
from backend.picturesys import service as picture_service


class DirectorPromptTests(unittest.TestCase):
    def test_personality_and_message_clip_limits(self):
        companion = {
            "name": "Aria",
            "description": "D" * 1000,
            "personality": "P" * 1150,
            "scenario": "S" * 1000,
        }
        transcript = [
            {"role": "User", "text": "U" * 900},
            {"role": "Assistant", "text": "A" * 1000},
        ]
        msgs = picture_prompt.build_director_messages(companion, transcript)
        user_content = msgs[1]["content"]
        # personality raised to 1200 -> 1150 chars kept intact
        self.assertIn("Personality: " + ("P" * 1150), user_content)
        # description and scenario stay clipped at 900
        self.assertIn("Appearance and background: " + ("D" * 900) + "…", user_content)
        self.assertIn("Setting: " + ("S" * 900) + "…", user_content)
        # message clip raised to 950 -> 900 intact, 1000 clipped at 950
        self.assertIn("User: " + ("U" * 900), user_content)
        self.assertIn("Assistant: " + ("A" * 950) + "…", user_content)

        # personality over 1200 gets clipped at 1200
        companion_long = {"name": "Aria", "personality": "P" * 1250}
        msgs_long = picture_prompt.build_director_messages(companion_long, [])
        self.assertIn("Personality: " + ("P" * 1200) + "…", msgs_long[1]["content"])

    def test_dynamic_system_prompt_styles_and_auto_resolution(self):
        # Every listed style injects distinct formatting rules into system prompt
        for item in picture_prompt.list_prompt_styles():
            sid = item["id"]
            if sid == "auto":
                continue
            sys_msg = picture_prompt.build_director_system(sid)
            self.assertIn("Return ONE JSON object", sys_msg)
            self.assertIn("Core directing rule:", sys_msg)
            self.assertIn("latest 4 messages", sys_msg)
            self.assertIn("character card", sys_msg)
            self.assertIn(picture_prompt.PROMPT_STYLES[sid]["rules"], sys_msg)

        # Specific markers per template
        self.assertIn("An anime illustration of", picture_prompt.build_director_system("qwen-anime"))
        self.assertIn("sharp focus, natural skin texture", picture_prompt.build_director_system("qwen-real"))
        self.assertIn("score_7", picture_prompt.build_director_system("anima"))
        self.assertIn("never full sentences", picture_prompt.build_director_system("tags"))

        # Auto resolution across providers and ComfyUI workflows
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "gemini"),
            "natural",
        )
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "aihorde"),
            "tags",
        )
        qwen_anime_wf = {
            "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "An anime illustration of a girl"}},
        }
        qwen_real_wf = {
            "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "A photograph of a room"}},
        }
        anima_wf = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "Hoseki_animaBaseV1.safetensors"}},
            "6": {"class_type": "KSampler", "inputs": {"sampler_name": "er_sde"}},
        }
        sd_wf = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "hassaku.safetensors"}},
        }
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "comfyui", "comfyui:ABCWUTU", qwen_anime_wf),
            "qwen-anime",
        )
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "comfyui", "comfyui:NoctQ_V4_workflow", qwen_real_wf),
            "qwen-real",
        )
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "comfyui", "comfyui:ABCanima", anima_wf),
            "anima",
        )
        self.assertEqual(
            picture_prompt.resolve_prompt_style("auto", "comfyui", "comfyui:ABCchengshu", sd_wf),
            "tags",
        )
        # Explicit user selection overrides auto (including "none")
        self.assertEqual(
            picture_prompt.resolve_prompt_style("tags", "comfyui", "comfyui:ABCWUTU", qwen_anime_wf),
            "tags",
        )
        self.assertEqual(
            picture_prompt.resolve_prompt_style("none", "comfyui", "comfyui:ABCWUTU", qwen_anime_wf),
            "none",
        )
        self.assertEqual(picture_prompt.default_negative_for("none"), "")
        none_sys = picture_prompt.build_director_system("none")
        self.assertIn("comma-separated visual phrases, no sentences", none_sys)
        self.assertIn('Rules for "negative": short comma-separated list of what to avoid.', none_sys)
        self.assertNotIn("(baseline:", none_sys)


class TranscriptPreconditionTests(unittest.TestCase):
    def _cfg(self):
        cfg = picture_config.default_config()
        cfg["enabled"] = True
        cfg["autoChat"] = True
        cfg["activeProvider"] = "comfyui"
        cfg["providers"]["comfyui"]["enabled"] = True
        return cfg

    def test_transcript_keeps_latest_4_usable_messages(self):
        sample = [
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "m2"},
            {"role": "system", "content": "ignored system"},
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": "summary", "message_type": "compaction_summary"},
            {"role": "user", "content": "m3"},
            {"role": "assistant", "content": "m4"},
            {"role": "user", "content": "m5"},
            {"role": "assistant", "content": "m6"},
        ]
        with patch.object(picture_service.companion_db, "get_messages", return_value=sample):
            out = picture_service._transcript("c1", "main", picture_prompt.TRANSCRIPT_TURNS)
        self.assertEqual(
            out,
            [
                {"role": "User", "text": "m3"},
                {"role": "Assistant", "text": "m4"},
                {"role": "User", "text": "m5"},
                {"role": "Assistant", "text": "m6"},
            ],
        )

    def test_start_manual_fails_immediately_under_4_messages(self):
        sample = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "summary", "message_type": "compaction_summary"},
        ]
        with (
            patch.object(picture_service.picture_config, "load_config", return_value=self._cfg()),
            patch.object(picture_service.store, "count_today", return_value=0),
            patch.object(picture_service.companion_db, "get_messages", return_value=sample),
            patch.object(picture_service.store, "create_job") as create_job,
        ):
            with self.assertRaises(picture_service.PictureError) as ctx:
                picture_service.start_manual("c1", "main")
            self.assertIn("4", str(ctx.exception))
            create_job.assert_not_called()

    def test_maybe_auto_skips_under_4_messages(self):
        sample = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        with (
            patch.object(picture_service.picture_config, "load_config", return_value=self._cfg()),
            patch.object(picture_service.store, "has_running", return_value=False),
            patch.object(picture_service.store, "count_today", return_value=0),
            patch.object(picture_service.store, "seconds_since_last", return_value=None),
            patch.object(picture_service.companion_db, "get_messages", return_value=sample),
            patch.object(picture_service.store, "create_job") as create_job,
        ):
            res = picture_service.maybe_auto("c1", "main", "chat")
            self.assertIsNone(res)
            create_job.assert_not_called()

    def test_start_manual_succeeds_at_4_messages(self):
        sample = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        with (
            patch.object(picture_service.picture_config, "load_config", return_value=self._cfg()),
            patch.object(picture_service.store, "count_today", return_value=0),
            patch.object(picture_service.companion_db, "get_messages", return_value=sample),
            patch.object(picture_service.store, "create_job", return_value=42) as create_job,
            patch.object(picture_service, "_spawn", side_effect=lambda coro: coro.close()) as spawn,
        ):
            jid = picture_service.start_manual("c1", "main")
            self.assertEqual(jid, 42)
            create_job.assert_called_once()
            spawn.assert_called_once()


class DirectorModelPriorityTests(unittest.TestCase):
    def _write_llm_cfg(self, tmp_dir: str) -> Path:
        path = Path(tmp_dir) / "llm_providers.json"
        data = {
            "activeProviderIndex": 1,
            "providers": [
                {
                    "id": "default",
                    "provider": "deepseek",
                    "apiKey": "k-ds",
                    "baseUrl": "https://api.deepseek.com/v1/",
                    "models": ["deepseek-flash", "deepseek-v4-pro"],
                },
                {
                    "id": "p-mimo",
                    "provider": "xiaomimimo",
                    "apiKey": "k-mimo",
                    "baseUrl": "https://api.xiaomimimo.com/v1",
                    "models": ["mimo-v2.6-flash", "mimo-v2.6-pro"],
                },
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_picture_studio_prompt_model_has_highest_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._write_llm_cfg(tmp)
            with patch.object(picture_prompt, "LLM_CONFIG_PATH", cfg_path):
                # Explicit provider::model overrides activeProviderIndex and frontend chat model
                res = picture_prompt.resolve_director_llm(
                    preferred_id="default::deepseek-v4-pro",
                    chat_provider_id="p-mimo",
                    chat_model="mimo-v2.6-pro",
                )
                self.assertEqual(res["provider_type"], "deepseek")
                self.assertEqual(res["model"], "deepseek-v4-pro")
                self.assertEqual(res["source"], "picture_studio")

                # Legacy bare provider id still overrides chat model even on same provider
                res_legacy = picture_prompt.resolve_director_llm(
                    preferred_id="default",
                    chat_provider_id="default",
                    chat_model="deepseek-v4-pro",
                )
                self.assertEqual(res_legacy["provider_type"], "deepseek")
                self.assertEqual(res_legacy["model"], "deepseek-flash")
                self.assertEqual(res_legacy["source"], "picture_studio")

                # Missing explicit selection must fail loudly instead of falling back to chat
                with self.assertRaises(picture_prompt.DirectorError):
                    picture_prompt.resolve_director_llm(
                        preferred_id="deleted-provider::some-model",
                        chat_provider_id="p-mimo",
                        chat_model="mimo-v2.6-pro",
                    )

                # Empty selection falls back to frontend chat selection
                res_empty = picture_prompt.resolve_director_llm(
                    preferred_id="",
                    chat_provider_id="p-mimo",
                    chat_model="mimo-v2.6-pro",
                )
                self.assertEqual(res_empty["provider_type"], "xiaomimimo")
                self.assertEqual(res_empty["model"], "mimo-v2.6-pro")
                self.assertEqual(res_empty["source"], "chat_fallback")


if __name__ == "__main__":
    unittest.main()

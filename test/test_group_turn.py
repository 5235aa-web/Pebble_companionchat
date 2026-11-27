# -*- coding: utf-8 -*-
"""Group room speaker selection, mention stripping, and turn locking."""

import ast
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import audio_input, companion_db
from backend.group_sessions import db, memory, prompt, routes, turn


def _member(cid: str, name: str) -> dict:
    return {"companion_id": cid, "name": name, "personality": "", "description": ""}


class PickSpeakerAtMentionTest(unittest.TestCase):
    def test_cjk_without_space_matches_addressed_member(self):
        members = [_member("c1", "小红"), _member("c2", "小美")]
        picked = turn.pick_speaker(members, "@小美你觉得呢", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_cjk_with_punctuation_and_second_name_still_picks_at_target(self):
        members = [_member("c1", "小红"), _member("c2", "小美")]
        picked = turn.pick_speaker(members, "@小美：你去问问小红", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_multiword_latin_full_name_beats_shared_first_word(self):
        members = [_member("c1", "Grandma Rose"), _member("c2", "Grandma Evelyn")]
        picked = turn.pick_speaker(members, "@Grandma Evelyn, what do you think?", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_single_word_alias_reaches_multiword_member(self):
        members = [_member("c1", "Bob"), _member("c2", "Grandma Evelyn")]
        picked = turn.pick_speaker(members, "@Evelyn, over here", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_longest_prefix_beats_shorter_name(self):
        members = [_member("c1", "Lily"), _member("c2", "Lily Rose")]
        picked = turn.pick_speaker(members, "@Lily Rose, wait", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_email_address_does_not_trigger_at_mention(self):
        members = [_member("c1", "Alice"), _member("c2", "lin")]
        history = [{"speaker_id": "c2", "content": "hi"}]
        for msg in ("Write to me at foo@lin.com", "Write to me at lin@example.com"):
            picked = turn.pick_speaker(members, msg, history)
            self.assertEqual(picked["companion_id"], "c1", msg)


class PickSpeakerTextMentionTest(unittest.TestCase):
    def test_earliest_mention_in_sentence_wins_over_roster_order(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob")]
        picked = turn.pick_speaker(members, "Bob, did you see Alice earlier?", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_latin_word_boundary_ignores_substrings(self):
        members = [_member("c1", "Lin"), _member("c2", "Bob")]
        history = [{"speaker_id": "c1", "content": "hello"}]
        picked = turn.pick_speaker(members, "Please listen carefully to the announcement", history)
        self.assertEqual(picked["companion_id"], "c2")

    def test_multiword_latin_part_matches_without_at(self):
        members = [_member("c1", "Alice"), _member("c2", "Grandma Evelyn")]
        picked = turn.pick_speaker(members, "Evelyn, how is the tea?", [])
        self.assertEqual(picked["companion_id"], "c2")


class PickSpeakerFallbackAndContinuityTest(unittest.TestCase):
    def test_dormant_member_speaks_before_repeat(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob"), _member("c3", "Cara")]
        history = [
            {"speaker_id": "user", "content": "hi"},
            {"speaker_id": "c1", "content": "hello"},
        ]
        picked = turn.pick_speaker(members, "Nice weather today, isn't it everyone?", history)
        self.assertEqual(picked["companion_id"], "c2")

    def test_short_followup_sticks_to_last_speaker_up_to_two_turns(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob")]
        history = [
            {"speaker_id": "c2", "content": "hey"},
            {"speaker_id": "user", "content": "@Alice what happened?"},
            {"speaker_id": "c1", "content": "I dropped the cup."},
        ]
        # First follow-up sticks to Alice (c1) instead of jumping straight to Bob.
        picked1 = turn.pick_speaker(members, "Really?", history)
        self.assertEqual(picked1["companion_id"], "c1")

        # After two consecutive Alice turns, rotate to Bob.
        history.extend([
            {"speaker_id": "user", "content": "Really?"},
            {"speaker_id": "c1", "content": "Yes, right on the rug."},
        ])
        picked2 = turn.pick_speaker(members, "Oh no?", history)
        self.assertEqual(picked2["companion_id"], "c2")

    def test_empty_continue_rotates_away_from_last_speaker(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob")]
        history = [
            {"speaker_id": "c2", "content": "hey"},
            {"speaker_id": "c1", "content": "what's up"},
        ]
        picked = turn.pick_speaker(members, "", history)
        self.assertEqual(picked["companion_id"], "c2")

    def test_narration_rotates_instead_of_sticking(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob")]
        history = [
            {"speaker_id": "c2", "content": "hey"},
            {"speaker_id": "c1", "content": "what's up"},
        ]
        picked = turn.pick_speaker(members, "Thunder rumbles.", history, is_narration=True)
        self.assertEqual(picked["companion_id"], "c2")


class PromptStripMentionsTest(unittest.TestCase):
    def test_strips_full_and_alias_mentions_preserving_emails(self):
        members = [_member("c1", "Grandma Evelyn"), _member("c2", "小美")]
        text = "@Grandma Evelyn and @Evelyn, ask @小美你觉得呢 or mail a@evelyn.com"
        cleaned = prompt._strip_mentions(text, members)
        self.assertNotIn("@Grandma", cleaned)
        self.assertNotIn("@Evelyn", cleaned)
        self.assertNotIn("@小美", cleaned)
        self.assertIn("a@evelyn.com", cleaned)

    def test_build_messages_strips_mentions_in_narration(self):
        members = [_member("c1", "Alice")]
        session = {"scene": "", "user_note": ""}
        history = [
            {"speaker_id": "narrator", "kind": "narration", "content": "@Alice looks out the window."}
        ]
        with patch("backend.group_sessions.prompt.load_global_prompts", return_value={}):
            msgs = prompt.build_messages(session, members, members[0], history)
        narration_msg = [m["content"] for m in msgs if "present moment" in m["content"]]
        self.assertEqual(len(narration_msg), 1)
        self.assertNotIn("@Alice", narration_msg[0])
        self.assertIn("Alice looks out the window.", narration_msg[0])

    def test_build_system_prompt_includes_scenario_with_macros(self):
        speaker = {
            **_member("c1", "Alice"),
            "personality": "Calm",
            "description": "Librarian",
            "scenario": "{{char}} meets {{user}} in the old archive.",
            "user_info": "Name: Lin",
        }
        session = {"scene": "", "user_note": ""}
        with patch("backend.group_sessions.prompt.load_global_prompts", return_value={}):
            sys_prompt = prompt.build_system_prompt(session, [speaker], speaker)
        self.assertIn("[Scenario]\nAlice meets Lin in the old archive.", sys_prompt)

    def test_build_messages_attaches_recent_images_only(self):
        img_file = self.tmp / "pic.png"
        img_file.write_bytes(b"fakepng")
        members = [_member("c1", "Alice")]
        session = {"scene": "", "user_note": ""}
        att = [{"kind": "image", "url": "/uploads/attachments/pic.png", "mime": "image/png"}]

        with (
            patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            patch("backend.group_sessions.prompt.resolve_image_path", return_value=img_file),
        ):
            # Image-only message (empty text) still reaches the model as multimodal parts.
            recent_msgs = prompt.build_messages(
                session,
                members,
                members[0],
                [{"speaker_id": "user", "content": "", "attachments": att}],
            )
            user_msg = [m for m in recent_msgs if m["role"] == "user"][-1]
            self.assertIsInstance(user_msg["content"], list)
            self.assertEqual(user_msg["content"][1]["type"], "image_url")
            self.assertTrue(
                user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            )

            # Older image (>2 messages behind) is not re-encoded into base64.
            stale_history = [
                {"speaker_id": "user", "content": "look", "attachments": att},
                {"speaker_id": "c1", "content": "nice"},
                {"speaker_id": "user", "content": "and then?"},
                {"speaker_id": "c1", "content": "more"},
                {"speaker_id": "user", "content": "next"},
            ]
            stale_msgs = prompt.build_messages(session, members, members[0], stale_history)
            first_user = [m for m in stale_msgs if m["role"] == "user"][0]
            self.assertIsInstance(first_user["content"], str)

            # Context token meter disables base64 image encoding.
            no_img_msgs = prompt.build_messages(
                session,
                members,
                members[0],
                [{"speaker_id": "user", "content": "look", "attachments": att}],
                include_images=False,
            )
            self.assertIsInstance(no_img_msgs[-1]["content"], str)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-grp-prompt-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PersonaRelevanceAndRelayTest(unittest.TestCase):
    def test_persona_relevance_picks_domain_expert_even_when_other_is_dormant(self):
        members = [
            {
                "companion_id": "c1",
                "name": "Bob",
                "personality": "Warm",
                "description": "French pastry chef who loves baking and desserts",
                "scenario": "",
            },
            {
                "companion_id": "c2",
                "name": "Alice",
                "personality": "Calm",
                "description": "急诊科医生，擅长急救和处理头晕发烧症状",
                "scenario": "",
            },
        ]
        picked = turn.pick_speaker(members, "我有点头晕，这里有医生吗？", [])
        self.assertEqual(picked["companion_id"], "c2")

    def test_small_talk_does_not_misfire_persona_relevance(self):
        members = [
            {
                "companion_id": "c1",
                "name": "Bob",
                "personality": "Warm",
                "description": "French pastry chef",
                "scenario": "",
            },
            {
                "companion_id": "c2",
                "name": "Alice",
                "personality": "Calm",
                "description": "Emergency doctor",
                "scenario": "",
            },
        ]
        history = [{"speaker_id": "c1", "content": "hi"}]
        picked = turn.pick_speaker(members, "今天天气真不错", history)
        self.assertEqual(picked["companion_id"], "c2")

    def test_relay_empty_turn_hands_off_to_member_addressed_by_last_ai(self):
        members = [_member("c1", "Alice"), _member("c2", "Bob"), _member("c3", "Cara")]
        history = [
            {"speaker_id": "c2", "content": "hello"},
            {"speaker_id": "c3", "content": "hi"},
            {"speaker_id": "c1", "content": "Cara，你觉得这个计划稳妥吗？"},
        ]
        picked = turn.pick_speaker(members, "", history)
        self.assertEqual(picked["companion_id"], "c3")


class TurnLockAndPendingMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-grp-turn-"))
        self._orig_db = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / "companions.db"
        db._ready = False
        session = db.create_session("Turn Room", scene="A cafe")
        self.session_id = int(session["id"])
        conn = companion_db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO companions (id, name, personality, description) VALUES (?, ?, ?, ?)",
                ("c1", "Alice", "Warm", "Barista"),
            )
            conn.commit()
        finally:
            conn.close()
        db.add_member(self.session_id, "c1")

    def tearDown(self):
        turn._turn_locks.pop(self.session_id, None)
        db._ready = False
        companion_db.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_busy_room_refuses_without_writing_pending_message(self):
        async def scenario():
            lock = turn._lock_for(self.session_id)
            await lock.acquire()
            try:
                members = db.list_members(self.session_id)
                events = []
                async for ev in turn.generate_turn(
                    self.session_id,
                    {"provider_type": "openai", "model": "gpt-4o-mini"},
                    members[0],
                    [],
                    members,
                    pending_message={
                        "content": "Should not be stored",
                        "role": "user",
                        "speaker_id": "user",
                        "speaker_name": "You",
                        "kind": "say",
                    },
                ):
                    events.append(ev)
                self.assertEqual(events[0]["type"], "error")
                self.assertEqual(db.list_messages(self.session_id), [])
            finally:
                lock.release()

        asyncio.run(scenario())

    def test_pending_message_persists_and_emits_ids(self):
        async def fake_stream(**_kwargs):
            yield {"type": "content", "content": "Hello there!"}

        async def scenario():
            members = db.list_members(self.session_id)
            events = []
            with (
                patch("backend.group_sessions.turn.call_llm_stream", side_effect=fake_stream),
                patch("backend.group_sessions.memory.schedule_extraction"),
                patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            ):
                async for ev in turn.generate_turn(
                    self.session_id,
                    {"provider_type": "openai", "model": "gpt-4o-mini"},
                    members[0],
                    [],
                    members,
                    pending_message={
                        "content": "@Alice hi",
                        "role": "user",
                        "speaker_id": "user",
                        "speaker_name": "You",
                        "kind": "say",
                    },
                ):
                    events.append(ev)

            speaker_ev = next(e for e in events if e["type"] == "speaker")
            done_ev = next(e for e in events if e["type"] == "done")
            self.assertIn("user_message", speaker_ev)
            self.assertEqual(done_ev["user_message_id"], speaker_ev["user_message"]["id"])
            stored = db.list_messages(self.session_id)
            self.assertEqual([m["content"] for m in stored], ["@Alice hi", "Hello there!"])

        asyncio.run(scenario())

    def test_replace_message_id_deletes_old_reply_and_stores_replacement(self):
        conn = companion_db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO companions (id, name, personality, description) VALUES (?, ?, ?, ?)",
                ("c2", "Bob", "Sharp", "Detective"),
            )
            conn.commit()
        finally:
            conn.close()
        db.add_member(self.session_id, "c2")
        db.add_message(self.session_id, "Who broke the vase?", role="user", speaker_id="user", speaker_name="You")
        old_ai = db.add_message(self.session_id, "Maybe the wind.", role="assistant", speaker_id="c1", speaker_name="Alice")

        async def fake_stream(**_kwargs):
            yield {"type": "content", "content": "Let me check the footprints."}

        async def scenario():
            members = db.list_members(self.session_id)
            bob = next(m for m in members if m["companion_id"] == "c2")
            history = db.list_messages(self.session_id)
            with (
                patch("backend.group_sessions.turn.call_llm_stream", side_effect=fake_stream),
                patch("backend.group_sessions.memory.schedule_extraction"),
                patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            ):
                async for _ in turn.generate_turn(
                    self.session_id,
                    {"provider_type": "openai", "model": "gpt-4o-mini"},
                    bob,
                    history,
                    members,
                    replace_message_id=int(old_ai["id"]),
                ):
                    pass
            stored = db.list_messages(self.session_id)
            self.assertEqual(len(stored), 2)
            self.assertEqual(stored[-1]["speaker_id"], "c2")
            self.assertEqual(stored[-1]["content"], "Let me check the footprints.")

        asyncio.run(scenario())

    def test_charx_companion_cannot_be_invited_to_group_room(self):
        companion_db.create_companion("c_charx", {"name": "Rin"}, card_format="charx")
        res = db.add_member(self.session_id, "c_charx")
        self.assertFalse(res.get("ok"))
        self.assertIn("charx", (res.get("error") or "").lower())


class GroupVoiceTurnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-grp-voice-"))
        self._orig_db = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / "companions.db"
        db._ready = False
        audio_input.AUDIO_REJECTED.clear()
        session = db.create_session("Voice Room", scene="A quiet terrace")
        self.session_id = int(session["id"])
        conn = companion_db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO companions (id, name, personality, description) VALUES (?, ?, ?, ?)",
                ("c1", "Alice", "Warm", "Barista"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO companions (id, name, personality, description) VALUES (?, ?, ?, ?)",
                ("c2", "Bob", "Sharp", "Detective"),
            )
            conn.commit()
        finally:
            conn.close()
        db.add_member(self.session_id, "c1")
        db.add_member(self.session_id, "c2")

    def tearDown(self):
        turn._turn_locks.pop(self.session_id, None)
        audio_input.AUDIO_REJECTED.clear()
        db._ready = False
        companion_db.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_voice_turn_injects_audio_with_speaker_prefix_and_stores_transcript(self):
        wav = self.tmp / "sample.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        seen_messages = []

        async def fake_stream(**kwargs):
            seen_messages.append(kwargs["messages"])
            yield {"type": "content", "content": "I heard you clearly."}

        async def scenario():
            members = db.list_members(self.session_id)
            with (
                patch("backend.group_sessions.turn.call_llm_stream", side_effect=fake_stream),
                patch("backend.group_sessions.memory.schedule_extraction"),
                patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            ):
                async for _ in turn.generate_turn(
                    self.session_id,
                    {"provider_type": "openai", "model": "gpt-4o-audio-preview"},
                    members[0],
                    [],
                    members,
                    pending_message={
                        "content": "Alice, can you hear me?",
                        "role": "user",
                        "speaker_id": "user",
                        "speaker_name": "You",
                        "kind": "voice",
                    },
                    audio_file_path=str(wav),
                ):
                    pass

        asyncio.run(scenario())
        self.assertEqual(len(seen_messages), 1)
        last_user = [m for m in seen_messages[0] if m["role"] == "user"][-1]
        self.assertIsInstance(last_user["content"], list)
        self.assertEqual(last_user["content"][0], {"type": "text", "text": "You:"})
        self.assertEqual(last_user["content"][1]["type"], "input_audio")
        stored = db.list_messages(self.session_id)
        self.assertEqual(stored[0]["kind"], "voice")
        self.assertEqual(stored[0]["content"], "Alice, can you hear me?")
        # Voice rows stay part of replay and memory extraction.
        transcript = memory._format_transcript(stored, db.list_members(self.session_id))
        self.assertIn("User: Alice, can you hear me?", transcript)

    def test_audio_rejection_falls_back_to_transcript_and_caches_model(self):
        wav = self.tmp / "sample.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        calls = []

        async def fake_stream(**kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                yield {"type": "error", "message": "unknown variant `input_audio`, expected text"}
                return
            yield {"type": "content", "content": "Got the text."}

        async def scenario():
            members = db.list_members(self.session_id)
            cfg = {"provider_type": "deepseek", "model": "deepseek-chat"}
            with (
                patch("backend.group_sessions.turn.call_llm_stream", side_effect=fake_stream),
                patch("backend.group_sessions.memory.schedule_extraction"),
                patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            ):
                async for _ in turn.generate_turn(
                    self.session_id,
                    cfg,
                    members[0],
                    [],
                    members,
                    pending_message={
                        "content": "Hello room",
                        "role": "user",
                        "speaker_id": "user",
                        "speaker_name": "You",
                        "kind": "voice",
                    },
                    audio_file_path=str(wav),
                ):
                    pass
                # Second turn for the same provider/model should skip input_audio immediately.
                async for _ in turn.generate_turn(
                    self.session_id,
                    cfg,
                    members[1],
                    db.list_messages(self.session_id),
                    members,
                    pending_message={
                        "content": "Second voice line",
                        "role": "user",
                        "speaker_id": "user",
                        "speaker_name": "You",
                        "kind": "voice",
                    },
                    audio_file_path=str(wav),
                ):
                    pass

        asyncio.run(scenario())
        self.assertEqual(len(calls), 3)
        last_user = lambda msgs: [m for m in msgs if m["role"] == "user"][-1]["content"]
        self.assertIsInstance(last_user(calls[0]), list)
        self.assertIsInstance(last_user(calls[1]), str)
        self.assertIn("Hello room", last_user(calls[1]))
        self.assertIsInstance(last_user(calls[2]), str)
        self.assertIn("Second voice line", last_user(calls[2]))
        self.assertIn(("deepseek", "deepseek-chat"), audio_input.AUDIO_REJECTED)

    def test_audio_group_stream_auto_picks_speaker_from_transcript_and_cleans_temp_file(self):
        class FakeStt:
            def transcribe_bytes(self, audio_bytes, sample_rate=16000):
                self.seen = (audio_bytes, sample_rate)
                return "Bob, what do you think?"

        fake_stt = FakeStt()
        captured_audio_paths = []

        async def fake_stream(**kwargs):
            yield {"type": "content", "content": "Looks suspicious."}

        orig_generate = turn.generate_turn

        async def spy_generate(*args, **kwargs):
            captured_audio_paths.append(kwargs.get("audio_file_path"))
            async for ev in orig_generate(*args, **kwargs):
                yield ev

        class FakeRegistry:
            def get_engine(self):
                return fake_stt

        async def scenario():
            with (
                patch("backend.stt_registry.get_registry", return_value=FakeRegistry()),
                patch("backend.group_sessions.turn.call_llm_stream", side_effect=fake_stream),
                patch("backend.group_sessions.turn.generate_turn", side_effect=spy_generate),
                patch("backend.group_sessions.memory.schedule_extraction"),
                patch("backend.group_sessions.prompt.load_global_prompts", return_value={}),
            ):
                resp = await routes._audio_group_stream(
                    self.session_id,
                    b"RIFF\x00\x00\x00\x00WAVEfmt ",
                    {"provider_type": "openai", "model": "gpt-4o"},
                )
                body = []
                async for chunk in resp.body_iterator:
                    body.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
                return "".join(body)

        sse_text = asyncio.run(scenario())
        self.assertIn('"type": "stt_transcript"', sse_text)
        self.assertIn("Bob, what do you think?", sse_text)
        self.assertIn('"name": "Bob"', sse_text)
        self.assertEqual(len(captured_audio_paths), 1)
        self.assertTrue(captured_audio_paths[0])
        self.assertFalse(Path(captured_audio_paths[0]).exists())


class GroupRouteSurfaceTest(unittest.TestCase):
    def test_no_duplicate_group_routes(self):
        routes_path = (
            Path(__file__).resolve().parent.parent / "backend" / "group_sessions" / "routes.py"
        )
        tree = ast.parse(routes_path.read_text(encoding="utf-8"))
        found = []
        verbs = {"get", "post", "put", "delete", "patch"}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in verbs
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    found.append((dec.func.attr, dec.args[0].value))
        self.assertEqual(len(found), len(set(found)))
        self.assertIn(("post", "/sessions/{session_id}/audio/stream"), found)
        self.assertIn(("post", "/sessions/{session_id}/audio/stream-json"), found)


if __name__ == "__main__":
    unittest.main()

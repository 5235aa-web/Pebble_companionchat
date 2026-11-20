# -*- coding: utf-8 -*-
"""Group room memory: zero-dependency hybrid retrieval, extraction, supersedes, optional vectors."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import companion_db
from backend.group_sessions import db, embeddings, memory, retriever


class GroupMemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-grp-mem-"))
        self._orig_db = companion_db.DB_PATH
        companion_db.DB_PATH = self.tmp / "companions.db"
        # Force fresh schema initialisation on the temp DB path.
        db._ready = False
        session = db.create_session("Test Room", scene="A quiet library")
        self.session_id = int(session["id"])

    def tearDown(self):
        db._ready = False
        companion_db.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)


class RetrieverUnitTest(GroupMemoryCase):
    def test_english_word_boundary_prevents_substring_misfires(self):
        db.add_memory(self.session_id, "Lin prefers sitting by the window", "Lin,window", "Lin")
        db.add_memory(self.session_id, "Ann brought an old map", "Ann,map", "Ann")

        hits = db.search_memories(self.session_id, "Please listen carefully to this announcement")
        self.assertEqual(hits, [])

    def test_stemming_matches_inflected_english(self):
        db.add_memory(
            self.session_id,
            "Rhea promised to return before winter",
            "promise,return,winter",
            "Rhea",
        )
        hits = db.search_memories(self.session_id, "She was promising she would be returning soon")
        self.assertEqual(len(hits), 1)
        self.assertIn("promised", hits[0]["content"])

    def test_bilingual_and_synonym_keywords_recall_across_wording(self):
        db.add_memory(
            self.session_id,
            "Rhea promised to come back",
            "promise,word,swear,return,come back,答应,承诺,回来,约定",
            "Rhea",
        )
        db.add_memory(
            self.session_id,
            "Aslyn dislikes bitter coffee",
            "coffee,bitter,咖啡,苦",
            "Aslyn",
        )

        for probe in (
            "you gave me your word",
            "她答应会回来",
            "Did Rhea keep her promise?",
        ):
            hits = db.search_memories(self.session_id, probe)
            self.assertEqual([h["content"] for h in hits], ["Rhea promised to come back"], probe)

    def test_cjk_ngram_searches_content_even_with_empty_keywords(self):
        db.add_memory(
            self.session_id,
            "阿杰在阁楼里找到了一把旧口琴",
            "",
            "阿杰",
        )
        db.add_memory(
            self.session_id,
            "林夏喜欢喝茉莉花茶",
            "",
            "林夏",
        )
        hits = db.search_memories(self.session_id, "你还记得阁楼里那把旧口琴吗")
        self.assertEqual(len(hits), 1)
        self.assertIn("旧口琴", hits[0]["content"])

    def test_noise_floor_blocks_single_weak_content_overlap(self):
        db.add_memory(
            self.session_id,
            "Rhea fixed the kitchen window tonight",
            "window,kitchen,repair,窗户,厨房",
            "Rhea",
        )
        hits = db.search_memories(self.session_id, "What should we have for dinner tonight?")
        self.assertEqual(hits, [])

    def test_mark_two_standing_memories_ride_along_without_keyword_hit(self):
        strong_id = db.add_memory(
            self.session_id,
            "Always speak softly around the sleeping cat",
            "cat,quiet",
            "",
        )
        db.update_memory(self.session_id, strong_id, mark=2)
        db.add_memory(
            self.session_id,
            "Aslyn left her umbrella on the train",
            "umbrella,train,雨伞,火车",
            "Aslyn",
        )

        hits = db.search_memories(self.session_id, "Did anyone find that umbrella on the train?")
        ids = [int(h["id"]) for h in hits]
        self.assertIn(strong_id, ids)
        self.assertEqual(len(hits), 2)

        unrelated_hits = db.search_memories(self.session_id, "Let's check the telescope")
        self.assertEqual([int(h["id"]) for h in unrelated_hits], [strong_id])

    def test_use_count_rotates_tied_memories(self):
        m1 = db.add_memory(self.session_id, "Aslyn likes rainy evenings", "rain,evening", "Aslyn")
        m2 = db.add_memory(self.session_id, "Rhea likes rainy evenings too", "rain,evening", "Rhea")
        for _ in range(5):
            db.touch_memories([m1])

        hits = db.search_memories(self.session_id, "What a rainy evening")
        self.assertEqual([int(h["id"]) for h in hits], [m2, m1])

    def test_near_duplicate_catches_minor_rephrasing(self):
        existing = ["Rhea promised to come back before winter"]
        self.assertTrue(
            retriever.is_near_duplicate("Rhea promised she would come back before winter", existing)
        )
        self.assertFalse(
            retriever.is_near_duplicate("Aslyn bought a brass lantern at the market", existing)
        )


class ExtractionAndBlockTest(GroupMemoryCase):
    def test_extraction_injects_existing_and_retires_superseded_unpinned_only(self):
        old_unpinned = db.add_memory(
            self.session_id,
            "Aslyn is angry at the user after the argument",
            "angry,argument",
            "Aslyn",
        )
        pinned_id = db.add_memory(
            self.session_id,
            "Aslyn hates loud thunder",
            "thunder,loud",
            "Aslyn",
        )
        db.update_memory(self.session_id, pinned_id, mark=1)

        # Seed 16 messages so extract_memories triggers one batch.
        for i in range(16):
            db.add_message(
                self.session_id,
                f"Turn line {i}: Aslyn smiled and forgave the user.",
                role="user" if i % 2 == 0 else "assistant",
                speaker_id="user" if i % 2 == 0 else "c1",
                speaker_name="User" if i % 2 == 0 else "Aslyn",
            )

        captured_prompts = []

        async def fake_stream(**kwargs):
            captured_prompts.append(kwargs["messages"][0]["content"])
            payload = [
                {
                    "content": "Aslyn forgave the user and made up",
                    "keywords": "forgive,made up,reconcile,原谅,和好",
                    "about": "Aslyn",
                    "supersedes": str(old_unpinned),
                },
                {
                    "content": "Aslyn actually enjoys loud thunder now",
                    "keywords": "thunder,enjoy,打雷",
                    "about": "Aslyn",
                    "supersedes": str(pinned_id),
                },
            ]
            yield {"type": "content", "content": json.dumps(payload, ensure_ascii=False)}

        with patch("backend.group_sessions.memory.call_llm_stream", side_effect=fake_stream):
            written = asyncio.run(
                memory.extract_memories(
                    self.session_id,
                    {"provider_type": "openai", "model": "mock-model"},
                )
            )

        self.assertEqual(written, 2)
        self.assertIn(f"[{old_unpinned}] Aslyn is angry", captured_prompts[0])
        remaining_ids = {int(m["id"]) for m in db.list_memories(self.session_id)}
        self.assertNotIn(old_unpinned, remaining_ids)
        # Pinned memory (`mark=1`) must survive automatic supersedes.
        self.assertIn(pinned_id, remaining_ids)

    def test_build_memory_block_respects_touch_and_collect(self):
        mid = db.add_memory(
            self.session_id,
            "Rhea hid the spare key under the ceramic flowerpot",
            "key,flowerpot,ceramic,钥匙,花盆",
            "Rhea",
        )
        history = [{"role": "user", "content": "Where did we put the spare key?"}]

        collected = []
        block = asyncio.run(
            memory.build_memory_block(self.session_id, history, touch=False, collect=collected)
        )
        self.assertIn("ceramic flowerpot", block)
        self.assertEqual([int(m["id"]) for m in collected], [mid])
        self.assertEqual(db.list_memories(self.session_id)[0]["use_count"], 0)

        asyncio.run(memory.build_memory_block(self.session_id, history, touch=True))
        self.assertEqual(db.list_memories(self.session_id)[0]["use_count"], 1)


class OptionalEmbeddingAdapterTest(GroupMemoryCase):
    def test_disabled_by_default_and_stdlib_vector_math_works(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PEBBLE_EMBEDDING_URL", None)
            os.environ.pop("PEBBLE_EMBEDDING_MODEL", None)
            with patch("backend.paths.load_app_config", return_value={}):
                self.assertFalse(embeddings.available())
                self.assertEqual(embeddings.status()["mode"], "hybrid_lexical")
                self.assertIsNone(asyncio.run(embeddings.encode(["hello"])))

        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        rows = [(10, embeddings.to_blob(v1)), (20, embeddings.to_blob(v2))]
        self.assertAlmostEqual(embeddings.top_score([0.99, 0.01, 0.0], rows), 0.99, places=5)
        self.assertEqual(embeddings.rank([0.9, 0.1, 0.0], rows), [10])

    def test_hybrid_fusion_rescues_pure_semantic_hit_when_vectors_exist(self):
        m_sem = db.add_memory(
            self.session_id,
            "Rhea is terrified of summer storms",
            "storm,summer",
            "Rhea",
        )
        m_other = db.add_memory(
            self.session_id,
            "Aslyn bought fresh apples",
            "apples,fruit",
            "Aslyn",
        )
        # Zero lexical overlap ("flash of lightning" vs "summer storms"), but
        # external vector cosine lifts `m_sem`.
        hits = retriever.rank_memories(
            "Did you see that flash of lightning outside?",
            db.list_memories(self.session_id),
            vector_scores={m_sem: 0.72, m_other: 0.25},
        )
        self.assertEqual([int(h["id"]) for h in hits], [m_sem])

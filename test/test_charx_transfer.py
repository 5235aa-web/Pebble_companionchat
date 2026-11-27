# -*- coding: utf-8 -*-
"""CHARX import/export, asset persistence, and staged large-file transfer tests."""

import base64
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import card_transfer, charx, companion_db, paths, tavern_exporter, tavern_importer, world_book_db


def _make_sample_charx(dest: Path, *, polyglot_jpeg: bool = False, bad_traversal: bool = False) -> Path:
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "Rin",
            "description": "A violinist in a rainy city.",
            "personality": "Calm and observant",
            "scenario": "Late evening rehearsal hall",
            "first_mes": "{{img::smile}} Welcome back. {{bgm::rain_theme}}",
            "tags": ["music", "charx"],
            "assets": [
                {"type": "icon", "name": "main", "uri": "embeded://assets/icon/main.png", "ext": "png"},
                {"type": "emotion", "name": "smile", "uri": "embedded://assets/emotion/smile.webp", "ext": "webp"},
                {"type": "bgm", "name": "rain_theme", "uri": "embeded://assets/audio/rain_theme.mp3", "ext": "mp3"},
            ],
            "extensions": {
                "risuai": {
                    "additionalAssets": [
                        ["chime", "assets/audio/chime.wav", "wav"],
                    ],
                },
            },
        },
    }
    zip_path = dest.with_suffix(".zip") if polyglot_jpeg else dest
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("card.json", json.dumps(card, ensure_ascii=False).encode("utf-8"))
        zf.writestr("assets/icon/main.png", b"\x89PNG\r\n\x1a\nMAIN_PNG_BYTES")
        zf.writestr("assets/emotion/smile.webp", b"RIFF\x00\x00\x00\x00WEBPSMIILE")
        zf.writestr("assets/audio/rain_theme.mp3", b"ID3FAKE_MP3_STREAM")
        zf.writestr("assets/audio/chime.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ")
        if bad_traversal:
            zf.writestr("../escape.txt", b"boom")

    if polyglot_jpeg:
        # Prefix valid JPEG SOI/header bytes before the ZIP payload.
        dest.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + zip_path.read_bytes())
        zip_path.unlink()
    return dest


class CharxRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pebble-charx-"))
        self._orig = {
            "DATA_ROOT": paths.DATA_ROOT,
            "UPLOAD_ROOT": paths.UPLOAD_ROOT,
            "AVATAR_DIR": paths.AVATAR_DIR,
            "COMPANION_ASSETS_DIR": paths.COMPANION_ASSETS_DIR,
            "TMP_UPLOAD_DIR": paths.TMP_UPLOAD_DIR,
            "DB_PATH": companion_db.DB_PATH,
            "WB_DB_PATH": world_book_db.DB_PATH,
        }
        paths.DATA_ROOT = self.tmp
        paths.UPLOAD_ROOT = self.tmp / "uploads"
        paths.AVATAR_DIR = paths.UPLOAD_ROOT / "companion_avatars"
        paths.COMPANION_ASSETS_DIR = paths.UPLOAD_ROOT / "companion_assets"
        paths.TMP_UPLOAD_DIR = self.tmp / "tmp_uploads"
        companion_db.DB_PATH = self.tmp / "companions.db"
        world_book_db.DB_PATH = companion_db.DB_PATH
        paths.ensure_dirs()

    def tearDown(self):
        paths.DATA_ROOT = self._orig["DATA_ROOT"]
        paths.UPLOAD_ROOT = self._orig["UPLOAD_ROOT"]
        paths.AVATAR_DIR = self._orig["AVATAR_DIR"]
        paths.COMPANION_ASSETS_DIR = self._orig["COMPANION_ASSETS_DIR"]
        paths.TMP_UPLOAD_DIR = self._orig["TMP_UPLOAD_DIR"]
        companion_db.DB_PATH = self._orig["DB_PATH"]
        world_book_db.DB_PATH = self._orig["WB_DB_PATH"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_charx_extracts_card_and_all_media_assets(self):
        src = _make_sample_charx(self.tmp / "rin.charx")
        cid = tavern_importer.import_charx(str(src))

        comp = companion_db.get_companion(cid)
        self.assertIsNotNone(comp)
        self.assertEqual(comp["name"], "Rin")
        self.assertTrue(comp["avatar_path"].startswith("uploads/companion_assets/"))

        assets = companion_db.get_companion_assets(cid)
        self.assertEqual(len(assets), 4)
        names = {a["name"] for a in assets}
        self.assertEqual(names, {"main", "smile", "rain_theme", "chime"})
        for a in assets:
            self.assertTrue((self.tmp / a["rel_path"]).is_file(), a["rel_path"])

    def test_import_rejects_zip_slip_traversal(self):
        src = _make_sample_charx(self.tmp / "evil.charx", bad_traversal=True)
        with self.assertRaises(charx.CharxError):
            tavern_importer.import_charx(str(src))

    def test_import_polyglot_charx_jpeg(self):
        src = _make_sample_charx(self.tmp / "poly.charx", polyglot_jpeg=True)
        cid = tavern_importer.import_charx(str(src))
        self.assertEqual(companion_db.get_companion(cid)["name"], "Rin")
        self.assertEqual(len(companion_db.get_companion_assets(cid)), 4)

    def test_delete_companion_removes_asset_files_on_disk(self):
        src = _make_sample_charx(self.tmp / "rin.charx")
        cid = tavern_importer.import_charx(str(src))
        asset_dir = paths.COMPANION_ASSETS_DIR / cid
        self.assertTrue(asset_dir.is_dir())

        self.assertTrue(companion_db.delete_companion(cid))
        self.assertFalse(asset_dir.exists())

    def test_export_charx_packs_media_and_reimports_cleanly(self):
        src = _make_sample_charx(self.tmp / "rin.charx")
        cid = tavern_importer.import_charx(str(src))
        comp = companion_db.get_companion(cid)
        assets = companion_db.get_companion_assets(cid)

        exported = self.tmp / "rin_exported.charx"
        tavern_exporter.export_to_charx_file(comp, assets, exported)
        self.assertTrue(exported.is_file())

        with zipfile.ZipFile(exported, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("card.json", names)
            card_json = json.loads(zf.read("card.json").decode("utf-8"))
            self.assertEqual(card_json["spec"], "chara_card_v3")
            self.assertEqual(len(card_json["data"]["assets"]), 4)
            # Compressed media formats should use ZIP_STORED to avoid CPU overhead.
            mp3_info = next(i for i in zf.infolist() if i.filename.endswith(".mp3"))
            self.assertEqual(mp3_info.compress_type, zipfile.ZIP_STORED)

        cid2 = tavern_importer.import_charx(str(exported))
        self.assertEqual(len(companion_db.get_companion_assets(cid2)), 4)

    def test_chunked_staged_upload_and_export_roundtrip(self):
        src = _make_sample_charx(self.tmp / "big_rin.charx")
        raw = src.read_bytes()

        init = card_transfer.init_staged_upload("big_rin.charx", len(raw))
        upload_id = init["upload_id"]
        step = 200  # small chunk size to exercise multi-chunk boundary & idempotent retry
        offset = 0
        while offset < len(raw):
            piece = raw[offset : offset + step]
            b64 = base64.b64encode(piece).decode("ascii")
            res = card_transfer.append_staged_chunk(upload_id, offset, b64)
            self.assertEqual(res["received_bytes"], offset + len(piece))
            # Idempotent retry of the same slice must succeed without duplicating bytes.
            retry = card_transfer.append_staged_chunk(upload_id, offset, b64)
            self.assertEqual(retry["received_bytes"], offset + len(piece))
            offset += len(piece)

        cid = card_transfer.commit_staged_upload(upload_id)
        self.assertEqual(companion_db.get_companion(cid)["name"], "Rin")
        self.assertFalse((paths.TMP_UPLOAD_DIR / "in" / upload_id).exists())

        comp = companion_db.get_companion(cid)
        assets = companion_db.get_companion_assets(cid)
        prep = card_transfer.prepare_charx_export(comp, assets)
        export_id = prep["export_id"]
        total_size = prep["total_size"]

        downloaded = bytearray()
        dl_offset = 0
        while dl_offset < total_size:
            chunk = card_transfer.read_export_chunk(export_id, dl_offset, 256)
            self.assertTrue(len(chunk) > 0)
            downloaded.extend(chunk)
            dl_offset += len(chunk)

        self.assertEqual(len(downloaded), total_size)
        self.assertTrue(card_transfer.cleanup_staged_export(export_id))

        reimported_path = self.tmp / "reimported.charx"
        reimported_path.write_bytes(bytes(downloaded))
        cid3 = tavern_importer.import_charx(str(reimported_path))
        self.assertEqual(len(companion_db.get_companion_assets(cid3)), 4)

    def test_import_charx_sets_card_format_distinct_from_standard_cards(self):
        charx_path = _make_sample_charx(self.tmp / "rin_fmt.charx")
        cid_charx = tavern_importer.import_charx(str(charx_path))

        json_path = self.tmp / "plain.json"
        json_path.write_text(json.dumps({"name": "Plain", "description": "text"}), encoding="utf-8")
        cid_json = tavern_importer.import_json(str(json_path))
        companion_db.create_companion("c_manual", {"name": "Manual"})

        by_id = {c["id"]: c for c in companion_db.get_all_companions()}
        self.assertEqual(by_id[cid_charx]["card_format"], "charx")
        self.assertEqual(by_id[cid_json]["card_format"], "json")
        self.assertEqual(by_id["c_manual"]["card_format"], "")

    def test_svg_uploads_and_thumbs_are_sandboxed_and_servable(self):
        from fastapi.testclient import TestClient
        from backend import main as backend_main

        svg_rel = Path("companion_assets") / "c_svg" / "icon" / "0000_badge.svg"
        svg_abs = paths.UPLOAD_ROOT / svg_rel
        svg_abs.parent.mkdir(parents=True, exist_ok=True)
        svg_abs.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")

        client = TestClient(backend_main.app)
        up_res = client.get(f"/uploads/{svg_rel.as_posix()}")
        self.assertEqual(up_res.status_code, 200)
        self.assertEqual(up_res.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("sandbox", up_res.headers.get("content-security-policy", ""))
        self.assertIn("attachment", up_res.headers.get("content-disposition", ""))

        th_res = client.get(f"/thumbs/uploads/{svg_rel.as_posix()}?w=256")
        self.assertEqual(th_res.status_code, 200)
        self.assertTrue(th_res.headers.get("content-type", "").startswith("image/svg+xml"))
        self.assertIn("sandbox", th_res.headers.get("content-security-policy", ""))
        self.assertEqual(th_res.headers.get("x-content-type-options"), "nosniff")

    def test_charx_import_wires_character_book_with_media_only_active_and_api_endpoints(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend import api_routes, companion

        card = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Evelyn",
                "assets": [
                    {"type": "icon", "name": "main", "uri": "embeded://assets/icon/main.png", "ext": "png"},
                    {"type": "image", "name": "evelyn_shy", "uri": "embeded://assets/other/evelyn_shy.webp", "ext": "webp"},
                ],
                "extensions": {
                    "risuai": {
                        "customScripts": [
                            {"type": "editdisplay", "in": '<pimg="(.+?)">', "out": '<img src="{{raw::$1}}">'}
                        ]
                    }
                },
                "character_book": {
                    "entries": [
                        {
                            "comment": "Asset Output Protocol",
                            "keys": [],
                            "content": 'Output <pimg="evelyn_shy"> on its own line.',
                            "constant": True,
                            "enabled": True,
                        },
                        {
                            "comment": "Shop Economy",
                            "keys": [],
                            "content": "Shop prices: copper coin 10.",
                            "constant": True,
                            "enabled": True,
                        },
                    ]
                },
            },
        }
        charx_path = self.tmp / "evelyn.charx"
        with zipfile.ZipFile(charx_path, "w") as zf:
            zf.writestr("card.json", json.dumps(card, ensure_ascii=False).encode("utf-8"))
            zf.writestr("assets/icon/main.png", b"\x89PNG\r\n\x1a\nMAIN")
            zf.writestr("assets/other/evelyn_shy.webp", b"RIFF\x00\x00\x00\x00WEBPSHY")

        cid = tavern_importer.import_charx(str(charx_path))
        prompt = companion.Companion(cid).build_system_prompt(history=[])
        self.assertIn('<pimg="evelyn_shy">', prompt)
        self.assertNotIn("copper coin", prompt)

        app = FastAPI()
        app.include_router(api_routes.router, prefix="/api")
        client = TestClient(app)
        info_res = client.get(f"/api/companions/{cid}/world-books")
        self.assertEqual(info_res.status_code, 200)
        payload = info_res.json()
        self.assertEqual(payload["card_lore_count"], 2)
        self.assertEqual(len(payload["world_books"]), 1)
        book_id = payload["world_books"][0]["id"]

        # Test bulk toggle + frontend-style entry update payload
        entries = world_book_db.get_entries(book_id)
        rpg = next(e for e in entries if e["comment"] == "Shop Economy")
        upd_res = client.put(
            f"/api/companions/world-books/{book_id}/entries/{rpg['id']}",
            json={
                "name": "Shop Economy",
                "comment": "Shop Economy",
                "content": rpg["content"],
                "keys": [],
                "secondary_keys": [],
                "position": "before_char",
                "depth": 4,
                "probability": 100,
                "constant": True,
                "selective": False,
                "enabled": True,
                "logic": "OR",
                "category": "rpg",
            },
        )
        self.assertEqual(upd_res.status_code, 200)
        self.assertIn("copper coin", companion.Companion(cid).build_system_prompt(history=[]))

        bulk_res = client.post(
            f"/api/companions/world-books/{book_id}/entries/bulk-toggle",
            json={"mode": "media_only"},
        )
        self.assertEqual(bulk_res.status_code, 200)
        prompt_after = companion.Companion(cid).build_system_prompt(history=[])
        self.assertIn('<pimg="evelyn_shy">', prompt_after)
        self.assertNotIn("copper coin", prompt_after)


if __name__ == "__main__":
    unittest.main()

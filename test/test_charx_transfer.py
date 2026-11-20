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


if __name__ == "__main__":
    unittest.main()

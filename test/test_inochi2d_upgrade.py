# -*- coding: utf-8 -*-
"""Tests for Inochi2D Live2D upgrade: depth extraction, skeletal pivots, and config."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # Optional ML/CV dependency absent in core/packaged env
    np = None  # type: ignore[assignment]

from PIL import Image

from test._helpers import ROOT  # noqa: F401 - initializes PEBBLE_DATA_ROOT guard

from backend.Inochi2D import pose_manager, service


class _FakeLayer:
    def __init__(self, name: str, arr: Any, left: int = 0, top: int = 0, visible: bool = True):
        self.name = name
        self._arr = arr
        self.left = left
        self.top = top
        self.visible = visible

    def is_group(self) -> bool:
        return False

    def topil(self) -> Image.Image:
        return Image.fromarray(self._arr)


class _FakeGroup:
    def __init__(self, name: str, children):
        self.name = name
        self._children = children
        self.visible = True

    def is_group(self) -> bool:
        return True

    def __iter__(self):
        return iter(self._children)


class _FakePSD:
    def __init__(self, layers, width: int, height: int):
        self._layers = layers
        self.width = width
        self.height = height

    def __iter__(self):
        return iter(self._layers)


class TestInochi2DUpgrade(unittest.TestCase):
    def test_default_config_contains_upgraded_fields(self):
        self.assertIn("depthRelief", service.DEFAULT_PUPPET_CONFIG)
        self.assertIn("limbMotion", service.DEFAULT_PUPPET_CONFIG)
        self.assertIn("autoChatSync", service.DEFAULT_PUPPET_CONFIG)
        self.assertEqual(service.DEFAULT_PUPPET_CONFIG["depthRelief"], 1.0)
        self.assertEqual(service.DEFAULT_PUPPET_CONFIG["limbMotion"], 1.0)
        self.assertTrue(service.DEFAULT_PUPPET_CONFIG["autoChatSync"])

        saved = service.save_puppet_config(
            "unit_inochi_cfg",
            {"depthRelief": 1.6, "limbMotion": 1.4, "autoChatSync": False},
        )
        loaded = service.load_puppet_config("unit_inochi_cfg")
        self.assertEqual(saved["depthRelief"], 1.6)
        self.assertEqual(loaded["limbMotion"], 1.4)
        self.assertFalse(loaded["autoChatSync"])

    def test_build_puppet_extracts_depth_maps_and_skeletal_pivots(self):
        if np is None:
            self.skipTest("numpy not installed")
        from backend.Inochi2D import puppet_builder

        with tempfile.TemporaryDirectory(prefix="puppet-up-") as tmp:
            tmp_path = Path(tmp)
            psd_path = tmp_path / "char.psd"
            depth_psd_path = tmp_path / "char_depth.psd"
            psd_path.write_bytes(b"stub")
            depth_psd_path.write_bytes(b"stub")

            w, h = 200, 300
            face = np.zeros((h, w, 4), dtype=np.uint8)
            face[30:90, 70:130] = [255, 220, 200, 255]

            torso = np.zeros((h, w, 4), dtype=np.uint8)
            torso[100:200, 60:140] = [80, 110, 200, 255]

            arm_l = np.zeros((h, w, 4), dtype=np.uint8)
            arm_l[105:195, 138:165] = [255, 220, 200, 255]

            arm_r = np.zeros((h, w, 4), dtype=np.uint8)
            arm_r[105:195, 35:62] = [255, 220, 200, 255]

            lower = np.zeros((h, w, 4), dtype=np.uint8)
            lower[200:280, 62:138] = [40, 50, 90, 255]

            color_layers = [
                _FakeLayer("face", face),
                _FakeLayer("topwear", torso),
                _FakeLayer("handwear-l", arm_l),
                _FakeLayer("handwear-r", arm_r),
                _FakeLayer("bottomwear", lower),
            ]
            depth_layers = [
                _FakeLayer("face", np.full((h, w), 64, dtype=np.uint8)),
                _FakeLayer("topwear", np.full((h, w), 120, dtype=np.uint8)),
                _FakeLayer("handwear-l", np.full((h, w), 90, dtype=np.uint8)),
                _FakeLayer("handwear-r", np.full((h, w), 95, dtype=np.uint8)),
            ]

            fake_psd_mod = types.ModuleType("psd_tools")

            class _FakePSDImage:
                @staticmethod
                def open(path_str: str):
                    if path_str.endswith("_depth.psd"):
                        return _FakePSD(depth_layers, w, h)
                    return _FakePSD(color_layers, w, h)

            fake_psd_mod.PSDImage = _FakePSDImage

            orig_mod = sys.modules.get("psd_tools")
            sys.modules["psd_tools"] = fake_psd_mod
            try:
                out_dir = tmp_path / "puppet"
                manifest = puppet_builder.build_puppet_from_psd(
                    psd_path=psd_path,
                    out_dir=out_dir,
                    rel_prefix="uploads/inochi2d/test",
                )
            finally:
                if orig_mod is not None:
                    sys.modules["psd_tools"] = orig_mod
                else:
                    sys.modules.pop("psd_tools", None)

            anchors = manifest["anchors"]
            self.assertIn("shoulderL", anchors)
            self.assertIn("shoulderR", anchors)
            self.assertIn("waistCenter", anchors)
            # Right arm is viewer-left (< chestCenter[0]), left arm is viewer-right (> chestCenter[0])
            self.assertLess(anchors["shoulderR"][0], anchors["chestCenter"][0])
            self.assertGreater(anchors["shoulderL"][0], anchors["chestCenter"][0])
            self.assertGreater(anchors["waistCenter"][1], anchors["chestCenter"][1])

            by_tag = {l["tag"]: l for l in manifest["layers"]}
            self.assertIn("depthRelPath", by_tag["face"])
            self.assertIn("depthRelPath", by_tag["handwear-l"])
            self.assertNotIn("depthRelPath", by_tag["bottomwear"])

            face_depth_file = out_dir / "layers" / Path(by_tag["face"]["depthRelPath"]).name
            self.assertTrue(face_depth_file.is_file())
            with Image.open(face_depth_file) as dim:
                self.assertEqual(dim.size, (by_tag["face"]["width"], by_tag["face"]["height"]))
                self.assertEqual(int(np.array(dim).mean()), 64)

    def test_multi_pose_slots_cue_matching_and_rig_update(self):
        with tempfile.TemporaryDirectory(prefix="puppet-pose-") as tmp:
            root = Path(tmp) / "c1"
            root.mkdir(parents=True)
            (root / "puppet.json").write_text(
                json.dumps({
                    "version": 1,
                    "anchors": {
                        "headPivot": [0.5, 0.3],
                        "faceCenter": [0.5, 0.22],
                        "eyeL": [0.56, 0.2],
                        "eyeR": [0.44, 0.2],
                        "mouthCenter": [0.5, 0.27],
                        "mouthSize": [0.06, 0.03],
                        "chestCenter": [0.5, 0.5],
                    },
                    "layers": [
                        {"id": "l1", "tag": "topwear", "rig": "torso", "zIndex": 1, "physics": 0.2, "parallaxX": 0.02, "parallaxY": 0.01},
                        {"id": "l2", "tag": "face", "rig": "face", "zIndex": 2, "physics": 0.0, "parallaxX": 0.08, "parallaxY": 0.05},
                    ],
                }),
                encoding="utf-8",
            )
            side_dir = pose_manager.pose_dir_for(root, "side_view")
            side_dir.mkdir(parents=True)
            (side_dir / "puppet.json").write_text(json.dumps({"version": 1, "anchors": {}, "layers": []}), encoding="utf-8")

            pose_manager.register_pose(
                root,
                pose_id="side_view",
                label="侧身回眸",
                trigger_moods=["shy"],
                trigger_keywords=["转身", "侧身"],
                activate=False,
            )
            idx = pose_manager.list_poses(root)
            self.assertEqual(len(idx["poses"]), 2)
            self.assertEqual(idx["activePoseId"], "default")

            hit = pose_manager.match_pose_for_cue(root, text="她轻轻转身看向窗外", mood="neutral")
            self.assertEqual(hit, "side_view")

            updated = pose_manager.update_puppet_rig(
                root,
                pose_id="default",
                anchors_patch={"headPivot": [0.52, 0.31]},
                layer_patches=[{"id": "l1", "zIndex": 5, "physics": 0.65}],
            )
            self.assertEqual(updated["anchors"]["headPivot"], [0.52, 0.31])
            self.assertEqual(updated["layers"][0]["id"], "l2")
            self.assertEqual(updated["layers"][1]["physics"], 0.65)

            pose_manager.delete_pose(root, "side_view")
            self.assertEqual(len(pose_manager.list_poses(root)["poses"]), 1)
            with self.assertRaises(ValueError):
                pose_manager.delete_pose(root, "default")

    def test_direct_psd_multi_subject_and_inx_import(self):
        if np is None:
            self.skipTest("numpy not installed")
        from backend.Inochi2D import inx_importer, psd_importer

        with tempfile.TemporaryDirectory(prefix="puppet-import-") as tmp:
            tmp_path = Path(tmp)
            psd_path = tmp_path / "hug_cg.psd"
            psd_path.write_bytes(b"stub")

            w, h = 300, 300
            a_face = np.zeros((h, w, 4), dtype=np.uint8)
            a_face[30:90, 50:110] = [255, 220, 200, 255]
            a_body = np.zeros((h, w, 4), dtype=np.uint8)
            a_body[95:220, 40:120] = [90, 120, 200, 255]

            b_face = np.zeros((h, w, 4), dtype=np.uint8)
            b_face[35:95, 180:240] = [255, 210, 190, 255]
            b_body = np.zeros((h, w, 4), dtype=np.uint8)
            b_body[100:225, 170:250] = [200, 90, 120, 255]

            groups = [
                _FakeGroup("角色A", [_FakeLayer("脸", a_face), _FakeLayer("上衣", a_body)]),
                _FakeGroup("角色B", [_FakeLayer("face", b_face), _FakeLayer("topwear", b_body)]),
            ]
            fake_psd_mod = types.ModuleType("psd_tools")
            fake_psd_mod.PSDImage = type("PSDImg", (), {"open": staticmethod(lambda _p: _FakePSD(groups, w, h))})

            sys.path.insert(0, str(ROOT / "backend" / "Inochi2D"))
            orig_mod = sys.modules.get("psd_tools")
            sys.modules["psd_tools"] = fake_psd_mod
            try:
                manifest = psd_importer.import_layered_psd(psd_path, tmp_path / "psd_out", "uploads/inochi2d/t")
            finally:
                if orig_mod is not None:
                    sys.modules["psd_tools"] = orig_mod
                else:
                    sys.modules.pop("psd_tools", None)

            self.assertEqual(manifest["format"], "psd_direct")
            self.assertEqual(len(manifest["subjects"]), 2)
            self.assertLess(
                manifest["subjects"][0]["anchors"]["faceCenter"][0],
                manifest["subjects"][1]["anchors"]["faceCenter"][0],
            )

            # Test open-source Inochi2D .inx binary import
            import io
            import struct

            tex_im = Image.new("RGBA", (64, 64), (240, 180, 160, 255))
            buf = io.BytesIO()
            tex_im.save(buf, format="PNG")
            tex_bytes = buf.getvalue()

            inx_json = json.dumps({
                "meta": {"name": "InxHero"},
                "nodes": {
                    "type": "Node",
                    "name": "Root",
                    "children": [
                        {
                            "type": "Part",
                            "name": "face",
                            "zsort": 1.0,
                            "textures": [0],
                            "transform": {"trans": [0, 0, 0]},
                            "mesh": {
                                "verts": [-32, -32, 32, -32, 32, 32, -32, 32],
                                "uvs": [0, 0, 1, 0, 1, 1, 0, 1],
                                "indices": [0, 1, 2, 0, 2, 3],
                                "origin": [0, 0],
                            },
                        }
                    ],
                },
            }).encode("utf-8")

            blob = (
                b"TRNSRTS\x00"
                + struct.pack(">I", len(inx_json))
                + inx_json
                + b"TEX_SECT"
                + struct.pack(">I", 1)
                + struct.pack(">IB", len(tex_bytes), 0)
                + tex_bytes
            )
            inx_path = tmp_path / "hero.inx"
            inx_path.write_bytes(blob)
            inx_manifest = inx_importer.import_inx_file(inx_path, tmp_path / "inx_out", "uploads/inochi2d/inx")
            self.assertEqual(inx_manifest["format"], "inochi2d_inx")
            self.assertEqual(len(inx_manifest["layers"]), 1)
            self.assertEqual(inx_manifest["layers"][0]["rig"], "face")

    def test_layer_repair_recovers_missing_shoulders_prunes_ghost_and_fixes_z_order(self):
        if np is None:
            self.skipTest("numpy not installed")
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("cv2 not installed")
        from backend.Inochi2D import layer_repair

        with tempfile.TemporaryDirectory(prefix="puppet-repair-") as tmp:
            tmp_path = Path(tmp)
            layers_dir = tmp_path / "layers"
            layers_dir.mkdir(parents=True, exist_ok=True)
            w, h = 256, 256

            # Build synthetic source image with dark background, colorful torso, dark high-contrast leather shoulder,
            # black bottomwear V-strap over skin thighs, and bangs over eyelashes.
            src = np.zeros((h, w, 4), dtype=np.uint8)
            src[:, :, 3] = 255
            # Purple front hair bangs
            src[30:90, 90:166, :3] = [100, 70, 150]
            # Torso suit (white/lavender)
            src[95:165, 95:160, :3] = [215, 200, 225]
            # High-detail dark leather shoulder gap next to torso with crisp ink border & shiny highlights
            src[95:145, 160:205, :3] = [45, 42, 58]
            src[100:140:4, 165:200, :3] = [180, 180, 210]  # leather highlight creases
            src[95:98, 160:205, :3] = [245, 245, 255]      # crisp outer ink/edge border
            src[95:145, 202:205, :3] = [245, 245, 255]
            src[142:145, 160:205, :3] = [245, 245, 255]
            # Thighs (legwear) + overlying black bodysuit bottomwear
            src[160:230, 80:176, :3] = [240, 200, 180]
            src[162:215, 110:146, :3] = [25, 25, 30]  # bottomwear sits ON TOP of inner thighs

            src_path = tmp_path / "source.png"
            Image.fromarray(src, mode="RGBA").save(src_path)

            # Decomposed layers with deliberate bugs mirroring climax pose:
            # 1) bottomwear buried at z=0 behind legwear at z=4
            bottom = np.zeros((h, w, 4), dtype=np.uint8)
            bottom[162:215, 110:146] = [25, 25, 30, 255]
            # 2) legwear AI-inpainted peach across center overlap
            legwear = np.zeros((h, w, 4), dtype=np.uint8)
            legwear[160:230, 80:176] = [240, 200, 180, 255]
            # 3) topwear missing the right leather shoulder (160:205)
            topwear = np.zeros((h, w, 4), dtype=np.uint8)
            topwear[95:165, 95:160] = [215, 200, 225, 255]
            # 4) hallucinated headwear blob matching <5% of source
            headwear = np.zeros((h, w, 4), dtype=np.uint8)
            headwear[20:70, 80:140] = [250, 10, 10, 255]
            # 5) inverted eyewhite > front_hair > eyelash
            eyelash = np.zeros((h, w, 4), dtype=np.uint8)
            eyelash[60:68, 105:130] = [20, 20, 20, 255]
            front_hair = np.zeros((h, w, 4), dtype=np.uint8)
            front_hair[30:90, 90:166] = [100, 70, 150, 255]
            eyewhite = np.zeros((h, w, 4), dtype=np.uint8)
            eyewhite[60:68, 105:130] = [250, 250, 250, 255]

            layers_spec = [
                ("layer_0_bottomwear", "bottomwear", "lower", 0, bottom, "00_bottomwear.png"),
                ("layer_1_headwear", "headwear", "hair_front", 1, headwear, "01_headwear.png"),
                ("layer_2_topwear", "topwear", "torso", 2, topwear, "02_topwear.png"),
                ("layer_3_legwear", "legwear", "lower", 3, legwear, "03_legwear.png"),
                ("layer_4_eyelash_l", "eyelash-l", "eye_l", 4, eyelash, "04_eyelash_l.png"),
                ("layer_5_front_hair", "front hair", "hair_front", 5, front_hair, "05_front_hair.png"),
                ("layer_6_eyewhite_l", "eyewhite-l", "eye_l", 6, eyewhite, "06_eyewhite_l.png"),
            ]
            manifest_layers = []
            for lid, tag, rig, z, arr, fn in layers_spec:
                Image.fromarray(arr, mode="RGBA").save(layers_dir / fn)
                manifest_layers.append({
                    "id": lid, "tag": tag, "rig": rig, "zIndex": z,
                    "x": 0, "y": 0, "width": w, "height": h,
                    "relPath": f"uploads/inochi2d/test/layers/{fn}",
                })
            (tmp_path / "puppet.json").write_text(
                json.dumps({
                    "version": 1,
                    "canvas": {"width": w, "height": h},
                    "contentBounds": {"x": 0, "y": 0, "width": w, "height": h},
                    "source": "source.png",
                    "layers": manifest_layers,
                }),
                encoding="utf-8",
            )

            repaired = layer_repair.repair_puppet_dir(tmp_path, rel_prefix="uploads/inochi2d/test")
            by_tag = {l["tag"]: l for l in repaired["layers"]}
            self.assertNotIn("headwear", by_tag)
            self.assertIn("recovered", by_tag)
            self.assertGreater(by_tag["bottomwear"]["zIndex"], by_tag["legwear"]["zIndex"])
            self.assertLess(by_tag["eyewhite-l"]["zIndex"], by_tag["eyelash-l"]["zIndex"])
            self.assertGreater(by_tag["front hair"]["zIndex"], by_tag["eyelash-l"]["zIndex"])
            # Recovered layer must actually cover missing shoulder x=165..198
            rec_img = np.array(Image.open(layers_dir / Path(by_tag["recovered"]["relPath"]).name))
            rx, ry = by_tag["recovered"]["x"], by_tag["recovered"]["y"]
            self.assertGreater(by_tag["recovered"]["width"], 25)
            self.assertLessEqual(rx, 165)
            self.assertGreaterEqual(rx + by_tag["recovered"]["width"], 195)
            self.assertGreater(int((rec_img[..., 3] > 128).sum()), 1000)

    def test_part_lr_split_preserves_detached_third_component(self):
        if np is None:
            self.skipTest("numpy not installed")
        from unittest import mock
        from backend.Inochi2D import puppet_builder

        iu_dir = str(Path(puppet_builder.__file__).resolve().parent / "see-through" / "common")
        if iu_dir not in sys.path:
            sys.path.insert(0, iu_dir)

        stub_ld = types.ModuleType("modules.layerdiffuse.diffusers_kdiffusion_sdxl")
        stub_ld.KDiffusionStableDiffusionXLPipeline = object
        stub_ld.UNetFrameConditionModel = object
        stub_vae = types.ModuleType("modules.layerdiffuse.vae")
        stub_vae.TransparentVAE = object
        stub_l3d = types.ModuleType("modules.layerdiffuse.layerdiff3d")
        stub_l3d.UNetFrameConditionModel = object
        stub_mg = types.ModuleType("modules.marigold")
        stub_mg.MarigoldDepthPipeline = object
        stub_mod = types.ModuleType("modules")
        stub_psd = types.ModuleType("psd_tools")
        stub_psd.PSDImage = object
        stub_tu = types.ModuleType("utils.torch_utils")
        stub_tu.seed_everything = lambda *a, **kw: None
        stub_tcv = types.ModuleType("utils.torchcv")
        stub_tcv.cluster_inpaint_part = lambda **kw: []
        stub_safetensors = types.ModuleType("safetensors.torch")
        stub_safetensors.load_file = lambda *a, **kw: {}
        stub_sf = types.ModuleType("safetensors")
        stub_torch = types.ModuleType("torch")
        stub_cv = types.ModuleType("utils.cv")
        for _fn in ("center_square_pad_resize", "img_alpha_blending", "smart_resize", "validate_resolution"):
            setattr(stub_cv, _fn, lambda *a, **kw: None)
        stub_io = types.ModuleType("utils.io_utils")
        for _fn in ("json2dict", "dict2json", "load_parts", "save_tmp_img", "load_part", "save_psd"):
            setattr(stub_io, _fn, lambda *a, **kw: None)

        with mock.patch.dict(sys.modules, {
            "modules": stub_mod,
            "modules.layerdiffuse": types.ModuleType("modules.layerdiffuse"),
            "modules.layerdiffuse.diffusers_kdiffusion_sdxl": stub_ld,
            "modules.layerdiffuse.vae": stub_vae,
            "modules.layerdiffuse.layerdiff3d": stub_l3d,
            "modules.marigold": stub_mg,
            "psd_tools": stub_psd,
            "utils.cv": stub_cv,
            "utils.io_utils": stub_io,
            "utils.torch_utils": stub_tu,
            "utils.torchcv": stub_tcv,
            "safetensors": stub_sf,
            "safetensors.torch": stub_safetensors,
            "torch": stub_torch,
        }):
            import importlib
            iu = importlib.import_module("utils.inference_utils")
            iu = importlib.reload(iu)
            part_lr_split = iu.part_lr_split

            h, w = 128, 128
            img = np.zeros((h, w, 4), dtype=np.uint8)
            depth = np.full((h, w), 0.5, dtype=np.float32)
            # Right arm sleeve (x=10..30) + detached right shoulder cuff (x=34..48) + left arm (x=92..114)
            img[40:100, 10:30] = [100, 100, 120, 255]
            img[15:32, 34:48] = [120, 120, 140, 255]
            img[45:95, 92:114] = [100, 100, 120, 255]
            res = part_lr_split("handwear", {
                "img": img,
                "depth": depth,
                "mask": img[..., 3] > 10,
                "xyxy": [0, 0, w, h],
                "tag": "handwear",
            })
            self.assertIn("handwear-r", res)
            self.assertIn("handwear-l", res)
            # Ensure detached shoulder cuff (y=15..32) was kept inside handwear-r instead of dropped
            r_bbox = res["handwear-r"]["xyxy"]
            self.assertLessEqual(r_bbox[1], 16)
            self.assertGreaterEqual(r_bbox[3], 99)

    def test_shutdown_workers_immediately_kills_live_child_process(self):
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            service._PROCS["unit_shutdown_comp"] = proc
            service._JOBS["unit_shutdown_comp"] = {"status": "running", "stage": "layerdiff", "progress": 25, "error": ""}
            stopped = service.shutdown_workers()
            self.assertEqual(stopped, 1)
            self.assertIsNotNone(proc.poll())
            self.assertEqual(service._JOBS["unit_shutdown_comp"]["status"], "idle")
            self.assertEqual(service._JOBS["unit_shutdown_comp"]["stage"], "cancelled")
            self.assertEqual(service.shutdown_workers(), 0)
        finally:
            service._PROCS.pop("unit_shutdown_comp", None)
            service._JOBS.pop("unit_shutdown_comp", None)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_backend_imports_without_numpy_or_cv2(self):
        import subprocess

        probe = (
            "import sys\n"
            "class _Block:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname.split('.')[0] in ('numpy', 'cv2', 'psd_tools', 'torch'):\n"
            "            raise ModuleNotFoundError(f'blocked: {fullname}')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "from backend import main, api_routes, api_routesV2\n"
            "from backend.Inochi2D import service, pose_manager\n"
            "assert service.DEFAULT_PUPPET_CONFIG['autoChatSync'] is True\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)


if __name__ == "__main__":
    unittest.main()

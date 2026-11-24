"""Convert See-through decomposition output (.psd / .psd.json) into a web-ready 2D puppet manifest."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image


# Semantic metadata per See-through tag (covers v3 + tblr_split variants).
_TAG_META: Dict[str, Dict[str, Any]] = {
    "hairb": {"label": "后发", "labelEn": "Back Hair", "category": "hair", "rig": "hair_back", "parallax": (-0.18, -0.10), "physics": 1.0},
    "back hair": {"label": "后发", "labelEn": "Back Hair", "category": "hair", "rig": "hair_back", "parallax": (-0.18, -0.10), "physics": 1.0},
    "hairf": {"label": "前发", "labelEn": "Front Hair", "category": "hair", "rig": "hair_front", "parallax": (0.20, 0.10), "physics": 0.85},
    "front hair": {"label": "前发", "labelEn": "Front Hair", "category": "hair", "rig": "hair_front", "parallax": (0.20, 0.10), "physics": 0.85},
    "hair": {"label": "头发", "labelEn": "Hair", "category": "hair", "rig": "hair_front", "parallax": (0.14, 0.08), "physics": 0.85},
    "head": {"label": "头部轮廓", "labelEn": "Head Base", "category": "head", "rig": "head", "parallax": (0.04, 0.03), "physics": 0.0},
    "face": {"label": "脸部", "labelEn": "Face", "category": "face", "rig": "face", "parallax": (0.08, 0.05), "physics": 0.0},
    "earl": {"label": "左耳", "labelEn": "Left Ear", "category": "head", "rig": "head", "parallax": (-0.12, 0.02), "physics": 0.0},
    "earr": {"label": "右耳", "labelEn": "Right Ear", "category": "head", "rig": "head", "parallax": (-0.12, 0.02), "physics": 0.0},
    "ears": {"label": "耳朵", "labelEn": "Ears", "category": "head", "rig": "head", "parallax": (-0.12, 0.02), "physics": 0.0},
    "earwear": {"label": "耳饰", "labelEn": "Earwear", "category": "accessory", "rig": "head", "parallax": (-0.10, 0.02), "physics": 0.5},
    "eyewhite_l": {"label": "左眼白", "labelEn": "Left Sclera", "category": "eyes", "rig": "eye_l", "parallax": (0.13, 0.08), "physics": 0.0},
    "eyewhite_r": {"label": "右眼白", "labelEn": "Right Sclera", "category": "eyes", "rig": "eye_r", "parallax": (0.13, 0.08), "physics": 0.0},
    "eyewhite": {"label": "眼白", "labelEn": "Sclera", "category": "eyes", "rig": "eye_l", "parallax": (0.13, 0.08), "physics": 0.0},
    "irides_l": {"label": "左瞳孔", "labelEn": "Left Iris", "category": "eyes", "rig": "iris_l", "parallax": (0.14, 0.09), "physics": 0.0},
    "irides_r": {"label": "右瞳孔", "labelEn": "Right Iris", "category": "eyes", "rig": "iris_r", "parallax": (0.14, 0.09), "physics": 0.0},
    "irides": {"label": "瞳孔", "labelEn": "Iris", "category": "eyes", "rig": "iris_l", "parallax": (0.14, 0.09), "physics": 0.0},
    "eyelash_l": {"label": "左睫毛", "labelEn": "Left Eyelash", "category": "eyes", "rig": "eye_l", "parallax": (0.15, 0.09), "physics": 0.0},
    "eyelash_r": {"label": "右睫毛", "labelEn": "Right Eyelash", "category": "eyes", "rig": "eye_r", "parallax": (0.15, 0.09), "physics": 0.0},
    "eyelash": {"label": "睫毛", "labelEn": "Eyelash", "category": "eyes", "rig": "eye_l", "parallax": (0.15, 0.09), "physics": 0.0},
    "eyebrow_l": {"label": "左眉毛", "labelEn": "Left Brow", "category": "eyes", "rig": "brow_l", "parallax": (0.16, 0.10), "physics": 0.0},
    "eyebrow_r": {"label": "右眉毛", "labelEn": "Right Brow", "category": "eyes", "rig": "brow_r", "parallax": (0.16, 0.10), "physics": 0.0},
    "eyebrow": {"label": "眉毛", "labelEn": "Eyebrow", "category": "eyes", "rig": "brow_l", "parallax": (0.16, 0.10), "physics": 0.0},
    "eyes": {"label": "眼睛", "labelEn": "Eyes", "category": "eyes", "rig": "eye_l", "parallax": (0.14, 0.08), "physics": 0.0},
    "nose": {"label": "鼻子", "labelEn": "Nose", "category": "face", "rig": "face", "parallax": (0.20, 0.10), "physics": 0.0},
    "mouth": {"label": "嘴巴", "labelEn": "Mouth", "category": "mouth", "rig": "mouth", "parallax": (0.16, 0.08), "physics": 0.0},
    "eyewear": {"label": "眼镜", "labelEn": "Eyewear", "category": "accessory", "rig": "face", "parallax": (0.18, 0.10), "physics": 0.0},
    "headwear": {"label": "头饰", "labelEn": "Headwear", "category": "accessory", "rig": "hair_front", "parallax": (0.18, 0.12), "physics": 0.45},
    "neck": {"label": "脖颈", "labelEn": "Neck", "category": "body", "rig": "neck", "parallax": (0.02, 0.01), "physics": 0.0},
    "neckwear": {"label": "颈饰/领结", "labelEn": "Neckwear", "category": "clothes", "rig": "torso", "parallax": (0.05, 0.03), "physics": 0.45},
    "topwear": {"label": "上衣", "labelEn": "Topwear", "category": "clothes", "rig": "torso", "parallax": (0.02, 0.01), "physics": 0.18},
    "handwear_l": {"label": "左手/袖", "labelEn": "Left Arm/Hand", "category": "body", "rig": "arm_l", "parallax": (0.03, 0.02), "physics": 0.35},
    "handwear_r": {"label": "右手/袖", "labelEn": "Right Arm/Hand", "category": "body", "rig": "arm_r", "parallax": (0.03, 0.02), "physics": 0.35},
    "handwear": {"label": "手臂/手饰", "labelEn": "Arms/Handwear", "category": "body", "rig": "arm_l", "parallax": (0.03, 0.02), "physics": 0.30},
    "bottomwear": {"label": "下装/裙裤", "labelEn": "Bottomwear", "category": "clothes", "rig": "lower", "parallax": (0.01, 0.01), "physics": 0.28},
    "legwear": {"label": "腿部/袜子", "labelEn": "Legwear", "category": "clothes", "rig": "lower", "parallax": (0.0, 0.0), "physics": 0.05},
    "footwear": {"label": "鞋子", "labelEn": "Footwear", "category": "clothes", "rig": "lower", "parallax": (0.0, 0.0), "physics": 0.0},
    "tail": {"label": "尾巴", "labelEn": "Tail", "category": "accessory", "rig": "tail", "parallax": (-0.12, -0.06), "physics": 1.15},
    "wings": {"label": "翅膀", "labelEn": "Wings", "category": "accessory", "rig": "wings", "parallax": (-0.15, -0.08), "physics": 0.85},
    "objects": {"label": "手持/饰品", "labelEn": "Objects", "category": "accessory", "rig": "prop", "parallax": (0.04, 0.02), "physics": 0.40},
}


def _layer_meta(tag: str) -> Dict[str, Any]:
    key = tag.lower().strip()
    if key in _TAG_META:
        return _TAG_META[key]
    norm = key.replace("-", "_")
    if norm in _TAG_META:
        return _TAG_META[norm]
    if key in ("eyel", "eyer", "browl", "browr"):
        side = "l" if key.endswith("l") else "r"
        base = "eyewhite" if key.startswith("eye") else "eyebrow"
        return _TAG_META[f"{base}_{side}"]
    for prefix, meta in _TAG_META.items():
        if norm.startswith(prefix):
            return meta
    return {"label": tag, "labelEn": tag, "category": "accessory", "rig": "torso", "parallax": (0.04, 0.02), "physics": 0.25}


def _mask_centroid(rgba: np.ndarray) -> Optional[Tuple[float, float, Tuple[int, int, int, int]]]:
    alpha = rgba[..., 3]
    ys, xs = np.where(alpha > 20)
    if len(xs) == 0:
        return None
    cx = float(xs.mean())
    cy = float(ys.mean())
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return cx, cy, bbox


def _trim_rgba(rgba: np.ndarray, pad: int = 2) -> Optional[Tuple[np.ndarray, int, int, int, int]]:
    alpha = rgba[..., 3]
    ys, xs = np.where(alpha > 6)
    if len(xs) == 0:
        return None
    h, w = rgba.shape[:2]
    x1 = max(0, int(xs.min()) - pad)
    y1 = max(0, int(ys.min()) - pad)
    x2 = min(w, int(xs.max()) + 1 + pad)
    y2 = min(h, int(ys.max()) + 1 + pad)
    return rgba[y1:y2, x1:x2], x1, y1, x2 - x1, y2 - y1


def build_puppet_from_psd(
    psd_path: Path,
    out_dir: Path,
    rel_prefix: str,
    source_image: Optional[Path] = None,
) -> Dict[str, Any]:
    """Extract tight layer PNGs from ``psd_path`` and write ``puppet.json``."""
    from psd_tools import PSDImage

    psd = PSDImage.open(str(psd_path))
    json_meta_path = Path(str(psd_path) + ".json")
    part_meta: Dict[str, Any] = {}
    canvas_w, canvas_h = psd.width, psd.height
    if json_meta_path.is_file():
        meta_doc = json.loads(json_meta_path.read_text(encoding="utf-8"))
        part_meta = meta_doc.get("parts") or {}
        fs = meta_doc.get("frame_size")
        if isinstance(fs, list) and len(fs) == 2:
            canvas_h, canvas_w = int(fs[0]), int(fs[1])

    # Crop out solid padding if source_image is known so canvas matches character aspect cleanly.
    # For consistent alignment with all decomposed layers, keep full normalized canvas coordinates,
    # and store contentBounds so the WebGL stage frames the character tightly without letterbox waste.
    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    for stale_png in layers_dir.glob("*.png"):
        try:
            stale_png.unlink()
        except OSError:
            pass

    raw_layers: List[Dict[str, Any]] = []
    anchors_acc: Dict[str, Tuple[float, float, Tuple[int, int, int, int]]] = {}
    min_x, min_y, max_x, max_y = canvas_w, canvas_h, 0, 0

    for idx, layer in enumerate(psd):
        pil_img = layer.topil()
        if pil_img is None:
            continue
        rgba = np.array(pil_img.convert("RGBA"))
        trimmed = _trim_rgba(rgba, pad=2)
        if trimmed is None:
            continue
        crop, lx, ly, lw, lh = trimmed
        gx = int(layer.left) + lx
        gy = int(layer.top) + ly

        min_x = min(min_x, gx)
        min_y = min(min_y, gy)
        max_x = max(max_x, gx + lw)
        max_y = max(max_y, gy + lh)

        tag = str(layer.name or f"layer_{idx}").strip()
        safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in tag)
        filename = f"{idx:02d}_{safe_name}.png"
        Image.fromarray(crop, mode="RGBA").save(layers_dir / filename)

        cinfo = _mask_centroid(crop)
        if cinfo is not None:
            cx_local, cy_local, lbbox = cinfo
            anchors_acc[tag.lower()] = (
                gx + cx_local,
                gy + cy_local,
                (gx + lbbox[0], gy + lbbox[1], gx + lbbox[2], gy + lbbox[3]),
            )

        pinfo = part_meta.get(tag, {})
        meta = _layer_meta(tag)
        depth_median = float(pinfo.get("depth_median", 0.5))

        raw_layers.append(
            {
                "id": f"layer_{idx}_{safe_name}",
                "tag": tag,
                "label": meta["label"],
                "labelEn": meta["labelEn"],
                "category": meta["category"],
                "rig": meta["rig"],
                "x": gx,
                "y": gy,
                "width": lw,
                "height": lh,
                "zIndex": idx,
                "depth": depth_median,
                "parallaxX": meta["parallax"][0],
                "parallaxY": meta["parallax"][1],
                "physics": meta["physics"],
                "visible": True,
                "relPath": f"{rel_prefix.rstrip('/')}/layers/{filename}",
            }
        )

    if max_x <= min_x or max_y <= min_y:
        min_x, min_y, max_x, max_y = 0, 0, canvas_w, canvas_h

    # Derive normalized anatomical anchors in [0..1] of contentBounds
    cb_w = max(1, max_x - min_x)
    cb_h = max(1, max_y - min_y)

    def _norm_pt(x: float, y: float) -> List[float]:
        return [round((x - min_x) / cb_w, 4), round((y - min_y) / cb_h, 4)]

    def _pick(*names: str) -> Optional[Tuple[float, float, Tuple[int, int, int, int]]]:
        for n in names:
            if n in anchors_acc:
                return anchors_acc[n]
        return None

    face_a = _pick("face", "head")
    mouth_a = _pick("mouth")
    eye_l_a = _pick("irides-l", "irides_l", "eyewhite-l", "eyewhite_l", "eyelash-l", "eyelash_l", "eyel", "eyes")
    eye_r_a = _pick("irides-r", "irides_r", "eyewhite-r", "eyewhite_r", "eyelash-r", "eyelash_r", "eyer", "eyes")
    neck_a = _pick("neck")
    torso_a = _pick("topwear", "neckwear")

    if face_a is not None:
        _, _, fb = face_a
        head_pivot = _norm_pt((fb[0] + fb[2]) * 0.5, fb[3])
        face_center = _norm_pt(face_a[0], face_a[1])
    elif neck_a is not None:
        head_pivot = _norm_pt(neck_a[0], neck_a[1])
        face_center = _norm_pt(neck_a[0], max(min_y, neck_a[1] - cb_h * 0.12))
    else:
        head_pivot = [0.5, 0.34]
        face_center = [0.5, 0.24]

    if mouth_a is not None:
        _, _, mb = mouth_a
        mouth_center = _norm_pt(mouth_a[0], mouth_a[1])
        mouth_size = [round((mb[2] - mb[0]) / cb_w, 4), round((mb[3] - mb[1]) / cb_h, 4)]
    else:
        mouth_center = [face_center[0], min(0.95, face_center[1] + 0.06)]
        mouth_size = [0.06, 0.03]

    eye_l = _norm_pt(eye_l_a[0], eye_l_a[1]) if eye_l_a else [face_center[0] - 0.06, face_center[1] - 0.02]
    eye_r = _norm_pt(eye_r_a[0], eye_r_a[1]) if eye_r_a else [face_center[0] + 0.06, face_center[1] - 0.02]
    chest = _norm_pt(torso_a[0], torso_a[1]) if torso_a else [0.5, min(0.85, head_pivot[1] + 0.16)]

    # Angle of the inter-eye line in content pixel coordinates (radians)
    dx_eye = (eye_l[0] - eye_r[0]) * cb_w
    dy_eye = (eye_l[1] - eye_r[1]) * cb_h
    head_tilt = float(np.arctan2(dy_eye, dx_eye)) if (dx_eye * dx_eye + dy_eye * dy_eye) > 1e-3 else 0.0

    # Normalize each layer's bounding box relative to contentBounds as well
    for item in raw_layers:
        item["nx"] = round((item["x"] - min_x) / cb_w, 5)
        item["ny"] = round((item["y"] - min_y) / cb_h, 5)
        item["nw"] = round(item["width"] / cb_w, 5)
        item["nh"] = round(item["height"] / cb_h, 5)

    manifest = {
        "version": 1,
        "canvas": {"width": canvas_w, "height": canvas_h},
        "contentBounds": {"x": min_x, "y": min_y, "width": cb_w, "height": cb_h},
        "anchors": {
            "headPivot": head_pivot,
            "faceCenter": face_center,
            "eyeL": eye_l,
            "eyeR": eye_r,
            "headTilt": round(head_tilt, 5),
            "mouthCenter": mouth_center,
            "mouthSize": mouth_size,
            "chestCenter": chest,
        },
        "source": str(source_image.name) if source_image else "",
        "layers": raw_layers,
    }
    (out_dir / "puppet.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

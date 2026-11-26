"""Direct multi-layer / multi-subject PSD importer for Inochi2D Live2D puppets.

Bypasses See-through inference so artist-layered PSDs (including side-views, sitting
CGs, custom poses, and multi-character scenes such as two people embracing) can be
imported directly with independent per-subject anchors and preserved draw order.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image


_SEMANTIC_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("blink_l", "blink-l", "闭眼左", "左闭眼"), "blink_l"),
    (("blink_r", "blink-r", "闭眼右", "右闭眼"), "blink_r"),
    (("irides_l", "irides-l", "iris_l", "左瞳", "左眼珠", "瞳孔左"), "irides_l"),
    (("irides_r", "irides-r", "iris_r", "右瞳", "右眼珠", "瞳孔右"), "irides_r"),
    (("irides", "iris", "pupil", "瞳", "眼珠", "虹膜"), "irides"),
    (("eyewhite_l", "eyewhite-l", "sclera_l", "左眼白"), "eyewhite_l"),
    (("eyewhite_r", "eyewhite-r", "sclera_r", "右眼白"), "eyewhite_r"),
    (("eyewhite", "sclera", "眼白"), "eyewhite"),
    (("eyelash_l", "eyelash-l", "eye_l", "eyel", "左眼", "左睫"), "eyelash_l"),
    (("eyelash_r", "eyelash-r", "eye_r", "eyer", "右眼", "右睫"), "eyelash_r"),
    (("eyebrow_l", "eyebrow-l", "brow_l", "browl", "左眉"), "eyebrow_l"),
    (("eyebrow_r", "eyebrow-r", "brow_r", "browr", "右眉"), "eyebrow_r"),
    (("eyebrow", "brow", "眉"), "eyebrow"),
    (("eyes", "eye", "眼"), "eyes"),
    (("mouth", "lips", "嘴", "口", "唇"), "mouth"),
    (("nose", "鼻"), "nose"),
    (("hairb", "back hair", "hair_back", "后发", "背发", "马尾", "双马尾"), "hairb"),
    (("hairf", "front hair", "hair_front", "bangs", "前发", "刘海", "鬓角"), "hairf"),
    (("hair", "发"), "hair"),
    (("earl", "ear_l", "左耳"), "earl"),
    (("earr", "ear_r", "右耳"), "earr"),
    (("ears", "ear", "耳"), "ears"),
    (("face", "skin_face", "脸", "面部"), "face"),
    (("headwear", "hat", "hairpin", "发饰", "头饰", "帽"), "headwear"),
    (("head", "头"), "head"),
    (("neckwear", "tie", "ribbon", "collar", "领", "丝带", "项"), "neckwear"),
    (("neck", "脖", "颈"), "neck"),
    (("handwear_l", "handwear-l", "arm_l", "hand_l", "左手", "左臂", "左袖"), "handwear_l"),
    (("handwear_r", "handwear-r", "arm_r", "hand_r", "右手", "右臂", "右袖"), "handwear_r"),
    (("handwear", "arm", "hand", "sleeve", "手", "臂", "袖"), "handwear"),
    (("bottomwear", "skirt", "pants", "下装", "裙", "裤"), "bottomwear"),
    (("legwear", "stocking", "socks", "leg", "腿", "袜"), "legwear"),
    (("footwear", "shoe", "boot", "鞋", "靴"), "footwear"),
    (("tail", "尾"), "tail"),
    (("wings", "wing", "翅", "羽"), "wings"),
    (("topwear", "torso", "chest", "body", "cloth", "coat", "shirt", "dress", "上衣", "躯干", "身", "胸", "衣"), "topwear"),
    (("bg", "background", "backdrop", "scene", "背景", "场景"), "objects"),
]


def _infer_semantic_tag(raw_name: str) -> str:
    low = (raw_name or "").strip().lower()
    for keys, canonical in _SEMANTIC_RULES:
        for k in keys:
            if k in low:
                return canonical
    return raw_name.strip() or "objects"


def _flatten_psd_layers(psd: Any) -> List[Tuple[str, Any]]:
    """Collect visible leaf layers paired with their top-level group name (for multi-subject)."""
    out: List[Tuple[str, Any]] = []

    def _walk(node: Any, top_group: str) -> None:
        if hasattr(node, "is_group") and node.is_group():
            gname = str(getattr(node, "name", "") or "").strip()
            next_top = top_group or gname
            for child in node:
                _walk(child, next_top)
        else:
            if getattr(node, "visible", True):
                out.append((top_group, node))

    for child in psd:
        _walk(child, "")
    return out


def _compute_anchors_from_acc(
    anchors_acc: Dict[str, Tuple[float, float, Tuple[int, int, int, int]]],
    min_x: int,
    min_y: int,
    cb_w: int,
    cb_h: int,
    fallback_bbox: Optional[Tuple[int, int, int, int]] = None,
) -> Dict[str, Any]:
    def _norm_pt(x: float, y: float) -> List[float]:
        return [round((x - min_x) / cb_w, 4), round((y - min_y) / cb_h, 4)]

    def _pick(*names: str) -> Optional[Tuple[float, float, Tuple[int, int, int, int]]]:
        for n in names:
            if n in anchors_acc:
                return anchors_acc[n]
        return None

    fb_x0, fb_y0, fb_x1, fb_y1 = fallback_bbox or (min_x, min_y, min_x + cb_w, min_y + cb_h)
    sub_cx = (fb_x0 + fb_x1) * 0.5
    sub_w = max(1.0, float(fb_x1 - fb_x0))
    sub_h = max(1.0, float(fb_y1 - fb_y0))

    face_a = _pick("face", "head")
    mouth_a = _pick("mouth")
    eye_l_a = _pick("irides_l", "eyewhite_l", "eyelash_l", "eyes")
    eye_r_a = _pick("irides_r", "eyewhite_r", "eyelash_r", "eyes")
    neck_a = _pick("neck")
    torso_a = _pick("topwear", "neckwear")

    if face_a is not None:
        _, _, fb = face_a
        head_pivot = _norm_pt((fb[0] + fb[2]) * 0.5, fb[3])
        face_center = _norm_pt(face_a[0], face_a[1])
    elif neck_a is not None:
        head_pivot = _norm_pt(neck_a[0], neck_a[1])
        face_center = _norm_pt(neck_a[0], max(min_y, neck_a[1] - sub_h * 0.12))
    else:
        head_pivot = _norm_pt(sub_cx, fb_y0 + sub_h * 0.32)
        face_center = _norm_pt(sub_cx, fb_y0 + sub_h * 0.22)

    if mouth_a is not None:
        _, _, mb = mouth_a
        mouth_center = _norm_pt(mouth_a[0], mouth_a[1])
        mouth_size = [round((mb[2] - mb[0]) / cb_w, 4), round((mb[3] - mb[1]) / cb_h, 4)]
    else:
        mouth_center = [face_center[0], round(min(0.95, face_center[1] + 0.06), 4)]
        mouth_size = [0.06, 0.03]

    eye_l = _norm_pt(eye_l_a[0], eye_l_a[1]) if eye_l_a else [round(face_center[0] + 0.04, 4), round(face_center[1] - 0.02, 4)]
    eye_r = _norm_pt(eye_r_a[0], eye_r_a[1]) if eye_r_a else [round(face_center[0] - 0.04, 4), round(face_center[1] - 0.02, 4)]
    chest = _norm_pt(torso_a[0], torso_a[1]) if torso_a else _norm_pt(sub_cx, fb_y0 + sub_h * 0.52)

    arm_l_a = _pick("handwear_l")
    arm_r_a = _pick("handwear_r")
    lower_a = _pick("bottomwear", "legwear")

    def _shoulder(arm_info: Optional[Tuple[float, float, Tuple[int, int, int, int]]], sign: float) -> List[float]:
        if arm_info is not None:
            _, _, ab = arm_info
            ix = ab[0] + (ab[2] - ab[0]) * (0.22 if sign > 0 else 0.78)
            uy = ab[1] + (ab[3] - ab[1]) * 0.14
            return _norm_pt(ix, uy)
        if torso_a is not None:
            _, _, tb = torso_a
            sw = max(1.0, float(tb[2] - tb[0]))
            return _norm_pt((tb[0] + tb[2]) * 0.5 + sign * sw * 0.34, tb[1] + (tb[3] - tb[1]) * 0.16)
        return _norm_pt(sub_cx + sign * sub_w * 0.24, fb_y0 + sub_h * 0.40)

    shoulder_r = _shoulder(arm_r_a, -1.0)
    shoulder_l = _shoulder(arm_l_a, 1.0)

    if torso_a is not None:
        _, _, tb = torso_a
        wy = float(tb[3]) if lower_a is None else float(tb[3] * 0.5 + lower_a[2][1] * 0.5)
        waist_center = _norm_pt((tb[0] + tb[2]) * 0.5, wy)
    elif lower_a is not None:
        waist_center = _norm_pt((lower_a[2][0] + lower_a[2][2]) * 0.5, float(lower_a[2][1]))
    else:
        waist_center = _norm_pt(sub_cx, fb_y0 + sub_h * 0.72)

    dx_eye = (eye_l[0] - eye_r[0]) * cb_w
    dy_eye = (eye_l[1] - eye_r[1]) * cb_h
    head_tilt = float(np.arctan2(dy_eye, dx_eye)) if (dx_eye * dx_eye + dy_eye * dy_eye) > 1e-3 else 0.0

    return {
        "headPivot": head_pivot,
        "faceCenter": face_center,
        "eyeL": eye_l,
        "eyeR": eye_r,
        "headTilt": round(head_tilt, 5),
        "mouthCenter": mouth_center,
        "mouthSize": mouth_size,
        "chestCenter": chest,
        "shoulderL": shoulder_l,
        "shoulderR": shoulder_r,
        "waistCenter": waist_center,
    }


def import_layered_psd(
    psd_path: Path,
    out_dir: Path,
    rel_prefix: str,
    source_name: str = "",
) -> Dict[str, Any]:
    """Import any artist-layered PSD (single or multi-subject) into ``puppet.json``."""
    from psd_tools import PSDImage
    from puppet_builder import _layer_meta, _mask_centroid, _trim_rgba

    psd = PSDImage.open(str(psd_path))
    canvas_w, canvas_h = max(1, int(psd.width)), max(1, int(psd.height))
    flat_layers = _flatten_psd_layers(psd)

    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    for stale in layers_dir.glob("*.png"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Detect distinct top-level groups that look like characters (ignoring pure background groups)
    group_counts: Dict[str, int] = {}
    for gname, leaf in flat_layers:
        if gname and "bg" not in gname.lower() and "背景" not in gname:
            group_counts[gname] = group_counts.get(gname, 0) + 1
    multi_groups = [g for g, cnt in group_counts.items() if cnt >= 2]
    use_groups = len(multi_groups) >= 2

    raw_layers: List[Dict[str, Any]] = []
    per_subject_acc: Dict[str, Dict[str, Tuple[float, float, Tuple[int, int, int, int]]]] = {}
    per_subject_bounds: Dict[str, List[int]] = {}
    min_x, min_y, max_x, max_y = canvas_w, canvas_h, 0, 0

    for idx, (gname, layer) in enumerate(flat_layers):
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

        raw_name = str(getattr(layer, "name", "") or f"layer_{idx}").strip()
        canonical = _infer_semantic_tag(raw_name)
        meta = _layer_meta(canonical)
        # Preserve background layers cleanly as non-moving objects
        is_bg = any(tok in raw_name.lower() for tok in ("background", "backdrop", "背景", "bg_"))
        rig = "prop" if is_bg else meta["rig"]
        parallax_x, parallax_y = (0.0, 0.0) if is_bg else meta["parallax"]
        physics = 0.0 if is_bg else meta["physics"]

        safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in raw_name)
        filename = f"{idx:02d}_{safe_name}.png"
        Image.fromarray(crop, mode="RGBA").save(layers_dir / filename)

        subject_id = ""
        if use_groups and gname in multi_groups:
            subject_id = f"sub_{multi_groups.index(gname)}"
        elif not use_groups and not is_bg:
            subject_id = "sub_0"

        cinfo = _mask_centroid(crop)
        if cinfo is not None and subject_id:
            cx_l, cy_l, lb = cinfo
            sub_acc = per_subject_acc.setdefault(subject_id, {})
            sub_acc[canonical.lower()] = (
                gx + cx_l,
                gy + cy_l,
                (gx + lb[0], gy + lb[1], gx + lb[2], gy + lb[3]),
            )
            sb = per_subject_bounds.setdefault(subject_id, [gx, gy, gx + lw, gy + lh])
            sb[0] = min(sb[0], gx)
            sb[1] = min(sb[1], gy)
            sb[2] = max(sb[2], gx + lw)
            sb[3] = max(sb[3], gy + lh)

        entry: Dict[str, Any] = {
            "id": f"layer_{idx}_{safe_name}",
            "tag": raw_name,
            "label": raw_name,
            "labelEn": meta["labelEn"] if meta["labelEn"] != canonical else raw_name,
            "category": meta["category"],
            "rig": rig,
            "x": gx,
            "y": gy,
            "width": lw,
            "height": lh,
            "zIndex": idx,
            "depth": 0.5,
            "parallaxX": parallax_x,
            "parallaxY": parallax_y,
            "physics": physics,
            "visible": True,
            "relPath": f"{rel_prefix.rstrip('/')}/layers/{filename}",
        }
        if subject_id:
            entry["subjectId"] = subject_id
        raw_layers.append(entry)

    if max_x <= min_x or max_y <= min_y:
        min_x, min_y, max_x, max_y = 0, 0, canvas_w, canvas_h

    cb_w = max(1, max_x - min_x)
    cb_h = max(1, max_y - min_y)

    for item in raw_layers:
        item["nx"] = round((item["x"] - min_x) / cb_w, 5)
        item["ny"] = round((item["y"] - min_y) / cb_h, 5)
        item["nw"] = round(item["width"] / cb_w, 5)
        item["nh"] = round(item["height"] / cb_h, 5)

    subjects: List[Dict[str, Any]] = []
    if use_groups:
        for gidx, gname in enumerate(multi_groups):
            sid = f"sub_{gidx}"
            acc = per_subject_acc.get(sid, {})
            sb = per_subject_bounds.get(sid)
            sub_anchors = _compute_anchors_from_acc(
                acc, min_x, min_y, cb_w, cb_h, tuple(sb) if sb else None,
            )
            subjects.append({
                "id": sid,
                "label": gname,
                "anchors": sub_anchors,
            })
        primary_anchors = subjects[0]["anchors"] if subjects else _compute_anchors_from_acc({}, min_x, min_y, cb_w, cb_h)
    else:
        acc = per_subject_acc.get("sub_0", {})
        sb = per_subject_bounds.get("sub_0")
        primary_anchors = _compute_anchors_from_acc(
            acc, min_x, min_y, cb_w, cb_h, tuple(sb) if sb else None,
        )
        subjects.append({"id": "sub_0", "label": "主体", "anchors": primary_anchors})

    manifest = {
        "version": 2,
        "format": "psd_direct",
        "canvas": {"width": canvas_w, "height": canvas_h},
        "contentBounds": {"x": min_x, "y": min_y, "width": cb_w, "height": cb_h},
        "anchors": primary_anchors,
        "subjects": subjects,
        "source": source_name or psd_path.name,
        "layers": raw_layers,
    }
    (out_dir / "puppet.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    parser = argparse.ArgumentParser()
    parser.add_argument("--psd", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rel-prefix", required=True)
    parser.add_argument("--source-name", default="")
    ns = parser.parse_args()
    import_layered_psd(ns.psd, ns.out, ns.rel_prefix, ns.source_name)

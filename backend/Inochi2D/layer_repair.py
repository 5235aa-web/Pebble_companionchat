"""Post-decomposition layer verification, z-order correction, and missing-part recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

_OPTIONAL_PRUNE_TAGS = frozenset(
    {"headwear", "eyewear", "earwear", "tail", "wings", "objects", "footwear", "neckwear"}
)
_Z_SWAP_RIGS = frozenset({"lower", "torso"})
_RECOVERED_LAYER_ID = "layer_recovered_torso"


def align_source_to_canvas(
    source_path: Optional[Path],
    canvas_w: int,
    canvas_h: int,
) -> Optional[Tuple[np.ndarray, Tuple[int, int, int, int]]]:
    """Align ``source_path`` onto ``(canvas_w, canvas_h)`` matching center-square padding."""
    if source_path is None or not Path(source_path).is_file():
        return None
    try:
        src = np.array(Image.open(source_path).convert("RGBA"))
    except Exception:
        return None

    sh, sw = src.shape[:2]
    if sh <= 0 or sw <= 0 or canvas_w <= 0 or canvas_h <= 0:
        return None

    sz = max(sh, sw)
    px = (sz - sw) // 2
    py = (sz - sh) // 2
    if sh != sw:
        padded = np.zeros((sz, sz, 4), dtype=np.uint8)
        padded[py : py + sh, px : px + sw] = src
    else:
        padded = src

    interp = cv2.INTER_AREA if sz >= canvas_w else cv2.INTER_LINEAR
    resized = cv2.resize(padded, (canvas_w, canvas_h), interpolation=interp)

    scale_x = canvas_w / float(sz)
    scale_y = canvas_h / float(sz)
    bx = max(0, int(round(px * scale_x)))
    by = max(0, int(round(py * scale_y)))
    bw = min(canvas_w - bx, max(1, int(round(sw * scale_x))))
    bh = min(canvas_h - by, max(1, int(round(sh * scale_y))))
    return resized, (bx, by, bw, bh)


def _eye_order_rank(layer: Dict[str, Any]) -> int:
    tag = str(layer.get("tag") or "").lower()
    if "eyewhite" in tag or tag in ("eyel", "eyer"):
        return 1
    if "irides" in tag or "iris" in tag:
        return 2
    if "eyelash" in tag:
        return 3
    if "eyebrow" in tag or tag in ("browl", "browr"):
        return 4
    return 0


def enforce_eye_z_order(layers_with_rgba: List[Tuple[Dict[str, Any], np.ndarray]]) -> bool:
    """Keep sclera/irises behind eyelashes/eyebrows, and keep eyelashes below front hair bangs."""
    eye_indices = [i for i, (l, _) in enumerate(layers_with_rgba) if _eye_order_rank(l) > 0]
    if len(eye_indices) < 2:
        return False

    front_hair_zs = [
        float(l["zIndex"]) for l, _ in layers_with_rgba if l.get("rig") == "hair_front" and _eye_order_rank(l) == 0
    ]
    z_slots = sorted(float(layers_with_rgba[i][0]["zIndex"]) for i in eye_indices)
    current = [layers_with_rgba[i] for i in eye_indices]
    ordered = sorted(current, key=lambda pair: (_eye_order_rank(pair[0]), float(pair[0]["zIndex"])))
    changed = not all(ordered[k][0]["id"] == current[k][0]["id"] for k in range(len(eye_indices)))

    if changed:
        for k, idx in enumerate(eye_indices):
            meta, arr = ordered[k]
            meta["zIndex"] = z_slots[k]
            layers_with_rgba[idx] = (meta, arr)

    # If eyelashes ended up above front hair bangs while permuting slots, place front hair above eyelashes.
    if front_hair_zs:
        min_fh_z = min(front_hair_zs)
        max_lash_z = max(
            (float(l["zIndex"]) for l, _ in layers_with_rgba if _eye_order_rank(l) in (1, 2, 3)),
            default=-1.0,
        )
        if max_lash_z >= min_fh_z:
            bump = max_lash_z + 0.5
            for l, _ in layers_with_rgba:
                if l.get("rig") == "hair_front" and _eye_order_rank(l) == 0 and float(l["zIndex"]) <= max_lash_z:
                    l["zIndex"] = bump
                    bump += 0.01
                    changed = True

    if changed:
        layers_with_rgba.sort(key=lambda pair: float(pair[0]["zIndex"]))
    return changed


def prune_hallucinated_optional_layers(
    layers_with_rgba: List[Tuple[Dict[str, Any], np.ndarray]],
    src_rgb: np.ndarray,
) -> List[Tuple[Dict[str, Any], np.ndarray]]:
    """Drop optional accessory layers that clearly hallucinate pixels absent from source."""
    kept: List[Tuple[Dict[str, Any], np.ndarray]] = []
    src_f = src_rgb.astype(np.float32)
    for meta, arr in layers_with_rgba:
        tag = str(meta.get("tag") or "").lower().strip()
        mask = arr[..., 3] > 64
        if tag in _OPTIONAL_PRUNE_TAGS and int(mask.sum()) > 200:
            diff = np.abs(arr[..., :3].astype(np.float32) - src_f).mean(axis=2)
            match_frac = float((diff[mask] < 28.0).mean())
            if match_frac < 0.16:
                continue
        kept.append((meta, arr))
    return kept


def fix_source_grounded_z_order(
    layers_with_rgba: List[Tuple[Dict[str, Any], np.ndarray]],
    src_rgb: np.ndarray,
) -> bool:
    """Reorder torso/lower layers when overlapping regions prove lower zIndex matches source better."""
    src_f = src_rgb.astype(np.float32)
    changed = False
    swapped = True
    passes = 0
    while swapped and passes < 4:
        swapped = False
        passes += 1
        n = len(layers_with_rgba)
        for i in range(n):
            for j in range(i + 1, n):
                li, ai = layers_with_rgba[i]
                lj, aj = layers_with_rgba[j]
                if li.get("rig") not in _Z_SWAP_RIGS or lj.get("rig") not in _Z_SWAP_RIGS:
                    continue
                if str(li.get("tag") or "").lower() == "recovered" or str(lj.get("tag") or "").lower() == "recovered":
                    continue
                ov = (ai[..., 3] > 180) & (aj[..., 3] > 180)
                if int(ov.sum()) < 250:
                    continue
                di = np.abs(ai[..., :3].astype(np.float32) - src_f).mean(axis=2)[ov]
                dj = np.abs(aj[..., :3].astype(np.float32) - src_f).mean(axis=2)[ov]
                win_i = float((di + 15.0 < dj).mean())
                win_j = float((dj + 15.0 < di).mean())
                if win_i > win_j + 0.15 and float(di.mean()) + 8.0 < float(dj.mean()):
                    zi, zj = li["zIndex"], lj["zIndex"]
                    li["zIndex"], lj["zIndex"] = zj, zi
                    layers_with_rgba[i], layers_with_rgba[j] = (lj, aj), (li, ai)
                    swapped = True
                    changed = True
    return changed


def recover_missing_foreground_rgba(
    layers_with_rgba: List[Tuple[Dict[str, Any], np.ndarray]],
    src_rgba: np.ndarray,
    src_box: Tuple[int, int, int, int],
) -> Optional[np.ndarray]:
    """Recover missing solid foreground regions (e.g. dark jacket shoulders) via ink-barrier segmentation."""
    ch, cw = src_rgba.shape[:2]
    cb_x, cb_y, cb_w, cb_h = src_box
    if cb_w <= 8 or cb_h <= 8:
        return None

    cb_mask = np.zeros((ch, cw), dtype=bool)
    cb_mask[cb_y : cb_y + cb_h, cb_x : cb_x + cb_w] = True
    src_rgb = src_rgba[..., :3]

    known_fg = np.zeros((ch, cw), dtype=bool)
    upper_body_fg = np.zeros((ch, cw), dtype=bool)
    for l_meta, arr in layers_with_rgba:
        if l_meta.get("id") == _RECOVERED_LAYER_ID:
            continue
        m = arr[..., 3] > 110
        known_fg |= m
        if l_meta.get("rig") in {"torso", "neck", "arm_l", "arm_r"}:
            upper_body_fg |= m

    if not known_fg.any():
        return None

    gx = cv2.Sobel(src_rgb.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(src_rgb.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    grad_raw = np.sqrt((gx * gx + gy * gy).max(axis=2)) / 4.0

    # Ink/garment outline barrier protects dark garments on dark backgrounds from exterior flood fill.
    edge_barrier = (grad_raw > 14.0) & cb_mask
    edge_barrier = cv2.dilate(edge_barrier.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    barrier = known_fg | edge_barrier | (~cb_mask)

    free_space = (~barrier).astype(np.uint8)
    seeds = np.zeros((ch, cw), dtype=bool)
    y0, y1 = cb_y, cb_y + cb_h - 1
    x0, x1 = cb_x, cb_x + cb_w - 1
    seeds[y0 : min(ch, y0 + 4), x0 : x1 + 1] = True
    seeds[y0 : y1 + 1, x0 : min(cw, x0 + 4)] = True
    seeds[y0 : y1 + 1, max(0, x1 - 3) : x1 + 1] = True
    seeds &= free_space > 0

    _, lbl_fs, _, _ = cv2.connectedComponentsWithStats(free_space, connectivity=8)
    bg_labels = set(np.unique(lbl_fs[seeds])) - {0}
    ext_bg = np.isin(lbl_fs, list(bg_labels))

    cand = cb_mask & (~known_fg) & (~ext_bg)
    # Morph opening on `cand` alone strips thin radial comic bristles and wispy halos.
    cand_core = (
        cv2.morphologyEx(
            cand.astype(np.uint8),
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
        )
        > 0
    )

    upper_reach = cv2.dilate(upper_body_fg.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    body_reach = cv2.dilate(known_fg.astype(np.uint8), np.ones((13, 13), np.uint8)) > 0

    recovered = np.zeros((ch, cw), dtype=bool)
    num_c, lbl_c, stats_c, _ = cv2.connectedComponentsWithStats(cand_core.astype(np.uint8), connectivity=8)
    max_rgb = src_rgb.max(axis=2)

    for cid in range(1, num_c):
        area = int(stats_c[cid, cv2.CC_STAT_AREA])
        if area < 450:
            continue
        comp = lbl_c == cid
        if not (comp & body_reach).any():
            continue

        bx = int(stats_c[cid, cv2.CC_STAT_LEFT])
        by = int(stats_c[cid, cv2.CC_STAT_TOP])
        bw = int(stats_c[cid, cv2.CC_STAT_WIDTH])
        bh = int(stats_c[cid, cv2.CC_STAT_HEIGHT])

        interior = cv2.erode(comp.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        if not interior.any():
            interior = comp

        int_val_p50 = float(np.percentile(max_rgb[interior], 50))
        int_grad_p90 = float(np.percentile(grad_raw[interior], 90))
        int_sharp_frac = float((grad_raw[interior] > 28.0).mean())
        touches_upper = bool((comp & upper_reach).any())
        touches_bottom = by + bh >= y1 - 3
        touches_side = (bx <= x0 + 2) or (bx + bw >= x1 - 2)

        # Reject solid black voids (gaps between limb and backdrop)
        if int_val_p50 < 18.0:
            continue
        # Reject smooth fog / smoke clouds
        if int_sharp_frac < 0.25 and int_grad_p90 < 60.0:
            continue
        # Require non-upper isolated blobs (e.g. wispy smoke near hair) to be substantial
        if not touches_upper and area < 1200:
            continue
        # Reject diffuse floor/wall background regions along canvas borders
        if (touches_bottom or touches_side) and not touches_upper:
            continue
        if touches_bottom and int_val_p50 > 150.0 and int_sharp_frac < 0.35:
            continue

        recovered |= comp

    if not recovered.any():
        return None

    rec_full = cv2.dilate(recovered.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    rec_full &= cb_mask
    # Keep a subtle anti-aliased outer edge without pulling surrounding bristles back in
    alpha_f = cv2.GaussianBlur(rec_full.astype(np.float32) * 255.0, (3, 3), 0.7)
    alpha_u8 = np.where(rec_full, 255, np.clip(alpha_f, 0, 255)).astype(np.uint8)

    rec_rgba = np.zeros((ch, cw, 4), dtype=np.uint8)
    rec_rgba[..., :3] = src_rgb
    rec_rgba[..., 3] = alpha_u8
    return rec_rgba


def _trim_rgba_box(rgba: np.ndarray, pad: int = 2) -> Optional[Tuple[np.ndarray, int, int, int, int]]:
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


def repair_inmemory_layers(
    layers_with_rgba: List[Tuple[Dict[str, Any], np.ndarray]],
    source_image: Optional[Path],
    canvas_w: int,
    canvas_h: int,
    layers_dir: Path,
    rel_prefix: str,
) -> List[Tuple[Dict[str, Any], np.ndarray]]:
    """Run all source-grounded repairs on full-canvas layers and persist recovered assets if added."""
    aligned = align_source_to_canvas(source_image, canvas_w, canvas_h)
    if aligned is None:
        enforce_eye_z_order(layers_with_rgba)
        return layers_with_rgba

    src_rgba, src_box = aligned
    src_rgb = src_rgba[..., :3]

    layers_with_rgba = prune_hallucinated_optional_layers(layers_with_rgba, src_rgb)
    enforce_eye_z_order(layers_with_rgba)
    fix_source_grounded_z_order(layers_with_rgba, src_rgb)

    rec_rgba = recover_missing_foreground_rgba(layers_with_rgba, src_rgba, src_box)
    if rec_rgba is not None:
        trimmed = _trim_rgba_box(rec_rgba, pad=2)
        if trimmed is not None:
            crop, gx, gy, lw, lh = trimmed
            fname = "00_recovered_shoulders.png"
            dfname = "00_recovered_shoulders_depth.png"
            Image.fromarray(crop, mode="RGBA").save(layers_dir / fname)
            # Mid-back torso depth so recovered shoulders sit slightly behind head/hair and torso front
            Image.fromarray(np.full((lh, lw), 178, dtype=np.uint8), mode="L").save(layers_dir / dfname)

            min_z = min((float(l["zIndex"]) for l, _ in layers_with_rgba), default=0.0)
            rec_meta: Dict[str, Any] = {
                "id": _RECOVERED_LAYER_ID,
                "tag": "recovered",
                "label": "补全肩部/服饰",
                "labelEn": "Recovered Shoulders",
                "category": "clothes",
                "rig": "torso",
                "x": gx,
                "y": gy,
                "width": lw,
                "height": lh,
                "zIndex": min_z - 1.0,
                "depth": 0.70,
                "parallaxX": 0.02,
                "parallaxY": 0.01,
                "physics": 0.14,
                "visible": True,
                "relPath": f"{rel_prefix.rstrip('/')}/layers/{fname}",
                "depthRelPath": f"{rel_prefix.rstrip('/')}/layers/{dfname}",
            }
            layers_with_rgba.insert(0, (rec_meta, rec_rgba))

    layers_with_rgba.sort(key=lambda pair: float(pair[0]["zIndex"]))
    for new_idx, (meta, _) in enumerate(layers_with_rgba):
        meta["zIndex"] = new_idx
    return layers_with_rgba


def repair_puppet_dir(pose_dir: Path, rel_prefix: str = "") -> Optional[Dict[str, Any]]:
    """Idempotently repair an existing built puppet directory (used to upgrade prior builds cleanly)."""
    manifest_path = pose_dir / "puppet.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cw = int(manifest["canvas"]["width"])
    ch = int(manifest["canvas"]["height"])

    source_image: Optional[Path] = None
    src_name = str(manifest.get("source") or "").strip()
    if src_name and (pose_dir / src_name).is_file():
        source_image = pose_dir / src_name
    else:
        for cand in ("source.webp", "source.png", "source.jpg", "source.jpeg"):
            if (pose_dir / cand).is_file():
                source_image = pose_dir / cand
                break
    if source_image is None:
        return manifest

    layers_dir = pose_dir / "layers"
    if not rel_prefix:
        for l_item in manifest.get("layers", []):
            rp = str(l_item.get("relPath") or "")
            if "/layers/" in rp:
                rel_prefix = rp.split("/layers/", 1)[0]
                break
    if not rel_prefix:
        rel_prefix = "uploads/inochi2d"

    base_layers = [
        dict(l_item) for l_item in manifest.get("layers", [])
        if l_item.get("id") != _RECOVERED_LAYER_ID
    ]
    loaded: List[Tuple[Dict[str, Any], np.ndarray]] = []
    for l_item in sorted(base_layers, key=lambda x: float(x.get("zIndex", 0))):
        fname = Path(str(l_item.get("relPath") or "")).name
        lp = layers_dir / fname
        if not lp.is_file():
            continue
        patch = np.array(Image.open(lp).convert("RGBA"))
        full = np.zeros((ch, cw, 4), dtype=np.uint8)
        lx, ly = int(l_item["x"]), int(l_item["y"])
        ph, pw = patch.shape[:2]
        x1 = max(0, lx)
        y1 = max(0, ly)
        x2 = min(cw, lx + pw)
        y2 = min(ch, ly + ph)
        if x2 > x1 and y2 > y1:
            full[y1:y2, x1:x2] = patch[y1 - ly : y2 - ly, x1 - lx : x2 - lx]
        loaded.append((l_item, full))

    repaired = repair_inmemory_layers(
        loaded,
        source_image=source_image,
        canvas_w=cw,
        canvas_h=ch,
        layers_dir=layers_dir,
        rel_prefix=rel_prefix,
    )

    min_x, min_y, max_x, max_y = cw, ch, 0, 0
    final_layers: List[Dict[str, Any]] = []
    for meta, _ in repaired:
        gx, gy = int(meta["x"]), int(meta["y"])
        lw, lh = int(meta["width"]), int(meta["height"])
        min_x = min(min_x, gx)
        min_y = min(min_y, gy)
        max_x = max(max_x, gx + lw)
        max_y = max(max_y, gy + lh)
        final_layers.append(meta)

    if max_x <= min_x or max_y <= min_y:
        cb = manifest.get("contentBounds") or {"x": 0, "y": 0, "width": cw, "height": ch}
        min_x, min_y = int(cb["x"]), int(cb["y"])
        cb_w, cb_h = max(1, int(cb["width"])), max(1, int(cb["height"]))
    else:
        cb_w = max(1, max_x - min_x)
        cb_h = max(1, max_y - min_y)

    for item in final_layers:
        item["nx"] = round((item["x"] - min_x) / cb_w, 5)
        item["ny"] = round((item["y"] - min_y) / cb_h, 5)
        item["nw"] = round(item["width"] / cb_w, 5)
        item["nh"] = round(item["height"] / cb_h, 5)

    manifest["contentBounds"] = {"x": min_x, "y": min_y, "width": cb_w, "height": cb_h}
    manifest["layers"] = final_layers

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)
    return manifest

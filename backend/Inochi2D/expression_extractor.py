from __future__ import annotations

"""Extract pixel-diff expression overlays and crisp closed-eye blink layers
from a folder of same-pose expression PNGs into a See-through puppet folder.
"""

import argparse
import json
import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2  # type: ignore
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

_SEE_THROUGH_DIR = Path(__file__).resolve().parent / "see-through"
if str(_SEE_THROUGH_DIR) not in sys.path:
    sys.path.insert(0, str(_SEE_THROUGH_DIR))

from common.utils.cv import center_square_pad_resize  # type: ignore  # noqa: E402


# Semantic mapping from common Chinese/English expression stems to canonical UI/emotion keys
_EMOTION_META: Dict[str, Tuple[str, str]] = {
    "正常": ("Normal", "neutral"),
    "高兴": ("Happy", "happy"),
    "开心": ("Happy", "happy"),
    "得意": ("Smug", "happy"),
    "害羞": ("Shy", "shy"),
    "亲密": ("Affectionate", "shy"),
    "惊讶1": ("Surprised 1", "surprised"),
    "惊讶2": ("Surprised 2", "surprised"),
    "惊讶": ("Surprised", "surprised"),
    "生气": ("Angry 1", "angry"),
    "生气2": ("Angry 2", "angry"),
    "发怒": ("Furious", "angry"),
    "为难1": ("Troubled 1", "sad"),
    "为难2": ("Troubled 2", "sad"),
    "悲伤": ("Sad", "sad"),
    "委屈": ("Sad", "sad"),
}

_EN_EMOTION_META: Dict[str, Tuple[str, str, int]] = {
    "normal": ("Normal", "neutral", 0),
    "neutral": ("Normal", "neutral", 0),
    "default": ("Normal", "neutral", 0),
    "idle": ("Normal", "neutral", 0),
    "base": ("Normal", "neutral", 0),
    "happy": ("Happy", "happy", 1),
    "smile": ("Happy", "happy", 1),
    "joy": ("Happy", "happy", 1),
    "cheerful": ("Happy", "happy", 1),
    "glad": ("Happy", "happy", 1),
    "smug": ("Smug", "happy", 2),
    "proud": ("Smug", "happy", 2),
    "grin": ("Smug", "happy", 2),
    "playful": ("Smug", "happy", 2),
    "shy": ("Shy", "shy", 3),
    "blush": ("Shy", "shy", 3),
    "embarrassed": ("Shy", "shy", 3),
    "bashful": ("Shy", "shy", 3),
    "affectionate": ("Affectionate", "shy", 4),
    "love": ("Affectionate", "shy", 4),
    "fond": ("Affectionate", "shy", 4),
    "gentle": ("Affectionate", "shy", 4),
    "surprised": ("Surprised", "surprised", 5),
    "surprise": ("Surprised", "surprised", 5),
    "surprised1": ("Surprised 1", "surprised", 5),
    "surprise1": ("Surprised 1", "surprised", 5),
    "startled": ("Surprised 1", "surprised", 5),
    "surprised2": ("Surprised 2", "surprised", 6),
    "surprise2": ("Surprised 2", "surprised", 6),
    "amazed": ("Surprised 2", "surprised", 6),
    "wideeyed": ("Surprised 2", "surprised", 6),
    "shock": ("Surprised 2", "surprised", 6),
    "shocked": ("Surprised 2", "surprised", 6),
    "angry": ("Angry 1", "angry", 7),
    "angry1": ("Angry 1", "angry", 7),
    "mad": ("Angry 1", "angry", 7),
    "annoyed": ("Angry 1", "angry", 7),
    "angry2": ("Angry 2", "angry", 8),
    "furious": ("Furious", "angry", 9),
    "rage": ("Furious", "angry", 9),
    "disgust": ("Disgusted", "angry", 9),
    "disgusted": ("Disgusted", "angry", 9),
    "troubled": ("Troubled 1", "sad", 10),
    "troubled1": ("Troubled 1", "sad", 10),
    "worried": ("Troubled 1", "sad", 10),
    "anxious": ("Troubled 1", "sad", 10),
    "confused": ("Confused", "sad", 10),
    "troubled2": ("Troubled 2", "sad", 11),
    "sad": ("Sad", "sad", 12),
    "cry": ("Sad", "sad", 12),
    "teary": ("Sad", "sad", 12),
    "upset": ("Sad", "sad", 12),
    "sleep": ("Sleepy", "shy", 13),
    "sleepy": ("Sleepy", "shy", 13),
}

_PREFERRED_ORDER = [
    "正常", "高兴", "得意", "害羞", "亲密",
    "惊讶1", "惊讶2", "生气", "生气2", "发怒", "为难1", "为难2",
]


def _resolve_emotion_meta(stem: str) -> Tuple[str, str, int]:
    """Resolve display label, canonical emotionKey, and sort priority from a filename stem."""
    if stem in _EMOTION_META:
        label_en, emo_key = _EMOTION_META[stem]
        rank = _PREFERRED_ORDER.index(stem) if stem in _PREFERRED_ORDER else 50
        return label_en, emo_key, rank

    norm = re.sub(r"[\s_-]+", "", stem.strip().lower())
    if norm in _EN_EMOTION_META:
        return _EN_EMOTION_META[norm]

    no_digits = re.sub(r"\d+$", "", norm)
    if no_digits in _EN_EMOTION_META:
        base_en, emo_key, rank = _EN_EMOTION_META[no_digits]
        suffix = norm[len(no_digits):]
        label_en = f"{base_en} {suffix}".strip() if suffix else base_en
        return label_en, emo_key, rank

    for cn_key, (label_en, emo_key) in _EMOTION_META.items():
        if cn_key in stem:
            rank = _PREFERRED_ORDER.index(cn_key) if cn_key in _PREFERRED_ORDER else 50
            return label_en, emo_key, rank

    return stem, "", 99


def _dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask
    im = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    return np.array(im.filter(ImageFilter.MaxFilter(radius * 2 + 1))) > 127


def _feather_mask(mask: np.ndarray, radius: float = 1.1) -> np.ndarray:
    im = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    blurred = np.array(im.filter(ImageFilter.GaussianBlur(radius=radius)), dtype=np.float32) / 255.0
    return np.maximum(mask.astype(np.float32) * 0.92, blurred)


def _segment_expression_eye_mask(
    expr_arr: np.ndarray,
    face_under: np.ndarray,
    eyeball_seed: np.ndarray,
    search_box: Tuple[int, int, int, int],
    forbidden: np.ndarray,
) -> np.ndarray:
    """Isolate one expression's eye (lash + sclera + iris + lid crease) using connected
    components against bare skin so lowered angry/normal eyebrows are never clipped.
    """
    ch, cw = expr_arr.shape[:2]
    x0, y0, x1, y1 = max(0, search_box[0]), max(0, search_box[1]), min(cw, search_box[2]), min(ch, search_box[3])
    roi_e = expr_arr[y0:y1, x0:x1]
    roi_f = face_under[y0:y1, x0:x1]
    seed_roi = eyeball_seed[y0:y1, x0:x1]
    forb_roi = forbidden[y0:y1, x0:x1]

    diff_skin = np.abs(roi_e[:, :, :3].astype(np.int16) - roi_f[:, :, :3].astype(np.int16)).max(axis=2)
    lum = roi_e[:, :, :3].mean(axis=2)
    min_rgb = roi_e[:, :, :3].min(axis=2)
    # Connect dark lashes/iris + bright sclera; exclude soft cheek blush & pale skin gradients
    dark_feat = (diff_skin > 24) & (lum < 145)
    sclera_feat = (diff_skin > 18) & (min_rgb > 230)
    features = (dark_feat | sclera_feat) & (~forb_roi)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(features.astype(np.uint8), connectivity=8)
    selected = np.zeros_like(features, dtype=bool)
    brow_components = np.zeros_like(features, dtype=bool)
    eye_cands: List[int] = []

    comp_stats: List[Tuple[int, int, float, int, int, int]] = []
    for cid in range(1, num_labels):
        area = int(stats[cid, cv2.CC_STAT_AREA])
        if area < 4:
            continue
        comp = labels == cid
        ys_c = np.where(comp)[0]
        overlap_seed = int((comp & seed_roi).sum())
        comp_stats.append((cid, area, float(centroids[cid][1]), int(ys_c.min()), int(ys_c.max()), overlap_seed))

    # Pick primary eye core(s) overlapping lower-eyelid seed strongly (or any pure sclera touching seed)
    max_ov = max((ov for _, _, _, _, _, ov in comp_stats), default=0)
    ov_thresh = max(14, int(max_ov * 0.18))
    for cid, area, cy, y_min, y_max, overlap_seed in comp_stats:
        comp = labels == cid
        has_sclera = int((comp & sclera_feat).sum()) >= 8
        if overlap_seed >= ov_thresh or (overlap_seed >= 4 and has_sclera):
            eye_cands.append(cid)
            selected |= comp

    # Attach outer upper eyelash / iris touching the selected sclera halo (e.g. wide-eyed 惊讶1)
    if selected.any():
        halo = _dilate_mask(selected, 3)
        for cid, area, cy, y_min, y_max, overlap_seed in comp_stats:
            if cid in eye_cands:
                continue
            comp = labels == cid
            # Must touch sclera/eye halo AND not extend way above typical upper lid height into brow
            if (comp & halo).any() and (overlap_seed > 0 or area > 40):
                # Do not swallow an eyebrow whose inner tip barely brushed near halo unless it sits below top-most brow
                eye_bot = int(np.where(selected)[0].max())
                if y_max >= eye_bot - 22:
                    eye_cands.append(cid)
                    selected |= comp

    if selected.any():
        eye_top = int(np.where(selected)[0].min())
        for cid, area, cy, y_min, y_max, _ in comp_stats:
            if cid in eye_cands:
                continue
            comp = labels == cid
            if y_max < eye_top - 1:
                brow_components |= comp

    # Re-attach ONLY a tiny double-eyelid crease directly above eye_top if a separate brow sits above it
    if selected.any() and brow_components.any():
        eye_top = int(np.where(selected)[0].min())
        highest_brow_y = int(np.where(brow_components)[0].min())
        for cid, area, cy, y_min, y_max, _ in comp_stats:
            if cid in eye_cands:
                continue
            comp = labels == cid
            if 0 <= eye_top - y_max <= 6 and y_min > highest_brow_y + 8 and area < 180:
                selected |= comp
                brow_components &= ~comp

    # Expand 3px to catch subtle lid-edge antialiasing, strictly blocked from any eyebrow pixels
    out = np.zeros((ch, cw), dtype=bool)
    sel_dil = _dilate_mask(selected, 3) & (~_dilate_mask(brow_components, 2)) & (~forb_roi)
    out[y0:y1, x0:x1] = sel_dil
    return out


def _trim_rgba(rgba: np.ndarray, pad: int = 2) -> Optional[Tuple[np.ndarray, int, int, int, int]]:
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > 4)
    if len(xs) == 0:
        return None
    h, w = rgba.shape[:2]
    x0 = max(0, int(xs.min()) - pad)
    y0 = max(0, int(ys.min()) - pad)
    x1 = min(w, int(xs.max()) + 1 + pad)
    y1 = min(h, int(ys.max()) + 1 + pad)
    return rgba[y0:y1, x0:x1], x0, y0, x1 - x0, y1 - y0


def _complete_occluded_outer_eye_and_brow(
    eye_canvas: np.ndarray,
    rest_canvas: np.ndarray,
    arr: np.ndarray,
    lash_base: np.ndarray,
    white_base: np.ndarray,
    hair_forbid: np.ndarray,
    box_r: Tuple[int, int, int, int],
) -> Tuple[np.ndarray, np.ndarray]:
    """Isolated Plan 1 blind-spot completion: smoothly extend the right eye's
    occluded outer corner (sclera, upper lash wing, lower rim, lid crease) and
    right eyebrow tail underneath 19_front_hair using 4x supersampling so hair
    sway never reveals a blunt cut. Can be disabled via --no-complete-eye.
    """
    ch, cw = eye_canvas.shape[:2]
    bx0, by0, bx1, by1 = max(0, box_r[0]), max(0, box_r[1]), min(cw, box_r[2]), min(ch, box_r[3])

    white_ys, white_xs = np.where(white_base[:, :, 3] > 45)
    lash_ys, lash_xs = np.where(lash_base[:, :, 3] > 45)
    if len(white_xs) < 6 or len(lash_xs) < 6:
        return eye_canvas, rest_canvas

    x_corner = float(white_xs.min())
    w_tip_ys = np.where(white_base[:, int(x_corner), 3] > 45)[0]
    y_corner_base = float(w_tip_ys.mean()) if len(w_tip_ys) else float(np.median(white_ys))
    x_wing_tip = max(float(bx0), min(float(lash_xs.min()), x_corner - 8.0))
    l_tip_ys = np.where(lash_base[:, int(max(0, round(x_wing_tip))), 3] > 45)[0]
    y_wing_tip = float(l_tip_ys.mean()) if len(l_tip_ys) else (y_corner_base - 1.2)

    # Locate the unoccluded reference cross-section just to the right of hair_forbid
    eye_roi_a = eye_canvas[by0:by1, bx0:bx1, 3] > 140
    hair_roi = hair_forbid[by0:by1, bx0:bx1]
    if not eye_roi_a.any() or not hair_roi.any():
        return eye_canvas, rest_canvas

    # Find rows with significant eye content and rightmost hair column within eye height
    ey_rel, ex_rel = np.where(eye_roi_a)
    y_e0 = by0 + int(ey_rel.min())
    y_e1 = by0 + int(ey_rel.max())
    hair_edge_xs: List[int] = []
    for y in range(max(by0, y_e0), min(by1, y_e1 + 1)):
        hx = np.where(hair_forbid[y, bx0:bx1])[0]
        ex = np.where(eye_canvas[y, bx0:bx1, 3] > 140)[0]
        if len(hx) and len(ex):
            hmax = bx0 + int(hx.max())
            emin = bx0 + int(ex.min())
            if emin - hmax <= 4:
                hair_edge_xs.append(hmax)
    if not hair_edge_xs:
        return eye_canvas, rest_canvas

    x_ref = min(bx1 - 3, max(hair_edge_xs) + 2)
    if x_ref <= x_corner + 2:
        return eye_canvas, rest_canvas

    # Inspect vertical eye structure across [x_ref .. x_ref + 2]
    strip_a = eye_canvas[by0:by1, x_ref:x_ref + 3, 3]
    strip_rgb = arr[by0:by1, x_ref:x_ref + 3, :3].astype(np.float32)
    strip_lum = strip_rgb.mean(axis=2)
    strip_min = strip_rgb.min(axis=2)
    dark_rows = np.where((strip_a > 120) & (strip_lum < 120))[0]
    if len(dark_rows) == 0:
        return eye_canvas, rest_canvas

    # Group contiguous dark rows to distinguish upper lash core vs tiny crease/lower rim
    dark_ys = sorted(set(int(by0 + r) for r in dark_rows))
    groups: List[List[int]] = []
    for y in dark_ys:
        if not groups or y - groups[-1][-1] > 2:
            groups.append([y])
        else:
            groups[-1].append(y)
    # Main upper lash is the thickest dark group (or lower dark band above sclera)
    main_group = max(groups, key=len)
    y_lash_top = float(min(main_group))
    y_lash_bot = float(max(main_group))
    t_lash = max(2.0, y_lash_bot - y_lash_top + 1.0)

    lash_px = strip_rgb[(strip_a > 120) & (strip_lum < 110)]
    lash_rgb = tuple(int(v) for v in np.median(lash_px, axis=0)) if len(lash_px) else (44, 22, 18)

    # Find contiguous neutral/cool-white sclera rows below upper lash (includes blue-gray eyeball shadow R-B<=22, rejects warm peach skin R-B~50)
    col_y_arr = np.arange(by0, by1)[:, None]
    is_neutral_white = (strip_min >= 172) & ((strip_rgb[:, :, 0] - strip_rgb[:, :, 2]) < 22)
    sclera_cands = np.where((strip_a > 120) & is_neutral_white & (col_y_arr >= y_lash_bot - 0.5))[0]
    sclera_ys = sorted(set(int(by0 + r) for r in sclera_cands if by0 + r <= int(y_lash_bot) + 22))
    contig_sclera: List[int] = []
    for y in sclera_ys:
        if not contig_sclera:
            if y - int(y_lash_bot) <= 4:
                contig_sclera.append(y)
        elif y - contig_sclera[-1] <= 2:
            contig_sclera.append(y)
        else:
            break

    if contig_sclera:
        y_w_top = float(min(contig_sclera))
        y_w_bot = float(max(contig_sclera)) + 0.4
        scl_idx = [y - by0 for y in contig_sclera]
        scl_px = strip_rgb[scl_idx, :, :][is_neutral_white[scl_idx, :]]
        sclera_rgb = tuple(int(v) for v in np.percentile(scl_px, 80, axis=0)) if len(scl_px) else (244, 238, 238)
    else:
        y_w_top = y_lash_bot + 0.2
        y_w_bot = y_lash_bot + 0.6
        sclera_rgb = (242, 236, 236)

    # Estimate natural lash slope from visible columns [x_ref .. x_ref + 7]
    lash_xs_fit: List[float] = []
    lash_ys_fit: List[float] = []
    for xk in range(x_ref, min(bx1, x_ref + 8)):
        col_l = arr[max(by0, int(y_lash_top) - 4):min(by1, int(y_lash_bot) + 5), xk, :3].mean(axis=1)
        col_m = eye_canvas[max(by0, int(y_lash_top) - 4):min(by1, int(y_lash_bot) + 5), xk, 3] > 120
        hits = np.where(col_m & (col_l < 120))[0]
        if len(hits):
            lash_xs_fit.append(float(xk))
            lash_ys_fit.append(float(max(by0, int(y_lash_top) - 4)) + float(hits.min()))
    lash_slope = float(np.polyfit(lash_xs_fit, lash_ys_fit, 1)[0]) if len(lash_xs_fit) >= 3 else -0.25
    lash_slope = max(-0.45, min(0.15, lash_slope))

    # Align outer corner cleanly to this expression's actual sclera bottom + subtle upward sweep
    open_h = max(0.0, y_w_bot - y_w_top)
    if open_h > 8.0:
        # Wide-eyed (惊讶1): anchor smoothly to base white corner & shade upper sclera slightly
        wb_at_ref = np.where(white_base[:, int(round(x_ref)), 3] > 45)[0]
        y_base_bot = float(wb_at_ref.max()) if len(wb_at_ref) else y_w_bot
        y_corner = y_corner_base + (y_w_bot - y_base_bot) * 0.7
        scl_bright = strip_rgb[scl_idx, :, :][(strip_min[scl_idx, :] > 225)]
        if len(scl_bright):
            sclera_rgb = tuple(int(v) for v in np.median(scl_bright, axis=0))
    else:
        # Squinted / normal eyes: outer corner sweeps gently up-left from actual lower rim y_w_bot
        y_corner = y_w_bot - 1.2 + lash_slope * 2.5
    x_wing_tip = max(float(bx0), x_corner - 5.5)
    y_wing = y_corner - 1.1 + lash_slope * 1.4

    # Optional eyelid crease line 4..14px above upper lash
    crease_y: Optional[float] = None
    crease_rgb = (165, 118, 98)
    for y_c in range(max(by0, int(y_lash_top) - 14), max(by0, int(y_lash_top) - 3)):
        r = y_c - by0
        if (strip_a[r, :] > 100).any() and strip_lum[r, :].mean() < 218:
            crease_y = float(y_c)
            cpx = strip_rgb[r, strip_a[r, :] > 100]
            if len(cpx):
                med = np.median(cpx, axis=0)
                crease_rgb = (int(med[0]), int(med[1]), int(med[2]))
            break

    # Build 4x supersampled outer corner patch spanning [x_wing_tip-4 .. x_ref+3] (overlapping 2px into real eye for zero seam)
    x_anchor = float(x_ref + 2)
    x0_p = max(0, int(math.floor(x_wing_tip)) - 4)
    x1_p = min(cw, int(math.ceil(x_anchor)) + 2)
    y0_p = max(0, int(math.floor(min(y_lash_top, y_wing) - 16)))
    y1_p = min(ch, int(math.ceil(max(y_w_bot, y_corner) + 7)))
    pw, ph = x1_p - x0_p, y1_p - y0_p
    if pw <= 2 or ph <= 2:
        return eye_canvas, rest_canvas

    ss = 4
    hi = Image.new("RGBA", (pw * ss, ph * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(hi)

    def _to_hi(px: float, py: float) -> Tuple[float, float]:
        return ((px - x0_p) * ss, (py - y0_p) * ss)

    # 1. Sclera wedge (convex lower rim so outer corner curves smoothly instead of spiking)
    if y_w_bot - y_w_top > 1.2:
        poly: List[Tuple[float, float]] = []
        steps = 14
        for i in range(steps + 1):
            t = i / steps
            px = x_anchor + (x_corner - x_anchor) * t
            py = (y_w_top - 0.4) + (y_corner - 0.3 - (y_w_top - 0.4)) * (t ** 0.85)
            poly.append(_to_hi(px, py))
        for i in range(steps, -1, -1):
            t = i / steps
            px = x_anchor + (x_corner - x_anchor) * t
            py = y_w_bot + (y_corner - y_w_bot) * (t ** 1.75)
            poly.append(_to_hi(px, py))
        draw.polygon(poly, fill=(sclera_rgb[0], sclera_rgb[1], sclera_rgb[2], 255))

    # 2. Faint lower rim line following rounded corner
    rim_pts: List[Tuple[float, float]] = []
    for i in range(13):
        t = i / 12
        px = x_anchor + (x_corner - x_anchor) * t
        py = (y_w_bot + 0.2) + (y_corner - (y_w_bot + 0.2)) * (t ** 1.75)
        rim_pts.append(_to_hi(px, py))
    rim_rgb = (min(255, lash_rgb[0] + 55), min(255, lash_rgb[1] + 45), min(255, lash_rgb[2] + 40))
    for i in range(len(rim_pts) - 1):
        t = i / (len(rim_pts) - 1)
        alpha = int(round(125 * (1.0 - 0.6 * t)))
        draw.line([rim_pts[i], rim_pts[i + 1]], fill=(*rim_rgb, alpha), width=max(2, int(round(0.9 * ss))))

    # 3. Upper lash polygon tapering smoothly into gentle outer wing
    lash_pts_top: List[Tuple[float, float]] = []
    lash_pts_bot: List[Tuple[float, float]] = []
    n_lash = 16
    for i in range(n_lash + 1):
        t = i / n_lash
        if t <= 0.72:
            u = t / 0.72
            px = x_anchor + (x_corner - x_anchor) * u
            yc = y_lash_top + (y_corner - 1.2 - y_lash_top) * (u ** 0.90)
            thick = t_lash + (1.45 - t_lash) * (u ** 0.85)
        else:
            u = (t - 0.72) / 0.28
            px = x_corner + (x_wing_tip - x_corner) * u
            yc = (y_corner - 1.2) + (y_wing - (y_corner - 1.2)) * (u ** 1.15)
            thick = 1.45 * (1.0 - u * 0.85)
        lash_pts_top.append(_to_hi(px, yc))
        lash_pts_bot.append(_to_hi(px, yc + thick))
    draw.polygon(lash_pts_top + list(reversed(lash_pts_bot)), fill=(*lash_rgb, 255))

    # 4. Optional faint crease taper overlapping seamlessly from x_anchor
    if crease_y is not None:
        cr_len = max(5.0, min(9.0, (x_anchor - x_corner) * 0.65))
        for i in range(8):
            t0, t1 = i / 8, (i + 1) / 8
            p0 = _to_hi(x_anchor - cr_len * t0, crease_y + 1.2 * (t0 ** 1.1))
            p1 = _to_hi(x_anchor - cr_len * t1, crease_y + 1.2 * (t1 ** 1.1))
            al = int(round(160 * (1.0 - t1)))
            draw.line([p0, p1], fill=(*crease_rgb, al), width=max(2, int(round(0.85 * ss))))

    synth = np.array(hi.resize((pw, ph), resample=Image.Resampling.LANCZOS), dtype=np.uint8)
    out_eye = eye_canvas.copy()
    roi_dst = out_eye[y0_p:y1_p, x0_p:x1_p].astype(np.float32)
    # Composite synth OVER peach-skin boundary / attenuated hair fringe, while letting real opaque features stay on top
    hair_zone_roi = _dilate_mask(hair_forbid, 3)[y0_p:y1_p, x0_p:x1_p]
    real_eye_lum = roi_dst[:, :, :3].mean(axis=2)
    is_real_feature = (~hair_zone_roi) & (roi_dst[:, :, 3] > 215) & (
        (real_eye_lum < 145) | (roi_dst[:, :, :3].min(axis=2) > 210)
    )
    dst_a = roi_dst[:, :, 3:4] / 255.0
    src_a = synth[:, :, 3:4].astype(np.float32) / 255.0
    # Where real feature sits, existing over synth; along hair/skin cut margin, synth over existing so zero seam slit
    over_rgb = synth[:, :, :3].astype(np.float32) * src_a + roi_dst[:, :, :3] * dst_a * (1.0 - src_a)
    over_a = np.clip(synth[:, :, 3:4].astype(np.float32) + roi_dst[:, :, 3:4] * (1.0 - src_a), 0, 255)
    under_rgb = roi_dst[:, :, :3] * dst_a + synth[:, :, :3].astype(np.float32) * src_a * (1.0 - dst_a)
    under_a = np.clip(roi_dst[:, :, 3:4] + synth[:, :, 3:4].astype(np.float32) * (1.0 - dst_a), 0, 255)
    use_under = is_real_feature[:, :, None]
    comp_rgb = np.where(use_under, under_rgb, over_rgb)
    comp_a = np.where(use_under, under_a, over_a)
    roi_dst[:, :, :3] = np.where(comp_a > 1, comp_rgb / np.maximum(1e-4, comp_a / 255.0), 0)
    roi_dst[:, :, 3:4] = comp_a
    out_eye[y0_p:y1_p, x0_p:x1_p] = np.clip(roi_dst, 0, 255).astype(np.uint8)

    # 5. Extend occluded right eyebrow tail cleanly with full overlap across hair cut seam
    out_rest = rest_canvas.copy()
    brow_y0 = max(0, by0 - 16)
    brow_y1 = max(brow_y0 + 1, int(y_lash_top) - 4)
    brow_roi_a = rest_canvas[brow_y0:brow_y1, bx0:bx1, 3] > 95
    brow_roi_lum = arr[brow_y0:brow_y1, bx0:bx1, :3].mean(axis=2)
    brow_dark = brow_roi_a & (brow_roi_lum < 140)
    by_rel, bx_rel = np.where(brow_dark)
    if len(bx_rel) >= 6:
        xb_min = bx0 + int(bx_rel.min())
        y_at_tip = brow_y0 + float(np.median(by_rel[bx_rel <= bx_rel.min() + 1]))
        hx_brow = np.where(hair_forbid[max(0, int(y_at_tip) - 2):min(ch, int(y_at_tip) + 3), max(0, xb_min - 5):xb_min + 2])[1]
        if len(hx_brow) > 0:
            xs_fit: List[float] = []
            ys_fit: List[float] = []
            ws_fit: List[float] = []
            # Skip attenuated outermost 1px, fit weighted center across interior [xb_min+2 .. xb_min+10]
            for x in range(xb_min + 2, min(bx1, xb_min + 11)):
                cols = np.where(brow_dark[:, x - bx0])[0]
                if len(cols):
                    wts = np.maximum(1.0, 160.0 - brow_roi_lum[cols, x - bx0])
                    yc_val = float(brow_y0) + float((cols * wts).sum() / wts.sum())
                    xs_fit.append(float(x))
                    ys_fit.append(yc_val)
                    ws_fit.append(float(cols.max() - cols.min() + 1))
            if len(xs_fit) >= 3:
                poly_b = np.polyfit(xs_fit, ys_fit, 1)
                slope = max(-0.85, min(0.85, float(poly_b[0])))
                xb_anchor = xs_fit[0] + 2.5
                y_start = float(np.polyval(poly_b, xb_anchor))
                r_start = max(0.85, float(np.median(ws_fit[:3])) * 0.42)
                bpx = arr[brow_y0:brow_y1, bx0:bx1, :3][brow_dark]
                brow_rgb = tuple(int(v) for v in np.median(bpx, axis=0))
                tail_len = 8.0
                rx0 = max(0, int(math.floor(xb_min - tail_len - 3)))
                rx1 = min(cw, int(math.ceil(xb_anchor)) + 2)
                ry0 = max(0, int(math.floor(min(y_start, y_start - slope * (tail_len + 4)) - 5)))
                ry1 = min(ch, int(math.ceil(max(y_start, y_start - slope * (tail_len + 4)) + 5)))
                rw, rh = rx1 - rx0, ry1 - ry0
                if rw > 2 and rh > 2:
                    bhi = Image.new("RGBA", (rw * ss, rh * ss), (0, 0, 0, 0))
                    bdraw = ImageDraw.Draw(bhi)
                    n_b = 10
                    top_p: List[Tuple[float, float]] = []
                    bot_p: List[Tuple[float, float]] = []
                    x_tip = xb_min - tail_len
                    for i in range(n_b + 1):
                        t = i / n_b
                        px = xb_anchor + (x_tip - xb_anchor) * t
                        py = y_start + slope * (px - xb_anchor)
                        half_w = r_start * (1.0 - 0.88 * (t ** 0.95))
                        top_p.append(((px - rx0) * ss, (py - half_w - ry0) * ss))
                        bot_p.append(((px - rx0) * ss, (py + half_w - ry0) * ss))
                    bdraw.polygon(top_p + list(reversed(bot_p)), fill=(*brow_rgb, 245))
                    bsynth = np.array(bhi.resize((rw, rh), resample=Image.Resampling.LANCZOS), dtype=np.uint8)
                    bdst = out_rest[ry0:ry1, rx0:rx1].astype(np.float32)
                    bhair = _dilate_mask(hair_forbid, 5)[ry0:ry1, rx0:rx1]
                    real_brow = (~bhair) & (bdst[:, :, 3] > 215) & (bdst[:, :, :3].mean(axis=2) < 140)
                    da = bdst[:, :, 3:4] / 255.0
                    sa = bsynth[:, :, 3:4].astype(np.float32) / 255.0
                    o_rgb = bsynth[:, :, :3].astype(np.float32) * sa + bdst[:, :, :3] * da * (1.0 - sa)
                    o_a = np.clip(bsynth[:, :, 3:4].astype(np.float32) + bdst[:, :, 3:4] * (1.0 - sa), 0, 255)
                    u_rgb = bdst[:, :, :3] * da + bsynth[:, :, :3].astype(np.float32) * sa * (1.0 - da)
                    u_a = np.clip(bdst[:, :, 3:4] + bsynth[:, :, 3:4].astype(np.float32) * (1.0 - da), 0, 255)
                    ub = real_brow[:, :, None]
                    crgb = np.where(ub, u_rgb, o_rgb)
                    ca = np.where(ub, u_a, o_a)
                    bdst[:, :, :3] = np.where(ca > 1, crgb / np.maximum(1e-4, ca / 255.0), 0)
                    bdst[:, :, 3:4] = ca
                    out_rest[ry0:ry1, rx0:rx1] = np.clip(bdst, 0, 255).astype(np.uint8)

    return out_eye, out_rest


def _load_layer_canvas(puppet_dir: Path, manifest: Dict[str, Any], rigs: Tuple[str, ...]) -> np.ndarray:
    cw = int(manifest["canvas"]["width"])
    ch = int(manifest["canvas"]["height"])
    acc = np.zeros((ch, cw, 4), dtype=np.uint8)
    layers_dir = puppet_dir / "layers"
    for item in manifest["layers"]:
        if item.get("rig") in rigs or item.get("tag", "").lower() in rigs:
            fpath = layers_dir / Path(item["relPath"]).name
            if not fpath.exists():
                continue
            patch = np.array(Image.open(fpath).convert("RGBA"))
            x, y = int(item["x"]), int(item["y"])
            h, w = patch.shape[:2]
            src_a = patch[:, :, 3:4].astype(np.float32) / 255.0
            roi = acc[y:y + h, x:x + w].astype(np.float32)
            roi[:, :, :3] = patch[:, :, :3].astype(np.float32) * src_a + roi[:, :, :3] * (1.0 - src_a)
            roi[:, :, 3:4] = np.clip(patch[:, :, 3:4].astype(np.float32) + roi[:, :, 3:4] * (1.0 - src_a), 0, 255)
            acc[y:y + h, x:x + w] = np.clip(roi, 0, 255).astype(np.uint8)
    return acc


def _sample_dark_lash_color(lash: np.ndarray) -> Tuple[int, int, int]:
    alpha = lash[:, :, 3]
    lum = lash[:, :, :3].mean(axis=2)
    mask = (alpha > 180) & (lum < 80)
    if mask.any():
        rgb = np.median(lash[mask][:, :3], axis=0)
        return int(rgb[0]), int(rgb[1]), int(rgb[2])
    return (34, 14, 10)


def _smooth_1d(vals: np.ndarray, k: int = 7) -> np.ndarray:
    pad = k // 2
    padded = np.pad(vals, pad, mode="edge")
    kernel = np.ones(k, dtype=np.float64) / k
    return np.convolve(padded, kernel, mode="valid")


def _build_closed_eye_canvas(
    lash: np.ndarray,
    white: np.ndarray,
    cw: int,
    ch: int,
    side: str,
) -> Optional[np.ndarray]:
    """Synthesize a crisp 4x-supersampled closed eyelash & lowered crease
    along the eye's natural lower rim and outer corner wing.
    """
    lash_ys, lash_xs = np.where(lash[:, :, 3] > 35)
    white_ys, white_xs = np.where(white[:, :, 3] > 45)
    if len(lash_xs) < 6:
        return None

    # Find lower rim endpoints directly from eyewhite columns so we anchor
    # strictly on the eye opening rather than floating upper eyelid creases.
    has_white = len(white_xs) >= 6
    if not has_white:
        return None
    wx0, wx1 = int(white_xs.min()), int(white_xs.max())
    w_span = max(1, wx1 - wx0)

    dark_rgb = _sample_dark_lash_color(lash)
    halo_rgb = (min(255, dark_rgb[0] + 62), min(255, dark_rgb[1] + 34), min(255, dark_rgb[2] + 26))

    # First trace lower rim inside the eyewhite opening [wx0..wx1]
    raw_xs: List[float] = []
    raw_ys: List[float] = []
    raw_ws: List[float] = []
    for x in range(wx0, wx1 + 1):
        wa = np.where(white[:, x, 3] > 45)[0]
        if len(wa) == 0:
            continue
        t_w = (x - wx0) / w_span
        y_pos = float(wa.min()) * 0.07 + float(wa.max()) * 0.93
        w_thick = 1.6 + 1.45 * math.sin(math.pi * t_w)
        raw_xs.append(float(x))
        raw_ys.append(y_pos)
        raw_ws.append(w_thick)

    if len(raw_xs) < 6:
        return None

    # Smooth the core lower-rim lid curve
    xs_arr = np.array(raw_xs, dtype=np.float64)
    ys_arr = _smooth_1d(np.array(raw_ys, dtype=np.float64), k=min(9, len(raw_ys) | 1))
    ws_arr = _smooth_1d(np.array(raw_ws, dtype=np.float64), k=min(7, len(raw_ws) | 1))

    # Extrapolate a clean outer lash wing on the outer corner side and gentle taper at inner corner
    # (side == 'r' means viewer-left eye, outer wing goes to smaller x; 'l' goes to larger x)
    outer_dir = -1 if side == "r" else 1
    wing_len = min(14, int(round(w_span * 0.28)))
    outer_anchor_x = int(xs_arr[0] if outer_dir < 0 else xs_arr[-1])
    outer_anchor_y = float(ys_arr[0] if outer_dir < 0 else ys_arr[-1])
    slope_out = float((ys_arr[0] - ys_arr[min(4, len(ys_arr) - 1)]) / max(1.0, abs(xs_arr[0] - xs_arr[min(4, len(xs_arr) - 1)]))) if outer_dir < 0 else float((ys_arr[-1] - ys_arr[max(0, len(ys_arr) - 5)]) / max(1.0, abs(xs_arr[-1] - xs_arr[max(0, len(xs_arr) - 5)])))
    slope_out = max(-0.55, min(0.55, slope_out * 0.55))

    ext_outer_x: List[float] = []
    ext_outer_y: List[float] = []
    ext_outer_w: List[float] = []
    for k in range(1, wing_len + 1):
        xk = outer_anchor_x + outer_dir * k
        if not (0 <= xk < cw):
            break
        frac = k / max(1, wing_len)
        # Follow darkest real wing pixel if within 6px of predicted continuation, else clean tangent
        y_pred = outer_anchor_y + slope_out * k
        y_lo = max(0, int(y_pred - 6))
        y_hi = min(ch, int(y_pred + 6))
        col_a = lash[y_lo:y_hi, xk, 3]
        if (col_a > 60).any():
            best_y = float(y_lo + np.argmax(col_a))
            y_val = 0.65 * y_pred + 0.35 * best_y
        else:
            y_val = y_pred
        ext_outer_x.append(float(xk))
        ext_outer_y.append(y_val)
        ext_outer_w.append(max(0.45, 1.5 * (1.0 - frac * 0.85)))

    inner_len = min(5, int(round(w_span * 0.10)))
    inner_anchor_x = int(xs_arr[-1] if outer_dir < 0 else xs_arr[0])
    inner_anchor_y = float(ys_arr[-1] if outer_dir < 0 else ys_arr[0])
    inner_dir = -outer_dir
    ext_inner_x: List[float] = []
    ext_inner_y: List[float] = []
    ext_inner_w: List[float] = []
    for k in range(1, inner_len + 1):
        xk = inner_anchor_x + inner_dir * k
        frac = k / max(1, inner_len)
        ext_inner_x.append(float(xk))
        ext_inner_y.append(inner_anchor_y + 0.3 * k)
        ext_inner_w.append(max(0.4, 1.2 * (1.0 - frac)))

    if outer_dir < 0:
        all_x = np.array(list(reversed(ext_outer_x)) + xs_arr.tolist() + ext_inner_x)
        all_y = np.array(list(reversed(ext_outer_y)) + ys_arr.tolist() + ext_inner_y)
        all_w = np.array(list(reversed(ext_outer_w)) + ws_arr.tolist() + ext_inner_w)
    else:
        all_x = np.array(list(reversed(ext_inner_x)) + xs_arr.tolist() + ext_outer_x)
        all_y = np.array(list(reversed(ext_inner_y)) + ys_arr.tolist() + ext_outer_y)
        all_w = np.array(list(reversed(ext_inner_w)) + ws_arr.tolist() + ext_outer_w)

    xs_arr = all_x
    ys_arr = _smooth_1d(all_y, k=5)
    ws_arr = _smooth_1d(all_w, k=5)

    pad_px = 12
    bx0 = max(0, int(np.floor(xs_arr.min())) - pad_px)
    by0 = max(0, int(np.floor(ys_arr.min())) - 16)
    bx1 = min(cw, int(np.ceil(xs_arr.max())) + pad_px)
    by1 = min(ch, int(np.ceil(ys_arr.max())) + 12)
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 4 or bh <= 4:
        return None

    scale = 4
    def pt(x: float, y: float) -> Tuple[float, float]:
        return ((x - bx0) * scale, (y - by0) * scale)

    upper_poly = [pt(x, y - w * 0.55) for x, y, w in zip(xs_arr, ys_arr, ws_arr)]
    lower_poly = [pt(x, y + w * 0.45) for x, y, w in reversed(list(zip(xs_arr, ys_arr, ws_arr)))]

    halo = Image.new("RGBA", (bw * scale, bh * scale), (0, 0, 0, 0))
    halo_up = [pt(x, y - w * 0.85) for x, y, w in zip(xs_arr, ys_arr, ws_arr)]
    halo_lo = [pt(x, y + w * 0.75) for x, y, w in reversed(list(zip(xs_arr, ys_arr, ws_arr)))]
    ImageDraw.Draw(halo).polygon(halo_up + halo_lo, fill=(*halo_rgb, 125))
    halo = halo.filter(ImageFilter.GaussianBlur(radius=1.8))

    fg = Image.new("RGBA", (bw * scale, bh * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fg)
    draw.polygon(upper_poly + lower_poly, fill=(*dark_rgb, 248))

    # Subtle lowered double-eyelid crease over the middle 65% of the eye
    i0 = int(len(xs_arr) * 0.16)
    i1 = int(len(xs_arr) * 0.84)
    if i1 - i0 >= 4:
        crease_pts = [pt(xs_arr[i], ys_arr[i] - 5.2 - ws_arr[i] * 0.3) for i in range(i0, i1)]
        draw.line(crease_pts, fill=(min(255, dark_rgb[0] + 115), min(255, dark_rgb[1] + 75), min(255, dark_rgb[2] + 62), 125), width=scale)

    combined = Image.alpha_composite(halo, fg).resize((bw, bh), Image.Resampling.LANCZOS)
    canvas = np.zeros((ch, cw, 4), dtype=np.uint8)
    canvas[by0:by0 + bh, bx0:bx0 + bw] = np.array(combined)
    return canvas


def clamp_01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _make_layer_entry(
    crop: np.ndarray,
    gx: int,
    gy: int,
    cw: int,
    ch: int,
    cb_x: int,
    cb_y: int,
    cb_w: int,
    cb_h: int,
    *,
    layer_id: str,
    tag: str,
    label: str,
    label_en: str,
    rig: str,
    role: str,
    z_index: float,
    parallax_x: float,
    parallax_y: float,
    rel_path: str,
) -> Dict[str, Any]:
    lh, lw = crop.shape[:2]
    return {
        "id": layer_id,
        "tag": tag,
        "label": label,
        "labelEn": label_en,
        "category": "face",
        "rig": rig,
        "role": role,
        "x": gx,
        "y": gy,
        "width": lw,
        "height": lh,
        "nx": round((gx - cb_x) / cb_w, 5),
        "ny": round((gy - cb_y) / cb_h, 5),
        "nw": round(lw / cb_w, 5),
        "nh": round(lh / cb_h, 5),
        "zIndex": z_index,
        "depth": 0.15,
        "parallaxX": parallax_x,
        "parallaxY": parallax_y,
        "physics": 0.0,
        "visible": True,
        "relPath": rel_path,
    }


def _fix_neck_topwear_occlusion(
    puppet_dir: Path,
    manifest: Dict[str, Any],
    rel_base: str,
    outfit_id: Optional[str] = None,
) -> None:
    """Separate upper back-collar behind neck and remove AI-inpainted chest skin inside
    topwear V-necklines so neck/clavicle skin and necklaces render in natural anatomical order.
    """
    layers = manifest.get("layers", [])
    scoped = [
        l for l in layers
        if (l.get("outfit") == outfit_id if outfit_id is not None else not l.get("outfit"))
    ]
    by_id = {l["id"]: l for l in scoped}
    neck_item = next((l for l in scoped if l.get("rig") == "neck"), None)
    top_item = next((l for l in scoped if l.get("tag", "").lower() == "topwear"), None)
    if neck_item is None or top_item is None:
        return
    bc_id = f"layer_{outfit_id}_back_collar" if outfit_id else "layer_0_back_collar"
    if any(l.get("id") == bc_id for l in scoped):
        return

    cw = int(manifest["canvas"]["width"])
    ch = int(manifest["canvas"]["height"])
    cb = manifest["contentBounds"]
    cb_x, cb_y = int(cb["x"]), int(cb["y"])
    cb_w, cb_h = max(1, int(cb["width"])), max(1, int(cb["height"]))

    def _local_rel_dir(rel_path: str) -> str:
        rel_dir = Path(str(rel_path or "layers/x.png")).parent.as_posix().strip("/")
        if rel_base and rel_dir.startswith(rel_base + "/"):
            rel_dir = rel_dir[len(rel_base) + 1 :]
        elif rel_base and rel_dir == rel_base:
            rel_dir = "layers"
        else:
            for marker in ("layers", "outfits"):
                pos = rel_dir.find(marker)
                if pos > 0 and rel_dir[pos - 1] == "/":
                    rel_dir = rel_dir[pos:]
                    break
        return rel_dir or "layers"

    top_local_dir = _local_rel_dir(str(top_item.get("relPath") or "layers/topwear.png"))
    top_disk_dir = puppet_dir / top_local_dir

    def _load(item: Dict[str, Any]) -> np.ndarray:
        arr = np.zeros((ch, cw, 4), dtype=np.uint8)
        item_local_dir = _local_rel_dir(str(item.get("relPath") or "layers/x.png"))
        patch = np.array(Image.open(puppet_dir / item_local_dir / Path(item["relPath"]).name).convert("RGBA"))
        x, y, h, w = int(item["x"]), int(item["y"]), patch.shape[0], patch.shape[1]
        arr[y:y + h, x:x + w] = patch
        return arr

    neck = _load(neck_item)
    top = _load(top_item)

    t_a = top[:, :, 3] > 20
    r = top[:, :, 0].astype(np.int16)
    g = top[:, :, 1].astype(np.int16)
    b = top[:, :, 2].astype(np.int16)

    # Check whether topwear contains overlapping AI throat/chest skin over neck
    is_warm_skin = t_a & ((r - b) > 20) & (r > 180)
    overlap_skin = is_warm_skin & (neck[:, :, 3] > 20)
    if int(overlap_skin.sum()) < 800:
        return

    shirt_wall = t_a & (~is_warm_skin) & ((b >= r - 12) | ((r + g + b) < 460))
    yy, xx = np.mgrid[0:ch, 0:cw]
    nx_all = np.where(neck[:, :, 3] > 20)[1]
    ny_all = np.where(neck[:, :, 3] > 20)[0]
    tx_all = np.where(t_a)[1]
    ty_all = np.where(t_a)[0]
    if len(nx_all) == 0 or len(tx_all) == 0:
        return

    roi_box = (
        (xx >= max(0, int(nx_all.min()) - 10))
        & (xx <= min(cw - 1, int(nx_all.max()) + 10))
        & (yy >= max(0, int(ty_all.min()) - 2))
        & (yy <= min(ch - 1, int(ny_all.max()) + 30))
    )
    open_v = t_a & (~shirt_wall) & roi_box
    num, lbl, stats, _ = cv2.connectedComponentsWithStats(open_v.astype(np.uint8), connectivity=8)
    seed_center = (
        (xx >= int((nx_all.min() + nx_all.max()) * 0.5) - 45)
        & (xx <= int((nx_all.min() + nx_all.max()) * 0.5) + 45)
        & (yy >= int(ty_all.min()) + 20)
        & (yy <= int(ny_all.max()) + 5)
    )
    v_skin = np.zeros((ch, cw), dtype=bool)
    for cid in range(1, num):
        comp = lbl == cid
        if (comp & seed_center).any() and int(stats[cid, cv2.CC_STAT_AREA]) > 200:
            v_skin |= comp
    if not v_skin.any():
        return

    # Fill enclosed accessories/pendants inside V-opening
    v_filled = cv2.dilate(v_skin.astype(np.uint8), np.ones((15, 15), np.uint8))
    num_h, lbl_h, _, _ = cv2.connectedComponentsWithStats((v_filled == 0).astype(np.uint8), connectivity=8)
    for cid in range(1, num_h):
        comp_h = lbl_h == cid
        if int(comp_h.sum()) < 4000:
            v_skin |= comp_h

    v_dil = cv2.dilate(v_skin.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0

    # Extract purple back-collar strictly across upper neck span (measured above front lapel folds)
    ref_y = min(ch - 1, int(ty_all.min()) + 8)
    neck_throat_xs = np.where(neck[ref_y, :, 3] > 20)[0]
    bc_x0 = int(neck_throat_xs.min()) - 6 if len(neck_throat_xs) else int(nx_all.min())
    bc_x1 = int(neck_throat_xs.max()) - 2 if len(neck_throat_xs) else int(nx_all.max())

    back_collar_mask = np.zeros((ch, cw), dtype=bool)
    for x in range(max(0, bc_x0), min(cw, bc_x1 + 1)):
        vs_y = np.where(v_skin[:, x])[0]
        if len(vs_y):
            top_skin_y = int(vs_y.min())
            for y in range(max(0, top_skin_y - 40), top_skin_y + 2):
                if t_a[y, x] and not v_skin[y, x]:
                    back_collar_mask[y, x] = True

    # Extend neck generously (7px) underneath topwear skin past the carved edge so
    # feathering and breathing/sway motion never expose a semi-transparent seam slit.
    v_ext = (
        (cv2.dilate(v_dil.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
        & t_a
        & (~back_collar_mask)
        & (~shirt_wall)
    )
    ext_mask = (v_dil | v_ext) & (neck[:, :, 3] < 250) & t_a
    if ext_mask.any():
        dark_in_ext = ext_mask & ((r + g + b) < 380)
        inpaint_rgb = cv2.inpaint(top[:, :, :3], (dark_in_ext.astype(np.uint8) * 255), 5, cv2.INPAINT_TELEA)
        take = (v_dil | v_ext) & (neck[:, :, 3] < 250) & t_a
        a_n = neck[take, 3:4].astype(np.float32) / 255.0
        blended_rgb = neck[take, :3].astype(np.float32) * a_n + inpaint_rgb[take].astype(np.float32) * (1.0 - a_n)
        neck[take, :3] = np.clip(blended_rgb, 0, 255).astype(np.uint8)
        neck[take, 3] = np.maximum(neck[take, 3], top[take, 3])

    carve_from_top = v_dil | back_collar_mask
    soft_blur = np.array(
        Image.fromarray((v_dil.astype(np.uint8) * 255)).filter(ImageFilter.GaussianBlur(radius=1.6)),
        dtype=np.float32,
    ) / 255.0
    collar_blur = np.array(
        Image.fromarray((back_collar_mask.astype(np.uint8) * 255)).filter(ImageFilter.GaussianBlur(radius=0.9)),
        dtype=np.float32,
    ) / 255.0
    # Only feather into pixels backed by extended neck (v_ext) so fabric/collar edges stay crisp
    carve_f = np.maximum.reduce([
        carve_from_top.astype(np.float32),
        np.where(v_ext, soft_blur, 0.0),
        collar_blur,
    ])

    top_clean = top.copy()
    top_clean[:, :, 3] = np.clip(top[:, :, 3].astype(np.float32) * (1.0 - carve_f), 0, 255).astype(np.uint8)

    back_collar = np.zeros_like(top)
    bc_dil = cv2.dilate(back_collar_mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    back_collar[:, :, :3] = top[:, :, :3]
    back_collar[:, :, 3] = np.minimum(top[:, :, 3], (bc_dil.astype(np.uint8) * 255))

    # Save cleaned neck & topwear and update manifest metadata/z-order
    for item, arr_full in [(neck_item, neck), (top_item, top_clean)]:
        trimmed = _trim_rgba(arr_full, pad=2)
        if trimmed is None:
            continue
        crop, gx, gy, lw, lh = trimmed
        item_local_dir = _local_rel_dir(str(item.get("relPath") or "layers/x.png"))
        fname = Path(item["relPath"]).name
        Image.fromarray(crop, mode="RGBA").save(puppet_dir / item_local_dir / fname)
        item["x"] = gx
        item["y"] = gy
        item["width"] = lw
        item["height"] = lh
        item["nx"] = round((gx - cb_x) / cb_w, 5)
        item["ny"] = round((gy - cb_y) / cb_h, 5)
        item["nw"] = round(lw / cb_w, 5)
        item["nh"] = round(lh / cb_h, 5)

    bc_trim = _trim_rgba(back_collar, pad=2)
    if bc_trim is not None:
        bc_crop, gx, gy, lw, lh = bc_trim
        bc_fname = "00_back_collar.png"
        Image.fromarray(bc_crop, mode="RGBA").save(top_disk_dir / bc_fname)
        bc_entry: Dict[str, Any] = {
            "id": bc_id,
            "tag": "back collar",
            "label": "后领",
            "labelEn": "Back Collar",
            "category": "clothes",
            "rig": "torso",
            "x": gx,
            "y": gy,
            "width": lw,
            "height": lh,
            "nx": round((gx - cb_x) / cb_w, 5),
            "ny": round((gy - cb_y) / cb_h, 5),
            "nw": round(lw / cb_w, 5),
            "nh": round(lh / cb_h, 5),
            "zIndex": 0.5,
            "depth": float(top_item.get("depth", 0.74)),
            "parallaxX": float(top_item.get("parallaxX", 0.02)),
            "parallaxY": float(top_item.get("parallaxY", 0.01)),
            "physics": float(top_item.get("physics", 0.18)),
            "visible": True,
            "relPath": f"{rel_base}/{top_local_dir}/{bc_fname}" if rel_base else f"{top_local_dir}/{bc_fname}",
        }
        if outfit_id:
            bc_entry["outfit"] = outfit_id
        layers.append(bc_entry)

    # Place necklace above neck skin (and under topwear lapels) and earwear above ears
    neckwear_item = next((l for l in scoped if l.get("tag", "").lower() == "neckwear"), None)
    if neckwear_item is not None and float(neckwear_item.get("zIndex", 0)) < float(top_item.get("zIndex", 4)):
        neckwear_item["zIndex"] = 2.5
    if outfit_id is None:
        earwear_item = by_id.get("layer_5_earwear") or next((l for l in scoped if l.get("tag", "").lower() == "earwear"), None)
        if earwear_item is not None and float(earwear_item.get("zIndex", 0)) < 7.5:
            earwear_item["zIndex"] = 7.5

    layers.sort(key=lambda l: float(l.get("zIndex", 0)))


_OUTFIT_BODY_TAGS = frozenset({
    "neck", "neckwear", "topwear", "back collar",
    "handwear", "handwear-l", "handwear-r", "handwear_l", "handwear_r",
    "bottomwear", "legwear", "footwear", "tail", "wings", "objects",
})


def add_outfit_from_psd(
    psd_path: Path,
    puppet_dir: Path,
    outfit_id: str,
    outfit_label: str,
    rel_prefix: str,
) -> Dict[str, Any]:
    """Extract only body/clothing layers from ``psd_path`` and merge them as a switchable
    outfit into an existing ``puppet.json``, preserving shared head/face/hair/expressions.
    """
    from psd_tools import PSDImage
    from puppet_builder import _layer_meta

    manifest_path = puppet_dir / "puppet.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    safe_oid = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in outfit_id.strip()) or "outfit"
    clean_label = (outfit_label or safe_oid).strip() or safe_oid
    rel_base = rel_prefix.rstrip("/")

    cw = int(manifest["canvas"]["width"])
    ch = int(manifest["canvas"]["height"])
    cb = manifest["contentBounds"]
    cb_x, cb_y = int(cb["x"]), int(cb["y"])
    cb_w, cb_h = max(1, int(cb["width"])), max(1, int(cb["height"]))

    existing_layers: List[Dict[str, Any]] = manifest.get("layers", [])

    # Ensure existing untagged body/clothing layers belong to "default" so switching back works
    outfits: List[Dict[str, Any]] = list(manifest.get("outfits") or [])
    if not outfits:
        outfits.append({"id": "default", "label": "默认服装", "labelEn": "Default"})
        default_oid = "default"
    else:
        default_oid = outfits[0]["id"]

    for item in existing_layers:
        tag_l = str(item.get("tag") or "").lower().strip()
        if not item.get("outfit") and (
            item.get("category") in ("body", "clothes") or tag_l in _OUTFIT_BODY_TAGS
        ):
            item["outfit"] = default_oid

    # Replace previous layers for this outfit_id if updating
    kept_layers = [l for l in existing_layers if l.get("outfit") != safe_oid]

    outfit_dir = puppet_dir / "outfits" / safe_oid
    if outfit_dir.exists():
        shutil.rmtree(outfit_dir, ignore_errors=True)
    outfit_dir.mkdir(parents=True, exist_ok=True)

    psd = PSDImage.open(str(psd_path))
    json_meta_path = Path(str(psd_path) + ".json")
    part_meta: Dict[str, Any] = {}
    if json_meta_path.is_file():
        part_meta = (json.loads(json_meta_path.read_text(encoding="utf-8")) or {}).get("parts") or {}

    new_layers: List[Dict[str, Any]] = []
    for idx, layer in enumerate(psd):
        tag = str(layer.name or f"layer_{idx}").strip()
        tag_l = tag.lower().strip()
        meta = _layer_meta(tag)
        if meta["category"] not in ("body", "clothes") and tag_l not in _OUTFIT_BODY_TAGS:
            continue

        pil_img = layer.topil()
        if pil_img is None:
            continue
        rgba = np.array(pil_img.convert("RGBA"))
        # If incoming PSD differs slightly in size from base puppet canvas, resize/pad to match
        if psd.width != cw or psd.height != ch:
            full = np.zeros((psd.height, psd.width, 4), dtype=np.uint8)
            lx0, ly0 = max(0, int(layer.left)), max(0, int(layer.top))
            lx1, ly1 = min(psd.width, int(layer.left) + rgba.shape[1]), min(psd.height, int(layer.top) + rgba.shape[0])
            sx0, sy0 = max(0, -int(layer.left)), max(0, -int(layer.top))
            if lx1 > lx0 and ly1 > ly0:
                full[ly0:ly1, lx0:lx1] = rgba[sy0:sy0 + (ly1 - ly0), sx0:sx0 + (lx1 - lx0)]
            full_pil = Image.fromarray(full, mode="RGBA").resize((cw, ch), Image.Resampling.LANCZOS)
            full_arr = np.array(full_pil)
            gx_off, gy_off = 0, 0
        else:
            full_arr = rgba
            gx_off, gy_off = int(layer.left), int(layer.top)

        trimmed = _trim_rgba(full_arr, pad=2)
        if trimmed is None:
            continue
        crop, lx, ly, lw, lh = trimmed
        gx = gx_off + lx
        gy = gy_off + ly

        safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in tag)
        filename = f"{idx:02d}_{safe_name}.png"
        Image.fromarray(crop, mode="RGBA").save(outfit_dir / filename)

        pinfo = part_meta.get(tag, {})
        depth_median = float(pinfo.get("depth_median", 0.5))

        new_layers.append({
            "id": f"layer_{safe_oid}_{idx}_{safe_name}",
            "tag": tag,
            "label": meta["label"],
            "labelEn": meta["labelEn"],
            "category": meta["category"],
            "rig": meta["rig"],
            "outfit": safe_oid,
            "x": gx,
            "y": gy,
            "width": lw,
            "height": lh,
            "nx": round((gx - cb_x) / cb_w, 5),
            "ny": round((gy - cb_y) / cb_h, 5),
            "nw": round(lw / cb_w, 5),
            "nh": round(lh / cb_h, 5),
            "zIndex": float(idx),
            "depth": depth_median,
            "parallaxX": meta["parallax"][0],
            "parallaxY": meta["parallax"][1],
            "physics": meta["physics"],
            "visible": True,
            "relPath": f"{rel_base}/outfits/{safe_oid}/{filename}",
        })

    manifest["layers"] = kept_layers + new_layers
    existing_out = next((o for o in outfits if o.get("id") == safe_oid), None)
    if existing_out is not None:
        existing_out["label"] = clean_label
        if not existing_out.get("labelEn"):
            existing_out["labelEn"] = clean_label
    else:
        outfits.append({"id": safe_oid, "label": clean_label, "labelEn": clean_label})
    manifest["outfits"] = outfits

    _fix_neck_topwear_occlusion(puppet_dir, manifest, rel_base, outfit_id=safe_oid)

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)
    return manifest


def extract_expressions_and_blink(
    puppet_dir: Path,
    source_dir: Optional[Path],
    base_name: str,
    rel_prefix: str,
    complete_occluded_eye: bool = True,
) -> Dict[str, Any]:
    manifest_path = puppet_dir / "puppet.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    cw = int(manifest["canvas"]["width"])
    ch = int(manifest["canvas"]["height"])
    cb = manifest["contentBounds"]
    cb_x, cb_y = int(cb["x"]), int(cb["y"])
    cb_w, cb_h = max(1, int(cb["width"])), max(1, int(cb["height"]))
    rel_base = rel_prefix.rstrip("/")

    # Normalize existing base layer relPath prefixes if caller requested a new prefix
    if rel_base:
        for item in manifest.get("layers", []):
            fname = Path(item.get("relPath", "")).name
            if fname:
                item["relPath"] = f"{rel_base}/layers/{fname}"

    # Ensure anchors has headTilt
    eye_l_pt = manifest["anchors"]["eyeL"]
    eye_r_pt = manifest["anchors"]["eyeR"]
    dx_e = (eye_l_pt[0] - eye_r_pt[0]) * cb_w
    dy_e = (eye_l_pt[1] - eye_r_pt[1]) * cb_h
    manifest["anchors"]["headTilt"] = round(
        float(math.atan2(dy_e, dx_e)) if (dx_e * dx_e + dy_e * dy_e) > 1e-3 else 0.0, 5
    )

    # Fix overlapping collar/neck AI chest skin if topwear sits over neck
    _fix_neck_topwear_occlusion(puppet_dir, manifest, rel_base)

    face = _load_layer_canvas(puppet_dir, manifest, ("face",))
    nose = _load_layer_canvas(puppet_dir, manifest, ("nose",))
    hair_f = _load_layer_canvas(puppet_dir, manifest, ("front_hair", "hair_front"))
    lash_r = _load_layer_canvas(puppet_dir, manifest, ("eyelash-r", "eyelash_r"))
    white_r = _load_layer_canvas(puppet_dir, manifest, ("eyewhite-r", "eyewhite_r"))
    ir_r = _load_layer_canvas(puppet_dir, manifest, ("irides-r", "irides_r", "iris_r"))
    lash_l = _load_layer_canvas(puppet_dir, manifest, ("eyelash-l", "eyelash_l"))
    white_l = _load_layer_canvas(puppet_dir, manifest, ("eyewhite-l", "eyewhite_l"))
    ir_l = _load_layer_canvas(puppet_dir, manifest, ("irides-l", "irides_l", "iris_l"))
    brow_r = _load_layer_canvas(puppet_dir, manifest, ("eyebrow-r", "eyebrow_r", "brow_r"))
    brow_l = _load_layer_canvas(puppet_dir, manifest, ("eyebrow-l", "eyebrow_l", "brow_l"))
    mouth_base = _load_layer_canvas(puppet_dir, manifest, ("mouth",))

    # 1. Generate closed-eye blink layers
    blink_layers: List[Dict[str, Any]] = []
    layers_dir = puppet_dir / "layers"
    for side, rig, lash, white, label, label_en in [
        ("r", "blink_r", lash_r, white_r, "闭眼-右", "Closed Eye R"),
        ("l", "blink_l", lash_l, white_l, "闭眼-左", "Closed Eye L"),
    ]:
        closed_canvas = _build_closed_eye_canvas(lash, white, cw, ch, side)
        if closed_canvas is None:
            continue
        trimmed = _trim_rgba(closed_canvas, pad=2)
        if trimmed is None:
            continue
        crop, gx, gy, _, _ = trimmed
        fname = f"blink_closed_{side}.png"
        Image.fromarray(crop, mode="RGBA").save(layers_dir / fname)
        blink_layers.append(
            _make_layer_entry(
                crop, gx, gy, cw, ch, cb_x, cb_y, cb_w, cb_h,
                layer_id=f"blink_closed_{side}",
                tag=rig,
                label=label,
                label_en=label_en,
                rig=rig,
                role="blink_closed",
                z_index=18.6 if side == "r" else 18.65,
                parallax_x=0.15,
                parallax_y=0.10,
                rel_path=f"{rel_base}/layers/{fname}",
            )
        )
    manifest["blinkLayers"] = blink_layers

    # 2. Extract split expression overlays if source_dir has multiple images
    expressions: List[Dict[str, Any]] = []
    if source_dir and source_dir.exists():
        pngs = {p.stem: p for p in sorted(source_dir.glob("*.png"))}
        if base_name in pngs and len(pngs) > 1:
            expr_root = puppet_dir / "expressions"
            if expr_root.exists():
                shutil.rmtree(expr_root)
            expr_root.mkdir(parents=True, exist_ok=True)

            base_arr = center_square_pad_resize(
                np.array(Image.open(pngs[base_name]).convert("RGBA")), cw
            )

            face_clip = _dilate_mask((face[:, :, 3] > 20) | (nose[:, :, 3] > 20), 2)
            hair_f_alpha = hair_f[:, :, 3].astype(np.float32)
            hair_forbid = hair_f[:, :, 3] > 12
            hair_fringe_ring = _dilate_mask(hair_f[:, :, 3] > 20, 2) & (~hair_forbid)
            hair_atten = np.clip(1.0 - hair_f_alpha / 24.0, 0.0, 1.0)
            base_features_union = (
                (lash_r[:, :, 3] > 20) | (white_r[:, :, 3] > 20) | (ir_r[:, :, 3] > 20)
                | (lash_l[:, :, 3] > 20) | (white_l[:, :, 3] > 20) | (ir_l[:, :, 3] > 20)
                | (brow_r[:, :, 3] > 20) | (brow_l[:, :, 3] > 20) | (mouth_base[:, :, 3] > 20)
            ) & (~hair_forbid)

            # Bare skin + nose underneath overlays for clean connected-component eye separation
            face_under = face.copy().astype(np.float32)
            na = nose[:, :, 3:4].astype(np.float32) / 255.0
            face_under[:, :, :3] = nose[:, :, :3].astype(np.float32) * na + face_under[:, :, :3] * (1.0 - na)
            face_under[:, :, 3:4] = np.clip(nose[:, :, 3:4].astype(np.float32) + face_under[:, :, 3:4] * (1.0 - na), 0, 255)
            face_under_u8 = np.clip(face_under, 0, 255).astype(np.uint8)

            mouth_x = float(manifest["anchors"]["mouthCenter"][0]) * cb_w + cb_x
            mouth_y = float(manifest["anchors"]["mouthCenter"][1]) * cb_h + cb_y
            mouth_w = max(48.0, float(manifest["anchors"]["mouthSize"][0]) * cb_w * 1.7)
            mouth_h = max(48.0, float(manifest["anchors"]["mouthSize"][1]) * cb_h * 2.2)

            yy, xx = np.mgrid[0:ch, 0:cw]
            mouth_zone = (
                (np.abs(xx - mouth_x) <= mouth_w * 0.52)
                & (np.abs(yy - mouth_y) <= mouth_h * 0.56)
            )

            # Seed strictly inside horizontal sclera core (inset 3px from nose bridge so slanted angry brows never touch seed)
            def _lower_eye_seed(w_arr: np.ndarray, l_arr: np.ndarray) -> np.ndarray:
                w_mask = w_arr[:, :, 3] > 25
                comb = w_mask | (l_arr[:, :, 3] > 35)
                out = np.zeros_like(comb)
                ys_e, xs_e = np.where(w_mask)
                if len(xs_e) < 6:
                    return _dilate_mask(comb, 1)
                x_lo, x_hi = int(xs_e.min()) + 3, int(xs_e.max()) - 2
                for x in range(x_lo, x_hi + 1):
                    wy = np.where(w_mask[:, x])[0]
                    if len(wy):
                        ymid = int(wy.max()) - 4
                        out[max(0, ymid - 4):min(ch, ymid + 5), x] = True
                return out

            seed_r = _lower_eye_seed(white_r, lash_r)
            seed_l = _lower_eye_seed(white_l, lash_l)

            def _box_for(mask_b: np.ndarray) -> Tuple[int, int, int, int]:
                ys_b, xs_b = np.where(mask_b)
                if len(xs_b) == 0:
                    return (0, 0, cw, ch)
                return (int(xs_b.min()) - 28, int(ys_b.min()) - 42, int(xs_b.max()) + 28, int(ys_b.max()) + 20)

            box_r = _box_for(seed_r | (lash_r[:, :, 3] > 20))
            box_l = _box_for(seed_l | (lash_l[:, :, 3] > 20))

            # Order expression stems cleanly (Normal first, then happy/shy/surprised/angry/sad)
            ordered_stems = sorted(
                pngs.keys(),
                key=lambda s: (_resolve_emotion_meta(s)[2], s.lower()),
            )

            for idx, stem in enumerate(ordered_stems):
                label_en, emo_key, _ = _resolve_emotion_meta(stem)
                if stem == base_name:
                    expressions.append({
                        "id": stem,
                        "label": stem,
                        "labelEn": label_en,
                        "emotionKey": emo_key,
                        "isBase": True,
                        "layers": [],
                    })
                    continue

                arr = center_square_pad_resize(
                    np.array(Image.open(pngs[stem]).convert("RGBA")), cw
                )
                diff = np.abs(arr[:, :, :3].astype(np.int16) - base_arr[:, :, :3].astype(np.int16)).max(axis=2) > 9
                # Suppress cool-navy front-hair fringe pixels right around 19_front_hair
                arr_rgb = arr[:, :, :3].astype(np.int16)
                cool_hair_fringe = (
                    hair_fringe_ring
                    & ((arr_rgb[:, :, 2] - arr_rgb[:, :, 0]) > 4)
                    & (arr_rgb.mean(axis=2) < 145)
                )
                hair_keep = (~hair_forbid) & (~cool_hair_fringe)
                raw_mask = _dilate_mask(diff | base_features_union, 2) & face_clip & hair_keep

                m_mouth = raw_mask & mouth_zone
                forbid_eyes = mouth_zone | (~raw_mask) | hair_forbid
                m_eye_r = raw_mask & _segment_expression_eye_mask(arr, face_under_u8, seed_r, box_r, forbid_eyes)
                m_eye_l = raw_mask & _segment_expression_eye_mask(arr, face_under_u8, seed_l, box_l, forbid_eyes)
                m_rest = raw_mask & (~mouth_zone) & (~m_eye_r) & (~m_eye_l)

                subdir = expr_root / f"expr_{idx:02d}"
                subdir.mkdir(parents=True, exist_ok=True)
                exp_layers: List[Dict[str, Any]] = []

                parts = [
                    ("rest", m_rest, "face", 18.2, 0.14, 0.10),
                    ("eye_r", m_eye_r, "eye_r", 18.3, 0.15, 0.10),
                    ("eye_l", m_eye_l, "eye_l", 18.4, 0.15, 0.10),
                    ("mouth", m_mouth, "mouth", 18.5, 0.15, 0.10),
                ]
                built_canvases: Dict[str, np.ndarray] = {}
                for role, m_part, _, _, _, _ in parts:
                    if not m_part.any():
                        continue
                    feathered = _feather_mask(m_part, 1.1) * hair_atten
                    keep_dilated = _dilate_mask(m_part, 3) & hair_keep
                    patch_arr = arr.copy()
                    patch_arr[:, :, 3] = np.where(
                        keep_dilated,
                        (feathered * arr[:, :, 3].astype(np.float32)).astype(np.uint8),
                        0,
                    )
                    built_canvases[role] = patch_arr

                if complete_occluded_eye and "eye_r" in built_canvases and "rest" in built_canvases:
                    built_canvases["eye_r"], built_canvases["rest"] = _complete_occluded_outer_eye_and_brow(
                        built_canvases["eye_r"],
                        built_canvases["rest"],
                        arr,
                        lash_r,
                        white_r,
                        hair_forbid,
                        box_r,
                    )

                for role, _, rig, z_idx, px, py in parts:
                    patch_arr = built_canvases.get(role)
                    if patch_arr is None:
                        continue
                    trimmed = _trim_rgba(patch_arr, pad=2)
                    if trimmed is None:
                        continue
                    crop, gx, gy, _, _ = trimmed
                    fname = f"{role}.png"
                    Image.fromarray(crop, mode="RGBA").save(subdir / fname)
                    exp_layers.append(
                        _make_layer_entry(
                            crop, gx, gy, cw, ch, cb_x, cb_y, cb_w, cb_h,
                            layer_id=f"expr_{idx:02d}_{role}",
                            tag=f"{stem}_{role}",
                            label=f"{stem}-{role}",
                            label_en=f"{label_en} ({role})",
                            rig=rig,
                            role=role,
                            z_index=z_idx,
                            parallax_x=px,
                            parallax_y=py,
                            rel_path=f"{rel_base}/expressions/expr_{idx:02d}/{fname}",
                        )
                    )

                expressions.append({
                    "id": stem,
                    "label": stem,
                    "labelEn": label_en,
                    "emotionKey": emo_key,
                    "isBase": False,
                    "layers": exp_layers,
                })

    manifest["expressions"] = expressions
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--puppet-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=None)
    parser.add_argument("--base-name", default="惊讶2")
    parser.add_argument("--rel-prefix", default="")
    parser.add_argument(
        "--no-complete-eye",
        action="store_false",
        dest="complete_eye",
        help="Disable Plan 1 blind-spot outer eye/brow completion (use pure Plan 2 front-hair constraint).",
    )
    args = parser.parse_args()

    manifest_path = args.puppet_dir / "puppet.json"
    rel_prefix = args.rel_prefix
    if not rel_prefix and manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("layers"):
            rel_prefix = data["layers"][0]["relPath"].rsplit("/layers/", 1)[0]

    res = extract_expressions_and_blink(
        args.puppet_dir,
        args.source_dir,
        args.base_name,
        rel_prefix,
        complete_occluded_eye=args.complete_eye,
    )
    print(
        f"Updated puppet.json: headTilt={res['anchors']['headTilt']} rad, "
        f"blinkLayers={len(res.get('blinkLayers', []))}, expressions={len(res.get('expressions', []))}"
    )


if __name__ == "__main__":
    main()

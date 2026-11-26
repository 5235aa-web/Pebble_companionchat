"""Open-source Inochi2D (.inx) puppet format importer.

Parses the standard Inochi2D binary container (``TRNSRTS\\0`` JSON scene graph +
``TEX_SECT`` texture atlas blobs), extracts PNG/TGA part textures, flattens the
node world transforms and z-sorting, and emits a web-ready ``puppet.json``
compatible with our upgraded WebGL2 renderer.
"""
from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from .psd_importer import _compute_anchors_from_acc, _infer_semantic_tag
from .puppet_builder import _layer_meta, _mask_centroid, _trim_rgba

_INX_MAGIC = b"TRNSRTS\x00"
_TEX_MAGIC = b"TEX_SECT"


def parse_inx_container(raw: bytes) -> Tuple[Dict[str, Any], List[Image.Image]]:
    """Decode an Inochi2D ``.inx`` binary blob into ``(puppet_json, texture_images)``."""
    if len(raw) < 16 or raw[:8] != _INX_MAGIC:
        raise ValueError("不是有效的 Inochi2D (.inx) 文件头")

    json_len = struct.unpack(">I", raw[8:12])[0]
    pos = 12
    if pos + json_len > len(raw):
        raise ValueError(".inx JSON 数据长度越界")
    puppet_doc = json.loads(raw[pos:pos + json_len].decode("utf-8"))
    pos += json_len

    if pos + 12 > len(raw) or raw[pos:pos + 8] != _TEX_MAGIC:
        raise ValueError(".inx 缺少 TEX_SECT 纹理段")
    pos += 8
    tex_count = struct.unpack(">I", raw[pos:pos + 4])[0]
    pos += 4

    textures: List[Image.Image] = []
    for _ in range(tex_count):
        if pos + 5 > len(raw):
            raise ValueError(".inx 纹理条目越界")
        blob_len = struct.unpack(">I", raw[pos:pos + 4])[0]
        fmt = raw[pos + 4]
        pos += 5
        blob = raw[pos:pos + blob_len]
        pos += blob_len
        if len(blob) != blob_len:
            raise ValueError(".inx 纹理数据不完整")
        if fmt in (0, 1):
            img = Image.open(io.BytesIO(blob)).convert("RGBA")
            textures.append(img)
        else:
            raise ValueError(f"暂不支持的 .inx 纹理编码格式 ({fmt})，请在 Inochi Creator 中以 PNG 纹理导出")

    return puppet_doc, textures


def _collect_inx_parts(
    node: Dict[str, Any],
    parent_tx: float = 0.0,
    parent_ty: float = 0.0,
    parent_z: float = 0.0,
    group_hint: str = "",
) -> List[Dict[str, Any]]:
    t = node.get("transform") or {}
    trans = t.get("trans") or [0.0, 0.0, 0.0]
    tx = parent_tx + float(trans[0] if len(trans) > 0 else 0.0)
    ty = parent_ty + float(trans[1] if len(trans) > 1 else 0.0)
    zsort = parent_z + float(node.get("zsort", 0.0))

    name = str(node.get("name") or "")
    ntype = str(node.get("type") or "")
    next_group = group_hint or (name if ntype in ("Node", "Composite") and name not in ("Root", "Nodes") else "")

    collected: List[Dict[str, Any]] = []
    if ntype in ("Part", "TexturedMesh", "MeshGroup") and node.get("mesh") and node.get("textures"):
        collected.append({
            "node": node,
            "name": name,
            "wx": tx,
            "wy": ty,
            "zsort": zsort,
            "group": next_group,
        })

    for child in node.get("children") or []:
        if isinstance(child, dict):
            collected.extend(_collect_inx_parts(child, tx, ty, zsort, next_group))
    return collected


def import_inx_file(
    inx_path: Path,
    out_dir: Path,
    rel_prefix: str,
    source_name: str = "",
) -> Dict[str, Any]:
    """Import an Inochi2D ``.inx`` puppet into ``out_dir/puppet.json`` + ``layers/*.png``."""
    doc, textures = parse_inx_container(inx_path.read_bytes())
    root_node = doc.get("nodes") or {}
    parts = _collect_inx_parts(root_node)
    if not parts:
        raise ValueError(".inx 模型中没有可渲染的 Part 图层")

    # Sort back-to-front: in Inochi2D, larger zsort renders behind (draw order descending zsort)
    parts.sort(key=lambda p: (-float(p["zsort"]), p["name"]))

    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    for stale in layers_dir.glob("*.png"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Project mesh vertex world positions to find unified canvas bounds
    baked_parts: List[Dict[str, Any]] = []
    world_min_x, world_min_y = float("inf"), float("inf")
    world_max_x, world_max_y = float("-inf"), float("-inf")

    for idx, item in enumerate(parts):
        node = item["node"]
        tex_ids = node.get("textures") or []
        tex_idx = int(tex_ids[0]) if tex_ids else -1
        if tex_idx < 0 or tex_idx >= len(textures):
            continue
        tex_img = textures[tex_idx]
        tw, th = tex_img.size

        mesh = node.get("mesh") or {}
        verts = mesh.get("verts") or []
        uvs = mesh.get("uvs") or []
        origin = mesh.get("origin") or [0.0, 0.0]
        if len(verts) < 6 or len(uvs) < 6:
            continue

        xs = [float(verts[i]) + item["wx"] - float(origin[0]) for i in range(0, len(verts), 2)]
        ys = [float(verts[i + 1]) + item["wy"] - float(origin[1]) for i in range(0, len(verts), 2)]
        us = [float(uvs[i]) for i in range(0, len(uvs), 2)]
        vs = [float(uvs[i + 1]) for i in range(0, len(uvs), 2)]

        u0, u1 = max(0.0, min(us)), min(1.0, max(us))
        v0, v1 = max(0.0, min(vs)), min(1.0, max(vs))
        px0, py0 = int(round(u0 * tw)), int(round(v0 * th))
        px1, py1 = max(px0 + 1, int(round(u1 * tw))), max(py0 + 1, int(round(v1 * th)))
        crop_pil = tex_img.crop((px0, py0, px1, py1))
        rgba = np.array(crop_pil.convert("RGBA"))
        trimmed = _trim_rgba(rgba, pad=2)
        if trimmed is None:
            continue
        crop, _, _, _, _ = trimmed

        x0_w, x1_w = min(xs), max(xs)
        y0_w, y1_w = min(ys), max(ys)
        world_min_x = min(world_min_x, x0_w)
        world_min_y = min(world_min_y, y0_w)
        world_max_x = max(world_max_x, x1_w)
        world_max_y = max(world_max_y, y1_w)

        baked_parts.append({
            "idx": idx,
            "name": item["name"] or f"part_{idx}",
            "crop": crop,
            "x0": x0_w,
            "y0": y0_w,
            "w": max(1.0, x1_w - x0_w),
            "h": max(1.0, y1_w - y0_w),
            "zsort": float(item["zsort"]),
        })

    if not baked_parts:
        raise ValueError(".inx 模型没有有效的可见网格纹理")

    cb_w = max(1, int(round(world_max_x - world_min_x)))
    cb_h = max(1, int(round(world_max_y - world_min_y)))
    ox = int(round(world_min_x))
    oy = int(round(world_min_y))

    raw_layers: List[Dict[str, Any]] = []
    anchors_acc: Dict[str, Tuple[float, float, Tuple[int, int, int, int]]] = {}

    for order_idx, bp in enumerate(baked_parts):
        raw_name = bp["name"]
        canonical = _infer_semantic_tag(raw_name)
        meta = _layer_meta(canonical)
        safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in raw_name)
        fname = f"{order_idx:02d}_{safe_name}.png"
        Image.fromarray(bp["crop"], mode="RGBA").save(layers_dir / fname)

        gx = int(round(bp["x0"] - world_min_x))
        gy = int(round(bp["y0"] - world_min_y))
        lw = max(1, int(round(bp["w"])))
        lh = max(1, int(round(bp["h"])))

        cinfo = _mask_centroid(bp["crop"])
        if cinfo is not None:
            sx = lw / max(1, bp["crop"].shape[1])
            sy = lh / max(1, bp["crop"].shape[0])
            cx_l, cy_l, lb = cinfo
            anchors_acc[canonical.lower()] = (
                gx + cx_l * sx,
                gy + cy_l * sy,
                (
                    int(gx + lb[0] * sx),
                    int(gy + lb[1] * sy),
                    int(gx + lb[2] * sx),
                    int(gy + lb[3] * sy),
                ),
            )

        raw_layers.append({
            "id": f"inx_{order_idx}_{safe_name}",
            "tag": raw_name,
            "label": raw_name,
            "labelEn": meta["labelEn"] if meta["labelEn"] != canonical else raw_name,
            "category": meta["category"],
            "rig": meta["rig"],
            "subjectId": "sub_0",
            "x": gx,
            "y": gy,
            "width": lw,
            "height": lh,
            "nx": round(gx / cb_w, 5),
            "ny": round(gy / cb_h, 5),
            "nw": round(lw / cb_w, 5),
            "nh": round(lh / cb_h, 5),
            "zIndex": order_idx,
            "depth": 0.5,
            "parallaxX": meta["parallax"][0],
            "parallaxY": meta["parallax"][1],
            "physics": meta["physics"],
            "visible": True,
            "relPath": f"{rel_prefix.rstrip('/')}/layers/{fname}",
        })

    anchors = _compute_anchors_from_acc(anchors_acc, 0, 0, cb_w, cb_h, (0, 0, cb_w, cb_h))
    meta_info = doc.get("meta") or {}
    manifest = {
        "version": 2,
        "format": "inochi2d_inx",
        "canvas": {"width": cb_w, "height": cb_h},
        "contentBounds": {"x": 0, "y": 0, "width": cb_w, "height": cb_h},
        "originOffset": [ox, oy],
        "anchors": anchors,
        "subjects": [{"id": "sub_0", "label": str(meta_info.get("name") or "主体"), "anchors": anchors}],
        "source": source_name or inx_path.name,
        "layers": raw_layers,
    }
    (out_dir / "puppet.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

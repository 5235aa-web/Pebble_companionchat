"""Multi-pose slot management and visual rig persistence for Inochi2D puppets."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..paths import assert_safe_data_path


def _sanitize_pose_id(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", (raw or "").strip()).strip("_-")
    return clean[:48] or "pose"


def _index_path(puppet_root: Path) -> Path:
    return puppet_root / "poses_index.json"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def pose_dir_for(puppet_root: Path, pose_id: str = "") -> Path:
    pid = (pose_id or "").strip()
    if not pid or pid == "default":
        return puppet_root
    return puppet_root / "poses" / _sanitize_pose_id(pid)


def pose_rel_prefix_for(companion_dir_name: str, pose_id: str = "") -> str:
    pid = (pose_id or "").strip()
    if not pid or pid == "default":
        return f"uploads/inochi2d/{companion_dir_name}"
    return f"uploads/inochi2d/{companion_dir_name}/poses/{_sanitize_pose_id(pid)}"


def _read_index(puppet_root: Path) -> Dict[str, Any]:
    idx_file = _index_path(puppet_root)
    doc: Dict[str, Any] = {"activePoseId": "default", "poses": []}
    if idx_file.is_file():
        try:
            raw = json.loads(idx_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                doc["activePoseId"] = str(raw.get("activePoseId") or "default")
                if isinstance(raw.get("poses"), list):
                    doc["poses"] = [p for p in raw["poses"] if isinstance(p, dict) and p.get("id")]
        except Exception:
            pass

    # Auto-register legacy default puppet if puppet.json exists without index
    has_default = (puppet_root / "puppet.json").is_file()
    if has_default and not any(p.get("id") == "default" for p in doc["poses"]):
        doc["poses"].insert(
            0,
            {
                "id": "default",
                "label": "默认姿态",
                "labelEn": "Default Pose",
                "triggerMoods": [],
                "triggerKeywords": [],
                "isDefault": True,
                "createdAt": int(time.time()),
            },
        )

    # Filter out entries whose puppet.json was removed externally
    valid: List[Dict[str, Any]] = []
    for entry in doc["poses"]:
        pid = str(entry["id"])
        pdir = pose_dir_for(puppet_root, pid)
        if (pdir / "puppet.json").is_file():
            valid.append(entry)
    doc["poses"] = valid

    if valid and not any(p.get("id") == doc["activePoseId"] for p in valid):
        doc["activePoseId"] = str(valid[0]["id"])
    elif not valid:
        doc["activePoseId"] = "default"
    return doc


def list_poses(puppet_root: Path) -> Dict[str, Any]:
    return _read_index(puppet_root)


def get_active_pose_id(puppet_root: Path) -> str:
    return str(_read_index(puppet_root).get("activePoseId") or "default")


def register_pose(
    puppet_root: Path,
    pose_id: str,
    label: str = "",
    label_en: str = "",
    trigger_moods: Optional[List[str]] = None,
    trigger_keywords: Optional[List[str]] = None,
    activate: bool = True,
) -> Dict[str, Any]:
    assert_safe_data_path(puppet_root)
    pid = "default" if (not pose_id or pose_id == "default") else _sanitize_pose_id(pose_id)
    doc = _read_index(puppet_root)
    existing = next((p for p in doc["poses"] if p.get("id") == pid), None)
    clean_label = (label or (existing or {}).get("label") or ("默认姿态" if pid == "default" else pid)).strip()
    clean_en = (label_en or (existing or {}).get("labelEn") or ("Default Pose" if pid == "default" else clean_label)).strip()

    entry = {
        "id": pid,
        "label": clean_label,
        "labelEn": clean_en,
        "triggerMoods": list(trigger_moods) if trigger_moods is not None else list((existing or {}).get("triggerMoods") or []),
        "triggerKeywords": list(trigger_keywords) if trigger_keywords is not None else list((existing or {}).get("triggerKeywords") or []),
        "isDefault": pid == "default",
        "createdAt": int((existing or {}).get("createdAt") or time.time()),
    }
    if existing is not None:
        doc["poses"] = [entry if p.get("id") == pid else p for p in doc["poses"]]
    else:
        doc["poses"].append(entry)
    if activate:
        doc["activePoseId"] = pid
    _atomic_write_json(_index_path(puppet_root), doc)
    return doc


def select_pose(puppet_root: Path, pose_id: str) -> Dict[str, Any]:
    assert_safe_data_path(puppet_root)
    doc = _read_index(puppet_root)
    pid = "default" if (not pose_id or pose_id == "default") else _sanitize_pose_id(pose_id)
    if not any(p.get("id") == pid for p in doc["poses"]):
        raise FileNotFoundError(f"未找到姿态模型: {pid}")
    doc["activePoseId"] = pid
    _atomic_write_json(_index_path(puppet_root), doc)
    return doc


def update_pose_meta(
    puppet_root: Path,
    pose_id: str,
    label: Optional[str] = None,
    trigger_moods: Optional[List[str]] = None,
    trigger_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    assert_safe_data_path(puppet_root)
    doc = _read_index(puppet_root)
    pid = "default" if (not pose_id or pose_id == "default") else _sanitize_pose_id(pose_id)
    hit = next((p for p in doc["poses"] if p.get("id") == pid), None)
    if hit is None:
        raise FileNotFoundError(f"未找到姿态模型: {pid}")
    if label is not None and label.strip():
        hit["label"] = label.strip()
        hit["labelEn"] = label.strip()
    if trigger_moods is not None:
        hit["triggerMoods"] = [str(m).strip() for m in trigger_moods if str(m).strip()]
    if trigger_keywords is not None:
        hit["triggerKeywords"] = [str(k).strip() for k in trigger_keywords if str(k).strip()]
    _atomic_write_json(_index_path(puppet_root), doc)
    return doc


def delete_pose(puppet_root: Path, pose_id: str) -> Dict[str, Any]:
    assert_safe_data_path(puppet_root)
    pid = _sanitize_pose_id(pose_id)
    if not pose_id or pid == "default":
        raise ValueError("不能删除默认基础姿态")
    doc = _read_index(puppet_root)
    if not any(p.get("id") == pid for p in doc["poses"]):
        raise FileNotFoundError(f"未找到姿态模型: {pid}")
    pdir = pose_dir_for(puppet_root, pid)
    if pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
    doc["poses"] = [p for p in doc["poses"] if p.get("id") != pid]
    if doc["activePoseId"] == pid:
        doc["activePoseId"] = str(doc["poses"][0]["id"]) if doc["poses"] else "default"
    _atomic_write_json(_index_path(puppet_root), doc)
    return doc


def match_pose_for_cue(
    puppet_root: Path,
    text: str = "",
    mood: str = "",
    expr_hint: str = "",
) -> Optional[str]:
    """Return a poseId whose triggerMoods or triggerKeywords match the cue, if any."""
    doc = _read_index(puppet_root)
    poses = doc.get("poses") or []
    if len(poses) <= 1:
        return None
    hay_mood = f"{expr_hint} {mood}".lower().strip()
    hay_text = f"{hay_mood} {text}".lower().strip()
    for p in poses:
        pid = str(p.get("id") or "")
        for m in p.get("triggerMoods") or []:
            tok = str(m).strip().lower()
            if tok and tok in hay_mood:
                return pid
        for kw in p.get("triggerKeywords") or []:
            tok = str(kw).strip().lower()
            if tok and tok in hay_text:
                return pid
    return None


def update_puppet_rig(
    puppet_root: Path,
    pose_id: str,
    anchors_patch: Optional[Dict[str, Any]] = None,
    subjects_patch: Optional[List[Dict[str, Any]]] = None,
    layer_patches: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist visual rigging edits (anchors, subjects, layer zIndex/rig/parallax/physics)."""
    assert_safe_data_path(puppet_root)
    pdir = pose_dir_for(puppet_root, pose_id)
    manifest_path = pdir / "puppet.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("未找到 Live2D 模型配置")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if anchors_patch and isinstance(anchors_patch, dict):
        anchors = dict(manifest.get("anchors") or {})
        for key in (
            "headPivot", "faceCenter", "eyeL", "eyeR",
            "mouthCenter", "mouthSize", "chestCenter",
            "shoulderL", "shoulderR", "waistCenter",
        ):
            val = anchors_patch.get(key)
            if isinstance(val, (list, tuple)) and len(val) == 2:
                anchors[key] = [round(float(val[0]), 5), round(float(val[1]), 5)]
        if "headTilt" in anchors_patch and anchors_patch["headTilt"] is not None:
            anchors["headTilt"] = round(float(anchors_patch["headTilt"]), 5)
        manifest["anchors"] = anchors

    if subjects_patch is not None and isinstance(subjects_patch, list):
        manifest["subjects"] = subjects_patch

    if layer_patches and isinstance(layer_patches, list):
        by_id = {str(p.get("id")): p for p in layer_patches if p.get("id")}
        for layer in manifest.get("layers") or []:
            patch = by_id.get(str(layer.get("id")))
            if not patch:
                continue
            if "zIndex" in patch and patch["zIndex"] is not None:
                layer["zIndex"] = float(patch["zIndex"])
            if "parallaxX" in patch and patch["parallaxX"] is not None:
                layer["parallaxX"] = float(patch["parallaxX"])
            if "parallaxY" in patch and patch["parallaxY"] is not None:
                layer["parallaxY"] = float(patch["parallaxY"])
            if "physics" in patch and patch["physics"] is not None:
                layer["physics"] = max(0.0, float(patch["physics"]))
            if "rig" in patch and patch["rig"]:
                layer["rig"] = str(patch["rig"])
            if "subjectId" in patch:
                if patch["subjectId"]:
                    layer["subjectId"] = str(patch["subjectId"])
                else:
                    layer.pop("subjectId", None)
        manifest["layers"] = sorted(
            manifest.get("layers") or [],
            key=lambda x: float(x.get("zIndex", 0)),
        )

    _atomic_write_json(manifest_path, manifest)
    return manifest

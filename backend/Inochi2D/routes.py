"""HTTP routes for the Inochi2D / 2D Puppet subsystem."""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .. import companion_db
from ..charx import classify_media_kind, infer_ext, infer_mime
from ..paths import COMPANION_ASSETS_DIR, DATA_ROOT
from . import service

router = APIRouter(prefix="/inochi2d", tags=["inochi2d"])

_ALLOWED_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class BuildRequest(BaseModel):
    companionId: str
    sourceRelPath: Optional[str] = None
    resolution: int = 768
    steps: int = 20
    seed: int = 42
    noCompleteEye: bool = False
    poseId: str = ""
    poseLabel: str = ""


class ConfigRequest(BaseModel):
    companionId: str
    config: Dict[str, Any]


class PoseSelectRequest(BaseModel):
    companionId: str
    poseId: str


class PoseMetaRequest(BaseModel):
    companionId: str
    poseId: str
    label: Optional[str] = None
    triggerMoods: Optional[List[str]] = None
    triggerKeywords: Optional[List[str]] = None


class RigUpdateRequest(BaseModel):
    companionId: str
    poseId: str = ""
    anchors: Optional[Dict[str, Any]] = None
    subjects: Optional[List[Dict[str, Any]]] = None
    layers: Optional[List[Dict[str, Any]]] = None


@router.get("/status")
async def inochi_status(refresh: bool = Query(False)):
    return service.get_environment_status(refresh=refresh)


@router.get("/puppet")
async def get_puppet(
    companion_id: str = Query(..., alias="companionId"),
    pose_id: str = Query("", alias="poseId"),
):
    return {
        "companionId": companion_id,
        "env": service.get_environment_status(),
        "job": service.get_job_state(companion_id),
        "puppet": service.load_puppet_manifest(companion_id, pose_id=pose_id),
        "posesIndex": service.list_poses(companion_id),
        "config": service.load_puppet_config(companion_id),
    }


@router.post("/build")
async def build_puppet(req: BuildRequest):
    try:
        src = service.resolve_source_image(req.companionId, req.sourceRelPath)
        job = await service.start_build_job(
            companion_id=req.companionId,
            source_path=src,
            resolution=req.resolution,
            steps=req.steps,
            seed=req.seed,
            no_complete_eye=req.noCompleteEye,
            pose_id=req.poseId,
            pose_label=req.poseLabel,
        )
        return {"job": job}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/build-upload")
async def build_puppet_upload(
    companionId: str = Form(...),
    resolution: int = Form(768),
    steps: int = Form(20),
    seed: int = Form(42),
    poseId: str = Form(""),
    poseLabel: str = Form(""),
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "portrait.png").suffix.lower()
    if suffix not in _ALLOWED_IMG_EXT:
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/webp/bmp 图片")
    pdir = service.companion_puppet_dir(companionId)
    pdir.mkdir(parents=True, exist_ok=True)
    dest = pdir / f"source_upload_{uuid.uuid4().hex[:6]}{suffix}"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传图片为空")
    dest.write_bytes(data)
    try:
        job = await service.start_build_job(
            companion_id=companionId,
            source_path=dest,
            resolution=resolution,
            steps=steps,
            seed=seed,
            pose_id=poseId,
            pose_label=poseLabel or Path(file.filename or "").stem,
        )
        return {"job": job}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _clean_stem(raw: str, fallback: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (raw or "").strip()).strip(". ")
    return s[:64] or fallback


@router.post("/build-pack")
async def build_puppet_pack(
    companionId: str = Form(...),
    mode: str = Form("base"),
    outfitId: str = Form(""),
    outfitLabel: str = Form(""),
    poseId: str = Form(""),
    poseLabel: str = Form(""),
    baseIndex: int = Form(0),
    labelsJson: str = Form("[]"),
    assetIdsJson: str = Form("[]"),
    saveToAssets: bool = Form(True),
    resolution: int = Form(768),
    steps: int = Form(20),
    seed: int = Form(42),
    noCompleteEye: bool = Form(False),
    files: List[UploadFile] = File(default=[]),
):
    """Build a full multi-expression puppet OR incrementally add one switchable outfit
    from uploaded images and/or existing ``companion_assets`` images.
    """
    if not companion_db.get_companion(companionId):
        raise HTTPException(status_code=404, detail="Companion not found")

    try:
        custom_labels = json.loads(labelsJson or "[]")
        asset_ids = json.loads(assetIdsJson or "[]")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"无效的 JSON 参数: {exc}")

    if not isinstance(custom_labels, list):
        custom_labels = []
    if not isinstance(asset_ids, list):
        asset_ids = []

    pdir = service.companion_puppet_dir(companionId)
    pack_dir = pdir / "_work" / "pack_src"
    if pack_dir.exists():
        shutil.rmtree(pack_dir, ignore_errors=True)
    pack_dir.mkdir(parents=True, exist_ok=True)

    staged: List[Path] = []
    existing_count = len(companion_db.get_companion_assets(companionId))

    # 1) Stage selected existing companion_assets images
    for idx, aid in enumerate(asset_ids):
        try:
            src_path, asset_name = service.resolve_asset_image(companionId, int(aid))
        except (ValueError, TypeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        raw_lbl = str(custom_labels[idx]) if idx < len(custom_labels) and custom_labels[idx] else asset_name
        stem = _clean_stem(raw_lbl, f"expr_{idx:02d}")
        dest = pack_dir / f"{stem}{src_path.suffix.lower() or '.png'}"
        if dest.exists():
            dest = pack_dir / f"{stem}_{idx:02d}{src_path.suffix.lower() or '.png'}"
        shutil.copy2(src_path, dest)
        staged.append(dest)

    # 2) Stage newly uploaded files (optionally persist to companion_assets without altering card_format)
    offset = len(staged)
    for fidx, uf in enumerate(files):
        orig_name = uf.filename or f"upload_{fidx}.png"
        suffix = Path(orig_name).suffix.lower()
        if suffix not in _ALLOWED_IMG_EXT:
            raise HTTPException(status_code=400, detail=f"不支持的图片格式: {orig_name}")
        data = await uf.read()
        if not data:
            continue

        lbl_idx = offset + fidx
        raw_lbl = (
            str(custom_labels[lbl_idx])
            if lbl_idx < len(custom_labels) and custom_labels[lbl_idx]
            else Path(orig_name).stem
        )
        stem = _clean_stem(raw_lbl, f"expr_{lbl_idx:02d}")
        dest = pack_dir / f"{stem}{suffix}"
        if dest.exists():
            dest = pack_dir / f"{stem}_{lbl_idx:02d}{suffix}"
        dest.write_bytes(data)
        staged.append(dest)

        if saveToAssets:
            ext = infer_ext(orig_name, "png")
            semantic_type = classify_media_kind(ext, "emotion")
            target_dir = COMPANION_ASSETS_DIR / companionId / semantic_type
            target_dir.mkdir(parents=True, exist_ok=True)
            asset_disk = target_dir / f"{uuid.uuid4().hex[:10]}.{ext}"
            asset_disk.write_bytes(data)
            rel_path = asset_disk.resolve().relative_to(DATA_ROOT.resolve()).as_posix()
            companion_db.add_companion_asset(companionId, {
                "asset_type": semantic_type,
                "name": stem,
                "ext": ext,
                "mime_type": infer_mime(ext),
                "rel_path": rel_path,
                "source_uri": f"embeded://assets/{semantic_type}/{asset_disk.name}",
                "size_bytes": len(data),
                "sort_order": existing_count + fidx,
            })

    if not staged:
        raise HTTPException(status_code=400, detail="请至少选择或上传 1 张图片")

    chosen_idx = max(0, min(len(staged) - 1, int(baseIndex)))
    base_src = staged[chosen_idx]

    is_outfit = mode == "outfit" and (pdir / "puppet.json").is_file()
    clean_oid = ""
    clean_olabel = ""
    if is_outfit:
        raw_oid = outfitId.strip() or outfitLabel.strip()
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_oid).strip("_-")
        clean_oid = sanitized or f"outfit_{uuid.uuid4().hex[:6]}"
        clean_olabel = outfitLabel.strip() or clean_oid

    try:
        job = await service.start_build_job(
            companion_id=companionId,
            source_path=base_src,
            resolution=resolution,
            steps=steps,
            seed=seed,
            expr_dir=None if is_outfit else pack_dir,
            base_name=base_src.stem,
            outfit_id=clean_oid,
            outfit_label=clean_olabel,
            no_complete_eye=noCompleteEye,
            pose_id=poseId,
            pose_label=poseLabel,
        )
        return {"job": job}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/poses")
async def list_puppet_poses(companion_id: str = Query(..., alias="companionId")):
    return service.list_poses(companion_id)


@router.post("/poses/select")
async def select_puppet_pose(req: PoseSelectRequest):
    try:
        return service.select_pose(req.companionId, req.poseId)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/poses/meta")
async def update_puppet_pose_meta(req: PoseMetaRequest):
    try:
        idx = service.update_pose_meta(
            req.companionId,
            req.poseId,
            label=req.label,
            trigger_moods=req.triggerMoods,
            trigger_keywords=req.triggerKeywords,
        )
        return {"posesIndex": idx}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/poses")
async def delete_puppet_pose(
    companion_id: str = Query(..., alias="companionId"),
    pose_id: str = Query(..., alias="poseId"),
):
    try:
        return service.delete_pose(companion_id, pose_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/import-psd")
async def import_layered_psd_route(
    companionId: str = Form(...),
    poseId: str = Form(""),
    poseLabel: str = Form(""),
    file: UploadFile = File(...),
):
    orig = file.filename or "model.psd"
    if Path(orig).suffix.lower() != ".psd":
        raise HTTPException(status_code=400, detail="请上传 .psd 分层文件")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    pdir = service.companion_puppet_dir(companionId)
    tmp_dir = pdir / "_work"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_psd = tmp_dir / f"import_{uuid.uuid4().hex[:8]}.psd"
    tmp_psd.write_bytes(data)
    try:
        return service.import_psd_puppet(
            companion_id=companionId,
            psd_path=tmp_psd,
            pose_id=poseId,
            pose_label=poseLabel or Path(orig).stem,
            source_name=orig,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        tmp_psd.unlink(missing_ok=True)


@router.post("/import-inx")
async def import_inx_route(
    companionId: str = Form(...),
    poseId: str = Form(""),
    poseLabel: str = Form(""),
    file: UploadFile = File(...),
):
    orig = file.filename or "model.inx"
    if Path(orig).suffix.lower() != ".inx":
        raise HTTPException(status_code=400, detail="请上传 Inochi2D .inx 模型文件")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    pdir = service.companion_puppet_dir(companionId)
    tmp_dir = pdir / "_work"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_inx = tmp_dir / f"import_{uuid.uuid4().hex[:8]}.inx"
    tmp_inx.write_bytes(data)
    try:
        return service.import_inx_puppet(
            companion_id=companionId,
            inx_path=tmp_inx,
            pose_id=poseId,
            pose_label=poseLabel or Path(orig).stem,
            source_name=orig,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        tmp_inx.unlink(missing_ok=True)


@router.put("/rig")
async def update_puppet_rig_route(req: RigUpdateRequest):
    try:
        manifest = service.save_puppet_rig(
            companion_id=req.companionId,
            pose_id=req.poseId,
            anchors_patch=req.anchors,
            subjects_patch=req.subjects,
            layer_patches=req.layers,
        )
        return {"puppet": manifest}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/config")
async def update_puppet_config(req: ConfigRequest):
    saved = service.save_puppet_config(req.companionId, req.config)
    return {"config": saved}


@router.delete("/outfit")
async def delete_outfit(
    companion_id: str = Query(..., alias="companionId"),
    outfit_id: str = Query(..., alias="outfitId"),
    pose_id: str = Query("", alias="poseId"),
):
    try:
        manifest = service.delete_puppet_outfit(companion_id, outfit_id, pose_id=pose_id)
        return {"puppet": manifest, "config": service.load_puppet_config(companion_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/cancel")
async def cancel_build(companion_id: str = Query(..., alias="companionId")):
    job = await service.cancel_build_job(companion_id)
    return {"job": job}

"""Orchestrator for See-through 2D puppet decomposition and per-companion configs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import companion_db
from ..paths import DATA_ROOT, UPLOAD_ROOT, inochi2d_dir, load_app_config, models_root

logger = logging.getLogger(__name__)

INOCHI_UPLOAD_DIR = UPLOAD_ROOT / "inochi2d"


def _runtime_dir() -> Path:
    return inochi2d_dir()


def _persistent_requirements_path(pkg_dir: Path) -> Path:
    bundled_req = pkg_dir / "requirements.txt"
    if getattr(sys, "frozen", False) and bundled_req.is_file():
        dst = DATA_ROOT / "inochi2d-requirements.txt"
        try:
            src_text = bundled_req.read_text(encoding="utf-8")
            if not dst.is_file() or dst.read_text(encoding="utf-8") != src_text:
                dst.write_text(src_text, encoding="utf-8")
            return dst
        except OSError:
            pass
    return bundled_req

_JOB_LOCK = asyncio.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_PROCS: Dict[str, asyncio.subprocess.Process] = {}
_PROBE_CACHE: Dict[Tuple[str, float, int], Dict[str, Any]] = {}

DEFAULT_PUPPET_CONFIG: Dict[str, Any] = {
    "motionEnabled": True,
    "breathAmp": 1.0,
    "breathSpeed": 1.0,
    "swayAmp": 0.9,
    "headParallax": 1.0,
    "eyeTrack": True,
    "blinkRate": 1.0,
    "mouthSens": 1.2,
    "physicsStrength": 1.0,
    "physicsDamping": 0.85,
    "emotion": "neutral",
    "outfit": "",
    "testMouth": 0.0,
    "hiddenLayers": [],
}

_REQUIRED_PROBE_MODULES = [
    "torch",
    "torchvision",
    "diffusers",
    "transformers",
    "accelerate",
    "huggingface_hub",
    "safetensors",
    "timm",
    "einops",
    "kornia",
    "omegaconf",
    "cv2",
    "PIL",
    "numpy",
    "scipy",
    "skimage",
    "psd_tools",
    "pycocotools",
    "yaml",
]

_PROBE_SCRIPT = """
import importlib.util, json
mods = %s
missing = [m for m in mods if importlib.util.find_spec(m) is None]
rt = {"backend": "unknown", "gpuName": "", "vramGb": 0.0, "dtype": "fp32", "torchVersion": "", "offloadDefault": True}
if "torch" not in missing:
    import torch
    rt["torchVersion"] = str(getattr(torch, "__version__", ""))
    cuda_ok = bool(torch.cuda.is_available())
    mps_ok = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
    if cuda_ok:
        is_rocm = bool(getattr(torch.version, "hip", None))
        rt["backend"] = "rocm" if is_rocm else "cuda"
        major = 0
        try:
            props = torch.cuda.get_device_properties(0)
            rt["gpuName"] = str(getattr(props, "name", "") or "")
            rt["vramGb"] = round(float(props.total_memory) / (1024.0 ** 3), 1)
            major = int(getattr(props, "major", 0))
        except Exception:
            pass
        bf16 = bool(torch.cuda.is_bf16_supported()) if hasattr(torch.cuda, "is_bf16_supported") else False
        rt["dtype"] = "bf16" if ((is_rocm and bf16) or (not is_rocm and major >= 8 and bf16)) else "fp16"
        rt["offloadDefault"] = rt["vramGb"] <= 0.0 or rt["vramGb"] < 20.0
    elif mps_ok:
        rt["backend"] = "mps"
        rt["gpuName"] = "Apple Silicon (MPS)"
        rt["dtype"] = "fp16"
        rt["offloadDefault"] = False
    else:
        rt["backend"] = "cpu"
        rt["dtype"] = "fp32"
        rt["offloadDefault"] = False
print(json.dumps({"missingDeps": missing, "runtime": rt}))
""" % json.dumps(_REQUIRED_PROBE_MODULES)


def invalidate_env_cache() -> None:
    _PROBE_CACHE.clear()


def _safe_companion_dir(companion_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(companion_id or "").strip())
    return clean or "default"


def companion_puppet_dir(companion_id: str) -> Path:
    return INOCHI_UPLOAD_DIR / _safe_companion_dir(companion_id)


def _resolve_python_from_path(raw: str) -> Optional[Path]:
    text = (raw or "").strip()
    if not text:
        return None
    p = Path(text).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        for sub in (
            p / "Scripts" / "python.exe",
            p / ".venv" / "Scripts" / "python.exe",
            p / "venv" / "Scripts" / "python.exe",
            p / "bin" / "python",
            p / ".venv" / "bin" / "python",
            p / "venv" / "bin" / "python",
            p / "python.exe",
            p / "python",
        ):
            if sub.is_file():
                return sub
    return None


def _find_python() -> Optional[Path]:
    configured = str(load_app_config().get("inochiPythonPath") or "").strip()
    if configured:
        hit = _resolve_python_from_path(configured)
        if hit is not None:
            return hit

    from_env = os.getenv("PEBBLE_INOCHI_PYTHON", "").strip()
    if from_env:
        hit = _resolve_python_from_path(from_env)
        if hit is not None:
            return hit

    pkg_dir = _runtime_dir()
    for candidate in (
        pkg_dir / ".venv" / "Scripts" / "python.exe",
        pkg_dir / "venv" / "Scripts" / "python.exe",
        pkg_dir / ".venv" / "bin" / "python",
        pkg_dir / "venv" / "bin" / "python",
        DATA_ROOT / "inochi2d-venv" / "Scripts" / "python.exe",
        DATA_ROOT / "inochi2d-venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return None


def _has_model_weights(model_dir: Path) -> bool:
    if not (model_dir / "model_index.json").is_file():
        return False
    return any(model_dir.rglob("*.safetensors")) or any(model_dir.rglob("*.bin"))


def _find_model_dir(names: List[str]) -> Optional[Path]:
    mroot = models_root()
    pkg_dir = _runtime_dir()
    bases: List[Path] = []
    for base in (
        mroot,
        mroot / "see-through",
        mroot / "inochi2d",
        mroot / "live2d",
        pkg_dir / "models",
        DATA_ROOT / "models",
        Path(r"E:\dearpebble\models") if os.name == "nt" else None,
    ):
        if base is not None and base not in bases:
            bases.append(base)

    for base in bases:
        if not base.exists():
            continue
        for name in names:
            p = base / name
            if p.is_dir() and _has_model_weights(p):
                return p
    return None


def _probe_python_env(py: Path) -> Dict[str, Any]:
    try:
        st = py.stat()
        key = (str(py), st.st_mtime, st.st_size)
    except OSError:
        return {"missingDeps": list(_REQUIRED_PROBE_MODULES), "runtime": {}}

    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {"missingDeps": [], "runtime": {}}
    try:
        proc = subprocess.run(
            [str(py), "-c", _PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
            if isinstance(parsed, dict):
                result = parsed
        else:
            result = {"missingDeps": ["probe_failed"], "runtime": {}}
    except Exception as exc:
        logger.debug("Inochi2D python probe failed: %s", exc)
        result = {"missingDeps": ["probe_failed"], "runtime": {}}

    _PROBE_CACHE[key] = result
    return result


def get_environment_status(refresh: bool = False) -> Dict[str, Any]:
    if refresh:
        invalidate_env_cache()

    py = _find_python()
    mroot = models_root()
    pkg_dir = _runtime_dir()
    seethrough_dir = pkg_dir / "see-through"
    layerdiff_dir = _find_model_dir(["seethroughv0.0.2_layerdiff3d"])
    marigold_dir = _find_model_dir(["seethroughv0.0.1_marigold", "seethroughv0.0.1_marigol"])

    probe = _probe_python_env(py) if py is not None else {"missingDeps": [], "runtime": {}}
    missing_deps: List[str] = list(probe.get("missingDeps") or [])
    runtime: Dict[str, Any] = dict(probe.get("runtime") or {})

    missing: List[str] = []
    expected_venv = DATA_ROOT / "inochi2d-venv"
    if py is None:
        missing.append(str(expected_venv))
    elif missing_deps:
        missing.append(f"pip deps ({', '.join(missing_deps[:5])}{'...' if len(missing_deps) > 5 else ''})")
    if not seethrough_dir.is_dir():
        missing.append("Inochi2D/see-through")
    if layerdiff_dir is None:
        missing.append("seethroughv0.0.2_layerdiff3d")
    if marigold_dir is None:
        missing.append("seethroughv0.0.1_marigold")

    req_file = _persistent_requirements_path(pkg_dir)
    return {
        "ready": len(missing) == 0,
        "pythonPath": str(py) if py else "",
        "configuredPythonPath": str(load_app_config().get("inochiPythonPath") or ""),
        "expectedVenvDir": str(expected_venv),
        "seethroughPath": str(seethrough_dir) if seethrough_dir.is_dir() else "",
        "modelsRoot": str(mroot),
        "layerdiffPath": str(layerdiff_dir) if layerdiff_dir else "",
        "marigoldPath": str(marigold_dir) if marigold_dir else "",
        "expectedLayerdiffDir": str(mroot / "seethroughv0.0.2_layerdiff3d"),
        "expectedMarigoldDir": str(mroot / "seethroughv0.0.1_marigold"),
        "requirementsPath": str(req_file),
        "missingDeps": missing_deps,
        "runtime": runtime,
        "missing": missing,
    }


def load_puppet_manifest(companion_id: str) -> Optional[Dict[str, Any]]:
    manifest_path = companion_puppet_dir(companion_id) / "puppet.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("failed to read puppet manifest: %s", manifest_path, exc_info=True)
        return None


def load_puppet_config(companion_id: str) -> Dict[str, Any]:
    cfg = dict(DEFAULT_PUPPET_CONFIG)
    cfg_path = companion_puppet_dir(companion_id) / "puppet_config.json"
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            logger.warning("failed to read puppet config: %s", cfg_path, exc_info=True)
    if not isinstance(cfg.get("hiddenLayers"), list):
        cfg["hiddenLayers"] = []
    return cfg


def save_puppet_config(companion_id: str, incoming: Dict[str, Any]) -> Dict[str, Any]:
    pdir = companion_puppet_dir(companion_id)
    pdir.mkdir(parents=True, exist_ok=True)
    merged = load_puppet_config(companion_id)
    for k in DEFAULT_PUPPET_CONFIG:
        if k in incoming:
            merged[k] = incoming[k]
    if not isinstance(merged.get("hiddenLayers"), list):
        merged["hiddenLayers"] = []
    merged["hiddenLayers"] = [str(x) for x in merged["hiddenLayers"]]

    cfg_path = pdir / "puppet_config.json"
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)
    return merged


def delete_puppet_outfit(companion_id: str, outfit_id: str) -> Dict[str, Any]:
    pdir = companion_puppet_dir(companion_id)
    manifest_path = pdir / "puppet.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("未找到 Live2D 模型配置")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outfits: List[Dict[str, Any]] = list(manifest.get("outfits") or [])
    if len(outfits) <= 1 or not any(o.get("id") == outfit_id for o in outfits):
        raise ValueError("至少需要保留一套基础服装，或该服装不存在")

    manifest["outfits"] = [o for o in outfits if o.get("id") != outfit_id]
    manifest["layers"] = [l for l in manifest.get("layers", []) if l.get("outfit") != outfit_id]

    outfit_dir = pdir / "outfits" / re.sub(r"[^A-Za-z0-9_-]+", "_", outfit_id)
    if outfit_dir.is_dir():
        shutil.rmtree(outfit_dir, ignore_errors=True)

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)

    cfg = load_puppet_config(companion_id)
    if cfg.get("outfit") == outfit_id:
        next_oid = manifest["outfits"][0]["id"] if manifest["outfits"] else ""
        save_puppet_config(companion_id, {"outfit": next_oid})

    return manifest


def get_job_state(companion_id: str) -> Dict[str, Any]:
    return dict(
        _JOBS.get(
            companion_id,
            {"status": "idle", "stage": "", "progress": 0, "error": ""},
        )
    )


def resolve_source_image(companion_id: str, source_rel_path: Optional[str] = None) -> Path:
    candidates: List[str] = []
    if source_rel_path and source_rel_path.strip():
        candidates.append(source_rel_path.strip())

    comp = companion_db.get_companion(companion_id)
    if comp and comp.get("avatar_path"):
        candidates.append(str(comp["avatar_path"]).strip())

    data_root_resolved = DATA_ROOT.resolve()
    uploads_resolved = UPLOAD_ROOT.resolve()
    for rel in candidates:
        cleaned = rel.lstrip("/\\")
        if cleaned.startswith("uploads/"):
            target = (data_root_resolved / cleaned).resolve()
        else:
            target = (uploads_resolved / cleaned).resolve()
        if target.is_relative_to(uploads_resolved) and target.is_file():
            return target

    raise FileNotFoundError("找不到可用于拆层的角色立绘图片")


def resolve_asset_image(companion_id: str, asset_id: int) -> Tuple[Path, str]:
    asset = companion_db.get_companion_asset(companion_id, asset_id)
    if not asset:
        raise FileNotFoundError(f"Asset {asset_id} not found")
    rel = str(asset.get("rel_path") or "").strip().lstrip("/\\")
    data_root_resolved = DATA_ROOT.resolve()
    uploads_resolved = UPLOAD_ROOT.resolve()
    target = (data_root_resolved / rel if rel.startswith("uploads/") else uploads_resolved / rel).resolve()
    if not target.is_relative_to(uploads_resolved) or not target.is_file():
        raise FileNotFoundError(f"Asset file missing: {rel}")
    return target, str(asset.get("name") or target.stem)


async def start_build_job(
    companion_id: str,
    source_path: Path,
    resolution: int = 768,
    steps: int = 20,
    seed: int = 42,
    expr_dir: Optional[Path] = None,
    base_name: str = "",
    outfit_id: str = "",
    outfit_label: str = "",
    no_complete_eye: bool = False,
) -> Dict[str, Any]:
    env = get_environment_status()
    if not env["ready"]:
        raise RuntimeError(f"2D 拆层环境未就绪，缺少: {', '.join(env['missing'])}")

    existing = _JOBS.get(companion_id)
    if existing and existing.get("status") == "running":
        return dict(existing)

    out_dir = companion_puppet_dir(companion_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy source image only for base builds so adding an outfit does not overwrite source portrait
    if outfit_id and (out_dir / "puppet.json").is_file():
        src_copy = source_path
    else:
        src_copy = out_dir / f"source{source_path.suffix.lower() or '.png'}"
        if source_path.resolve() != src_copy.resolve():
            shutil.copy2(source_path, src_copy)

    job = {
        "status": "running",
        "stage": "queued",
        "progress": 5,
        "error": "",
        "startedAt": time.time(),
    }
    _JOBS[companion_id] = job

    asyncio.create_task(
        _run_worker(
            companion_id=companion_id,
            src_copy=src_copy,
            out_dir=out_dir,
            env=env,
            resolution=max(512, min(1280, int(resolution))),
            steps=max(10, min(40, int(steps))),
            seed=int(seed),
            expr_dir=expr_dir,
            base_name=base_name,
            outfit_id=outfit_id,
            outfit_label=outfit_label,
            no_complete_eye=no_complete_eye,
        )
    )
    return dict(job)


async def _run_worker(
    companion_id: str,
    src_copy: Path,
    out_dir: Path,
    env: Dict[str, Any],
    resolution: int,
    steps: int,
    seed: int,
    expr_dir: Optional[Path] = None,
    base_name: str = "",
    outfit_id: str = "",
    outfit_label: str = "",
    no_complete_eye: bool = False,
) -> None:
    async with _JOB_LOCK:
        job = _JOBS.get(companion_id)
        if not job or job.get("status") != "running":
            return

        work_dir = out_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)
        # Preserve staged pack_src if expr_dir lives inside work_dir, clean older run artifacts
        for item in work_dir.iterdir():
            if expr_dir and item.resolve() == expr_dir.resolve():
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)

        rel_prefix = f"uploads/inochi2d/{_safe_companion_dir(companion_id)}"
        cmd = [
            env["pythonPath"],
            str(_runtime_dir() / "run_decompose.py"),
            "--srcp",
            str(src_copy),
            "--save_dir",
            str(work_dir),
            "--layerdiff_model",
            env["layerdiffPath"],
            "--marigold_model",
            env["marigoldPath"],
            "--resolution",
            str(resolution),
            "--inference_steps",
            str(steps),
            "--seed",
            str(seed),
            "--offload",
            "auto",
            "--puppet_out",
            str(out_dir),
            "--rel_prefix",
            rel_prefix,
        ]
        if expr_dir is not None:
            cmd.extend(["--expr_dir", str(expr_dir)])
        if base_name:
            cmd.extend(["--base_name", base_name])
        if outfit_id and (out_dir / "puppet.json").is_file():
            cmd.extend(["--outfit_id", outfit_id])
            if outfit_label:
                cmd.extend(["--outfit_label", outfit_label])
        if no_complete_eye:
            cmd.append("--no_complete_eye")

        job["stage"] = "loading"
        job["progress"] = 10

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            _PROCS[companion_id] = proc
            tail_lines: List[str] = []
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    tail_lines.append(line)
                    if len(tail_lines) > 30:
                        tail_lines.pop(0)
                    if "Running LayerDiff3D" in line:
                        job["stage"] = "layerdiff"
                        job["progress"] = 25
                    elif "Running Marigold depth" in line:
                        job["stage"] = "depth"
                        job["progress"] = 65
                    elif "Extracting layered output" in line:
                        job["stage"] = "extract"
                        job["progress"] = 80
                    elif "Building puppet manifest" in line:
                        job["stage"] = "puppet"
                        job["progress"] = 90
                    elif "Extracting blink" in line or "Merging outfit layers" in line:
                        job["stage"] = "postprocess"
                        job["progress"] = 95

            rc = await proc.wait()
            _PROCS.pop(companion_id, None)
            if rc != 0:
                err_msg = "\n".join(tail_lines[-8:]) or f"拆层进程退出码 {rc}"
                job["status"] = "error"
                job["stage"] = "failed"
                job["error"] = err_msg[-600:]
                return

            if outfit_id:
                save_puppet_config(companion_id, {"outfit": outfit_id})

            # Remove temporary PSD / inference artifacts once puppet files are written
            shutil.rmtree(work_dir, ignore_errors=True)

            job["status"] = "done"
            job["stage"] = "done"
            job["progress"] = 100
            job["error"] = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _PROCS.pop(companion_id, None)
            logger.exception("inochi2d worker failed")
            job["status"] = "error"
            job["stage"] = "failed"
            job["error"] = str(exc)


async def cancel_build_job(companion_id: str) -> Dict[str, Any]:
    proc = _PROCS.pop(companion_id, None)
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    job = _JOBS.get(companion_id)
    if job and job.get("status") == "running":
        job["status"] = "idle"
        job["stage"] = "cancelled"
        job["progress"] = 0
        job["error"] = ""
    return get_job_state(companion_id)

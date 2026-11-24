"""Standalone worker script to run See-through layer decomposition and puppet assembly.

Executed inside an isolated Python venv (e.g. ``backend/Inochi2D/.venv`` or a user-configured
conda/venv) so PyTorch and diffusion dependencies stay isolated from the core backend and
release all VRAM/RAM when decomposition finishes.

Supports NVIDIA CUDA (Ampere+ bf16 & Turing/Pascal fp16), AMD ROCm (Windows/Linux),
Apple Silicon MPS, and CPU fallback without requiring upstream edits to ``see-through/``.
"""
from __future__ import annotations

import argparse
import gc
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

_ORIG_SDPA = F.scaled_dot_product_attention


def _detect_runtime(requested_device: str = "auto", requested_dtype: str = "auto") -> dict:
    req_dev = (requested_device or "auto").strip().lower()
    req_dt = (requested_dtype or "auto").strip().lower()

    cuda_ok = torch.cuda.is_available()
    mps_ok = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())

    if req_dev in ("cuda", "rocm") and cuda_ok:
        device = "cuda"
    elif req_dev == "mps" and mps_ok:
        device = "mps"
    elif req_dev == "cpu":
        device = "cpu"
    elif cuda_ok:
        device = "cuda"
    elif mps_ok:
        device = "mps"
    else:
        device = "cpu"

    is_rocm = device == "cuda" and bool(getattr(torch.version, "hip", None))
    backend = "rocm" if is_rocm else device
    gpu_name = ""
    vram_gb = 0.0
    major = 0

    if device == "cuda":
        try:
            props = torch.cuda.get_device_properties(0)
            gpu_name = str(getattr(props, "name", "") or "")
            vram_gb = round(float(props.total_memory) / (1024.0 ** 3), 2)
            major = int(getattr(props, "major", 0))
        except Exception:
            pass
    elif device == "mps":
        gpu_name = "Apple Silicon (MPS)"

    # Pick optimal dtype per hardware generation:
    # - CPU: fp32 (fastest & avoids broken cpu half kernels)
    # - MPS: fp16 (Apple MPS supports fp16 broadly; bf16 varies across macOS versions)
    # - ROCm: bf16 when supported (prevents SDXL fp16 VAE NaN/overflow)
    # - NVIDIA CUDA: bf16 on Ampere+ (sm_80+), fp16 on Turing/Volta/Pascal (sm_60..sm_75)
    if req_dt in ("bf16", "bfloat16"):
        dtype = torch.bfloat16
    elif req_dt in ("fp16", "float16"):
        dtype = torch.float16
    elif req_dt in ("fp32", "float32"):
        dtype = torch.float32
    elif device == "cpu":
        dtype = torch.float32
    elif device == "mps":
        dtype = torch.float16
    elif is_rocm:
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.bfloat16 if (major >= 8 and torch.cuda.is_bf16_supported()) else torch.float16

    return {
        "device": device,
        "backend": backend,
        "is_rocm": is_rocm,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
        "dtype": dtype,
        "dtype_name": {torch.bfloat16: "bf16", torch.float16: "fp16", torch.float32: "fp32"}[dtype],
    }


def _configure_sdpa(runtime: dict) -> None:
    """Use chunked math SDPA on ROCm to avoid hipErrorInvalidValue & 12GB OOM; keep native SDPA on CUDA."""
    force_math = os.getenv("PEBBLE_FORCE_MATH_SDPA", "").strip() == "1"
    if not runtime["is_rocm"] and not force_math:
        return

    if hasattr(torch.backends, "cuda") and runtime["device"] == "cuda":
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)

    def _chunked_math_sdpa(
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        attn_mask=None,
        dropout_p: float = 0.0,
        is_causal: bool = False,
        scale=None,
        chunk_size: int = 512,
        batch_chunk_size: int = 4,
        **kwargs,
    ) -> torch.Tensor:
        lq = query.shape[-2]
        if attn_mask is not None or is_causal:
            return _ORIG_SDPA(
                query, key, value, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, scale=scale, **kwargs
            )
        batch = query.shape[0] if query.ndim >= 3 else 1
        if lq <= chunk_size and (query.ndim < 3 or batch <= batch_chunk_size):
            return _ORIG_SDPA(
                query, key, value, attn_mask=None, dropout_p=dropout_p, is_causal=False, scale=scale, **kwargs
            )

        def _run_q_chunks(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
            if q.shape[-2] <= chunk_size:
                return _ORIG_SDPA(q, k, v, attn_mask=None, dropout_p=dropout_p, is_causal=False, scale=scale, **kwargs)
            q_chunks = []
            for start in range(0, q.shape[-2], chunk_size):
                end = min(start + chunk_size, q.shape[-2])
                q_chunks.append(
                    _ORIG_SDPA(
                        q[..., start:end, :],
                        k,
                        v,
                        attn_mask=None,
                        dropout_p=dropout_p,
                        is_causal=False,
                        scale=scale,
                        **kwargs,
                    )
                )
            return torch.cat(q_chunks, dim=-2)

        if query.ndim >= 3 and batch > batch_chunk_size:
            b_chunks = []
            for b_start in range(0, batch, batch_chunk_size):
                b_end = min(b_start + batch_chunk_size, batch)
                k_slice = key[b_start:b_end] if key.shape[0] == batch else key
                v_slice = value[b_start:b_end] if value.shape[0] == batch else value
                b_chunks.append(_run_q_chunks(query[b_start:b_end], k_slice, v_slice))
            return torch.cat(b_chunks, dim=0)

        return _run_q_chunks(query, key, value)

    F.scaled_dot_product_attention = _chunked_math_sdpa


def _empty_device_cache(device: str) -> None:
    gc.collect()
    if device == "cuda" and torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device == "mps" and getattr(torch, "mps", None) and hasattr(torch.mps, "empty_cache"):
        try:
            torch.mps.empty_cache()
        except Exception:
            pass


def _sync_device(device: str) -> None:
    if device == "cuda" and torch.cuda.is_available():
        torch.cuda.synchronize()
    elif device == "mps" and getattr(torch, "mps", None) and hasattr(torch.mps, "synchronize"):
        try:
            torch.mps.synchronize()
        except Exception:
            pass


def _patch_cross_device_shims(runtime: dict) -> None:
    """Redirect upstream See-through hardcoded ``'cuda'`` / ``torch.bfloat16`` cleanly at runtime."""
    target_device = runtime["device"]
    target_dtype = runtime["dtype"]

    if target_device != "cuda":
        # Upstream Marigold/inference_utils call torch.cuda.* directly without availability guards.
        torch.cuda.empty_cache = lambda: _empty_device_cache(target_device)  # type: ignore[assignment]
        torch.cuda.synchronize = lambda *a, **kw: _sync_device(target_device)  # type: ignore[assignment]
        torch.cuda.manual_seed_all = lambda *a, **kw: None  # type: ignore[assignment]
        torch.cuda.mem_get_info = lambda *a, **kw: (0, 0)  # type: ignore[assignment]
        torch.cuda.set_device = lambda *a, **kw: None  # type: ignore[assignment]

        orig_generator = torch.Generator

        def _safe_generator(device: Optional[str] = None):
            if device is not None and str(device).startswith("cuda"):
                return orig_generator(device=target_device)
            return orig_generator(device=device)

        torch.Generator = _safe_generator  # type: ignore[misc, assignment]

    if target_device != "cuda" or target_dtype != torch.bfloat16:
        orig_to = nn.Module.to

        def _mapped_to(self, *args, **kwargs):
            new_args = []
            for a in args:
                if isinstance(a, str) and a.startswith("cuda") and target_device != "cuda":
                    new_args.append(target_device)
                elif isinstance(a, torch.device) and a.type == "cuda" and target_device != "cuda":
                    new_args.append(torch.device(target_device))
                elif a is torch.bfloat16 and target_dtype != torch.bfloat16:
                    new_args.append(target_dtype)
                else:
                    new_args.append(a)
            if "device" in kwargs:
                dev = kwargs["device"]
                if (isinstance(dev, str) and dev.startswith("cuda")) or (
                    isinstance(dev, torch.device) and dev.type == "cuda"
                ):
                    if target_device != "cuda":
                        kwargs["device"] = target_device
            if kwargs.get("dtype") is torch.bfloat16 and target_dtype != torch.bfloat16:
                kwargs["dtype"] = target_dtype
            return orig_to(self, *new_args, **kwargs)

        nn.Module.to = _mapped_to  # type: ignore[method-assign]


def _patch_clip_eager() -> None:
    from transformers import CLIPTextModel, CLIPTextModelWithProjection

    for cls in (CLIPTextModel, CLIPTextModelWithProjection):
        orig = cls.from_pretrained.__func__

        @classmethod  # type: ignore[misc]
        def _wrapped_from_pretrained(subcls, *args, _orig=orig, **kwargs):
            kwargs.setdefault("attn_implementation", "eager")
            model = _orig(subcls, *args, **kwargs)
            if hasattr(model, "set_attn_implementation"):
                try:
                    model.set_attn_implementation("eager")
                except Exception:
                    pass
            return model

        cls.from_pretrained = _wrapped_from_pretrained  # type: ignore[assignment]


def _patch_pipeline_vram_swap(runtime: dict) -> None:
    """Keep active weights on target device/dtype and flush VRAM cleanly between stages."""
    from modules.layerdiffuse.diffusers_kdiffusion_sdxl import KDiffusionStableDiffusionXLPipeline
    from modules.layerdiffuse.vae import TransparentVAEDecoder
    from modules.marigold.marigold_depth_pipeline import MarigoldDepthPipeline

    dev = runtime["device"]
    dt = runtime["dtype"]

    orig_call = KDiffusionStableDiffusionXLPipeline.__call__

    def _wrapped_call(self, *args, **kwargs):
        if getattr(self, "unet", None) is not None:
            self.unet.to(dev, dtype=dt)
        if getattr(self, "vae", None) is not None:
            self.vae.to(dev, dtype=dt)
        if getattr(self, "trans_vae", None) is not None:
            self.trans_vae.to(dev, dtype=dt)
        _empty_device_cache(dev)
        out = orig_call(self, *args, **kwargs)
        _sync_device(dev)
        _empty_device_cache(dev)
        return out

    KDiffusionStableDiffusionXLPipeline.__call__ = _wrapped_call  # type: ignore[assignment]

    orig_dec = TransparentVAEDecoder.forward

    @torch.no_grad()
    def _wrapped_dec(self, sd_vae, latent, *args, **kwargs):
        sd_vae.to(dev, dtype=dt)
        self.to(dev, dtype=dt)
        return orig_dec(self, sd_vae, latent, *args, **kwargs)

    TransparentVAEDecoder.forward = _wrapped_dec  # type: ignore[assignment]

    orig_marigold = MarigoldDepthPipeline.single_infer

    @torch.no_grad()
    def _wrapped_marigold(self, cond_latent, num_inference_steps, generator):
        self.to(dev, dtype=dt)
        cond_latent = cond_latent.to(device=dev, dtype=dt)
        _empty_device_cache(dev)
        return orig_marigold(self, cond_latent, num_inference_steps, generator)

    MarigoldDepthPipeline.single_infer = _wrapped_marigold  # type: ignore[assignment]


def _setup_paths(seethrough_root: Path) -> None:
    common_dir = seethrough_root / "common"
    if str(common_dir) not in sys.path:
        sys.path.insert(0, str(common_dir))
    scripts_dir = seethrough_root / "inference" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _resolve_offload(mode: str, no_offload_flag: bool, runtime: dict) -> bool:
    if no_offload_flag or mode == "off":
        return False
    if runtime["device"] != "cuda":
        return False
    if mode == "on":
        return True
    # Auto: enable group_offload below 20 GB VRAM (e.g. 8/12/16 GB GPUs) to prevent OOM/stalls
    vram = float(runtime.get("vram_gb") or 0.0)
    return vram <= 0.0 or vram < 20.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run See-through layer decomposition")
    parser.add_argument("--srcp", required=True, help="Input image path")
    parser.add_argument("--save_dir", required=True, help="Output directory")
    parser.add_argument("--layerdiff_model", required=True, help="Local LayerDiff3D model directory")
    parser.add_argument("--marigold_model", required=True, help="Local Marigold depth model directory")
    parser.add_argument("--resolution", type=int, default=1024)
    parser.add_argument("--resolution_depth", type=int, default=768)
    parser.add_argument("--inference_steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "mps", "cpu"])
    parser.add_argument("--dtype", default="auto", choices=["auto", "bf16", "fp16", "fp32"])
    parser.add_argument("--offload", default="auto", choices=["auto", "on", "off"])
    parser.add_argument("--group_offload", action="store_true", default=True)
    parser.add_argument("--no_group_offload", action="store_true")
    parser.add_argument("--save_to_psd", action="store_true", default=True)
    parser.add_argument("--tblr_split", action="store_true", default=True)
    parser.add_argument("--puppet_out", default="", help="Optional output folder for web puppet.json + layer PNGs")
    parser.add_argument("--rel_prefix", default="", help="Relative URL prefix for puppet layer PNGs")
    parser.add_argument("--expr_dir", default="", help="Optional folder of same-pose expression PNGs")
    parser.add_argument("--base_name", default="", help="Stem of wide-eyed reference image in --expr_dir")
    parser.add_argument("--outfit_id", default="", help="If set, merge extracted body layers as an outfit")
    parser.add_argument("--outfit_label", default="", help="Display label for --outfit_id")
    parser.add_argument("--no_complete_eye", action="store_true", help="Disable Plan 1 blind-spot eye/brow completion")
    args = parser.parse_args()

    runtime = _detect_runtime(args.device, args.dtype)
    _configure_sdpa(runtime)
    _patch_cross_device_shims(runtime)

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    seethrough_root = here / "see-through"
    _setup_paths(seethrough_root)
    _patch_clip_eager()
    _patch_pipeline_vram_swap(runtime)
    os.chdir(seethrough_root)

    from utils.inference_utils import apply_layerdiff, apply_marigold, further_extr
    from utils.torch_utils import seed_everything

    use_offload = _resolve_offload(args.offload, args.no_group_offload, runtime)
    srcp = str(Path(args.srcp).resolve())
    save_dir = str(Path(args.save_dir).resolve())

    print(
        f"[Inochi2D] Runtime: device={runtime['device']} backend={runtime['backend']} "
        f"gpu={runtime['gpu_name'] or 'cpu'} vram={runtime['vram_gb']}GB "
        f"dtype={runtime['dtype_name']} offload={use_offload}",
        flush=True,
    )

    seed_everything(args.seed)
    print(f"[Inochi2D] Running LayerDiff3D on {srcp} (res={args.resolution}, steps={args.inference_steps}, offload={use_offload}) ...", flush=True)
    apply_layerdiff(
        srcp,
        args.layerdiff_model,
        save_dir=save_dir,
        seed=args.seed,
        resolution=args.resolution,
        num_inference_steps=args.inference_steps,
        group_offload=use_offload,
    )

    import utils.inference_utils as iu
    if getattr(iu, "layerdiff_pipeline", None) is not None:
        del iu.layerdiff_pipeline
        iu.layerdiff_pipeline = None
    _empty_device_cache(runtime["device"])

    print("[Inochi2D] Running Marigold depth ...", flush=True)
    apply_marigold(
        srcp,
        args.marigold_model,
        save_dir=save_dir,
        seed=args.seed,
        resolution=args.resolution_depth,
        group_offload=use_offload,
    )

    if getattr(iu, "marigold_pipeline", None) is not None:
        del iu.marigold_pipeline
        iu.marigold_pipeline = None
    _empty_device_cache(runtime["device"])

    srcname = Path(srcp).stem
    saved = os.path.join(save_dir, srcname)
    print(f"[Inochi2D] Extracting layered output to {saved} ...", flush=True)
    further_extr(saved, rotate=False, save_to_psd=args.save_to_psd, tblr_split=args.tblr_split)

    if args.puppet_out:
        puppet_dir = Path(args.puppet_out).resolve()
        rel_prefix = args.rel_prefix or "uploads/inochi2d"
        psd_path = Path(save_dir) / f"{srcname}.psd"
        from expression_extractor import add_outfit_from_psd, extract_expressions_and_blink

        if args.outfit_id and (puppet_dir / "puppet.json").is_file():
            print(f"[Inochi2D] Merging outfit layers ({args.outfit_id}) from {psd_path} ...", flush=True)
            add_outfit_from_psd(
                psd_path=psd_path,
                puppet_dir=puppet_dir,
                outfit_id=args.outfit_id,
                outfit_label=args.outfit_label or args.outfit_id,
                rel_prefix=rel_prefix,
            )
        else:
            from puppet_builder import build_puppet_from_psd

            print(f"[Inochi2D] Building puppet manifest from {psd_path} ...", flush=True)
            build_puppet_from_psd(
                psd_path=psd_path,
                out_dir=puppet_dir,
                rel_prefix=rel_prefix,
                source_image=Path(srcp),
            )
            print("[Inochi2D] Extracting blink & expression layers ...", flush=True)
            expr_dir = Path(args.expr_dir).resolve() if args.expr_dir else None
            base_name = args.base_name.strip() or Path(srcp).stem
            extract_expressions_and_blink(
                puppet_dir=puppet_dir,
                source_dir=expr_dir,
                base_name=base_name,
                rel_prefix=rel_prefix,
                complete_occluded_eye=not args.no_complete_eye,
            )

    print(f"[Inochi2D] Done: {saved}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# On AMD ROCm, default MIOpen exhaustive find mode spends 15-60s benchmarking
# Conv2d algorithms on first forward pass in every subprocess; FAST cuts warmup sharply.
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")

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
    """Use smart chunked math SDPA on ROCm to avoid hipErrorInvalidValue & 12GB OOM; keep native SDPA on CUDA.

    Note: On Windows ROCm (including RDNA4 / 7.14.1), both flash_sdp and mem_efficient_sdp can
    leave asynchronous hipErrorInvalidValue state across calls. Using math_sdp while skipping
    unnecessary Python chunking on small-sequence temporal/cross attention keeps both 100% stability
    and high throughput.
    """
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
        chunk_size: int = 1024,
        batch_chunk_size: int = 4,
        **kwargs,
    ) -> torch.Tensor:
        if attn_mask is not None or is_causal:
            return _ORIG_SDPA(
                query, key, value, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, scale=scale, **kwargs
            )
        lq = query.shape[-2]
        lk = key.shape[-2]
        batch = query.shape[0] if query.ndim >= 3 else 1
        # Temporal attention (batch=2304, lq=13, lk=13) and cross-attention (lk=77) have tiny Q*K matrices:
        # run in a single kernel instead of hundreds of Python loop iterations.
        if lq * lk <= 512 * 512:
            return _ORIG_SDPA(
                query, key, value, attn_mask=None, dropout_p=dropout_p, is_causal=False, scale=scale, **kwargs
            )
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


class _QuantLinear(nn.Module):
    """Pure-PyTorch symmetric per-output-channel INT8 weight-only Linear layer.

    Shrinks Linear weights by 2x vs bf16/fp16 (e.g. 6.96 GB -> 3.48 GB on LayerDiff3D UNet)
    with zero external dependencies (works natively on AMD ROCm, CUDA, MPS, and CPU).
    """

    def __init__(self, weight_q: torch.Tensor, weight_scale: torch.Tensor, bias: Optional[torch.Tensor]):
        super().__init__()
        self.register_buffer("weight_q", weight_q)
        self.register_buffer("weight_scale", weight_scale)
        if bias is not None:
            self.register_buffer("bias", bias)
        else:
            self.bias = None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        w = self.weight_q.to(dtype=x.dtype)
        y = F.linear(x, w, None) * self.weight_scale.to(dtype=x.dtype)
        if self.bias is not None:
            y = y + self.bias.to(dtype=x.dtype)
        return y


def _quantize_linears_int8(
    root: nn.Module,
    dtype: torch.dtype,
    min_numel: int = 65536,
    target_device: Optional[str] = None,
) -> Tuple[int, int]:
    """Replace large nn.Linear modules in-place to cut both RAM and VRAM usage.

    When ``target_device`` is provided (e.g. ``'cuda'``), each weight is quantized one layer
    at a time on the GPU and kept there directly, cutting quantization + transfer from ~16s to ~1-2s.
    """
    targets = []
    for name, mod in root.named_modules():
        if isinstance(mod, nn.Linear) and mod.weight is not None and mod.weight.numel() >= min_numel:
            targets.append((name, mod))

    replaced = 0
    saved_bytes = 0
    for idx, (full_name, mod) in enumerate(targets):
        parent_path, _, attr = full_name.rpartition(".")
        parent = root.get_submodule(parent_path) if parent_path else root
        with torch.no_grad():
            src_w = mod.weight.data.detach()
            orig_bytes = src_w.numel() * src_w.element_size()
            if target_device:
                w = src_w.to(device=target_device, dtype=torch.float32)
            else:
                w = src_w.to(dtype=torch.float32)
            scale = (w.abs().amax(dim=1).clamp_min(1e-8) / 127.0).to(dtype=dtype)
            w_q = torch.round(w / scale.to(torch.float32).unsqueeze(1)).clamp(-127, 127).to(torch.int8)
            bias = None
            if mod.bias is not None:
                bias = mod.bias.data.detach().to(device=target_device or mod.bias.device, dtype=dtype).clone()
            new_bytes = w_q.numel() * w_q.element_size() + scale.numel() * scale.element_size()
            del w, src_w

        qmod = _QuantLinear(w_q, scale, bias)
        setattr(parent, attr, qmod)
        mod.weight = None  # type: ignore[assignment]
        mod.bias = None
        replaced += 1
        saved_bytes += max(0, orig_bytes - new_bytes)
        if (idx + 1) % 128 == 0:
            gc.collect()

    gc.collect()
    return replaced, saved_bytes


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


def _patch_pipeline_vram_swap(runtime: dict, offload_active: bool = False) -> None:
    """Keep active weights on target device/dtype and flush VRAM cleanly between stages."""
    from modules.layerdiffuse.diffusers_kdiffusion_sdxl import KDiffusionStableDiffusionXLPipeline
    from modules.layerdiffuse.vae import TransparentVAEDecoder
    from modules.marigold.marigold_depth_pipeline import MarigoldDepthPipeline

    dev = runtime["device"]
    dt = runtime["dtype"]

    def _is_group_offloaded(mod: object) -> bool:
        return offload_active or getattr(mod, "_diffusers_hook", None) is not None

    def _on_target_device(mod: object) -> bool:
        target_type = torch.device(dev).type
        if hasattr(mod, "device"):
            return torch.device(mod.device).type == target_type
        p = next(mod.parameters(), None) if hasattr(mod, "parameters") else None
        return p is not None and p.device.type == target_type

    active_layerdiff_pipe = None

    orig_call = KDiffusionStableDiffusionXLPipeline.__call__

    def _wrapped_call(self, *args, **kwargs):
        nonlocal active_layerdiff_pipe
        if getattr(self, "unet", None) is not None and not _is_group_offloaded(self.unet):
            if not _on_target_device(self.unet):
                self.unet.to(dev)
        if getattr(self, "vae", None) is not None and not _is_group_offloaded(self.vae):
            if not _on_target_device(self.vae):
                self.vae.to(dev, dtype=dt)
        if getattr(self, "trans_vae", None) is not None and not _is_group_offloaded(self.trans_vae):
            if not _on_target_device(self.trans_vae):
                self.trans_vae.to(dev, dtype=dt)
        _empty_device_cache(dev)
        prev_pipe = active_layerdiff_pipe
        active_layerdiff_pipe = self
        try:
            out = orig_call(self, *args, **kwargs)
        finally:
            active_layerdiff_pipe = prev_pipe
        _sync_device(dev)
        _empty_device_cache(dev)
        return out

    KDiffusionStableDiffusionXLPipeline.__call__ = _wrapped_call  # type: ignore[assignment]

    orig_dec = TransparentVAEDecoder.forward

    @torch.no_grad()
    def _wrapped_dec(self, sd_vae, latent, *args, **kwargs):
        # Free the 4.1 GB UNet from VRAM once before decoding layers so TransVAE (UNet1024)
        # stays well inside 12 GB VRAM without Windows WDDM shared-memory throttling.
        if (
            not offload_active
            and active_layerdiff_pipe is not None
            and getattr(active_layerdiff_pipe, "unet", None) is not None
            and not _is_group_offloaded(active_layerdiff_pipe.unet)
            and _on_target_device(active_layerdiff_pipe.unet)
        ):
            active_layerdiff_pipe.unet.to("cpu")
            _empty_device_cache(dev)
        if not _is_group_offloaded(sd_vae) and not _on_target_device(sd_vae):
            sd_vae.to(dev, dtype=dt)
        if not _is_group_offloaded(self) and not _on_target_device(self):
            self.to(dev, dtype=dt)
        return orig_dec(self, sd_vae, latent, *args, **kwargs)

    TransparentVAEDecoder.forward = _wrapped_dec  # type: ignore[assignment]

    orig_marigold = MarigoldDepthPipeline.single_infer

    @torch.no_grad()
    def _wrapped_marigold(self, cond_latent, num_inference_steps, generator):
        if not offload_active and not _is_group_offloaded(self.unet):
            if not _on_target_device(self.unet):
                self.unet.to(dev, dtype=dt)
            if getattr(self, "vae", None) is not None and not _on_target_device(self.vae):
                self.vae.to(dev, dtype=dt)
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


def _resolve_quant(mode: str, runtime: dict) -> bool:
    req = (mode or "auto").strip().lower()
    if req == "none":
        return False
    if req == "int8":
        return True
    env_override = os.getenv("PEBBLE_INOCHI_QUANT", "").strip().lower()
    if env_override in ("0", "false", "none", "off"):
        return False
    if env_override in ("1", "true", "int8", "on"):
        return True
    if runtime["device"] not in ("cuda", "mps"):
        return False
    vram = float(runtime.get("vram_gb") or 0.0)
    return vram <= 0.0 or vram < 20.0


def _resolve_offload(mode: str, no_offload_flag: bool, runtime: dict, use_int8: bool) -> bool:
    if no_offload_flag or mode == "off":
        return False
    if runtime["device"] != "cuda":
        return False
    if mode == "on":
        return True
    vram = float(runtime.get("vram_gb") or 0.0)
    if use_int8:
        # Quantized UNet (~4.1 GB) + VAE (~0.36 GB) fits comfortably in >= 8 GB VRAM without offload
        return vram <= 0.0 or vram < 7.5
    return vram <= 0.0 or vram < 20.0


def _build_layerdiff_staged(pretrained: str, runtime: dict, use_int8: bool, use_offload: bool):
    """Load LayerDiff3D in memory-frugal stages so 16 GB system RAM + 12 GB VRAM never thrash.

    1. Load text encoders alone -> cache the fixed 24 tag embeddings -> immediately free text encoders (1.56 GB).
    2. Load UNet on CPU -> optionally quantize Linear layers to INT8 (7.58 GB -> ~4.10 GB) -> move to GPU.
    3. Load VAE + TransVAE (~0.36 GB) -> attach cached embeddings.
    """
    from diffusers import AutoencoderKL
    from transformers import CLIPTextModel, CLIPTextModelWithProjection, CLIPTokenizer
    from modules.layerdiffuse.diffusers_kdiffusion_sdxl import KDiffusionStableDiffusionXLPipeline
    from modules.layerdiffuse.layerdiff3d import UNetFrameConditionModel
    from modules.layerdiffuse.vae import TransparentVAE

    dev = runtime["device"]
    dt = runtime["dtype"]

    # Stage 1: Text encoders only -> cache tag embeddings -> delete encoders before UNet loads
    tok1 = CLIPTokenizer.from_pretrained(pretrained, subfolder="tokenizer")
    tok2 = CLIPTokenizer.from_pretrained(pretrained, subfolder="tokenizer_2")
    te1 = CLIPTextModel.from_pretrained(pretrained, subfolder="text_encoder", torch_dtype=dt).to(dev)
    te2 = CLIPTextModelWithProjection.from_pretrained(pretrained, subfolder="text_encoder_2", torch_dtype=dt).to(dev)

    tag_groups = [
        ['front hair', 'back hair', 'head', 'neck', 'neckwear', 'topwear', 'handwear', 'bottomwear', 'legwear', 'footwear', 'tail', 'wings', 'objects'],
        ['headwear', 'face', 'irides', 'eyebrow', 'eyewhite', 'eyelash', 'eyewear', 'ears', 'earwear', 'nose', 'mouth'],
        ['hair', 'headwear', 'face', 'eyes', 'eyewear', 'ears', 'earwear', 'nose', 'mouth', 'neck', 'neckwear', 'topwear', 'handwear', 'bottomwear', 'legwear', 'footwear', 'tail', 'wings', 'objects'],
    ]
    cached_embeds = {}
    with torch.inference_mode():
        for tag_list in tag_groups:
            unseen = [t for t in tag_list if t not in cached_embeds]
            if not unseen:
                continue
            embeds_list = []
            pooled = None
            for tok, te in ((tok1, te1), (tok2, te2)):
                ids = tok(unseen, padding="max_length", max_length=tok.model_max_length, truncation=True, return_tensors="pt").input_ids
                out = te(ids.to(dev), output_hidden_states=True, return_dict=False)
                pooled = out[0]
                hid = out[-1][-2]
                embeds_list.append(hid.view(hid.shape[0], hid.shape[1], -1))
            cat_embeds = torch.concat(embeds_list, dim=-1).to(dtype=dt)
            pooled_v = pooled.view(cat_embeds.shape[0], -1).to(dtype=dt)
            for idx, tag in enumerate(unseen):
                cached_embeds[tag] = [cat_embeds[[idx]].cpu(), pooled_v[[idx]].cpu()]

    del te1, te2
    gc.collect()
    _empty_device_cache(dev)

    # Stage 2: Load UNet with low_cpu_mem_usage (0.36s), quantize Linear layers on GPU directly
    unet = UNetFrameConditionModel.from_pretrained(pretrained, subfolder="unet", torch_dtype=dt, low_cpu_mem_usage=True)
    if use_int8:
        qdev = dev if (not use_offload and dev in ("cuda", "mps")) else None
        n_rep, saved_b = _quantize_linears_int8(unet, dtype=dt, target_device=qdev)
        print(
            f"[Inochi2D] LayerDiff3D UNet INT8 quantized {n_rep} Linear layers (saved {saved_b / (1024 ** 3):.2f} GB)",
            flush=True,
        )

    # Stage 3: Load lightweight VAEs and assemble pipeline
    vae = AutoencoderKL.from_pretrained(pretrained, subfolder="vae", torch_dtype=dt, low_cpu_mem_usage=True)
    trans_vae = TransparentVAE.from_pretrained(pretrained, subfolder="trans_vae", torch_dtype=dt, low_cpu_mem_usage=True)

    pipeline = KDiffusionStableDiffusionXLPipeline(
        vae=vae,
        text_encoder=nn.Identity(),
        tokenizer=tok1,
        text_encoder_2=nn.Identity(),
        tokenizer_2=tok2,
        unet=unet,
        scheduler=None,
        trans_vae=trans_vae,
    )
    pipeline._cached_prompt_embeds = cached_embeds

    vae.to(device=dev, dtype=dt)
    trans_vae.to(device=dev, dtype=dt)
    if use_offload:
        pipeline.enable_group_offload(dev, num_blocks_per_group=1)
    else:
        unet.to(device=dev)
    _empty_device_cache(dev)
    return pipeline


def _build_marigold_staged(pretrained: str, runtime: dict, use_offload: bool):
    """Load Marigold depth pipeline, cache & unload its text encoder, and keep UNet resident when VRAM allows."""
    from diffusers import AutoencoderKL, DDIMScheduler
    from transformers import CLIPTextModel, CLIPTokenizer
    from modules.layerdiffuse.layerdiff3d import UNetFrameConditionModel
    from modules.marigold import MarigoldDepthPipeline

    dev = runtime["device"]
    dt = runtime["dtype"]
    vram = float(runtime.get("vram_gb") or 0.0)

    # Encode empty prompt first & drop CLIP text_encoder (avoiding transformers 5 low_cpu_mem bug)
    tok = CLIPTokenizer.from_pretrained(pretrained, subfolder="tokenizer")
    te = CLIPTextModel.from_pretrained(pretrained, subfolder="text_encoder", torch_dtype=dt).to(dev)
    text_inputs = tok("", padding="do_not_pad", max_length=tok.model_max_length, truncation=True, return_tensors="pt")
    with torch.inference_mode():
        empty_embed = te(text_inputs.input_ids.to(dev))[0].to(dtype=dt).cpu()
    del te
    gc.collect()
    _empty_device_cache(dev)

    unet = UNetFrameConditionModel.from_pretrained(pretrained, subfolder="unet", torch_dtype=dt, low_cpu_mem_usage=True)
    vae = AutoencoderKL.from_pretrained(pretrained, subfolder="vae", torch_dtype=dt, low_cpu_mem_usage=True)
    scheduler = DDIMScheduler.from_pretrained(pretrained, subfolder="scheduler")
    pipe = MarigoldDepthPipeline(
        unet=unet,
        vae=vae,
        scheduler=scheduler,
        text_encoder=nn.Identity(),
        tokenizer=tok,
    )
    pipe.empty_text_embed = empty_embed.to(device=dev, dtype=dt)
    pipe.vae.to(device=dev, dtype=dt)

    marigold_offload = use_offload and (vram <= 0.0 or vram < 5.0)
    if marigold_offload:
        pipe.enable_group_offload(dev, num_blocks_per_group=1)
    else:
        pipe.unet.to(device=dev, dtype=dt)
    _empty_device_cache(dev)
    return pipe


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
    parser.add_argument("--quant", default="auto", choices=["auto", "int8", "none"])
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
    use_int8 = _resolve_quant(args.quant, runtime)
    use_offload = _resolve_offload(args.offload, args.no_group_offload, runtime, use_int8)

    _configure_sdpa(runtime)
    _patch_cross_device_shims(runtime)

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    seethrough_root = here / "see-through"
    _setup_paths(seethrough_root)
    _patch_clip_eager()
    _patch_pipeline_vram_swap(runtime, offload_active=use_offload)
    os.chdir(seethrough_root)

    import utils.inference_utils as iu
    from utils.inference_utils import apply_layerdiff, apply_marigold, further_extr
    from utils.torch_utils import seed_everything

    srcp = str(Path(args.srcp).resolve())
    save_dir = str(Path(args.save_dir).resolve())

    print(
        f"[Inochi2D] Runtime: device={runtime['device']} backend={runtime['backend']} "
        f"gpu={runtime['gpu_name'] or 'cpu'} vram={runtime['vram_gb']}GB "
        f"dtype={runtime['dtype_name']} quant={'int8' if use_int8 else 'none'} offload={use_offload}",
        flush=True,
    )

    seed_everything(args.seed)
    print(f"[Inochi2D] Running LayerDiff3D on {srcp} (res={args.resolution}, steps={args.inference_steps}, quant={'int8' if use_int8 else 'none'}, offload={use_offload}) ...", flush=True)
    iu.layerdiff_pipeline = _build_layerdiff_staged(args.layerdiff_model, runtime, use_int8=use_int8, use_offload=use_offload)
    apply_layerdiff(
        srcp,
        args.layerdiff_model,
        save_dir=save_dir,
        seed=args.seed,
        resolution=args.resolution,
        num_inference_steps=args.inference_steps,
        group_offload=use_offload,
    )

    if getattr(iu, "layerdiff_pipeline", None) is not None:
        del iu.layerdiff_pipeline
        iu.layerdiff_pipeline = None
    _empty_device_cache(runtime["device"])

    print("[Inochi2D] Running Marigold depth ...", flush=True)
    iu.marigold_pipeline = _build_marigold_staged(args.marigold_model, runtime, use_offload=use_offload)
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

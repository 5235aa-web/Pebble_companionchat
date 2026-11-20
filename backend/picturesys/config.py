"""
Picture Studio configuration.

One JSON file under the app data directory, written atomically. The file is
never allowed to be a source of truth for what providers *exist*: the built-in
catalogue below is authoritative, and a saved file only overrides fields. That
way adding a provider in a later version shows up for users who already have a
config file, instead of silently missing from their panel.

Defaults are deliberately conservative: the feature is off, automatic triggers
are off, and only providers that work without an account are enabled.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any, Dict

from ..paths import CONFIG_DIR

logger = logging.getLogger(__name__)

CONFIG_PATH = CONFIG_DIR / "picture_studio.json"
_LOCK = threading.Lock()

# Provider metadata the API returns to the panel: what each one needs, and the
# defaults a fresh install starts from. `fields` drives the form, so a new
# provider needs no frontend change.
PROVIDER_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "siliconflow": {
        "label": "SiliconFlow / Kolors",
        "homepage": "https://siliconflow.cn",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "note": "Kolors is listed as free per image. OpenAI-compatible endpoint.",
        # Mirror of /v1/images/generations: num_inference_steps (1-100),
        # guidance_scale (Kolors only, <=20), cfg (Qwen-Image only, 0.1-20),
        # seed (<= 9999999999).
        "params": [
            {
                "key": "steps", "type": "number", "label": "Steps", "labelZh": "推理步数",
                "min": 1, "max": 100, "default": 20,
                "tip": "Diffusion steps. More steps usually means better quality and a longer wait. Kolors is commonly run at 20 to 30.",
                "tipZh": "推理步数，步数越多通常质量越高、耗时越长。Kolors 常用 20 到 30。",
            },
            {
                "key": "guidance", "type": "number", "label": "Guidance (Kolors)", "labelZh": "引导强度（Kolors）",
                "min": 0, "max": 20, "step": 0.5, "default": 7.5,
                "tip": "How strictly the image follows the prompt. Higher sticks closer to the text, lower allows more variation. Kolors only.",
                "tipZh": "与提示词的贴合程度，越高越严格按提示词画，越低越自由多样。仅 Kolors 生效。",
            },
            {
                "key": "cfg", "type": "number", "label": "CFG (Qwen-Image)", "labelZh": "CFG（Qwen-Image）",
                "min": 0.1, "max": 20, "step": 0.1, "default": 4,
                "tip": "Classifier-free guidance balancing accuracy and creativity. Qwen-Image only. Keep it above 1 when the picture must contain text; the official sample uses 50 steps with CFG 4.0.",
                "tipZh": "平衡精确度与创造性。仅 Qwen-Image 生效。需要图中有文字时必须大于 1，官方示例用 50 步配 CFG 4.0。",
            },
            {
                "key": "seed", "type": "number", "label": "Seed (empty = random)", "labelZh": "种子（留空随机）",
                "min": 0, "max": 9999999999, "default": "",
                "tip": "A fixed seed reproduces a similar picture. Leave empty to draw a new one each run.",
                "tipZh": "固定种子可以复现相近的画面，留空则每次重新随机。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "https://api.siliconflow.cn/v1",
            "model": "Kwai-Kolors/Kolors",
            "params": {"steps": 20, "guidance": 7.5, "cfg": "", "seed": ""},
        },
    },
    "zhipu": {
        "label": "Zhipu / CogView-3-Flash",
        "homepage": "https://docs.bigmodel.cn",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "note": "CogView-3-Flash is a free image model. OpenAI-compatible endpoint.",
        # CreateImageRequest: quality (hd|standard), size, watermark_enabled,
        # user_id. No seed, no steps, no negative prompt.
        "params": [
            {
                "key": "quality", "type": "select", "label": "Quality", "labelZh": "画质",
                "options": ["standard", "hd"], "default": "standard",
                "tip": "standard is the quick pass, about 5 to 10 seconds. hd is more detailed, about 20 seconds, and is the default for glm-image only.",
                "tipZh": "standard 生成更快，约 5 到 10 秒；hd 细节更完整，约 20 秒。只有 glm-image 默认使用 hd。",
            },
            {
                "key": "watermark", "type": "bool", "label": "AI watermark", "labelZh": "AI 水印", "default": True,
                "tip": "Adds the visible and invisible AI watermarks required by policy. Turning it off only works after signing the no-watermark waiver in the platform console.",
                "tipZh": "开启后按政策要求添加显式与隐式 AI 水印。关闭需先在平台控制台签署去水印声明，否则无效。",
            },
            {
                "key": "userId", "type": "text", "label": "End-user ID", "labelZh": "终端用户 ID", "default": "",
                "tip": "Optional end-user identifier, 6 to 128 characters. The platform uses it to trace abusive generations back to a user.",
                "tipZh": "可选的终端用户标识，6 到 128 个字符。平台用它追溯违规生成的责任用户。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
            "model": "cogview-3-flash",
            "params": {"quality": "standard", "watermark": True, "userId": ""},
        },
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI / FLUX.1 schnell",
        "homepage": "https://developers.cloudflare.com/workers-ai/",
        "fields": ["baseUrl", "accountId", "apiKey"],
        "free": True,
        "note": "10,000 Neurons per day free, roughly 170 images at 1024x1024.",
        # The published schema for flux-1-schnell takes only prompt and steps
        # (1..8). Width, height and seed are not part of it.
        "params": [
            {
                "key": "steps", "type": "number", "label": "Steps (max 8)", "labelZh": "步数（上限 8）",
                "min": 1, "max": 8, "default": 4,
                "tip": "The number of diffusion steps. Higher values can improve quality but take longer, and this model accepts at most 8.",
                "tipZh": "扩散步数。数值越高画质可能越好，但耗时更长；该模型最多接受 8 步。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "accountId": "",
            "baseUrl": "https://api.cloudflare.com/client/v4",
            "model": "@cf/black-forest-labs/flux-1-schnell",
            "params": {"steps": 4},
        },
    },
    "aihorde": {
        "label": "AI Horde",
        "homepage": "https://aihorde.net",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "note": "Crowdsourced and free. Anonymous key works; a personal key gets priority.",
        # ModelGenerationInputStable: steps 1..500, cfg_scale 0..100,
        # sampler_name, scheduler, karras, nsfw, seed (a string in the payload).
        "params": [
            {
                "key": "steps", "type": "number", "label": "Steps", "labelZh": "步数",
                "min": 1, "max": 500, "default": 25,
                "tip": "Sampling steps. More detail per step, but the job also sits longer in the crowd queue. Most models look right at 20 to 30.",
                "tipZh": "采样步数，步数越多细节越足，但在众包队列里等待也更久。多数模型在 20 到 30 就够。",
            },
            {
                "key": "cfgScale", "type": "number", "label": "CFG scale", "labelZh": "CFG 强度",
                "min": 0, "max": 100, "step": 0.5, "default": 7,
                "tip": "Prompt adherence. Too high looks stiff and over-saturated; the community sweet spot is roughly 4 to 9.",
                "tipZh": "提示词贴合强度。过高会显得僵硬、过饱和；社区常用区间约为 4 到 9。",
            },
            {
                "key": "sampler", "type": "select", "label": "Sampler", "labelZh": "采样器",
                "options": ["k_euler", "k_euler_a", "k_dpmpp_2m", "k_dpmpp_sde", "k_heun", "k_lms"], "default": "k_euler",
                "tip": "Which denoising sampler the worker runs. k_euler is the most common, k_dpmpp_2m usually needs fewer steps.",
                "tipZh": "工作节点使用的去噪采样器。k_euler 最通用，k_dpmpp_2m 通常更省步数。",
            },
            {
                "key": "scheduler", "type": "select", "label": "Scheduler", "labelZh": "调度器",
                "options": ["", "karras", "exponential", "simple", "normal", "ddim_uniform"], "default": "",
                "tip": "Noise schedule. Empty means the model default; karras often brings out finer texture.",
                "tipZh": "噪声调度方式。留空即模型默认；karras 常能带出更细的纹理。",
            },
            {
                "key": "karras", "type": "bool", "label": "Karras", "labelZh": "Karras", "default": False,
                "tip": "Legacy Karras switch. Redundant when the scheduler is already karras, so it is usually left off.",
                "tipZh": "旧式 Karras 开关。调度器已经选 karras 时它是重复的，通常留关。",
            },
            {
                "key": "nsfw", "type": "bool", "label": "Allow NSFW", "labelZh": "允许 NSFW", "default": False,
                "tip": "Lets workers return mature content. Many volunteer workers refuse such jobs, so the queue gets longer.",
                "tipZh": "允许工作节点产出成人内容。不少志愿者节点会拒单，等待时间会变长。",
            },
            {
                "key": "seed", "type": "number", "label": "Seed (empty = random)", "labelZh": "种子（留空随机）",
                "min": 0, "max": 4294967295, "default": "",
                "tip": "A fixed seed reproduces the same picture. Leave empty to draw a new one each run.",
                "tipZh": "固定种子可以复现同一张图，留空则每次重新随机。",
            },
        ],
        "config": {
            # Off by default even though it needs no account: enabling it sends
            # a summary of the scene to a third party, and that has to be a
            # deliberate choice rather than a side effect of clicking the button.
            "enabled": False,
            "apiKey": "0000000000",
            "baseUrl": "https://aihorde.net/api/v2",
            "model": "Flux.1-Schnell fp8 (Compact)",
            "params": {"steps": 25, "cfgScale": 7, "sampler": "k_euler", "scheduler": "", "karras": False, "nsfw": False, "seed": ""},
        },
    },
    "gemini": {
        "label": "Google Gemini image",
        "homepage": "https://ai.google.dev",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "note": "Free tier has a daily image quota; paid above it. Needs a reachable network.",
        # GenerationConfig.imageConfig: aspectRatio (14 documented values) and
        # imageSize (512 | 1K | 2K | 4K, uppercase K required).
        "params": [
            {
                "key": "aspectRatio", "type": "select", "label": "Aspect ratio", "labelZh": "宽高比",
                "options": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "1:4", "4:1", "1:8", "8:1"], "default": "1:1",
                "tip": "Output aspect ratio. Pick from the values the model documents; portrait needs 2:3 or 9:16, landscape 3:2 or 16:9.",
                "tipZh": "输出画幅比例，只能取模型支持列表里的值；竖图用 2:3 或 9:16，横图用 3:2 或 16:9。",
            },
            {
                "key": "imageSize", "type": "select", "label": "Image size", "labelZh": "输出尺寸",
                "options": ["512", "1K", "2K", "4K"], "default": "1K",
                "tip": "Resolution tier. Higher tiers are sharper but slower and count harder against the free quota.",
                "tipZh": "分辨率档位。档位越高越清晰，生成更慢，也更占用免费额度。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-2.5-flash-image",
            "params": {"aspectRatio": "1:1", "imageSize": "1K"},
        },
    },
    "modelscope": {
        "label": "ModelScope API-Inference",
        "homepage": "https://modelscope.cn",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "note": "Free but needs a bound Aliyun account with real-name verification.",
        # Async image task: model, prompt, size, negative_prompt, seed,
        # steps (1..100), guidance (1.5..20). Returns a task id, then polls.
        "params": [
            {
                "key": "steps", "type": "number", "label": "Steps", "labelZh": "步数",
                "min": 1, "max": 100, "default": 30,
                "tip": "Sampling steps. More steps means finer detail and a longer wait; FLUX-family models are usually fine at 25 to 30.",
                "tipZh": "采样步数，步数越多细节越细、耗时越长；FLUX 类模型通常 25 到 30 足够。",
            },
            {
                "key": "guidance", "type": "number", "label": "Guidance", "labelZh": "引导系数",
                "min": 1.5, "max": 20, "step": 0.1, "default": 3.5,
                "tip": "How strictly the picture follows the prompt. FLUX-family models expect a low value, around 3.5.",
                "tipZh": "画面贴合提示词的程度。FLUX 类模型适合较低数值，一般在 3.5 左右。",
            },
            {
                "key": "seed", "type": "number", "label": "Seed (empty = random)", "labelZh": "种子（留空随机）",
                "min": 0, "max": 2147483647, "default": "",
                "tip": "A fixed seed reproduces the same picture. Leave empty to draw a new one each run.",
                "tipZh": "固定种子可以复现同一张图，留空则每次重新随机。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "https://api-inference.modelscope.cn/v1",
            "model": "MusePublic/489_ckpt_FLUX_1",
            "params": {"steps": 30, "guidance": 3.5, "seed": ""},
        },
    },
    "pollinations": {
        "label": "Pollinations",
        "homepage": "https://pollinations.ai",
        "fields": ["baseUrl", "apiKey"],
        "free": False,
        "note": "Runs through a Pollen wallet; a key is required. Model stays empty so the gateway default applies.",
        # GET /image/{prompt} documents prompt, model, width, height, seed, safe,
        # quality, aspectRatio and the video-only fields. nologo, enhance,
        # private and negative_prompt were dropped from the published schema, so
        # this provider no longer sends them; the gateway would ignore them.
        "params": [
            {
                "key": "safe",
                "type": "select",
                "label": "Safety filter",
                "labelZh": "安全过滤",
                "options": ["", "privacy,secrets", "sexual,violence", "nsfw"],
                "default": "",
                "tip": "Comma-separated filter list sent to the gateway. privacy,secrets covers personal data, sexual,violence and nsfw cover explicit content. Leave empty for the gateway default.",
                "tipZh": "发送给网关的过滤项，逗号分隔。privacy,secrets 过滤个人隐私，sexual,violence 与 nsfw 过滤露骨内容。留空则用网关默认。",
            },
            {
                "key": "seed",
                "type": "number",
                "label": "Seed (empty = random)",
                "labelZh": "种子（留空随机）",
                "min": 0,
                "max": 2147483647,
                "default": "",
                "tip": "A fixed seed reproduces the same picture; leave empty to draw a new one each run.",
                "tipZh": "固定种子可以复现同一张图，留空则每次重新随机。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "https://gen.pollinations.ai",
            # Empty means "gateway default": hard-coding a model id here breaks
            # the moment the catalogue renames it, which has already happened.
            "model": "",
            "params": {"safe": "", "seed": ""},
        },
    },
    "comfyui": {
        "label": "ComfyUI（本地）",
        "homepage": "https://www.comfy.org",
        "fields": ["baseUrl", "apiKey"],
        "free": True,
        "requiresKey": False,
        "note": "本地出图，不消耗任何额度。需要 ComfyUI 已启动并开放 HTTP 接口，默认地址 http://127.0.0.1:8188。",
        # Everything here maps onto nodes in the workflow, not onto a vendor API.
        # ckpt/sampler/scheduler ship with empty options on purpose: the panel
        # fills them from the running instance, so a model the user installs
        # later shows up without touching this file.
        "params": [
            {
                "key": "workflow", "type": "select", "label": "Workflow", "labelZh": "工作流",
                "options": ["builtin:sd"], "default": "builtin:sd",
                "tip": "Which graph to run. The built-in one is stock SD text-to-image; imported ones are API-format JSON you added in this panel.",
                "tipZh": "要执行的工作流。内置的是标准 SD 文生图；导入项是本面板里添加的 API 格式 JSON。",
            },
            {
                "key": "ckpt", "type": "select", "label": "Checkpoint", "labelZh": "模型",
                "options": [], "default": "",
                "tip": "Checkpoint file name exactly as ComfyUI lists it. The built-in workflow loads this as its only model.",
                "tipZh": "checkpoint 文件名，必须与 ComfyUI 列表里完全一致。内置工作流把它作为唯一的底模。",
            },
            {
                "key": "steps", "type": "number", "label": "Steps", "labelZh": "采样步数",
                "min": 1, "max": 150, "default": 20,
                "tip": "Sampler steps written into the KSampler node. SD models are usually fine at 20 to 30.",
                "tipZh": "写入 KSampler 的采样步数。SD 类模型通常 20 到 30 就够。",
            },
            {
                "key": "cfg", "type": "number", "label": "CFG", "labelZh": "CFG 强度",
                "min": 0, "max": 30, "step": 0.1, "default": 7,
                "tip": "Classifier-free guidance. Higher sticks closer to the prompt and looks stiffer; SD is usually run at 6 to 8.",
                "tipZh": "分类器自由引导。越高越贴合提示词也越僵硬，SD 类模型常用 6 到 8。",
            },
            {
                "key": "sampler", "type": "select", "label": "Sampler", "labelZh": "采样器",
                "options": [], "default": "euler",
                "tip": "Sampling algorithm read from KSampler's own list, so it always matches what this ComfyUI build ships.",
                "tipZh": "采样算法，选项来自 KSampler 自身的列表，因此始终与这个 ComfyUI 版本一致。",
            },
            {
                "key": "scheduler", "type": "select", "label": "Scheduler", "labelZh": "调度器",
                "options": [], "default": "normal",
                "tip": "How noise is stepped down. normal is the plain default; karras and exponential often add texture.",
                "tipZh": "噪声衰减方式。normal 是基础默认值，karras 与 exponential 往往纹理更丰富。",
            },
            {
                "key": "denoise", "type": "number", "label": "Denoise", "labelZh": "重绘幅度",
                "min": 0, "max": 1, "step": 0.01, "default": 1,
                "tip": "Text-to-image needs 1.0; lower values only make sense for image-to-image graphs.",
                "tipZh": "文生图必须是 1.0，低于 1 只对图生图工作流有意义。",
            },
            {
                "key": "timeout", "type": "number", "label": "Timeout (s)", "labelZh": "超时（秒）",
                "min": 30, "max": 1800, "step": 10, "default": 300,
                "tip": "How long to wait for one picture. Loading a large checkpoint the first time can take minutes.",
                "tipZh": "单张图最长等待时间。首次加载大模型可能要几分钟。",
            },
        ],
        "config": {
            "enabled": False,
            "apiKey": "",
            "baseUrl": "http://127.0.0.1:8188",
            "model": "",
            "params": {
                "workflow": "builtin:sd",
                "ckpt": "",
                "steps": 20,
                "cfg": 7,
                "sampler": "euler",
                "scheduler": "normal",
                "denoise": 1,
                "timeout": 300,
            },
        },
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    # Master switch. On by default so the manual button works after the user
    # fills in a key; the automatic triggers below are what stay off.
    "enabled": True,
    "autoChat": False,
    "autoScript": False,
    # Per branch, applies to automatic triggers only. Manual clicks always run.
    "cooldownSeconds": 180,
    "dailyLimit": 40,
    "size": "1024x1024",
    "stylePrompt": "",
    "negativePrompt": (
        "lowres, bad anatomy, bad hands, extra digits, watermark, signature, "
        "text, jpeg artifacts, worst quality, blurry"
    ),
    # Which text model writes the image prompt. Empty means the active chat
    # provider. Kept separate so a cheap model can be used for directing.
    "promptProviderId": "",
    "activeProvider": "siliconflow",
    "providers": {name: copy.deepcopy(meta["config"]) for name, meta in PROVIDER_CATALOGUE.items()},
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Two-level merge: top-level scalars, plus one nested map for providers."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        # The fallback chain was removed. Dropping the key here means an old
        # config file cannot reintroduce it and a stale client cannot save it
        # back, without needing a migration step.
        if key == "fallbackChain":
            continue
        if key == "providers" and isinstance(value, dict):
            merged = out.setdefault("providers", {})
            for pid, pcfg in value.items():
                if pid not in merged:
                    # Unknown provider: keep it rather than drop user data, even
                    # though the runtime will ignore it.
                    merged[pid] = pcfg if isinstance(pcfg, dict) else {}
                    continue
                if isinstance(pcfg, dict):
                    # Provider params also merge one level deeper: a file saved
                    # before a knob existed should still get that knob's default
                    # instead of rendering an empty field.
                    base_params = merged[pid].get("params")
                    if not isinstance(base_params, dict):
                        base_params = {}
                    merged[pid].update(pcfg)
                    saved_params = pcfg.get("params")
                    if isinstance(saved_params, dict):
                        merged[pid]["params"] = {**base_params, **saved_params}
            continue
        out[key] = value
    return out


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> Dict[str, Any]:
    """Read the file and fold it over the defaults.

    A corrupt file is reported once and then treated as absent: the app still
    starts, and the next save replaces the damage.
    """
    with _LOCK:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return default_config()
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("picture config unreadable (%s), using defaults", exc)
            return default_config()

    if not isinstance(stored, dict):
        return default_config()
    return _merge(DEFAULT_CONFIG, stored)


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Merge into the current config and write atomically.

    Writing to a temp file and swapping it in means a crash mid-write leaves the
    previous config intact instead of a truncated JSON file the next start
    cannot read.
    """
    merged = _merge(load_config(), payload or {})
    # `catalogue` is emitted by public_config for the panel; if a client echoes
    # it back it must not be persisted as if it were user state.
    merged.pop("catalogue", None)

    with _LOCK:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    return merged


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Config for the UI, with the catalogue attached.

    Keys are returned as stored: the panel edits them in place, and masking them
    would make "save" wipe a working key whenever the field was left untouched.
    """
    out = copy.deepcopy(cfg)
    out["catalogue"] = {
        pid: {
            "label": meta["label"],
            "homepage": meta["homepage"],
            "fields": list(meta["fields"]),
            "free": meta["free"],
            # A local runner needs no key. The panel and the fallback chain both
            # read this instead of hardcoding provider ids.
            "requiresKey": bool(meta.get("requiresKey", True)),
            "note": meta["note"],
            # The panel renders the parameter form from this list, so a vendor
            # that gains a field needs no frontend change.
            "params": copy.deepcopy(meta.get("params") or []),
            "defaults": copy.deepcopy(meta["config"]),
        }
        for pid, meta in PROVIDER_CATALOGUE.items()
    }
    return out

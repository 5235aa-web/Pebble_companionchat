"""
Text-to-image providers behind one interface.

Every provider differs in transport: some speak the OpenAI image schema, one
needs an account id in the path, one is an async job queue, one returns bytes
straight from the wire. The orchestrator should not care, so the differences
live here and each class translates its own dialect.

Nothing in this module knows about chat. It receives a finished prompt.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import random
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from . import comfyui as _comfyui

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 24 * 1024 * 1024
# Cloudflare and Gemini answer in seconds; AI Horde can queue for minutes.
REQUEST_TIMEOUT = 120.0
HORDE_TIMEOUT = 300.0
HORDE_POLL_INTERVAL = 5.0
# ModelScope returns a task id immediately; the image shows up in a later poll.
MODELSCOPE_POLL_TIMEOUT = 240.0
MODELSCOPE_POLL_INTERVAL = 2.0

# Zhipu rejects any size outside this list, so a free-form size is snapped to the
# nearest supported one instead of failing the whole run.
ZHIPU_SIZES = (
    "1024x1024",
    "768x1344",
    "864x1152",
    "1344x768",
    "1152x864",
    "1440x720",
    "720x1440",
)


class ImageProviderError(Exception):
    """A provider refused or failed, with enough detail to fall through."""

    def __init__(self, message: str, kind: str = "upstream", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class ImageRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    seed: Optional[int] = None
    model: str = ""


@dataclass
class ImageResult:
    data: bytes
    mime: str = "image/png"
    seed: Optional[int] = None
    provider: str = ""
    model: str = ""


def parse_size(size: str) -> Tuple[int, int]:
    """'1024x768' -> (1024, 768). Anything malformed falls back to a square."""
    try:
        w, _, h = str(size or "").lower().partition("x")
        width, height = int(w), int(h)
        if 256 <= width <= 4096 and 256 <= height <= 4096:
            return width, height
    except (TypeError, ValueError):
        pass
    return 1024, 1024


def _sniff_mime(data: bytes, declared: str = "") -> str:
    """Trust the bytes over the header: providers mislabel WebP and JPEG."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    declared = (declared or "").split(";")[0].strip().lower()
    return declared if declared.startswith("image/") else "image/png"


def _decode_b64(value: str) -> bytes:
    raw = value.strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageProviderError(f"图片数据解码失败: {exc}", kind="decode") from exc


async def _fetch_image(
    client: httpx.AsyncClient,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[bytes, str]:
    """Download a generated image with a hard size ceiling."""
    try:
        async with client.stream("GET", url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ImageProviderError(
                    f"下载生成图失败 HTTP {resp.status_code}: {body[:200]!r}",
                    kind="download",
                    retryable=resp.status_code >= 500,
                )
            declared = resp.headers.get("content-type", "")
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ImageProviderError("生成图超过大小上限", kind="download")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ImageProviderError(f"下载生成图失败: {exc}", kind="network", retryable=True) from exc

    data = b"".join(chunks)
    if not data:
        raise ImageProviderError("生成图内容为空", kind="download")
    return data, _sniff_mime(data, declared)


def _first_image_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("images", "data", "generations"):
        items = payload.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return {}


class ImageProvider:
    """Base class: subclasses only implement `generate`."""

    id = ""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg or {}
        # Optional hooks the runner attaches before calling generate(). Only the
        # local ComfyUI provider reads them: the upstream job id makes a running
        # prompt cancellable, the progress callback feeds the panel's bar.
        # Cloud providers leave both unset and behave exactly as before.
        self.on_job_id: Optional[Callable[[str], None]] = None
        self.on_progress: Optional[Callable[[int, int], None]] = None
        # Polled by long-running wait loops so a cancelled job stops locally
        # even when the upstream prompt never produces a terminal event.
        self.should_stop: Optional[Callable[[], bool]] = None

    @property
    def model(self) -> str:
        return str(self.cfg.get("model") or "").strip()

    @property
    def api_key(self) -> str:
        return str(self.cfg.get("apiKey") or "").strip()

    @property
    def base_url(self) -> str:
        return str(self.cfg.get("baseUrl") or "").strip().rstrip("/")

    def require_key(self) -> str:
        if not self.api_key:
            raise ImageProviderError(f"{self.id} 缺少 API Key", kind="config")
        return self.api_key

    # --- per-provider parameter bag ---------------------------------------
    #
    # Each provider reads the keys its own API documents and ignores the rest,
    # so a new knob needs no schema migration: the panel stores whatever the
    # catalogue declared, and an unknown key simply does not reach a payload.

    @property
    def params(self) -> Dict[str, Any]:
        value = self.cfg.get("params")
        return value if isinstance(value, dict) else {}

    def param(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key)
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return value

    def param_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        value = self.param(key)
        if value is None:
            return default
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def param_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        value = self.param(key)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def param_bool(self, key: str, default: bool = False) -> bool:
        value = self.param(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        raise NotImplementedError


class OpenAICompatibleProvider(ImageProvider):
    """Shared shape for SiliconFlow, Zhipu and ModelScope.

    All three expose `/images/generations` with a bearer token and return either
    a URL or base64. They differ only in the size field name and the default
    model, so those are class attributes.
    """

    size_field = "image_size"
    model_fallback = ""

    def _snap_size(self, req: ImageRequest) -> str:
        return f"{req.width}x{req.height}"

    def _payload(self, req: ImageRequest) -> Dict[str, Any]:
        """The three fields every OpenAI-shaped image endpoint documents.

        Optional knobs are added by each subclass: the vendors disagree about
        which of them exist, and several reject a body that carries a field they
        do not define.
        """
        return {
            "model": self.model or self.model_fallback,
            "prompt": req.prompt,
            self.size_field: self._snap_size(req),
        }

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        url = f"{self.base_url}/images/generations"
        headers = {"Authorization": f"Bearer {self.require_key()}"}
        try:
            resp = await client.post(url, json=self._payload(req), headers=headers, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"{self.id} 请求失败: {exc}", kind="network", retryable=True) from exc

        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise ImageProviderError(
                f"{self.id} HTTP {resp.status_code}: {detail}",
                kind="auth" if resp.status_code in (401, 403) else "upstream",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ImageProviderError(f"{self.id} 返回非 JSON", kind="decode") from exc

        entry = _first_image_entry(payload)
        if entry.get("url"):
            data, mime = await _fetch_image(client, str(entry["url"]))
        elif entry.get("b64_json"):
            data, mime = _decode_b64(str(entry["b64_json"])), "image/png"
        elif entry.get("image_base64"):
            data, mime = _decode_b64(str(entry["image_base64"])), "image/png"
        else:
            raise ImageProviderError(f"{self.id} 未返回图片: {str(payload)[:200]}", kind="empty")

        seed = payload.get("seed")
        if seed is None:
            seed = entry.get("seed")
        return ImageResult(
            data=data,
            mime=mime,
            seed=int(seed) if isinstance(seed, (int, float)) else req.seed,
            provider=self.id,
            model=self.model or self.model_fallback,
        )


class SiliconFlowProvider(OpenAICompatibleProvider):
    """`/images/generations` with the Kolors and Qwen-Image knobs.

    Documented optional fields: negative_prompt, image_size, seed,
    num_inference_steps, guidance_scale (Kolors only), cfg (Qwen-Image only).
    The two Image-Edit models reject image_size, so it is dropped for them.
    """

    id = "siliconflow"
    size_field = "image_size"
    model_fallback = "Kwai-Kolors/Kolors"

    def _payload(self, req: ImageRequest) -> Dict[str, Any]:
        payload = super()._payload(req)
        model = str(payload.get("model") or "")
        if "Image-Edit" in model:
            payload.pop("image_size", None)
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        steps = self.param_int("steps")
        if steps is not None:
            payload["num_inference_steps"] = max(1, min(steps, 100))
        guidance = self.param_float("guidance")
        if guidance is not None:
            payload["guidance_scale"] = max(0.0, min(guidance, 20.0))
        cfg = self.param_float("cfg")
        if cfg is not None:
            payload["cfg"] = max(0.1, min(cfg, 20.0))
        # A seed typed into the provider card wins over the random one, which is
        # what makes "same prompt, same seed" reproducible.
        seed = self.param_int("seed")
        if seed is None:
            seed = req.seed
        if seed is not None:
            payload["seed"] = max(0, min(int(seed), 9999999999))
        return payload


class ZhipuProvider(OpenAICompatibleProvider):
    """CogView: size, quality, watermark_enabled, user_id. No seed or steps."""

    id = "zhipu"
    size_field = "size"
    model_fallback = "cogview-3-flash"

    def _snap_size(self, req: ImageRequest) -> str:
        wanted = f"{req.width}x{req.height}"
        if wanted in ZHIPU_SIZES:
            return wanted
        # Same aspect ratio when possible, otherwise the closest square.
        ratio = req.width / max(1, req.height)
        best = min(
            ZHIPU_SIZES,
            key=lambda s: abs(
                (int(s.split("x")[0]) / max(1, int(s.split("x")[1]))) - ratio
            ),
        )
        return best

    def _payload(self, req: ImageRequest) -> Dict[str, Any]:
        payload = super()._payload(req)
        quality = self.param("quality")
        if quality:
            payload["quality"] = str(quality)
        if self.param("watermark") is not None:
            payload["watermark_enabled"] = self.param_bool("watermark", False)
        user_id = self.param("userId")
        if user_id:
            payload["user_id"] = str(user_id)
        return payload


class ModelScopeProvider(ImageProvider):
    """API-Inference image tasks: submit async, poll, then download.

    The endpoint answers 202 with a task id; the picture only exists once the
    poll reports SUCCEED. Treating it like a synchronous OpenAI endpoint returns
    a task id where an image was expected, which is why this is not a subclass.
    """

    id = "modelscope"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        key = self.require_key()
        model = self.model or "MusePublic/489_ckpt_FLUX_1"
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "size": f"{req.width}x{req.height}",
        }
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        steps = self.param_int("steps")
        if steps is not None:
            payload["steps"] = max(1, min(steps, 100))
        guidance = self.param_float("guidance")
        if guidance is not None:
            payload["guidance"] = max(1.5, min(guidance, 20.0))
        # A seed typed into the provider card wins over the random one, which is
        # what makes "same prompt, same seed" reproducible.
        seed = self.param_int("seed")
        if seed is None:
            seed = req.seed
        if seed is not None:
            payload["seed"] = max(0, min(int(seed), 2**31 - 1))

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-ModelScope-Async-Mode": "true",
        }
        try:
            resp = await client.post(
                f"{self.base_url}/images/generations",
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ImageProviderError(
                f"modelscope 请求失败: {exc}", kind="network", retryable=True
            ) from exc
        if resp.status_code >= 400:
            raise ImageProviderError(
                f"modelscope HTTP {resp.status_code}: {resp.text[:300]}",
                kind="auth" if resp.status_code in (401, 403) else "upstream",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ImageProviderError("modelscope 返回非 JSON", kind="decode") from exc
        task_id = str((body or {}).get("task_id") or "").strip()
        if not task_id:
            raise ImageProviderError(
                f"modelscope 未返回 task_id: {str(body)[:200]}", kind="upstream"
            )

        poll_headers = {
            "Authorization": f"Bearer {key}",
            "X-ModelScope-Task-Type": "image_generation",
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MODELSCOPE_POLL_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(MODELSCOPE_POLL_INTERVAL)
            try:
                check = await client.get(
                    f"{self.base_url}/tasks/{task_id}",
                    headers=poll_headers,
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                raise ImageProviderError(
                    f"modelscope 轮询失败: {exc}", kind="network", retryable=True
                ) from exc
            if check.status_code >= 400:
                raise ImageProviderError(
                    f"modelscope 轮询 HTTP {check.status_code}: {check.text[:200]}",
                    kind="upstream",
                )
            try:
                data = check.json()
            except ValueError:
                continue
            status = str((data or {}).get("task_status") or "").upper()
            if status in ("FAILED", "FAIL"):
                raise ImageProviderError(
                    f"modelscope 任务失败: {str(data)[:200]}", kind="upstream"
                )
            if status in ("SUCCEED", "SUCCESS"):
                urls = (data or {}).get("output_images") or []
                if not urls:
                    raise ImageProviderError("modelscope 任务成功但没有图片", kind="decode")
                data_bytes, mime = await _fetch_image(client, str(urls[0]))
                return ImageResult(
                    data=data_bytes,
                    mime=mime,
                    seed=req.seed,
                    provider=self.id,
                    model=model,
                )
        raise ImageProviderError("modelscope 任务超时", kind="timeout", retryable=True)


class CloudflareProvider(ImageProvider):
    """Workers AI: bytes on success, JSON with base64 when asked for a data URL."""

    id = "cloudflare"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        account = str(self.cfg.get("accountId") or "").strip()
        if not account:
            raise ImageProviderError("Cloudflare 缺少 Account ID", kind="config")
        key = self.require_key()
        model = self.model or "@cf/black-forest-labs/flux-1-schnell"
        url = f"{self.base_url}/accounts/{account}/ai/run/{model}"

        # The published schema for flux-1-schnell accepts exactly prompt and
        # steps (1..8). Sending width/height is not documented and is rejected
        # by models that only take these two.
        payload: Dict[str, Any] = {
            "prompt": req.prompt,
            "steps": max(1, min(self.param_int("steps", 4) or 4, 8)),
        }

        headers = {"Authorization": f"Bearer {key}"}
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"cloudflare 请求失败: {exc}", kind="network", retryable=True) from exc

        if resp.status_code >= 400:
            raise ImageProviderError(
                f"cloudflare HTTP {resp.status_code}: {resp.text[:300]}",
                kind="auth" if resp.status_code in (401, 403) else "upstream",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )

        declared = resp.headers.get("content-type", "")
        if declared.startswith("image/"):
            return ImageResult(
                data=resp.content,
                mime=_sniff_mime(resp.content, declared),
                seed=req.seed,
                provider=self.id,
                model=model,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ImageProviderError("cloudflare 返回无法识别", kind="decode") from exc

        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and result.get("image"):
            data = _decode_b64(str(result["image"]))
        elif isinstance(result, str) and result:
            data = _decode_b64(result)
        else:
            raise ImageProviderError(f"cloudflare 未返回图片: {str(payload)[:200]}", kind="empty")

        return ImageResult(
            data=data,
            mime=_sniff_mime(data),
            seed=req.seed,
            provider=self.id,
            model=model,
        )


class AIHordeProvider(ImageProvider):
    """Crowdsourced queue: submit, poll, then download.

    The anonymous key is literally "0000000000"; requests still work with it,
    they just sit behind users who have kudos.
    """

    id = "aihorde"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        key = self.api_key or "0000000000"
        model = self.model or "Flux.1-Schnell fp8 (Compact)"
        headers = {"apikey": key, "Client-Agent": "pebble-companion-chat:1.0"}

        payload: Dict[str, Any] = {
            "prompt": req.prompt,
            "params": {
                "width": max(64, min(req.width, 3072)),
                "height": max(64, min(req.height, 3072)),
                "steps": max(1, min(self.param_int("steps", 25) or 25, 500)),
                "cfg_scale": max(0.0, min(self.param_float("cfgScale", 7.0) or 7.0, 100.0)),
                "sampler_name": str(self.param("sampler") or "k_euler"),
                "n": 1,
            },
            "models": [model],
            "nsfw": self.param_bool("nsfw", False),
            "r2": True,
            "shared": False,
        }
        scheduler = self.param("scheduler")
        if scheduler:
            payload["params"]["scheduler"] = str(scheduler)
        if self.param_bool("karras", False):
            payload["params"]["karras"] = True
        # A seed typed into the provider card wins over the random one, which is
        # what makes "same prompt, same seed" reproducible.
        seed = self.param_int("seed")
        if seed is None:
            seed = req.seed
        if seed is not None:
            payload["params"]["seed"] = str(max(0, int(seed)))
        if req.negative_prompt:
            payload["params"]["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            payload["params"]["seed"] = str(req.seed)

        try:
            resp = await client.post(
                f"{self.base_url}/generate/async",
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"aihorde 提交失败: {exc}", kind="network", retryable=True) from exc

        if resp.status_code >= 400:
            raise ImageProviderError(
                f"aihorde HTTP {resp.status_code}: {resp.text[:300]}",
                kind="auth" if resp.status_code in (401, 403) else "upstream",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )

        try:
            job_id = str(resp.json().get("id") or "")
        except ValueError as exc:
            raise ImageProviderError("aihorde 提交返回非 JSON", kind="decode") from exc
        if not job_id:
            raise ImageProviderError("aihorde 未返回任务 id", kind="upstream")

        deadline = asyncio.get_event_loop().time() + HORDE_TIMEOUT
        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise ImageProviderError("aihorde 排队超时", kind="timeout", retryable=True)
            await asyncio.sleep(HORDE_POLL_INTERVAL)
            try:
                check = await client.get(
                    f"{self.base_url}/generate/check/{job_id}",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                check_payload = check.json()
            except (httpx.HTTPError, ValueError):
                continue
            if isinstance(check_payload, dict) and check_payload.get("faulted"):
                raise ImageProviderError("aihorde 生成失败", kind="upstream")
            if isinstance(check_payload, dict) and check_payload.get("done"):
                break

        try:
            final = await client.get(
                f"{self.base_url}/generate/status/{job_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            final_payload = final.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageProviderError(f"aihorde 取结果失败: {exc}", kind="upstream") from exc

        generations = final_payload.get("generations") if isinstance(final_payload, dict) else None
        if not isinstance(generations, list) or not generations:
            raise ImageProviderError("aihorde 未返回图片", kind="empty")
        img_url = str((generations[0] or {}).get("img") or "")
        if not img_url:
            raise ImageProviderError("aihorde 结果缺少图片地址", kind="empty")

        data, mime = await _fetch_image(client, img_url)
        seed = (generations[0] or {}).get("seed")
        return ImageResult(
            data=data,
            mime=mime,
            seed=int(seed) if isinstance(seed, (int, float)) else req.seed,
            provider=self.id,
            model=model,
        )


class GeminiProvider(ImageProvider):
    """Gemini image models return inline base64 parts, not a URL."""

    id = "gemini"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        key = self.require_key()
        model = self.model or "gemini-2.5-flash-image"
        url = f"{self.base_url}/models/{model}:generateContent"
        # imageConfig carries aspect ratio and output size; both are optional and
        # an empty object is rejected by older models, so it is only added when a
        # value exists.
        generation_config: Dict[str, Any] = {"responseModalities": ["IMAGE"]}
        image_config: Dict[str, Any] = {}
        aspect = self.param("aspectRatio")
        if aspect:
            image_config["aspectRatio"] = str(aspect)
        output_size = self.param("imageSize")
        if output_size:
            image_config["imageSize"] = str(output_size)
        if image_config:
            generation_config["imageConfig"] = image_config

        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": req.prompt}]}],
            "generationConfig": generation_config,
        }
        try:
            resp = await client.post(url, params={"key": key}, json=payload, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"gemini 请求失败: {exc}", kind="network", retryable=True) from exc

        # Older endpoints reject the modality hint; retry once without it rather
        # than failing a request the model could have answered.
        if resp.status_code == 400 and "responseModalities" in resp.text:
            payload.pop("generationConfig", None)
            try:
                resp = await client.post(url, params={"key": key}, json=payload, timeout=REQUEST_TIMEOUT)
            except httpx.HTTPError as exc:
                raise ImageProviderError(f"gemini 请求失败: {exc}", kind="network", retryable=True) from exc

        if resp.status_code >= 400:
            raise ImageProviderError(
                f"gemini HTTP {resp.status_code}: {resp.text[:300]}",
                kind="auth" if resp.status_code in (401, 403) else "upstream",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise ImageProviderError("gemini 返回非 JSON", kind="decode") from exc

        candidates = body.get("candidates") if isinstance(body, dict) else None
        parts: Any = []
        if isinstance(candidates, list) and candidates:
            content = (candidates[0] or {}).get("content") or {}
            parts = content.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                data = _decode_b64(str(inline["data"]))
                return ImageResult(
                    data=data,
                    mime=_sniff_mime(data, str(inline.get("mimeType") or "")),
                    seed=req.seed,
                    provider=self.id,
                    model=model,
                )
        raise ImageProviderError("gemini 未返回图片（模型可能不支持图像输出）", kind="empty")


class PollinationsProvider(ImageProvider):
    """Plain GET returning image bytes. A key is required on the 2026 gateway."""

    id = "pollinations"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        prompt = urllib.parse.quote(req.prompt[:1800], safe="")
        query = {"width": str(req.width), "height": str(req.height)}
        # Empty model means "whatever the gateway defaults to". Baking an id in
        # here is how this provider broke before: the catalogue renamed flux.
        if self.model:
            query["model"] = self.model
        seed = self.param_int("seed")
        if seed is None:
            seed = req.seed
        if seed is not None:
            query["seed"] = str(seed)
        # `safe` is a comma-separated filter list now, not a boolean. Configs
        # written before that change still hold true/false, and str(False) would
        # be sent verbatim, so booleans are translated instead of stringified.
        raw_safe = self.param("safe", "")
        if isinstance(raw_safe, bool):
            safe = "true" if raw_safe else ""
        else:
            safe = str(raw_safe or "").strip()
        if safe:
            query["safe"] = safe
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = await client.get(
                f"{self.base_url}/image/{prompt}",
                params=query,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"pollinations 请求失败: {exc}", kind="network", retryable=True) from exc

        if resp.status_code in (401, 402, 403):
            raise ImageProviderError(
                f"pollinations 需要有效密钥或余额 (HTTP {resp.status_code})",
                kind="auth",
            )
        if resp.status_code >= 400:
            raise ImageProviderError(
                f"pollinations HTTP {resp.status_code}: {resp.text[:200]}",
                kind="upstream",
                retryable=resp.status_code >= 500,
            )

        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageProviderError("pollinations 返回图片过大", kind="download")
        if not data:
            raise ImageProviderError("pollinations 返回空内容", kind="empty")
        return ImageResult(
            data=data,
            mime=_sniff_mime(data, resp.headers.get("content-type", "")),
            seed=req.seed,
            provider=self.id,
            model=self.model or "gateway-default",
        )


class ComfyUIProvider(ImageProvider):
    """Local ComfyUI over HTTP. Slow and free, and only as good as its workflow."""

    id = "comfyui"

    async def generate(self, req: ImageRequest, client: httpx.AsyncClient) -> ImageResult:
        workflow = str(self.param("workflow") or "builtin:sd").strip()
        ckpt = str(self.param("ckpt") or "").strip()
        # The built-in graph always needs a checkpoint; an imported one only needs
        # one when it actually references {{ckpt}}, which the renderer enforces.
        if not ckpt and workflow.startswith(_comfyui.BUILTIN_PREFIX):
            raise ImageProviderError(
                "comfyui 还没有选择 checkpoint，请先在后台「生图」里选一个本地模型", kind="config"
            )

        seed = req.seed if req.seed is not None else random_seed()
        values: Dict[str, Any] = {
            "prompt": req.prompt,
            "negative": req.negative_prompt,
            "seed": seed,
            "width": req.width,
            "height": req.height,
            "steps": self.param_int("steps", 20),
            "cfg": self.param_float("cfg", 7.0),
            "sampler": str(self.param("sampler") or "euler"),
            "scheduler": str(self.param("scheduler") or "normal"),
            "denoise": self.param_float("denoise", 1.0),
            "ckpt": ckpt,
        }
        timeout = float(self.param_int("timeout", 300) or 300)
        try:
            # A local instance must not go through the system proxy: httpx honours
            # HTTP_PROXY by default, and a proxy answers 503 for 127.0.0.1.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=True) as local:
                data, declared, used_seed = await _comfyui.generate(
                    self.base_url,
                    self.api_key,
                    workflow,
                    values,
                    timeout,
                    local,
                    on_job_id=self.on_job_id,
                    on_progress=self.on_progress,
                    should_stop=self.should_stop,
                )
        except _comfyui.ComfyUIError as exc:
            raise ImageProviderError(f"comfyui {exc}", kind=exc.kind) from exc

        return ImageResult(
            data=data,
            mime=_sniff_mime(data, declared),
            seed=used_seed if used_seed is not None else seed,
            provider=self.id,
            model=ckpt or workflow,
        )


PROVIDERS: Dict[str, type] = {
    SiliconFlowProvider.id: SiliconFlowProvider,
    ZhipuProvider.id: ZhipuProvider,
    CloudflareProvider.id: CloudflareProvider,
    AIHordeProvider.id: AIHordeProvider,
    GeminiProvider.id: GeminiProvider,
    ModelScopeProvider.id: ModelScopeProvider,
    PollinationsProvider.id: PollinationsProvider,
    ComfyUIProvider.id: ComfyUIProvider,
}


def build_provider(provider_id: str, cfg: Dict[str, Any]) -> Optional[ImageProvider]:
    cls = PROVIDERS.get(provider_id)
    if cls is None:
        return None
    return cls(cfg or {})


def random_seed() -> int:
    return random.randint(1, 2**31 - 1)

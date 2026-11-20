"""ComfyUI bridge for the picture studio.

Everything here talks HTTP to a running ComfyUI instance. Nothing reads the
ComfyUI installation directory: the install path is different on every machine,
so the only contract is `baseUrl` plus the documented endpoints (`/prompt`,
`/history/{id}`, `/view`, `/object_info`, `/system_stats`).

Three things make a workflow usable:

1. Placeholders. A template may carry `{{prompt}}`, `{{seed}}` and friends. If it
   does, those are substituted and nothing else is touched.
2. Automatic injection. A workflow exported straight from the canvas has no
   placeholders, so the prompt, sampler knobs and latent size are written into
   the nodes that declare them. That is what makes "Export (API)" drop-in usable.
3. Selection. Built-in templates live in code; imported ones live in
   `config/comfyui/*.json`, next to this project's own settings, never inside
   the ComfyUI tree.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Top-level import on purpose: PyInstaller's static scan must see this, and the
# progress socket is the only non-HTTP part of this module.
import websockets

import httpx

from ..paths import CONFIG_DIR

logger = logging.getLogger(__name__)

WORKFLOW_DIR = CONFIG_DIR / "comfyui"
BUILTIN_PREFIX = "builtin:"

CONNECT_TIMEOUT = 8.0
INFO_TIMEOUT = 40.0
SUBMIT_TIMEOUT = 30.0
# One history poll per 1.5s keeps the job responsive without hammering a server
# that is busy running the graph on the same machine.
POLL_INTERVAL = 1.5
MAX_WORKFLOW_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_NAME_CLEAN = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff ]+")

# Nodes that carry the text prompt in stock ComfyUI and the usual custom packs.
_TEXT_NODES = {
    "CLIPTextEncode",
    "CLIPTextEncodeSDXL",
    "CLIPTextEncodeFlux",
    "T5TextEncode",
    "TextEncodeQwenImageEdit",
}
_SAMPLER_NODES = {"KSampler", "KSamplerAdvanced"}
_LATENT_NODES = {
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "EmptyHunyuanLatentVideo",
    "EmptyLTXVLatentVideo",
    "EmptyMochiLatentVideo",
}

# Stock SD text-to-image graph in ComfyUI's API format. Values that the studio
# owns are placeholders; `batch_size` and the two node ids are fixed.
BUILTIN_SD: Dict[str, Any] = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "{{ckpt}}"}},
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": 1},
    },
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{negative}}", "clip": ["4", 1]}},
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": "{{seed}}",
            "steps": "{{steps}}",
            "cfg": "{{cfg}}",
            "sampler_name": "{{sampler}}",
            "scheduler": "{{scheduler}}",
            "denoise": "{{denoise}}",
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "pebble", "images": ["8", 0]},
    },
}

BUILTIN_WORKFLOWS: Dict[str, Dict[str, Any]] = {
    BUILTIN_PREFIX + "sd": {
        "label": "SD 文生图（内置）",
        "labelZh": "SD 文生图（内置）",
        "note": "单文件 checkpoint 的标准文生图图：Load Checkpoint、双 CLIP 编码、KSampler、VAE Decode、Save Image。",
        "workflow": BUILTIN_SD,
    },
}


class ComfyUIError(Exception):
    """Carries a kind so the provider can map it onto its own error taxonomy."""

    def __init__(self, message: str, kind: str = "api") -> None:
        super().__init__(message)
        self.kind = kind


# --- workflow storage ------------------------------------------------------


def workflow_dir() -> Path:
    return WORKFLOW_DIR


def _sanitise_name(name: str) -> str:
    cleaned = _NAME_CLEAN.sub("", str(name or "")).strip().replace(" ", "-")
    if not cleaned:
        raise ComfyUIError("工作流名称不能为空，只能用字母、数字、中文、下划线和短横线", kind="config")
    if len(cleaned) > 64:
        cleaned = cleaned[:64]
    return cleaned


def _validate_workflow(data: Any) -> Dict[str, Any]:
    """Accept only ComfyUI's API format.

    The canvas saves {"nodes": [...], "links": [...]}, which `/prompt` rejects
    with a bare 400. Catching it here turns that into an instruction the user can
    act on: re-export with "Export (API)".
    """
    if not isinstance(data, dict) or not data:
        raise ComfyUIError("工作流内容为空：需要 ComfyUI 的 API 格式，不是界面保存的 UI 格式", kind="config")
    if "nodes" in data and "links" in data:
        raise ComfyUIError(
            "这是 ComfyUI 的界面格式（含 nodes/links），不能直接提交。请在 ComfyUI 里用「导出（API）」重新导出。",
            kind="config",
        )
    for node_id, node in data.items():
        if not isinstance(node, dict):
            raise ComfyUIError(f"节点 {node_id} 不是对象，无法识别为 API 格式", kind="config")
        if not isinstance(node.get("class_type"), str) or not node.get("class_type"):
            raise ComfyUIError(f"节点 {node_id} 缺少 class_type，无法识别为 API 格式", kind="config")
        if not isinstance(node.get("inputs"), dict):
            raise ComfyUIError(f"节点 {node_id} 缺少 inputs，无法识别为 API 格式", kind="config")
    return data


def list_workflows() -> List[Dict[str, Any]]:
    """Built-ins first, then whatever the user imported."""
    out: List[Dict[str, Any]] = []
    for key, meta in BUILTIN_WORKFLOWS.items():
        out.append(
            {
                "id": key,
                "label": meta["label"],
                "note": meta.get("note", ""),
                "builtin": True,
            }
        )
    directory = workflow_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            out.append(
                {
                    "id": path.stem,
                    "label": path.stem,
                    "note": "",
                    "builtin": False,
                }
            )
    return out


def load_workflow(name: str) -> Dict[str, Any]:
    key = str(name or "").strip()
    if not key:
        raise ComfyUIError("还没有选择工作流", kind="config")
    builtin = BUILTIN_WORKFLOWS.get(key)
    if builtin is not None:
        return copy.deepcopy(builtin["workflow"])

    path = workflow_dir() / f"{key}.json"
    if not path.is_file():
        raise ComfyUIError(f"工作流「{key}」不存在，可能已被删除", kind="config")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComfyUIError(f"工作流「{key}」读取失败：{exc}", kind="config") from exc
    return copy.deepcopy(_validate_workflow(data))


def save_workflow(name: str, data: Any) -> Dict[str, Any]:
    """Store an imported workflow. Built-in ids are reserved."""
    cleaned = _sanitise_name(name)
    if BUILTIN_PREFIX in cleaned:
        raise ComfyUIError("工作流名称不能包含 builtin: 前缀", kind="config")
    payload = _validate_workflow(data)
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_WORKFLOW_BYTES:
        raise ComfyUIError("工作流文件过大（上限 512 KB）", kind="config")

    directory = workflow_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{cleaned}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    logger.info("picture studio: saved comfyui workflow %s", cleaned)
    return {"id": cleaned, "label": cleaned, "note": "", "builtin": False, "nodes": len(payload)}


def delete_workflow(name: str) -> None:
    key = str(name or "").strip()
    if key in BUILTIN_WORKFLOWS:
        raise ComfyUIError("内置工作流不能删除", kind="config")
    path = workflow_dir() / f"{key}.json"
    if not path.is_file():
        raise ComfyUIError(f"工作流「{key}」不存在", kind="config")
    try:
        path.unlink()
    except OSError as exc:
        raise ComfyUIError(f"删除工作流失败：{exc}", kind="config") from exc


# --- template rendering ----------------------------------------------------


def _coerce(key: str, values: Dict[str, Any]) -> Any:
    return values[key]


def _substitute(node: Any, values: Dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    if isinstance(node, str):
        exact = _PLACEHOLDER.fullmatch(node.strip())
        if exact:
            key = exact.group(1)
            # An empty negative prompt is a valid instruction ("exclude nothing"),
            # not a missing value: ComfyUI takes an empty conditioning string.
            if key == "negative":
                return str(values.get("negative") or "")
            # Any other unfilled placeholder must not reach ComfyUI: it would
            # arrive as the literal string and the queue would reject it with a
            # list error nobody can read.
            if key not in values or values[key] is None or values[key] == "":
                raise ComfyUIError(f"工作流需要 {key}，但当前没有值，请先在面板里填好", kind="config")
            return _coerce(key, values)

        def replace(match: "re.Match[str]") -> str:
            key = match.group(1)
            if key not in values or values[key] is None:
                raise ComfyUIError(f"工作流需要 {key}，但当前没有值，请先在面板里填好", kind="config")
            return str(values[key])

        return _PLACEHOLDER.sub(replace, node)
    return node


def _inject(workflow: Dict[str, Any], values: Dict[str, Any]) -> None:
    """Write studio values into the nodes a canvas export already contains.

    Only fields the node actually declares are touched, so a graph whose sampler
    lives inside a group node or a custom pack is left alone instead of being
    corrupted.
    """
    text_nodes = [node for node in workflow.values() if node.get("class_type") in _TEXT_NODES]
    if text_nodes:
        text_nodes[0].setdefault("inputs", {})["text"] = values["prompt"]
        if len(text_nodes) > 1 and values.get("negative"):
            text_nodes[1].setdefault("inputs", {})["text"] = values["negative"]

    for node in workflow.values():
        class_type = node.get("class_type")
        inputs = node.setdefault("inputs", {})
        if class_type in _SAMPLER_NODES:
            for key, value in (
                ("seed", values.get("seed")),
                ("noise_seed", values.get("seed")),
                ("steps", values.get("steps")),
                ("cfg", values.get("cfg")),
                ("sampler_name", values.get("sampler")),
                ("scheduler", values.get("scheduler")),
                ("denoise", values.get("denoise")),
            ):
                if key in inputs and value is not None and value != "":
                    inputs[key] = value
        elif class_type in _LATENT_NODES:
            if "width" in inputs:
                inputs["width"] = values["width"]
            if "height" in inputs:
                inputs["height"] = values["height"]
        elif class_type == "CheckpointLoaderSimple":
            if values.get("ckpt"):
                inputs["ckpt_name"] = values["ckpt"]
        elif class_type == "SaveImage":
            inputs["filename_prefix"] = "pebble"


def render(workflow: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a template plus studio values into a submittable graph."""
    raw = json.dumps(workflow)
    if _PLACEHOLDER.search(raw):
        return _substitute(copy.deepcopy(workflow), values)
    prepared = copy.deepcopy(workflow)
    _inject(prepared, values)
    return prepared


# --- HTTP ------------------------------------------------------------------


def _base(url: str) -> str:
    return str(url or "http://127.0.0.1:8188").strip().rstrip("/")


def _headers(api_key: str) -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts: List[str] = []
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        details = str(error.get("details") or "").strip()
        if message and details and details != message:
            parts.append(f"{message}（{details}）")
        elif message or details:
            parts.append(message or details)
    elif error:
        parts.append(str(error))

    node_errors = payload.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id, info in node_errors.items():
            if not isinstance(info, dict):
                continue
            label = str(info.get("class_type") or node_id)
            messages = [
                str(item.get("message") or item.get("details") or "")
                for item in (info.get("errors") or [])
                if isinstance(item, dict)
            ]
            joined = "；".join(part for part in messages if part)
            if joined:
                parts.append(f"{label}: {joined}")
    return "；".join(part for part in parts if part)


def _status_error(status: Dict[str, Any]) -> str:
    for item in status.get("messages") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        kind, body = item[0], item[1]
        if kind == "execution_error" and isinstance(body, dict):
            node = str(body.get("node_type") or "")
            message = str(body.get("exception_message") or "").strip()
            if node or message:
                return f"{node}: {message}" if node else message
    return ""


async def probe(base_url: str, api_key: str, client: httpx.AsyncClient) -> Dict[str, Any]:
    """Connection check plus what the instance can offer.

    A missing instance is a normal state, not an exception: the panel shows it
    and the user starts ComfyUI when they want a picture.
    """
    url = _base(base_url)
    headers = _headers(api_key)
    result: Dict[str, Any] = {"baseUrl": url, "connected": False, "error": ""}
    try:
        response = await client.get(f"{url}/system_stats", headers=headers, timeout=CONNECT_TIMEOUT)
        response.raise_for_status()
        stats = response.json()
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        result["error"] = f"无法连接 ComfyUI（{url}），请确认它已启动：{exc}"
        return result

    result["connected"] = True
    if isinstance(stats, dict):
        system = stats.get("system") or {}
        result["system"] = {
            "comfyuiVersion": system.get("comfyui_version"),
            "pythonVersion": system.get("python_version"),
            "os": system.get("os"),
        }
        devices = stats.get("devices") or []
        if devices and isinstance(devices[0], dict):
            device = devices[0]
            result["device"] = device.get("name")
            result["vramTotal"] = device.get("vram_total")
    try:
        result["models"] = await fetch_models(url, api_key, client)
    except Exception as exc:  # noqa: BLE001 - listing is best-effort
        result["models"] = {"checkpoints": [], "samplers": [], "schedulers": []}
        result["modelsError"] = f"读取节点信息失败：{exc}"
    return result


async def fetch_models(base_url: str, api_key: str, client: httpx.AsyncClient) -> Dict[str, List[str]]:
    url = _base(base_url)
    response = await client.get(f"{url}/object_info", headers=_headers(api_key), timeout=INFO_TIMEOUT)
    response.raise_for_status()
    info = response.json()
    if not isinstance(info, dict):
        raise ComfyUIError("ComfyUI 返回的节点信息不是对象", kind="api")

    def combo(node: str, field: str) -> List[str]:
        spec = (((info.get(node) or {}).get("input") or {}).get("required") or {}).get(field)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return [str(item) for item in spec[0]]
        return []

    return {
        "checkpoints": combo("CheckpointLoaderSimple", "ckpt_name"),
        "samplers": combo("KSampler", "sampler_name"),
        "schedulers": combo("KSampler", "scheduler"),
    }


async def _submit(
    base_url: str,
    api_key: str,
    workflow: Dict[str, Any],
    client: httpx.AsyncClient,
    client_id: str,
) -> str:
    url = _base(base_url)
    # The progress socket must use this exact id: ComfyUI addresses progress
    # messages to the client that submitted the prompt, not to every socket.
    payload = {"prompt": workflow, "client_id": client_id}
    try:
        response = await client.post(
            f"{url}/prompt", json=payload, headers=_headers(api_key), timeout=SUBMIT_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ComfyUIError(f"提交到 ComfyUI 失败：{exc}", kind="network") from exc

    text = ""
    try:
        body = response.json()
    except ValueError:
        body = None
        text = response.text[:300]

    if response.status_code >= 400:
        reason = _error_text(body) or text or f"HTTP {response.status_code}"
        raise ComfyUIError(f"ComfyUI 拒绝了该工作流：{reason}", kind="config")

    if not isinstance(body, dict):
        raise ComfyUIError("ComfyUI 返回的不是 JSON", kind="api")
    prompt_id = body.get("prompt_id")
    if not prompt_id:
        raise ComfyUIError(f"ComfyUI 没有返回任务 id：{_error_text(body) or body}", kind="api")
    return str(prompt_id)


def _collect_images(outputs: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if not isinstance(outputs, dict):
        return found
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        images = node_output.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and image.get("filename"):
                    found.append(image)
    # Output files are the durable ones; temp previews only appear when the graph
    # has no SaveImage node at all.
    saved = [image for image in found if str(image.get("type") or "") == "output"]
    return saved or found


async def _wait(
    base_url: str,
    api_key: str,
    prompt_id: str,
    timeout: float,
    client: httpx.AsyncClient,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, Any]]:
    url = _base(base_url)
    deadline = time.monotonic() + max(5.0, float(timeout))
    while True:
        # A cancelled prompt may be deleted from the queue so cleanly that no
        # terminal history event ever appears; without this the loop would burn
        # the full timeout while holding the studio's single job slot.
        if should_stop is not None and should_stop():
            raise ComfyUIError("任务已取消", kind="cancelled")
        try:
            response = await client.get(
                f"{url}/history/{prompt_id}", headers=_headers(api_key), timeout=SUBMIT_TIMEOUT
            )
        except httpx.HTTPError as exc:
            # A dropped connection mid-job means the server itself went away.
            # Retrying would burn the whole timeout against a closed port, so the
            # failure is reported now with something the user can act on.
            raise ComfyUIError(
                f"与 ComfyUI 的连接中断（{exc.__class__.__name__}），请确认它仍在运行后重试",
                kind="network",
            ) from exc

        if response.status_code == 200:
            try:
                payload = response.json() or {}
            except ValueError:
                payload = {}
            entry = payload.get(prompt_id) if isinstance(payload, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                if str(status.get("status_str") or "") == "error":
                    raise ComfyUIError(
                        _status_error(status) or "ComfyUI 执行工作流时出错", kind="api"
                    )
                images = _collect_images(entry.get("outputs") or {})
                if images:
                    return images
                if status.get("completed"):
                    raise ComfyUIError(
                        "工作流执行完成但没有图片：请确认图里有 Save Image 节点", kind="empty"
                    )

        if time.monotonic() >= deadline:
            raise ComfyUIError(
                f"ComfyUI 生成超时（{int(timeout)} 秒）。本地模型首次加载较慢，可以在参数里调大超时。",
                kind="timeout",
            )
        await asyncio.sleep(POLL_INTERVAL)


async def _fetch(
    base_url: str, api_key: str, image: Dict[str, Any], client: httpx.AsyncClient
) -> Tuple[bytes, str]:
    url = _base(base_url)
    params = {
        "filename": str(image.get("filename") or ""),
        "type": str(image.get("type") or "output"),
        "subfolder": str(image.get("subfolder") or ""),
    }
    try:
        response = await client.get(
            f"{url}/view", params=params, headers=_headers(api_key), timeout=INFO_TIMEOUT
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ComfyUIError(f"从 ComfyUI 取图失败：{exc}", kind="download") from exc

    data = response.content
    if not data:
        raise ComfyUIError("ComfyUI 返回了空图片", kind="empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ComfyUIError("ComfyUI 返回的图片过大", kind="download")
    declared = response.headers.get("content-type", "")
    return data, declared


async def generate(
    base_url: str,
    api_key: str,
    workflow_name: str,
    values: Dict[str, Any],
    timeout: float,
    client: httpx.AsyncClient,
    on_job_id: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[bytes, str, Optional[int]]:
    """Render, submit, wait, download. Returns bytes, declared mime and seed.

    `on_job_id` and `on_progress` are optional. The studio passes them to show a
    sampling bar and to be able to interrupt the prompt later; without them this
    behaves exactly as before.
    """
    workflow = load_workflow(workflow_name)
    prepared = render(workflow, values)
    client_id = uuid.uuid4().hex
    prompt_id = await _submit(base_url, api_key, prepared, client, client_id)
    if on_job_id is not None:
        try:
            on_job_id(prompt_id)
        except Exception:  # noqa: BLE001 - a listener must not break the job
            logger.debug("comfyui on_job_id callback failed", exc_info=True)

    stop = asyncio.Event()
    tracker: Optional[asyncio.Task] = None
    if on_progress is not None:
        tracker = asyncio.create_task(
            _track_progress(base_url, prompt_id, client_id, on_progress, stop)
        )
    try:
        images = await _wait(base_url, api_key, prompt_id, timeout, client, should_stop)
    finally:
        stop.set()
        if tracker is not None:
            tracker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tracker
    data, mime = await _fetch(base_url, api_key, images[0], client)
    seed = values.get("seed")
    return data, mime, int(seed) if isinstance(seed, (int, float)) else None


async def _track_progress(
    base_url: str,
    prompt_id: str,
    client_id: str,
    on_progress: Callable[[int, int], None],
    stop: asyncio.Event,
) -> None:
    """Follow one prompt's sampling steps over ComfyUI's WebSocket.

    This is the one thing the HTTP surface does not expose: `/api/jobs/{id}`
    carries a status, never a step counter. ComfyUI addresses progress to the
    client id that submitted the prompt, which is why `generate()` mints one id
    and reuses it for both the submission and this socket.

    Every failure is swallowed on purpose. A broken progress feed must never
    fail an image that is still being drawn.
    """
    ws_base = _base(base_url).replace("https://", "wss://").replace("http://", "ws://")
    # Same id the prompt was submitted with. A fresh id here would connect fine
    # and then receive nothing.
    ws_url = f"{ws_base}/ws?clientId={client_id}"
    try:
        async with websockets.connect(
            ws_url, open_timeout=5, close_timeout=1, max_size=16 * 1024 * 1024
        ) as socket:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    # Preview frames; the counters are all we need.
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                kind = message.get("type")
                data = message.get("data") or {}
                if kind == "progress":
                    if data.get("prompt_id") == prompt_id:
                        try:
                            on_progress(int(data.get("value") or 0), int(data.get("max") or 0))
                        except Exception:  # noqa: BLE001
                            logger.debug("comfyui progress callback failed", exc_info=True)
                elif kind in ("execution_success", "execution_error", "execution_interrupted"):
                    if data.get("prompt_id") in (None, prompt_id):
                        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - best effort by design
        logger.debug("comfyui progress socket ended: %r", exc)


async def cancel(
    base_url: str, api_key: str, prompt_id: str, client: httpx.AsyncClient
) -> bool:
    """Interrupt a running prompt and drop it from the queue if it is waiting.

    A queued prompt is not executing yet, so `/interrupt` ignores it; a running
    one is not in the pending list, so `/queue delete` ignores it. Sending both
    covers either state without asking the server which one it is in.
    """
    url = _base(base_url)
    cancelled = False
    for path, payload in (
        ("/interrupt", {"prompt_id": prompt_id}),
        ("/queue", {"delete": [prompt_id]}),
    ):
        try:
            response = await client.post(
                f"{url}{path}", json=payload, headers=_headers(api_key), timeout=SUBMIT_TIMEOUT
            )
            if response.status_code < 400:
                cancelled = True
        except httpx.HTTPError as exc:
            logger.debug("comfyui cancel %s failed: %r", path, exc)
    return cancelled

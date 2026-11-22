"""ComfyUI bridge for the picture studio.

Everything here talks HTTP to a running ComfyUI instance. Nothing reads the
ComfyUI installation directory on disk: the install path differs on every
machine, so the only contract is `baseUrl` plus ComfyUI's HTTP endpoints
(`/prompt`, `/history/{id}`, `/view`, `/object_info`, `/system_stats`,
`/userdata`).

Three things make a workflow usable:

1. Placeholders. A template may carry `{{prompt}}`, `{{seed}}` and friends. If it
   does, those are substituted and nothing else is touched.
2. Automatic injection. A workflow without placeholders has the prompt, seed,
   latent size and (optionally) sampler knobs written into the nodes that
   declare them, so both exported API graphs and ComfyUI's own saved canvas
   workflows (converted via `/object_info`) work drop-in.
3. Selection. Built-in templates live in code; imported ones live in
   `config/comfyui/*.json`; user-saved ComfyUI workflows are discovered from the
   running instance over `/userdata?dir=workflows`.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import time
import urllib.parse
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
COMFYUI_WORKFLOW_PREFIX = "comfyui:"

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
    "TextEncodeQwenImage21",
}
_DUAL_PROMPT_NODES = {"TextEncodeQwenImage21"}
_SAMPLER_NODES = {"KSampler", "KSamplerAdvanced", "RandomNoise"}
_LATENT_NODES = {
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "EmptyHunyuanLatentVideo",
    "EmptyLTXVLatentVideo",
    "EmptyMochiLatentVideo",
}
_SAVE_IMAGE_NODES = {"SaveImage", "SaveImageAdvanced"}
_IMAGE_OUTPUT_NODES = {
    "SaveImage",
    "PreviewImage",
    "SaveImageAdvanced",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
}
_VIRTUAL_SKIP_NODES = {"MarkdownNote", "Note"}
_WIDGET_KINDS = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}

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


def is_ui_workflow(data: Any) -> bool:
    """True for ComfyUI's LiteGraph canvas JSON (`nodes` + `links`)."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("nodes"), list)
        and isinstance(data.get("links"), list)
    )


def _validate_workflow(data: Any) -> Dict[str, Any]:
    """Accept only ComfyUI's API format.

    The canvas saves {"nodes": [...], "links": [...]}, which `/prompt` rejects
    with a bare 400. Catching it here turns that into an instruction the user can
    act on: re-export with "Export (API)".
    """
    if not isinstance(data, dict) or not data:
        raise ComfyUIError("工作流内容为空：需要 ComfyUI 的 API 格式，不是界面保存的 UI 格式", kind="config")
    if is_ui_workflow(data):
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


def _workflow_loader_type(api_workflow: Dict[str, Any]) -> str:
    has_ckpt = any(
        isinstance(node, dict) and node.get("class_type") == "CheckpointLoaderSimple"
        for node in api_workflow.values()
    )
    has_unet = any(
        isinstance(node, dict) and node.get("class_type") == "UNETLoader"
        for node in api_workflow.values()
    )
    if has_unet and not has_ckpt:
        return "unet"
    if has_ckpt and not has_unet:
        return "checkpoint"
    if has_unet and has_ckpt:
        return "both"
    return "none"


def _workflow_sampler_defaults(
    api_workflow: Dict[str, Any], is_builtin: bool = False
) -> Dict[str, Any]:
    """Extract initial sampler knobs from the workflow's primary sampler node."""
    if is_builtin:
        return {
            "steps": 20,
            "cfg": 7,
            "sampler": "euler",
            "scheduler": "normal",
            "denoise": 1,
        }

    samplers = [
        node
        for node in api_workflow.values()
        if isinstance(node, dict) and node.get("class_type") in ("KSampler", "KSamplerAdvanced")
    ]
    # Prefer KSampler over KSamplerAdvanced when both appear.
    samplers.sort(key=lambda n: 0 if n.get("class_type") == "KSampler" else 1)
    if not samplers:
        return {}

    inputs = samplers[0].get("inputs") or {}
    out: Dict[str, Any] = {}
    for target_key, input_key in (
        ("steps", "steps"),
        ("cfg", "cfg"),
        ("sampler", "sampler_name"),
        ("scheduler", "scheduler"),
        ("denoise", "denoise"),
    ):
        val = inputs.get(input_key)
        if val is None or isinstance(val, (list, dict)):
            continue
        if isinstance(val, str) and _PLACEHOLDER.search(val):
            continue
        out[target_key] = val
    return out


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
                "readonly": True,
                "source": "builtin",
                "loaderType": _workflow_loader_type(meta["workflow"]),
                "samplerDefaults": _workflow_sampler_defaults(meta["workflow"], is_builtin=True),
            }
        )
    directory = workflow_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            loader_type = ""
            sampler_defaults: Dict[str, Any] = {}
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    loader_type = _workflow_loader_type(raw)
                    sampler_defaults = _workflow_sampler_defaults(raw)
            except (OSError, json.JSONDecodeError):
                pass
            out.append(
                {
                    "id": path.stem,
                    "label": path.stem,
                    "note": "",
                    "builtin": False,
                    "readonly": False,
                    "source": "imported",
                    "loaderType": loader_type,
                    "samplerDefaults": sampler_defaults,
                }
            )
    return out


def _safe_comfy_rel_path(workflow_id: str) -> str:
    """Extract and validate a relative workflow path from `comfyui:<stem>`."""
    if not workflow_id.startswith(COMFYUI_WORKFLOW_PREFIX):
        raise ComfyUIError(f"无效的 ComfyUI 工作流标识：{workflow_id}", kind="config")
    rel = workflow_id[len(COMFYUI_WORKFLOW_PREFIX):].strip().replace("\\", "/").lstrip("/")
    if rel.lower().endswith(".json"):
        rel = rel[:-5]
    parts = rel.split("/") if rel else []
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ComfyUIError(f"无效的 ComfyUI 工作流路径：{workflow_id}", kind="config")
    return "/".join(parts) + ".json"


def load_workflow(name: str) -> Dict[str, Any]:
    key = str(name or "").strip()
    if not key:
        raise ComfyUIError("还没有选择工作流", kind="config")
    if key.startswith(COMFYUI_WORKFLOW_PREFIX):
        raise ComfyUIError(f"工作流「{key}」需要连接 ComfyUI 实例才能读取", kind="config")
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
    """Store an imported workflow. Built-in and ComfyUI prefixes are reserved."""
    cleaned = _sanitise_name(name)
    if BUILTIN_PREFIX in cleaned or COMFYUI_WORKFLOW_PREFIX in cleaned:
        raise ComfyUIError("工作流名称不能包含 builtin: 或 comfyui: 前缀", kind="config")
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
    return {
        "id": cleaned,
        "label": cleaned,
        "note": "",
        "builtin": False,
        "readonly": False,
        "source": "imported",
        "loaderType": _workflow_loader_type(payload),
        "samplerDefaults": _workflow_sampler_defaults(payload),
        "nodes": len(payload),
    }


def delete_workflow(name: str) -> None:
    key = str(name or "").strip()
    if key in BUILTIN_WORKFLOWS:
        raise ComfyUIError("内置工作流不能删除", kind="config")
    if key.startswith(COMFYUI_WORKFLOW_PREFIX):
        raise ComfyUIError("ComfyUI 用户工作流请在 ComfyUI 里删除", kind="config")
    path = workflow_dir() / f"{key}.json"
    if not path.is_file():
        raise ComfyUIError(f"工作流「{key}」不存在", kind="config")
    try:
        path.unlink()
    except OSError as exc:
        raise ComfyUIError(f"删除工作流失败：{exc}", kind="config") from exc


# --- UI -> API workflow conversion -----------------------------------------


def _parse_links(raw_links: Any) -> Dict[int, Tuple[str, int, str]]:
    """Index canvas links as `link_id -> (origin_node_id, origin_slot, type)`."""
    links: Dict[int, Tuple[str, int, str]] = {}
    if not isinstance(raw_links, list):
        return links
    for item in raw_links:
        if isinstance(item, (list, tuple)) and len(item) >= 5:
            link_id, origin_id, origin_slot = item[0], item[1], item[2]
            link_type = str(item[5]) if len(item) > 5 and item[5] is not None else "*"
        elif isinstance(item, dict) and "id" in item and "origin_id" in item:
            link_id = item["id"]
            origin_id = item["origin_id"]
            origin_slot = item.get("origin_slot", 0)
            link_type = str(item.get("type") or "*")
        else:
            continue
        if link_id is not None and origin_id is not None:
            links[int(link_id)] = (str(origin_id), int(origin_slot or 0), link_type)
    return links


def _node_input_specs(node_meta: Dict[str, Any]) -> Tuple[List[Tuple[str, Any]], Dict[str, Any]]:
    """Return ordered `(name, spec)` pairs and a flat dict for all declared inputs."""
    inputs_section = node_meta.get("input") or {}
    order_section = node_meta.get("input_order") or {}
    ordered: List[Tuple[str, Any]] = []
    all_specs: Dict[str, Any] = {}
    for bucket in ("required", "optional"):
        bucket_specs = inputs_section.get(bucket)
        if not isinstance(bucket_specs, dict):
            continue
        all_specs.update(bucket_specs)
        keys = order_section.get(bucket)
        if not isinstance(keys, list):
            keys = list(bucket_specs.keys())
        for key in keys:
            if key in bucket_specs:
                ordered.append((str(key), bucket_specs[key]))
    return ordered, all_specs


def _widget_slots(node_meta: Dict[str, Any]) -> List[Optional[str]]:
    """List the positional `widgets_values` slots for a node type.

    Synthetic UI widgets (`control_after_generate` after seed, `upload` button
    after image_upload) occupy a slot in `widgets_values` but have no backend
    input name, so they are represented as `None` and skipped when reading values.
    """
    ordered, _ = _node_input_specs(node_meta)
    slots: List[Optional[str]] = []
    for name, spec in ordered:
        if not isinstance(spec, (list, tuple)) or not spec:
            continue
        cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if cfg.get("forceInput"):
            continue
        kind = spec[0]
        is_widget = isinstance(kind, list) or (isinstance(kind, str) and kind in _WIDGET_KINDS)
        if not is_widget:
            continue
        slots.append(name)
        if cfg.get("control_after_generate"):
            slots.append(None)
        if cfg.get("image_upload"):
            slots.append(None)
    return slots


def ui_to_api(data: Any, object_info: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a ComfyUI canvas UI workflow into submittable API prompt format."""
    if not is_ui_workflow(data):
        raise ComfyUIError("工作流不是 ComfyUI 界面格式", kind="config")
    if not isinstance(object_info, dict) or not object_info:
        raise ComfyUIError("缺少 ComfyUI 节点定义（/object_info），无法转换界面工作流", kind="api")

    raw_nodes = data.get("nodes") or []
    subgraphs = {
        str(sg.get("id"))
        for sg in ((data.get("definitions") or {}).get("subgraphs") or [])
        if isinstance(sg, dict) and sg.get("id")
    }
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    for node in raw_nodes:
        if isinstance(node, dict) and node.get("id") is not None:
            nodes_by_id[str(node["id"])] = node

    links_by_id = _parse_links(data.get("links"))

    def resolve_link(link_id: int, seen: set[int]) -> Optional[Tuple[str, Any]]:
        if link_id in seen:
            return None
        entry = links_by_id.get(int(link_id))
        if entry is None:
            return None
        origin_id, origin_slot, link_type = entry
        src_node = nodes_by_id.get(origin_id)
        if src_node is None:
            return None
        mode = int(src_node.get("mode") or 0)
        if mode == 2:  # NEVER (muted)
            return None
        src_type = str(src_node.get("type") or "")
        if src_type == "PrimitiveNode":
            vals = src_node.get("widgets_values")
            if isinstance(vals, list) and vals:
                return ("value", vals[0])
            return None
        if src_type == "Reroute":
            for inp in src_node.get("inputs") or []:
                if isinstance(inp, dict) and inp.get("link") is not None:
                    return resolve_link(int(inp["link"]), seen | {link_id})
            return None
        if mode == 4:  # BYPASS: forward matching-type input to output
            outputs = src_node.get("outputs") or []
            out_type = link_type
            if 0 <= origin_slot < len(outputs) and isinstance(outputs[origin_slot], dict):
                out_type = str(outputs[origin_slot].get("type") or out_type)
            for inp in src_node.get("inputs") or []:
                if not isinstance(inp, dict) or inp.get("link") is None:
                    continue
                inp_type = str(inp.get("type") or "*")
                if inp_type == out_type or out_type == "*" or inp_type == "*":
                    return resolve_link(int(inp["link"]), seen | {link_id})
            return None
        return ("node", [origin_id, origin_slot])

    api_graph: Dict[str, Any] = {}
    for node in raw_nodes:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        node_id = str(node["id"])
        mode = int(node.get("mode") or 0)
        if mode in (2, 4):  # muted or bypassed nodes do not execute
            continue
        node_type = str(node.get("type") or "").strip()
        if not node_type or node_type in _VIRTUAL_SKIP_NODES or node_type in ("Reroute", "PrimitiveNode"):
            continue
        if node_type in subgraphs:
            raise ComfyUIError(f"暂不支持包含子图（Subgraph）的工作流：{node_type}", kind="config")
        node_meta = object_info.get(node_type)
        if not isinstance(node_meta, dict):
            # Unconnected custom comment/group widgets have no outputs and no links;
            # ignore them instead of failing the whole workflow.
            has_outputs = any(
                isinstance(out, dict) and out.get("links")
                for out in (node.get("outputs") or [])
            )
            if not has_outputs:
                continue
            raise ComfyUIError(f"ComfyUI 未安装节点「{node_type}」，无法转换该工作流", kind="config")

        api_inputs: Dict[str, Any] = {}
        for inp in node.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            inp_name = str(inp.get("name") or "").strip()
            link_id = inp.get("link")
            if not inp_name or link_id is None:
                continue
            resolved = resolve_link(int(link_id), set())
            if resolved is not None:
                api_inputs[inp_name] = resolved[1]

        _, all_specs = _node_input_specs(node_meta)
        named_widgets = node.get("widgets_values_named")
        raw_widgets = node.get("widgets_values")
        if isinstance(named_widgets, dict) and named_widgets:
            for key, value in named_widgets.items():
                if key in all_specs and key not in api_inputs and value is not None:
                    api_inputs[key] = value
        elif isinstance(raw_widgets, dict) and raw_widgets:
            for key, value in raw_widgets.items():
                if key in all_specs and key not in api_inputs and value is not None:
                    api_inputs[key] = value
        elif isinstance(raw_widgets, list):
            slots = _widget_slots(node_meta)
            for idx, slot_name in enumerate(slots):
                if idx >= len(raw_widgets):
                    break
                if slot_name is None or slot_name in api_inputs:
                    continue
                if raw_widgets[idx] is not None:
                    api_inputs[slot_name] = raw_widgets[idx]

        api_graph[node_id] = {
            "class_type": node_type,
            "inputs": api_inputs,
        }

    return _validate_workflow(api_graph)


def _has_image_output(api_workflow: Dict[str, Any]) -> bool:
    return any(
        isinstance(node, dict) and node.get("class_type") in _IMAGE_OUTPUT_NODES
        for node in api_workflow.values()
    )


def _has_load_image(api_workflow: Dict[str, Any]) -> bool:
    return any(
        isinstance(node, dict) and node.get("class_type") == "LoadImage"
        for node in api_workflow.values()
    )


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


def _write_prompt_node(node: Dict[str, Any], text: str, is_negative: bool) -> bool:
    class_type = node.get("class_type")
    inputs = node.setdefault("inputs", {})
    if class_type in _DUAL_PROMPT_NODES or ("prompt" in inputs and "negative_prompt" in inputs):
        if is_negative:
            if text:
                inputs["negative_prompt"] = text
        else:
            inputs["prompt"] = text
        return True
    if class_type in _TEXT_NODES or "text" in inputs:
        if is_negative and not text:
            return False
        inputs["text"] = text
        return True
    return False


def _inject(workflow: Dict[str, Any], values: Dict[str, Any]) -> None:
    """Write studio values into the nodes a workflow already contains.

    Only fields the node actually declares are touched, so a graph whose sampler
    lives inside a custom pack is left alone instead of being corrupted.
    """
    prompt_text = str(values.get("prompt") or "")
    negative_text = str(values.get("negative") or "")
    pos_done = False
    neg_done = False

    # Prefer resolving positive/negative directly from sampler/guider links so
    # node ordering never swaps positive and negative prompts.
    for node in workflow.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for role, text, is_neg in (
            ("positive", prompt_text, False),
            ("negative", negative_text, True),
        ):
            link = inputs.get(role)
            if isinstance(link, list) and link:
                target = workflow.get(str(link[0]))
                if isinstance(target, dict) and _write_prompt_node(target, text, is_neg):
                    if is_neg:
                        neg_done = True
                    else:
                        pos_done = True

    if not pos_done or (negative_text and not neg_done):
        text_nodes = [node for node in workflow.values() if node.get("class_type") in _TEXT_NODES]
        dual_nodes = [
            node
            for node in text_nodes
            if node.get("class_type") in _DUAL_PROMPT_NODES
            or ("prompt" in (node.get("inputs") or {}) and "negative_prompt" in (node.get("inputs") or {}))
        ]
        if dual_nodes:
            if not pos_done:
                _write_prompt_node(dual_nodes[0], prompt_text, False)
            if negative_text and not neg_done:
                _write_prompt_node(dual_nodes[0], negative_text, True)
        elif text_nodes:
            if not pos_done:
                _write_prompt_node(text_nodes[0], prompt_text, False)
            if negative_text and not neg_done and len(text_nodes) > 1:
                _write_prompt_node(text_nodes[1], negative_text, True)

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
        elif class_type == "UNETLoader":
            if values.get("unet"):
                inputs["unet_name"] = values["unet"]
        elif class_type in _SAVE_IMAGE_NODES:
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


async def fetch_object_info(
    base_url: str, api_key: str, client: httpx.AsyncClient
) -> Dict[str, Any]:
    url = _base(base_url)
    response = await client.get(f"{url}/object_info", headers=_headers(api_key), timeout=INFO_TIMEOUT)
    response.raise_for_status()
    info = response.json()
    if not isinstance(info, dict):
        raise ComfyUIError("ComfyUI 返回的节点信息不是对象", kind="api")
    return info


def _extract_models(info: Dict[str, Any]) -> Dict[str, List[str]]:
    def combo(node: str, field: str) -> List[str]:
        spec = (((info.get(node) or {}).get("input") or {}).get("required") or {}).get(field)
        if isinstance(spec, list) and spec:
            if isinstance(spec[0], list):
                return [str(item) for item in spec[0]]
            if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
                opts = spec[1].get("options")
                if isinstance(opts, list):
                    return [str(item) for item in opts]
        return []

    return {
        "checkpoints": combo("CheckpointLoaderSimple", "ckpt_name"),
        "unets": combo("UNETLoader", "unet_name"),
        "samplers": combo("KSampler", "sampler_name"),
        "schedulers": combo("KSampler", "scheduler"),
    }


async def fetch_models(base_url: str, api_key: str, client: httpx.AsyncClient) -> Dict[str, List[str]]:
    info = await fetch_object_info(base_url, api_key, client)
    return _extract_models(info)


async def _fetch_userdata_json(
    url: str, headers: Dict[str, str], rel_file: str, client: httpx.AsyncClient
) -> Any:
    encoded = urllib.parse.quote(rel_file, safe="")
    response = await client.get(f"{url}/userdata/{encoded}", headers=headers, timeout=INFO_TIMEOUT)
    response.raise_for_status()
    return response.json()


async def fetch_user_workflows(
    base_url: str,
    api_key: str,
    client: httpx.AsyncClient,
    object_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Discover image workflows saved in ComfyUI's user directory via `/userdata`."""
    url = _base(base_url)
    headers = _headers(api_key)
    list_resp = await client.get(
        f"{url}/userdata",
        params={"dir": "workflows", "recurse": "true"},
        headers=headers,
        timeout=CONNECT_TIMEOUT,
    )
    if list_resp.status_code == 404:
        return []
    list_resp.raise_for_status()
    entries = list_resp.json()
    if not isinstance(entries, list):
        return []

    json_files = sorted(
        str(item).replace("\\", "/").lstrip("/")
        for item in entries
        if isinstance(item, str) and item.lower().endswith(".json")
    )
    if not json_files:
        return []

    if object_info is None:
        object_info = await fetch_object_info(url, api_key, client)

    sem = asyncio.Semaphore(8)

    async def inspect_one(rel: str) -> Optional[Dict[str, Any]]:
        async with sem:
            try:
                raw = await _fetch_userdata_json(url, headers, f"workflows/{rel}", client)
            except Exception as exc:  # noqa: BLE001
                logger.debug("comfyui userdata workflow read failed (%s): %r", rel, exc)
                return None

        try:
            api_graph = ui_to_api(raw, object_info) if is_ui_workflow(raw) else _validate_workflow(raw)
        except ComfyUIError as exc:
            logger.debug("comfyui workflow skipped (%s): %s", rel, exc)
            return None

        if not _has_image_output(api_graph):
            return None

        stem = rel[:-5] if rel.lower().endswith(".json") else rel
        note = f"来自 ComfyUI 用户工作流（workflows/{rel}）"
        if _has_load_image(api_graph):
            note += "；含 LoadImage 节点，将使用工作流内预设参考图"
        return {
            "id": f"{COMFYUI_WORKFLOW_PREFIX}{stem}",
            "label": f"{stem}（ComfyUI）",
            "note": note,
            "builtin": False,
            "readonly": True,
            "source": "comfyui",
            "loaderType": _workflow_loader_type(api_graph),
            "samplerDefaults": _workflow_sampler_defaults(api_graph),
        }

    items = await asyncio.gather(*(inspect_one(rel) for rel in json_files))
    return [item for item in items if item is not None]


async def load_user_workflow(
    base_url: str,
    api_key: str,
    workflow_id: str,
    client: httpx.AsyncClient,
    object_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Load and convert one workflow from ComfyUI's `/userdata/workflows/`."""
    rel = _safe_comfy_rel_path(workflow_id)
    url = _base(base_url)
    headers = _headers(api_key)
    try:
        raw = await _fetch_userdata_json(url, headers, f"workflows/{rel}", client)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ComfyUIError(f"ComfyUI 中未找到工作流「{rel}」，可能已被移动或删除", kind="config") from exc
        raise ComfyUIError(f"读取 ComfyUI 工作流失败：HTTP {exc.response.status_code}", kind="api") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ComfyUIError(f"读取 ComfyUI 工作流失败：{exc}", kind="network") from exc

    if is_ui_workflow(raw):
        if object_info is None:
            try:
                object_info = await fetch_object_info(url, api_key, client)
            except (httpx.HTTPError, ValueError) as exc:
                raise ComfyUIError(f"读取 ComfyUI 节点信息失败：{exc}", kind="network") from exc
        return ui_to_api(raw, object_info)
    return copy.deepcopy(_validate_workflow(raw))


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

    object_info: Optional[Dict[str, Any]] = None
    try:
        object_info = await fetch_object_info(url, api_key, client)
        result["models"] = _extract_models(object_info)
    except Exception as exc:  # noqa: BLE001 - listing is best-effort
        result["models"] = {"checkpoints": [], "unets": [], "samplers": [], "schedulers": []}
        result["modelsError"] = f"读取节点信息失败：{exc}"

    try:
        result["userWorkflows"] = await fetch_user_workflows(url, api_key, client, object_info)
    except Exception as exc:  # noqa: BLE001 - user workflow listing is best-effort
        logger.debug("comfyui user workflow scan failed: %r", exc)
        result["userWorkflows"] = []
    return result


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
    key = str(workflow_name or "").strip()
    if key.startswith(COMFYUI_WORKFLOW_PREFIX):
        workflow = await load_user_workflow(base_url, api_key, key, client)
    else:
        workflow = load_workflow(key)
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

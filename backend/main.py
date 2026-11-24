"""Pebble Companion Chat — 独立后端入口"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .api_routes import router as companion_router
from .script_routes import router as script_router
from .picturesys import router as picture_router
from .picturesys import service as picture_service
from .paths import (
    DATA_ROOT,
    LLM_CONFIG_PATH,
    UPLOAD_ROOT,
    audio_cpp_dir,
    audio_cpp_exe_name,
    ffmpeg_path,
    invalidate_ffmpeg_cache,
    is_ffmpeg_usable,
    load_app_config,
    llama_dir,
    llama_server_exe_name,
    models_root,
    save_app_config,
)
from .tts_service import router as tts_router
from .settings_routes import router as settings_router
from .group_sessions.routes import router as group_router
from .Inochi2D import router as inochi2d_router
from .llm_client import SUPPORTED_PROVIDERS
from . import audio_cpp_job
from .llama_server_engine import (
    llama_server, list_devices, resolve_device, MODEL_PATH, _server_exe,
    describe_models, resolve_default_model_key,
    upsert_custom_model, delete_custom_model, scan_gguf_models,
)

logger = logging.getLogger("main")

# Join the kill-on-exit job before anything can spawn an engine. Inheritance is
# decided at CreateProcess time, so a job created after the first Popen covers
# nothing: that ordering is why audiocpp survived a hard kill while llama-server
# did not. ensure() is idempotent and cheap.
audio_cpp_job.ensure()


def _shutdown_stt() -> None:
    """End both STT servers on the way out.

    STT only. Those two are the engines whose processes were being orphaned, and
    they have no reuse path worth keeping across a worker swap. Stopping the TTS
    and LLM engines here would make every `--reload` restart reload gigabytes of
    weights; their exit path stays `run_backend` plus their own atexit hooks.

    Synchronous on purpose: the port is already closed and nothing else is
    waiting, while a background task could be cancelled before the child is gone.
    """
    from .engine_shutdown import shutdown_stt

    try:
        shutdown_stt()
    except Exception:
        logger.warning("stt shutdown failed", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup work that has to run in both launch modes.

    The dev server runs `uvicorn backend.main:app` directly and never touches
    run_backend.py, so anything placed there silently does nothing during
    development. A database snapshot belongs here instead.

    Backup is insurance, not a feature: a failure is logged and the app still
    starts.
    """
    try:
        from .backup import snapshot, snapshot_configs

        snapshot()
        snapshot_configs()
    except Exception:
        logger.warning("database snapshot skipped", exc_info=True)
    try:
        yield
    finally:
        # This is the exit path `uvicorn --reload` takes, and the reason the STT
        # engines cannot rely on run_backend alone: a replaced worker shuts down
        # through the ASGI lifespan and never reaches that file.
        _shutdown_stt()


app = FastAPI(title="Companion Chat API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Must stay False: a wildcard origin combined with credentials is forbidden
    # by the CORS spec, and Starlette answers such a preflight with 400. The
    # shell sends no cookies, so nothing is lost.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # The packaged shell serves the app from http://tauri.localhost, which
    # WebView2 classifies as a public origin. Without this flag the preflight
    # from public to loopback is rejected with "Disallowed CORS private-network"
    # and the frontend only ever sees a bare "Failed to fetch".
    allow_private_network=True,
)

app.include_router(companion_router)
app.include_router(script_router)
app.include_router(tts_router)
app.include_router(picture_router)
app.include_router(settings_router)
app.include_router(group_router)
app.include_router(inochi2d_router)

# Picture Studio: create the output folder and close rows a previous process
# left mid-flight. Wrapped because a locked or unreadable database here must
# cost the feature, not the whole backend: nothing else depends on this module.
try:
    picture_service.init()
except Exception:  # noqa: BLE001
    logger.exception("picture studio init failed; image generation is unavailable")

# STT 引擎 — 注入到 api_routes 模块供 companion_chat_audio_stream 使用
# Qwen3-ASR 走 audio.cpp 常驻子进程（懒加载 + 闲置 2 分钟卸载显存）；
# 其构造函数在 exe/模型缺失时会快速抛错，所以用延迟导入避免拖垮整个后端启动
try:
# STT engine is created on demand by stt_registry, not at import time.
    # 挂到 api_routes 模块上，_get_stt_engine() 能从 sys.modules 中找到
    import sys
    api_mod = sys.modules.get('backend.api_routes')
    if api_mod:
        logger.info("Qwen3-ASR engine injected into api_routes")
except Exception as e:
    logger.warning(f"Qwen3-ASR init failed (audio chat will 503): {e}")

# The packaged shell tunnels every API call through this socket; see ws_bridge.
# Installed before the static mount so a failure here cannot be masked by an
# exception thrown later in the module.
from .ws_bridge import install as _install_ws_bridge

_install_ws_bridge(app)

# Realtime call: raw PCM up, transcript deltas down. Separate socket from /ws,
# see the module docstring for why.
from .ws_call import install as _install_ws_call

_install_ws_call(app)
logger.info("ws bridge installed, routes=%d", len(app.routes))

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_ROOT)), name="uploads")

_UPLOAD_ACTIVE_EXTS = (".svg", ".svgz", ".html", ".htm", ".xhtml", ".xml")
_SANDBOX_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'"


@app.middleware("http")
async def _harden_upload_responses(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path.lower()
    if path.startswith("/uploads/") or path.startswith("/thumbs/"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if path.endswith(_UPLOAD_ACTIVE_EXTS):
            # Neutralize inline scripts/active content in uploaded SVGs or HTML/XML.
            response.headers["Content-Security-Policy"] = _SANDBOX_CSP
            if path.startswith("/uploads/"):
                response.headers.setdefault("Content-Disposition", "attachment")
    return response


THUMB_ROOT = DATA_ROOT / "thumbs"


def _write_thumb(src: Path, dst: Path, width: int) -> None:
    """Render one scaled copy. Runs off the event loop."""
    from PIL import Image

    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.mode else "RGB")
        if im.width > width:
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.LANCZOS)
        # Unique temp name: every card asks for the same thumbnail at once on
        # first paint. A shared temp path made those requests fight over the
        # file (WinError 32/5) and lose the race to os.replace.
        tmp = dst.with_name(f"{dst.name}.{os.urandom(6).hex()}.tmp")
        try:
            im.save(tmp, "WEBP", quality=82, method=4)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


# Formats the thumbnailer may serve. Anything else under the uploads tree, and
# everything outside it, is rejected before a byte is read.
THUMB_SERVABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".apng", ".svg"}


@app.get("/thumbs/{path:path}")
async def serve_thumb(path: str, w: int = 256):
    """Scaled copy of an upload.

    Avatars are kept at whatever size they were uploaded - the largest here is
    21 MB - while the cast list draws them at about 40 px. Sending originals
    over the socket is what made the drawer fill in one card at a time.
    """
    import anyio
    from fastapi.responses import Response

    # Paths arrive as stored in the DB, which are relative to DATA_ROOT and
    # carry the "uploads/" prefix ("uploads/companion_avatars/x.png"). They are
    # resolved against DATA_ROOT, but containment is checked against UPLOAD_ROOT:
    # DATA_ROOT also holds companions.db and the provider config, so accepting
    # anything under it would serve the database to any caller.
    root = DATA_ROOT.resolve()
    uploads = UPLOAD_ROOT.resolve()
    src = (root / path).resolve()
    if not src.is_relative_to(uploads) or not src.is_file():
        raise HTTPException(status_code=404, detail="not found")
    suffix = src.suffix.lower()
    if suffix not in THUMB_SERVABLE_SUFFIXES:
        raise HTTPException(status_code=404, detail="not found")
    if suffix == ".svg":
        body = await anyio.to_thread.run_sync(src.read_bytes)
        return Response(
            content=body,
            media_type="image/svg+xml",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": _SANDBOX_CSP,
            },
        )

    # The scene column is roughly 450x860 CSS px and draws with object-fit:
    # cover, so a 512 copy is upscaled ~1.7x and reads as soft. 2048 leaves
    # headroom for HiDPI without going back to shipping the 21 MB original.
    width = max(32, min(2048, int(w)))
    # Rebuild the relative path from the resolved source so ".." segments in the
    # request cannot steer the cache write outside THUMB_ROOT.
    dst = THUMB_ROOT / str(width) / src.relative_to(root)
    dst = dst.with_name(dst.name + ".webp")

    if not (dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime):
        try:
            await anyio.to_thread.run_sync(_write_thumb, src, dst, width)
        except Exception as exc:  # noqa: BLE001 - fall back to the original
            logger.warning("thumb %s failed: %s", path, exc)
            body = await anyio.to_thread.run_sync(src.read_bytes)
            return Response(content=body, media_type="application/octet-stream")

    # Buffered rather than FileResponse: these are a few KB, and a streaming
    # response reopens the path after the reply has started, which loses the
    # race against a concurrent os.replace on the same file.
    body = await anyio.to_thread.run_sync(dst.read_bytes)
    return Response(content=body, media_type="image/webp")


class LLMProviderConfig(BaseModel):
    """单个 LLM 提供商配置（对应 llm_providers.json 中 providers 数组的元素）"""
    id: str = ""
    # 用户自定义的显示名。空字符串表示沿用 provider 的协议标签。
    # 必须显式声明：Pydantic 默认丢弃未声明字段，前端填的名字会存不进去。
    name: str = ""
    provider: str
    apiKey: str = ""
    baseUrl: str = ""
    models: List[str] = Field(default_factory=list)
    temperature: float = Field(default=0.6, ge=0, le=2)
    maxTokens: int = Field(default=4096, ge=1, le=1000000)
    topP: float = Field(default=0.9, ge=0, le=1)
    # Context window for this provider, in tokens. 0 means "use the built-in
    # table", so a user who never fills it keeps the previous behaviour.
    maxContext: int = Field(default=0, ge=0, le=10000000)
    # llama.cpp 服务端扩展采样参数。None 表示不下发，沿用服务端默认
    # （repeat_penalty 的服务端默认是 1.0，等于完全不惩罚）。
    # 默认值取 Gemma 4 12B QAT Uncensored（HauhauCS Balanced）作者给出的推荐参数。
    topK: Optional[int] = Field(default=64, ge=0, le=1000000)
    minP: Optional[float] = Field(default=0.05, ge=0, le=1)
    repeatPenalty: Optional[float] = Field(default=1.1, gt=0, le=5)
    # 本地 llama-server 要加载的权重 key（对应 llama_server_engine.MODEL_REGISTRY），
    # 以及是否挂载 MTP 投机解码头。必须落库，否则刷新后前端选中的权重会丢。
    localModelKey: str = ""
    enableMtp: bool = False
    # 本地 provider 每个权重各自一套采样参数，key 是 MODEL_REGISTRY 的 key。
    # 上面那组标量字段仍保留：云端 provider 只用它们，本地 provider 则作为兼容回退。
    modelParams: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class LLMConfigPayload(BaseModel):
    """POST /llm/config 请求体"""
    providers: List[LLMProviderConfig]
    activeProviderIndex: int = 0


def _load_llm_config():
    if LLM_CONFIG_PATH.exists():
        with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"providers": [], "activeProviderIndex": 0}


def _save_llm_config(config: dict) -> None:
    """原子写入 llm_providers.json：先写临时文件并落盘，再替换目标文件，避免写入中断损坏配置。"""
    tmp_path = LLM_CONFIG_PATH.parent / (LLM_CONFIG_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, LLM_CONFIG_PATH)


@app.get("/llm/config")
async def get_llm_config():
    return _load_llm_config()


@app.post("/llm/config")
async def update_llm_config(payload: LLMConfigPayload):
    """保存 LLM 提供商配置到 llm_providers.json（前端配置面板实时持久化）。"""
    providers = []
    for p in payload.providers:
        item = p.model_dump()
        # 清理模型列表：去空白、去空项、去重
        cleaned: List[str] = []
        for m in item["models"]:
            name = (m or "").strip()
            if name and name not in cleaned:
                cleaned.append(name)
        item["models"] = cleaned
        # 补全缺失的 id
        if not item["id"]:
            item["id"] = os.urandom(6).hex()
        # 校验提供商类型（与 llm_client 支持列表保持一致）
        if item["provider"] not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported provider type: {item['provider']}. Supported: {SUPPORTED_PROVIDERS}",
            )
        providers.append(item)

    active_idx = payload.activeProviderIndex
    if not providers:
        active_idx = 0
    elif active_idx < 0 or active_idx >= len(providers):
        active_idx = 0

    config = {"providers": providers, "activeProviderIndex": active_idx}
    try:
        _save_llm_config(config)
    except OSError as e:
        logger.error(f"Failed to write LLM config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save LLM config: {e}")

    logger.info(f"LLM config saved: {len(providers)} provider(s), active={active_idx}")
    return {"status": "ok", "providerCount": len(providers), "activeProviderIndex": active_idx}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ==================== 应用级配置 ====================
# The model root is the one setting a packaged build cannot guess: weights are
# never bundled, so the user points the app at them once and we persist it.

class AppConfigPayload(BaseModel):
    modelsRoot: Optional[str] = None
    llamaRoot: Optional[str] = None
    inochiPythonPath: Optional[str] = None
    ffmpegPath: Optional[str] = None
    videoAutoStt: Optional[bool] = None


@app.get("/app/config")
async def get_app_config():
    from .Inochi2D.service import _find_python
    config = load_app_config()
    root = models_root()
    llama = llama_dir()
    audio_dir = audio_cpp_dir()
    inochi_py = _find_python()
    ff = ffmpeg_path()
    return {
        "modelsRoot": config.get("modelsRoot") or "",
        "resolvedModelsRoot": str(root),
        "modelsRootExists": root.exists(),
        "configuredLlamaRoot": config.get("llamaRoot") or "",
        "llamaRoot": str(llama),
        "llamaFound": (llama / llama_server_exe_name()).exists(),
        "configuredInochiPython": config.get("inochiPythonPath") or "",
        "inochiPython": str(inochi_py) if inochi_py else "",
        "configuredFfmpegPath": config.get("ffmpegPath") or "",
        "ffmpegPath": str(ff) if ff else "",
        "ffmpegFound": is_ffmpeg_usable(force_refresh=True),
        "videoAutoStt": bool(config.get("videoAutoStt", False)),
        "audioCppDir": str(audio_dir),
        "audioCppFound": (audio_dir / audio_cpp_exe_name()).exists(),
        "exeExt": ".exe" if os.name == "nt" else "",
        "dataRoot": str(DATA_ROOT),
    }


@app.post("/app/config")
async def set_app_config(payload: AppConfigPayload):
    """Persist model/runtime roots. Engines pick them up on next start/scan."""
    current = load_app_config()
    updated = dict(current)

    if payload.modelsRoot is not None:
        value = payload.modelsRoot.strip()
        if value:
            target = Path(value).expanduser()
            if not target.exists():
                raise HTTPException(status_code=400, detail=f"Path does not exist: {target}")
            if not target.is_dir():
                raise HTTPException(status_code=400, detail=f"Not a directory: {target}")
            updated["modelsRoot"] = str(target)
        else:
            updated["modelsRoot"] = ""

    if payload.llamaRoot is not None:
        lval = payload.llamaRoot.strip()
        if lval:
            llama_exe = llama_server_exe_name()
            target = Path(lval).expanduser()
            if not target.exists():
                raise HTTPException(status_code=400, detail=f"Runtime path does not exist: {target}")
            if target.is_file():
                if target.name.lower() != llama_exe.lower():
                    raise HTTPException(status_code=400, detail=f"Expected {llama_exe}, got: {target.name}")
                target = target.parent
            elif not (target / llama_exe).exists():
                # Allow parent dir containing llama/ or llama-* subdir
                from .paths import _find_llama_in
                hit = _find_llama_in(target)
                if hit is None:
                    raise HTTPException(status_code=400, detail=f"{llama_exe} not found in: {target}")
                target = hit
            updated["llamaRoot"] = str(target)
        else:
            updated["llamaRoot"] = ""

    if payload.inochiPythonPath is not None:
        pval = payload.inochiPythonPath.strip()
        if pval:
            target = Path(pval).expanduser()
            if not target.exists():
                raise HTTPException(status_code=400, detail=f"Python path does not exist: {target}")
            if target.is_dir():
                found = None
                for cand in (
                    target / "Scripts" / "python.exe",
                    target / ".venv" / "Scripts" / "python.exe",
                    target / "bin" / "python",
                    target / ".venv" / "bin" / "python",
                    target / "python.exe",
                    target / "python",
                ):
                    if cand.is_file():
                        found = cand
                        break
                if found is None:
                    raise HTTPException(status_code=400, detail=f"Python executable not found in: {target}")
                target = found
            elif not target.is_file():
                raise HTTPException(status_code=400, detail=f"Invalid Python executable: {target}")
            updated["inochiPythonPath"] = str(target)
        else:
            updated["inochiPythonPath"] = ""

    if payload.ffmpegPath is not None:
        fval = payload.ffmpegPath.strip()
        if fval:
            from .paths import _resolve_ffmpeg_candidate, ffmpeg_exe_name
            target = Path(fval).expanduser()
            if not target.exists():
                raise HTTPException(status_code=400, detail=f"FFmpeg path does not exist: {target}")
            resolved_ff = _resolve_ffmpeg_candidate(target)
            if resolved_ff is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{ffmpeg_exe_name()} not found in: {target}",
                )
            if "ffmpeg" not in resolved_ff.stem.lower():
                raise HTTPException(
                    status_code=400,
                    detail=f"Expected {ffmpeg_exe_name()}, got: {resolved_ff.name}",
                )
            updated["ffmpegPath"] = str(resolved_ff)
        else:
            updated["ffmpegPath"] = ""

    if payload.videoAutoStt is not None:
        updated["videoAutoStt"] = bool(payload.videoAutoStt)

    save_app_config(updated)
    invalidate_ffmpeg_cache()
    from .Inochi2D.service import invalidate_env_cache
    invalidate_env_cache()
    return {
        "status": "ok",
        "modelsRoot": updated.get("modelsRoot", ""),
        "llamaRoot": updated.get("llamaRoot", ""),
        "inochiPythonPath": updated.get("inochiPythonPath", ""),
        "ffmpegPath": updated.get("ffmpegPath", ""),
        "videoAutoStt": bool(updated.get("videoAutoStt", False)),
    }


@app.get("/app/browse")
async def browse_filesystem(path: str = "", ext: str = "", dirs_only: bool = False):
    """List local directories (and optional filtered files) for the UI path picker."""
    raw = (path or "").strip()
    if not raw:
        if os.name == "nt":
            import string
            drives = []
            for letter in string.ascii_uppercase:
                root = Path(f"{letter}:\\")
                if root.exists():
                    drives.append({"name": f"{letter}:\\", "path": str(root), "isDir": True})
            return {"path": "", "parent": "", "entries": drives}
        raw = str(Path.home())

    target = Path(raw).expanduser()
    if target.is_file():
        target = target.parent
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {target}")

    allowed_exts = {
        e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
        for e in (ext or "").split(",") if e.strip()
    }
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"Cannot read directory: {exc}") from exc

    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if is_dir:
            entries.append({"name": child.name, "path": str(child), "isDir": True})
        elif not dirs_only:
            if not allowed_exts or child.suffix.lower() in allowed_exts:
                size_mb = 0.0
                try:
                    size_mb = round(child.stat().st_size / (1024 * 1024), 1)
                except OSError:
                    pass
                entries.append({"name": child.name, "path": str(child), "isDir": False, "sizeMb": size_mb})

    parent = str(target.parent) if target.parent != target else ""
    return {"path": str(target), "parent": parent, "entries": entries}


class ProbeModelsPayload(BaseModel):
    baseUrl: str
    apiKey: str = ""


@app.post("/llm/probe-models")
async def probe_llm_models(payload: ProbeModelsPayload):
    """Query GET <baseUrl>/models on an OpenAI-compatible / Ollama / LM Studio endpoint."""
    import httpx
    base = payload.baseUrl.strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="baseUrl is required")
    url = base if base.endswith("/models") else f"{base}/models"
    local_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")
    trust_env = not any(h in url.lower() for h in local_hosts)
    headers = {}
    if payload.apiKey.strip():
        headers["Authorization"] = f"Bearer {payload.apiKey.strip()}"
    try:
        async with httpx.AsyncClient(timeout=8.0, trust_env=trust_env) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:240])
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        models = []
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict):
                    mid = str(m.get("id") or m.get("name") or "").strip()
                    if mid and mid not in models:
                        models.append(mid)
                elif isinstance(m, str) and m.strip() and m.strip() not in models:
                    models.append(m.strip())
        return {"models": models}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to probe models: {exc}") from exc


# ==================== 本地 llama.cpp 服务 ====================
# llama-server 以子进程常驻（Vulkan GPU 推理），由本后端起停，不依赖外部进程。
# 模型与运行时位于 E:\dearpebble 下，见 llama_server_engine.SHARED_LLM_ROOT。

@app.get("/local-llm/status")
def local_llm_status():
    """本地模型服务状态（含设备、KV 类型、并行槽等运行参数）。"""
    return llama_server.status()


@app.get("/local-llm/devices")
def local_llm_devices():
    """枚举 Vulkan 设备，用于确认模型落在独显而非核显。"""
    status = llama_server.status()
    return {
        "devices": list_devices(),
        "resolved": resolve_device(),
        "server_exe": str(_server_exe()),
        "model": status.get("model", ""),
        "model_path": status.get("model_path", str(MODEL_PATH)),
        "models": status.get("models", describe_models()),
    }


class LocalLlmStartRequest(BaseModel):
    """本地模型启动参数。字段留空表示沿用后端当前选择。"""
    model_key: str = Field(default="", description="模型注册表 key，空串沿用当前选择")
    enable_mtp: Optional[bool] = Field(default=None, description="是否挂载 MTP 投机解码头")


@app.get("/local-llm/models")
def local_llm_models():
    """可选模型清单（含权重文件是否就绪），供前端切换器渲染。"""
    status = llama_server.status()
    return {
        "models": describe_models(),
        "default": resolve_default_model_key(),
        "current": status.get("model", ""),
        "mtp": status.get("mtp", False),
    }


@app.post("/local-llm/start")
async def local_llm_start(request: Optional[LocalLlmStartRequest] = None):
    """启动 llama-server（冷加载 6.5-8.5GB 权重，约 10-20 秒）。目标模型与当前不同时会先停旧进程。"""
    payload = request or LocalLlmStartRequest()
    model_key = payload.model_key.strip() or None
    try:
        return await asyncio.to_thread(llama_server.start, model_key, payload.enable_mtp)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/local-llm/stop")
async def local_llm_stop():
    """停止 llama-server，释放全部显存。"""
    try:
        return await asyncio.to_thread(llama_server.stop)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/local-llm/restart")
async def local_llm_restart():
    """重启 llama-server（改运行参数后生效）。"""
    try:
        return await asyncio.to_thread(llama_server.restart)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/local-llm/release")
async def local_llm_release():
    """释放显存。单模型模式下 llama-server 没有热卸载接口，等价于停止进程。"""
    try:
        return await asyncio.to_thread(llama_server.release)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class LocalModelSaveRequest(BaseModel):
    key: str
    old_key: str = ""
    label: str = ""
    detail: str = ""
    model: str
    mmproj: str = ""
    mtp: str = ""
    ctx: int = Field(default=32768, ge=512, le=2097152)
    ngl: Optional[int] = Field(default=None, ge=0, le=999)
    device: str = "auto"
    kv_type: str = "q8_0"
    flash_attn: bool = True
    jinja: bool = True


@app.post("/local-llm/models")
async def save_local_llm_model(payload: LocalModelSaveRequest):
    """Save or update a custom local GGUF LLM definition."""
    try:
        entry = upsert_custom_model(payload.model_dump())
        return {"status": "ok", "entry": entry, "models": describe_models()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/local-llm/models/{model_key}")
async def delete_local_llm_model(model_key: str):
    """Delete a custom local GGUF LLM definition."""
    removed = delete_custom_model(model_key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Custom model not found: {model_key}")
    return {"status": "ok", "models": describe_models()}


class ScanModelsPayload(BaseModel):
    root: str = ""


@app.post("/local-llm/scan")
async def scan_local_gguf_models(payload: Optional[ScanModelsPayload] = None):
    """Scan modelsRoot (or a given directory) for GGUF files."""
    raw = (payload.root if payload else "").strip()
    target = Path(raw).expanduser() if raw else None
    if target is not None and not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {target}")
    found = await asyncio.to_thread(scan_gguf_models, target)
    return {"models": found, "root": str(target or models_root())}

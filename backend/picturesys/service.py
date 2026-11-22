"""
Picture Studio orchestrator.

Owns the job lifecycle: reserve a row, ask the director model for a prompt, run
the selected provider, write the file, publish the result. Chat calls into this
module with a one-line fire-and-forget hook; nothing here writes back into the
conversation.

Failure policy:
- There is no fallback chain. When the selected provider fails, the job is
  marked failed with that provider's own error and the user decides what to do.
- A director error fails the job immediately: without a prompt there is nothing
  to draw, and retrying against the same model usually repeats the failure.
- Any pending row left by a previous process is closed at startup.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from .. import companion_db, script_db
from ..paths import UPLOAD_ROOT
from . import comfyui as picture_comfyui
from . import config as picture_config
from . import prompt as picture_prompt
from . import store
from .providers import (
    ImageProviderError,
    ImageRequest,
    ImageResult,
    build_provider,
    parse_size,
    random_seed,
)

logger = logging.getLogger(__name__)

GENERATED_DIR = UPLOAD_ROOT / "generated"
URL_PREFIX = "/uploads/generated/"

# One job at a time. Image providers have low free-tier concurrency, and a queue
# of two clicks arriving at once would burn the daily allowance on duplicates.
_SEM = asyncio.Semaphore(1)
_TASKS: Set[asyncio.Task] = set()

_ALLOWED_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


class PictureError(Exception):
    """A request-level failure the UI should show verbatim."""


def _spawn(coro) -> Optional[asyncio.Task]:
    """Run a job on the current loop, or drop it cleanly.

    Every caller today is inside an async request, so a loop always exists. The
    guard is here because the alternative is a RuntimeError escaping into a
    chat stream and an un-awaited coroutine warning: if a future caller is
    synchronous, the job should simply not start, and startup cleanup will mark
    the row failed instead of leaving it pending forever.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning("picture studio: no event loop, job dropped")
        return None
    task = loop.create_task(coro)
    # Keep a reference: an unreferenced task can be collected mid-flight.
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def init() -> int:
    """Prepare the directory and close rows orphaned by the last process."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    closed = store.fail_stale_jobs()
    if closed:
        logger.info("picture studio: closed %d stale job(s)", closed)
    return closed


# --- files ----------------------------------------------------------------


def _safe_segment(value: str) -> str:
    """A directory name that cannot escape the generated folder."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    return cleaned[:48] or "unknown"


def _image_size(data: bytes) -> Tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 - a missing size is not worth failing a picture
        return 0, 0


def _save_bytes(companion_id: str, data: bytes, mime: str) -> Tuple[str, int, int, int]:
    """Write atomically and return (url, size, width, height)."""
    ext = _ALLOWED_MIME.get(mime or "", "png")
    folder = GENERATED_DIR / _safe_segment(companion_id)
    folder.mkdir(parents=True, exist_ok=True)

    name = f"{uuid.uuid4().hex}.{ext}"
    target = folder / name
    tmp = target.with_name(name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)

    width, height = _image_size(data)
    url = f"{URL_PREFIX}{folder.name}/{name}"
    return url, len(data), width, height


def resolve_url_path(url: str) -> Optional[str]:
    """Map a stored URL back to a relative uploads path, or None.

    Only paths under `uploads/generated/` are accepted, so a crafted URL cannot
    delete an attachment or anything else in the data root.
    """
    if not url or not url.startswith(URL_PREFIX):
        return None
    rest = url[len(URL_PREFIX):]
    if not rest or ".." in rest or "\\" in rest or rest.startswith("/"):
        return None
    parts = [p for p in rest.split("/") if p]
    if len(parts) != 2:
        return None
    return "uploads/generated/" + "/".join(parts)


def _delete_file(url: str) -> bool:
    relative = resolve_url_path(url)
    if not relative:
        return False
    path = (UPLOAD_ROOT.parent / relative).resolve()
    if not path.is_relative_to(GENERATED_DIR.resolve()):
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("picture delete failed for %s: %s", path, exc)
        return False


# --- inputs ---------------------------------------------------------------


def _transcript(companion_id: str, branch_id: str, turns: int) -> List[Dict[str, str]]:
    """The last few turns, newest text only.

    Swipes are ignored: the visible line is the one on screen, and the director
    should see the scene the user is actually reading.
    """
    try:
        messages = companion_db.get_messages(companion_id, branch_id)
    except Exception as exc:  # noqa: BLE001 - a missing transcript must not abort
        logger.warning("picture studio: transcript read failed: %s", exc)
        return []

    usable = [
        m
        for m in messages
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and str(m.get("content") or "").strip()
        and m.get("message_type") not in ("compaction_summary",)
    ]
    tail = usable[-turns:]
    return [
        {
            "role": "Assistant" if m.get("role") == "assistant" else "User",
            "text": str(m.get("content") or ""),
        }
        for m in tail
    ]


def _require_enough_transcript(companion_id: str, branch_id: str) -> List[Dict[str, str]]:
    """Precondition: at least `TRANSCRIPT_TURNS` usable messages must exist."""
    transcript = _transcript(companion_id, branch_id, picture_prompt.TRANSCRIPT_TURNS)
    if len(transcript) < picture_prompt.TRANSCRIPT_TURNS:
        raise PictureError(
            f"当前有效对话消息不足 {picture_prompt.TRANSCRIPT_TURNS} 条"
            f"（当前 {len(transcript)} 条），请先进行更多对话后再生成图片"
        )
    return transcript


def _hud(companion_id: str, branch_id: str) -> Optional[Dict[str, Any]]:
    try:
        session = script_db.get_session_by_branch(companion_id, branch_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("picture studio: no script session (%s)", exc)
        return None
    if not session:
        return None
    hud = session.get("hud")
    if isinstance(hud, str):
        try:
            hud = json.loads(hud)
        except (TypeError, ValueError):
            return None
    return hud if isinstance(hud, dict) and hud else None


def _active_provider(cfg: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The single provider the user picked, or None when it cannot run.

    There is deliberately no fallback chain. A failed picture is reported to the
    user, who decides whether to switch providers. Retrying a different vendor
    automatically would spend someone else's quota and hide which one broke.
    """
    pid = str(cfg.get("activeProvider") or "").strip()
    if not pid:
        return None
    entry = (cfg.get("providers") or {}).get(pid)
    if not isinstance(entry, dict) or not entry.get("enabled"):
        return None
    if pid == "aihorde":
        return (pid, entry)
    if pid == "cloudflare":
        if str(entry.get("accountId") or "").strip() and str(entry.get("apiKey") or "").strip():
            return (pid, entry)
        return None
    meta = picture_config.PROVIDER_CATALOGUE.get(pid) or {}
    # Local runners need no key at all, so they are ready once enabled.
    if not bool(meta.get("requiresKey", True)):
        return (pid, entry)
    if str(entry.get("apiKey") or "").strip():
        return (pid, entry)
    return None


# --- job runner -----------------------------------------------------------


def _progress_writer(picture_id: int):
    """Build a throttled progress sink for one job.

    ComfyUI emits one message per sampling step, which is 20 to 40 writes per
    image. SQLite would take them happily, and the panel refreshes every three
    seconds anyway, so only changes at least a second apart are persisted.
    """
    state = {"at": 0.0, "value": -1}

    def write(value: int, total: int) -> None:
        if total <= 0:
            return
        percent = max(0, min(99, int(round(value * 100 / total))))
        now = time.monotonic()
        if percent == state["value"] and now - state["at"] < 1.0:
            return
        state["value"] = percent
        state["at"] = now
        try:
            store.update_progress(picture_id, percent)
        except Exception:  # noqa: BLE001 - progress must never fail the job
            logger.debug("progress write failed for job %s", picture_id, exc_info=True)

    return write


def _cancelled(picture_id: int) -> bool:
    """Did the user cancel this job while it was running?

    An interrupted prompt surfaces as an ordinary provider error. Without this
    check the job would overwrite the "cancelled" row with that error text, and
    the user would see a failure they did not cause.
    """
    row = store.get_job(picture_id)
    return bool(row) and row.get("status") == "failed" and str(row.get("error") or "") == "已取消"


async def _resolve_job_prompt_style(cfg: Dict[str, Any]) -> str:
    configured = str(cfg.get("promptStyle") or "auto").strip()
    if configured in picture_prompt.PROMPT_STYLES and configured != "auto":
        return configured

    pid = str(cfg.get("activeProvider") or "").strip()
    wf_name = ""
    wf_data: Optional[Dict[str, Any]] = None
    if pid == "comfyui":
        entry = (cfg.get("providers") or {}).get("comfyui") or {}
        wf_name = str(((entry.get("params") or {}).get("workflow")) or "builtin:sd").strip()
        if wf_name.startswith(picture_comfyui.COMFYUI_WORKFLOW_PREFIX):
            base_url = str(entry.get("baseUrl") or "http://127.0.0.1:8188")
            key = str(entry.get("apiKey") or "")
            try:
                async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
                    wf_data = await picture_comfyui.load_user_workflow(
                        base_url, key, wf_name, client
                    )
            except Exception:  # noqa: BLE001 - fall back to workflow name heuristics
                wf_data = None
        else:
            try:
                wf_data = picture_comfyui.load_workflow(wf_name)
            except picture_comfyui.ComfyUIError:
                wf_data = None

    return picture_prompt.resolve_prompt_style(
        configured_style=configured,
        provider_id=pid,
        workflow_id=wf_name,
        workflow_data=wf_data,
    )


async def _run_job(
    picture_id: int,
    chat_provider_id: str = "",
    chat_model: str = "",
) -> None:
    job = store.get_job(picture_id)
    if not job:
        return

    companion_id = str(job.get("companion_id") or "")
    branch_id = str(job.get("branch_id") or "main")
    source = str(job.get("source") or "manual")
    instruction = str(job.get("instruction") or "")
    manual = source == "manual"

    store.update_job(picture_id, status="running")
    cfg = picture_config.load_config()

    companion = companion_db.get_companion(companion_id)
    if not companion:
        store.update_job(picture_id, status="failed", error="角色不存在")
        return

    width, height = parse_size(str(cfg.get("size") or "1024x1024"))
    style = str(cfg.get("stylePrompt") or "")
    negative_default = str(cfg.get("negativePrompt") or "")

    async with _SEM:
        try:
            transcript = _require_enough_transcript(companion_id, branch_id)
            prompt_style = await _resolve_job_prompt_style(cfg)
            plan = await picture_prompt.draft_prompt(
                companion=companion,
                transcript=transcript,
                hud=_hud(companion_id, branch_id),
                instruction=instruction,
                style=style,
                source=source,
                preferred_provider_id=str(cfg.get("promptProviderId") or ""),
                force=manual,
                prompt_style=prompt_style,
                chat_provider_id=chat_provider_id,
                chat_model=chat_model,
            )
        except PictureError as exc:
            store.update_job(picture_id, status="failed", error=str(exc))
            return
        except picture_prompt.DirectorError as exc:
            store.update_job(picture_id, status="failed", error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never leave a row running
            logger.exception("picture studio: director crashed")
            store.update_job(picture_id, status="failed", error=f"提示词生成失败: {exc}")
            return

        if not plan.generate:
            store.update_job(
                picture_id,
                status="skipped",
                prompt=plan.prompt,
                prompt_raw=plan.raw,
                error=plan.reason or "模型判断当前场景无需生图",
            )
            return

        style_negative = picture_prompt.default_negative_for(prompt_style)
        fallback_negative = (
            style_negative
            if (
                style_negative
                and negative_default == picture_config.DEFAULT_CONFIG["negativePrompt"]
            )
            else negative_default
        )
        effective_negative = plan.negative or fallback_negative

        store.update_job(
            picture_id,
            prompt=plan.prompt,
            prompt_raw=plan.raw,
            negative_prompt=effective_negative,
        )

        picked = _active_provider(cfg)
        if picked is None:
            store.update_job(
                picture_id,
                status="failed",
                error=(
                    "当前选择的生图提供商还不能用：请到「后台 → 生图」启用它并填好必需的字段"
                ),
            )
            return
        # A one-item list, not a chain: it keeps the body with a single exit path
        # (`continue` after a failure) instead of duplicating the try/except
        # stack. There is no second candidate to fall back to by design.
        candidates = [picked]

        seed = random_seed()
        errors: List[str] = []

        async with httpx.AsyncClient(follow_redirects=True) as client:
            for pid, entry in candidates:
                # A cleared URL or model field falls back to the shipped default.
                # An empty endpoint would otherwise fail with an opaque httpx
                # error that says nothing about which field the user emptied.
                merged = dict(entry)
                defaults = picture_config.PROVIDER_CATALOGUE.get(pid, {}).get("config") or {}
                if not str(merged.get("baseUrl") or "").strip():
                    merged["baseUrl"] = defaults.get("baseUrl", "")
                if not str(merged.get("model") or "").strip():
                    merged["model"] = defaults.get("model", "")
                # Cancelled before anything was sent: nothing to interrupt.
                if _cancelled(picture_id):
                    logger.info("picture job %s cancelled before provider %s", picture_id, pid)
                    return
                provider = build_provider(pid, merged)
                if provider is None:
                    continue
                # Both hooks are best effort. A cloud provider never calls them,
                # so an entry that does not report progress simply keeps 0 and
                # shows a spinner instead of a bar.
                provider.on_job_id = lambda jid: store.update_job(
                    picture_id, provider_job_id=jid
                )
                provider.on_progress = _progress_writer(picture_id)
                provider.should_stop = lambda: _cancelled(picture_id)
                # Written before the call so the panel knows which provider is
                # being tried while it runs, not only after it succeeds.
                store.update_job(picture_id, provider=pid)
                request = ImageRequest(
                    prompt=plan.prompt,
                    negative_prompt=effective_negative,
                    width=width,
                    height=height,
                    seed=seed,
                    model=str(merged.get("model") or ""),
                )
                try:
                    result: ImageResult = await provider.generate(request, client)
                except ImageProviderError as exc:
                    # A cancel is not a provider failure: keep the "cancelled"
                    # row instead of replacing it with the upstream error text.
                    if _cancelled(picture_id):
                        logger.info("picture job %s cancelled during %s", picture_id, pid)
                        return
                    errors.append(f"{pid}: {exc}")
                    logger.warning("picture provider %s failed: %s", pid, exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - report it, never crash the runner
                    if _cancelled(picture_id):
                        logger.info("picture job %s cancelled during %s", picture_id, pid)
                        return
                    errors.append(f"{pid}: {exc}")
                    logger.exception("picture provider %s crashed", pid)
                    continue

                url, size, real_w, real_h = _save_bytes(companion_id, result.data, result.mime)
                store.update_job(
                    picture_id,
                    status="done",
                    url=url,
                    size=size,
                    width=real_w or width,
                    height=real_h or height,
                    provider=pid,
                    model=result.model or request.model,
                    seed=result.seed if result.seed is not None else seed,
                    progress=100,
                    error="",
                )
                logger.info(
                    "picture ready id=%s provider=%s size=%dx%d bytes=%d",
                    picture_id,
                    pid,
                    real_w,
                    real_h,
                    size,
                )
                return

        store.update_job(
            picture_id,
            status="failed",
            error="；".join(errors) or "生图失败",
        )


# --- public entry points --------------------------------------------------


def start_manual(
    companion_id: str,
    branch_id: str,
    instruction: str = "",
    message_id: Optional[int] = None,
    session_id: Optional[int] = None,
    chat_provider_id: str = "",
    chat_model: str = "",
) -> int:
    """Queue a picture because the user asked for one.

    Manual runs ignore the cooldown and the automatic flags: the user already
    made the decision. The daily limit still applies, because it protects a
    shared free allowance rather than the model's judgement.
    """
    cfg = picture_config.load_config()
    if not cfg.get("enabled"):
        raise PictureError("生图功能已关闭，请先在「后台 → 生图」里启用")
    if not companion_id or not branch_id:
        raise PictureError("缺少角色或分支")

    limit = int(cfg.get("dailyLimit") or 0)
    if limit > 0 and store.count_today() >= limit:
        raise PictureError(f"已达到今日生图上限（{limit} 张），可在设置里调整")

    if _active_provider(cfg) is None:
        raise PictureError(
            "当前选择的生图提供商还不能用：请到「后台 → 生图」启用它并填好必需的字段"
        )

    _require_enough_transcript(companion_id, branch_id)

    picture_id = store.create_job(
        companion_id=companion_id,
        branch_id=branch_id,
        source="manual",
        message_id=message_id,
        session_id=session_id,
    )
    if instruction:
        store.update_job(picture_id, instruction=instruction)
    _spawn(
        _run_job(
            picture_id,
            chat_provider_id=chat_provider_id,
            chat_model=chat_model,
        )
    )
    return picture_id


def maybe_auto(
    companion_id: str,
    branch_id: str,
    source: str,
    message_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> Optional[int]:
    """Queue a picture on its own, or explain to the log why it did not.

    Every gate is silent by design: an automatic trigger that cannot run is not
    an error the user needs to see on every message.
    """
    cfg = picture_config.load_config()
    if not cfg.get("enabled"):
        return None
    if source == "chat" and not cfg.get("autoChat"):
        return None
    if source == "script" and not cfg.get("autoScript"):
        return None
    if not companion_id or not branch_id:
        return None
    if store.has_running(companion_id, branch_id):
        return None

    limit = int(cfg.get("dailyLimit") or 0)
    if limit > 0 and store.count_today() >= limit:
        return None

    cooldown = int(cfg.get("cooldownSeconds") or 0)
    age = store.seconds_since_last(companion_id, branch_id)
    if cooldown > 0 and age is not None and age < cooldown:
        return None

    if _active_provider(cfg) is None:
        return None
    if len(_transcript(companion_id, branch_id, picture_prompt.TRANSCRIPT_TURNS)) < picture_prompt.TRANSCRIPT_TURNS:
        return None

    picture_id = store.create_job(
        companion_id=companion_id,
        branch_id=branch_id,
        source=source,
        message_id=message_id,
        session_id=session_id,
    )
    _spawn(_run_job(picture_id))
    return picture_id


def retry(picture_id: int) -> int:
    """Run a failed job again as a fresh row, keeping the old record."""
    job = store.get_job(picture_id)
    if not job:
        raise PictureError("记录不存在")
    if job.get("status") in ("pending", "running"):
        raise PictureError("这张图还在生成中")

    companion_id = str(job.get("companion_id") or "")
    branch_id = str(job.get("branch_id") or "main")
    _require_enough_transcript(companion_id, branch_id)

    new_id = store.create_job(
        companion_id=companion_id,
        branch_id=branch_id,
        source=str(job.get("source") or "manual"),
        message_id=job.get("message_id"),
        session_id=job.get("session_id"),
    )
    if job.get("instruction"):
        store.update_job(new_id, instruction=str(job["instruction"]))
    _spawn(_run_job(new_id))
    return new_id


async def cancel(picture_id: int) -> Dict[str, Any]:
    """Cancel a queued or running job.

    The row is flipped first so the UI reacts at once, then the upstream prompt
    is interrupted. A job that finished in between keeps its picture: the
    guarded UPDATE returns no row and the caller is told so.
    """
    row = store.claim_cancel(picture_id)
    if row is None:
        raise PictureError("这个任务已经结束，无法取消")

    if str(row.get("provider") or "") == "comfyui":
        job_id = str(row.get("provider_job_id") or "")
        if job_id:
            cfg = picture_config.load_config()
            entry = (cfg.get("providers") or {}).get("comfyui") or {}
            base_url = str(entry.get("baseUrl") or "")
            key = str(entry.get("apiKey") or "")
            try:
                # trust_env=False: same reason as everywhere else here, a local
                # instance must not be routed through the system proxy.
                async with httpx.AsyncClient(trust_env=False, follow_redirects=True) as client:
                    await picture_comfyui.cancel(base_url, key, job_id, client)
            except Exception:  # noqa: BLE001 - the local row is already cancelled
                logger.warning("comfyui cancel failed for %s", job_id, exc_info=True)
    return {"status": "cancelled", "id": picture_id}


def delete(picture_id: int) -> Dict[str, Any]:
    job = store.delete_job(picture_id)
    if not job:
        raise PictureError("记录不存在")
    removed = _delete_file(str(job.get("url") or ""))
    return {"status": "success", "id": picture_id, "file_deleted": removed}


def list_for_branch(companion_id: str, branch_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    return store.list_jobs(companion_id, branch_id, limit)


def usage(companion_id: str = "", branch_id: str = "") -> Dict[str, Any]:
    cfg = picture_config.load_config()
    data: Dict[str, Any] = {
        "today": store.count_today(),
        "dailyLimit": int(cfg.get("dailyLimit") or 0),
        "cooldownSeconds": int(cfg.get("cooldownSeconds") or 0),
    }
    if companion_id and branch_id:
        data["branchTotal"] = store.count_done(companion_id, branch_id)
        data["branchLastAgeSeconds"] = store.seconds_since_last(companion_id, branch_id)
    return data

"""GitHub Releases self-update check, download, and Windows installer launch."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("updater")

DEFAULT_REPO = "5235aa-web/Pebble_companionchat"
FALLBACK_VERSION = "0.1.0"
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

_SEMVER_RE = re.compile(r"^\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def update_repo() -> str:
    """Return owner/repo used for GitHub Releases lookups."""
    env = os.getenv("PEBBLE_UPDATE_REPO", "").strip()
    return env or DEFAULT_REPO


def get_current_version() -> str:
    """Read the packaged or workspace VERSION file, falling back to a default."""
    candidates: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "VERSION")
    candidates.append(Path(__file__).resolve().parent.parent / "VERSION")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.lstrip("vV")
        except OSError:
            continue
    return FALLBACK_VERSION


def parse_version(raw: str) -> Tuple[int, int, int, int, str]:
    """Parse a semver-like tag into a comparable tuple.

    Stable releases sort above prereleases with the same numeric triple
    (fourth element 1 vs 0).
    """
    match = _SEMVER_RE.match(raw or "")
    if not match:
        return (0, 0, 0, 0, "")
    major = int(match.group(1) or 0)
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    pre = match.group(4) or ""
    stability = 0 if pre else 1
    return (major, minor, patch, stability, pre)


def is_newer_version(candidate: str, current: str) -> bool:
    """Return True if *candidate* is strictly newer than *current*."""
    c_ver = parse_version(candidate)
    cur_ver = parse_version(current)
    if c_ver[:4] != cur_ver[:4]:
        return c_ver[:4] > cur_ver[:4]
    if c_ver[3] == 0 and cur_ver[3] == 0:
        return c_ver[4] > cur_ver[4]
    return False


def select_windows_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the preferred Windows installer asset from a GitHub Release."""
    if not assets:
        return None
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not name or not url:
            continue
        lower = name.lower()
        if lower.endswith("-setup.exe") or lower.endswith("_setup.exe") or (
            lower.endswith(".exe") and "setup" in lower
        ):
            scored.append((0, asset))
        elif lower.endswith(".exe"):
            scored.append((1, asset))
        elif lower.endswith(".msi"):
            scored.append((2, asset))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def is_allowed_download_url(url: str) -> bool:
    """Ensure installer downloads only originate from trusted GitHub HTTPS hosts."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.hostname.lower() in ALLOWED_DOWNLOAD_HOSTS
    )


def _safe_asset_filename(name: str) -> str:
    leaf = Path(name or "Pebble-Setup.exe").name
    cleaned = _SAFE_NAME_RE.sub("_", leaf).strip("._")
    if not cleaned:
        cleaned = "Pebble-Setup.exe"
    if not cleaned.lower().endswith((".exe", ".msi")):
        cleaned += ".exe"
    return cleaned


def _build_check_payload(
    *,
    current: str,
    tag: str,
    release_name: str,
    notes: str,
    published_at: str,
    release_url: str,
    asset: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    latest = (tag or "").lstrip("vV") or current
    return {
        "ok": True,
        "hasRelease": True,
        "currentVersion": current,
        "latestVersion": latest,
        "tagName": tag or f"v{latest}",
        "hasUpdate": is_newer_version(latest, current),
        "releaseName": release_name or tag or f"v{latest}",
        "notes": notes or "",
        "publishedAt": published_at or "",
        "releaseUrl": release_url or f"https://github.com/{update_repo()}/releases",
        "downloadUrl": str(asset.get("browser_download_url") or "") if asset else "",
        "assetName": str(asset.get("name") or "") if asset else "",
        "assetSize": int(asset.get("size") or 0) if asset else 0,
    }


async def _fetch_latest_json_fallback(
    client: httpx.AsyncClient, repo: str, current: str
) -> Optional[Dict[str, Any]]:
    """Fallback to CDN-hosted latest.json when GitHub REST API is rate-limited."""
    url = f"https://github.com/{repo}/releases/latest/download/latest.json"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    data = resp.json()
    version = str(data.get("version") or "").lstrip("vV")
    if not version:
        return None
    platforms = data.get("platforms") or {}
    win = platforms.get("windows-x86_64") or platforms.get("windows") or {}
    dl_url = str(win.get("url") or "")
    asset = (
        {
            "name": str(win.get("name") or Path(urlparse(dl_url).path).name or "setup.exe"),
            "browser_download_url": dl_url,
            "size": int(win.get("size") or 0),
        }
        if dl_url
        else None
    )
    return _build_check_payload(
        current=current,
        tag=f"v{version}",
        release_name=str(data.get("name") or f"v{version}"),
        notes=str(data.get("notes") or ""),
        published_at=str(data.get("pub_date") or ""),
        release_url=f"https://github.com/{repo}/releases/tag/v{version}",
        asset=asset,
    )


async def check_for_update() -> Dict[str, Any]:
    """Query GitHub Releases for the latest version and release notes."""
    repo = update_repo()
    current = get_current_version()
    releases_page = f"https://github.com/{repo}/releases"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Pebble-Companion-Chat/{current}",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            api_url = f"https://api.github.com/repos/{repo}/releases/latest"
            resp = await client.get(api_url)
            if resp.status_code == 404:
                return {
                    "ok": True,
                    "hasRelease": False,
                    "currentVersion": current,
                    "latestVersion": current,
                    "tagName": f"v{current}",
                    "hasUpdate": False,
                    "releaseName": "",
                    "notes": "",
                    "publishedAt": "",
                    "releaseUrl": releases_page,
                    "downloadUrl": "",
                    "assetName": "",
                    "assetSize": 0,
                }
            if resp.status_code in (403, 429):
                fallback = await _fetch_latest_json_fallback(client, repo, current)
                if fallback is not None:
                    return fallback
                return {
                    "ok": False,
                    "error": "rate_limited",
                    "detail": f"GitHub API returned {resp.status_code}",
                    "currentVersion": current,
                    "releaseUrl": releases_page,
                }
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": "github_error",
                    "detail": f"HTTP {resp.status_code}",
                    "currentVersion": current,
                    "releaseUrl": releases_page,
                }

            data = resp.json()
            asset = select_windows_asset(list(data.get("assets") or []))
            return _build_check_payload(
                current=current,
                tag=str(data.get("tag_name") or ""),
                release_name=str(data.get("name") or ""),
                notes=str(data.get("body") or ""),
                published_at=str(data.get("published_at") or ""),
                release_url=str(data.get("html_url") or releases_page),
                asset=asset,
            )
    except Exception as exc:  # noqa: BLE001 - network errors return structured status
        logger.warning("Update check failed: %s", exc)
        return {
            "ok": False,
            "error": "network_error",
            "detail": str(exc),
            "currentVersion": current,
            "releaseUrl": releases_page,
        }


_download_lock = asyncio.Lock()
_download_task: Optional[asyncio.Task] = None
_download_state: Dict[str, Any] = {
    "phase": "idle",
    "downloadedBytes": 0,
    "totalBytes": 0,
    "percent": 0,
    "installerPath": "",
    "assetName": "",
    "error": "",
}


def get_download_status() -> Dict[str, Any]:
    """Return a snapshot of the active or latest installer download."""
    return dict(_download_state)


async def _run_download(download_url: str, asset_name: str) -> None:
    safe_name = _safe_asset_filename(asset_name)
    target_dir = Path(tempfile.gettempdir()) / "pebble-updates"
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / safe_name
    part_path = target_dir / f"{safe_name}.part"

    _download_state.update({
        "phase": "downloading",
        "downloadedBytes": 0,
        "totalBytes": 0,
        "percent": 0,
        "installerPath": "",
        "assetName": safe_name,
        "error": "",
    })

    headers = {"User-Agent": f"Pebble-Companion-Chat/{get_current_version()}"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", download_url) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"Download failed with HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or 0)
                _download_state["totalBytes"] = total
                downloaded = 0
                with part_path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=131072):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        _download_state["downloadedBytes"] = downloaded
                        if total > 0:
                            _download_state["percent"] = min(
                                100, int((downloaded * 100) / total)
                            )

        if part_path.stat().st_size <= 0:
            raise RuntimeError("Downloaded installer is empty")
        if safe_name.lower().endswith(".exe"):
            with part_path.open("rb") as fh:
                if fh.read(2) != b"MZ":
                    raise RuntimeError("Downloaded file is not a valid Windows executable")

        part_path.replace(final_path)
        _download_state.update({
            "phase": "ready",
            "percent": 100,
            "installerPath": str(final_path),
            "error": "",
        })
    except Exception as exc:  # noqa: BLE001 - reported via status polling
        logger.warning("Installer download failed: %s", exc)
        try:
            if part_path.exists():
                part_path.unlink()
        except OSError:
            pass
        _download_state.update({
            "phase": "error",
            "error": str(exc),
        })


async def start_download(download_url: str, asset_name: str = "") -> Dict[str, Any]:
    """Start downloading the release installer in the background."""
    global _download_task
    if not is_allowed_download_url(download_url):
        raise ValueError("Untrusted download URL; only GitHub HTTPS URLs are allowed")

    async with _download_lock:
        if _download_task and not _download_task.done():
            return get_download_status()
        resolved_name = asset_name or Path(urlparse(download_url).path).name
        _download_state.update({
            "phase": "downloading",
            "downloadedBytes": 0,
            "totalBytes": 0,
            "percent": 0,
            "installerPath": "",
            "assetName": _safe_asset_filename(resolved_name),
            "error": "",
        })
        _download_task = asyncio.create_task(_run_download(download_url, resolved_name))
        return get_download_status()


def launch_installer() -> Dict[str, Any]:
    """Spawn the downloaded installer detached so the app can exit for upgrade."""
    raw_path = str(_download_state.get("installerPath") or "").strip()
    if _download_state.get("phase") != "ready" or not raw_path:
        raise RuntimeError("No downloaded installer is ready")
    installer = Path(raw_path)
    if not installer.is_file():
        raise FileNotFoundError(f"Installer not found: {installer}")

    kwargs: Dict[str, Any] = {"close_fds": True}
    if os.name == "nt":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = detached | new_group

    subprocess.Popen([str(installer)], **kwargs)
    _download_state["phase"] = "launching"
    return {"ok": True, "installerPath": str(installer)}

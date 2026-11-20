"""HTTP endpoints for checking, downloading, and launching app updates."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import updater

router = APIRouter(prefix="/app/update", tags=["update"])


class InstallRequest(BaseModel):
    """Payload describing the GitHub Release asset to download."""

    downloadUrl: str
    assetName: str = ""


@router.get("/check")
async def check_update_endpoint():
    """Check GitHub Releases for a newer version and return release notes."""
    return await updater.check_for_update()


@router.post("/install")
async def start_install_endpoint(payload: InstallRequest):
    """Begin downloading the Windows installer asset in the background."""
    try:
        return await updater.start_download(payload.downloadUrl, payload.assetName)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def install_status_endpoint():
    """Return current installer download progress and readiness."""
    return updater.get_download_status()


@router.post("/launch")
async def launch_update_endpoint():
    """Launch the downloaded installer detached so the desktop shell can exit."""
    try:
        return updater.launch_installer()
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

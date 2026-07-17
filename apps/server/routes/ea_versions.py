"""EA version, download, and audit endpoints."""

import asyncio
import base64
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .auth import _require_auth, _verify_admin_key, verify_signal_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ea-versions"])

# ---------------------------------------------------------------------------
# EA file paths
# ---------------------------------------------------------------------------

_EA_DIR = Path(__file__).resolve().parents[2] / "ea"
_EA_VERSION_FILE = _EA_DIR / "mt5" / "VERSION.json"
_DLL_DIR = _EA_DIR / "dll" / "bin"

# EA compiled file paths (served for auto-update)
_EA_EX5 = _EA_DIR / "mt5" / "PineTunnel_EA.ex5"
_EA_EX4 = _EA_DIR / "mt4" / "PineTunnel_EA_MT4.ex4"

# DLL compiled file paths (served for auto-update)
_DLL_MT5 = _DLL_DIR / "PTWebSocket.dll"
_DLL_MT4 = _DLL_DIR / "PTWebSocket32.dll"

# File cache for base64-encoded downloads (avoid re-reading on every request)
_ea_file_cache: dict[str, dict] = {}
_dll_file_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Version loading
# ---------------------------------------------------------------------------


def _load_ea_versions() -> dict[str, dict[str, str]]:
    """Load EA version info from VERSION.json (replaces old ea_release.json)."""
    defaults: dict[str, dict[str, str]] = {
        "mt5": {"version": "0.00", "date": ""},
        "mt4": {"version": "0.00", "date": ""},
    }
    try:
        if _EA_VERSION_FILE.exists():
            with _EA_VERSION_FILE.open("r") as f:
                data = json.load(f)
            defaults["mt5"] = {
                "version": data.get("version", "0.00"),
                "date": data.get("release_date", ""),
                "release_notes": "",
                "dll_version": data.get("dll_version", ""),
                "release_type": data.get("release_type", ""),
                "min_compatible_version": data.get("min_compatible_version", ""),
                "critical": data.get("critical", False),
            }
            # MT4 version is embedded in the same file or we derive it
            mt4 = data.get("mt4", {})
            defaults["mt4"] = {
                "version": mt4.get("version", data.get("version", "0.00")),
                "date": mt4.get("date", data.get("release_date", "")),
                "release_notes": "",
                "dll_version": mt4.get("dll_version", data.get("dll_version", "")),
                "release_type": data.get("release_type", ""),
                "min_compatible_version": data.get("min_compatible_version", ""),
                "critical": data.get("critical", False),
            }
            logger.info(
                "Loaded EA versions: MT5=%s, MT4=%s",
                defaults["mt5"]["version"],
                defaults["mt4"]["version"],
            )
    except Exception as e:
        logger.warning("Failed to load VERSION.json: %s", e)
    return defaults


_ea_versions = _load_ea_versions()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_ea_download(platform: str) -> dict:
    """Return base64-encoded EA file with metadata for auto-update.

    Caches the encoded file to avoid re-reading on every request.
    Cache is invalidated when the file modification time changes.
    """
    if platform not in ("mt5", "mt4"):
        raise ValueError(f"Invalid platform: {platform}")

    ea_path = _EA_EX5 if platform == "mt5" else _EA_EX4
    filename = ea_path.name
    platform_key = f"{platform}:{filename}"

    # Check cache validity
    if platform_key in _ea_file_cache:
        cached = _ea_file_cache[platform_key]
        if cached.get("mtime") == ea_path.stat().st_mtime:
            return cached

    if not ea_path.exists():
        raise FileNotFoundError(f"EA file not found: {ea_path}")

    file_data = ea_path.read_bytes()
    sha256 = hashlib.sha256(file_data).hexdigest()
    b64_data = base64.b64encode(file_data).decode("ascii")

    version_info = _ea_versions.get(platform, {})
    result = {
        "version": version_info.get("version", "0.00"),
        "filename": filename,
        "data": b64_data,
        "sha256": sha256,
        "size": len(file_data),
        "release_date": version_info.get("date", ""),
        "changelog": version_info.get("release_notes", ""),
        "mtime": ea_path.stat().st_mtime,
    }

    _ea_file_cache[platform_key] = result
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/admin/ea-version")
async def get_ea_versions(_username: str = Depends(_require_auth)):
    """Get current published EA versions for all platforms"""
    return {"status": "success", "versions": _ea_versions}


@router.get("/api/ea/dll/{platform}")
async def download_dll(
    platform: str,
    request: Request,
    _admin_key: str = Depends(_verify_admin_key),
):
    """Download the latest PTWebSocket DLL for the given platform.

    platform: 'mt5' (x64) or 'mt4' (x86)
    Requires X-Admin-Key header for authentication.
    """
    if platform not in ("mt5", "mt4"):
        raise HTTPException(
            status_code=400,
            detail="Invalid platform. Use 'mt5' or 'mt4'.",
        )

    # DLL filenames: x64 for MT5, x86 (Win32) for MT4
    filename = "PTWebSocket.dll" if platform == "mt5" else "PTWebSocket32.dll"
    dll_path = _DLL_DIR / filename

    if not dll_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"DLL not found: {filename}. Build the DLL first with the CI pipeline.",
        )

    return FileResponse(
        dll_path,
        media_type="application/octet-stream",
        filename=filename,
    )


@router.get("/api/ea/dll/download/{platform}")
async def download_dll_auto_update(
    platform: str,
    request: Request,
    _sig: None = Depends(verify_signal_request),
):
    """Download the latest DLL for auto-update (license-key authenticated).

    EAs call this to download DLL updates. Returns base64-encoded DLL
    with SHA-256 checksum, same format as EA download endpoint.
    """
    if platform not in ("mt5", "mt4"):
        raise HTTPException(
            status_code=400,
            detail="Invalid platform. Use 'mt5' or 'mt4'.",
        )

    dll_path = _DLL_MT5 if platform == "mt5" else _DLL_MT4
    filename = dll_path.name
    cache_key = f"dll:{platform}:{filename}"

    # Check cache
    if cache_key in _dll_file_cache:
        cached = _dll_file_cache[cache_key]
        if cached.get("mtime") == dll_path.stat().st_mtime:
            result = cached
        else:
            result = None
    else:
        result = None

    if result is None:
        if not dll_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"DLL file not found: {filename}. Build the DLL first.",
            )

        def _read_dll():
            file_data = dll_path.read_bytes()
            sha256 = hashlib.sha256(file_data).hexdigest()
            b64_data = base64.b64encode(file_data).decode("ascii")
            return file_data, sha256, b64_data

        file_data, sha256, b64_data = await asyncio.to_thread(_read_dll)

        version_info = _ea_versions.get(platform, {})
        result = {
            "version": version_info.get("dll_version", ""),
            "filename": filename,
            "data": b64_data,
            "sha256": sha256,
            "size": len(file_data),
            "mtime": dll_path.stat().st_mtime,
        }
        _dll_file_cache[cache_key] = result

    response_data = {
        "status": "success",
        "platform": platform,
        "version": result["version"],
        "filename": result["filename"],
        "data": result["data"],
        "sha256": result["sha256"],
        "size": result["size"],
    }
    return JSONResponse(content=response_data)


@router.get("/api/ea/download/{platform}")
async def download_ea(
    platform: str,
    request: Request,
    _sig: None = Depends(verify_signal_request),
):
    """Download the latest EA compiled file for auto-update.

    The EA calls this endpoint to check for and download updates.
    Requires a valid license key (same auth as signal polling).
    Returns base64-encoded EA file with SHA-256 checksum.
    """
    if platform not in ("mt5", "mt4"):
        raise HTTPException(
            status_code=400,
            detail="Invalid platform. Use 'mt5' or 'mt4'.",
        )

    try:
        result = await asyncio.to_thread(_get_ea_download, platform)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"EA file not available for {platform}.",
        )

    # Remove cache metadata before sending
    response_data = {
        "status": "success",
        "platform": platform,
        "version": result["version"],
        "filename": result["filename"],
        "data": result["data"],
        "sha256": result["sha256"],
        "size": result["size"],
        "release_date": result["release_date"],
    }
    return JSONResponse(content=response_data)


@router.get("/api/ea/check/{platform}")
async def check_ea_update(
    platform: str,
    request: Request,
    _sig: None = Depends(verify_signal_request),
):
    """Lightweight version check endpoint for EA auto-update.

    Returns version info without the actual file data.
    The EA can call this first, then download only if needed.
    """
    if platform not in ("mt5", "mt4"):
        raise HTTPException(
            status_code=400,
            detail="Invalid platform. Use 'mt5' or 'mt4'.",
        )

    version_info = _ea_versions.get(platform, {})
    ea_path = _EA_EX5 if platform == "mt5" else _EA_EX4
    dll_path = _DLL_MT5 if platform == "mt5" else _DLL_MT4

    response_data = {
        "status": "success",
        "platform": platform,
        "latest_version": version_info.get("version", "0.00"),
        "latest_dll_version": version_info.get("dll_version", ""),
        "release_date": version_info.get("date", ""),
        "release_notes": version_info.get("release_notes", ""),
        "release_type": version_info.get("release_type", ""),
        "min_compatible_version": version_info.get("min_compatible_version", ""),
        "critical": "true" if version_info.get("critical", False) else "false",
        "file_available": "true" if ea_path.exists() else "false",
        "dll_available": "true" if dll_path and dll_path.exists() else "false",
    }

    if ea_path.exists():
        ea_sha, ea_size = await asyncio.to_thread(
            lambda: (_sha256_file(ea_path), ea_path.stat().st_size)
        )
        response_data["file_sha256"] = ea_sha
        response_data["file_size"] = ea_size

    if dll_path and dll_path.exists():
        dll_sha, dll_size = await asyncio.to_thread(
            lambda: (_sha256_file(dll_path), dll_path.stat().st_size)
        )
        response_data["dll_sha256"] = dll_sha
        response_data["dll_size"] = dll_size

    return JSONResponse(content=response_data)


class EAAuditReport(BaseModel):
    """Audit/telemetry data from EA instances — validated schema."""

    ea_version: str = Field(default="unknown", max_length=50)
    dll_version: str = Field(default="unknown", max_length=50)
    platform: str = Field(default="unknown", max_length=10)
    account_number: int | str = Field(default=0)
    broker: str = Field(default="", max_length=100)
    is_vps: bool = False
    vps_provider: str = Field(default="", max_length=100)
    net_quality: str = Field(default="", max_length=50)
    ntp_drift_ms: int = 0
    uptime_sec: int = 0
    ws_status: str = Field(default="unknown", max_length=30)
    error_count: int = 0
    model_config = {"extra": "allow"}


@router.post("/api/ea/audit/{license_key}")
async def ea_audit(
    license_key: str,
    audit: EAAuditReport,
):
    """Receive audit/telemetry data from EA instances.

    EAs POST comprehensive account/system/VPS/network/NTP diagnostic info
    on startup and periodically. All data is persisted to the ea_audit table
    for monitoring and analytics.
    """
    from apps.server.state import client_manager, db_manager

    body = audit.dict()

    # Validate license key
    valid, msg = client_manager.validate_license(license_key)
    if not valid:
        raise HTTPException(status_code=403, detail=msg)

    # Persist audit data to database (background, non-blocking)
    if db_manager and hasattr(db_manager, "save_ea_audit"):
        try:
            await asyncio.to_thread(db_manager.save_ea_audit, license_key, body)
        except Exception as e:
            logger.warning("Failed to save EA audit for %s: %s", license_key[:8], e)

    # Extract key fields for structured logging
    ea_version = body.get("ea_version", "unknown")
    dll_version = body.get("dll_version", "unknown")
    platform = body.get("platform", "unknown")
    account_number = body.get("account_number", 0)
    broker = body.get("broker", "")
    is_vps = body.get("is_vps", False)
    vps_provider = body.get("vps_provider", "")
    net_quality = body.get("net_quality", "")
    ntp_drift = body.get("ntp_drift_ms", 0)
    uptime_sec = body.get("uptime_sec", 0)
    ws_status = body.get("ws_status", "unknown")
    error_count = body.get("error_count", 0)

    logger.info(
        "EA_AUDIT license=%s platform=%s ea=%s dll=%s acct=%s broker=%s "
        "vps=%s(%s) net=%s ntp_drift=%dms uptime=%ds ws=%s err=%d",
        license_key[:8] + "...",
        platform,
        ea_version,
        dll_version,
        account_number,
        broker[:20] if broker else "",
        "Y" if is_vps else "N",
        vps_provider[:10] if vps_provider else "",
        net_quality,
        ntp_drift,
        uptime_sec,
        ws_status,
        error_count,
    )

    # Build response — include any server-side directives
    response_data = {
        "status": "ok",
        "server_time": datetime.utcnow().isoformat() + "Z",
    }

    # If there's a newer version available, include update info
    version_key = platform if platform in ("mt5", "mt4") else "mt5"
    latest_version = _ea_versions.get(version_key, {}).get("version", "0.00")
    response_data["latest_version"] = latest_version
    response_data["update_available"] = (
        "true" if (latest_version != "0.00" and ea_version != latest_version) else "false"
    )

    # Check if DLL update is needed
    dll_version_info = _ea_versions.get(version_key, {})
    response_data["latest_dll_version"] = dll_version_info.get("dll_version", "3.0.0")

    return JSONResponse(content=response_data)

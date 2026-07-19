"""Auth dependencies and session/log routes."""

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request

from apps.server.config.logging_config import log_security_event
from apps.server.middleware.main import failed_attempt_tracker
from apps.server.utils.security import (
    get_trusted_client_ip,
    verify_hmac_signature,
    verify_secret_key,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Reusable auth dependencies (other routers import these)
# ---------------------------------------------------------------------------


def _is_localhost(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in ("127.0.0.1", "::1", "localhost"))


async def _require_auth(
    request: Request,
    session_token: str | None = Cookie(None),
) -> str:
    """Session-based authentication dependency.

    Delegates to the dependency created during startup (stored in state).
    Falls back to inline verification if state hasn't been initialized yet.
    Localhost requests bypass auth (matches dashboard endpoint behavior).
    """
    if _is_localhost(request):
        return "localhost"

    from apps.server.state import _require_auth_dependency

    # If the wired dependency is available, use it (normal runtime path)
    if _require_auth_dependency is not None:
        return await _require_auth_dependency(session_token=session_token)

    # Fallback: inline verification when state hasn't been wired yet
    # (e.g. during testing or before lifespan runs)
    from apps.server.state import auth_manager as _auth_manager

    _is_production = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).lower() == "production"
    auth_enabled = _auth_manager.config.get("auth_enabled", True) if _auth_manager else True

    if not auth_enabled and not _is_production:
        return "anonymous"

    if not auth_enabled and _is_production:
        auth_enabled = True

    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if _auth_manager is None:
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    is_valid, username = await _auth_manager.verify_session(session_token)
    if not is_valid:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    return username


async def _verify_admin_key(request: Request) -> None:
    if _is_localhost(request):
        return

    from apps.server.state import settings

    if not settings.admin_api_key:
        logger.error("ADMIN_API_KEY not configured - admin endpoint blocked")
        raise HTTPException(status_code=503, detail="Service unavailable")

    provided = request.headers.get("X-Admin-Key", "")
    client_ip = get_trusted_client_ip(request) if request.client else "unknown"

    # Accept current key or previous key (during rotation)
    key_valid = verify_secret_key(provided, settings.admin_api_key)
    is_previous_key = False
    if not key_valid and settings.admin_api_key_previous:
        key_valid = verify_secret_key(provided, settings.admin_api_key_previous)
        is_previous_key = True

    if not key_valid:
        log_security_event(
            event_type="admin_key_failure",
            description=f"Invalid admin key attempt from {client_ip}",
            severity="warning",
            source_ip=client_ip,
        )
        if failed_attempt_tracker:
            await failed_attempt_tracker.record_failure(client_ip)
        raise HTTPException(status_code=403, detail="Forbidden")

    if is_previous_key:
        log_security_event(
            event_type="admin_key_previous_used",
            description=f"Previous admin key used from {client_ip} - complete rotation",
            severity="info",
            source_ip=client_ip,
        )


async def _require_license_sync_access(request: Request) -> None:
    """Gate temporary sync endpoints behind explicit feature flag and admin key.

    Validates admin API key using constant-time comparison to prevent timing attacks.
    Also supports HMAC-SHA256 signature via X-PT-Signature header for integrations
    that can compute HMAC (custom scripts, but NOT TradingView alerts).
    """
    from apps.server.state import settings

    if not settings.enable_license_sync_endpoints:
        raise HTTPException(
            status_code=404,
            detail="Temporary license sync endpoints are disabled",
        )

    if not settings.admin_api_key:
        logger.error("ADMIN_API_KEY not configured - license sync endpoint blocked")
        raise HTTPException(status_code=503, detail="Service unavailable")

    signature = request.headers.get("X-PT-Signature", "")
    if signature:
        body = await request.body()
        if verify_hmac_signature(body, signature, settings.admin_api_key):
            return
        log_security_event(
            event_type="invalid_hmac_signature",
            description="HMAC signature verification failed on license sync endpoint",
            severity="warning",
            source_ip=get_trusted_client_ip(request) if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Invalid HMAC signature")

    auth_header = request.headers.get("X-Admin-Key", "")
    if not verify_secret_key(auth_header, settings.admin_api_key):
        client_ip = get_trusted_client_ip(request) if request.client else "unknown"
        log_security_event(
            event_type="admin_key_failure",
            description=f"Invalid admin key on license sync endpoint from {client_ip}",
            severity="warning",
            source_ip=client_ip,
        )
        if failed_attempt_tracker:
            await failed_attempt_tracker.record_failure(client_ip)
        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )


async def verify_signal_request(request: Request) -> None:
    """License check for signal polling endpoints.

    Tracks per-IP and per-license-key failed attempts to prevent brute-force.
    After 20 failures on the same license key within 1 hour, the key is
    temporarily blocked.
    """
    from apps.server.state import client_manager

    parts = request.url.path.strip("/").split("/")

    # Extract license key from URL path for signal/AK endpoints
    license_key = None
    if len(parts) >= 3 and parts[1] in ("signals", "signals-longpoll", "signals-batch-ack"):
        license_key = parts[2]
    elif (
        len(parts) >= 4
        and parts[1] == "ea"
        and parts[2]
        in (
            "download",
            "dll",
            "check",
            "audit",
        )
    ):
        # For /api/ea/download/{platform}, /api/ea/dll/download/{platform}, etc.
        # License key comes via X-License-Key header instead
        license_key = request.headers.get("X-License-Key")

    if not license_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    client_ip = get_trusted_client_ip(request) if request.client else "unknown"

    # Check per-license-key rate limit (separate from IP tracking)
    _license_attempt_key = f"license:{license_key}"
    if failed_attempt_tracker:
        if await failed_attempt_tracker.is_blocked(_license_attempt_key):
            log_security_event(
                event_type="license_key_blocked",
                description=f"License key {license_key[:8]}... blocked due to excessive failures",
                severity="warning",
                source_ip=client_ip,
            )
            raise HTTPException(
                status_code=429, detail="Too many failed attempts. Try again later."
            )

    valid, msg = client_manager.validate_license(license_key)
    if not valid:
        # Track per-IP failure
        if failed_attempt_tracker:
            await failed_attempt_tracker.record_failure(client_ip)
        # Track per-license-key failure with separate namespace
        if failed_attempt_tracker:
            await failed_attempt_tracker.record_failure(_license_attempt_key)
        raise HTTPException(status_code=403, detail=msg)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@router.get("/api/auth/sessions")
async def get_active_sessions(_username: str = Depends(_require_auth)):
    """Get active authentication sessions"""
    from apps.server.state import auth_manager

    sessions = []
    for token, data in auth_manager.sessions.items():
        sessions.append(
            {
                "token": token[:16] + "...",
                "username": data.get("username"),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
            }
        )

    return {
        "sessions": sessions,
        "count": len(sessions),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/api/auth/logs")
async def get_auth_logs(
    limit: int = Query(50, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get authentication activity logs"""
    from apps.server.state import admin_logger

    try:
        logs = admin_logger.get_recent_activity(limit)
        return {
            "logs": logs,
            "count": len(logs),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception:
        return {"logs": [], "count": 0}

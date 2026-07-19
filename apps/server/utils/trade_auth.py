"""Auth dependencies for trade analytics endpoints."""

from fastapi import HTTPException, Request

from apps.server.config.settings import get_config
from apps.server.utils.security import verify_secret_key


def _is_localhost(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in ("127.0.0.1", "::1", "localhost"))


async def get_current_user(request: Request):
    """Validate admin access via X-Admin-Key header. Localhost bypasses auth."""
    if _is_localhost(request):
        return {"auth": "localhost"}
    try:
        config = get_config()
        secret = config.admin_api_key
    except Exception:
        raise HTTPException(status_code=503, detail="Configuration unavailable")
    if not secret:
        raise HTTPException(status_code=503, detail="Service unavailable")
    provided = request.headers.get("X-Admin-Key", "")
    if not verify_secret_key(provided, secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"auth": "admin_key"}


async def get_current_user_optional(request: Request):
    """Optional auth — returns None when key is missing."""
    try:
        config = get_config()
        secret = config.admin_api_key
    except Exception:
        return None
    if not secret:
        return None
    provided = request.headers.get("X-Admin-Key", "")
    if verify_secret_key(provided, secret):
        return {"auth": "admin_key"}
    return None


def _get_client_manager():
    """Lazy import to avoid circular dependency."""
    from apps.server.state import client_manager

    return client_manager

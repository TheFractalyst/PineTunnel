"""FastAPI dependency for session-based admin authentication."""

import logging
import os
from typing import Callable

from fastapi import Cookie, HTTPException, Request

logger = logging.getLogger(__name__)


def _is_localhost(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in ("127.0.0.1", "::1", "localhost"))


def create_auth_dependency(auth_manager: object) -> Callable:
    """Create a FastAPI dependency that enforces session-based admin auth.

    Usage::

        _require_auth = create_auth_dependency(auth_manager)

        @app.get("/protected")
        async def protected(username: str = Depends(_require_auth)):
            ...
    """
    _is_production = os.getenv("APP_ENV", "").lower() == "production"

    async def require_session_auth(
        request: Request,
        session_token: str | None = Cookie(None),
    ) -> str:
        if _is_localhost(request):
            return "localhost"

        auth_enabled = auth_manager.config.get("auth_enabled", True)  # type: ignore[attr-defined]

        if not auth_enabled and not _is_production:
            return "anonymous"

        if not auth_enabled and _is_production:
            logger.warning("auth_enabled=False ignored in production - forcing auth on")
            auth_enabled = True

        if not session_token:
            raise HTTPException(status_code=401, detail="Not authenticated")

        is_valid, username = await auth_manager.verify_session(session_token)  # type: ignore[attr-defined]
        if not is_valid:
            raise HTTPException(status_code=401, detail="Session expired or invalid")

        return username

    return require_session_auth

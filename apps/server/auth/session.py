"""Session middleware and auth dependency for the dashboard."""

import os

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware

SESSION_COOKIE_NAME = "pinetunnel_admin"
SESSION_MAX_AGE = 28800

REQUIRE_AUTH = os.getenv("HOST", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost")


def setup_session_middleware(app: FastAPI, secret_key: str | None = None) -> None:
    key = secret_key or os.getenv("SESSION_SECRET", "")
    if not key:
        raise RuntimeError("SESSION_SECRET env var is required for dashboard auth")
    app.add_middleware(
        SessionMiddleware,
        secret_key=key,
        session_cookie=SESSION_COOKIE_NAME,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        path="/",
        https_only=False,
    )


async def require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")

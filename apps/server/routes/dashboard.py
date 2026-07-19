"""Dashboard API endpoints: auth, setup-status, config."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from apps.lib.env_manager import read_env, redact_value, write_env_updates
from apps.server.auth.session import require_auth
from apps.server.auth.telegram_auth import TelegramAuthStore


class LoginRequest(BaseModel):
    code: str
    user_id: int


class ConfigUpdateRequest(BaseModel):
    updates: dict[str, str]


def create_dashboard_router(
    auth_store: TelegramAuthStore,
    admin_ids: list[int],
    env_path: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

    @router.post("/login")
    async def login(req: LoginRequest, request: Request):
        if req.user_id not in admin_ids:
            raise HTTPException(status_code=401, detail="Not authorized")
        if not await auth_store.verify_code_async(req.code, expected_user_id=req.user_id):
            raise HTTPException(status_code=401, detail="Invalid or expired code")
        request.session["authenticated"] = True
        request.session["user_id"] = req.user_id
        return {"status": "ok"}

    @router.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"status": "ok"}

    @router.get("/setup-status")
    async def setup_status():
        env = read_env(env_path)
        return {
            "initialized": env.get("PINETUNNEL_INITIALIZED") == "true",
            "telegram_configured": bool(env.get("TELEGRAM_BOT_TOKEN")) and bool(env.get("TELEGRAM_ADMIN_IDS")),
            "cloudflare_configured": env.get("SERVER_BASE_URL", "").startswith("https://"),
        }

    @router.get("/config")
    async def get_config(_=Depends(require_auth)):
        env = read_env(env_path)
        return {k: redact_value(k, v) for k, v in env.items()}

    @router.put("/config")
    async def update_config(req: ConfigUpdateRequest, _=Depends(require_auth)):
        write_env_updates(env_path, req.updates)
        return {"status": "ok", "updated_keys": list(req.updates.keys())}

    return router

"""Dashboard API endpoints: auth, setup-status, config."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from apps.lib.env_manager import read_env, redact_value, write_env_updates
from apps.server.auth.session import require_auth
from apps.server.auth.telegram_auth import TelegramAuthStore


class LoginRequest(BaseModel):
    code: str
    user_id: int


class ConfigUpdateRequest(BaseModel):
    updates: dict[str, str]


class ValidateTelegramRequest(BaseModel):
    token: str
    user_id: str


class TestWebhookRequest(BaseModel):
    symbol: str = "EURUSD"
    action: str = "buy"
    lots: str = "0.10"


async def require_csrf(x_admin_csrf: str | None = Header(default=None)) -> None:
    if x_admin_csrf != "1":
        raise HTTPException(status_code=403, detail="Missing CSRF header")


def create_dashboard_router(
    auth_store: TelegramAuthStore,
    admin_ids: list[int],
    env_path: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

    @router.post("/login")
    async def login(req: LoginRequest, request: Request, _=Depends(require_csrf)):
        if req.user_id not in admin_ids:
            raise HTTPException(status_code=401, detail="Not authorized")
        if not await auth_store.verify_code_async(req.code, expected_user_id=req.user_id):
            raise HTTPException(status_code=401, detail="Invalid or expired code")
        request.session.clear()
        request.session["authenticated"] = True
        request.session["user_id"] = req.user_id
        return {"status": "ok"}

    @router.post("/logout")
    async def logout(request: Request, _=Depends(require_csrf)):
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
    async def update_config(req: ConfigUpdateRequest, _=Depends(require_auth), _c=Depends(require_csrf)):
        write_env_updates(env_path, req.updates)
        return {"status": "ok", "updated_keys": list(req.updates.keys())}

    @router.post("/validate-telegram")
    async def validate_telegram(req: ValidateTelegramRequest):
        token = req.token.strip()
        if not token:
            return {"valid": False, "error": "Token is required"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = resp.json()
        except httpx.TimeoutException:
            return {"valid": False, "error": "Telegram API timed out"}
        except Exception:
            return {"valid": False, "error": "Failed to reach Telegram API"}
        if not data.get("ok"):
            desc = data.get("description", "Invalid token")
            return {"valid": False, "error": desc}
        result = data.get("result", {})
        bot_username = result.get("username", "")
        bot_name = result.get("first_name", bot_username)
        return {
            "valid": True,
            "bot_username": f"@{bot_username}" if bot_username else "",
            "bot_name": bot_name,
        }

    @router.get("/webhook-url")
    async def webhook_url():
        env = read_env(env_path)
        base = env.get("SERVER_BASE_URL", "")
        ready = base.startswith("https://")
        url = base + "/" if base and not base.endswith("/") else base
        message = None if ready else "Complete Cloudflare setup first"
        return {"url": url, "ready": ready, "message": message}

    @router.post("/test-webhook")
    async def test_webhook(req: TestWebhookRequest, _=Depends(require_auth), _c=Depends(require_csrf)):
        env = read_env(env_path)
        webhook_secret = env.get("WEBHOOK_SECRET", "")
        port = env.get("PORT", "8000")

        from apps.server.state import client_manager

        license_key = "TEST"
        license_secret = webhook_secret
        if client_manager and client_manager.clients:
            first_key = next(iter(client_manager.clients))
            client = client_manager.clients[first_key]
            license_key = first_key
            license_secret = client.get("secret_key", webhook_secret)

        action = req.action.strip() or "buy"
        symbol = (req.symbol or "EURUSD").strip().upper()
        lots = (req.lots or "0.10").strip()
        payload = f"{license_key},{action},{symbol},lots={lots},secret={license_secret}"

        target = f"http://127.0.0.1:{port}/"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    target,
                    content=payload.encode("utf-8"),
                    headers={"Content-Type": "text/plain"},
                )
                body = resp.text
            return {
                "status": "sent",
                "response_code": resp.status_code,
                "response_body": body[:500],
            }
        except httpx.ConnectError:
            return {"status": "error", "message": f"Cannot connect to local webhook at {target}"}
        except httpx.TimeoutException:
            return {"status": "error", "message": "Local webhook timed out"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return router

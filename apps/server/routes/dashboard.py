"""Dashboard API endpoints: auth, setup-status, config."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from apps.lib.env_manager import read_env, redact_value, write_env_updates
from apps.server.auth.session import require_auth
from apps.server.auth.telegram_auth import TelegramAuthStore

_TELEGRAM_RELOAD_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_IDS"}


def _parse_admin_ids(raw: str) -> list[int]:
    return [int(s.strip()) for s in raw.split(",") if s.strip().isdigit()]


async def _reload_telegram_bot(env_path: Path) -> bool:
    from apps.server import state

    bot = getattr(state, "telegram_bot", None)
    env = read_env(env_path)
    new_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    new_admin_ids = _parse_admin_ids(env.get("TELEGRAM_ADMIN_IDS", ""))
    if not new_token or not new_admin_ids:
        return False
    os.environ["TELEGRAM_BOT_TOKEN"] = new_token
    os.environ["TELEGRAM_ADMIN_IDS"] = env.get("TELEGRAM_ADMIN_IDS", "")
    if bot is None:
        return False
    try:
        bot.token = new_token
        bot.admin_ids = new_admin_ids
        await bot.stop()
        await bot.start()
        return bot._started
    except Exception:
        return False


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
    sl: str | None = None
    tp: str | None = None


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
        needs_restart = False
        if _TELEGRAM_RELOAD_KEYS & set(req.updates.keys()):
            try:
                reloaded = await _reload_telegram_bot(env_path)
            except Exception:
                reloaded = False
            needs_restart = not reloaded
        return {"status": "ok", "updated_keys": list(req.updates.keys()), "needs_restart": needs_restart}

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
        parts = [license_key, action, symbol, f"lots={lots}"]
        sl_val = (req.sl or "").strip()
        tp_val = (req.tp or "").strip()
        if sl_val:
            parts.append(f"sl={sl_val}")
        if tp_val:
            parts.append(f"tp={tp_val}")
        parts.append(f"secret={license_secret}")
        payload = ",".join(parts)

        target = f"http://127.0.0.1:{port}/"
        import time as _time
        t0 = _time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    target,
                    content=payload.encode("utf-8"),
                    headers={"Content-Type": "text/plain"},
                )
                body = resp.text
            latency_ms = int((_time.perf_counter() - t0) * 1000)
            return {
                "status": "sent",
                "response_code": resp.status_code,
                "response_body": body[:500],
                "latency_ms": latency_ms,
            }
        except httpx.ConnectError:
            return {"status": "error", "message": f"Cannot connect to local webhook at {target}"}
        except httpx.TimeoutException:
            return {"status": "error", "message": "Local webhook timed out"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # -----------------------------------------------------------------
    # Session-gated proxies for management panels (Licenses, Security)
    # These reuse the same logic as the admin-key-gated endpoints but
    # authenticate via the dashboard session so the browser can reach
    # them without the X-Admin-Key header.
    # -----------------------------------------------------------------

    @router.get("/users")
    async def dashboard_users(_=Depends(require_auth)):
        from apps.server.state import client_manager, db_manager, ws_manager
        from sqlalchemy import text
        from sqlalchemy.exc import ProgrammingError
        import logging as _logging

        _log = _logging.getLogger("dashboard.users")
        if not client_manager:
            raise HTTPException(status_code=503, detail="Client manager not available")

        users: dict[str, dict] = {}
        for key, data in client_manager.clients.items():
            if not isinstance(data, dict):
                continue
            email = data.get("email", "") or ""
            if email not in users:
                users[email] = {"email": email, "name": data.get("name", ""), "licenses": []}
            users[email]["licenses"].append(
                {
                    "license_key": key,
                    "status": data.get("status", "active"),
                    "enabled": data.get("enabled", True),
                    "expires_at": data.get("expires_at"),
                    "secret_key": data.get("secret_key", ""),
                    "last_activity": data.get("last_activity"),
                }
            )

        all_license_keys: list[str] = []
        user_license_map: dict[str, list[str]] = {}
        for email, user_data in users.items():
            keys = [lk["license_key"] for lk in user_data["licenses"]]
            user_license_map[email] = keys
            all_license_keys.extend(keys)

        connected_by_lk: dict[str, int] = {}
        try:
            if ws_manager and hasattr(ws_manager, "get_connection_count"):
                for lk in all_license_keys:
                    connected_by_lk[lk] = ws_manager.get_connection_count(lk)
            elif ws_manager and hasattr(ws_manager, "_connections"):
                for lk in all_license_keys:
                    connected_by_lk[lk] = len(ws_manager._connections.get(lk, []))
        except Exception:
            pass

        if not db_manager or not all_license_keys:
            for user_data in users.values():
                ud_keys = user_license_map[user_data["email"]]
                user_data["stats"] = {
                    "total_signals": 0,
                    "total_trades": 0,
                    "total_positions": 0,
                    "connected_eas": sum(connected_by_lk.get(k, 0) for k in ud_keys),
                }
            return {"total_users": len(users), "users": list(users.values())}

        placeholders = ", ".join(f":lk{i}" for i in range(len(all_license_keys)))
        params = {f"lk{i}": lk for i, lk in enumerate(all_license_keys)}

        signal_counts_by_lk: dict[str, int] = {}
        trade_counts_by_lk: dict[str, int] = {}
        pos_counts_by_lk: dict[str, int] = {}

        try:
            with db_manager.get_connection() as session:
                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(*) FROM ws_signal_log "
                            f"WHERE license_key IN ({placeholders}) GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    signal_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    _log.warning("dashboard.users: ws_signal_log count skipped: %s", e)

                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(*) FROM trades "
                            f"WHERE license_key IN ({placeholders}) GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    trade_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    _log.warning("dashboard.users: trades count skipped: %s", e)

                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(DISTINCT (license_key, ticket)) "
                            f"FROM ws_open_positions WHERE license_key IN ({placeholders}) "
                            f"GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    pos_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    _log.warning("dashboard.users: ws_open_positions count skipped: %s", e)
        except Exception as e:
            _log.error("dashboard.users: telemetry summary failed: %s", e)

        for email, user_data in users.items():
            keys = user_license_map[email]
            user_data["stats"] = {
                "total_signals": sum(signal_counts_by_lk.get(k, 0) for k in keys),
                "total_trades": sum(trade_counts_by_lk.get(k, 0) for k in keys),
                "total_positions": sum(pos_counts_by_lk.get(k, 0) for k in keys),
                "connected_eas": sum(connected_by_lk.get(k, 0) for k in keys),
            }

        return {"total_users": len(users), "users": list(users.values())}

    @router.get("/rate-limits")
    async def dashboard_rate_limits(_=Depends(require_auth)):
        import time as _time
        from apps.server.state import rate_limiter

        stats = rate_limiter.get_statistics()
        blocked = []
        for ip, block_until in rate_limiter.blocked_ips.items():
            remaining = max(0, block_until - _time.time())
            blocked.append({"ip": ip, "remaining_seconds": int(remaining)})
        stats["blocked_ips"] = blocked
        return stats

    @router.get("/security-headers")
    async def dashboard_security_headers(_=Depends(require_auth)):
        tv_env = os.environ.get("TRADINGVIEW_IP_ALLOWLIST", "").lower()
        if tv_env in ("0", "false", "no"):
            tv_allowlist_on = False
        elif tv_env in ("1", "true", "yes"):
            tv_allowlist_on = True
        else:
            tv_allowlist_on = True
        headers = {
            "x_frame_options": "DENY",
            "content_security_policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'",
            "x_content_type_options": "nosniff",
            "referrer_policy": "strict-origin-when-cross-origin",
            "hsts": "max-age=15552000; includeSubDomains; preload",
            "x_xss_protection": "1; mode=block",
        }
        return {
            "headers": headers,
            "tradingview_ip_allowlist": tv_allowlist_on,
            "tradingview_ips": [ip.strip() for ip in os.environ.get("TRADINGVIEW_IPS", "").split(",") if ip.strip()] or ["52.89.214.238", "108.61.173.174", "52.89.214.238", "34.224.81.244"],
        }

    @router.get("/audit-actions")
    async def dashboard_audit_actions(_=Depends(require_auth), limit: int = 100):
        from apps.server.state import admin_logger
        from datetime import datetime

        try:
            actions = await asyncio.to_thread(admin_logger.get_recent_activity, limit)
            return {
                "actions": actions,
                "count": len(actions),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception:
            return {"actions": [], "count": 0, "error": "Failed to retrieve audit trail"}

    return router

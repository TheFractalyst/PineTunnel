"""Dashboard API endpoints: auth, setup-status, config."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from apps.lib.env_manager import generate_secret, read_env, redact_value, write_env_updates
from apps.server.auth.session import require_auth
from apps.server.auth.telegram_auth import TelegramAuthStore

_TELEGRAM_RELOAD_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_IDS"}

_ROTATABLE_PATTERNS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "PASSPHRASE")

_MIN_ENV_TEMPLATE = """\
HOST=127.0.0.1
PORT=8000
APP_ENV=production
WEBHOOK_SECRET={webhook_secret}
JWT_SECRET={jwt_secret}
ADMIN_API_KEY={admin_api_key}
SESSION_SECRET={session_secret}
SIGNAL_ENCRYPTION_KEY={encryption_key}
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=
SERVER_BASE_URL=http://127.0.0.1:8000
DATABASE_URL=sqlite:///pinetunnel.db
"""

_CONFIG_SCHEMA: dict[str, dict] = {
    "HOST": {"type": "str", "default": "127.0.0.1", "secret": False, "group": "Server"},
    "PORT": {"type": "int", "default": "8000", "secret": False, "group": "Server"},
    "APP_ENV": {"type": "str", "default": "production", "secret": False, "group": "Server"},
    "SERVER_BASE_URL": {"type": "str", "default": "http://127.0.0.1:8000", "secret": False, "group": "Server"},
    "SERVER_CORS_ORIGINS": {"type": "str", "default": "", "secret": False, "group": "Server"},
    "SERVER_WORKERS": {"type": "int", "default": "1", "secret": False, "group": "Server"},
    "WEBHOOK_SECRET": {"type": "str", "default": "", "secret": True, "group": "Security"},
    "JWT_SECRET": {"type": "str", "default": "", "secret": True, "group": "Security"},
    "ADMIN_API_KEY": {"type": "str", "default": "", "secret": True, "group": "Security"},
    "SIGNAL_ENCRYPTION_KEY": {"type": "str", "default": "", "secret": True, "group": "Security"},
    "TELEGRAM_BOT_TOKEN": {"type": "str", "default": "", "secret": True, "group": "Telegram"},
    "TELEGRAM_ADMIN_IDS": {"type": "str", "default": "", "secret": False, "group": "Telegram"},
    "TELEGRAM_BOT_URL": {"type": "str", "default": "", "secret": False, "group": "Telegram"},
    "DATABASE_URL": {"type": "str", "default": "sqlite:///pinetunnel.db", "secret": False, "group": "Database"},
    "REDIS_URL": {"type": "str", "default": "", "secret": False, "group": "Redis"},
    "TRUSTED_PROXY_COUNT": {"type": "int", "default": "0", "secret": False, "group": "Trading"},
    "TRADINGVIEW_IP_ALLOWLIST": {"type": "str", "default": "1", "secret": False, "group": "Trading"},
    "TRADINGVIEW_IPS": {"type": "str", "default": "", "secret": False, "group": "Trading"},
}


def _is_rotatable(key: str) -> bool:
    upper = key.upper()
    return any(p in upper for p in _ROTATABLE_PATTERNS)


def _generate_new_secret(key: str) -> str:
    upper = key.upper()
    if upper == "SIGNAL_ENCRYPTION_KEY":
        return secrets.token_hex(32)
    if "KEY" in upper or "TOKEN" in upper:
        return generate_secret(48)
    return generate_secret(32)

_LOGIN_RATE_LIMIT = 5
_LOGIN_RATE_WINDOW = 60
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _check_login_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _LOGIN_RATE_WINDOW
    attempts = _login_attempts[client_ip]
    _login_attempts[client_ip] = [t for t in attempts if t > cutoff]
    if len(_login_attempts[client_ip]) >= _LOGIN_RATE_LIMIT:
        return False
    _login_attempts[client_ip].append(now)
    return True


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


class RotateRequest(BaseModel):
    key: str


class ResetRequest(BaseModel):
    confirm: bool = False


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
        client_ip = _get_client_ip(request)
        if not _check_login_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
        if req.user_id not in admin_ids:
            raise HTTPException(status_code=401, detail="Not authorized")
        if not await auth_store.verify_code_async(req.code, expected_user_id=req.user_id):
            raise HTTPException(status_code=401, detail="Invalid or expired code")
        request.session.clear()
        request.session["authenticated"] = True
        request.session["user_id"] = req.user_id
        _login_attempts.pop(client_ip, None)
        return {"status": "ok"}

    @router.post("/logout")
    async def logout(request: Request, _=Depends(require_csrf)):
        request.session.clear()
        return {"status": "ok"}

    @router.get("/setup-status")
    async def setup_status():
        env = read_env(env_path)
        from apps.server.state import settings as _settings

        tg_settings = getattr(_settings, "telegram", None) if _settings else None
        server_settings = getattr(_settings, "server", None) if _settings else None
        if tg_settings is not None:
            telegram_configured = bool(tg_settings.is_configured)
        else:
            telegram_configured = bool(env.get("TELEGRAM_BOT_TOKEN")) and bool(env.get("TELEGRAM_ADMIN_IDS"))
        if server_settings is not None:
            base_url = getattr(server_settings, "base_url", "") or ""
        else:
            base_url = env.get("SERVER_BASE_URL", "")
        return {
            "initialized": env.get("PINETUNNEL_INITIALIZED") == "true",
            "telegram_configured": telegram_configured,
            "cloudflare_configured": base_url.startswith("https://"),
            "server_url": base_url,
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

    @router.get("/config/schema")
    async def get_config_schema(_=Depends(require_auth)):
        return _CONFIG_SCHEMA

    @router.post("/config/rotate")
    async def rotate_config(req: RotateRequest, _=Depends(require_auth), _c=Depends(require_csrf)):
        key = req.key.strip().upper()
        if not key:
            raise HTTPException(status_code=400, detail="key is required")
        if not _is_rotatable(key):
            raise HTTPException(status_code=400, detail=f"{key} is not a rotatable secret")
        env = read_env(env_path)
        current_value = env.get(key, "")
        updates: dict[str, str] = {}
        if current_value:
            previous_key = f"{key}_PREVIOUS"
            updates[previous_key] = current_value
        new_value = _generate_new_secret(key)
        updates[key] = new_value
        write_env_updates(env_path, updates)
        needs_restart = False
        if key == "TELEGRAM_BOT_TOKEN":
            try:
                reloaded = await _reload_telegram_bot(env_path)
            except Exception:
                reloaded = False
            needs_restart = not reloaded
        return {
            "status": "ok",
            "key": key,
            "new_value": redact_value(key, new_value),
            "needs_restart": needs_restart,
        }

    @router.post("/config/reset")
    async def reset_config(req: ResetRequest, _=Depends(require_auth), _c=Depends(require_csrf)):
        if not req.confirm:
            raise HTTPException(status_code=400, detail="confirm must be true to reset settings")
        if env_path.exists():
            try:
                env_path.unlink()
            except OSError:
                pass
        content = _MIN_ENV_TEMPLATE.format(
            webhook_secret=generate_secret(32),
            jwt_secret=generate_secret(48),
            admin_api_key=generate_secret(48),
            session_secret=generate_secret(32),
            encryption_key=secrets.token_hex(32),
        )
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        return {"status": "ok", "message": "Settings reset. Restart the server for changes to take effect."}

    @router.post("/validate-telegram")
    async def validate_telegram(req: ValidateTelegramRequest, _=Depends(require_csrf)):
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
        from apps.server.state import settings as _settings

        server_settings = getattr(_settings, "server", None) if _settings else None
        if server_settings is not None:
            base = getattr(server_settings, "base_url", "") or ""
        else:
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
            return {"status": "error", "message": "Cannot connect to local webhook server"}
        except httpx.TimeoutException:
            return {"status": "error", "message": "Local webhook timed out"}
        except Exception:
            return {"status": "error", "message": "Webhook test failed"}

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
        from apps.server.middleware.main import failed_attempt_tracker
        from apps.server.state import rate_limiter

        rl_stats: dict = {}
        rl_blocked: list[dict] = []
        if rate_limiter is not None:
            rl_stats = rate_limiter.get_statistics()
            for ip, block_until in rate_limiter.blocked_ips.items():
                remaining = max(0, block_until - _time.time())
                rl_blocked.append({
                    "ip": ip,
                    "remaining_seconds": int(remaining),
                    "source": "rate_limiter",
                    "reason": "Rate limit violations (20+ in 5min)",
                })

        fa_stats: dict = {"blocked_ips": [], "blocked_ip_count": 0, "failed_attempts_24h": 0}
        if failed_attempt_tracker is not None:
            fa_stats = failed_attempt_tracker.get_statistics()

        fa_blocked: list[dict] = []
        for entry in fa_stats.get("blocked_ips", []):
            fa_blocked.append({
                "ip": entry["ip"],
                "remaining_seconds": entry["remaining_seconds"],
                "source": "failed_attempt_tracker",
                "reason": "Failed auth attempts (10+ in 1hr)",
            })

        merged_blocked = fa_blocked + rl_blocked

        return {
            "total_requests": rl_stats.get("total_requests", 0),
            "blocked_requests": rl_stats.get("blocked_requests", 0),
            "rate_limited_requests": rl_stats.get("rate_limited_requests", 0),
            "passed_requests": rl_stats.get("passed_requests", 0),
            "active_identifiers": rl_stats.get("active_identifiers", 0),
            "pass_rate": rl_stats.get("pass_rate", 100),
            "blocked_ips": merged_blocked,
            "blocked_ip_count": len(merged_blocked),
            "failed_attempts_24h": fa_stats.get("failed_attempts_24h", 0),
            "rate_limiter_blocked_count": len(rl_blocked),
            "failed_attempt_blocked_count": len(fa_blocked),
        }

    @router.delete("/rate-limits/{ip}")
    async def dashboard_unblock_ip(ip: str, _=Depends(require_auth), _c=Depends(require_csrf)):
        import asyncio
        from apps.server.middleware.main import failed_attempt_tracker
        from apps.server.state import rate_limiter

        unblocked_any = False
        if rate_limiter is not None and ip in rate_limiter.blocked_ips:
            rate_limiter.unblock_identifier(ip)
            unblocked_any = True
        if failed_attempt_tracker is not None:
            await failed_attempt_tracker.reset_async(ip)
            unblocked_any = True
        if not unblocked_any:
            return {"success": False, "message": f"IP {ip} is not blocked"}
        logger.info("Dashboard: unblocked IP %s", ip)
        return {"success": True, "message": f"IP {ip} unblocked"}

    @router.get("/security-headers")
    async def dashboard_security_headers(_=Depends(require_auth)):
        from apps.server.config.settings import get_config
        from apps.server.middleware.ip_validation import _TRADINGVIEW_IPS
        from apps.server.middleware.security import get_security_headers

        cfg = get_config()
        tv_env = cfg.tradingview_ip_allowlist.lower()
        if tv_env in ("0", "false", "no"):
            tv_allowlist_on = False
        elif tv_env in ("1", "true", "yes"):
            tv_allowlist_on = True
        else:
            tv_allowlist_on = cfg.environment == "production"

        env_ips = cfg.tradingview_ips
        if env_ips:
            tv_ips = [ip.strip() for ip in env_ips.split(",") if ip.strip()]
        else:
            tv_ips = sorted(_TRADINGVIEW_IPS)

        headers = get_security_headers()
        return {
            "headers": headers,
            "headers_active": len(headers),
            "tradingview_ip_allowlist": tv_allowlist_on,
            "tradingview_ips": tv_ips,
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

    _PATH_RE = re.compile(r"(?:/[\w.\-]+)+/?|[A-Za-z]:\\[^\s]*")

    def _sanitize_err(e: Exception) -> str:
        text = str(e)[:200]
        return _PATH_RE.sub("<path>", text)

    @router.get("/bot-info")
    async def bot_info(_=Depends(require_auth)):
        from apps.server import state

        bot = getattr(state, "telegram_bot", None)
        if bot is None:
            return {
                "started": False,
                "has_app": False,
                "token_set": False,
                "updater_running": False,
                "app_running": False,
                "username": None,
                "first_name": None,
                "handler_count": 0,
                "admin_ids": [],
                "alerts_enabled": False,
            }
        admin_ids = list(getattr(bot, "admin_ids", []) or [])
        alerts_enabled = bool(getattr(bot, "alerts_enabled", False))
        started = bool(getattr(bot, "_started", False))
        token_set = bool(getattr(bot, "token", ""))
        app = getattr(bot, "app", None)
        has_app = app is not None
        updater_running = bool(app and getattr(app, "updater", None) and getattr(app.updater, "running", False))
        app_running = bool(app and getattr(app, "running", False))
        handler_count = 0
        if app is not None:
            handlers = getattr(app, "handlers", None)
            if isinstance(handlers, dict):
                for grp in handlers.values():
                    if isinstance(grp, list):
                        handler_count += len(grp)
        username = getattr(bot, "_cached_bot_username", None)
        first_name = getattr(bot, "_cached_bot_first_name", None)
        if (username is None or first_name is None) and app is not None and started:
            try:
                me = await app.bot.get_me()
                bot._cached_bot_username = me.username
                bot._cached_bot_first_name = me.first_name
                username = me.username
                first_name = me.first_name
            except Exception:
                pass
        return {
            "started": started,
            "has_app": has_app,
            "token_set": token_set,
            "updater_running": updater_running,
            "app_running": app_running,
            "username": username,
            "first_name": first_name,
            "handler_count": handler_count,
            "admin_ids": admin_ids,
            "alerts_enabled": alerts_enabled,
        }

    @router.post("/bot-test")
    async def bot_test(_=Depends(require_auth), _c=Depends(require_csrf)):
        from apps.server import state

        bot = getattr(state, "telegram_bot", None)
        if bot is None or not getattr(bot, "_started", False) or getattr(bot, "app", None) is None:
            return {"success": False, "error": "Bot not running"}
        admin_ids = list(getattr(bot, "admin_ids", []) or [])
        if not admin_ids:
            env_admin = os.environ.get("TELEGRAM_ADMIN_IDS", "")
            admin_ids = [int(s.strip()) for s in env_admin.split(",") if s.strip().isdigit()]
        if not admin_ids:
            return {"success": False, "error": "No admin IDs configured"}
        ts = datetime.now(timezone.utc).isoformat()
        text = f"Test message from PineTunnel dashboard at {ts}"
        try:
            await bot.app.bot.send_message(chat_id=admin_ids[0], text=text)
            return {"success": True, "message": "Test message sent"}
        except Exception as e:
            return {"success": False, "error": _sanitize_err(e)}

    return router

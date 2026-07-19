"""Admin, monitoring, and debug routes."""

import asyncio
import logging
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import _require_auth, _verify_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# Admin rate-limit management (webhook-secret-protected)
# ---------------------------------------------------------------------------


@router.get("/api/admin/rate-limits")
async def admin_rate_limit_stats(_: None = Depends(_verify_admin_key)):
    from apps.server.state import rate_limiter

    stats = rate_limiter.get_statistics()
    blocked = []
    for ip, block_until in rate_limiter.blocked_ips.items():
        remaining = max(0, block_until - time.time())
        blocked.append({"ip": ip, "remaining_seconds": int(remaining)})
    stats["blocked_ips"] = blocked
    return stats


@router.delete("/api/admin/rate-limits/{ip}")
async def admin_unblock_ip(ip: str, _: None = Depends(_verify_admin_key)):
    from apps.server.state import rate_limiter

    if ip not in rate_limiter.blocked_ips:
        return {"success": False, "message": f"IP {ip} is not blocked"}
    rate_limiter.unblock_identifier(ip)
    logger.info("Admin API: unblocked IP %s", ip)
    return {"success": True, "message": f"IP {ip} unblocked"}


@router.post("/api/admin/rate-limits/{ip}/reset")
async def admin_reset_ip(ip: str, _: None = Depends(_verify_admin_key)):
    from apps.server.state import rate_limiter

    rate_limiter.reset_identifier(ip)
    logger.info("Admin API: reset rate limits for IP %s", ip)
    return {"success": True, "message": f"Rate limits reset for IP {ip}"}


# ---------------------------------------------------------------------------
# Webhook activity API
# ---------------------------------------------------------------------------


@router.get("/api/webhooks/recent")
async def get_webhook_logs(
    limit: int = Query(50, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get recent webhook requests from alert_history table"""
    from apps.server.state import db_manager

    try:
        rows = await asyncio.to_thread(
            db_manager.execute_query,
            """
            SELECT id, timestamp, action, symbol, volume, response_code,
                   response_message, execution_time_ms, ip_address, payload
            FROM alert_history
            ORDER BY timestamp DESC
            LIMIT :limit
            """,
            {"limit": limit},
        )

        webhooks_list = [
            {
                "id": w["id"],
                "timestamp": w["timestamp"],
                "endpoint": "/webhook",
                "action": w["action"],
                "symbol": w["symbol"],
                "volume": w["volume"],
                "status": "success" if w["response_code"] == 200 else "failed",
                "response_code": w["response_code"],
                "response_message": w["response_message"],
                "execution_time_ms": w["execution_time_ms"],
                "ip_address": w["ip_address"],
                "payload": w["payload"],
            }
            for w in rows
        ]

        return {
            "webhooks": webhooks_list,
            "count": len(webhooks_list),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("Webhook logs not available: %s", e)
        return {"webhooks": [], "count": 0}


@router.get("/api/signals/recent")
async def get_recent_signals(
    limit: int = Query(50, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get recent signals from ws_signal_log (signal delivery lifecycle).

    This is the same data the Telegram bot monitors via signal_queue
    (pending/acknowledged) but with the full lifecycle from ws_signal_log:
    pending -> delivered -> executed/failed.
    """
    from apps.server.state import db_manager

    try:
        rows = await asyncio.to_thread(
            db_manager.execute_query,
            """
            SELECT id, license_key, timestamp, signal_id, action, symbol, volume,
                   delivered_via, acknowledged, acknowledged_at,
                   execution_status, execution_detail, executed_at
            FROM ws_signal_log
            ORDER BY timestamp DESC
            LIMIT :limit
            """,
            {"limit": limit},
        )

        signals_list = []
        for r in rows:
            ts = r["timestamp"]
            ack_at = r.get("acknowledged_at")
            exec_at = r.get("executed_at")
            latency_ms = None
            if exec_at and ts:
                delta = (exec_at - ts).total_seconds() * 1000.0
                latency_ms = round(delta, 1) if delta >= 0 else None
            elif ack_at and ts:
                delta = (ack_at - ts).total_seconds() * 1000.0
                latency_ms = round(delta, 1) if delta >= 0 else None
            signals_list.append(
                {
                    "id": r["id"],
                    "timestamp": ts,
                    "license_key": r["license_key"],
                    "signal_id": r["signal_id"],
                    "action": r["action"],
                    "symbol": r["symbol"],
                    "volume": r["volume"],
                    "delivered_via": r["delivered_via"],
                    "acknowledged": bool(r.get("acknowledged", 0)),
                    "execution_status": r["execution_status"],
                    "execution_detail": r["execution_detail"],
                    "executed_at": exec_at,
                    "latency_ms": latency_ms,
                }
            )

        return {
            "signals": signals_list,
            "count": len(signals_list),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("Recent signals not available: %s", e)
        return {"signals": [], "count": 0}


@router.get("/api/webhooks/stats")
async def get_webhook_stats(days: int = 7, _username: str = Depends(_require_auth)):
    """Get webhook statistics"""
    from apps.server.state import db_manager

    try:
        date_expr = db_manager.sql_interval_days(days)

        rows = await asyncio.to_thread(
            db_manager.execute_query,
            f"SELECT "
            f"COUNT(*) AS cnt_total, "
            f"COUNT(CASE WHEN response_code = 200 THEN 1 END) AS cnt_success, "
            f"AVG(execution_time_ms) AS avg_ms "
            f"FROM alert_history WHERE DATE(timestamp) >= {date_expr}",
        )
        total = rows[0]["cnt_total"] if rows else 0
        successful = rows[0]["cnt_success"] if rows else 0
        avg_response_time = round(rows[0]["avg_ms"], 1) if rows and rows[0]["avg_ms"] else 45.0

        failed = total - successful
        success_rate = (successful / total * 100) if total > 0 else 0

        return {
            "total": total,
            "successful": successful,
            "failed": failed,
            "success_rate": round(success_rate, 1),
            "avg_response_time": avg_response_time,
            "period_days": days,
        }
    except Exception as e:
        logger.error("Error getting webhook stats: %s", e)
        return {
            "total": 0,
            "successful": 0,
            "failed": 0,
            "success_rate": 0,
            "avg_response_time": 45.0,
            "period_days": days,
        }


# ---------------------------------------------------------------------------
# Error logs API
# ---------------------------------------------------------------------------


@router.get("/api/logs/errors")
async def get_error_logs(
    limit: int = Query(50, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get recent error logs"""
    try:
        # Read from log file
        log_file = Path(__file__).resolve().parents[1] / "mt5_webhook.log"
        if not log_file.exists():
            return {"errors": ["Request failed"], "count": 0}

        def _read_log_lines():
            with open(log_file, "r") as f:
                return f.readlines()[-500:]

        lines = await asyncio.to_thread(_read_log_lines)
        errors = [
            {
                "timestamp": line.split(" - ")[0] if " - " in line else "",
                "level": "ERROR" if "ERROR" in line else "WARNING",
                "message": line.split(" - ")[-1].strip() if " - " in line else line.strip(),
            }
            for line in lines
            if "ERROR" in line or "WARNING" in line
        ][-limit:]

        return {
            "errors": errors,
            "count": len(errors),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Failed to read error logs: %s", e)
        return {"errors": ["Request failed"], "count": 0}


# ---------------------------------------------------------------------------
# Database stats API
# ---------------------------------------------------------------------------


@router.get("/api/database/stats")
async def get_database_stats(_username: str = Depends(_require_auth)):
    """Get database statistics"""
    from apps.server.state import db_manager

    try:
        # Get table sizes
        tables = {}

        # Trades table
        trades_rows = await asyncio.to_thread(
            db_manager.execute_query, "SELECT COUNT(*) as cnt FROM trades"
        )
        tables["trades"] = trades_rows[0]["cnt"] if trades_rows else 0

        # Try to get alert_history table (stores webhook logs)
        try:
            alert_rows = await asyncio.to_thread(
                db_manager.execute_query, "SELECT COUNT(*) as cnt FROM alert_history"
            )
            tables["alert_history"] = alert_rows[0]["cnt"] if alert_rows else 0
        except Exception:
            tables["alert_history"] = 0

        # Database size from PostgreSQL
        db_size_mb = 0.0
        try:
            size_rows = await asyncio.to_thread(
                db_manager.execute_query,
                "SELECT pg_database_size(current_database()) / 1024.0 / 1024.0 as size_mb",
            )
            if size_rows:
                db_size_mb = round(float(size_rows[0]["size_mb"]), 2)
        except Exception:
            logger.debug("Could not query database size (permission denied is common)")

        return {
            "tables": tables,
            "total_records": sum(tables.values()),
            "size_mb": db_size_mb,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Failed to get database stats: %s", e)
        return {"error": "Failed to retrieve database stats"}


# ---------------------------------------------------------------------------
# Audit trail API
# ---------------------------------------------------------------------------


@router.get("/api/audit/actions")
async def get_audit_trail(
    limit: int = Query(100, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get admin action audit trail"""
    from apps.server.state import admin_logger

    try:
        actions = await asyncio.to_thread(admin_logger.get_recent_activity, limit)
        return {
            "actions": actions,
            "count": len(actions),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Failed to retrieve audit trail: %s", e)
        return {"actions": [], "count": 0, "error": "Failed to retrieve audit trail"}


# ---------------------------------------------------------------------------
# Database cleanup
# ---------------------------------------------------------------------------


@router.post("/api/database/cleanup")
async def cleanup_database(
    days_to_keep: int = Query(90, ge=1), _username: str = Depends(_require_auth)
):
    """Cleanup old database records"""
    from apps.server.state import db_manager

    result = await asyncio.to_thread(db_manager.cleanup_old_data, days_to_keep)
    return {"status": "success", "result": result}


# ---------------------------------------------------------------------------
# Trade activity & logs API
# ---------------------------------------------------------------------------


def _trade_row_to_dict(trade) -> dict:
    """Convert a trade row (dict or tuple) from the trades table into a JSON-serialisable dict."""
    if isinstance(trade, dict):
        return {
            "id": trade.get("id"),
            "timestamp": trade.get("timestamp"),
            "symbol": trade.get("symbol"),
            "action": trade.get("action"),
            "volume": trade.get("volume"),
            "price": trade.get("price"),
            "status": trade.get("status"),
            "client_id": trade.get("client_id", "unknown"),
            "message": trade.get("message", ""),
        }
    return {
        "id": trade[0],
        "timestamp": trade[1],
        "symbol": trade[2],
        "action": trade[3],
        "volume": trade[4],
        "price": trade[5],
        "status": trade[6],
        "client_id": trade[7] if len(trade) > 7 else "unknown",
        "message": trade[8] if len(trade) > 8 else "",
    }


@router.get("/api/trades/recent")
async def get_recent_trades(
    limit: int = Query(50, ge=1, le=500), _username: str = Depends(_require_auth)
):
    """Get recent trades with details"""
    from apps.server.state import db_manager

    try:
        rows = await asyncio.to_thread(
            db_manager.execute_query,
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT :limit",
            {"limit": limit},
        )
        trades_list = [_trade_row_to_dict(t) for t in rows]

        return {
            "trades": trades_list,
            "count": len(trades_list),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Failed to fetch recent trades: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch recent trades")


@router.get("/api/trades/search")
async def search_trades(
    _username: str = Depends(_require_auth),
    symbol: str | None = None,
    action: str | None = None,
    status: str | None = None,
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
):
    """Search trades with filters"""
    from apps.server.state import db_manager

    try:
        date_expr = db_manager.sql_interval_days(days)

        query = f"SELECT * FROM trades WHERE DATE(timestamp) >= {date_expr} "
        named_params: dict = {}
        if symbol:
            query += "AND symbol = :symbol "
            named_params["symbol"] = symbol
        if action:
            query += "AND action = :action "
            named_params["action"] = action
        if status:
            query += "AND status = :status "
            named_params["status"] = status
        query += "ORDER BY timestamp DESC LIMIT :limit"
        named_params["limit"] = limit
        rows = await asyncio.to_thread(db_manager.execute_query, query, named_params)

        trades_list = [_trade_row_to_dict(t) for t in rows]

        return {
            "trades": trades_list,
            "count": len(trades_list),
            "filters": {
                "symbol": symbol,
                "action": action,
                "status": status,
                "days": days,
            },
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Failed to search trades: %s", e)
        raise HTTPException(status_code=500, detail="Failed to search trades")


# ---------------------------------------------------------------------------
# Statistics & risk status
# ---------------------------------------------------------------------------


@router.get("/api/statistics")
async def get_statistics(days: int = 30, _username: str = Depends(_require_auth)):
    """Get trading statistics"""
    from apps.server.state import db_manager, rate_limiter

    stats, daily, symbols, alerts = await asyncio.gather(
        asyncio.to_thread(db_manager.get_trade_statistics, days),
        asyncio.to_thread(db_manager.get_daily_summary),
        asyncio.to_thread(db_manager.get_symbol_performance),
        asyncio.to_thread(db_manager.get_alert_statistics, 24),
    )
    rate_stats = rate_limiter.get_statistics()

    return {
        "overall": stats,
        "daily": daily,
        "symbol_performance": symbols,
        "alerts": alerts,
        "rate_limiting": rate_stats,
    }


@router.get("/api/risk-status")
async def get_risk_status(_username: str = Depends(_require_auth)):
    """Get current risk status"""
    from apps.server.state import mt5_manager, risk_manager

    account = mt5_manager.get_account_info()
    if account.get("error"):
        raise HTTPException(status_code=503, detail="MT5 not connected")

    risk_status = risk_manager.get_risk_status(account)
    can_trade, reason = risk_manager.can_trade(account, account.get("open_positions", 0))

    return {
        "can_trade": can_trade,
        "reason": reason,
        "risk_metrics": risk_status,
        "account": account,
    }


# ---------------------------------------------------------------------------
# Dashboard & positions
# ---------------------------------------------------------------------------


@router.get("/api/dashboard")
async def dashboard(_username: str = Depends(_require_auth)):
    """Full dashboard data — requires authentication"""
    from apps.server.state import mt5_manager, risk_manager

    account = mt5_manager.get_account_info()
    risk_status = risk_manager.get_risk_status(account) if not account.get("error") else {}
    return {
        "status": "online" if mt5_manager.initialized else "offline",
        "account": account,
        "risk_status": risk_status,
    }


@router.get("/positions")
async def get_positions(_username: str = Depends(_require_auth)):
    """Get open positions.

    The server runs in MT5 mock mode (the MetaTrader5 module is not loaded in
    this process), so live positions cannot be retrieved via this endpoint.
    Returns 503 rather than raising ``NameError`` on the undefined ``mt5`` symbol.
    """
    raise HTTPException(status_code=503, detail="MT5 positions unavailable via this endpoint")


# ---------------------------------------------------------------------------
# Debug routes (guarded by debug mode — return 404 when not in debug)
# ---------------------------------------------------------------------------


async def _require_debug():
    """Dependency that rejects requests when debug mode is off."""
    from apps.server.state import settings

    if not settings or not settings.debug:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/debug/telegram")
async def debug_telegram(
    _username: str = Depends(_require_auth), _dbg: None = Depends(_require_debug)
):
    """Debug endpoint to check Telegram bot status"""
    from apps.server.state import settings, telegram_bot

    _tg_token = settings.telegram.bot_token
    _tg_admin_ids = settings.telegram.parsed_admin_ids

    return {
        "environment_variables": {
            "TELEGRAM_BOT_TOKEN": f"Set ({len(_tg_token)} chars)" if _tg_token else None,
            "TELEGRAM_ADMIN_IDS": _tg_admin_ids,
        },
        "bot_status": {
            "initialized": telegram_bot is not None,
            "started": telegram_bot._started if telegram_bot else False,
            "app_exists": telegram_bot.app is not None if telegram_bot else False,
        },
        "handler_count": (
            len(telegram_bot.app.handlers) if telegram_bot and telegram_bot.app else 0
        ),
    }


@router.get("/debug/logs")
async def debug_logs(_username: str = Depends(_require_auth), _dbg: None = Depends(_require_debug)):
    """Get recent application logs to debug startup issues"""
    log_buffer = StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setLevel(logging.INFO)

    # Get the root logger
    root_logger = logging.getLogger()

    # Temporarily add our handler
    original_handlers = root_logger.handlers[:]
    root_logger.handlers = [handler]

    # Trigger a log message to test
    logger.info("=== Debug endpoint accessed ===")

    # Restore original handlers
    root_logger.handlers = original_handlers

    # Get the captured logs
    log_contents = log_buffer.getvalue()

    return {
        "recent_logs": log_contents.split("\n")[-20:],  # Last 20 lines
        "note": "This is a temporary debug endpoint. Check Render dashboard for full logs.",
    }


@router.get("/debug/telegram-test")
async def debug_telegram_test(
    _username: str = Depends(_require_auth), _dbg: None = Depends(_require_debug)
):
    """Test basic telegram library functionality"""
    from apps.server.state import settings

    try:
        from telegram import Bot
        from telegram.ext import Application

        token = settings.telegram.bot_token
        if not token:
            raise HTTPException(status_code=400, detail="No token configured")

        bot = Bot(token=token)
        app = Application.builder().token(token).build()
        await app.initialize()
        me = await bot.get_me()
        await app.shutdown()

        return {
            "success": True,
            "bot_info": {
                "id": me.id,
                "username": me.username,
                "first_name": me.first_name,
            },
            "message": "Telegram library works correctly",
        }
    except Exception as e:
        logger.error("Telegram test failed: %s", e)
        return {
            "success": False,
            "error": "Telegram test failed",
            "error_type": type(e).__name__,
        }


# ---------------------------------------------------------------------------
# Support chat logs (AI conversation review)
# ---------------------------------------------------------------------------


@router.get("/api/admin/support-logs/users")
async def api_support_log_users(_: None = Depends(_verify_admin_key)):
    """Get summary of all users who chatted with AI support."""
    from apps.server.state import db_manager

    if not db_manager or not hasattr(db_manager, "get_support_chat_users"):
        raise HTTPException(status_code=503, detail="Support logs not available")

    try:
        users = await asyncio.to_thread(db_manager.get_support_chat_users)
        return {"users": users, "count": len(users)}
    except Exception as e:
        logger.error("Failed to get support chat users: %s", e)
        raise HTTPException(status_code=500, detail="Failed to load support logs")


@router.get("/api/admin/support-logs/{chat_id}")
async def api_support_log_detail(chat_id: int, _: None = Depends(_verify_admin_key)):
    """Get full conversation transcript for a specific user."""
    from apps.server.state import db_manager

    if not db_manager or not hasattr(db_manager, "get_support_logs"):
        raise HTTPException(status_code=503, detail="Support logs not available")

    try:
        logs = await asyncio.to_thread(db_manager.get_support_logs, chat_id=chat_id, limit=200)
        return {"chat_id": chat_id, "messages": logs, "count": len(logs)}
    except Exception as e:
        logger.error("Failed to get support logs: %s", e)
        raise HTTPException(status_code=500, detail="Failed to load conversation")

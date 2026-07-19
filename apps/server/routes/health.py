"""Health / system-status endpoints."""

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

try:
    import psutil
except ImportError:
    psutil = None

from apps.server.utils import format_uptime
from apps.server.utils.disk import get_disk_usage
from apps.server.utils.version_info import get_version_info
from apps.server.webhook.pipeline import get_ws_push_latency_stats

from .auth import _require_auth, _verify_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Security policy - standard /.well-known/security.txt
# Configure SECURITY_EMAIL and SECURITY_URL env vars to customize
SECURITY_TXT = f"""Contact: {os.getenv("SECURITY_EMAIL", "security@your-server.com")}
Preferred-Languages: en
Canonical: {os.getenv("SECURITY_URL", "https://your-server.com/.well-known/security.txt")}
"""


@router.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt():
    """Standard security contact information for vulnerability reporting."""
    return PlainTextResponse(SECURITY_TXT.strip(), media_type="text/plain")


@router.get("/")
async def root():
    """Root endpoint - no data exposed to unauthenticated visitors"""
    return {"status": "ok"}


@router.get("/health", include_in_schema=False)
async def health_check():
    """Public health check for Render load balancer.

    Per Render docs: performs operation-critical checks (DB connectivity).
    Returns 503 if critical components are down. Health checks run every
    few seconds, so only lightweight checks are included.
    """
    from apps.server.state import _redis_client, db_manager

    checks: dict[str, str] = {"status": "ok"}

    # Check if server is shutting down (health_manager marked not ready)
    from apps.server.config.health import health_manager
    if not health_manager.is_ready:
        checks["status"] = "shutting_down"
        return JSONResponse(status_code=503, content=checks)

    # DB connectivity (critical -- 503 if down)
    try:
        if db_manager:
            db_manager.execute_query("SELECT 1")
            checks["database"] = "ok"
        else:
            checks["database"] = "starting"
    except Exception:
        checks["database"] = "error"
        checks["status"] = "degraded"

    # Redis connectivity (non-critical -- in-memory fallback exists)
    if _redis_client:
        try:
            await _redis_client.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

    # Render git metadata (non-sensitive, for debugging)
    render_commit = os.environ.get("RENDER_GIT_COMMIT", "")
    if render_commit:
        checks["git_commit"] = render_commit[:12]

    if checks["status"] != "ok":
        return JSONResponse(status_code=503, content=checks)
    return checks


# Additional health endpoints for Render compatibility
@router.get("/health/live")
async def health_live():
    """Liveness probe for load balancer"""
    return {"status": "ok"}


@router.get("/health/bot", include_in_schema=False)
async def health_bot(_: None = Depends(_verify_admin_key)):
    """Debug endpoint - bot status (admin only)."""
    from apps.server.state import telegram_bot
    import os as _os

    env_status = {
        "TELEGRAM_BOT_TOKEN_set": bool(_os.getenv("TELEGRAM_BOT_TOKEN", "")),
        "TELEGRAM_BOT_TOKEN_len": len(_os.getenv("TELEGRAM_BOT_TOKEN", "")),
        "TELEGRAM_ADMIN_IDS": _os.getenv("TELEGRAM_ADMIN_IDS", ""),
    }

    if not telegram_bot:
        return {"bot": None, "error": "telegram_bot is None - not initialized", "env": env_status}
    app = telegram_bot.app
    updater_running = False
    if app and app.updater:
        updater_running = app.updater.running
    return {
        "started": telegram_bot._started,
        "has_app": app is not None,
        "updater_running": updater_running,
        "app_running": app.running if app else False,
        "token_set": bool(telegram_bot.token),
        "env": env_status,
    }


@router.get("/health/ready")
async def health_ready():
    """Readiness probe -- verifies service is ready to handle requests.

    Render uses this to decide whether to route traffic to the instance.
    Uses health_manager.is_ready flag set during lifespan startup.
    Returns minimal status only -- no component details exposed.
    """
    from apps.server.config.health import health_manager

    if health_manager.is_ready:
        return {"status": "ready"}
    return JSONResponse(status_code=503, content={"status": "not_ready"})


@router.get("/health/ea-check")
async def health_ea_check(_: None = Depends(_verify_admin_key)):
    """Show all EA connection state from PostgreSQL (single source of truth).

    Requires admin API key - exposes connection metadata.

    The threshold for "active" matches HTTP_POLLING_TIMEOUT so the dashboard
    shows the same live/non-live split as the Telegram bot's /connections.
    Full license keys are returned (both endpoints here are admin-authenticated,
    same as /api/ea/ws-telemetry/overview) so the frontend can deduplicate
    entries across the overview and ea-check payloads by real key.
    """
    from apps.server.state import conn_manager, db_manager, ws_manager
    from apps.server.ws.connection import HTTP_POLLING_TIMEOUT

    ws_keys = ws_manager.get_connected_license_keys() if ws_manager else []
    http_clients = conn_manager.get_active_http_clients() if conn_manager else []

    ws_connection_counts: dict[str, int] = {}
    if ws_manager:
        for lic_key in ws_keys:
            ws_connection_counts[lic_key] = ws_manager.get_connection_count(lic_key)

    db_connections: list[dict] = []
    if db_manager and hasattr(db_manager, "get_active_ea_connections"):
        db_connections = db_manager.get_active_ea_connections(
            stale_seconds=HTTP_POLLING_TIMEOUT
        )

    db_license_keys = list({c["license_key"] for c in db_connections})
    db_by_type: dict[str, int] = {}
    for c in db_connections:
        ct = c.get("connection_type", "unknown")
        db_by_type[ct] = db_by_type.get(ct, 0) + 1

    return {
        "pid": os.getpid(),
        "ws_total": ws_manager.get_total_connections() if ws_manager else 0,
        "ws_license_keys": [k[:8] + "..." for k in ws_keys],
        "ws_connection_counts": ws_connection_counts,
        "http_active": len(http_clients),
        "http_keys": [k[:8] + "..." for k in http_clients],
        "http_polling_timeout_sec": HTTP_POLLING_TIMEOUT,
        "db_total": len(db_connections),
        "db_unique_licenses": len(db_license_keys),
        "db_license_keys": [k[:8] + "..." for k in db_license_keys],
        "db_by_type": db_by_type,
        "db_connections": [
            {
                "license_key": c["license_key"],
                "type": c.get("connection_type", ""),
                "last_seen": str(c.get("last_seen", ""))[:19],
            }
            for c in db_connections
        ],
    }


@router.get("/health/status")
async def health_status(_username: str = Depends(_require_auth)):
    """Detailed service health - component-level status without account details"""
    from apps.server.state import mt5_manager, settings, ws_manager

    disk = get_disk_usage(settings.data_dir)
    process = psutil.Process() if psutil else None
    uptime_seconds = (
        (datetime.now() - datetime.fromtimestamp(process.create_time())).total_seconds()
        if process
        else 0
    )

    return {
        "status": "healthy" if mt5_manager.initialized else "degraded",
        "service": "pinetunnel",
        "uptime": format_uptime(uptime_seconds) if uptime_seconds else None,
        "components": {
            "database": "connected",
            "disk": disk.get("status", "unknown"),
            "websocket": {
                "active_connections": ws_manager.get_total_connections(),
                "connected_licenses": len(ws_manager.get_connected_license_keys()),
            },
        },
        "disk": {
            "used_percent": disk.get("used_percent"),
        },
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/api/connections")
async def get_active_connections(_username: str = Depends(_require_auth)):
    """Get active connections (HTTP polling + WebSocket) - for monitoring"""
    from apps.server.state import conn_manager, ws_manager

    result = conn_manager.build_connections_response()
    result["websocket"] = ws_manager.build_connections_response()
    active_http_keys = set(conn_manager.get_active_http_clients()) if conn_manager else set()
    ws_license_keys = set(ws_manager.get_connected_license_keys()) if ws_manager else set()
    result["total_unique_licenses"] = len(active_http_keys | ws_license_keys)
    result["timestamp"] = datetime.now().isoformat()
    return result


@router.get("/api/status")
async def get_combined_status(_username: str = Depends(_require_auth)):
    """
    Combined status endpoint for SwiftBar monitoring
    Returns health and connections in one call
    """
    now = datetime.now()

    from apps.server.state import conn_manager, mt5_manager, ws_manager

    health_data = {
        "status": "healthy" if mt5_manager.initialized else "degraded",
        "mt5_initialized": mt5_manager.initialized,
    }

    connections_data = conn_manager.build_public_connections_response()
    ws_connections_data = ws_manager.build_public_connections_response()

    # WebSocket push latency stats
    ws_latency = get_ws_push_latency_stats()

    return {
        "timestamp": now.isoformat(),
        "server_version": "2.0.1",
        "health": health_data,
        "connections": {**connections_data, **ws_connections_data},
        "ws_push_latency": ws_latency,
    }


@router.get("/api/system/health")
async def get_system_health(_username: str = Depends(_require_auth)):
    """Get comprehensive system health status"""
    from apps.server.state import (
        MT5_AVAILABLE,
        _redis_client,
        client_manager,
        db_manager,
    )

    if not psutil:

        raise HTTPException(status_code=503, detail="psutil not available")

    # Get process info
    process = psutil.Process()

    # Calculate uptime
    create_time = datetime.fromtimestamp(process.create_time())
    uptime_seconds = (datetime.now() - create_time).total_seconds()
    uptime_str = format_uptime(uptime_seconds)

    # CPU and Memory
    cpu_percent = process.cpu_percent(interval=0.1)
    memory_info = process.memory_info()
    memory_mb = memory_info.rss / 1024 / 1024

    # System-wide stats
    system_cpu = psutil.cpu_percent(interval=0.1)
    system_memory = psutil.virtual_memory()

    # Database health + pool stats
    db_health = "healthy"
    db_pool_stats = {}
    try:
        db_manager.execute_query("SELECT 1")
        db_pool_stats = db_manager.get_pool_stats()
    except Exception:
        db_health = "error"

    # Redis health
    redis_health = "not_configured"
    redis_info = {}
    if _redis_client:
        try:
            redis_info_raw = await _redis_client.info()
            redis_health = "healthy"
            redis_info = {
                "used_memory_mb": round(redis_info_raw.get("used_memory", 0) / 1024 / 1024, 2),
                "connected_clients": redis_info_raw.get("connected_clients", 0),
                "keyspace_hits": redis_info_raw.get("keyspace_hits", 0),
                "keyspace_misses": redis_info_raw.get("keyspace_misses", 0),
            }
        except Exception:
            redis_health = "error"

    response = {
        "status": "online",
        "uptime": uptime_str,
        "uptime_seconds": int(uptime_seconds),
        "process": {
            "cpu_percent": round(cpu_percent, 1),
            "memory_mb": round(memory_mb, 1),
            "memory_percent": round((memory_mb / system_memory.total * 100 * 1024 * 1024), 1),
            "threads": process.num_threads(),
        },
        "system": {
            "cpu_percent": round(system_cpu, 1),
            "memory_total_gb": round(system_memory.total / 1024 / 1024 / 1024, 2),
            "memory_available_gb": round(system_memory.available / 1024 / 1024 / 1024, 2),
            "memory_percent": round(system_memory.percent, 1),
        },
        "connections": {
            "total_clients": len(client_manager.clients),
        },
        "services": {
            "database": db_health,
            "redis": redis_health,
            "mt5": "connected" if MT5_AVAILABLE else "mock",
        },
        "db_pool": db_pool_stats,
        "redis_info": redis_info,
        "timestamp": datetime.now().isoformat(),
    }

    return response


@router.get("/api/system/stats")
async def get_system_stats(_username: str = Depends(_require_auth)):
    """Get detailed system statistics"""
    from apps.server.state import db_manager

    if not psutil:

        raise HTTPException(status_code=503, detail="psutil not available")

    disk_path = os.getenv("DATA_DIR", "/data" if os.path.exists("/data") else "/")
    disk = psutil.disk_usage(disk_path)

    # Network stats (if available)
    try:
        net_io = psutil.net_io_counters()
        network_stats = {
            "bytes_sent": net_io.bytes_sent,
            "bytes_recv": net_io.bytes_recv,
            "packets_sent": net_io.packets_sent,
            "packets_recv": net_io.packets_recv,
        }
    except Exception:
        network_stats = None

    # Get trade counts from database with error handling
    try:
        today_expr = db_manager.sql_today()
        week_ago_expr = db_manager.sql_interval_days(7)

        rows = db_manager.execute_query(
            f"SELECT "
            f"COUNT(*) AS total_trades, "
            f"COUNT(CASE WHEN DATE(timestamp) = {today_expr} THEN 1 END) AS today_trades, "
            f"COUNT(CASE WHEN DATE(timestamp) >= {week_ago_expr} THEN 1 END) AS total_recent_trades, "
            f"COUNT(CASE WHEN status = 'success' AND DATE(timestamp) >= {week_ago_expr} THEN 1 END) AS successful_trades "
            f"FROM trades"
        )
        row = rows[0] if rows else {}
        today_trades = row.get("today_trades", 0)
        total_trades = row.get("total_trades", 0)
        successful_trades = row.get("successful_trades", 0)
        total_recent_trades = row.get("total_recent_trades", 0)

        success_rate = (
            round((successful_trades / total_recent_trades * 100), 1)
            if total_recent_trades > 0
            else 0
        )

        trades_data = {
            "today": today_trades,
            "total": total_trades,
            "success_rate_7d": round(success_rate, 1),
        }
    except Exception as e:
        logger.error("Failed to get trade stats: %s", e)
        trades_data = {
            "today": 0,
            "total": 0,
            "success_rate_7d": 0,
            "error": "Database unavailable",
        }

    return {
        "disk": {
            "total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
            "used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
            "free_gb": round(disk.free / 1024 / 1024 / 1024, 2),
            "free_mb": round(disk.free / 1024 / 1024, 0),
            "total_mb": round(disk.total / 1024 / 1024, 0),
            "percent": disk.percent,
            "used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
            "path": disk_path,
        },
        "network": network_stats,
        "trades": trades_data,
        "db_pool": db_manager.get_pool_stats(),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/api/version")
async def api_version_endpoint(_username: str = Depends(_require_auth)):
    """Version info - requires authentication"""
    version_data = get_version_info()
    if version_data.get("version"):
        return {"version": version_data["version"], "required": version_data.get("required", False)}
    return {"version": "2.1.0", "required": False}

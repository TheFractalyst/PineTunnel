"""Built-in diagnostics endpoint.

Probes all subsystems and reports status with latency.
Used for root cause analysis and operational visibility.

GET /api/diagnostics - run all probes (admin key required)
GET /api/diagnostics/public - run safe public probes (no auth)
"""

import asyncio
import time
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import _verify_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["diagnostics"])


async def _probe_db() -> dict[str, Any]:
    """Probe database connectivity."""
    from apps.server.state import db_manager

    if not db_manager:
        return {"name": "database", "status": "degraded", "latency_ms": 0, "detail": "not initialized"}

    try:
        t0 = time.monotonic()
        if hasattr(db_manager, "execute_query"):
            db_manager.execute_query("SELECT 1")
        elif hasattr(db_manager, "get_pool_stats"):
            db_manager.get_pool_stats()
        latency = (time.monotonic() - t0) * 1000
        return {"name": "database", "status": "ok", "latency_ms": round(latency, 2)}
    except Exception as e:
        return {"name": "database", "status": "fail", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_redis() -> dict[str, Any]:
    """Probe Redis connectivity."""
    from apps.server.state import redis_client

    if not redis_client:
        return {"name": "redis", "status": "degraded", "latency_ms": 0, "detail": "not initialized"}

    try:
        t0 = time.monotonic()
        await redis_client.ping()
        latency = (time.monotonic() - t0) * 1000
        return {"name": "redis", "status": "ok", "latency_ms": round(latency, 2)}
    except Exception as e:
        return {"name": "redis", "status": "fail", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_websocket_hub() -> dict[str, Any]:
    """Probe WebSocket connection manager."""
    from apps.server.state import ws_manager

    if not ws_manager:
        return {"name": "websocket_hub", "status": "degraded", "latency_ms": 0, "detail": "not initialized"}

    try:
        total = 0
        if hasattr(ws_manager, "get_total_connections"):
            total = ws_manager.get_total_connections()
        return {"name": "websocket_hub", "status": "ok", "latency_ms": 0, "detail": f"{total} connections"}
    except Exception as e:
        return {"name": "websocket_hub", "status": "fail", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_signal_queue() -> dict[str, Any]:
    """Probe signal queue depth."""
    from apps.server.state import db_manager

    try:
        depth = 0
        if db_manager and hasattr(db_manager, "execute_query"):
            rows = db_manager.execute_query(
                "SELECT COUNT(*) as cnt FROM signal_queue WHERE status = 'acknowledged'"
            )
            if rows:
                depth = rows[0].get("cnt", 0)
        return {"name": "signal_queue", "status": "ok", "latency_ms": 0, "detail": f"{depth} pending"}
    except Exception as e:
        return {"name": "signal_queue", "status": "degraded", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_rate_limiter() -> dict[str, Any]:
    """Probe rate limiter."""
    from apps.server.state import rate_limiter

    if not rate_limiter:
        return {"name": "rate_limiter", "status": "degraded", "latency_ms": 0, "detail": "not initialized"}

    return {"name": "rate_limiter", "status": "ok", "latency_ms": 0}


async def _probe_client_manager() -> dict[str, Any]:
    """Probe client/license manager."""
    from apps.server.state import client_manager

    if not client_manager:
        return {"name": "client_manager", "status": "degraded", "latency_ms": 0, "detail": "not initialized"}

    try:
        count = 0
        if hasattr(client_manager, "clients"):
            count = len(client_manager.clients)
        return {"name": "client_manager", "status": "ok", "latency_ms": 0, "detail": f"{count} clients"}
    except Exception as e:
        return {"name": "client_manager", "status": "fail", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_disk() -> dict[str, Any]:
    """Probe disk usage of data directory."""
    try:
        import psutil
        from apps.server.state import settings

        data_dir = "."
        if settings and hasattr(settings, "data_dir"):
            data_dir = settings.data_dir

        usage = psutil.disk_usage(data_dir)
        return {
            "name": "disk",
            "status": "ok" if usage.percent < 90 else "warning",
            "latency_ms": 0,
            "detail": f"{usage.percent}% used, {usage.free // (1024*1024)} MB free",
        }
    except Exception as e:
        return {"name": "disk", "status": "degraded", "latency_ms": 0, "detail": str(e)[:120]}


async def _probe_memory() -> dict[str, Any]:
    """Probe process memory usage."""
    try:
        import psutil

        proc = psutil.Process()
        mem = proc.memory_info()
        return {
            "name": "memory",
            "status": "ok",
            "latency_ms": 0,
            "detail": f"RSS {mem.rss // (1024*1024)} MB",
        }
    except Exception as e:
        return {"name": "memory", "status": "degraded", "latency_ms": 0, "detail": str(e)[:120]}


async def _run_all_probes() -> dict[str, Any]:
    """Run all diagnostic probes concurrently and return results."""
    probes = [
        _probe_db(),
        _probe_redis(),
        _probe_websocket_hub(),
        _probe_signal_queue(),
        _probe_rate_limiter(),
        _probe_client_manager(),
        _probe_disk(),
        _probe_memory(),
    ]
    results = await asyncio.gather(*probes)

    all_ok = all(r["status"] == "ok" for r in results)
    any_fail = any(r["status"] == "fail" for r in results)

    return {
        "overall_status": "ok" if all_ok else ("fail" if any_fail else "degraded"),
        "timestamp": time.time(),
        "probes": results,
    }


@router.get("/api/diagnostics")
async def diagnostics(_admin: None = Depends(_verify_admin_key)) -> dict[str, Any]:
    """Run full system diagnostics. Requires admin API key.

    Probes: database, redis, websocket hub, signal queue, rate limiter,
    client manager, disk usage, memory usage.
    """
    return await _run_all_probes()


@router.get("/api/diagnostics/public")
async def public_diagnostics() -> dict[str, Any]:
    """Run safe public diagnostics (no sensitive data). Requires no auth.

    Returns only: overall_status, database status, redis status, uptime.
    """
    full = await _run_all_probes()
    db = next((p for p in full["probes"] if p["name"] == "database"), None)
    redis = next((p for p in full["probes"] if p["name"] == "redis"), None)

    return {
        "overall_status": full["overall_status"],
        "database": db["status"] if db else "unknown",
        "redis": redis["status"] if redis else "unknown",
        "uptime_seconds": round(time.time() - time.monotonic(), 0),
    }

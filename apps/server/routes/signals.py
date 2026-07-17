"""Signal polling endpoints for EA clients."""

import asyncio
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from apps.server.middleware.main import failed_attempt_tracker
from apps.server.routes.ea_versions import _ea_versions
from apps.server.utils.security import get_trusted_client_ip
from apps.server.webhook.signal_queue import (
    acknowledge_signal_async,
    acknowledge_signals_batch_async,
    get_pending_signals_async,
)

from .auth import _verify_admin_key, verify_signal_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["signals"])


# Throttle the per-poll DB EA-connection refresh to once per 30s per license.
# register_ea_connection is a blocking Postgres upsert; calling it on every poll
# lets one leaked license key freeze the single-worker event loop for all
# customers. 30s is well under the 120s stale threshold, so liveness detection
# is identical - only admin-displayed last_seen coarsens slightly.
_POLL_DB_REFRESH_INTERVAL_S = 30.0
_last_poll_db_refresh: dict[str, float] = {}


def _should_refresh_poll_db(license_key: str) -> bool:
    """Return True if this license's DB connection record should be refreshed now."""
    now = time.time()
    last = _last_poll_db_refresh.get(license_key, 0.0)
    if now - last >= _POLL_DB_REFRESH_INTERVAL_S:
        _last_poll_db_refresh[license_key] = now
        return True
    return False


# ---------------------------------------------------------------------------
# Shared state accessors (wired during lifespan)
# ---------------------------------------------------------------------------


def _get_signal_queue(license_key: str):
    """Return (or create) the asyncio.Queue for a license key."""
    from apps.server.state import conn_manager

    return conn_manager.get_signal_queue(license_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_poll_activity(license_key: str, request: Request) -> None:
    """Shared polling registration: reset attempts, track HTTP client, register poll + DB connection."""
    from apps.server.state import client_manager, conn_manager, http_polling_clients

    client_ip = get_trusted_client_ip(request)
    client = client_manager.get_client_by_license(license_key)

    if failed_attempt_tracker:
        failed_attempt_tracker.reset(client_ip)

    http_polling_clients[license_key] = {
        "last_poll": datetime.now(),
        "client_info": client,
    }
    if hasattr(conn_manager, "register_poll"):
        try:
            await conn_manager.register_poll(license_key, client)
        except Exception as e:
            logger.debug("register_poll failed for %s: %s", license_key, e)

    if _should_refresh_poll_db(license_key):
        try:
            from apps.server.state import db_manager

            if db_manager and hasattr(db_manager, "register_ea_connection"):
                await asyncio.to_thread(
                    db_manager.register_ea_connection, license_key, "poll", os.getpid()
                )
        except Exception as e:
            logger.debug("register_ea_connection failed for %s: %s", license_key, e)


def _build_signals_response(
    license_key: str,
    signals: list,
    update_available: bool = False,
    update_message: str = "",
) -> JSONResponse:
    """Build the standard JSON response for signal polling endpoints."""
    response_data = {
        "status": "success",
        "license": license_key,
        "count": len(signals),
        "signals": signals,
        "timestamp": datetime.now().isoformat(),
        "latest_version_mt5": _ea_versions["mt5"]["version"],
        "latest_version_mt4": _ea_versions["mt4"]["version"],
        "update_notes_mt5": _ea_versions["mt5"].get("release_notes", ""),
        "update_notes_mt4": _ea_versions["mt4"].get("release_notes", ""),
        "update_available": "true" if update_available else "false",
        "update_message": update_message,
    }

    response = JSONResponse(content=response_data)
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/signals/{license_key}")
async def get_signals(
    license_key: str, request: Request, _sig: None = Depends(verify_signal_request)
):
    """
    V7.01: HTTP polling endpoint for EA to fetch pending signals
    Includes failed attempt tracking
    """
    await _register_poll_activity(license_key, request)

    signals = await get_pending_signals_async(license_key)

    if signals:
        logger.debug("Sending %s pending signal(s) to %s", len(signals), license_key)

    return _build_signals_response(
        license_key,
        signals,
        update_available=True,
        update_message=f"PineTunnel EA v{_ea_versions['mt5']['version']} available - update for longpoll support & faster execution. Contact admin for download.",
    )


@router.get("/api/signals-longpoll/{license_key}")
async def longpoll_signals(
    license_key: str,
    request: Request,
    timeout: int = 5,
    _sig: None = Depends(verify_signal_request),
):
    """
    Event-driven long-polling: holds connection open via asyncio.Queue.wait().
    Webhook notifies the queue instantly on signal arrival - sub-5ms server response.
    Falls back to empty response after timeout seconds.
    """
    await _register_poll_activity(license_key, request)

    # Edge case: signals may have arrived before EA connected (sitting in DB)
    signals = await get_pending_signals_async(license_key)
    if signals:
        logger.debug(
            "Longpoll: immediate delivery of %s signal(s) to %s", len(signals), license_key
        )
        return _build_signals_response(license_key, signals)

    # No pending signals - block on queue until webhook notifies or timeout
    timeout = max(1, min(timeout, 30))
    queue = _get_signal_queue(license_key)

    # Collect signal data carried in queue notifications (avoids post-wake DB read)
    queued_signals = []

    try:
        async with asyncio.timeout(float(timeout)):
            item = await queue.get()
            if isinstance(item, dict):
                queued_signals.append(item)
    except (TimeoutError, asyncio.QueueShutDown):
        pass

    # Drain any accumulated notifications (multiple webhooks during wait)
    while not queue.empty():
        try:
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_signals.append(item)
        except asyncio.QueueEmpty:
            break

    # Always read from DB for atomic claim, even when queue woke us up.
    # The queue is just a notification mechanism; DB is source of truth.
    signals = await get_pending_signals_async(license_key)
    if not signals and queued_signals:
        # Queue had data but DB claim returned nothing (race with another poller)
        # - the other poller already claimed these signals.
        pass

    return _build_signals_response(license_key, signals)


@router.delete("/api/signals/{license_key}/{signal_id}")
async def acknowledge_signal_endpoint(
    license_key: str, signal_id: str, request: Request, _sig: None = Depends(verify_signal_request)
):
    """
    EA calls this after successfully processing a signal.
    ACK is license-scoped: a caller's license must match the signal's owner
    (Gap 1 fix). A mismatched license returns 403.
    """
    from apps.server.state import client_manager, db_manager

    # Validate license
    client = client_manager.get_client_by_license(license_key)
    if not client:
        raise HTTPException(status_code=403, detail="Invalid license key")

    success = await acknowledge_signal_async(license_key, signal_id)

    if success:
        # Mark acknowledged in permanent signal log
        if db_manager and hasattr(db_manager, "acknowledge_signal_log"):
            try:
                await asyncio.to_thread(db_manager.acknowledge_signal_log, signal_id, "http")
            except Exception as e:
                logger.debug("acknowledge_signal_log failed for %s: %s", signal_id, e)
        return {
            "status": "acknowledged",
            "signal_id": signal_id,
            "timestamp": datetime.now().isoformat(),
        }
    # Distinguish "not found" from "license mismatch" for diagnostics
    try:
        rows = await asyncio.to_thread(
            db_manager.execute_query,
            "SELECT license_key, status FROM signal_queue WHERE signal_id = :sid",
            {"sid": signal_id},
        )
        _row = rows[0] if rows else None
    except Exception as e:
        _row = None
        logger.debug("Signal lookup failed for %s: %s", signal_id, e)
    if _row and _row["license_key"] != license_key:
        raise HTTPException(
            status_code=403,
            detail="Signal does not belong to this license",
        )
    raise HTTPException(status_code=404, detail="Signal not found")


@router.post("/api/signals-batch-ack/{license_key}")
async def batch_acknowledge_signals(
    license_key: str, request: Request, _sig: None = Depends(verify_signal_request)
):
    """
    Batch-acknowledge multiple signals in one HTTP call.
    Body: {"signal_ids": ["id1", "id2", ...]}
    """
    from apps.server.state import client_manager

    client = client_manager.get_client_by_license(license_key)
    if not client:
        raise HTTPException(status_code=403, detail="Invalid license key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if isinstance(body, list):
        signal_ids = body
    elif isinstance(body, dict):
        signal_ids = body.get("signal_ids", [])
    else:
        raise HTTPException(status_code=400, detail="Expected array or {signal_ids: [...]}")
    if not isinstance(signal_ids, list) or not signal_ids:
        raise HTTPException(status_code=400, detail="signal_ids must be a non-empty array")

    if len(signal_ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 signals per batch")

    if not all(isinstance(sid, str) and sid for sid in signal_ids):
        raise HTTPException(status_code=400, detail="All signal_ids must be non-empty strings")

    result = await acknowledge_signals_batch_async(license_key, signal_ids)

    # Mark acknowledged in permanent signal log
    from apps.server.state import db_manager

    db = db_manager
    if db and hasattr(db, "acknowledge_signal_log"):
        ack_sids = result.get("acknowledged", [])

        def _ack_logs():
            for sid in ack_sids:
                try:
                    db.acknowledge_signal_log(sid, "http")
                except Exception as e:
                    logger.debug("acknowledge_signal_log failed for %s: %s", sid, e)

        await asyncio.to_thread(_ack_logs)

    return {
        "status": "ok",
        "acknowledged": result.get("acknowledged", []),
        "failed": result.get("failed", []),
        "count": len(result.get("acknowledged", [])),
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Debug: per-license pipeline visibility
# ---------------------------------------------------------------------------


@router.get("/api/debug/license/{license_key}")
async def debug_license_pipeline(
    license_key: str, limit: int = 20, _admin: None = Depends(_verify_admin_key)
):
    """
    Per-license signal pipeline visibility for self-diagnosis.

    Returns:
      - client status (active/inactive/expired, last poll time)
      - signal_queue counts grouped by status
      - last N signals (any status) with age & ack latency
      - http polling activity for this license
      - async asyncio.Queue depth (longpoll waiters)

    License-gated: caller must know the license_key to query it. Does NOT
    expose signal_data payload, only metadata.
    """
    from apps.server.state import (
        client_manager,
        db_manager,
        http_polling_clients,
        signal_queues,
    )

    client = client_manager.get_client_by_license(license_key)
    if not client:
        raise HTTPException(status_code=403, detail="Invalid license key")

    limit = max(1, min(limit, 200))

    # Counts by status (last 24h)
    counts = {}
    recent = []
    try:
        interval_1d = db_manager.sql_interval_hours(24)
        age_expr = db_manager.sql_age_seconds("created_at")
        ack_latency_expr = (
            "EXTRACT(EPOCH FROM (COALESCE(acknowledged_at, NOW()) - created_at))::INTEGER"
        )
        json_action = db_manager.sql_json_extract("signal_data", "action")
        json_symbol = db_manager.sql_json_extract("signal_data", "symbol")

        rows = await asyncio.to_thread(
            db_manager.execute_query,
            f"""
            SELECT status, COUNT(*) as c
            FROM signal_queue
            WHERE license_key = :key
              AND created_at >= {interval_1d}
            GROUP BY status
            """,
            {"key": license_key},
        )
        for r in rows:
            counts[r["status"]] = r["c"]

        rows = await asyncio.to_thread(
            db_manager.execute_query,
            f"""
            SELECT signal_id, status, created_at, claimed_at, claimed_by,
                   acknowledged_at,
                   {age_expr} as age_s,
                   {ack_latency_expr} as ack_latency_s,
                   {json_action} as action,
                   {json_symbol} as symbol
            FROM signal_queue
            WHERE license_key = :key
            ORDER BY created_at DESC
            LIMIT :lim
            """,
            {"key": license_key, "lim": limit},
        )
        for r in rows:
            recent.append(
                {
                    "signal_id": r["signal_id"],
                    "status": r["status"],
                    "action": r["action"],
                    "symbol": r["symbol"],
                    "created_at": r["created_at"],
                    "claimed_at": r["claimed_at"],
                    "claimed_by": r["claimed_by"],
                    "acknowledged_at": r["acknowledged_at"],
                    "age_s": r["age_s"],
                    "ack_latency_s": (
                        r["ack_latency_s"] if r["status"] == "acknowledged" else None
                    ),
                }
            )
    except Exception as e:
        logger.error("debug_license query failed for %s: %s", license_key, e)
        raise HTTPException(status_code=500, detail="Debug query failed") from e

    poll_info = http_polling_clients.get(license_key) or {}
    last_poll = poll_info.get("last_poll")
    if isinstance(last_poll, datetime):
        last_poll = last_poll.isoformat()

    # Longpoll queue depth (pending notifications)
    try:
        q = signal_queues.get(license_key)
        longpoll_queue_size = q.qsize() if q is not None else 0
    except Exception:
        longpoll_queue_size = 0

    return {
        "license_key": license_key,
        "client": {
            "status": client.get("status"),
            "expires_at": client.get("expires_at"),
            "name": client.get("name"),
        },
        "polling": {
            "last_poll_at": last_poll,
            "longpoll_waiters_queue_depth": longpoll_queue_size,
        },
        "signal_counts_last_24h": counts,
        "recent_signals": recent,
        "hints": {
            "none_pending_but_tv_sent": (
                "If counts is empty, your TradingView alert never reached the server. "
                "Check TV webhook URL is https://<server>/ and alert first field matches this license."
            ),
            "pending_not_polled": (
                "If pending > 0 but last_poll_at is stale/null, the EA is not polling. "
                "Check EA SERVER_URL matches PT_LOCKED_ENDPOINT and InpLicenseID == this license."
            ),
            "claimed_not_acked": (
                "If claimed > 0 without ack_latency, EA fetched but never ACKed - likely crashed or URL mismatch blocks DELETE."
            ),
        },
        "timestamp": datetime.now().isoformat(),
    }

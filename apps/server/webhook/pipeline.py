"""Unified webhook signal delivery pipeline.

All webhook handlers (root, /webhook, pinetunnel) converge here
after parsing their specific format. This module handles:
  1. Queue signal (persistent DB via async executor)
  2. Push signal to WebSocket-connected EAs (instant delivery)
  3. Return standardized response
"""

import logging
import time
from datetime import datetime
from typing import Any, Callable, Coroutine

from fastapi.responses import JSONResponse

from apps.server.utils import mask_string as _mask

logger = logging.getLogger(__name__)

_DUPLICATE_WINDOW_MINUTES = 5
_STATUS_SERVICE_UNAVAILABLE = 503
_STATUS_OK = 200

# Latency tracking: counts and total ms for WS push delivery
_ws_push_count: int = 0
_ws_push_total_ms: float = 0.0

_queue_signal_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, int | None]] | None = None
_ws_push_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, int]] | None = None


def init_pipeline(
    queue_signal_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, int | None]],
) -> None:
    """Wire the persistent signal-queue function into the pipeline."""
    global _queue_signal_fn  # noqa: PLW0603
    _queue_signal_fn = queue_signal_fn


def init_ws_push(
    ws_push_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, int]],
) -> None:
    """Wire the WebSocket push function into the pipeline.

    After a signal is saved to DB, it is also pushed to any
    WebSocket-connected EAs for instant delivery.
    """
    global _ws_push_fn  # noqa: PLW0603
    _ws_push_fn = ws_push_fn


async def deliver_signal(
    license_key: str,
    signal_data: dict[str, Any],
    command: str = "",
    symbol: str = "",
) -> JSONResponse:
    """Queue a signal to the DB and return a standardized JSON response.

    Returns one of:
      - 503: pipeline not initialised or thread-pool exhausted
      - 200 (duplicate): signal deduplicated within the window
      - 200 (queued): signal accepted and persisted
    """
    # Record server-side receive timestamp for latency tracking
    if "server_received_at" not in signal_data:
        signal_data["server_received_at"] = time.time()

    if _queue_signal_fn is None:
        logger.error("Signal pipeline not initialized - cannot queue signal")
        return JSONResponse(
            status_code=_STATUS_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "message": "Signal pipeline not initialized",
                "command": command,
                "symbol": symbol,
                "timestamp": datetime.now().isoformat(),
            },
        )

    # Data validation layer: catch impossible signals before queuing
    from apps.server.webhook.validator import validate_signal
    validation = validate_signal(signal_data)
    if not validation:
        logger.warning(
            "Signal rejected by validator: %s | action=%s symbol=%s",
            validation.reason,
            command,
            symbol,
        )
        from apps.server.routes.metrics import record_webhook_signal
        record_webhook_signal(command, "rejected")
        return JSONResponse(
            status_code=422,
            content={
                "status": "rejected",
                "message": f"Signal validation failed: {validation.reason}",
                "command": command,
                "symbol": symbol,
                "details": validation.details,
                "timestamp": datetime.now().isoformat(),
            },
        )

    signal_id: int | None = None
    try:
        signal_id = await _queue_signal_fn(license_key, signal_data.copy())
    except RuntimeError as pool_err:
        logger.error(
            "CRITICAL: Failed to queue signal for %s: %s. Signal: %s %s",
            _mask(license_key),
            pool_err,
            command,
            symbol,
        )
        return JSONResponse(
            status_code=_STATUS_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "message": "Server temporarily unable to queue signal. Please retry.",
                "command": command,
                "symbol": symbol,
                "timestamp": datetime.now().isoformat(),
            },
        )

    if signal_id is None:
        from apps.server.routes.metrics import record_webhook_signal
        record_webhook_signal(command, "duplicate")
        return JSONResponse(
            status_code=_STATUS_OK,
            content={
                "status": "duplicate",
                "message": "Duplicate signal detected and skipped (already processed recently)",
                "command": command,
                "symbol": symbol,
                "duplicate_window_minutes": _DUPLICATE_WINDOW_MINUTES,
                "timestamp": datetime.now().isoformat(),
            },
        )

    # Push signal to WebSocket-connected EAs for instant delivery.
    # This is additive — signals are already persisted to DB, so EAs
    # that don't have WebSocket will still get them on their next poll.
    if _ws_push_fn is not None:
        try:
            # Ensure signal_id is included in push data — it's generated by the
            # DB queue, not by the webhook handler, so it won't be in signal_data yet.
            push_data = dict(signal_data)
            push_data["signal_id"] = signal_id
            push_data["server_received_at"] = signal_data.get("server_received_at", time.time())
            push_start = time.time()
            ws_sent = await _ws_push_fn(license_key, push_data)
            push_ms = (time.time() - push_start) * 1000
            global _ws_push_count, _ws_push_total_ms
            _ws_push_count += 1
            _ws_push_total_ms += push_ms
            from apps.server.routes.metrics import record_ws_delivery, set_ws_push_avg_ms
            set_ws_push_avg_ms(
                round(_ws_push_total_ms / _ws_push_count, 1) if _ws_push_count > 0 else 0.0
            )
            if ws_sent > 0:
                record_ws_delivery(ws_sent)
                logger.debug(
                    "WS push: %s delivered to %d EA(s) in %.1fms",
                    _mask(license_key),
                    ws_sent,
                    push_ms,
                )
        except Exception as ws_err:
            # WebSocket push failure must NOT affect the HTTP response —
            # the signal is already in DB and will be delivered via polling.
            logger.warning(
                "WS push failed for %s (signal will be delivered via poll): %s",
                _mask(license_key),
                ws_err,
            )

    from apps.server.routes.metrics import record_webhook_signal
    record_webhook_signal(command, "queued")

    response_data: dict[str, Any] = {
        "status": "queued",
        "message": "Signal queued for polling",
        "signal_id": signal_id,
        "timestamp": datetime.now().isoformat(),
    }
    if command:
        response_data["command"] = command
    if symbol:
        response_data["symbol"] = symbol

    return JSONResponse(status_code=_STATUS_OK, content=response_data)


def get_ws_push_latency_stats() -> dict[str, Any]:
    """Return WebSocket push latency statistics.

    Returns dict with count, total_ms, and avg_ms for WS signal pushes.
    Used by the health/status endpoint for monitoring.
    """
    return {
        "ws_push_count": _ws_push_count,
        "ws_push_total_ms": round(_ws_push_total_ms, 1),
        "ws_push_avg_ms": (
            round(_ws_push_total_ms / _ws_push_count, 1) if _ws_push_count > 0 else 0.0
        ),
    }

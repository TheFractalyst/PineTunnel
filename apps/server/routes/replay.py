"""Signal replay endpoint for end-to-end pipeline testing.

Allows admins to inject test signals into the live pipeline without
TradingView, verifying that the full chain works: webhook parser ->
signal queue -> WebSocket delivery -> EA response.

POST /api/admin/replay - inject a test signal
POST /api/admin/replay/batch - inject multiple test signals
GET  /api/admin/replay/results - check delivery results of replayed signals
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import _verify_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/replay", tags=["replay"])

_replay_results: list[dict[str, Any]] = []
_MAX_RESULTS = 100


class ReplaySignal(BaseModel):
    """Test signal to inject into the pipeline."""
    license_key: str = Field(..., description="Target license key")
    action: str = Field("buy", description="Trading command (buy, sell, close_long, etc.)")
    symbol: str = Field("EURUSD", description="Trading symbol")
    lots: float = Field(0.10, description="Lot size")
    sl: float | None = Field(None, description="Stop loss price")
    tp: float | None = Field(None, description="Take profit price")
    comment: str = Field("replay-test", description="Signal comment")
    secret: str = Field(..., description="Webhook secret for the license key")


class ReplayBatch(BaseModel):
    """Batch of test signals to inject."""
    signals: list[ReplaySignal] = Field(..., description="Signals to replay")


@router.post("")
async def replay_signal(
    signal: ReplaySignal,
    _admin: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Inject a single test signal into the live pipeline.

    The signal goes through the same path as a real TradingView webhook:
    CSV formatting -> webhook parser -> validation -> signal queue -> WebSocket delivery.

    This is useful for:
    - Verifying EA connectivity after deployment
    - Testing new command types after parser updates
    - Diagnosing signal delivery failures
    - Load testing the signal pipeline
    """
    csv_parts = [
        signal.license_key,
        signal.action,
        signal.symbol,
        f"lots={signal.lots}",
        f"comment={signal.comment}",
        f"secret={signal.secret}",
    ]
    if signal.sl is not None:
        csv_parts.append(f"sl={signal.sl}")
    if signal.tp is not None:
        csv_parts.append(f"tp={signal.tp}")

    csv_payload = ",".join(csv_parts)
    replay_id = str(uuid.uuid4())[:8]

    result = {
        "replay_id": replay_id,
        "csv_payload": csv_payload,
        "timestamp": time.time(),
        "status": "submitted",
    }

    try:
        from apps.server.webhook.parser import PineTunnelParser
        parser = PineTunnelParser()
        parsed = parser.parse(csv_payload)

        if parsed is None:
            result["status"] = "parse_failed"
            result["error"] = "Parser returned None - check CSV format"
            _store_result(result)
            return result

        from apps.server.webhook.validator import validate_signal
        validation = validate_signal(parsed)
        if not validation:
            result["status"] = "validation_failed"
            result["error"] = validation.reason
            result["parsed"] = parsed
            _store_result(result)
            return result

        result["parsed_action"] = parsed.get("action")
        result["parsed_symbol"] = parsed.get("symbol")

        from apps.server.state import ws_manager, client_manager

        client = client_manager.get_client_by_license(signal.license_key) if client_manager else None
        if not client:
            result["status"] = "license_not_found"
            result["error"] = f"License key not found: {signal.license_key}"
            _store_result(result)
            return result

        delivered = 0
        if ws_manager:
            try:
                delivered = await ws_manager.broadcast(signal.license_key, parsed)
                result["ws_delivered"] = delivered
                result["status"] = "delivered" if delivered > 0 else "no_connection"
            except Exception as e:
                result["status"] = "delivery_failed"
                result["error"] = str(e)[:200]
        else:
            result["status"] = "no_ws_manager"

        from apps.server.routes.metrics import record_webhook_signal
        record_webhook_signal(signal.action, result["status"])

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:200]
        logger.error("Replay failed: %s", e, exc_info=True)

    _store_result(result)
    return result


@router.post("/batch")
async def replay_batch(
    batch: ReplayBatch,
    _admin: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Inject multiple test signals sequentially.

    Useful for load testing the signal pipeline or verifying
    multiple command types in one call.
    """
    if len(batch.signals) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 signals per batch")

    results = []
    for signal in batch.signals:
        r = await replay_signal(signal, _admin)
        results.append(r)
        await asyncio.sleep(0.05)

    delivered = sum(1 for r in results if r.get("status") == "delivered")
    failed = sum(1 for r in results if "fail" in r.get("status", "") or r.get("status") == "error")

    return {
        "total": len(results),
        "delivered": delivered,
        "failed": failed,
        "results": results,
    }


@router.get("/results")
async def replay_results(
    _admin: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get results of recent replay signals.

    Returns the last 100 replay results with delivery status,
    parsed data, and any errors encountered.
    """
    return {
        "count": len(_replay_results),
        "results": list(reversed(_replay_results)),
    }


def _store_result(result: dict[str, Any]) -> None:
    """Store result in ring buffer (keep last 100)."""
    _replay_results.append(result)
    if len(_replay_results) > _MAX_RESULTS:
        _replay_results.pop(0)

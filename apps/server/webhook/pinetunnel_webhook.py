"""PineTunnel Webhook Integration
Handles PineTunnel syntax for MT5 Webhook Server

Features:
- 100% PineTunnel syntax compliance
- Multi-Strategy support (comment-based position filtering)
- 40+ commands (buy, sell, close_long, pending orders, etc.)

Author: PineTunnel
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from apps.server.config.settings import get_config
from apps.server.utils import log_trade_background
from apps.server.utils.security import get_trusted_client_ip, verify_webhook_signature
from apps.server.webhook import _state as deps
from apps.server.webhook.executor import execute_pinetunnel_command
from apps.server.webhook.parser import PineTunnelParser
from apps.server.webhook.types import PineTunnelRequest, _build_response, _build_trade_data

logger = logging.getLogger(__name__)

# Create router for PineTunnel endpoints
router = APIRouter(prefix="/pinetunnel", tags=["PineTunnel"])

_parser = PineTunnelParser()

# Backward-compat: keep state vars at module level so tests can patch them
# via apps.server.webhook.pinetunnel_webhook.<var>.  set_dependencies() below
# updates both these locals and _state for executor.py.
mt5_manager = None
client_manager = None
risk_manager = None
rate_limiter = None
db_manager = None


def set_dependencies(mt5, client, risk, rate, db):
    """Set global dependencies from main app - updates _state and local module."""
    deps.set_dependencies(mt5, client, risk, rate, db)
    global mt5_manager, client_manager, risk_manager, rate_limiter, db_manager
    mt5_manager = mt5
    client_manager = client
    risk_manager = risk
    rate_limiter = rate
    db_manager = db


async def _validate_webhook_secret(request: Request):
    """Verify HMAC-SHA256 webhook signature from X-PT-Signature header.

    If the X-PT-Signature header is absent (e.g. TradingView webhooks),
    HMAC is skipped and auth falls through to body secret= + license validation.

    Delegates to the shared verify_webhook_signature in security.py.
    """
    settings = get_config()
    await verify_webhook_signature(request, settings.webhook_secret, "PineTunnel webhook")


def _check_rate_limit(request: Request):
    """Rate limit check using the shared rate_limiter."""
    if rate_limiter is None:
        return
    client_ip = get_trusted_client_ip(request)
    allowed, reason, _ = rate_limiter.is_allowed(client_ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)


async def _handle_pinetunnel_signal(
    message: str,
    client_ip: str,
    req: Request,
    background_tasks: BackgroundTasks,
    key_override: str | None = None,
) -> JSONResponse:
    """Shared handler for both webhook endpoints.

    Parse -> validate license -> check MT5 -> execute -> log -> respond.
    """
    start_time = time.time()

    await _validate_webhook_secret(req)
    _check_rate_limit(req)

    signal = _parser.parse(message)

    if signal is None:
        logger.error("Failed to parse PineTunnel message: %s", message)
        raise HTTPException(status_code=400, detail="Invalid PineTunnel syntax")

    logger.info(
        "PineTunnel webhook from %s: Command=%s, Symbol=%s, Risk=%s, SL=%s",
        client_ip,
        signal.command.value,
        signal.symbol,
        signal.risk,
        signal.sl,
    )

    license_key = key_override if key_override else signal.license_id

    if not signal.secret:
        raise HTTPException(
            status_code=403,
            detail="Secret key required - include secret= in your alert",
        )

    if not client_manager:
        raise HTTPException(status_code=500, detail="Client manager not available")

    is_valid, error_msg = client_manager.validate_license(license_key)
    if not is_valid:
        logger.warning("Invalid license from %s: %s", client_ip, error_msg)
        raise HTTPException(status_code=401, detail=error_msg)

    client = client_manager.get_client_by_license(license_key)
    if not client:
        raise HTTPException(status_code=401, detail="License not found")

    # Server-side secret key validation (mandatory for all licenses)
    expected_secret = client.get("secret_key", "")
    if not expected_secret:
        raise HTTPException(
            status_code=403,
            detail="License has no secret key configured - contact admin",
        )
    if not hmac.compare_digest(signal.secret, expected_secret):
        logger.warning(
            "Secret key mismatch for %s: provided=%s expected=%s (masked)",
            license_key[:4] + "***",
            signal.secret[:2] + "***",
            expected_secret[:2] + "***",
        )
        raise HTTPException(
            status_code=403,
            detail="Invalid secret key",
        )

    if not client_manager.is_symbol_allowed(license_key, signal.symbol):
        raise HTTPException(
            status_code=403, detail=f"Symbol {signal.symbol} not allowed for this license"
        )

    logger.info("Request from license: %s", license_key)

    # Idempotency - reject duplicate execute signals (webhook retry / network
    # double-POST) before touching MT5. Records an 'acknowledged' row keyed on a
    # content hash so a retry within the dedup window is detected and skipped
    # instead of placing a second order. 'acknowledged' is invisible to the EA
    # poll (which reads only 'pending') and is reaped by existing cleanup. (C4)
    if db_manager:
        _dedup_data = {k: v for k, v in signal.to_dict().items() if k != "secret"}
        try:
            _exec_id = db_manager.save_signal(license_key, _dedup_data, status="acknowledged")
        except Exception as e:
            logger.warning(
                "Dedup record failed for %s - proceeding (fail-open): %s",
                license_key[:4] + "***",
                e,
            )
            _exec_id = "dedup-error"
        if _exec_id is None:
            logger.info(
                "Duplicate execute signal skipped (license=%s, cmd=%s, symbol=%s)",
                license_key[:4] + "***",
                signal.command.value,
                signal.symbol,
            )
            return _build_response(
                signal,
                (time.time() - start_time) * 1000,
                {
                    "success": True,
                    "duplicate": True,
                    "message": "Duplicate signal - already executed",
                },
            )

    if not mt5_manager:
        raise HTTPException(status_code=500, detail="MT5 manager not available")
    if not mt5_manager.initialized:
        if not mt5_manager.initialize():
            raise HTTPException(status_code=503, detail="MT5 not connected")

    account_info = mt5_manager.get_account_info()

    result = await execute_pinetunnel_command(
        signal=signal,
        license_key=license_key,
        account_info=account_info,
        background_tasks=background_tasks,
    )

    execution_time = (time.time() - start_time) * 1000

    if db_manager:
        background_tasks.add_task(
            log_trade_background,
            db_manager,
            _build_trade_data(signal, client_ip, execution_time, result),
        )

    return _build_response(signal, execution_time, result)


@router.post("/webhook")
async def pinetunnel_webhook(
    request: PineTunnelRequest, req: Request, background_tasks: BackgroundTasks
):
    """PineTunnel webhook endpoint (JSON body).

    Format: ``LicenseID,Command,Symbol,Parameters``
    Examples: ``123456789,buy,EURUSD,risk=0.01,sl=50,tp=100``
    """
    client_ip = get_trusted_client_ip(req)
    return await _handle_pinetunnel_signal(
        request.message, client_ip, req, background_tasks, key_override=request.key
    )


@router.post("/webhook/simple")
async def pinetunnel_webhook_simple(req: Request, background_tasks: BackgroundTasks):
    """PineTunnel webhook endpoint - plain text / form data body.

    Accepts raw PineTunnel syntax directly (no JSON wrapper):
    ``8417008713941,buy,EURUSD,risk=2,sl=1.08000,tp=1.09000``
    """
    client_ip = get_trusted_client_ip(req)

    try:
        body = await req.body()
        message = body.decode("utf-8").strip()

        if "=" in message and "message=" in message:
            parsed = parse_qs(message)
            if "message" in parsed:
                message = parsed["message"][0]

        logger.info("Plain text webhook received: %s", message)

    except Exception as e:
        logger.error("Failed to read request body: %s", e)
        raise HTTPException(status_code=400, detail="Could not read request body")

    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    return await _handle_pinetunnel_signal(message, client_ip, req, background_tasks)


@router.get("/test")
async def test_pinetunnel():
    """Test endpoint to verify PineTunnel parser with real syntax."""
    if not get_config().debug:
        raise HTTPException(status_code=404, detail="Not found")

    parser = _parser

    test_cases = [
        # Market orders
        ("123456789,buy,EURUSD,risk=0.01,sl=50,tp=100", "Market Buy"),
        ("123456789,sell,GBPUSD,risk=2,sl=30", "Market Sell"),
        ("123456789,long,USDJPY,risk=1", "Buy (alias)"),
        ("123456789,short,AUDUSD,risk=0.5", "Sell (alias)"),
        # Pending orders
        ("123456789,buy_stop,EURUSD,pending=1.1050,risk=1,sl=50", "Buy Stop"),
        ("123456789,sell_limit,GBPUSD,pending=1.2950,risk=0.5", "Sell Limit"),
        # Close commands
        ("123456789,close_all,EURUSD", "Close All"),
        ("123456789,close_long,EURUSD", "Close Long"),
        ("123456789,close_short,GBPUSD", "Close Short"),
        # Partial close
        ("123456789,close_long_pct,EURUSD,risk=50", "Close 50% Long"),
        ("123456789,close_short_vol,GBPUSD,risk=0.05", "Close 0.05 lot Short"),
        # Modify SL/TP
        ("123456789,sltp_long,EURUSD,sl=1.0950,tp=1.1150", "Modify Long SL/TP"),
        # Breakeven
        ("123456789,buy,EURUSD,risk=1,be_trigger=20,be_offset=5", "Buy with Breakeven"),
        # Trailing stop
        (
            "123456789,buy,EURUSD,risk=1,trail_trigger=15,trail_distance=10,trail_step=5",
            "Buy with Trailing",
        ),
    ]

    results = []
    for test_message, description in test_cases:
        try:
            signal = parser.parse(test_message)
            if signal:
                results.append(
                    {
                        "description": description,
                        "input": test_message,
                        "command": signal.command.value,
                        "symbol": signal.symbol,
                        "risk": signal.risk,
                        "sl": signal.sl,
                        "tp": signal.tp,
                        "status": "OK",
                    }
                )
            else:
                results.append(
                    {
                        "description": description,
                        "input": test_message,
                        "status": "Failed",
                    }
                )
        except Exception as e:
            logger.error("Test '%s' failed: %s", description, e)
            results.append(
                {
                    "description": description,
                    "input": test_message,
                    "error": "Test failed",
                    "status": "Error",
                }
            )

    return {
        "status": "ok",
        "parser_version": "2.0.0",
        "format": "LicenseID,Command,Symbol,Parameters",
        "test_results": results,
        "summary": f"{len([r for r in results if r['status'] == 'OK'])}/{len(test_cases)} passed",
    }

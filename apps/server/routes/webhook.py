"""Webhook endpoints - root and /webhook."""

import asyncio
import hmac
import json
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from apps.server.config.settings import get_config
from apps.server.middleware.main import failed_attempt_tracker
from apps.server.utils import mask_string as _mask_key
from apps.server.utils.security import get_trusted_client_ip, verify_webhook_signature
from apps.server.utils.symbol import normalize_symbol
from apps.server.webhook.parser import PineTunnelParser
from apps.server.webhook.pipeline import deliver_signal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class TradeAction(BaseModel):
    """Enhanced webhook payload model"""

    key: str = Field(..., description="Security key")
    action: Literal["buy", "sell", "close", "close_all"] = Field(..., description="Trade action")
    symbol: str = Field(..., description="Trading symbol")
    volume: float | None = Field(None, gt=0, le=100, description="Position size")
    sl: float | None = Field(None, description="Stop loss price")
    tp: float | None = Field(None, description="Take profit price")
    sl_points: int | None = Field(None, description="Stop loss in points")
    tp_points: int | None = Field(None, description="Take profit in points")
    risk_percent: float | None = Field(None, gt=0, le=10, description="Risk % for sizing")
    secret: str = Field(..., description="Secret key for signal authentication")
    comment: str | None = Field("TradingView", max_length=31)
    magic: int | None = Field(None, description="Magic number")
    nm: bool | None = Field(None, description="Near-Market flag: enables limit order conversion")

    @validator("symbol")
    def validate_symbol(cls, v):
        return v.upper().strip()


# ---------------------------------------------------------------------------
# Helper: HMAC verification
# ---------------------------------------------------------------------------


async def _verify_webhook_hmac(request: Request, secret: str, label: str) -> None:
    """Verify HMAC-SHA256 signature on a webhook request.

    Delegates to the shared verify_webhook_signature in security.py.
    See that function for full documentation.
    """
    await verify_webhook_signature(request, secret, label)


# ---------------------------------------------------------------------------
# Background task: log alerts
# ---------------------------------------------------------------------------


async def log_alert_background(alert_data: dict):
    """Log alert to database"""
    from apps.server.state import db_manager

    try:
        db_manager.log_alert(alert_data)
    except Exception as e:
        logger.error("Failed to log alert: %s", e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/")
async def root_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Root webhook endpoint - accepts PineTunnel syntax and routes to EA clients

    This allows you to send alerts to your bridge server directly

    Accepts two formats:
    1. Plain text: "LicenseID,Command,Symbol,Parameters"
    2. JSON: {"message": "LicenseID,Command,Symbol,Parameters"}

    Example: 2097863951024,buy,EURUSD,risk=1,sl=50,tp=100
    """
    from apps.server.state import PINETUNNEL_AVAILABLE, client_manager

    if not PINETUNNEL_AVAILABLE:
        raise HTTPException(status_code=503, detail="PineTunnel integration not available")

    # Verify HMAC signature BEFORE any parsing
    cfg = get_config()
    await _verify_webhook_hmac(request, cfg.webhook_secret, "Root webhook")

    # Read raw body for plain-text path (HMAC helper reads it internally too,
    # but Starlette caches the body so this is safe)
    raw_body = await request.body()

    # Parse body - safe now that auth has passed
    try:
        content_type = request.headers.get("content-type", "")
        message = None

        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict) and "message" in body:
                message = body["message"]
            else:
                raise HTTPException(
                    status_code=400,
                    detail='Invalid JSON format. Expected: {"message": "LicenseID,Command,Symbol,Parameters"}',
                )
        else:
            # Plain text format (TradingView default)
            message = raw_body.decode("utf-8").strip()

        if not message:
            raise HTTPException(
                status_code=400,
                detail="Empty message. Format: LicenseID,Command,Symbol,Parameters",
            )

        # Check if message is RC4-encrypted (auto-detect)
        from apps.server.crypto.signal_crypto import (
            is_encrypted_message,
            is_encryption_configured,
            parse_rc4_payload,
            decrypt_rc4_signal,
        )
        if is_encrypted_message(message):
            if not is_encryption_configured():
                raise HTTPException(
                    status_code=403,
                    detail="Encrypted signal received but no encryption key configured on server",
                )
            # Parse: RC4,<nonce_hex>:<ct_hex>:<checksum_hex>
            parts = message.split(",", 1)
            payload = parts[1].strip()
            parsed = parse_rc4_payload(payload)
            if parsed is None:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid RC4 encrypted format. Expected: RC4,<nonce_hex>:<ct_hex>:<checksum_hex>",
                )
            nonce_hex, ct_hex, checksum_hex = parsed
            plaintext = decrypt_rc4_signal(nonce_hex, ct_hex, checksum_hex)
            if plaintext is None:
                raise HTTPException(
                    status_code=403,
                    detail="Signal decryption or authentication failed",
                )
            logger.info("RC4 signal decrypted successfully")
            message = plaintext

        # Parse using PineTunnelParser for full validation
        # (conflicting params, required SL for dollar risk, breakeven checks, etc.)
        parser = PineTunnelParser()
        signal = parser.parse(message)

        if signal is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid PineTunnel syntax. Expected: LicenseID,Command,Symbol[,Parameters]",
            )

        license_key = signal.license_id
        command = signal.command.value
        symbol_normalized = normalize_symbol(signal.symbol)

        # Enforce secret= parameter
        if not signal.secret:
            raise HTTPException(
                status_code=403,
                detail="Secret key required - include secret= in your alert",
            )

        logger.info("Root webhook: %s,%s,%s", _mask_key(license_key), command, symbol_normalized)

        # Validate license (checks existence, status, expiry)
        valid, msg = client_manager.validate_license(license_key)
        if not valid:
            raise HTTPException(status_code=403, detail=msg)

        # Server-side secret key validation (mandatory for all licenses)
        client = client_manager.get_client_by_license(license_key)
        if client:
            expected_secret = client.get("secret_key", "")
            if not expected_secret:
                raise HTTPException(
                    status_code=403,
                    detail="License has no secret key configured - contact admin",
                )
            if not hmac.compare_digest(signal.secret, expected_secret):
                logger.warning(
                    "Secret key mismatch for %s: provided=%s expected=%s (masked)",
                    _mask_key(license_key),
                    signal.secret[:2] + "***",
                    expected_secret[:2] + "***",
                )
                raise HTTPException(
                    status_code=403,
                    detail="Invalid secret key",
                )

        # Build signal_data using parser's to_dict() for correct type mapping
        signal_data = signal.to_dict()
        signal_data["type"] = "trade_signal"
        signal_data["timestamp"] = datetime.now().isoformat()

        response = await deliver_signal(
            license_key=license_key,
            signal_data=signal_data,
            command=command,
            symbol=symbol_normalized,
        )

        return response

    except HTTPException:
        raise


@router.post("/webhook")
async def webhook_handler(
    action: TradeAction,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Main webhook endpoint - routes signals to connected EA clients"""
    from apps.server.state import client_manager

    client_ip = get_trusted_client_ip(request)

    # Verify HMAC signature - fail-closed in production
    await _verify_webhook_hmac(request, get_config().webhook_secret, "Webhook")

    # Structured logging for webhook
    logger.info(
        "webhook_received: ip=%s, action=%s, symbol=%s, license=%s, volume=%s",
        client_ip,
        action.action,
        action.symbol,
        _mask_key(action.key),
        action.volume,
    )

    # Silent admin logging - log webhook (client doesn't know)
    webhook_message = f"{action.key},{action.action},{action.symbol}"
    from apps.server.state import admin_logger

    admin_logger.log_webhook(action.key, webhook_message, client_ip)

    # Verify API key and get client
    is_valid, client_id, error_msg = client_manager.validate_api_key(action.key)

    if not is_valid:
        logger.warning(
            "invalid_api_key: ip=%s, error=%s, key=%s",
            client_ip,
            error_msg,
            _mask_key(action.key),
        )

        if failed_attempt_tracker:
            await failed_attempt_tracker.record_failure(client_ip)

        background_tasks.add_task(
            log_alert_background,
            {
                "ip_address": client_ip,
                "action": action.action,
                "symbol": action.symbol,
                "response_code": 401,
                "response_message": error_msg,
            },
        )

        raise HTTPException(status_code=401, detail=error_msg)

    # Normalize symbol: uppercase base, lowercase extension
    symbol_normalized = normalize_symbol(action.symbol)

    license_key = action.key

    signal_data = {
        "type": "trade_signal",
        "action": action.action,
        "symbol": symbol_normalized,
        "volume": action.volume,
        "sl": action.sl,
        "tp": action.tp,
        "sl_points": action.sl_points,
        "tp_points": action.tp_points,
        "risk_percent": action.risk_percent,
        "secret": action.secret,
        "comment": action.comment,
        "magic": action.magic,
        "nm": "true" if action.nm else None,
        "timestamp": datetime.now().isoformat(),
    }

    # Remove None values so EA only sees explicitly set fields
    signal_data = {k: v for k, v in signal_data.items() if v is not None}

    return await deliver_signal(
        license_key=license_key,
        signal_data=signal_data,
        command=action.action,
        symbol=symbol_normalized,
    )

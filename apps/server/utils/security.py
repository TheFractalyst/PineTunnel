"""PineTunnel Security Module.

HMAC-SHA256 webhook verification, constant-time comparisons,
API key generation, trusted IP extraction, replay protection,
and response signing for EA signal integrity.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional, Tuple

from apps.server.config.settings import get_config

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Number of trusted reverse proxies in front of this app.
# Default: 2 (Cloudflare + reverse proxy: Client -> Cloudflare -> Proxy -> App).
# Set to 1 for Render-only (no Cloudflare). Must be >= 1.
TRUSTED_PROXY_COUNT = max(1, int(os.environ.get("TRUSTED_PROXY_COUNT", "2")))

# HMAC signature constants
HMAC_SHA256_PREFIX = "sha256="

# Replay protection: maximum age of webhook timestamp in seconds
WEBHOOK_MAX_TIMESTAMP_SKEW = int(os.environ.get("WEBHOOK_MAX_TIMESTAMP_SKEW", "300"))  # 5 minutes

# Response signing: whether to sign EA signal responses with HMAC for integrity verification
# EA can verify X-PT-Response-Signature header to detect MITM signal tampering.
RESPONSE_SIGNING_ENABLED = os.environ.get("RESPONSE_SIGNING_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
)

# License key validation constants
MIN_LICENSE_KEY_LENGTH = 13
_ALNUM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_EXTENDED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

# Default IP when no client address can be determined
UNKNOWN_IP = "unknown"


def verify_hmac_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature of a request body.

    Used for webhook endpoints where the sender can attach
    custom headers (custom integrations, not TradingView alerts).

    Args:
        body: Raw request body bytes (do NOT parse before verifying).
        signature: Value from X-PT-Signature header (format: sha256=<hex>).
        secret: Shared webhook secret.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not secret or not signature:
        return False

    if signature.startswith(HMAC_SHA256_PREFIX):
        signature = signature[len(HMAC_SHA256_PREFIX) :]

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, signature)


def verify_secret_key(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison of secret keys.

    Prevents timing attacks that could leak information about
    the expected secret via response-time analysis.

    Args:
        provided: Secret key from the request (may be None).
        expected: Expected secret key from license config (may be None).

    Returns:
        True if secrets match, False otherwise.
    """
    if provided is None or expected is None:
        return False

    return hmac.compare_digest(str(provided), str(expected))


def validate_license_key_format(key: str) -> Tuple[bool, str]:
    """Validate license key format.

    Accepts:
    - 13+ character alphanumeric strings (PineTunnel-style)
    - Prefixed keys like pt_live_xxx (new format)

    Args:
        key: License key to validate.

    Returns:
        (is_valid, reason) tuple.
    """
    if not key:
        return False, "License key is required"

    key = key.strip()

    if len(key) < MIN_LICENSE_KEY_LENGTH:
        return False, f"License key must be at least {MIN_LICENSE_KEY_LENGTH} characters"

    if key.startswith("pt_"):
        if not _EXTENDED_CHARS.issuperset(key):
            return False, "License key contains invalid characters"
        return True, "Valid"

    if not _ALNUM_CHARS.issuperset(key):
        return False, "License key contains invalid characters"

    return True, "Valid"


async def verify_webhook_signature(request: Request, secret: str, label: str = "webhook") -> None:
    """Verify HMAC signature on an incoming webhook request.

    Shared implementation for all webhook endpoints. Reads the request body,
    checks the X-PT-Signature header against the shared secret, and raises
    HTTPException on failure.

    **TradingView compatibility**: TradingView webhooks cannot send custom
    HTTP headers, so they POST JSON without X-PT-Signature. When that header
    is absent, HMAC verification is skipped and authentication falls through
    to body-level `secret=` parameter validation (enforced in route handlers).

    **Non-TradingView integrations** (custom scripts, etc.) SHOULD send
    X-PT-Signature for stronger authentication. When present, the signature
    is verified and replay protection is applied via X-PT-Timestamp.

    Replay protection: if the X-PT-Timestamp header is present (Unix epoch
    seconds), the request is rejected if the timestamp is older than
    WEBHOOK_MAX_TIMESTAMP_SKEW seconds (default 300s / 5 minutes). This
    prevents captured webhooks from being replayed indefinitely.

    Args:
        request: FastAPI request (body read exactly once).
        secret: Webhook secret from config. If empty/None in production, raises 503.
        label: Human-readable label for log messages (e.g. "PineTunnel", "raw webhook").

    Raises:
        HTTPException: 401 if signature is invalid or timestamp is stale, 503 if secret is not configured.
    """
    if not secret:
        config = get_config()
        if config.environment == "production":
            raise HTTPException(status_code=503, detail=f"{label} secret not configured")
        logger.debug("[%s] No webhook secret configured - skipping signature verification", label)
        return

    signature = request.headers.get("X-PT-Signature", "")
    if not signature:
        # TradingView webhooks cannot send custom headers.
        # Skip HMAC verification; auth falls through to body secret= param.
        logger.debug("[%s] No X-PT-Signature header - HMAC skipped (TradingView path)", label)
        return

    # Replay protection: validate timestamp if present
    ts_header = request.headers.get("X-PT-Timestamp", "")
    if ts_header:
        try:
            timestamp = int(ts_header)
            now = int(time.time())
            if abs(now - timestamp) > WEBHOOK_MAX_TIMESTAMP_SKEW:
                logger.warning(
                    "[%s] Rejected stale webhook signature (ts=%d, now=%d, skew=%ds)",
                    label,
                    timestamp,
                    now,
                    abs(now - timestamp),
                )
                raise HTTPException(status_code=401, detail=f"Stale {label} signature")
        except ValueError:
            raise HTTPException(status_code=401, detail=f"Invalid {label} timestamp")

    body = await request.body()
    if not verify_hmac_signature(body, signature, secret):
        raise HTTPException(status_code=401, detail=f"Invalid {label} signature")


def get_trusted_client_ip(request: Request) -> str:
    """Extract client IP with trusted proxy validation.

    Prefers CF-Connecting-IP header set by Cloudflare when present.
    Falls back to X-Forwarded-For with trusted proxy counting.

    Args:
        request: FastAPI/Starlette request object.

    Returns:
        Client IP address string.
    """
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",")]
        if len(parts) > TRUSTED_PROXY_COUNT:
            return parts[-(TRUSTED_PROXY_COUNT + 1)]
        return parts[0]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client:
        return request.client.host

    return UNKNOWN_IP


def sign_response(data: dict, secret: str) -> str:
    """Sign a response payload with HMAC-SHA256 for EA integrity verification.

    The EA can verify the X-PT-Response-Signature header to confirm that
    signal data was not modified in transit (MITM protection).

    The signature covers the canonical JSON of the response body (sorted keys,
    no whitespace) plus a timestamp to prevent replay. Format::

        sha256=<hex_hmac>:<unix_timestamp>

    Args:
        data: Response payload dict to sign.
        secret: The webhook secret (same as used for inbound webhook verification).

    Returns:
        HMAC signature string in ``sha256=<hex>:<timestamp>`` format.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    ts = str(int(time.time()))
    message = f"{canonical}:{ts}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"sha256={sig}:{ts}"


def verify_response_signature(data: dict, signature: str, secret: str) -> bool:
    """Verify a response signature (EA-side use).

    Args:
        data: The parsed response payload.
        signature: The X-PT-Response-Signature header value.
        secret: The shared webhook secret.

    Returns:
        True if signature is valid and timestamp is within skew window.
    """
    if not signature or not secret:
        return False

    # Parse: sha256=<hex>:<timestamp>
    try:
        if not signature.startswith(HMAC_SHA256_PREFIX):
            return False
        payload = signature[len(HMAC_SHA256_PREFIX) :]
        hex_sig, ts_str = payload.rsplit(":", 1)
        timestamp = int(ts_str)
    except (ValueError, IndexError):
        return False

    # Check timestamp within 5-minute window
    if abs(time.time() - timestamp) > WEBHOOK_MAX_TIMESTAMP_SKEW:
        return False

    # Verify HMAC
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    message = f"{canonical}:{ts_str}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hex_sig)


def sign_response_bytes(body: bytes, secret: str) -> str:
    """Sign raw response body bytes with HMAC-SHA256 for EA header verification.

    Used for the X-PT-Response-Signature HTTP header. The EA can verify
    the raw body bytes directly without JSON re-canonicalization.

    Format: ``sha256=<hex_hmac>:<unix_timestamp>``

    Args:
        body: Raw response body bytes (as sent to client).
        secret: The webhook secret.

    Returns:
        HMAC signature string in ``sha256=<hex>:<timestamp>`` format.
    """
    ts = str(int(time.time()))
    message = body + b":" + ts.encode()
    sig = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={sig}:{ts}"


def verify_response_bytes_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify a response body signature (for testing).

    Args:
        body: Raw response body bytes.
        signature: The X-PT-Response-Signature header value.
        secret: The shared webhook secret.

    Returns:
        True if signature is valid and timestamp is within skew window.
    """
    if not signature or not secret:
        return False

    try:
        if not signature.startswith(HMAC_SHA256_PREFIX):
            return False
        payload = signature[len(HMAC_SHA256_PREFIX) :]
        hex_sig, ts_str = payload.rsplit(":", 1)
        timestamp = int(ts_str)
    except (ValueError, IndexError):
        return False

    if abs(time.time() - timestamp) > WEBHOOK_MAX_TIMESTAMP_SKEW:
        return False

    message = body + b":" + ts_str.encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hex_sig)

"""Security middleware - security headers and failed attempt tracking."""

import logging
import time
from collections import defaultdict
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from apps.server.config.logging_config import log_security_event
from apps.server.middleware.request_validation import _ResponseRecorder

logger = logging.getLogger(__name__)

# --- Constants ---

_BLOCK_DURATION_SECS = 3600
_FAILED_ATTEMPT_THRESHOLD = 10
_MAX_FAILED_ATTEMPT_ENTRIES = 50_000  # cap for FailedAttemptTracker


class FailedAttemptTracker:
    """Track failed license attempts and block abusive IPs.

    When a Redis client is provided via ``set_redis()``, blocking state
    is shared across workers.  Otherwise falls back to in-process dicts
    (single-worker deployments only).
    """

    def __init__(self) -> None:
        self.attempts: dict[str, list[float]] = defaultdict(list)
        self.blocked_ips: dict[str, float] = {}
        self._redis: Any = None  # aioredis.Redis, set via set_redis()

    def cleanup(self) -> None:
        """Prune expired entries and enforce size caps (called periodically)."""
        now = time.time()
        # Remove expired blocks
        expired = [ip for ip, until in self.blocked_ips.items() if now >= until]
        for ip in expired:
            del self.blocked_ips[ip]
            self.attempts.pop(ip, None)
        # Prune stale attempt timestamps
        stale = [
            ip
            for ip, timestamps in self.attempts.items()
            if not timestamps or now - timestamps[-1] > _BLOCK_DURATION_SECS
        ]
        for ip in stale:
            del self.attempts[ip]
        # Evict oldest entries if over cap
        if len(self.attempts) > _MAX_FAILED_ATTEMPT_ENTRIES:
            excess = len(self.attempts) - _MAX_FAILED_ATTEMPT_ENTRIES
            for ip in list(self.attempts.keys())[:excess]:
                del self.attempts[ip]
                self.blocked_ips.pop(ip, None)

    def set_redis(self, redis_client: Any) -> None:
        """Inject a Redis async client for multi-worker blocking state."""
        self._redis = redis_client

    async def record_failure(self, ip: str) -> None:
        """Record a failed attempt and block IP if threshold is reached."""
        if self._redis is not None:
            try:
                key = f"pt:failed_attempts:{ip}"
                now = time.time()
                await self._redis.zadd(key, {str(now): now})
                await self._redis.zremrangebyscore(key, 0, now - _BLOCK_DURATION_SECS)
                await self._redis.expire(key, _BLOCK_DURATION_SECS)
                count = await self._redis.zcard(key)
                if count >= _FAILED_ATTEMPT_THRESHOLD:
                    block_key = f"pt:blocked_ip:{ip}"
                    await self._redis.setex(block_key, _BLOCK_DURATION_SECS, "1")
                    log_security_event(
                        event_type="ip_blocked",
                        description=f"IP {ip} blocked due to excessive failed attempts (Redis)",
                        severity="warning",
                        source_ip=ip,
                    )
                return
            except Exception:
                logger.warning("Redis record_failure failed for %s - using local", ip)

        # In-memory fallback
        now = time.time()
        self.attempts[ip].append(now)
        self.attempts[ip] = [t for t in self.attempts[ip] if now - t < _BLOCK_DURATION_SECS]
        if len(self.attempts[ip]) >= _FAILED_ATTEMPT_THRESHOLD:
            self.blocked_ips[ip] = now + _BLOCK_DURATION_SECS
            log_security_event(
                event_type="ip_blocked",
                description=f"IP {ip} blocked due to excessive failed attempts",
                severity="warning",
                source_ip=ip,
            )
        # Enforce cap on insertion to prevent unbounded growth
        if len(self.attempts) > _MAX_FAILED_ATTEMPT_ENTRIES:
            stale_ips = [
                k
                for k, ts in self.attempts.items()
                if not ts or now - ts[-1] > _BLOCK_DURATION_SECS
            ]
            for k in stale_ips[: len(self.attempts) - _MAX_FAILED_ATTEMPT_ENTRIES + 1]:
                del self.attempts[k]
                self.blocked_ips.pop(k, None)

    async def is_blocked(self, ip: str) -> bool:
        """Check if an IP is currently blocked."""
        if self._redis is not None:
            try:
                block_key = f"pt:blocked_ip:{ip}"
                return await self._redis.exists(block_key) > 0
            except Exception:
                logger.warning("Redis is_blocked check failed for %s - using local", ip)

        # In-memory fallback
        if ip not in self.blocked_ips:
            return False
        if time.time() < self.blocked_ips[ip]:
            return True
        del self.blocked_ips[ip]
        return False

    def reset(self, ip: str) -> None:
        """Reset failure count for an IP (sync - local only)."""
        self.attempts.pop(ip, None)

    async def reset_async(self, ip: str) -> None:
        """Reset failure count for an IP across all workers."""
        if self._redis is not None:
            try:
                key = f"pt:failed_attempts:{ip}"
                block_key = f"pt:blocked_ip:{ip}"
                await self._redis.delete(key, block_key)
                return
            except Exception as e:
                logger.debug("Redis reset_async failed for %s: %s", ip, e)
        self.attempts.pop(ip, None)


failed_attempt_tracker = FailedAttemptTracker()


class SecurityHeadersMiddleware:
    """Add security headers to all responses - pure ASGI (no BaseHTTPMiddleware)."""

    # Pre-built security headers as (lowercased-bytes, bytes) tuples.
    # Avoids per-request string encoding and dict creation.
    _SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"x-xss-protection", b"1; mode=block"),
        # HSTS aligned with Cloudflare (max-age=15552000 ~ 6 months)
        (b"strict-transport-security", b"max-age=15552000; includeSubDomains; preload"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none';",
        ),
    ]
    # Headers to replace (security headers + server header) - pre-computed for fast filtering
    _REPLACE_KEYS: frozenset[bytes] = frozenset(k for k, _ in _SECURITY_HEADERS) | {
        b"server",
        b"x-render-origin-server",
        b"rndr-id",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        recorder = _ResponseRecorder()
        await self.app(scope, receive, recorder)

        # Filter out headers we replace, then append pre-built security headers in one pass
        replace_keys = self._REPLACE_KEYS
        recorder.headers = [
            (k, v) for k, v in recorder.headers if k.lower() not in replace_keys
        ] + self._SECURITY_HEADERS
        await recorder.replay(send)

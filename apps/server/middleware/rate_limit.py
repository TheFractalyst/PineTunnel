"""Rate limiting middleware - per-IP and per-endpoint rate limiting with Redis support."""

import logging
import time
from collections import deque
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from apps.server.config.logging_config import get_correlation_id, log_security_event
from apps.server.config.settings import get_config
from apps.server.middleware.request_validation import _get_client_ip_from_scope, _send_json
from apps.server.middleware.security import failed_attempt_tracker

logger = logging.getLogger(__name__)

# --- Constants ---

_HTTP_RATE_LIMIT_WINDOW_SECS = 60
_DEFAULT_ENDPOINT_RATE_LIMIT = 60
_MAX_IN_MEMORY_ENTRIES = 50_000  # cap for in-memory fallback dicts (matches RateLimiter)
_HTTP_429 = 429
_RETRY_AFTER_60_SECS = 60
_RETRY_AFTER_BLOCK_SECS = 3600


class RateLimitMiddleware:
    """Per-IP rate limiting with per-endpoint buckets - pure ASGI.

    Supports optional Redis-backed rate limiting for multi-worker deployments.
    Set ``_redis_limiter`` to a RedisRateLimiter instance (done during lifespan)
    to enable distributed rate limiting.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._requests: dict[str, deque[float]] = {}
        self._endpoint_requests: dict[tuple[str, str], deque[float]] = {}
        self._service_rate_limiter: Any = None
        self._redis_limiter: Any = None  # Set during lifespan wiring
        self._general_limit: int = 0
        self._webhook_limit: int = 0
        self._signals_limit: int = 0
        self._limits_cached: bool = False

    def _ensure_limits(self) -> None:
        """Cache rate-limit values on first call to avoid per-request config lookups."""
        if not self._limits_cached:
            cfg = get_config()
            self._general_limit = cfg.rate_limit.requests_per_minute
            self._webhook_limit = cfg.rate_limit.webhook_requests_per_minute
            self._signals_limit = cfg.rate_limit.requests_per_minute
            self._limits_cached = True

    def cleanup(self) -> None:
        """Prune stale in-memory rate-limit entries and enforce size caps."""
        now = time.time()
        cutoff = now - _HTTP_RATE_LIMIT_WINDOW_SECS
        # Prune general-rate entries
        idle_ips = [ip for ip, ts in self._requests.items() if not ts or ts[-1] < cutoff]
        for ip in idle_ips:
            del self._requests[ip]
        if len(self._requests) > _MAX_IN_MEMORY_ENTRIES:
            excess = len(self._requests) - _MAX_IN_MEMORY_ENTRIES
            for ip in list(self._requests.keys())[:excess]:
                del self._requests[ip]
        # Prune endpoint-rate entries
        idle_keys = [k for k, ts in self._endpoint_requests.items() if not ts or ts[-1] < cutoff]
        for k in idle_keys:
            del self._endpoint_requests[k]
        if len(self._endpoint_requests) > _MAX_IN_MEMORY_ENTRIES:
            excess = len(self._endpoint_requests) - _MAX_IN_MEMORY_ENTRIES
            for k in list(self._endpoint_requests.keys())[:excess]:
                del self._endpoint_requests[k]

    @property
    def _rate_limiter_service(self) -> Any:
        if self._service_rate_limiter is None:
            try:
                from apps.server.state import rate_limiter

                self._service_rate_limiter = rate_limiter
            except Exception as e:
                logger.debug("Failed to get rate_limiter service: %s", e)
        return self._service_rate_limiter

    def _record_service_stats(self, *keys: str) -> None:
        """Record multiple stats in a single lock acquisition."""
        rl = self._rate_limiter_service
        if not rl:
            return
        try:
            with rl.lock:
                for key in keys:
                    rl.stats[key] = rl.stats.get(key, 0) + 1
        except Exception as e:
            logger.debug("Failed to record service stats: %s", e)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = _get_client_ip_from_scope(scope)
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if path.startswith("/health") or path.startswith("/api/health"):
            await self.app(scope, receive, send)
            return

        if path.startswith("/api/signals"):
            if not await self._check_endpoint_rate_async(client_ip, "signals"):
                log_security_event(
                    event_type="rate_limit_exceeded",
                    description=f"Signal endpoint rate limit exceeded for {client_ip}",
                    severity="warning",
                    source_ip=client_ip,
                )
                self._record_service_stats(
                    "total_requests", "blocked_requests", "rate_limited_requests"
                )
                await _send_json(
                    send,
                    _HTTP_429,
                    {
                        "detail": "Signal polling rate limit exceeded",
                        "retry_after": _RETRY_AFTER_60_SECS,
                        "correlation_id": get_correlation_id(),
                    },
                )
                return
            self._record_service_stats("total_requests", "passed_requests")
            await self.app(scope, receive, send)
            return

        is_webhook = ((path == "/webhook" or path == "/") and method == "POST") or path.startswith(
            "/pinetunnel/webhook"
        )
        if is_webhook:
            if not await self._check_endpoint_rate_async(client_ip, "webhook"):
                log_security_event(
                    event_type="rate_limit_exceeded",
                    description=f"Webhook rate limit exceeded for {client_ip}",
                    severity="warning",
                    source_ip=client_ip,
                )
                self._record_service_stats(
                    "total_requests", "blocked_requests", "rate_limited_requests"
                )
                await _send_json(
                    send,
                    _HTTP_429,
                    {
                        "detail": "Webhook rate limit exceeded",
                        "retry_after": _RETRY_AFTER_60_SECS,
                        "correlation_id": get_correlation_id(),
                    },
                )
                return
            self._record_service_stats("total_requests", "passed_requests")
            await self.app(scope, receive, send)
            return

        if await failed_attempt_tracker.is_blocked(client_ip):
            log_security_event(
                event_type="blocked_ip_request",
                description=f"Blocked IP {client_ip} attempted request",
                severity="warning",
                source_ip=client_ip,
            )
            self._record_service_stats("total_requests", "blocked_requests")
            await _send_json(
                send,
                _HTTP_429,
                {
                    "detail": "Too many failed attempts. Try again later.",
                    "retry_after": _RETRY_AFTER_BLOCK_SECS,
                    "correlation_id": get_correlation_id(),
                },
            )
            return

        if not await self._check_general_rate_async(client_ip):
            log_security_event(
                event_type="rate_limit_exceeded",
                description=f"General rate limit exceeded for {client_ip}",
                severity="warning",
                source_ip=client_ip,
            )
            self._record_service_stats(
                "total_requests", "blocked_requests", "rate_limited_requests"
            )
            await _send_json(
                send,
                _HTTP_429,
                {
                    "detail": "Rate limit exceeded",
                    "retry_after": _RETRY_AFTER_60_SECS,
                    "correlation_id": get_correlation_id(),
                },
            )
            return

        self._record_service_stats("total_requests", "passed_requests")
        await self.app(scope, receive, send)

    def _check_general_rate(self, client_ip: str) -> bool:
        """Check if client IP is within the general rate limit (in-memory fallback)."""
        now = time.time()
        self._ensure_limits()
        limit = self._general_limit
        timestamps = self._requests.get(client_ip)
        if timestamps is None:
            timestamps = deque()
            self._requests[client_ip] = timestamps

        cutoff = now - _HTTP_RATE_LIMIT_WINDOW_SECS
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= limit:
            return False

        timestamps.append(now)
        # Enforce cap on insertion to prevent unbounded growth
        if len(self._requests) > _MAX_IN_MEMORY_ENTRIES:
            idle_ips = [ip for ip, ts in self._requests.items() if not ts or ts[-1] < cutoff]
            for ip in idle_ips[: len(self._requests) - _MAX_IN_MEMORY_ENTRIES + 1]:
                del self._requests[ip]
        return True

    async def _check_general_rate_async(self, client_ip: str) -> bool:
        """Check general rate limit - uses Redis when available."""
        if self._redis_limiter is not None:
            self._ensure_limits()
            allowed, _ = await self._redis_limiter.check_rate(
                f"general:{client_ip}",
                self._general_limit,
                _HTTP_RATE_LIMIT_WINDOW_SECS,
            )
            return allowed
        return self._check_general_rate(client_ip)

    def _get_endpoint_limit(self, bucket: str) -> int:
        """Get rate limit for an endpoint bucket."""
        self._ensure_limits()
        if bucket == "webhook":
            return self._webhook_limit
        if bucket == "signals":
            return self._signals_limit
        return _DEFAULT_ENDPOINT_RATE_LIMIT

    def _check_endpoint_rate(self, client_ip: str, bucket: str) -> bool:
        """Check if client IP is within the per-endpoint rate limit (in-memory fallback)."""
        now = time.time()
        limit = self._get_endpoint_limit(bucket)
        key = (client_ip, bucket)

        timestamps = self._endpoint_requests.get(key)
        if timestamps is None:
            timestamps = deque()
            self._endpoint_requests[key] = timestamps

        cutoff = now - _HTTP_RATE_LIMIT_WINDOW_SECS
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= limit:
            return False

        timestamps.append(now)
        # Enforce cap on insertion to prevent unbounded growth
        if len(self._endpoint_requests) > _MAX_IN_MEMORY_ENTRIES:
            idle_keys = [
                k for k, ts in self._endpoint_requests.items() if not ts or ts[-1] < cutoff
            ]
            for k in idle_keys[: len(self._endpoint_requests) - _MAX_IN_MEMORY_ENTRIES + 1]:
                del self._endpoint_requests[k]
        return True

    async def _check_endpoint_rate_async(self, client_ip: str, bucket: str) -> bool:
        """Check endpoint rate limit - uses Redis when available."""
        if self._redis_limiter is not None:
            limit = self._get_endpoint_limit(bucket)
            allowed, _ = await self._redis_limiter.check_rate(
                f"{bucket}:{client_ip}", limit, _HTTP_RATE_LIMIT_WINDOW_SECS
            )
            return allowed
        return self._check_endpoint_rate(client_ip, bucket)


# Module-level reference to the rate limit middleware instance
_rate_limit_middleware: RateLimitMiddleware | None = None


def set_redis_rate_limiter(redis_limiter: Any) -> None:
    """Inject a Redis rate limiter into the middleware chain.

    Called during application lifespan when Redis is available.
    """
    if _rate_limit_middleware is not None:
        _rate_limit_middleware._redis_limiter = redis_limiter

"""Rate limiting module — sliding-window and token-bucket strategies to prevent abuse."""

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

_VIOLATION_BLOCK_THRESHOLD = 20
_VIOLATION_DECAY_INTERVAL = 300  # 5 minutes per decayed violation
_MAX_ENTRIES = 50_000  # prevent unbounded growth


class RateLimiter:
    """Advanced rate limiting with sliding-window and token-bucket strategies."""

    def __init__(
        self,
        max_requests_per_minute: int = 10000,
        max_requests_per_hour: int = 500000,
        max_burst_size: int = 500,
        enable_ip_blocking: bool = True,
        block_duration_minutes: int = 5,
    ) -> None:
        self.max_requests_per_minute = max_requests_per_minute
        self.max_requests_per_hour = max_requests_per_hour
        self.max_burst_size = max_burst_size
        self.enable_ip_blocking = enable_ip_blocking
        self.block_duration = block_duration_minutes * 60

        self.minute_requests: dict[str, deque] = defaultdict(deque)
        self.hour_requests: dict[str, deque] = defaultdict(deque)
        self.token_buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tokens": max_burst_size, "last_refill": time.time()}
        )
        self.blocked_ips: dict[str, float] = {}
        self.violation_counts: dict[str, int] = defaultdict(int)
        self._last_good_time: dict[str, float] = {}

        self.lock = threading.Lock()
        self._cleanup_timer: threading.Timer | None = None

        self.stats: dict[str, int] = {
            "total_requests": 0,
            "blocked_requests": 0,
            "rate_limited_requests": 0,
            "passed_requests": 0,
        }

        logger.info(
            "Rate limiter initialized: %d/min, %d/hour, burst=%d",
            max_requests_per_minute,
            max_requests_per_hour,
            max_burst_size,
        )

    def is_allowed(
        self, identifier: str, cost: float = 1.0
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """Check if a request is allowed based on rate-limiting strategies.

        Returns:
            Tuple of (allowed, reason, metrics).
        """
        with self.lock:
            self.stats["total_requests"] += 1
            now = time.time()

            if self._is_blocked(identifier, now):
                self.stats["blocked_requests"] += 1
                remaining_block_time = self.blocked_ips[identifier] - now
                return (
                    False,
                    f"IP blocked for {int(remaining_block_time)} seconds",
                    {"blocked": True},
                )

            window_check, window_reason, window_metrics = self._check_sliding_window(
                identifier, now
            )
            if not window_check:
                self._handle_violation(identifier, now)
                self.stats["rate_limited_requests"] += 1
                return False, window_reason, window_metrics

            bucket_check, bucket_reason, bucket_metrics = self._check_token_bucket(
                identifier, cost, now
            )
            if not bucket_check:
                self._handle_violation(identifier, now)
                self.stats["rate_limited_requests"] += 1
                return False, bucket_reason, bucket_metrics

            # Request allowed — decay violations for good behavior
            self.stats["passed_requests"] += 1
            last_good = self._last_good_time.get(identifier, now)
            if identifier in self.violation_counts:
                elapsed = now - last_good
                decays = int(elapsed / _VIOLATION_DECAY_INTERVAL)
                if decays > 0 and self.violation_counts[identifier] > 0:
                    self.violation_counts[identifier] = max(
                        0, self.violation_counts[identifier] - decays
                    )
            self._last_good_time[identifier] = now

            metrics = {
                **window_metrics,
                **bucket_metrics,
                "violations": self.violation_counts[identifier],
            }
            return True, "Allowed", metrics

    def _is_blocked(self, identifier: str, now: float) -> bool:
        """Check if identifier is currently blocked."""
        if identifier in self.blocked_ips:
            if now < self.blocked_ips[identifier]:
                return True
            # Block expired
            del self.blocked_ips[identifier]
            self.violation_counts[identifier] = 0
            logger.info("Block expired for %s", identifier)
        return False

    def _check_sliding_window(
        self, identifier: str, current_time: float
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """Check sliding-window rate limits.

        Returns:
            Tuple of (allowed, reason, metrics).
        """
        # Clean old requests from minute window
        minute_ago = current_time - 60
        while self.minute_requests[identifier] and self.minute_requests[identifier][0] < minute_ago:
            self.minute_requests[identifier].popleft()

        # Clean old requests from hour window
        hour_ago = current_time - 3600
        while self.hour_requests[identifier] and self.hour_requests[identifier][0] < hour_ago:
            self.hour_requests[identifier].popleft()

        # Check minute limit
        minute_count = len(self.minute_requests[identifier])
        if minute_count >= self.max_requests_per_minute:
            return (
                False,
                f"Minute limit exceeded ({minute_count}/{self.max_requests_per_minute})",
                {
                    "minute_count": minute_count,
                    "minute_limit": self.max_requests_per_minute,
                    "hour_count": len(self.hour_requests[identifier]),
                    "hour_limit": self.max_requests_per_hour,
                },
            )

        # Check hour limit
        hour_count = len(self.hour_requests[identifier])
        if hour_count >= self.max_requests_per_hour:
            return (
                False,
                f"Hour limit exceeded ({hour_count}/{self.max_requests_per_hour})",
                {
                    "minute_count": minute_count,
                    "minute_limit": self.max_requests_per_minute,
                    "hour_count": hour_count,
                    "hour_limit": self.max_requests_per_hour,
                },
            )

        # Add current request
        self.minute_requests[identifier].append(current_time)
        self.hour_requests[identifier].append(current_time)

        return (
            True,
            None,
            {
                "minute_count": minute_count + 1,
                "minute_limit": self.max_requests_per_minute,
                "hour_count": hour_count + 1,
                "hour_limit": self.max_requests_per_hour,
            },
        )

    def _check_token_bucket(
        self, identifier: str, cost: float, current_time: float
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """Check token-bucket rate limit.

        Returns:
            Tuple of (allowed, reason, metrics).
        """
        bucket = self.token_buckets[identifier]

        # Refill tokens based on time passed
        time_passed = current_time - bucket["last_refill"]
        tokens_to_add = time_passed * (self.max_requests_per_minute / 60)
        bucket["tokens"] = min(self.max_burst_size, bucket["tokens"] + tokens_to_add)
        bucket["last_refill"] = current_time

        if bucket["tokens"] < cost:
            return (
                False,
                f"Insufficient tokens ({bucket['tokens']:.1f} < {cost})",
                {
                    "tokens_available": bucket["tokens"],
                    "tokens_required": cost,
                    "max_burst": self.max_burst_size,
                },
            )

        bucket["tokens"] -= cost
        return (
            True,
            None,
            {
                "tokens_available": bucket["tokens"],
                "tokens_consumed": cost,
                "max_burst": self.max_burst_size,
            },
        )

    def _handle_violation(self, identifier: str, now: float) -> None:
        """Handle rate-limit violation by incrementing count and optionally blocking."""
        self.violation_counts[identifier] += 1
        violations = self.violation_counts[identifier]

        if self.enable_ip_blocking and violations >= _VIOLATION_BLOCK_THRESHOLD:
            block_until = now + self.block_duration
            self.blocked_ips[identifier] = block_until
            logger.warning(
                "Blocked %s for %d seconds after %d violations",
                identifier,
                self.block_duration,
                violations,
            )

    def reset_identifier(self, identifier: str) -> None:
        """Reset rate limits for a specific identifier."""
        with self.lock:
            self.minute_requests[identifier].clear()
            self.hour_requests[identifier].clear()
            self.token_buckets[identifier] = {
                "tokens": self.max_burst_size,
                "last_refill": time.time(),
            }
            self.blocked_ips.pop(identifier, None)
            self.violation_counts[identifier] = 0
            logger.info("Rate limits reset for %s", identifier)

    def unblock_identifier(self, identifier: str) -> None:
        """Manually unblock an identifier."""
        with self.lock:
            if identifier in self.blocked_ips:
                del self.blocked_ips[identifier]
                logger.info("Manually unblocked %s", identifier)

    def get_statistics(self) -> dict[str, Any]:
        """Return rate-limiter statistics snapshot."""
        with self.lock:
            total = self.stats["total_requests"]
            return {
                "total_requests": total,
                "blocked_requests": self.stats["blocked_requests"],
                "rate_limited_requests": self.stats["rate_limited_requests"],
                "passed_requests": self.stats["passed_requests"],
                "blocked_ips": len(self.blocked_ips),
                "active_identifiers": len(self.minute_requests),
                "pass_rate": (self.stats["passed_requests"] / total * 100) if total > 0 else 100,
            }

    def start_auto_cleanup(self, interval: int = 120) -> None:
        """Schedule periodic cleanup every *interval* seconds."""

        def _run() -> None:
            self.cleanup()
            self._cleanup_timer = threading.Timer(interval, _run)
            self._cleanup_timer.daemon = True
            self._cleanup_timer.start()

        self._cleanup_timer = threading.Timer(interval, _run)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def stop_auto_cleanup(self) -> None:
        """Cancel the auto-cleanup timer."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None

    def cleanup(self) -> None:
        """Clean up old data and enforce max entry cap."""
        with self.lock:
            current_time = time.time()
            minute_ago = current_time - 60
            hour_ago = current_time - 3600

            # Clean up minute requests
            idle_minute: list[str] = []
            for identifier, requests in self.minute_requests.items():
                while requests and requests[0] < minute_ago:
                    requests.popleft()
                if not requests:
                    idle_minute.append(identifier)
            for identifier in idle_minute:
                del self.minute_requests[identifier]

            # Clean up hour requests
            idle_hour: list[str] = []
            for identifier, requests in self.hour_requests.items():
                while requests and requests[0] < hour_ago:
                    requests.popleft()
                if not requests:
                    idle_hour.append(identifier)
            for identifier in idle_hour:
                del self.hour_requests[identifier]

            # Clean up expired blocks
            expired_blocks = [
                ident for ident, until in self.blocked_ips.items() if current_time >= until
            ]
            for identifier in expired_blocks:
                del self.blocked_ips[identifier]
                self.violation_counts.pop(identifier, None)

            if idle_minute or expired_blocks:
                logger.info(
                    "Cleanup: removed %d inactive identifiers, %d expired blocks",
                    len(idle_minute),
                    len(expired_blocks),
                )

            # Evict oldest entries if over cap
            for store in (self.minute_requests, self.hour_requests):
                store_len = len(store)
                if store_len > _MAX_ENTRIES:
                    excess = store_len - _MAX_ENTRIES
                    for k in list(store.keys())[:excess]:
                        del store[k]

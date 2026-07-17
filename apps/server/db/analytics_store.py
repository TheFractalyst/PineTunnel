"""Redis-backed shared state, trade DB manager accessor, and warm-up functions."""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# --- Constants ---
_MAX_TRADE_REPORTS = 10_000
_MARGIN_WARNING_THRESHOLD = 150  # margin level percentage
_EQUITY_DROP_THRESHOLD = 0.90  # equity/balance ratio
_STATS_ALERT_COOLDOWN = 300  # seconds (5 minutes)


class RedisBackedDict(dict):
    """Dict subclass that replicates writes to Redis for multi-worker consistency.

    Local reads are synchronous (fast). Writes update the local dict immediately
    and fire-and-forget to Redis. On startup, ``warm_from_redis()`` populates the
    local dict from Redis (or falls back to the DB).
    """

    def __init__(self, redis_key_prefix: str, ttl: int = 0) -> None:
        super().__init__()
        self._redis_key_prefix = redis_key_prefix
        self._redis: Any = None  # Set via set_redis_client()
        self._ttl = ttl  # 0 = no expiry

    def set_redis_client(self, client: Any) -> None:
        """Inject the Redis async client (called during lifespan startup)."""
        self._redis = client

    def _redis_key(self, sub_key: str = "") -> str:
        return f"{self._redis_key_prefix}:{sub_key}" if sub_key else self._redis_key_prefix

    def __setitem__(self, key: str, value: Any) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        self._replicate_set(key, value)

    def __delitem__(self, key: str) -> None:  # type: ignore[override]
        super().__delitem__(key)
        self._replicate_delete(key)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        result = super().pop(key, *args)
        self._replicate_delete(key)
        return result

    def update(self, __m: Any = None, **kwargs: Any) -> None:  # type: ignore[override]
        if __m:
            super().update(__m, **kwargs)
            if hasattr(__m, "items"):
                for k, v in __m.items():
                    self._replicate_set(k, v)
        else:
            super().update(**kwargs)
            for k, v in kwargs.items():
                self._replicate_set(k, v)

    # --- Redis replication (fire-and-forget) ---

    def _replicate_set(self, key: str, value: Any) -> None:
        if not self._redis:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_set(key, value))
        except RuntimeError:
            pass

    def _replicate_delete(self, key: str) -> None:
        if not self._redis:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_delete(key))
        except RuntimeError:
            pass

    async def _async_set(self, key: str, value: Any) -> None:
        try:
            serialized = json.dumps(value, default=str)
            key_name = self._redis_key(key)
            if self._ttl:
                await self._redis.setex(key_name, self._ttl, serialized)
            else:
                await self._redis.set(key_name, serialized)
            # Also maintain a set of all known keys for enumeration
            await self._redis.sadd(self._redis_key_prefix, key)
        except Exception as e:
            logger.debug("Redis replicate set failed: %s", e)

    async def _async_delete(self, key: str) -> None:
        try:
            await self._redis.delete(self._redis_key(key))
            await self._redis.srem(self._redis_key_prefix, key)
        except Exception as e:
            logger.debug("Redis replicate delete failed: %s", e)

    # --- Warm-up on startup ---

    async def warm_from_redis(self) -> int:
        """Load all entries from Redis into the local dict. Returns count loaded."""
        if not self._redis:
            return 0
        try:
            members = await self._redis.smembers(self._redis_key_prefix)
            count = 0
            for key in members:
                key_str = key if isinstance(key, str) else key.decode()
                raw = await self._redis.get(self._redis_key(key_str))
                if raw:
                    try:
                        value = json.loads(raw)
                        super().__setitem__(key_str, value)
                        count += 1
                    except (json.JSONDecodeError, TypeError):
                        pass
            return count
        except Exception as e:
            logger.warning("Redis warm-from failed for %s: %s", self._redis_key_prefix, e)
            return 0


def get_trade_db_manager() -> Any | None:
    """Lazy-load trade DB manager — returns the app's PostgresDatabaseManager."""
    try:
        from apps.server.state import db_manager

        return db_manager
    except ImportError:
        return None


# --- Shared state caches (Redis-backed when available, in-memory fallback) ---
trade_reports: deque = deque(maxlen=_MAX_TRADE_REPORTS)
license_stats: RedisBackedDict = RedisBackedDict("trade_analytics:license_stats", ttl=0)
account_stats_latest: RedisBackedDict = RedisBackedDict("trade_analytics:account_stats", ttl=0)

_stats_alert_cooldowns: dict[str, float] = {}  # "license_key:alert_type" -> last_alert_time
_MAX_STATS_COOLDOWN_ENTRIES = 10_000  # prevent unbounded growth


def _prune_stats_alert_cooldowns() -> None:
    """Remove stale cooldown entries and enforce cap."""
    global _stats_alert_cooldowns
    now = time.time()
    _stats_alert_cooldowns = {
        k: v for k, v in _stats_alert_cooldowns.items() if now - v < _STATS_ALERT_COOLDOWN * 2
    }
    if len(_stats_alert_cooldowns) > _MAX_STATS_COOLDOWN_ENTRIES:
        # Evict oldest entries
        sorted_keys = sorted(_stats_alert_cooldowns, key=_stats_alert_cooldowns.get)
        for k in sorted_keys[: len(_stats_alert_cooldowns) - _MAX_STATS_COOLDOWN_ENTRIES]:
            del _stats_alert_cooldowns[k]


def get_stats_for_license(license_key: str) -> dict | None:
    """Get stats from in-memory cache, falling back to PostgreSQL."""
    snap = account_stats_latest.get(license_key)
    if snap:
        return snap
    trade_db_manager = get_trade_db_manager()
    if trade_db_manager:
        try:
            rows = trade_db_manager.get_latest_account_stats(license_key)
            if rows:
                return rows[0]
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
    return None


def set_redis_client(redis_client: Any) -> None:
    """Inject Redis client for multi-worker state replication (called during lifespan)."""
    account_stats_latest.set_redis_client(redis_client)
    license_stats.set_redis_client(redis_client)


def warm_account_stats() -> int:
    """Populate in-memory stats from latest PostgreSQL rows on startup."""
    trade_db_manager = get_trade_db_manager()
    if not trade_db_manager:
        return 0
    try:
        rows = trade_db_manager.get_latest_account_stats()
        if not rows:
            return 0
        for row in rows:
            key = row.get("license_key")
            if key and key not in account_stats_latest:
                account_stats_latest[key] = {k: v for k, v in row.items() if k not in ("id",)}
                account_stats_latest[key]["received_at"] = row.get("timestamp", "")
        warmed = len([r for r in rows if r.get("license_key")])
        if warmed:
            logger.info("Warmed account stats from DB: %s licenses", warmed)
        return warmed
    except Exception as e:
        logger.warning("Failed to warm account stats: %s", e)
        return 0

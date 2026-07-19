"""Redis-backed connection manager for multi-worker deployments.

When Redis is available, connection state and signal notifications are shared
across all workers so that any Uvicorn worker can serve any EA client.

Falls back gracefully to the in-process ConnectionManager when Redis is
unavailable.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis

from apps.server.routes.metrics import record_redis_op
from apps.server.utils import mask_string as _mask
from apps.server.ws.connection import HTTP_POLLING_TIMEOUT, ConnectionManager

logger = logging.getLogger(__name__)

_KEY_PREFIX = "pt:conn"
_POLL_TTL = 60  # seconds - how long a poll registration lives in Redis
_NOTIFY_PREFIX = "pt:notify"
_WS_KEY_PREFIX = "pt:conn:ws"
_WS_TTL = 130  # seconds - must exceed WS idle timeout (120s) so heartbeats refresh before expiry


class RedisConnectionManager(ConnectionManager):
    """Connection manager that shares state via Redis.

    Inherit from ConnectionManager so that callers can use the same interface.
    When Redis operations fail, we fall back to the in-process dicts.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        super().__init__()
        self._redis = redis_client
        self._subscriber: aioredis.client.PubSub | None = None
        self._listen_task: asyncio.Task | None = None
        logger.info("RedisConnectionManager initialised")

    # ------------------------------------------------------------------
    # HTTP polling client tracking (Redis hash per license)
    # ------------------------------------------------------------------

    async def register_poll(
        self, license_key: str, client_info: dict[str, Any] | None = None
    ) -> None:
        """Record that an EA just polled for this license."""
        info = client_info or {}
        data: dict[str, Any] = {
            "last_poll": time.time(),
            "client_info": info,
        }
        key = f"{_KEY_PREFIX}:poll:{license_key}"
        try:
            await self._redis.hset(
                key,
                mapping={
                    k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in data.items()
                },
            )
            await self._redis.expire(key, _POLL_TTL)
            record_redis_op("hset", "ok")
            # Also keep local dict in sync for fast reads within the same worker
            self.http_polling_clients[license_key] = {
                "last_poll": datetime.fromtimestamp(data["last_poll"]),
                "client_info": info,
            }
        except Exception:
            record_redis_op("hset", "error")
            logger.warning("Redis register_poll failed for %s - using local", _mask(license_key))
            # Fallback already handled by super().__init__()

    async def deregister_poll(self, license_key: str) -> None:
        """Remove poll registration for a license."""
        key = f"{_KEY_PREFIX}:poll:{license_key}"
        try:
            await self._redis.delete(key)
            record_redis_op("delete", "ok")
        except Exception:
            record_redis_op("delete", "error")
            logger.debug("Redis deregister_poll failed for %s", _mask(license_key))
        self.http_polling_clients.pop(license_key, None)

    async def get_active_http_clients_async(self) -> list[str]:
        """Return license keys that polled within the timeout window (Redis-backed)."""
        now = time.time()
        cutoff = now - HTTP_POLLING_TIMEOUT
        active: list[str] = []

        try:
            # Scan all poll registrations (limit iterations to prevent infinite loop under load)
            pattern = f"{_KEY_PREFIX}:poll:*"
            cursor = 0
            iterations = 0
            max_iterations = 500  # safety cap: 500 * 200 = 100K keys max
            while True:
                iterations += 1
                if iterations > max_iterations:
                    logger.warning(
                        "Redis SCAN hit iteration limit (%d) - returning partial results",
                        max_iterations,
                    )
                    break
                cursor, keys = await self._redis.scan(cursor, match=pattern, count=200)
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode()
                    license_key = key.rsplit(":", 1)[-1]
                    last_poll_raw = await self._redis.hget(key, "last_poll")
                    record_redis_op("hget", "ok" if last_poll_raw else "miss")
                    if last_poll_raw:
                        try:
                            last_poll = float(last_poll_raw)
                            if last_poll >= cutoff:
                                active.append(license_key)
                            else:
                                await self._redis.delete(key)
                                record_redis_op("delete", "ok")
                        except (ValueError, TypeError):
                            pass
                if cursor == 0:
                    break
        except Exception:
            record_redis_op("scan", "error")
            logger.warning("Redis get_active_clients failed - using local fallback")
            return super().get_active_http_clients()

        return active

    # ------------------------------------------------------------------
    # Override the sync get_active_http_clients for non-async callers
    # ------------------------------------------------------------------

    def get_active_http_clients(self) -> list[str]:
        """Sync fallback - uses local in-memory dict."""
        return super().get_active_http_clients()

    # ------------------------------------------------------------------
    # WebSocket connection tracking (global across workers)
    # ------------------------------------------------------------------

    async def register_ws_connection(self, license_key: str, conn_id: str) -> None:
        """Register a WebSocket connection globally so other workers can see it."""
        key = f"{_WS_KEY_PREFIX}:{license_key}:{conn_id}"
        try:
            await self._redis.setex(key, _WS_TTL, "1")
            record_redis_op("setex", "ok")
        except Exception:
            record_redis_op("setex", "error")
            logger.debug("Redis register_ws failed for %s", _mask(license_key))

    async def deregister_ws_connection(self, license_key: str, conn_id: str) -> None:
        """Remove a WebSocket connection registration."""
        key = f"{_WS_KEY_PREFIX}:{license_key}:{conn_id}"
        try:
            await self._redis.delete(key)
            record_redis_op("delete", "ok")
        except Exception:
            record_redis_op("delete", "error")
            logger.debug("Redis deregister_ws failed for %s", _mask(license_key))

    async def refresh_ws_heartbeat(self, license_key: str, conn_id: str) -> None:
        """Refresh TTL on a WS connection key (called on EA heartbeat)."""
        key = f"{_WS_KEY_PREFIX}:{license_key}:{conn_id}"
        try:
            await self._redis.expire(key, _WS_TTL)
            record_redis_op("expire", "ok")
        except Exception:
            record_redis_op("expire", "error")
            logger.debug("Redis refresh_ws_heartbeat failed for %s", _mask(license_key))

    async def get_global_ws_count_async(self) -> int:
        """Count all active WS connections across all workers via Redis."""
        try:
            pattern = f"{_WS_KEY_PREFIX}:*"
            cursor = 0
            total = 0
            iterations = 0
            while True:
                iterations += 1
                if iterations > 500:
                    break
                cursor, keys = await self._redis.scan(cursor, match=pattern, count=200)
                total += len(keys)
                if cursor == 0:
                    break
            return total
        except Exception:
            logger.debug("Redis get_global_ws_count failed")
            return 0

    async def get_global_connection_count_async(self) -> int:
        """Total active connections (HTTP polling + WebSocket) across all workers."""
        http_count = len(await self.get_active_http_clients_async())
        ws_count = await self.get_global_ws_count_async()
        return http_count + ws_count

    # ------------------------------------------------------------------
    # Signal push notification via Redis pub/sub
    # ------------------------------------------------------------------

    async def publish_signal(self, license_key: str, signal_data: dict | None = None) -> None:
        """Publish a signal notification to all workers via Redis."""
        channel = f"{_NOTIFY_PREFIX}:{license_key}"
        payload = json.dumps(signal_data or {"type": "new_signal"})
        try:
            await self._redis.publish(channel, payload)
            record_redis_op("publish", "ok")
        except Exception:
            record_redis_op("publish", "error")
            logger.warning("Redis publish failed for %s - queuing locally", _mask(license_key))

        # Also push to local asyncio.Queue so this worker's EAs get it immediately
        self.notify_signal_queue(license_key, signal_data)

    async def subscribe_to_signals(self, license_key: str) -> None:
        """Subscribe to signal notifications for a license key on this worker."""
        if self._subscriber is None:
            self._subscriber = self._redis.pubsub()
            self._listen_task = asyncio.create_task(self._listen_for_signals())

        channel = f"{_NOTIFY_PREFIX}:{license_key}"
        try:
            await self._subscriber.subscribe(channel)
            record_redis_op("subscribe", "ok")
        except Exception:
            record_redis_op("subscribe", "error")
            logger.warning("Redis subscribe failed for %s", _mask(license_key))

    async def unsubscribe_from_signals(self, license_key: str) -> None:
        """Unsubscribe from signal notifications for a license key."""
        if self._subscriber is None:
            return

        channel = f"{_NOTIFY_PREFIX}:{license_key}"
        try:
            await self._subscriber.unsubscribe(channel)
            record_redis_op("unsubscribe", "ok")
        except Exception:
            record_redis_op("unsubscribe", "error")
            logger.debug("Redis unsubscribe failed for %s", _mask(license_key))

    async def _listen_for_signals(self) -> None:
        """Background task that receives Redis pub/sub messages and dispatches to local queues."""
        if self._subscriber is None:
            return

        try:
            async for message in self._subscriber.listen():
                if message["type"] != "message":
                    continue
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                license_key = channel.rsplit(":", 1)[-1]
                try:
                    data = json.loads(message["data"])
                    self.notify_signal_queue(license_key, data)
                except (json.JSONDecodeError, TypeError):
                    self.notify_signal_queue(license_key)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Redis signal listener error: %s", e)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Clean up Redis connections and background tasks."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._subscriber:
            try:
                await self._subscriber.unsubscribe()
                await self._subscriber.aclose()
            except Exception:
                logger.debug("Redis subscriber cleanup failed")
        # No need to close self._redis - it's the shared client managed by lifespan

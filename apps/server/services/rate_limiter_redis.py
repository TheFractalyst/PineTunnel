"""Redis-backed distributed rate limiter using sliding window algorithm."""

import logging
import time

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisRateLimiter:
    """Distributed rate limiter using Redis sorted sets."""

    def __init__(self, redis_client: redis.Redis):
        """Initialize with a shared Redis async client.

        Args:
            redis_client: An existing ``redis.asyncio.Redis`` instance.
        """
        self.redis = redis_client
        logger.info("Redis rate limiter initialized")

    async def check_rate(self, key: str, limit: int, window: int = 60) -> tuple[bool, int]:
        """Check if request is within rate limit using sliding window."""
        now = time.time()
        redis_key = f"ratelimit:{key}"

        await self.redis.zremrangebyscore(redis_key, 0, now - window)
        count = await self.redis.zcard(redis_key)

        if count >= limit:
            return (False, 0)

        await self.redis.zadd(redis_key, {str(now): now})
        await self.redis.expire(redis_key, window)
        return (True, limit - count - 1)

    async def block_ip(self, ip: str, duration: int = 3600) -> None:
        """Block IP for duration seconds."""
        await self.redis.setex(f"blocked:{ip}", duration, "1")

    async def is_blocked(self, ip: str) -> bool:
        """Check if IP is blocked."""
        return await self.redis.exists(f"blocked:{ip}") > 0

    async def close(self) -> None:
        """No-op — the Redis client is shared and closed centrally."""
        pass


def create_rate_limiter(redis_client=None):
    """Factory to create Redis or in-memory rate limiter."""
    if redis_client:
        return RedisRateLimiter(redis_client)

    # Fallback to existing in-memory rate limiter
    from apps.server.services.rate_limiter import RateLimiter

    return RateLimiter()

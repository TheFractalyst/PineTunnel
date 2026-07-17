"""Redis-backed session store for distributed sessions across workers."""

import logging
from datetime import datetime, timedelta

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisSessionStore:
    """Distributed session store using Redis."""

    def __init__(self, redis_client: redis.Redis, key_prefix: str = "session:"):
        """Initialize with a shared Redis async client.

        Args:
            redis_client: An existing ``redis.asyncio.Redis`` instance. Pass
                ``None`` to disable Redis and use the in-memory fallback instead.
            key_prefix: Redis key namespace prefix.
        """
        self.redis = redis_client
        self.key_prefix = key_prefix
        logger.info("Redis session store initialized")

    def _make_key(self, session_token: str) -> str:
        return f"{self.key_prefix}{session_token}"

    async def create_session(
        self, session_token: str, username: str, expires_in: int = 3600
    ) -> None:
        """Create a new session."""
        key = self._make_key(session_token)
        await self.redis.setex(key, expires_in, username)

    async def verify_session(self, session_token: str) -> tuple[bool, str]:
        """Verify session and return (is_valid, username)."""
        key = self._make_key(session_token)
        username = await self.redis.get(key)
        return (username is not None, username or "")

    async def delete_session(self, session_token: str) -> None:
        """Delete a session."""
        key = self._make_key(session_token)
        await self.redis.delete(key)

    async def extend_session(self, session_token: str, expires_in: int = 3600) -> bool:
        """Extend session expiration."""
        key = self._make_key(session_token)
        return await self.redis.expire(key, expires_in) > 0

    async def close(self) -> None:
        """No-op — the Redis client is shared and closed centrally."""
        pass


class InMemorySessionStore:
    """Fallback in-memory session store for development."""

    _MAX_SESSIONS = 10_000  # prevent unbounded growth

    def __init__(self):
        self.sessions: dict[str, tuple[str, datetime]] = {}
        logger.warning("Using in-memory session store (not suitable for multi-worker)")

    def cleanup(self) -> None:
        """Prune expired sessions and enforce cap."""
        now = datetime.now()
        expired = [k for k, (_, exp) in self.sessions.items() if now > exp]
        for k in expired:
            del self.sessions[k]
        if len(self.sessions) > self._MAX_SESSIONS:
            # Evict oldest by expiry time
            sorted_keys = sorted(self.sessions, key=lambda k: self.sessions[k][1])
            for k in sorted_keys[: len(self.sessions) - self._MAX_SESSIONS]:
                del self.sessions[k]

    async def create_session(
        self, session_token: str, username: str, expires_in: int = 3600
    ) -> None:
        expires_at = datetime.now() + timedelta(seconds=expires_in)
        self.sessions[session_token] = (username, expires_at)

    async def verify_session(self, session_token: str) -> tuple[bool, str]:
        if session_token not in self.sessions:
            return (False, "")

        username, expires_at = self.sessions[session_token]
        if datetime.now() > expires_at:
            del self.sessions[session_token]
            return (False, "")

        return (True, username)

    async def delete_session(self, session_token: str) -> None:
        self.sessions.pop(session_token, None)

    async def extend_session(self, session_token: str, expires_in: int = 3600) -> bool:
        if session_token in self.sessions:
            username, _ = self.sessions[session_token]
            expires_at = datetime.now() + timedelta(seconds=expires_in)
            self.sessions[session_token] = (username, expires_at)
            return True
        return False

    async def close(self) -> None:
        pass


def create_session_store(redis_client=None):
    """Factory to create appropriate session store."""
    if redis_client:
        return RedisSessionStore(redis_client)
    return InMemorySessionStore()

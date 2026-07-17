"""Admin authentication and session management.

Supports session-based admin auth with proper session token
generation, verification, and expiry. Sessions can be backed by
Redis for multi-worker deployments or fall back to in-memory.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_EXPIRY_WARNING = (
    "No auth config file found. Admin login disabled until password is set via /api/auth/setup"
)

_DEFAULT_SESSION_TTL = 3600  # 1 hour


class AuthManager:
    """Manages admin authentication and session lifecycle.

    When a session_store is provided, sessions are delegated to it
    (e.g. RedisSessionStore for multi-worker). Otherwise falls back
    to the per-process in-memory dict.
    """

    def __init__(
        self,
        config_file: str | None = None,
        session_store: Any | None = None,
    ) -> None:
        self.config_file = config_file
        self.config: dict[str, Any] = {}
        self._session_store = session_store
        self.sessions: dict[str, dict[str, Any]] = {}  # Per-process fallback when no Redis

        if config_file and Path(config_file).exists():
            try:
                with open(config_file, "r") as f:
                    self.config = json.load(f)
                logger.info("Loaded auth config from %s", config_file)
            except Exception as e:
                logger.error("Failed to load auth config: %s", e)
                self._set_defaults()
        else:
            self._set_defaults()

    def _set_defaults(self) -> None:
        """Set default auth config when no file is found."""
        self.config = {"auth_enabled": True}
        logger.warning(_SESSION_EXPIRY_WARNING)

    async def create_session(self, username: str, ttl: int = _DEFAULT_SESSION_TTL) -> str:
        """Create a new session and return the token.

        Uses the session store (Redis) when available, otherwise
        stores in-memory.
        """
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now() + timedelta(seconds=ttl)).isoformat()

        if self._session_store is not None:
            await self._session_store.create_session(token, username, expires_in=ttl)
            return token

        self.sessions[token] = {"username": username, "expires_at": expires_at}
        return token

    async def verify_session(self, session_token: str) -> tuple[bool, str]:
        """Verify a session token and return (is_valid, username).

        Delegates to the session store when available, otherwise
        checks the legacy in-memory dict.
        """
        if not session_token:
            return False, ""

        if self._session_store is not None:
            return await self._session_store.verify_session(session_token)

        # Legacy in-memory path
        session = self.sessions.get(session_token)
        if not session:
            return False, ""

        try:
            expires_at = datetime.fromisoformat(session.get("expires_at", ""))
            if datetime.now() > expires_at:
                self.sessions.pop(session_token, None)
                logger.info("Session expired for %s", session.get("username", "unknown"))
                return False, ""
        except (ValueError, TypeError):
            self.sessions.pop(session_token, None)
            return False, ""

        return True, session.get("username", "admin")

    async def delete_session(self, session_token: str) -> None:
        """Delete a session."""
        if self._session_store is not None:
            await self._session_store.delete_session(session_token)
            return

        self.sessions.pop(session_token, None)

    async def close(self) -> None:
        """Close the session store connection."""
        if self._session_store is not None:
            await self._session_store.close()

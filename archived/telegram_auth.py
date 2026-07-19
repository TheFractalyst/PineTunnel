"""Telegram bot /login one-time code store."""

import asyncio
import secrets
import time


class TelegramAuthStore:
    """In-memory store for one-time login codes issued by the Telegram bot."""

    def __init__(self, ttl_seconds: int = 90) -> None:
        self._ttl = ttl_seconds
        self._codes: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def issue_code_async(self, user_id: int) -> str:
        async with self._lock:
            code = secrets.token_urlsafe(8)
            self._codes[code] = (user_id, time.monotonic() + self._ttl)
            return code

    def issue_code(self, user_id: int) -> str:
        code = secrets.token_urlsafe(8)
        self._codes[code] = (user_id, time.monotonic() + self._ttl)
        return code

    async def verify_code_async(self, code: str, expected_user_id: int) -> bool:
        async with self._lock:
            entry = self._codes.pop(code, None)
            if entry is None:
                return False
            uid, expires_at = entry
            if time.monotonic() > expires_at:
                return False
            return uid == expected_user_id

    def verify_code(self, code: str, expected_user_id: int) -> bool:
        entry = self._codes.pop(code, None)
        if entry is None:
            return False
        uid, expires_at = entry
        if time.monotonic() > expires_at:
            return False
        return uid == expected_user_id

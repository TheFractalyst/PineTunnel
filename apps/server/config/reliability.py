"""PineTunnel Reliability Monitor.

Background health monitoring, alerting, and EA connection tracking.

Runs as a single asyncio task inside the application process.
NOTE: telegram_bot.py already sends startup/shutdown alerts (lines 285/296).
This module handles only what the bot does NOT cover:
disk pressure, EA connection loss, downtime tracking.

Database backups are handled by the PostgreSQL provider (Render managed backups).
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
import psutil

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60
_DISK_CRITICAL_PCT = 10
_EA_DISCONNECT_THRESHOLD = 0  # 0 = disabled (was 5)
_ALERT_COOLDOWN_SECONDS = 300


class ReliabilityMonitor:
    """Background monitoring for disk pressure and EA connections."""

    def __init__(
        self,
        data_dir: str = "",
        client_manager: Any = None,
        telegram_bot: Any = None,
        conn_manager: Any = None,
        ws_manager: Any = None,
        notify_admin_fn: Callable[..., Any] | None = None,
        bot_token: str | None = None,
        chat_id: int | None = None,
        db_manager: Any = None,
    ) -> None:
        self._data_dir = data_dir or "/data" if os.path.isdir("/data") else os.getcwd()
        self._marker_path = Path(self._data_dir) / ".last_start"

        if conn_manager:
            self._conn_manager = conn_manager
        elif client_manager:
            self._conn_manager = client_manager
        else:
            self._conn_manager = None

        self._ws_manager = ws_manager
        self._db_manager = db_manager
        self._worker_id = os.getpid()

        # Telegram: prefer bot's notify_admin (sends to all admins), fall back to raw HTTP
        self._notify_admin_fn = notify_admin_fn
        if telegram_bot:
            self._bot_token = getattr(telegram_bot, "token", bot_token)
            self._chat_id: int | None = None
            admin_ids = getattr(telegram_bot, "admin_ids", None)
            if admin_ids:
                self._chat_id = admin_ids[0] if isinstance(admin_ids, list) else admin_ids
        else:
            self._bot_token = bot_token
            self._chat_id = chat_id

        self._task: asyncio.Task | None = None
        self._tick = 0
        self._ea_zero_count = 0
        self._last_alert_at: dict[str, float] = {}

    def __repr__(self) -> str:
        token = getattr(self, "_bot_token", None)
        masked = f"{token[:4]}..." if token and len(token) > 4 else "***"
        return f"ReliabilityMonitor(bot_token={masked})"

    async def start(self) -> None:
        """Start the background monitoring loop."""
        self._write_marker()
        await self._init_table()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the background monitoring task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            self._tick += 1
            await self._run_checks()
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _run_checks(self) -> None:
        self._check_disk()
        await self._check_ea_connections()

    def _check_disk(self) -> None:
        try:
            drive = os.path.splitdrive(self._data_dir)[0] or "/"
            usage = psutil.disk_usage(drive)
            free_pct = round(usage.free / usage.total * 100, 1)
            if free_pct < _DISK_CRITICAL_PCT:
                self._alert_sync(
                    "disk",
                    "*Disk Space Critical*\n"
                    f"Drive: `{drive}`\n"
                    f"Free: {free_pct}% ({usage.free // (1024 ** 3)}GB)\n"
                    f"Time: {self._now()}",
                )
        except Exception as e:
            logger.error("Disk check failed: %s", e)

    async def _init_table(self) -> None:
        """Create worker_connections table if it doesn't exist."""
        if not self._db_manager:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._db_manager.execute_query,
                """CREATE TABLE IF NOT EXISTS worker_connections (
                    worker_id INTEGER PRIMARY KEY,
                    connection_count INTEGER NOT NULL DEFAULT 0,
                    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""",
            )
        except Exception as e:
            logger.warning("Could not create worker_connections table: %s", e)

    async def _check_ea_connections(self) -> None:
        try:
            active_http = 0
            active_ws = 0

            if self._conn_manager is not None:
                active_http = len(self._conn_manager.get_active_http_clients())

            if self._ws_manager is not None:
                active_ws = self._ws_manager.get_total_connections()

            local_count = active_http + active_ws

            # Try Redis-backed global count first (most accurate in multi-worker)
            redis_global = 0
            if hasattr(self._conn_manager, "get_global_connection_count_async"):
                try:
                    redis_global = await self._conn_manager.get_global_connection_count_async()
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)

            global_count = max(redis_global, await self._get_global_count())

            logger.info(
                "EA check [pid=%d]: local_http=%d local_ws=%d local_total=%d "
                "redis_global=%d global_db=%d global_total=%d zero_count=%d threshold=%d",
                self._worker_id,
                active_http,
                active_ws,
                local_count,
                redis_global,
                global_count,
                global_count,
                self._ea_zero_count,
                _EA_DISCONNECT_THRESHOLD,
            )

            # If we have local connections, EA is active on this worker
            if local_count > 0:
                self._ea_zero_count = 0
                await self._write_worker_count(local_count)
                return

            # No local connections. Check if other workers have any.
            if global_count > 0:
                self._ea_zero_count = 0
                await self._write_worker_count(0)
                return

            # No connections anywhere. Only alert if threshold is enabled (> 0).
            self._ea_zero_count += 1
            if _EA_DISCONNECT_THRESHOLD > 0 and self._ea_zero_count >= _EA_DISCONNECT_THRESHOLD:
                self._alert_sync(
                    "ea_disconnect",
                    f"*No EA Connections*\n"
                    f"No active EA for {_EA_DISCONNECT_THRESHOLD} min\n"
                    f"Time: {self._now()}",
                )
                self._ea_zero_count = 0
        except Exception as e:
            logger.error("EA connection check failed: %s", e)

    async def _write_worker_count(self, count: int) -> None:
        """Write this worker's connection count to the shared DB table."""
        if not self._db_manager:
            return
        try:
            loop = asyncio.get_running_loop()
            now_expr = self._db_manager.sql_now()
            await loop.run_in_executor(
                None,
                self._db_manager.execute_query,
                f"""INSERT INTO worker_connections (worker_id, connection_count, last_seen)
                   VALUES (:wid, :cnt, {now_expr})
                   ON CONFLICT (worker_id) DO UPDATE SET
                       connection_count = :cnt,
                       last_seen = {now_expr}""",
                {"wid": self._worker_id, "cnt": count},
            )
        except Exception as e:
            logger.warning("worker_connections write failed: %s", e)

    async def _get_global_count(self) -> int:
        """Query the global EA connection count across all workers."""
        if not self._db_manager:
            return 0
        try:
            loop = asyncio.get_running_loop()
            three_min_ago = self._db_manager.sql_interval_minutes(3)
            rows = await loop.run_in_executor(
                None,
                self._db_manager.execute_query,
                f"""SELECT COALESCE(SUM(connection_count), 0) AS total
                   FROM worker_connections
                   WHERE last_seen > {three_min_ago}""",
            )
            return rows[0]["total"] if rows else 0
        except Exception as e:
            logger.warning("Global EA count query failed: %s", e)
            return 0

    def _write_marker(self) -> None:
        try:
            self._marker_path.parent.mkdir(parents=True, exist_ok=True)
            self._marker_path.write_text(datetime.now().isoformat())
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

    async def _alert(self, message: str) -> None:
        """Send an alert message via Telegram, with HTTP fallback."""
        if self._notify_admin_fn:
            try:
                await self._notify_admin_fn(message)
                return
            except Exception as e:
                logger.error("notify_admin failed, using HTTP fallback: %s", e)

        # Fallback: raw HTTP (only reaches first admin)
        if not self._bot_token or not self._chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.error("Telegram alert failed: %s", e)

    def _alert_sync(self, key: str, message: str) -> None:
        """Fire-and-forget alert with cooldown deduplication."""
        now = time.time()
        last = self._last_alert_at.get(key, 0)
        if now - last < _ALERT_COOLDOWN_SECONDS:
            return
        self._last_alert_at[key] = now
        try:
            asyncio.get_running_loop().create_task(self._alert(message))
        except RuntimeError:
            pass

    @staticmethod
    def _now() -> str:
        """Return current timestamp as a formatted string."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

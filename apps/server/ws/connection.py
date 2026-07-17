"""Connection manager for HTTP polling clients.

Centralizes connection state for HTTP polling clients and signal
notification queues, plus cleanup/management helpers.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from apps.server.utils import mask_string as _mask

logger = logging.getLogger(__name__)

HTTP_POLLING_TIMEOUT = 30


class ConnectionManager:
    """Manages HTTP polling connection state."""

    MAX_SIGNAL_QUEUES = 10000

    def __init__(self) -> None:
        self.http_polling_clients: dict[str, dict[str, Any]] = {}
        self.signal_queues: dict[str, asyncio.Queue] = {}

    def get_signal_queue(self, license_key: str) -> asyncio.Queue:
        """Get or create a signal queue for the given license key."""
        if license_key not in self.signal_queues:
            if len(self.signal_queues) >= self.MAX_SIGNAL_QUEUES:
                self.prune_idle_signal_queues()
            if len(self.signal_queues) >= self.MAX_SIGNAL_QUEUES:
                logger.warning("Signal queue limit (%d) reached", self.MAX_SIGNAL_QUEUES)
            self.signal_queues[license_key] = asyncio.Queue(maxsize=64)
        return self.signal_queues[license_key]

    def notify_signal_queue(self, license_key: str, signal_data: dict | None = None) -> None:
        """Push a signal to the in-process queue for immediate notification."""
        if license_key in self.signal_queues:
            try:
                self.signal_queues[license_key].put_nowait(signal_data or True)
            except asyncio.QueueFull:
                logger.warning(
                    "Signal queue full for %s (maxsize=64) - "
                    "push notification dropped, signal will be delivered on next poll via DB",
                    _mask(license_key),
                )

    def cleanup_client_state(self, license_key: str) -> None:
        """Remove all state for a disconnected client."""
        self.http_polling_clients.pop(license_key, None)
        self.signal_queues.pop(license_key, None)

    def get_active_http_clients(self) -> list[str]:
        """Return license keys of clients that polled within the timeout window."""
        now = datetime.now()
        active: list[str] = []
        stale: list[str] = []
        for license_key, poll_data in list(self.http_polling_clients.items()):
            last_poll = poll_data.get("last_poll")
            if last_poll:
                time_since = (now - last_poll).total_seconds()
                if time_since <= HTTP_POLLING_TIMEOUT:
                    active.append(license_key)
                else:
                    stale.append(license_key)
        for key in stale:
            self.http_polling_clients.pop(key, None)
        return active

    def prune_idle_signal_queues(self) -> list[str]:
        """Remove signal queues for clients that no longer have an active connection."""
        stale = [key for key in self.signal_queues if key not in self.http_polling_clients]
        for key in stale:
            del self.signal_queues[key]
        return stale

    def build_connections_response(self) -> dict[str, Any]:
        """Build the standard connections info dict for API responses."""
        connections_info: list[dict[str, Any]] = []
        active_http_clients = self.get_active_http_clients()

        for http_license in active_http_clients:
            poll_data = self.http_polling_clients.get(http_license, {})
            client_info = poll_data.get("client_info", {})
            connections_info.append(
                {
                    "license": http_license,
                    "name": client_info.get("name", "Unknown"),
                    "email": client_info.get("email", "Unknown"),
                    "connection_type": "HTTP Polling",
                    "connected": True,
                }
            )

        return {
            "http_polling_connections": len(active_http_clients),
            "licenses": connections_info,
        }

    def build_public_connections_response(self) -> dict[str, int]:
        """Build a public-safe connections info dict (no PII) for unauthenticated endpoints."""
        active_http_clients = self.get_active_http_clients()

        return {
            "http_polling_connections": len(active_http_clients),
        }

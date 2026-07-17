"""Database-backed persistent signal queue.

Delegates storage to db_manager and push notifications via notify_fn.
Uses a thread executor to run synchronous save_signal without blocking the event loop.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from apps.server.utils import mask_string

logger = logging.getLogger(__name__)

_mask = mask_string

_db_manager: Any = None
_notify_fn: Callable[[str, dict], Any] | None = None


def init_signal_queue(
    db_manager: Any, notify_signal_queue_fn: Callable[[str, dict], Any] | None = None
) -> None:
    global _db_manager, _notify_fn  # noqa: PLW0603
    _db_manager = db_manager
    _notify_fn = notify_signal_queue_fn


async def queue_signal_async(license_key: str, signal_data: dict[str, Any]) -> str | None:
    """Save a signal to the database and notify listeners.

    Runs save_signal in a thread executor to avoid blocking the event loop.
    """
    signal_data["queued_at"] = datetime.now().isoformat()

    # Run synchronous save_signal in a thread executor to keep the event loop responsive.
    loop = asyncio.get_running_loop()
    signal_id = await loop.run_in_executor(None, _db_manager.save_signal, license_key, signal_data)

    if signal_id:
        logger.info(
            "Queued signal %s for %s: %s %s",
            signal_id,
            _mask(license_key),
            signal_data.get("action"),
            signal_data.get("symbol"),
        )
        if _notify_fn:
            result = _notify_fn(license_key, signal_data)
            # Support async notify functions (Redis pub/sub)
            if asyncio.iscoroutine(result):
                await result
    else:
        logger.info(
            "Skipped DUPLICATE signal for %s: %s %s",
            _mask(license_key),
            signal_data.get("action"),
            signal_data.get("symbol"),
        )

    return signal_id  # type: ignore[no-any-return]


async def get_pending_signals_async(license_key: str) -> list[dict]:
    """Return all pending signals for a license key from the database."""
    return await _db_manager.get_pending_signals_async(license_key)  # type: ignore[no-any-return]


async def acknowledge_signal_async(license_key: str, signal_id: str) -> bool:
    """Mark a signal as acknowledged in the database (license-scoped)."""
    success = await _db_manager.acknowledge_signal_async(signal_id, license_key=license_key)  # type: ignore[no-any-return]
    if success:
        logger.info("Acknowledged signal (async) %s for %s", signal_id, _mask(license_key))
    return success


async def acknowledge_signals_batch_async(
    license_key: str, signal_ids: list[str]
) -> dict[str, Any]:
    """Acknowledge multiple signals in one database transaction."""
    result = await _db_manager.acknowledge_signals_batch_async(signal_ids, license_key)  # type: ignore[no-any-return]
    ack_count = len(result.get("acknowledged", []))
    fail_count = len(result.get("failed", []))
    logger.info("Batch ACK for %s: %d ok, %d failed", _mask(license_key), ack_count, fail_count)
    return result

"""PineTunnel core utilities — logging helpers, uptime formatting, etc."""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_MASK_LENGTH = 8
_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


@lru_cache(maxsize=128)
def mask_string(key: str) -> str:
    """Truncate key for safe logging. Shows first 8 chars + '...' if longer."""
    return f"{key[:_MASK_LENGTH]}..." if len(key) > _MASK_LENGTH else key


async def log_trade_background(db_manager: object, trade_data: dict) -> None:
    """Background task to log trade to database. Safe if db_manager is None."""
    try:
        if db_manager:
            db_manager.log_trade(trade_data)  # type: ignore[attr-defined]
    except Exception as e:
        logger.error("Failed to log trade: %s", e)


@lru_cache(maxsize=128)
def format_uptime(seconds: float) -> str:
    """Format uptime in human-readable format."""
    days = int(seconds // _SECONDS_PER_DAY)
    hours = int((seconds % _SECONDS_PER_DAY) // _SECONDS_PER_HOUR)
    minutes = int((seconds % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE)

    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

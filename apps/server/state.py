"""Shared application state - single place for module-level globals.

All route modules import state from here instead of referencing
globals scattered across app_factory. The lifespan function in
the main app module sets these values during startup.
"""

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol types for type-safe service access
# ---------------------------------------------------------------------------


class DatabaseManagerProto(Protocol):
    """Database manager interface."""

    def execute_query(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]: ...
    def init_database(self) -> None: ...
    def close(self) -> None: ...
    def save_signal(self, *args: Any, **kwargs: Any) -> str: ...
    def delete_signals_by_license(self, license_key: str) -> int: ...
    def log_trade(self, trade_data: dict) -> int: ...
    def get_signal_log_for_license(self, *args: Any, **kwargs: Any) -> list[dict]: ...
    @property
    def dialect(self) -> str: ...


class ClientManagerProto(Protocol):
    """Client manager interface."""

    def validate_license(self, key: str) -> tuple[bool, str]: ...
    def validate_api_key(self, key: str) -> tuple[bool, str | None, str]: ...
    def get_client_by_license(self, key: str) -> dict | None: ...
    def add_client(self, key: str, data: dict) -> bool: ...
    def update_client(self, key: str, **fields: Any) -> bool: ...
    def remove_client(self, key: str) -> bool: ...
    def extend_client(self, key: str, days: int) -> str | None: ...
    def set_status(self, key: str, status: str, enabled: bool | None = None) -> bool: ...
    def save_clients(self) -> bool: ...
    def is_symbol_allowed(self, *args: Any, **kwargs: Any) -> bool: ...
    @property
    def clients(self) -> dict[str, dict]: ...


class MT5ManagerProto(Protocol):
    """MT5 manager interface."""

    def initialize(self) -> bool: ...
    def get_account_info(self) -> dict | None: ...
    def execute_order(self, *args: Any, **kwargs: Any) -> dict: ...
    def close_positions(self, *args: Any, **kwargs: Any) -> dict: ...
    @property
    def initialized(self) -> bool: ...


class RateLimiterProto(Protocol):
    """Rate limiter interface."""

    def is_allowed(self, client_ip: str) -> tuple[bool, str, Any]: ...


class AuthManagerProto(Protocol):
    """Auth manager interface."""

    def verify_token(self, *args: Any, **kwargs: Any) -> bool: ...


class AdminLoggerProto(Protocol):
    """Admin logger interface."""

    def log_action(self, *args: Any, **kwargs: Any) -> None: ...


class TelegramBotProto(Protocol):
    """Telegram bot interface."""

    @property
    def telegram_users(self) -> dict[str, dict]: ...
    def _save_telegram_users(self) -> None: ...


class ConnManagerProto(Protocol):
    """Connection manager interface."""

    def cleanup_client_state(self, key: str) -> None: ...


class WSManagerProto(Protocol):
    """WebSocket manager interface."""

    async def broadcast(self, *args: Any, **kwargs: Any) -> int: ...


# ---------------------------------------------------------------------------
# Service singletons (set during lifespan startup)
# ---------------------------------------------------------------------------
db_manager: DatabaseManagerProto | None = None
client_manager: ClientManagerProto | None = None
mt5_manager: MT5ManagerProto | None = None
risk_manager: Any = None
rate_limiter: RateLimiterProto | None = None
auth_manager: AuthManagerProto | None = None
admin_logger: AdminLoggerProto | None = None
telegram_bot: TelegramBotProto | None = None

# Redis
redis_client: Any = None  # Also accessible as _redis_client for backward compat
_redis_client: Any = None  # Alias used by route modules

# Connection state
conn_manager: ConnManagerProto | None = None
http_polling_clients: dict = {}
signal_queues: dict = {}

# WebSocket
ws_manager: WSManagerProto | None = None

# Config
settings: Any = None

# Background task references
_ws_subscriber_task: Any = None

# Auth dependency (created during startup by create_auth_dependency)
_require_auth_dependency: Any = None

_auth_store: Any = None

# Feature flags
MT5_AVAILABLE: bool = False
PINETUNNEL_AVAILABLE: bool = False

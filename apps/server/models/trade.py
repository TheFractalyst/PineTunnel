"""Pydantic models for trade analytics request/response schemas."""

from pydantic import BaseModel, validator


class TradeReport(BaseModel):
    """Trade execution report from EA"""

    license_key: str
    secret_key: str | None = None
    action: str  # BUY, SELL, CLOSE
    symbol: str
    volume: float
    price: float
    ticket: int
    success: bool
    error_msg: str | None = ""
    magic: int | str | None = 0  # Accept both int and string from EA
    profit: float | None = 0.0
    sl: float | None = 0.0
    tp: float | None = 0.0
    commission: float | None = 0.0
    swap: float | None = 0.0
    timestamp: str
    broker_time: str
    account: int | str | None = 0  # Accept both int and string from EA
    broker: str
    signal_id: str | None = None

    # Validators to convert string to int if needed
    @validator("magic", pre=True)
    def parse_magic(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0
        return v

    @validator("account", pre=True)
    def parse_account(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0
        return v


class CloseReport(BaseModel):
    """Position close report from EA"""

    license_key: str
    secret_key: str | None = None
    action: str = "CLOSE"
    symbol: str
    ticket: int
    close_price: float
    profit: float
    magic: int | str | None = 0  # Accept both int and string from EA
    timestamp: str
    account: int | str | None = 0  # Accept both int and string from EA
    signal_id: str | None = None

    # Validators to convert string to int if needed
    @validator("magic", pre=True)
    def parse_magic(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0
        return v

    @validator("account", pre=True)
    def parse_account(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0
        return v


class AccountStats(BaseModel):
    """Periodic account snapshot from EA"""

    license_key: str
    secret_key: str | None = None
    account: int | str | None = 0
    account_name: str | None = ""
    broker: str | None = ""
    currency: str | None = ""
    leverage: int | str | None = 0
    balance: float = 0.0
    equity: float = 0.0
    profit: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    open_positions: int = 0
    pending_orders: int = 0
    ea_version: str | None = ""
    dll_version: str | None = ""
    magic: int | str | None = 0
    timestamp: str

    @validator("account", "leverage", "magic", pre=True)
    def coerce_to_int(cls, v):
        if isinstance(v, str):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0
        return v

"""Telegram bot helper utilities — license formatting, validation, and message formatters."""

import re
import secrets as _secrets
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional

from dateutil import parser as date_parser

from telegram.helpers import escape_markdown as _escape_md

CONNECTED_CLIENT_THRESHOLD_SEC = 10
LICENSE_PAGE_SIZE = 8
SIGNAL_PAGE_SIZE = 5

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_PATH_PY_RE = re.compile(r"/[^\s]+\.py")
_PATH_WIN_RE = re.compile(r"C:\\[^\s]+")


def generate_license_key() -> str:
    """Generate a 13-digit numeric license key (PineTunnel-compatible format).

    Uses crypto-secure randomness. For new-style API keys, use
    security.generate_api_key() instead.
    """
    return "".join(str(_secrets.randbelow(10)) for _ in range(13))


def format_license_info(key: str, data: dict) -> str:
    """Format license data for Telegram display."""
    status = data.get("status", "unknown")
    status_emoji = "🟢" if status == "active" else "🔴"
    created_raw = data.get("created_at")
    created = created_raw or "N/A"
    if isinstance(created, str) and len(created) > 10:
        created = created[:10]

    # Calculate expiry
    expiry_str = "Lifetime"
    expires_at = data.get("expires_at")
    if expires_at is not None:
        expiry_str = expires_at[:10] if len(expires_at) > 10 else expires_at
    elif created_raw:
        try:
            created_dt = date_parser.parse(created_raw)
            expiry_dt = created_dt + timedelta(days=365)
            expiry_str = expiry_dt.strftime("%Y-%m-%d")
            if datetime.now() > expiry_dt:
                expiry_str += " ⚠️ EXPIRED"
        except Exception:
            expiry_str = "1 year from creation"

    features = _escape_md(", ".join(data.get("features", [])) or "None", version=1)
    secret_key = data.get("secret_key", "")
    user_id = data.get("user_id", 0)

    return (
        f"{status_emoji} *{_escape_md(data.get('name', 'Unknown'), version=1)}*\n"
        f"| License ID: `{key}`\n"
        f"| Secret Key: `{secret_key}`\n"
        f"| User ID: {user_id}\n"
        f"| Email: {_escape_md(data.get('email', 'N/A'), version=1)}\n"
        f"| Status: {status}\n"
        f"| Created: {created}\n"
        f"| Expires: {expiry_str}\n"
        f"| Max Volume: {_escape_md(str(data.get('max_volume', 'N/A')), version=1)}\n"
        f"| Max Symbols: {_escape_md(str(data.get('max_symbols', 'N/A')), version=1)}\n"
        f"| Max Daily Trades: {_escape_md(str(data.get('max_daily_trades', 'N/A')), version=1)}\n"
        f"| Max Daily Loss: {_escape_md(str(data.get('max_daily_loss', 'N/A')), version=1)}\n"
        f"| Features: {features}\n"
        f"- Enabled: {data.get('enabled', True)}"
    )


@lru_cache(maxsize=128)
def truncate(text: str, length: int = 30) -> str:
    """Truncate text with ellipsis if longer than length."""
    return text[:length] + "..." if len(text) > length else text


@lru_cache(maxsize=128)
def validate_email(email: str) -> bool:
    """Validate email format."""
    return _EMAIL_RE.match(email) is not None


def validate_volume(value: str) -> Optional[float]:
    """Validate and convert volume string to float."""
    try:
        vol = float(value)
        if vol <= 0 or vol > 10000 or vol != vol:  # nan check
            return None
        return vol
    except ValueError:
        return None


def validate_symbols(value: str) -> Optional[int]:
    """Validate and convert symbols count string to int."""
    try:
        sym = int(value)
        if sym <= 0 or sym > 1000:
            return None
        return sym
    except ValueError:
        return None


def is_benign_edit_error(err: Exception | str) -> bool:
    """Check if error is a benign 'message is not modified' or 'message to edit not found' error.

    python-telegram-bot v21+ strips 'Bad Request: ' prefix and capitalizes the
    message, so the check must be case-insensitive.
    """
    msg = str(err).lower()
    return "message is not modified" in msg or "message to edit not found" in msg


def _sanitize_error(e: Exception) -> str:
    """Sanitize exception text for safe Telegram display (strip paths, secrets)."""
    text = str(e)[:200]
    text = _PATH_PY_RE.sub("<path>", text)
    text = _PATH_WIN_RE.sub("<path>", text)
    return _escape_md(text, version=1)


# ── User notification message formatters ────────────────────────────

SEP = "━" * 20


@lru_cache(maxsize=128)
def calc_pagination(page: int, total_items: int, page_size: int = 8) -> tuple[int, int, int]:
    """Calculate pagination values. Returns (page, total_pages, start)."""
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return page, total_pages, start


def format_trade_executed_msg(report) -> str:
    """Format a trade-executed notification for Telegram."""
    direction = report.action.upper()
    return (
        "✅ Trade Executed\n"
        f"{SEP}\n"
        f"📊 {report.symbol} | {direction}\n"
        f"💰 Entry: {report.price:.5f}\n"
        f"📏 Vol: {report.volume:.2f} lots\n"
        f"🎫 Ticket: #{report.ticket}\n"
        f"🛡 SL: {report.sl:.5f} | TP: {report.tp:.5f}\n"
        f"🔑 Signal: {report.signal_id or 'N/A'}\n"
        f"{SEP}"
    )


def format_trade_failed_msg(report) -> str:
    """Format a trade-failed notification for Telegram."""
    msg = (
        "❌ Trade Failed\n"
        f"{SEP}\n"
        f"📊 {report.action} {report.symbol}\n"
        f"📏 Vol: {report.volume:.2f} lots\n"
    )
    if report.price > 0:
        msg += f"💰 Entry: {report.price:.5f}\n"
    if report.sl:
        msg += f"🛡 SL: {report.sl:.5f}\n"
    if report.tp:
        msg += f"🎯 TP: {report.tp:.5f}\n"
    msg += f"❗ {report.error_msg or 'Unknown error'}\n"
    msg += f"🔑 Signal: {report.signal_id or 'N/A'}\n"
    msg += f"{SEP}"
    return msg


def format_position_closed_msg(report) -> str:
    """Format a position-closed notification for Telegram."""
    emoji = "📈" if report.profit >= 0 else "📉"
    return (
        f"📊 Position Closed {emoji}\n"
        f"{SEP}\n"
        f"📊 {report.symbol}\n"
        f"🎫 Ticket: #{report.ticket}\n"
        f"💰 Close: {report.close_price:.5f}\n"
        f"💵 P/L: ${report.profit:.2f}\n"
        f"🔑 Signal: {report.signal_id or 'N/A'}\n"
        f"{SEP}"
    )


def format_margin_warning_msg(
    license_key: str, margin_level: float, balance: float, equity: float
) -> str:
    """Format a margin-warning notification for Telegram."""
    return (
        "⚠️ Margin Warning\n"
        f"{SEP}\n"
        f"🔑 License: {license_key}\n"
        f"📊 Margin Level: {margin_level:.1f}%\n"
        f"💰 Balance: ${balance:.2f}\n"
        f"📈 Equity: ${equity:.2f}\n"
        f"{SEP}"
    )


def format_equity_drawdown_msg(
    license_key: str, drop_pct: float, balance: float, equity: float
) -> str:
    """Format an equity-drawdown notification for Telegram."""
    return (
        "📉 Equity Drawdown Alert\n"
        f"{SEP}\n"
        f"🔑 License: {license_key}\n"
        f"📊 Drop: {drop_pct:.1f}%\n"
        f"💰 Balance: ${balance:.2f}\n"
        f"📈 Equity: ${equity:.2f}\n"
        f"{SEP}"
    )


def format_ea_status_msg(license_key: str, connected: bool) -> str:
    """Format an EA connection status notification for Telegram."""
    status = "Connected" if connected else "Disconnected"
    return f"🔌 EA {status}\n{SEP}\n🔑 License: {license_key}\n{SEP}"

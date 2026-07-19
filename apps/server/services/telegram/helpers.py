"""Telegram bot helper utilities - admin formatting and validation."""

import re
import secrets as _secrets
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional

from dateutil import parser as date_parser

from telegram.helpers import escape_markdown as _escape_md

from apps.server.ws.connection import HTTP_POLLING_TIMEOUT as CONNECTED_CLIENT_THRESHOLD_SEC

LICENSE_PAGE_SIZE = 8
SIGNAL_PAGE_SIZE = 5

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_PATH_PY_RE = re.compile(r"/[^\s]+\.py")
_PATH_WIN_RE = re.compile(r"C:\\[^\s]+")


def generate_license_key() -> str:
    """Generate a 13-digit numeric license key."""
    return "".join(str(_secrets.randbelow(10)) for _ in range(13))


def mask_secret(secret: str) -> str:
    """Mask a secret key for display: show first 4 chars + ****."""
    if not secret:
        return "****"
    s = str(secret)
    if len(s) <= 4:
        return s + "****"
    return s[:4] + "****"


def format_license_info(key: str, data: dict) -> str:
    """Format license data for Telegram admin display."""
    status = data.get("status", "unknown")
    status_icon = "[OK]" if status == "active" else "[X]"
    created_raw = data.get("created_at")
    created = created_raw or "N/A"
    if isinstance(created, str) and len(created) > 10:
        created = created[:10]

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
                expiry_str += " [!EXPIRED]"
        except Exception:
            expiry_str = "1 year from creation"

    features = _escape_md(", ".join(data.get("features", [])) or "None", version=1)
    secret_key = data.get("secret_key", "")
    masked_secret = mask_secret(secret_key)
    user_id = data.get("user_id", 0)

    return (
        f"{status_icon} *{_escape_md(data.get('name', 'Unknown'), version=1)}*\n"
        f"| License ID: `{key}`\n"
        f"| Secret Key: `{masked_secret}`\n"
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
        if vol <= 0 or vol > 10000 or vol != vol:
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
    """Check if error is a benign 'message is not modified' error."""
    msg = str(err).lower()
    return "message is not modified" in msg or "message to edit not found" in msg


def _sanitize_error(e: Exception) -> str:
    """Sanitize exception text for safe Telegram display (strip paths)."""
    text = str(e)[:200]
    text = _PATH_PY_RE.sub("<path>", text)
    text = _PATH_WIN_RE.sub("<path>", text)
    return _escape_md(text, version=1)


# -- Admin notification formatters ---------------------------------------

SEP = "-" * 20


@lru_cache(maxsize=128)
def calc_pagination(page: int, total_items: int, page_size: int = 8) -> tuple[int, int, int]:
    """Calculate pagination values. Returns (page, total_pages, start)."""
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return page, total_pages, start


def format_trade_executed_msg(report) -> str:
    """Format a trade-executed notification for admin Telegram."""
    direction = report.action.upper()
    return (
        "[OK] Trade Executed\n"
        f"{SEP}\n"
        f"Symbol: {report.symbol} | {direction}\n"
        f"Entry: {report.price:.5f}\n"
        f"Vol: {report.volume:.2f} lots\n"
        f"Ticket: #{report.ticket}\n"
        f"SL: {report.sl:.5f} | TP: {report.tp:.5f}\n"
        f"Signal: {report.signal_id or 'N/A'}\n"
        f"{SEP}"
    )


def format_trade_failed_msg(report) -> str:
    """Format a trade-failed notification for admin Telegram."""
    msg = (
        "[X] Trade Failed\n"
        f"{SEP}\n"
        f"Action: {report.action} {report.symbol}\n"
        f"Vol: {report.volume:.2f} lots\n"
    )
    if report.price > 0:
        msg += f"Entry: {report.price:.5f}\n"
    if report.sl:
        msg += f"SL: {report.sl:.5f}\n"
    if report.tp:
        msg += f"TP: {report.tp:.5f}\n"
    msg += f"Error: {report.error_msg or 'Unknown error'}\n"
    msg += f"Signal: {report.signal_id or 'N/A'}\n"
    msg += f"{SEP}"
    return msg


def format_position_closed_msg(report) -> str:
    """Format a position-closed notification for admin Telegram."""
    pnl = "+" if report.profit >= 0 else "-"
    return (
        f"Position Closed {pnl}\n"
        f"{SEP}\n"
        f"Symbol: {report.symbol}\n"
        f"Ticket: #{report.ticket}\n"
        f"Close: {report.close_price:.5f}\n"
        f"P/L: ${report.profit:.2f}\n"
        f"Signal: {report.signal_id or 'N/A'}\n"
        f"{SEP}"
    )


def format_ea_status_msg(license_key: str, connected: bool) -> str:
    """Format an EA connection status notification for admin Telegram."""
    status = "Connected" if connected else "Disconnected"
    return f"EA {status}\n{SEP}\nLicense: {license_key}\n{SEP}"

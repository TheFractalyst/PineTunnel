import re
from datetime import datetime, timezone
from functools import lru_cache

from telegram.helpers import escape_markdown as _escape_md
from telegram import InlineKeyboardButton

SEP = "-" * 20
_PAGE_SIZE = 8

_PATH_PY_RE = re.compile(r"/[^\s]+\.py")
_PATH_WIN_RE = re.compile(r"C:\\[^\s]+")


def escape_md(text: str) -> str:
    return _escape_md(str(text), version=1)


def mask_license_key(key: str) -> str:
    if not key:
        return "****"
    s = str(key)
    if len(s) <= 8:
        return s + "..."
    return s[:8] + "..."


def mask_secret(secret: str) -> str:
    if not secret:
        return "****"
    s = str(secret)
    if len(s) <= 4:
        return s + "****"
    return s[:4] + "****"


@lru_cache(maxsize=128)
def calc_pagination(page: int, total_items: int, page_size: int = 8) -> tuple[int, int, int]:
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return page, total_pages, start


def format_relative_time(iso_str: str) -> str:
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except Exception:
        return str(iso_str)[:16]
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def sanitize_error(e: Exception) -> str:
    text = str(e)[:200]
    text = _PATH_PY_RE.sub("<path>", text)
    text = _PATH_WIN_RE.sub("<path>", text)
    return escape_md(text)


def _count_active_licenses(bot) -> int:
    return sum(1 for c in bot.client_manager.clients.values() if c.get("status") == "active")


def _count_connected(bot) -> int:
    from datetime import datetime
    now = datetime.now()
    connected = set()
    for key, poll_data in bot.http_polling_clients.items():
        last = poll_data.get("last_poll")
        if last and (now - last).total_seconds() <= 60:
            connected.add(key)
    if bot.ws_manager:
        try:
            for lic in bot.ws_manager.get_connected_license_keys():
                connected.add(lic)
        except Exception:
            pass
    return len(connected)


def overview_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]:
    clients = bot.client_manager.clients
    total = len(clients)
    active = _count_active_licenses(bot)
    connected = _count_connected(bot)

    lines = ["Overview", SEP]

    if clients:
        first_key = list(clients.keys())[0]
        data = clients[first_key]
        name = escape_md(data.get("name", "Unknown"))
        expires = data.get("expires_at", "Lifetime")
        if expires and len(str(expires)) > 10:
            expires = str(expires)[:10]
        lines.append(f"Licenses: {active}/{total} active | Connected: {connected}")
        lines.append(f"First: {name} | Expires: {expires}")
    else:
        lines.append(f"Licenses: 0 | Connected: {connected}")
        lines.append("No licenses configured")

    pending = 0
    try:
        for key in clients:
            count = bot.db_manager.get_signal_count(key, "pending")
            pending += count
    except Exception:
        pass
    lines.append(f"Pending signals: {pending}")

    recent_signals = []
    try:
        recent_signals = bot.db_manager.execute_query(
            "SELECT timestamp, action, symbol, response_code FROM alert_history "
            "ORDER BY timestamp DESC LIMIT 5"
        )
    except Exception:
        pass

    if recent_signals:
        lines.append(SEP)
        lines.append("Recent Signals:")
        for s in recent_signals:
            r = dict(s)
            ts = str(r.get("timestamp", ""))[:16]
            action = (r.get("action", "?") or "?").upper()
            symbol = escape_md(r.get("symbol", "?") or "?")
            code = r.get("response_code", 0) or 0
            mark = "[OK]" if code == 200 else "[X]"
            lines.append(f"  {mark} {action} {symbol} - {ts}")

    recent_trades = []
    try:
        recent_trades = bot.db_manager.execute_query(
            "SELECT timestamp, symbol, action, volume, profit FROM trades "
            "ORDER BY timestamp DESC LIMIT 5"
        )
    except Exception:
        pass

    if recent_trades:
        lines.append(SEP)
        lines.append("Recent Trades:")
        for t in recent_trades:
            r = dict(t)
            ts = str(r.get("timestamp", ""))[:16]
            symbol = escape_md(r.get("symbol", "?") or "?")
            side = (r.get("action", "?") or "?").upper()
            vol = r.get("volume", 0) or 0
            profit = r.get("profit", 0) or 0
            sign = "+" if profit >= 0 else ""
            lines.append(f"  {ts} | {side} {symbol} {vol:.2f} | {sign}{profit:.2f}")

    text = "\n".join(lines)
    keyboard = [
        [InlineKeyboardButton("Refresh", callback_data="refresh:overview"),
         InlineKeyboardButton("Account", callback_data="nav:account")],
        [InlineKeyboardButton("Trades", callback_data="nav:trades"),
         InlineKeyboardButton("Signals", callback_data="nav:signals")],
        [InlineKeyboardButton("Back to Menu", callback_data="nav:main")],
    ]
    return text, keyboard

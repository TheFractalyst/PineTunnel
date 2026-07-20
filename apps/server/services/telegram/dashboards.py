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


def account_screen(bot, page: int = 0) -> tuple[str, list[list[InlineKeyboardButton]]]:
    clients = bot.client_manager.clients
    keys = list(clients.keys())
    total = len(keys)

    if total == 0:
        text = "Account & License\n" + SEP + "\nNo licenses found."
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="nav:main")]]
        return text, keyboard

    page, total_pages, start = calc_pagination(page, total, 1)
    key = keys[start]
    data = clients[key]
    revealed = key in getattr(bot, "_revealed_keys", set())

    name = escape_md(data.get("name", "Unknown"))
    status = data.get("status", "unknown")
    enabled = data.get("enabled", True)
    if not enabled:
        status = "disabled"

    expires = data.get("expires_at", "Lifetime")
    if expires and len(str(expires)) > 10:
        expires = str(expires)[:10]

    lic_display = key if revealed else mask_license_key(key)
    secret = data.get("secret_key", "")
    secret_display = secret if revealed else mask_secret(secret)

    connection = data.get("connection", "unknown")
    user_id = data.get("user_id", "N/A")

    lines = [
        f"Account & License ({page + 1}/{total_pages})",
        SEP,
        f"License: {lic_display}",
        f"Name: {name}",
        f"Secret: {secret_display}",
        f"Status: {status} | Expires: {expires}",
        f"Connection: {connection}",
        f"User ID: {user_id}",
        SEP,
    ]

    text = "\n".join(lines)

    reveal_label = "Hide" if revealed else "Reveal"

    keyboard = [
        [InlineKeyboardButton(reveal_label, callback_data=f"reveal:key:{key}")],
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"page:account:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"page:account:{page + 1}"))
    keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="nav:main")])
    return text, keyboard


def trades_screen(bot, page: int = 0, side_filter: str = "all") -> tuple[str, list[list[InlineKeyboardButton]]]:
    where = ""
    params: dict = {}
    if side_filter != "all":
        where = " WHERE LOWER(action) LIKE :side"
        params["side"] = f"%{side_filter}%"

    total = 0
    try:
        rows = bot.db_manager.execute_query(f"SELECT COUNT(*) as cnt FROM trades{where}", params)
        total = rows[0]["cnt"] if rows else 0
    except Exception:
        pass

    page, total_pages, start = calc_pagination(page, total, _PAGE_SIZE)

    records = []
    try:
        records = bot.db_manager.execute_query(
            f"SELECT timestamp, symbol, action, volume, profit FROM trades{where} "
            f"ORDER BY timestamp DESC LIMIT :lim OFFSET :off",
            {**params, "lim": _PAGE_SIZE, "off": start},
        )
    except Exception:
        pass

    filter_label = side_filter.title() if side_filter != "all" else "All sides"
    lines = [f"Trade History ({filter_label})", SEP]

    if not records:
        lines.append("No trades found.")
    else:
        lines.append(f"{len(records)} of {total} records | Page {page + 1}/{total_pages}")
        lines.append(SEP)
        for r in records:
            r = dict(r)
            ts = str(r.get("timestamp", ""))[:16]
            symbol = escape_md(r.get("symbol", "?") or "?")
            side = (r.get("action", "?") or "?").upper()
            vol = r.get("volume", 0) or 0
            profit = r.get("profit", 0) or 0
            sign = "+" if profit >= 0 else ""
            lines.append(f"{ts} | {side:4s} | {symbol} | {vol:.2f} | {sign}{profit:.2f}")

    text = "\n".join(lines)

    def flabel(s):
        return f"[OK] {s.title()}" if side_filter == s else s.title()

    keyboard = [
        [InlineKeyboardButton(flabel("all"), callback_data="filter:trades:all"),
         InlineKeyboardButton(flabel("buy"), callback_data="filter:trades:buy"),
         InlineKeyboardButton(flabel("sell"), callback_data="filter:trades:sell"),
         InlineKeyboardButton(flabel("close"), callback_data="filter:trades:close")],
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"page:trades:{page - 1}:{side_filter}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"page:trades:{page + 1}:{side_filter}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="nav:main")])
    return text, keyboard


def signals_screen(bot, page: int = 0, cmd_filter: str = "all", status_filter: str = "all") -> tuple[str, list[list[InlineKeyboardButton]]]:
    clauses = []
    params: dict = {}
    if cmd_filter != "all":
        clauses.append("LOWER(action) LIKE :cmd")
        params["cmd"] = f"%{cmd_filter}%"
    if status_filter != "all":
        if status_filter == "success":
            clauses.append("response_code = 200")
        elif status_filter == "failed":
            clauses.append("response_code != 200")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    total = 0
    try:
        rows = bot.db_manager.execute_query(f"SELECT COUNT(*) as cnt FROM alert_history{where}", params)
        total = rows[0]["cnt"] if rows else 0
    except Exception:
        pass

    page, total_pages, start = calc_pagination(page, total, _PAGE_SIZE)

    records = []
    try:
        records = bot.db_manager.execute_query(
            f"SELECT timestamp, action, symbol, response_code FROM alert_history{where} "
            f"ORDER BY timestamp DESC LIMIT :lim OFFSET :off",
            {**params, "lim": _PAGE_SIZE, "off": start},
        )
    except Exception:
        pass

    cmd_label = cmd_filter.title() if cmd_filter != "all" else "All"
    status_label = status_filter.title() if status_filter != "all" else "All"
    lines = [f"Signal Log ({cmd_label} / {status_label})", SEP]

    if not records:
        lines.append("No signals found.")
    else:
        lines.append(f"{len(records)} of {total} records | Page {page + 1}/{total_pages}")
        lines.append(SEP)
        for r in records:
            r = dict(r)
            ts = str(r.get("timestamp", ""))[:16]
            action = (r.get("action", "?") or "?").upper()
            symbol = escape_md(r.get("symbol", "?") or "?")
            code = r.get("response_code", 0) or 0
            mark = "[OK]" if code == 200 else "[X]"
            lines.append(f"{ts} | {mark} {action:6s} | {symbol}")

    text = "\n".join(lines)

    def clabel(s):
        return f"[OK] {s.title()}" if cmd_filter == s else s.title()

    def slabel(s):
        return f"[OK] {s.title()}" if status_filter == s else s.title()

    keyboard = [
        [InlineKeyboardButton(clabel("all"), callback_data="filter:signals:cmd:all"),
         InlineKeyboardButton(clabel("buy"), callback_data="filter:signals:cmd:buy"),
         InlineKeyboardButton(clabel("sell"), callback_data="filter:signals:cmd:sell"),
         InlineKeyboardButton(clabel("close"), callback_data="filter:signals:cmd:close")],
        [InlineKeyboardButton(slabel("all"), callback_data="filter:signals:status:all"),
         InlineKeyboardButton(slabel("success"), callback_data="filter:signals:status:success"),
         InlineKeyboardButton(slabel("failed"), callback_data="filter:signals:status:failed")],
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"page:signals:{page - 1}:{cmd_filter}:{status_filter}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"page:signals:{page + 1}:{cmd_filter}:{status_filter}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="nav:main")])
    return text, keyboard


DEFAULT_NOTIFICATION_PREFS = {
    "trade_opened": True,
    "trade_closed": True,
    "error_alerts": True,
    "connection_changes": False,
    "signal_received": False,
}

DEFAULT_QUIET_HOURS = {
    "enabled": False,
    "start": "22:00",
    "end": "08:00",
}

_NOTIF_ITEMS = [
    ("trade_opened", "Trade Opened"),
    ("trade_closed", "Trade Closed"),
    ("error_alerts", "Error Alerts"),
    ("connection_changes", "Connection Changes"),
    ("signal_received", "Signal Received"),
]


def settings_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]:
    prefs = getattr(bot, "notification_prefs", DEFAULT_NOTIFICATION_PREFS)
    quiet = getattr(bot, "quiet_hours", DEFAULT_QUIET_HOURS)
    alerts = getattr(bot, "alerts_enabled", True)

    lines = ["Settings", SEP, "Notification Preferences:"]
    for key, label in _NOTIF_ITEMS:
        on = prefs.get(key, False)
        mark = "[OK]" if on else "[  ]"
        state = "ON" if on else "OFF"
        lines.append(f"  {mark} {label:20s} - {state}")

    lines.append(SEP)
    quiet_on = quiet.get("enabled", False)
    quiet_mark = "[OK]" if quiet_on else "[  ]"
    quiet_state = f"ON ({quiet.get('start','?')}-{quiet.get('end','?')})" if quiet_on else "OFF"
    lines.append(f"Quiet Hours: {quiet_mark} {quiet_state}")

    lines.append(SEP)
    alerts_mark = "[OK]" if alerts else "[  ]"
    lines.append(f"Master Alerts: {alerts_mark} {'ON' if alerts else 'OFF'}")

    text = "\n".join(lines)

    def toggle_btn(key, label):
        on = prefs.get(key, False)
        state = "ON" if on else "OFF"
        return InlineKeyboardButton(f"{label}: {state}", callback_data=f"toggle:{key}")

    keyboard = [
        [toggle_btn("trade_opened", "Trade Opened"), toggle_btn("trade_closed", "Trade Closed")],
        [toggle_btn("error_alerts", "Error Alerts"), toggle_btn("connection_changes", "Conn Changes")],
        [toggle_btn("signal_received", "Signal Received")],
        [InlineKeyboardButton(
            f"Quiet Hours: {'ON' if quiet_on else 'OFF'}",
            callback_data="toggle:quiet_hours",
        )],
        [InlineKeyboardButton(
            f"Alerts Master: {'ON' if alerts else 'OFF'}",
            callback_data="toggle:alerts",
        )],
        [InlineKeyboardButton("Back to Menu", callback_data="nav:main")],
    ]
    return text, keyboard


def admin_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]:
    clients = bot.client_manager.clients
    total = len(clients)
    active = _count_active_licenses(bot)
    connected = _count_connected(bot)

    signals_7d = 0
    success_7d = 0
    try:
        interval = bot.db_manager.sql_interval_days(7)
        rows = bot.db_manager.execute_query(
            f"SELECT COUNT(*) as cnt_total, "
            f"COUNT(CASE WHEN response_code = 200 THEN 1 END) as cnt_success "
            f"FROM alert_history WHERE timestamp >= {interval}"
        )
        if rows:
            signals_7d = rows[0]["cnt_total"] or 0
            success_7d = rows[0]["cnt_success"] or 0
    except Exception:
        pass

    failed_7d = signals_7d - success_7d
    rate = round((success_7d / signals_7d * 100), 1) if signals_7d > 0 else 0

    ws_count = 0
    if bot.ws_manager:
        try:
            ws_count = len(bot.ws_manager.get_connected_license_keys())
        except Exception:
            pass

    http_count = 0
    from datetime import datetime
    now = datetime.now()
    for key, poll_data in bot.http_polling_clients.items():
        last = poll_data.get("last_poll")
        if last and (now - last).total_seconds() <= 60:
            http_count += 1

    lines = [
        "Admin Panel",
        SEP,
        f"Total Licenses: {total}",
        f"Active Licenses: {active}",
        f"Connected EAs: {connected}",
        f"Signals (7d): {signals_7d}",
        SEP,
        f"Signal Queue Stats (7d):",
        f"  Total: {signals_7d} | Success: {success_7d}",
        f"  Failed: {failed_7d} | Rate: {rate}%",
        SEP,
        f"EA Connections:",
        f"  WebSocket: {ws_count} | HTTP Polling: {http_count}",
        SEP,
    ]

    try:
        from apps.server.middleware.main import failed_attempt_tracker
        from apps.server.state import rate_limiter
        blocked = []
        if failed_attempt_tracker is not None:
            fa = failed_attempt_tracker.get_statistics()
            for entry in fa.get("blocked_ips", []):
                blocked.append(f"  {entry['ip']} (failed auth, {entry['remaining_seconds']}s left)")
        if rate_limiter is not None:
            import time as _time
            for ip, until in rate_limiter.blocked_ips.items():
                remaining = max(0, int(until - _time.time()))
                blocked.append(f"  {ip} (rate limit, {remaining}s left)")
        lines.append(f"Blocked IPs: {len(blocked)}")
        lines.extend(blocked[:10])
    except Exception:
        lines.append("Blocked IPs: N/A")

    text = "\n".join(lines)
    keyboard = [
        [InlineKeyboardButton("Refresh", callback_data="refresh:admin")],
        [InlineKeyboardButton("Back to Menu", callback_data="nav:main")],
    ]
    return text, keyboard


def main_menu(bot) -> tuple[str, list[list[InlineKeyboardButton]]]:
    from datetime import datetime, timezone
    total = len(bot.client_manager.clients)
    active = _count_active_licenses(bot)
    connected = _count_connected(bot)

    pending = 0
    try:
        for key in bot.client_manager.clients:
            count = bot.db_manager.get_signal_count(key, "pending")
            pending += count
    except Exception:
        pass

    alerts_state = "ON" if bot.alerts_enabled else "OFF"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    text = (
        "PineTunnel Dashboard\n"
        f"{SEP}\n"
        f"Licenses: {active}/{total} active | Connected: {connected} | Pending: {pending}\n"
        f"Alerts: {alerts_state} | {now_str}\n"
        f"{SEP}\n"
        "Select a dashboard:"
    )

    keyboard = [
        [InlineKeyboardButton("Overview", callback_data="nav:overview"),
         InlineKeyboardButton("Account", callback_data="nav:account")],
        [InlineKeyboardButton("Trades", callback_data="nav:trades"),
         InlineKeyboardButton("Signals", callback_data="nav:signals")],
        [InlineKeyboardButton("Settings", callback_data="nav:settings"),
         InlineKeyboardButton("Admin", callback_data="nav:admin")],
    ]
    return text, keyboard

# Telegram Inline Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing Telegram bot's menu/monitoring mixins with 5 inline-keyboard dashboard screens mirroring the PineTunnel Next.js dashboard, admin-only, no subscription/payment logic.

**Architecture:** Two files - `bot.py` (class, lifecycle, callback router, event hooks) and `dashboards.py` (6 screen functions + helpers). Stateless callback routing via encoded callback data. Direct manager access for data (no REST API calls).

**Tech Stack:** python-telegram-bot>=21.7, FastAPI, SQLAlchemy/SQLite, psutil

## Global Constraints

- ASCII-only in all user-facing strings (no emoji, em dashes, smart quotes). Use `[OK]`, `[X]`, `[!]`, `[^]`, `[v]` for status indicators.
- No comments in code unless explicitly requested.
- No ConversationHandler - all interactions are stateless callback queries.
- Constructor signature unchanged: `PineTunnelTelegramBot(token, admin_ids, client_manager, db_manager, data_dir, http_polling_clients, signal_queues, conn_manager, ws_manager, test_env, auth_store, admin_logger)`.
- No changes to lifespan.py instantiation.
- Telegram callback_data max 64 bytes.
- Telegram message max 4096 chars - lists paginated at 8 items per page.
- License keys masked to first 8 chars + `...`. Secret keys masked to first 4 chars + `****`.
- Tests stub the `telegram` package (not installed in test env) - see conftest pattern in `tests/test_telegram_helpers.py`.

---

## File Structure

```
apps/server/services/telegram/
  __init__.py          # MODIFY - update imports if needed
  bot.py               # REWRITE - class, lifecycle, router, event hooks
  dashboards.py        # CREATE - 6 screen functions + formatting helpers

apps/server/services/telegram/mixins/  # DELETE entire directory
apps/server/services/telegram/constants.py   # DELETE
apps/server/services/telegram/helpers.py     # DELETE
apps/server/services/telegram/keyboards.py   # DELETE

tests/
  test_telegram_dashboards.py  # CREATE - test dashboard formatting functions
  test_telegram_helpers.py     # DELETE (tests old helpers.py which is deleted)
```

---

### Task 1: Create dashboards.py with shared helpers

**Files:**
- Create: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Produces: `SEP` (str), `mask_license_key(key) -> str`, `mask_secret(secret) -> str`, `calc_pagination(page, total, page_size) -> tuple[int, int, int]`, `format_relative_time(iso_str) -> str`, `escape_md(text) -> str`

- [ ] **Step 1: Write failing tests for helpers**

Create `tests/test_telegram_dashboards.py`:

```python
"""Tests for Telegram dashboard formatting helpers.

The telegram package is not installed in test env, so we stub minimal
imports and test the pure-function helpers in dashboards.py.
"""

import importlib.util
import sys
import types

import pytest


@pytest.fixture
def dash_module():
    """Load dashboards.py with stubbed telegram imports."""
    tg = types.ModuleType("telegram")
    tg.helpers = types.ModuleType("telegram.helpers")
    tg.helpers.escape_markdown = lambda s, version=1: s
    sys.modules["telegram"] = tg
    sys.modules["telegram.helpers"] = tg.helpers

    spec = importlib.util.spec_from_file_location(
        "_dashboards_test",
        "apps/server/services/telegram/dashboards.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    for k in ("telegram", "telegram.helpers", "_dashboards_test"):
        sys.modules.pop(k, None)


def test_mask_license_key_long(dash_module):
    assert dash_module.mask_license_key("abcd1234efgh5678") == "abcd1234..."


def test_mask_license_key_short(dash_module):
    assert dash_module.mask_license_key("abc") == "abc..."


def test_mask_license_key_empty(dash_module):
    assert dash_module.mask_license_key("") == "****"


def test_mask_secret_long(dash_module):
    assert dash_module.mask_secret("abcd123456") == "abcd****"


def test_mask_secret_short(dash_module):
    assert dash_module.mask_secret("ab") == "ab****"


def test_mask_secret_empty(dash_module):
    assert dash_module.mask_secret("") == "****"


def test_calc_pagination_basic(dash_module):
    page, total_pages, start = dash_module.calc_pagination(0, 42, 8)
    assert page == 0
    assert total_pages == 6
    assert start == 0


def test_calc_pagination_last_page(dash_module):
    page, total_pages, start = dash_module.calc_pagination(5, 42, 8)
    assert page == 5
    assert total_pages == 6
    assert start == 40


def test_calc_pagination_clamps_negative(dash_module):
    page, total_pages, start = dash_module.calc_pagination(-1, 42, 8)
    assert page == 0
    assert start == 0


def test_calc_pagination_empty(dash_module):
    page, total_pages, start = dash_module.calc_pagination(0, 0, 8)
    assert page == 0
    assert total_pages == 1
    assert start == 0


def test_sep_is_ascii(dash_module):
    assert dash_module.SEP == "-" * 20
    assert dash_module.SEP.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: FAIL with "No module named" or "FileNotFoundError" (dashboards.py doesn't exist yet)

- [ ] **Step 3: Create dashboards.py with helper functions**

Create `apps/server/services/telegram/dashboards.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add dashboards.py with shared formatting helpers"
```

---

### Task 2: Add overview_screen to dashboards.py

**Files:**
- Modify: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Consumes: `bot.client_manager`, `bot.db_manager`, `bot.ws_manager`, `bot.http_polling_clients`, `bot.alerts_enabled`
- Produces: `overview_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]`

- [ ] **Step 1: Write failing test for overview_screen**

Append to `tests/test_telegram_dashboards.py`:

```python
class FakeClientManager:
    def __init__(self, clients):
        self.clients = clients


class FakeDbManager:
    def __init__(self, rows=None):
        self._rows = rows or []

    def execute_query(self, sql, params=None):
        return self._rows


class FakeBot:
    def __init__(self, clients=None, db_rows=None):
        self.client_manager = FakeClientManager(clients or {})
        self.db_manager = FakeDbManager(db_rows)
        self.ws_manager = None
        self.http_polling_clients = {}
        self.alerts_enabled = True
        self.signal_queues = {}


def test_overview_screen_basic(dash_module):
    bot = FakeBot(
        clients={"key1": {"name": "Test", "status": "active", "expires_at": "2026-08-07"}},
        db_rows=[{"timestamp": "2026-07-20T14:30:00", "action": "buy", "symbol": "EURUSD", "response_code": 200}],
    )
    text, keyboard = dash_module.overview_screen(bot)
    assert "Overview" in text
    assert "Licenses" in text or "No licenses" in text
    assert len(keyboard) > 0
    assert text.isascii(), "Overview text must be ASCII-only"


def test_overview_screen_no_licenses(dash_module):
    bot = FakeBot()
    text, keyboard = dash_module.overview_screen(bot)
    assert "No licenses" in text or "0" in text
    assert text.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py::test_overview_screen_basic -v`
Expected: FAIL with "AttributeError: module has no attribute 'overview_screen'"

- [ ] **Step 3: Implement overview_screen**

Append to `apps/server/services/telegram/dashboards.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add overview dashboard screen"
```

---

### Task 3: Add account_screen to dashboards.py

**Files:**
- Modify: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Consumes: `bot.client_manager.clients`, `bot._revealed_keys` (dict on bot instance)
- Produces: `account_screen(bot, page=0) -> tuple[str, list[list[InlineKeyboardButton]]]`

- [ ] **Step 1: Write failing test**

Append to `tests/test_telegram_dashboards.py`:

```python
def test_account_screen_single_license(dash_module):
    bot = FakeBot(clients={
        "abcd1234efgh5678": {
            "name": "Test Account",
            "status": "active",
            "expires_at": "2026-08-07",
            "secret_key": "supersecret123",
            "user_id": 12345,
        }
    })
    bot._revealed_keys = set()
    text, keyboard = dash_module.account_screen(bot, page=0)
    assert "Account" in text
    assert "abcd1234..." in text
    assert "supersecret123" not in text
    assert "supe****" in text
    assert text.isascii()


def test_account_screen_revealed_key(dash_module):
    bot = FakeBot(clients={
        "abcd1234efgh5678": {
            "name": "Test",
            "status": "active",
            "secret_key": "supersecret123",
            "user_id": 1,
        }
    })
    bot._revealed_keys = {"abcd1234efgh5678"}
    text, keyboard = dash_module.account_screen(bot, page=0)
    assert "abcd1234efgh5678" in text
    assert "supersecret123" in text


def test_account_screen_no_licenses(dash_module):
    bot = FakeBot()
    bot._revealed_keys = set()
    text, keyboard = dash_module.account_screen(bot, page=0)
    assert "No licenses" in text
    assert text.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py::test_account_screen_single_license -v`
Expected: FAIL with "AttributeError: module has no attribute 'account_screen'"

- [ ] **Step 3: Implement account_screen**

Append to `apps/server/services/telegram/dashboards.py`:

```python
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

    reveal_label = "Hide" if revealed else "Reveal Key"
    secret_label = "Hide Secret" if revealed else "Reveal Secret"

    keyboard = [
        [InlineKeyboardButton(reveal_label, callback_data=f"reveal:key:{key}"),
         InlineKeyboardButton(secret_label, callback_data=f"reveal:secret:{key}")],
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add account & license dashboard screen"
```

---

### Task 4: Add trades_screen and signals_screen to dashboards.py

**Files:**
- Modify: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Produces: `trades_screen(bot, page=0, side_filter="all") -> tuple[str, list[list[InlineKeyboardButton]]]`, `signals_screen(bot, page=0, cmd_filter="all", status_filter="all") -> tuple[str, list[list[InlineKeyboardButton]]]`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_telegram_dashboards.py`:

```python
def test_trades_screen_basic(dash_module):
    bot = FakeBot(db_rows=[
        {"timestamp": "2026-07-20T14:30:00", "symbol": "EURUSD", "action": "buy", "volume": 0.10, "profit": 12.50},
        {"timestamp": "2026-07-20T10:15:00", "symbol": "GBPUSD", "action": "sell", "volume": 0.05, "profit": -3.20},
    ])
    text, keyboard = dash_module.trades_screen(bot, page=0)
    assert "Trade History" in text
    assert "EURUSD" in text
    assert "GBPUSD" in text
    assert "+12.50" in text
    assert "-3.20" in text
    assert text.isascii()


def test_trades_screen_empty(dash_module):
    bot = FakeBot(db_rows=[])
    text, keyboard = dash_module.trades_screen(bot, page=0)
    assert "No trades" in text
    assert text.isascii()


def test_signals_screen_basic(dash_module):
    bot = FakeBot(db_rows=[
        {"timestamp": "2026-07-20T14:30:00", "action": "buy", "symbol": "EURUSD", "response_code": 200},
        {"timestamp": "2026-07-20T10:15:00", "action": "sell", "symbol": "GBPUSD", "response_code": 500},
    ])
    text, keyboard = dash_module.signals_screen(bot, page=0)
    assert "Signal Log" in text
    assert "EURUSD" in text
    assert "[OK]" in text
    assert "[X]" in text
    assert text.isascii()


def test_signals_screen_empty(dash_module):
    bot = FakeBot(db_rows=[])
    text, keyboard = dash_module.signals_screen(bot, page=0)
    assert "No signals" in text
    assert text.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py::test_trades_screen_basic -v`
Expected: FAIL with "AttributeError: module has no attribute 'trades_screen'"

- [ ] **Step 3: Implement trades_screen and signals_screen**

Append to `apps/server/services/telegram/dashboards.py`:

```python
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
        nav.append(InlineKeyboardButton("Prev", callback_data=f"page:trades:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"page:trades:{page + 1}"))
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
        nav.append(InlineKeyboardButton("Prev", callback_data=f"page:signals:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"page:signals:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="nav:main")])
    return text, keyboard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (20 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add trades and signals dashboard screens"
```

---

### Task 5: Add settings_screen and admin_screen to dashboards.py

**Files:**
- Modify: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Consumes: `bot.notification_prefs` (dict), `bot.quiet_hours` (dict), `bot.alerts_enabled` (bool), `bot._save_bot_settings()` (method), `bot._load_bot_settings()` (method)
- Produces: `settings_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]`, `admin_screen(bot) -> tuple[str, list[list[InlineKeyboardButton]]]`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_telegram_dashboards.py`:

```python
def test_settings_screen_defaults(dash_module):
    bot = FakeBot()
    bot.alerts_enabled = True
    bot.notification_prefs = {
        "trade_opened": True, "trade_closed": True, "error_alerts": True,
        "connection_changes": False, "signal_received": False,
    }
    bot.quiet_hours = {"enabled": False, "start": "22:00", "end": "08:00"}
    text, keyboard = dash_module.settings_screen(bot)
    assert "Settings" in text
    assert "ON" in text
    assert "OFF" in text
    assert text.isascii()
    assert any("toggle:trade_opened" in (b.callback_data or "") for row in keyboard for b in row)


def test_admin_screen_basic(dash_module):
    bot = FakeBot(clients={
        "k1": {"status": "active"},
        "k2": {"status": "active"},
        "k3": {"status": "disabled"},
    })
    text, keyboard = dash_module.admin_screen(bot)
    assert "Admin Panel" in text
    assert "Total Licenses" in text
    assert "3" in text
    assert text.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py::test_settings_screen_defaults -v`
Expected: FAIL with "AttributeError: module has no attribute 'settings_screen'"

- [ ] **Step 3: Implement settings_screen and admin_screen**

Append to `apps/server/services/telegram/dashboards.py`:

```python
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
        [InlineKeyboardButton("Refresh", callback_data="refresh:admin"),
         InlineKeyboardButton("Security", callback_data="nav:security")],
        [InlineKeyboardButton("Back to Menu", callback_data="nav:main")],
    ]
    return text, keyboard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (22 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add settings and admin dashboard screens"
```

---

### Task 6: Add main_menu function to dashboards.py

**Files:**
- Modify: `apps/server/services/telegram/dashboards.py`
- Test: `tests/test_telegram_dashboards.py`

**Interfaces:**
- Produces: `main_menu(bot) -> tuple[str, list[list[InlineKeyboardButton]]]`

- [ ] **Step 1: Write failing test**

Append to `tests/test_telegram_dashboards.py`:

```python
def test_main_menu_basic(dash_module):
    bot = FakeBot(clients={"k1": {"status": "active"}})
    bot.alerts_enabled = True
    text, keyboard = dash_module.main_menu(bot)
    assert "PineTunnel Dashboard" in text
    assert "Licenses" in text
    assert any("nav:overview" in (b.callback_data or "") for row in keyboard for b in row)
    assert any("nav:admin" in (b.callback_data or "") for row in keyboard for b in row)
    assert text.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_dashboards.py::test_main_menu_basic -v`
Expected: FAIL with "AttributeError: module has no attribute 'main_menu'"

- [ ] **Step 3: Implement main_menu**

Append to `apps/server/services/telegram/dashboards.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (23 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/dashboards.py tests/test_telegram_dashboards.py
git commit -m "feat(telegram): add main menu screen"
```

---

### Task 7: Rewrite bot.py with new router and event hooks

**Files:**
- Rewrite: `apps/server/services/telegram/bot.py`
- Modify: `apps/server/services/telegram/__init__.py` (if needed)

**Interfaces:**
- Consumes: all screen functions from `dashboards.py`, `generate_download_url` from `ea_download.py`
- Produces: `PineTunnelTelegramBot` class with same constructor signature, `notify_admin()`, `on_trade_executed()`, `on_trade_execution_failed()`, `on_position_closed()`, `on_trade_failure()`, `start()`, `stop()`

- [ ] **Step 1: Rewrite bot.py**

Overwrite `apps/server/services/telegram/bot.py` with:

```python
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown as _escape_md

from .dashboards import (
    SEP,
    account_screen,
    admin_screen,
    escape_md,
    main_menu,
    overview_screen,
    sanitize_error,
    settings_screen,
    signals_screen,
    trades_screen,
    DEFAULT_NOTIFICATION_PREFS,
    DEFAULT_QUIET_HOURS,
)

logger = logging.getLogger(__name__)

_ADMIN_AUDIT_LOG_MODE = 0o600


class PineTunnelTelegramBot:
    """Admin-only Telegram bot for PineTunnel management."""

    def __init__(
        self,
        token: str,
        admin_ids: list[int],
        client_manager,
        db_manager,
        data_dir: str,
        http_polling_clients: dict | None = None,
        signal_queues: dict | None = None,
        conn_manager=None,
        ws_manager=None,
        test_env: bool = False,
        auth_store=None,
        admin_logger=None,
    ):
        self.token = token
        self.admin_ids = admin_ids
        self.client_manager = client_manager
        self.db_manager = db_manager
        self.data_dir = data_dir
        self.http_polling_clients = http_polling_clients or {}
        self.signal_queues = signal_queues or {}
        self.conn_manager = conn_manager
        self.ws_manager = ws_manager
        self._test_env = test_env
        self._auth_store = auth_store
        self.admin_logger = admin_logger

        self.alerts_enabled = True
        self.notification_prefs = dict(DEFAULT_NOTIFICATION_PREFS)
        self.quiet_hours = dict(DEFAULT_QUIET_HOURS)
        self._revealed_keys: set[str] = set()
        self._load_bot_settings()
        self.app: Application | None = None
        self._started = False

        logger.info("TelegramBot initialized with %d admin(s)", len(self.admin_ids))
        if not self.admin_ids:
            logger.warning(
                "Telegram bot has NO admin IDs configured - all admin commands will be inaccessible!"
            )

    async def start(self):
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN not set - Telegram bot disabled")
            return

        if self.app is not None:
            try:
                if self._started:
                    await self.app.updater.stop()
                    await self.app.stop()
                await self.app.shutdown()
            except Exception:
                logger.debug("Failed to clean up previous app instance", exc_info=True)
            self.app = None
            self._started = False

        try:
            logger.info("Initializing Telegram bot...")
            token = self.token
            if getattr(self, "_test_env", False):
                token = self.token + "/test"
                logger.info("Using Telegram TEST environment")
            self.app = (
                Application.builder()
                .token(token)
                .rate_limiter(AIORateLimiter(max_retries=3))
                .concurrent_updates(False)
                .build()
            )

            self._register_handlers()

            await self.app.initialize()

            try:
                await self.app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                logger.debug("delete_webhook failed (non-critical)", exc_info=True)

            admin_commands = [
                BotCommand("start", "Main menu"),
                BotCommand("menu", "Show main menu"),
                BotCommand("help", "Show help"),
                BotCommand("login", "Dashboard login code"),
            ]
            for admin_id in self.admin_ids:
                try:
                    await self.app.bot.set_my_commands(
                        admin_commands,
                        scope=BotCommandScopeChat(admin_id),
                    )
                except Exception:
                    logger.debug("Failed to set admin commands for %s", admin_id, exc_info=True)

            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)

            self._started = True
            logger.info("Telegram bot started successfully")

        except Exception as e:
            logger.error("Failed to start Telegram bot: %s", e, exc_info=True)
            if self.app is not None:
                try:
                    await self.app.stop()
                    await self.app.shutdown()
                except Exception:
                    logger.debug("Failed to clean up app after start failure", exc_info=True)
            self._started = False
            self.app = None
            return

        try:
            await self.notify_admin("PineTunnel Bot Started - Server is online and ready.")
        except Exception:
            logger.debug("notify_admin failed on startup (bot is running)", exc_info=True)

    async def stop(self):
        if self._started and self.app:
            try:
                await self.notify_admin("PineTunnel Bot Stopping - Server shutting down.")
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
                self._started = False
                logger.info("Telegram bot stopped")
            except Exception as e:
                logger.error("Error stopping Telegram bot: %s", e)

    def _is_admin(self, update: Update) -> bool:
        return update.effective_user.id in self.admin_ids

    def _register_handlers(self):
        app = self.app
        _admin_filter = filters.User(user_id=self.admin_ids) if self.admin_ids else filters.Chat(-1)

        app.add_handler(CommandHandler("start", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("menu", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=_admin_filter))
        app.add_handler(CommandHandler("login", self._cmd_login, filters=_admin_filter))
        app.add_handler(CallbackQueryHandler(self._cb_handler))
        app.add_error_handler(self._error_handler)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = main_menu(self)
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "<b>PineTunnel Admin Bot</b>\n\n"
            "<b>Commands:</b>\n"
            "/start - Main menu (dashboards)\n"
            "/menu - Same as /start\n"
            "/help - This help message\n"
            "/login - Get web dashboard login code\n\n"
            "Use the inline buttons to navigate dashboards."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def _cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return
        store = getattr(self, "_auth_store", None)
        if store is None:
            await update.message.reply_text("Web dashboard auth not configured.")
            return
        code = await store.issue_code_async(user.id)
        await update.message.reply_text(
            f"Your PineTunnel dashboard login code:\n\n"
            f"{code}\n\n"
            f"Expires in 90 seconds. Do not share it."
        )

    async def _cb_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_admin(update):
            await query.edit_message_text("Admin access required.")
            return

        data = query.data or ""
        if data == "noop":
            return

        try:
            text, keyboard = self._route_callback(data)
            if text:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return
            raise

    def _route_callback(self, data: str) -> tuple[str, list[list[InlineKeyboardButton]]]:
        if data == "nav:main":
            return main_menu(self)

        if data.startswith("nav:"):
            screen = data[4:]
            return self._render_screen(screen)

        if data.startswith("refresh:"):
            screen = data[8:]
            return self._render_screen(screen)

        if data.startswith("page:"):
            parts = data.split(":")
            screen = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 0
            return self._render_screen(screen, page=page)

        if data.startswith("filter:"):
            parts = data.split(":")
            screen = parts[1]
            if screen == "trades":
                side = parts[2] if len(parts) > 2 else "all"
                return trades_screen(self, page=0, side_filter=side)
            if screen == "signals":
                ftype = parts[2] if len(parts) > 2 else "cmd"
                value = parts[3] if len(parts) > 3 else "all"
                return self._render_screen("signals", cmd_filter=value if ftype == "cmd" else "all",
                                           status_filter=value if ftype == "status" else "all")

        if data.startswith("toggle:"):
            key = data[7:]
            self._toggle_setting(key)
            return settings_screen(self)

        if data.startswith("reveal:"):
            parts = data.split(":")
            lic_key = parts[2] if len(parts) > 2 else ""
            if lic_key in self._revealed_keys:
                self._revealed_keys.discard(lic_key)
            else:
                self._revealed_keys.add(lic_key)
            return account_screen(self)

        return main_menu(self)

    def _render_screen(self, screen: str, page: int = 0, cmd_filter: str = "all", status_filter: str = "all") -> tuple[str, list[list[InlineKeyboardButton]]]:
        if screen == "overview":
            return overview_screen(self)
        if screen == "account":
            return account_screen(self, page=page)
        if screen == "trades":
            return trades_screen(self, page=page)
        if screen == "signals":
            return signals_screen(self, page=page, cmd_filter=cmd_filter, status_filter=status_filter)
        if screen == "settings":
            return settings_screen(self)
        if screen == "admin":
            return admin_screen(self)
        return main_menu(self)

    def _toggle_setting(self, key: str):
        if key == "alerts":
            self.alerts_enabled = not self.alerts_enabled
        elif key == "quiet_hours":
            self.quiet_hours["enabled"] = not self.quiet_hours.get("enabled", False)
        elif key in self.notification_prefs:
            self.notification_prefs[key] = not self.notification_prefs[key]
        self._save_bot_settings()

    def _load_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        try:
            if os.path.exists(settings_file):
                with open(settings_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.alerts_enabled = data.get("alerts_enabled", True)
                    prefs = data.get("notifications", {})
                    if isinstance(prefs, dict):
                        self.notification_prefs = {**DEFAULT_NOTIFICATION_PREFS, **prefs}
                    quiet = data.get("quiet_hours", {})
                    if isinstance(quiet, dict):
                        self.quiet_hours = {**DEFAULT_QUIET_HOURS, **quiet}
                    return
        except Exception as e:
            logger.error("Failed to load bot settings: %s", e)
        self.alerts_enabled = True

    def _save_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        try:
            parent = Path(settings_file).parent
            parent.mkdir(parents=True, exist_ok=True)
            data = {
                "alerts_enabled": self.alerts_enabled,
                "notifications": self.notification_prefs,
                "quiet_hours": self.quiet_hours,
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, settings_file)
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error("Failed to save bot settings: %s", e)

    def _cascade_delete_license(self, license_key: str):
        if self.conn_manager:
            self.conn_manager.cleanup_client_state(license_key)
        else:
            self.http_polling_clients.pop(license_key, None)
            self.signal_queues.pop(license_key, None)
        try:
            self.db_manager.delete_signals_by_license(license_key)
        except Exception:
            logger.debug("Failed to delete signals for %s", license_key, exc_info=True)

    async def _log_admin_action(self, user_id: int, username: str, action: str, details: dict):
        user = f"@{username}" if username else str(user_id)
        enriched = dict(details)
        enriched.setdefault("user_id", user_id)
        enriched.setdefault("username", username)

        if self.admin_logger is not None:
            try:
                self.admin_logger.log_activity(action=action, user=user, details=enriched)
                return
            except Exception as e:
                logger.error("Failed to write audit log via admin_logger: %s", e)

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "username": username,
            "action": action,
            "details": details,
        }
        log_file = os.path.join(self.data_dir, "admin_audit.log")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(log_file, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
            os.chmod(log_file, _ADMIN_AUDIT_LOG_MODE)
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)

    async def notify_admin(self, message: str):
        if not self._started or not self.app:
            return
        for admin_id in self.admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id, text=message, parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)

    def _should_notify(self, pref_key: str) -> bool:
        if not self.alerts_enabled:
            return False
        return self.notification_prefs.get(pref_key, False)

    async def on_trade_executed(self, report):
        if not self._should_notify("trade_opened"):
            return
        try:
            await self.notify_admin(
                f"Trade Executed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Side: {report.side}\n"
                f"Volume: {report.volume}"
            )
        except Exception as e:
            logger.error("Trade executed notification error: %s", e)

    async def on_trade_execution_failed(self, report):
        if not self._should_notify("error_alerts"):
            return
        try:
            await self.notify_admin(
                f"Trade Execution Failed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Error: {escape_md(str(report.error))}"
            )
        except Exception as e:
            logger.error("Trade execution failed notification error: %s", e)

    async def on_position_closed(self, report):
        if not self._should_notify("trade_closed"):
            return
        try:
            await self.notify_admin(
                f"Position Closed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Profit: {report.profit}"
            )
        except Exception as e:
            logger.error("Position closed notification error: %s", e)

    async def on_trade_failure(self, license_key: str, error: str):
        if not self._should_notify("error_alerts"):
            return
        await self.notify_admin(
            f"Trade Failure\n"
            f"License: {license_key}\n"
            f"Error: {escape_md(error)}"
        )

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err and "message is not modified" in str(err).lower():
            logger.debug("Benign Telegram edit error: %s", err)
            return
        logger.error("Exception while handling update: %s", err, exc_info=err)
        sanitized = sanitize_error(err) if err else "Unknown error"
        for admin_id in self.admin_ids:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"Bot Error\n\n{sanitized}")
            except Exception:
                logger.error("Failed to send error notification to admin %s", admin_id, exc_info=True)
```

- [ ] **Step 2: Verify __init__.py still works**

Check `apps/server/services/telegram/__init__.py` - it should still export `PineTunnelTelegramBot` from `.bot`. No changes needed unless it imports from deleted modules.

Read `apps/server/services/telegram/__init__.py`. If it imports from `.helpers`, `.keyboards`, `.constants`, or `.mixins`, update to only import from `.bot`.

Expected content should be:
```python
from .bot import PineTunnelTelegramBot

__all__ = ["PineTunnelTelegramBot"]
```

- [ ] **Step 3: Verify Python syntax**

Run: `python -c "import ast; ast.parse(open('apps/server/services/telegram/bot.py').read()); print('OK')"`
Expected: OK

Run: `python -c "import ast; ast.parse(open('apps/server/services/telegram/dashboards.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Run all dashboard tests**

Run: `python -m pytest tests/test_telegram_dashboards.py -v`
Expected: PASS (23 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/services/telegram/bot.py apps/server/services/telegram/__init__.py
git commit -m "feat(telegram): rewrite bot.py with callback router and event hooks"
```

---

### Task 8: Delete old files and old tests

**Files:**
- Delete: `apps/server/services/telegram/constants.py`
- Delete: `apps/server/services/telegram/helpers.py`
- Delete: `apps/server/services/telegram/keyboards.py`
- Delete: `apps/server/services/telegram/mixins/` (entire directory)
- Delete: `tests/test_telegram_helpers.py`

- [ ] **Step 1: Delete old files**

```bash
rm apps/server/services/telegram/constants.py
rm apps/server/services/telegram/helpers.py
rm apps/server/services/telegram/keyboards.py
rm -rf apps/server/services/telegram/mixins/
rm tests/test_telegram_helpers.py
```

- [ ] **Step 2: Verify no broken imports**

Search for any imports from deleted modules:

Run: `grep -r "from.*telegram.*helpers\|from.*telegram.*keyboards\|from.*telegram.*constants\|from.*telegram.*mixins" apps/ tests/`
Expected: No matches (or only matches in __pycache__ which can be ignored)

If any matches found in .py files, fix those imports to point to `dashboards.py` or `bot.py`.

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/ -v --ignore=tests/__pycache__`
Expected: All tests pass (including the 23 new dashboard tests)

- [ ] **Step 4: Verify no non-ASCII in new files**

Run: `grep -rn '[^\x00-\x7F]' apps/server/services/telegram/bot.py apps/server/services/telegram/dashboards.py`
Expected: No matches (all ASCII)

- [ ] **Step 5: Commit**

```bash
git add -A apps/server/services/telegram/ tests/test_telegram_helpers.py
git commit -m "refactor(telegram): delete old mixins, helpers, constants, keyboards

Replaced by dashboards.py (6 screen functions) and rewritten bot.py
(callback router + event hooks). No conversation handlers needed -
all interactions are stateless callback queries."
```

---

### Task 9: Final integration verification

**Files:**
- None modified (verification only)

- [ ] **Step 1: Verify __init__.py imports work**

Run:
```bash
python -c "
import sys
sys.path.insert(0, '.')
# Stub telegram package since it's not installed locally
import types
tg = types.ModuleType('telegram')
tg.helpers = types.ModuleType('telegram.helpers')
tg.helpers.escape_markdown = lambda s, version=1: s
sys.modules['telegram'] = tg
sys.modules['telegram.helpers'] = tg.helpers
from apps.server.services.telegram import PineTunnelTelegramBot
print('Import OK:', PineTunnelTelegramBot.__name__)
"
```
Expected: "Import OK: PineTunnelTelegramBot"

- [ ] **Step 2: Verify lifespan.py still references correct class**

Run: `grep -n "PineTunnelTelegramBot\|telegram_bot\." apps/server/config/lifespan.py | head -10`
Expected: Lines showing `PineTunnelTelegramBot(...)` instantiation and method calls (`telegram_bot.start()`, `telegram_bot.stop()`, `telegram_bot._started`, `telegram_bot.app`). No references to deleted methods like `_show_main_menu`, `_show_monitor_menu`, etc.

- [ ] **Step 3: Verify trade_analytics.py event hooks still match**

Run: `grep -n "telegram_bot\.\(notify_admin\|on_trade_executed\|on_trade_execution_failed\|on_position_closed\|on_trade_failure\)" apps/server/routes/trade_analytics.py`
Expected: 5 matches, all calling methods that exist in the new bot.py

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "test: verify telegram dashboard integration" || echo "Nothing to commit - clean working tree"
```

- [ ] **Step 6: Bump version and publish**

Update `pyproject.toml` version from `7.5.0` to `7.6.0`. Build and publish:

```bash
python -m build --wheel
TWINE_USERNAME=__token__ TWINE_PASSWORD='<token>' python -m twine upload dist/pinetunnel-7.6.0-py3-none-any.whl
git add pyproject.toml
git commit -m "release: 7.6.0 - telegram inline dashboards"
git push origin main
```

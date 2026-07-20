# Telegram Inline Dashboard - Design Spec

## Overview

Replace the existing Telegram bot's menu/monitoring mixins with 5 inline-keyboard dashboard screens that mirror the PineTunnel Next.js dashboard 1:1 (excluding Subscribe page). Admin-only, direct manager access, no subscription/payment logic.

## File Structure

```
apps/server/services/telegram/
  __init__.py          # exports PineTunnelTelegramBot (unchanged)
  bot.py               # ~280 lines - class, lifecycle, callback router, event hooks
  dashboards.py        # ~500 lines - 5 screen functions + admin panel + helpers
```

### Deleted entirely
- `constants.py` - conversation states (no longer needed, no ConversationHandler)
- `helpers.py` - old formatting (replaced by helpers in dashboards.py)
- `keyboards.py` - old respond helper (inlined into bot.py)
- `mixins/auth.py` - admin check + settings persistence (moved into bot.py)
- `mixins/events.py` - trade notification hooks (moved into bot.py)
- `mixins/menu.py` - old menu/licenses/download (replaced by dashboards)
- `mixins/monitoring.py` - old status/connections/logs/security (replaced by dashboards)

### Preserved in bot.py
These are called from other server modules and MUST keep working:
- `notify_admin(message)` - called from trade_analytics.py, reliability.py
- `on_trade_executed(report)` - called from trade_analytics.py:160
- `on_trade_execution_failed(report)` - called from trade_analytics.py:162
- `on_position_closed(report)` - called from trade_analytics.py:237
- `on_trade_failure(license_key, error)` - called from trade_analytics.py:151
- `_is_admin(update)` - admin filter
- `_load_bot_settings()` / `_save_bot_settings()` - alerts toggle persistence
- `_log_admin_action(user_id, username, action, details)` - audit logging
- `_cascade_delete_license(license_key)` - license cleanup
- `_cmd_login(update, context)` - web dashboard auth code issuance (infrastructure, not a dashboard screen)
- `start()` / `stop()` - Application lifecycle (unchanged)

### Constructor signature (unchanged)
```python
PineTunnelTelegramBot(
    token, admin_ids, client_manager, db_manager, data_dir,
    http_polling_clients, signal_queues, conn_manager, ws_manager,
    test_env, auth_store, admin_logger
)
```
No changes to lifespan.py instantiation.

## Main Menu

Replaces /start and /menu. Single message with 6 inline buttons (2x3 grid):

```
PineTunnel Dashboard
--------------------
Licenses: 3/5 active | Connected: 2 | Pending: 0
Alerts: ON | 2026-07-20 14:30 UTC
--------------------
```

Buttons:
```
[Overview]     [Account]
[Trades]       [Signals]
[Settings]     [Admin]
```

Commands: `/start`, `/menu` show main menu. `/help` shows command list.

## Dashboard Screens

### 1. Overview (dashboards.py: overview_screen)

Mirrors Next.js `page.tsx`. Shows aggregate admin-level data across all licenses.

**Text layout:**
```
Overview
--------------------
Account Balance: $5,230.50
  Equity: $5,310.20 | Margin: $120.00
Open Positions: 3
Connection: WebSocket (Connected)
EA Version: 2.1.0
Days Left: 18 (expires 2026-08-07)
--------------------
Recent Signals (last 5):
  [OK] BUY EURUSD - success - 2m ago
  [OK] SELL GBPUSD - success - 15m ago
  [X] CLOSE XAUUSD - failed - 1h ago
--------------------
Recent Trades (last 5):
  [^] EURUSD 0.10 lots - +12.50 - 2026-07-20
  [v] GBPUSD 0.05 lots - -3.20 - 2026-07-19
--------------------
```

**Data sources:**
- `account_stats_latest` (from trade_analytics) - aggregate balance, equity, margin, open positions across all connected accounts
- `client_manager.clients` - license count; first active license for days_left/expires_at display
- `ws_manager.get_connected_license_keys()` + `http_polling_clients` - connection status (aggregate: any connected = Connected)
- `db_manager.execute_query("SELECT ... FROM alert_history ORDER BY timestamp DESC LIMIT 5")` - recent signals across all licenses
- `db_manager.execute_query("SELECT ... FROM trades ORDER BY timestamp DESC LIMIT 5")` - recent trades across all licenses
- EA version from first connected client's client_info

**Keyboard:**
```
[Refresh]   [Account]
[Trades]    [Signals]
[Back to Menu]
```

### 2. Account & License (dashboards.py: account_screen)

Mirrors Next.js `account/page.tsx`. Shows all licenses with keys, secrets, expiry, download.

**Text layout (per license, 1 per page if multiple):**
```
Account & License (1/3)
--------------------
License: abcd1234... (reveal)
Name: Test Account
Secret: abcd**** (reveal)
Status: Active | Expires: 2026-08-07
Days Left: 18
Connection: WebSocket
EA Version: 2.1.0
Account Login: 12345
Server: ICMarkets-Demo
--------------------
```

**Data sources:**
- `client_manager.clients` - all licenses with name, status, expires_at, secret_key, connection, user_id
- `account_stats_latest` - login, server
- `generate_download_url(user_id, "mt5")` / `"mt4"` - EA download links

**Keyboard:**
```
[Reveal Key]  [Reveal Secret]
[Download MT5]  [Download MT4]
[Prev]  [Page 1/3]  [Next]
[Back to Menu]
```

- Reveal buttons toggle masking (callback stores revealed state in bot instance dict)
- Download buttons are URL buttons (open browser)
- Pagination: 1 license per page (full detail view)

### 3. Trade History (dashboards.py: trades_screen)

Mirrors Next.js `trades/page.tsx`. Paginated list with side filter.

**Text layout:**
```
Trade History (All sides)
--------------------
8 of 42 records | Page 1/6
--------------------
2026-07-20 14:30 | EURUSD | BUY  | 0.10 | +12.50
2026-07-20 10:15 | GBPUSD | SELL | 0.05 | -3.20
2026-07-19 16:00 | XAUUSD | BUY  | 0.20 | +45.00
...
--------------------
```

**Data sources:**
- `db_manager.execute_query("SELECT timestamp, symbol, side, volume, profit FROM trades ORDER BY timestamp DESC LIMIT 8 OFFSET :off")` - paginated trades
- Count query for total

**Keyboard:**
```
[All] [Buy] [Sell] [Close]
[Prev]  [Page 1/6]  [Next]
[Back to Menu]
```

- Filter buttons highlighted (active filter marked with [OK])
- 8 records per page (Telegram message length constraint)
- Callback data: `filter:trades:buy`, `page:trades:2`

### 4. Signal Log (dashboards.py: signals_screen)

Mirrors Next.js `signals/page.tsx`. Paginated list with command + status filters.

**Text layout:**
```
Signal Log (All commands / All status)
--------------------
8 of 156 records | Page 1/20
--------------------
2026-07-20 14:30 | BUY   | EURUSD | success
2026-07-20 10:15 | SELL  | GBPUSD | success
2026-07-19 16:00 | CLOSE | XAUUSD | failed
...
--------------------
```

**Data sources:**
- `db_manager.execute_query("SELECT timestamp, action, symbol, response_code FROM alert_history ORDER BY timestamp DESC LIMIT 8 OFFSET :off")` - paginated signals
- Count query for total, with filter WHERE clauses

**Keyboard:**
```
[All Cmd] [Buy] [Sell] [Close]
[All Status] [Success] [Failed]
[Prev]  [Page 1/20]  [Next]
[Back to Menu]
```

- Two filter rows: command + status
- Active filters marked with [OK]
- Callback data: `filter:signals:cmd:buy`, `filter:signals:status:failed`, `page:signals:3`

### 5. Settings (dashboards.py: settings_screen)

Mirrors Next.js `settings/page.tsx`. Inline toggle buttons for notification preferences.

**Text layout:**
```
Settings
--------------------
Notification Preferences:
  [OK] Trade Opened      - ON
  [OK] Trade Closed      - ON
  [OK] Error Alerts      - ON
  [  ] Connection Changes - OFF
  [  ] Signal Received   - OFF
--------------------
Quiet Hours:
  [  ] Disabled
--------------------
Master Alerts Toggle: ON
```

**Data sources:**
- `self.notification_prefs` (dict stored in bot_settings.json)
- `self.quiet_hours` (dict stored in bot_settings.json)
- `self.alerts_enabled` (existing master toggle)

**Keyboard:**
```
[Trade Opened: ON]   [Trade Closed: ON]
[Error Alerts: ON]   [Conn Changes: OFF]
[Signal Received: OFF]
[Quiet Hours: OFF]
[Alerts Master: ON]
[Back to Menu]
```

- Each toggle button flips the setting and refreshes the screen
- Quiet hours: if toggled ON, show preset time buttons:
  `[22:00-08:00] [23:00-07:00] [00:00-06:00]`
- No free text input (Telegram inline limitation) - preset times cover common cases
- Settings persisted to `bot_settings.json` via `_save_bot_settings()` (extended to store prefs + quiet hours)

**Settings schema (bot_settings.json):**
```json
{
  "alerts_enabled": true,
  "notifications": {
    "trade_opened": true,
    "trade_closed": true,
    "error_alerts": true,
    "connection_changes": false,
    "signal_received": false
  },
  "quiet_hours": {
    "enabled": false,
    "start": "22:00",
    "end": "08:00"
  }
}
```

The existing event hooks (on_trade_executed, etc.) check both `alerts_enabled` AND the specific notification pref before sending. E.g., `on_trade_executed` checks `alerts_enabled and notification_prefs["trade_opened"]`.

### 6. Admin Panel (dashboards.py: admin_screen)

Mirrors Next.js `admin/page.tsx`. System-wide stats.

**Text layout:**
```
Admin Panel
--------------------
Total Licenses: 5
Active Licenses: 3
Connected EAs: 4
Signals (7d): 156
--------------------
Signal Queue Stats (7d):
  Total: 156 | Success: 148
  Failed: 8 | Rate: 94.9%
--------------------
EA Connections:
  WebSocket: 3 | HTTP Polling: 1
  DB Unique Licenses: 4
--------------------
Blocked IPs: 2
  192.168.1.1 (failed auth, 300s left)
  10.0.0.1 (rate limit, 120s left)
--------------------
```

**Data sources:**
- `client_manager.clients` - total/active license count
- `ws_manager` + `http_polling_clients` - connected EA count
- `db_manager.execute_query("SELECT COUNT(*)... FROM alert_history WHERE ...")` - webhook stats (7d)
- `failed_attempt_tracker.get_statistics()` - blocked IPs from failed auth
- `rate_limiter.get_statistics()` - blocked IPs from rate limiter
- `db_manager.execute_query("SELECT COUNT(DISTINCT license_key) FROM ...")` - DB unique licenses

**Keyboard:**
```
[Refresh]
[Security Detail]
[Back to Menu]
```

Security Detail shows the full security center (from old monitoring.py `_show_security`): security headers, TradingView IP allowlist, failed attempts, rate limit hits.

## Callback Data Encoding

All callback data strings (max 64 bytes, Telegram limit):

| Pattern | Example | Description |
|---------|---------|-------------|
| `nav:<screen>` | `nav:overview` | Navigate to screen |
| `nav:main` | `nav:main` | Back to main menu |
| `page:<screen>:<n>` | `page:trades:2` | Pagination |
| `filter:<screen>:<type>:<value>` | `filter:signals:cmd:buy` | Filter toggle |
| `toggle:<key>` | `toggle:trade_opened` | Settings toggle |
| `reveal:<type>:<key>` | `reveal:secret:abcd1234` | Reveal license key/secret |
| `refresh:<screen>` | `refresh:admin` | Refresh screen |

Screen names: `overview`, `account`, `trades`, `signals`, `settings`, `admin`

## Router Design (bot.py)

Single `CallbackQueryHandler` with pattern `.*` catches all. Router parses callback data:

```python
async def _cb_handler(self, update, context):
    query = update.callback_query
    await query.answer()
    if not self._is_admin(update):
        await query.edit_message_text("Admin access required.")
        return
    data = query.data
    # Parse: nav:, page:, filter:, toggle:, reveal:, refresh:
    # Route to appropriate dashboards.py function
    # Each function returns (text, keyboard) and we edit the message
```

No ConversationHandler - all interactions are stateless callback queries. Filter/pagination state is encoded in callback data, not conversation state.

## Sensitive Data Handling

- License keys: masked to first 8 chars + `...` by default. Full key shown only on explicit "Reveal Key" button press.
- Secret keys: masked to first 4 chars + `****`. Full secret shown only on "Reveal Secret" button.
- Reveal state: stored in `self._revealed_keys` dict (per-admin, cleared on bot restart).
- Download URLs: generated via `generate_download_url(user_id, platform)` - admin-scoped, not exposed in message text (URL buttons only).
- No tokens, passwords, or API keys shown in any dashboard screen.
- Error messages sanitized via `_sanitize_error()` (strips file paths).

## ASCII Compliance

All user-facing strings in dashboards.py and bot.py use ASCII only (per AGENTS.md). No em dashes, smart quotes, emoji, or Unicode symbols. Status indicators use `[OK]`, `[X]`, `[!]`, `[^]`, `[v]` instead of emoji.

## What's NOT Included

- Subscribe page (explicitly excluded)
- Payment/subscription logic (explicitly excluded)
- License CRUD (add/edit/delete) - the Next.js dashboard doesn't have this; it's view-only
- Support chat logs (from admin page) - requires AI support infrastructure not present in PineTunnel-public
- Free-text input for settings (quiet hours times) - Telegram inline limitation; presets used instead
- Telegram Mini App / WebApp integration - inline keyboards only

## Testing

Manual testing via Telegram bot commands:
1. `/start` - main menu appears with 6 buttons
2. Tap each button - corresponding dashboard renders
3. Pagination - tap Prev/Next on trades/signals
4. Filters - tap Buy/Sell/Close on trades, command/status on signals
5. Settings toggles - tap each, verify state flips and persists
6. Reveal - tap Reveal Key, verify full key shows; tap again to mask
7. Admin panel - verify all KPIs render
8. `/help` - shows command list
9. Event hooks - trigger a trade, verify notification arrives (if alerts + trade_opened enabled)
10. Non-admin user - send /start, verify "Not authorized"

# EA Input Parameters

This document covers all Expert Advisor input parameters for PineTunnel. Press `F7` on the chart to access these settings.

## Input Groups

The EA inputs are organized into 9 sections:

1. [License](#1-license)
2. [Syntax](#2-syntax)
3. [Input](#3-input)
4. [General](#4-general)
5. [Dashboard](#5-dashboard)
6. [Account](#6-account)
7. [WebSocket](#7-websocket)
8. [Auto-Update](#8-auto-update)
9. [Miscellaneous](#miscellaneous)

---

## 1. License

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `InpLicenseID` | string | "" | Your license ID (from the server admin) |

Your license ID is assigned by the server administrator. It identifies your account and is required for the EA to connect to the server.

---

## 2. Syntax

These settings control how the EA interprets parameter values in alert messages.

### InpTargetType

Controls how `sl=` and `tp=` values are interpreted in alert messages.

| Option | Description | Example |
|--------|-------------|---------|
| Pips | SL/TP values are in pips (broker-dependent) | `sl=300` = 300 pips |
| Price | SL/TP values are absolute prices | `sl=1.0850` = price level |
| Percentage | SL/TP values are percentage of current price | `sl=0.5` = 0.5% from entry |

**Note:** Use `sl_price=` or `sl_points=` in alert messages to override this setting per-signal.

### InpVolumeType

Controls how `risk=` values are interpreted in alert messages.

| Option | Description | Example |
|--------|-------------|---------|
| Lots | risk= is treated as fixed lot size | `risk=0.10` = 0.10 lots |
| Dollar | risk= is treated as dollar amount | `risk=100` = $100 position |
| Balance % Lots | risk= is % of balance converted to lots | `risk=5` = 5% of balance as lots |
| Balance % Loss | risk= is % of balance to risk based on SL distance | `risk=1` = 1% of balance |
| Equity % Loss | risk= is % of equity to risk based on SL distance | `risk=1` = 1% of equity |
| Balance % Margin | risk= is % of balance used as margin | `risk=5` = 5% of balance as margin |
| Equity % Margin | risk= is % of equity used as margin | `risk=5` = 5% of equity as margin |

**Note:** Use `lots=`, `usd=`, `risk_bal_pct=`, `risk_eq_pct=`, `margin_bal_pct=`, or `margin_eq_pct=` in alert messages to override this setting per-signal.

### InpPendingOrderEntry

Controls how `pending=` values are interpreted for pending orders.

| Option | Description | Example |
|--------|-------------|---------|
| Pips from Market | Entry is N pips from current market price | `pending=50` = 50 pips away |
| Price from Signal | Entry is an absolute price from the alert | `pending=1.0900` = price level |
| Percentage from Market | Entry is N% from current market price | `pending=0.5` = 0.5% away |

**Note:** Use `entry_price=` or `entry_points=` in alert messages to override this setting per-signal.

### InpAccountFilterBasis

Controls how `acc_filter=` values are interpreted.

| Option | Description |
|--------|-------------|
| Balance | Filter based on account balance |
| Equity | Filter based on account equity |
| Free Margin | Filter based on free margin |
| Margin Percentage | Filter based on margin level percentage |

When `acc_filter=` is included in an alert, the EA checks if the specified account metric meets the threshold. If not, the trade is skipped.

---

## 3. Input

These settings provide default values when alert messages don't include specific parameters.

### InpSetting (Parameter Source)

Controls whether signal parameters or EA defaults are used.

| Option | Description |
|--------|-------------|
| Use Signal Values | Use parameters from the alert message only |
| Use EA Defaults | Use EA input values (below) only |
| EA SL/TP, Signal Volume | SL and TP from EA, volume from signal |
| Signal SL/TP, EA Volume | SL and TP from signal, volume from EA |

### Default Values

| Input | Default | Description |
|-------|---------|-------------|
| `InpStopLoss` | 0.0 | Default stop loss (0 = no SL) |
| `InpTakeProfit` | 0.0 | Default take profit (0 = no TP) |
| `InpRisk` | 1.0 | Default risk/volume value |

These are only used when `InpSetting` is set to use EA defaults (or the mixed modes above).

---

## 4. General

### InpPyramiding

Controls whether multiple positions can be opened on the same symbol.

| Option | Description |
|--------|-------------|
| On | Allow multiple positions in same direction |
| On - Only If In Profit | Allow new position only if existing is in profit |
| Off - One Position Per Symbol | Only one position per symbol (new signal rejected if one exists) |
| Off - One Buy and One Sell (Hedge) | Allow one buy AND one sell per symbol (hedge mode) |

### InpCloseOnReverse

Controls behavior when an opposite signal arrives while a position is open.

| Option | Description |
|--------|-------------|
| On - Close and Reverse | Close existing position, then open new one |
| On - Close Only | Close existing position, don't open new |
| Off | Ignore opposite signal if position exists |

### InpHiddenSLTP (Hidden SL/TP)

When enabled, the EA stores SL/TP in memory instead of placing them on the broker. The broker sees offset values far from the real levels. This prevents broker stop-hunting.

| Option | Description |
|--------|-------------|
| Off | Place real SL/TP on broker (normal) |
| On | Hide real SL/TP, place offset values on broker |

**Hidden SL/TP Offset:** `InpHiddenOffset` (default: 100 points) - How far the visible SL/TP is placed from the real level.

**Compatibility:** Hidden SL/TP is NOT compatible with breakeven, trailing stops, or ATR trailing. If both are enabled, the EA logs a warning and uses hidden SL/TP.

### InpPartialClosePercentage

Default percentage for `close_long_pct` and `close_short_pct` commands.

| Option | Value |
|--------|-------|
| 10% | 10 |
| 20% | 20 |
| 25% | 25 |
| 34% | 34 |
| 50% | 50 |

### Position Limits

| Input | Default | Description |
|-------|---------|-------------|
| `InpMaxOpenPositions` | 0 (unlimited) | Maximum concurrent positions across all symbols |
| `InpMaxOpenPositionsPerSymbol` | 0 (unlimited) | Maximum positions per symbol |
| `InpMaxUniqueSymbols` | 0 (unlimited) | Maximum different symbols with open positions |

### InpEnableSmartMarket

When enabled, market orders are converted to near-market limit orders for potential price improvement.

| Input | Default | Description |
|-------|---------|-------------|
| `InpEnableSmartMarket` | true | Convert market orders to limit orders for price improvement |
| `InpNearMarketPoints` | 10 | How many points from market to place the limit order |
| `InpEntryLimitTimeoutMs` | 300 | Timeout in ms before falling back to market order |
| `InpEnableExitLimit` | false | Use limit orders for exits (maker rebate) |
| `InpExitLimitTimeoutMs` | 500 | Timeout in ms before falling back to market close |

---

## 5. Dashboard

| Input | Default | Description |
|-------|---------|-------------|
| `InpFontSize` | 10 | Dashboard font size (6-14) |
| `InpShowDashboard` | true | Show on-chart dashboard |

The dashboard shows: connection status, server URL, license ID, account info, positions, daily P/L.

---

## 6. Account

### Daily Profit/Loss Limits

| Input | Default | Description |
|-------|---------|-------------|
| `InpDailyTimezoneGMT` | 0 | GMT offset for daily reset (-12 to +14) |
| `InpDailyProfit` | 0.0 | Daily profit target (0 = disabled) |
| `InpDailyLoss` | 0.0 | Daily loss limit (0 = disabled) |

### Daily Action

| Option | Description |
|--------|-------------|
| Halt EA | Stop EA from processing new signals |
| Close All Positions | Close all open positions |
| Close All and Halt EA | Close all positions and halt EA |

### Cumulative Profit/Loss Limits

| Input | Default | Description |
|-------|---------|-------------|
| `InpCumulativeProfit` | 0.0 | Cumulative profit target (0 = disabled) |
| `InpCumulativeLoss` | 0.0 | Cumulative loss limit (0 = disabled) |

### Cumulative Action

| Option | Description |
|--------|-------------|
| Halt for Day | Stop EA until next day |
| Halt Persistently | Stop EA until manually resumed |
| Close All Positions | Close all open positions |
| Close All and Halt Day | Close all and halt until next day |
| Close All and Halt Persistent | Close all and halt until manually resumed |

**Resuming after halt:** Re-attach the EA, press the AutoTrading button, or send an `ea_on` command.

---

## 7. WebSocket

| Input | Default | Description |
|-------|---------|-------------|
| `InpConnectionMode` | Hybrid | Connection mode (Hybrid/WebSocket/Polling) |
| `InpServerURL` | https://your-server.com | Your PineTunnel server URL (HTTPS required) |
| `InpWSPollIntervalMs` | 100 | Polling interval in ms (for polling mode) |
| `InpWSHeartbeatSec` | 30 | Heartbeat ping interval in seconds |
| `InpWSMaxReconnectAttempts` | 0 (unlimited) | Max reconnect attempts on disconnect |
| `InpWSLogLevel` | 0 | DLL log level (0=off, 1=error, 2=info, 3=debug) |
| `InpStatsInterval` | 60 | Account stats reporting interval in seconds (0=off) |

### InpConnectionMode

| Option | Description |
|--------|-------------|
| Hybrid | Use WebSocket for real-time, fall back to polling if WS fails |
| WebSocket | WebSocket only (no polling fallback) |
| Polling | HTTP polling only (no WebSocket) |

---

## 8. Auto-Update

| Input | Default | Description |
|-------|---------|-------------|
| `InpAutoUpdate` | true | Automatically check for EA updates |
| `InpAutoUpdateDLL` | true | Automatically update the DLL |
| `InpAutoRestart` | true | Auto-restart EA after update |
| `InpUpdateCheckInterval` | 3600 | Update check interval in seconds |
| `InpAuditInterval` | 300 | Audit report interval in seconds (0=off) |

---

## Miscellaneous

| Input | Default | Description |
|-------|---------|-------------|
| `InpMagicNumber` | 1001 | Magic number for trade identification (1001-1005) |
| `InpMagicRestriction` | On | On: EA only manages its own trades. Off: manages all trades on symbol |
| `InpStartTime` | 00:00 | EA active start time (broker server time) |
| `InpEndTime` | 23:59 | EA active end time (broker server time) |
| `InpPrefix` | "" | Symbol prefix (auto-prepended to symbol names) |
| `InpSuffix` | "" | Symbol suffix (auto-appended to symbol names) |
| `InpEnableSignalQueue` | true | Queue signals when market is closed or EA is halted |
| `InpEnableSLTPVerify` | false | Verify SL/TP placement after order (prevents naked positions) |
| `InpSLTPVerifyRetries` | 3 | Number of SL/TP verification retries |

# PineTunnel Command Reference

All commands are sent as TradingView alert messages in CSV format:

```
LICENSE_ID,COMMAND,SYMBOL,parameters...,comment=X,secret=Y
```

- `LICENSE_ID` - Your license ID (from the server admin)
- `COMMAND` - One of the commands below (case-insensitive)
- `SYMBOL` - Trading symbol (e.g. `EURUSD`, `XAUUSD`, `BTCUSD`)
- `parameters` - Comma-separated key=value pairs (order doesn't matter)
- `comment=X` - Optional trade comment
- `secret=Y` - Your secret key (required for server-side verification)
- `nm=true` - Optional: Near-Market flag (enables limit order conversion for price improvement)

## Volume Parameters

Use ONE of these to specify position size:

| Parameter | Description | Example |
|-----------|-------------|---------|
| `lots=0.10` | Fixed lot size | `lots=0.10` |
| `usd=100` | Fixed dollar amount | `usd=100` |
| `risk=1` | Volume (interpretation depends on InpVolumeType) | `risk=1` |
| `risk_bal_pct=1` | % of balance to risk (SL-based) | `risk_bal_pct=1` |
| `risk_eq_pct=2` | % of equity to risk (SL-based) | `risk_eq_pct=2` |
| `margin_bal_pct=5` | % of balance as margin | `margin_bal_pct=5` |
| `margin_eq_pct=5` | % of equity as margin | `margin_eq_pct=5` |

## Stop Loss / Take Profit Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `sl=1.0850` | Stop loss as absolute price | `sl=1.0850` |
| `sl=50` | Stop loss as points (broker-dependent) | `sl=50` |
| `tp=1.0950` | Take profit as absolute price | `tp=1.0950` |
| `tp=100` | Take profit as points | `tp=100` |

## Market Orders

| Command | Description | Example |
|---------|-------------|---------|
| `buy` | Market buy | `LICENSE,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=Y` |
| `sell` | Market sell | `LICENSE,sell,EURUSD,risk=1,sl=1.0950,secret=Y` |

## Pending Orders

| Command | Description | Example |
|---------|-------------|---------|
| `buy_stop` | Buy stop order (entry above current price) | `LICENSE,buy_stop,EURUSD,pending=1.0900,lots=0.10,sl=1.0850,tp=1.0950,secret=Y` |
| `buy_limit` | Buy limit order (entry below current price) | `LICENSE,buy_limit,EURUSD,pending=1.0800,lots=0.10,secret=Y` |
| `sell_stop` | Sell stop order (entry below current price) | `LICENSE,sell_stop,EURUSD,pending=1.0800,lots=0.10,secret=Y` |
| `sell_limit` | Sell limit order (entry above current price) | `LICENSE,sell_limit,EURUSD,pending=1.0900,lots=0.10,secret=Y` |

Required parameter: `pending=PRICE` (entry price for the pending order)

## Close Positions

| Command | Description | Example |
|---------|-------------|---------|
| `close_long` | Close all long positions for symbol | `LICENSE,close_long,EURUSD,secret=Y` |
| `close_short` | Close all short positions for symbol | `LICENSE,close_short,EURUSD,secret=Y` |
| `close_all` | Close all positions for symbol | `LICENSE,close_all,EURUSD,secret=Y` |

### Partial Close

| Command | Description | Example |
|---------|-------------|---------|
| `close_long_pct` | Close X% of long positions | `LICENSE,close_long_pct,EURUSD,risk=50,secret=Y` |
| `close_short_pct` | Close X% of short positions | `LICENSE,close_short_pct,EURUSD,risk=50,secret=Y` |
| `close_long_vol` | Close X lots of long positions | `LICENSE,close_long_vol,EURUSD,risk=0.05,secret=Y` |
| `close_short_vol` | Close X lots of short positions | `LICENSE,close_short_vol,EURUSD,risk=0.05,secret=Y` |

`risk=` means percentage (1-100) for `pct` commands, or lot size for `vol` commands.

## Modify SL/TP

| Command | Description | Example |
|---------|-------------|---------|
| `sltp_long` | Modify SL/TP on all long positions | `LICENSE,sltp_long,EURUSD,sl=1.0800,tp=1.1000,secret=Y` |
| `sltp_short` | Modify SL/TP on all short positions | `LICENSE,sltp_short,EURUSD,sl=1.1000,secret=Y` |
| `sltp_buy_stop` | Modify SL/TP on buy stop orders | `LICENSE,sltp_buy_stop,EURUSD,sl=1.0800,secret=Y` |
| `sltp_buy_limit` | Modify SL/TP on buy limit orders | `LICENSE,sltp_buy_limit,EURUSD,sl=1.0800,secret=Y` |
| `sltp_sell_stop` | Modify SL/TP on sell stop orders | `LICENSE,sltp_sell_stop,EURUSD,sl=1.1000,secret=Y` |
| `sltp_sell_limit` | Modify SL/TP on sell limit orders | `LICENSE,sltp_sell_limit,EURUSD,sl=1.1000,secret=Y` |

You can specify `sl=`, `tp=`, or both. Omitted parameters are not modified.

## Cancel Pending Orders

| Command | Description | Example |
|---------|-------------|---------|
| `cancel_long` | Cancel all long pending orders for symbol | `LICENSE,cancel_long,EURUSD,secret=Y` |
| `cancel_short` | Cancel all short pending orders for symbol | `LICENSE,cancel_short,EURUSD,secret=Y` |

## Combined Actions (Close + Open)

These commands close existing positions in one direction and open new ones:

| Command | Description |
|---------|-------------|
| `close_long_buy` | Close longs, then open a new long |
| `close_long_sell` | Close longs, then open a new short |
| `close_short_buy` | Close shorts, then open a new long |
| `close_short_sell` | Close shorts, then open a new short |
| `close_all_buy` | Close all, then open a new long |
| `close_all_sell` | Close all, then open a new short |

Example: `LICENSE,close_long_buy,EURUSD,lots=0.10,sl=1.0850,secret=Y`

## Cancel + Place (Cancel pending, place new)

| Command | Description |
|---------|-------------|
| `cancel_long_buy_stop` | Cancel long pending, place buy stop |
| `cancel_long_buy_limit` | Cancel long pending, place buy limit |
| `cancel_short_sell_stop` | Cancel short pending, place sell stop |
| `cancel_short_sell_limit` | Cancel short pending, place sell limit |

Example: `LICENSE,cancel_long_buy_stop,EURUSD,pending=1.0900,lots=0.10,secret=Y`

## Full Example (TradingView Alert Message)

```
ABC123DEF456,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,comment=myTrade,secret=XYZ789,nm=true
```

This will:
1. Buy 0.10 lots of EURUSD at market
2. Set SL at 1.0850 and TP at 1.0950
3. Attach comment "myTrade"
4. Use Near-Market flag (limit order conversion for price improvement)

## EA Management

| Command | Description | Example |
|---------|-------------|---------|
| `ea_on` | Resume EA signal processing | `LICENSE,ea_on,ea_on,secret=Y` |
| `ea_off` | Halt EA (stop processing new signals) | `LICENSE,ea_off,ea_off,secret=Y` |
| `close_all_off` | Close all positions and halt EA | `LICENSE,close_all_off,close_all_off,secret=Y` |

## Command Aliases

| Alias | Maps to |
|-------|---------|
| `long` | `buy` |
| `bull` | `buy` |
| `bullish` | `buy` |
| `short` | `sell` |
| `bear` | `sell` |
| `bearish` | `sell` |

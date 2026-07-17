# FAQ

## Getting Started

### What is PineTunnel?

PineTunnel is an open-source bridge that relays TradingView webhook alerts to MetaTrader 4/5 for automated order execution. You deploy the server on any VPS or cloud instance, install the EA on your MT4/MT5 terminal, and configure TradingView alerts to send signals to your server.

### Do I need to leave TradingView or my browser open?

No. Only your MetaTrader terminal needs to be running. TradingView alerts are server-side - they fire even when your browser is closed.

### Do I need a Windows cloud instance?

The PineTunnel server runs on any VPS or cloud instance (Linux). MetaTrader requires Windows, so for lowest latency, run your MetaTrader terminal on a Windows cloud instance near your broker's server.

### What TradingView plan do I need?

Any paid plan (Essential, Plus, Premium, Expert, Elite, or Ultimate) that supports webhook alerts. The free plan does NOT support webhooks.

### How long does setup take?

Approximately 30 minutes: 10 minutes for the server, 10 minutes for the EA, 10 minutes for TradingView alerts.

---

## Latency

### What is the typical latency?

End-to-end latency depends on your infrastructure:

| Component | Typical Latency |
|-----------|----------------|
| TradingView to your server | 50-200ms (varies, TradingView-controlled) |
| Server processing (parse + queue + sign) | 3-15ms |
| WebSocket delivery to EA | 5-30ms (depends on network) |
| EA processing (DLL + OrderSend) | 10-100ms (depends on Windows instance specs) |
| Broker execution | 1-50ms (depends on broker server proximity) |
| **Total** | **~70-400ms** |

For lowest latency: run MetaTrader on a Windows cloud instance in the same datacenter as your broker.

---

## Price Discrepancies

### Why does the price in TradingView differ from MetaTrader?

TradingView and your broker use different data feeds. Small price differences are normal and expected. Causes include:

1. **Different liquidity providers** - TradingView aggregates from multiple sources
2. **Spread** - MetaTrader shows bid/ask, TradingView shows mid-price
3. **Execution delay** - By the time your signal arrives, price has moved
4. **Timeframe differences** - Candle close prices may differ slightly

**Recommendation:** Use `sl_points=` and `tp_points=` (relative to entry) instead of `sl_price=` and `tp_price=` (absolute prices) to avoid discrepancy issues.

---

## Repainting

### What is repainting and does PineTunnel handle it?

Repainting is when an indicator changes its past values after new data arrives. This is a TradingView/PineScript issue, not a PineTunnel issue.

**Types of repainting:**

| Type | Description | Impact |
|------|-------------|--------|
| Widespread | Indicators that recalculate on every tick (e.g. ZigZag) | Signals may appear/disappear |
| Misleading | Indicators that use future data (e.g. Centered MA) | Backtests look great, live fails |
| Unavoidable | Indicators that update on candle close | Normal, signals fire on confirmed bar |

**Recommendation:** Use `once_per_bar_close` as your TradingView alert trigger to avoid repainting issues. PineTunnel's idempotent queue also prevents duplicate trades from repaint-induced multiple alerts.

---

## Volume and Position Sizing

### How does PineTunnel calculate lot size?

PineTunnel supports 6 volume modes:

| Mode | Parameter | Description |
|------|-----------|-------------|
| Fixed lots | `lots=0.10` | Exactly 0.10 lots |
| Dollar amount | `usd=100` | $100 position (converted to lots using margin rate) |
| Balance % risk | `risk_bal_pct=1` | Risk 1% of balance based on SL distance |
| Equity % risk | `risk_eq_pct=1` | Risk 1% of equity based on SL distance |
| Balance % margin | `margin_bal_pct=5` | Use 5% of balance as margin |
| Equity % margin | `margin_eq_pct=5` | Use 5% of equity as margin |

**Risk-based sizing requires a stop loss.** Without SL, the EA cannot calculate the risk per lot.

### What if my calculated lot size is below broker minimum?

The EA automatically adjusts to broker constraints:
- Minimum: `SymbolInfoDouble(SYMBOL_VOLUME_MIN)` (usually 0.01)
- Maximum: `SymbolInfoDouble(SYMBOL_VOLUME_MAX)`
- Step: `SymbolInfoDouble(SYMBOL_VOLUME_STEP)` (usually 0.01)

If the calculated size is below minimum, the EA uses the minimum. If above maximum, it uses the maximum.

---

## MetaTrader Terminal Navigation

### How to check the Symbols List

Press `Ctrl+U` in MetaTrader. This shows all available symbols. The symbol name in your alert MUST match exactly (case-sensitive).

### How to check the Experts tab

In MetaTrader, open the Toolbox panel (bottom). Click the "Experts" tab. This shows EA log messages including signal processing, errors, and connection status.

### How to check the Journal tab

In the Toolbox panel, click the "Journal" tab. This shows system-level events including DLL load errors and order execution details.

### How to access historical logs after closing MetaTrader

1. File > Open Data Folder
2. Navigate to `MQL5/Logs/` (MT5) or `MQL4/Logs/` (MT4) or `logs/` (MT4)
3. Log files are named by date (e.g. `20260717.log`)

### How to check Global Variables

Press `F3` or Tools > Global Variables. PineTunnel stores some state here for persistence across restarts.

---

## TradingView Limits

### "Triggered too often" error

TradingView limits webhook alerts to approximately 15 per 3 minutes per alert. If your strategy fires more frequently:

1. Use `once_per_bar_close` trigger instead of `once_per_bar`
2. Increase your timeframe
3. Create separate alerts for different conditions
4. PineTunnel's idempotent queue will still deduplicate if TradingView retries

---

## Data Privacy

### What data does PineTunnel collect?

PineTunnel runs on your own VPS or cloud instance, so you control all data. The server processes:

- Alert messages (your trading signals)
- License IDs and secret keys (for authentication)
- Trade execution reports (from the EA)
- Account statistics (balance, equity, positions - for monitoring)

**No data leaves your server.** There is no telemetry, no analytics, no phone-home. Everything stays on your infrastructure.

### Can I encrypt my TradingView alerts?

Yes. PineTunnel supports RC4-drop256 signal encryption. Set `SIGNAL_ENCRYPTION_KEY` in your server's `.env` and the same key in your PineScript's `encKey` input. The server auto-detects encrypted vs plaintext signals. See [TRADINGVIEW_ALERTS.md](TRADINGVIEW_ALERTS.md#6-signal-encryption-rc4) for details.

### How do I get HTTPS for my server?

Use Cloudflare tunnel:

```bash
pinetunnel setup-cloudflare  # Quick tunnel (no domain) or DNS setup (with domain)
```

This provides TLS termination and DDoS protection without opening ports on your VPS.

### How do I keep the server running 24/7?

Install PineTunnel as an OS service so it survives reboots:

```bash
pinetunnel install-service  # systemd (Linux) or launchd (macOS)
```

For MetaTrader, run it on a Windows VPS with "Allow Algorithmic Trading" enabled.

---

## Errors

### Common MT4/MT5 Error Codes

| Code | Name | Cause | Fix |
|------|------|-------|-----|
| 130 | Invalid stops | SL/TP too close to market price | Increase SL/TP distance or check broker stop level |
| 131 | Invalid trade volume | Lot size below minimum or above maximum | Check broker min/max lot size |
| 132 | Market closed | Market is closed for this symbol | Check market hours or enable signal queue |
| 133 | Trade is disabled | Trade not allowed on this account | Check account permissions |
| 134 | Not enough money | Insufficient margin | Reduce position size or add funds |
| 135 | Price changed | Price moved between order and execution | Normal, EA retries automatically |
| 136 | Off quotes | No price available | Check connection, wait for market to stabilize |
| 138 | Requote | Broker requoted the price | Normal, EA retries with new price |
| 4106 | Unknown symbol | Symbol not in Market Watch | Check symbol name (case-sensitive), add to Market Watch |
| 4109 | Trade not allowed | AutoTrading is disabled | Enable AutoTrading button (green) |
| 4752 | Trade request sending failed | DLL connection issue | Check WebSocket connection status |
| 4756 | Trade request not completed | Order timed out | Check network latency, broker server status |

### EA shows "Disconnected"

1. Check `InpServerURL` uses `https://` (not `http://`)
2. Verify server is running: `curl https://your-server.com/health`
3. Check DLL imports are enabled (Tools > Options > Expert Advisors)
4. Check DLL is in the correct folder (MQL5/Libraries or MQL4/Libraries)
5. Check firewall allows outbound WebSocket connections

### EA not taking trades

1. Check AutoTrading button is green
2. Check EA Inputs > InpLicenseID is correct
3. Check Experts tab for error messages
4. Check InpMaxOpenPositions isn't set too low (0 = unlimited, which allows all trades)
5. Check InpStartTime/InpEndTime covers current broker server time
6. Check daily/cumulative P/L limits aren't triggered

### Webhook returns 401/403

1. Check `secret=` parameter in alert matches the license's secret_key configured on the server
2. Check license ID is valid and active on the server
3. Check the Content-Type header is correct

### Webhook returns 429

You're being rate limited. Wait 1 minute or increase `RATE_LIMIT_REQUESTS_PER_MINUTE` env var.

---

## Multi-Account

### Can I run multiple MetaTrader terminals with one PineTunnel server?

Yes. Each terminal gets its own license ID. The server supports unlimited connections. One webhook alert can be routed to multiple EAs if they share the same license ID.

### Can I run the EA on multiple charts?

No need. One EA instance on one chart handles all symbols. The EA processes signals for any symbol in your Market Watch.

### Can I run MT4 and MT5 simultaneously?

Yes. MT5 uses the x64 DLL (`PTWebSocket.dll`), MT4 uses the x86 DLL (`PTWebSocket32.dll`). They can run side by side on the same Windows instance.

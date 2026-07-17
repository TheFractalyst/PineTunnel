# TradingView Alert Setup

This guide covers configuring TradingView alerts to send signals to your PineTunnel server.

## 1. Create a TradingView Alert

1. Open a chart in TradingView
2. Click the "Alert" icon (clock icon) in the top toolbar or press `Alt + A`
3. In the "Create Alert" dialog:

### Condition Tab
- **Condition:** Select your indicator or strategy that generates buy/sell signals
- **Trigger:** Choose when to fire (e.g. "Once Per Bar Close")

### Actions Tab
- **Webhook URL:** Enter your PineTunnel server's root endpoint:
  ```
  https://your-domain.com/
  ```

- **Message:** Enter the PineTunnel command in CSV format (see [Command Reference](COMMANDS.md)):
  ```
  LICENSE_ID,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,comment=myTrade,secret=YOUR_SECRET
  ```

  Replace:
  - `LICENSE_ID` - your license ID from the server admin
  - `EURUSD` - the trading symbol (must match MT4/MT5 Market Watch)
  - `lots=0.10` - volume in lots (or use `risk=1` for risk-based sizing)
  - `sl=` / `tp=` - stop loss and take profit prices
  - `YOUR_SECRET` - your secret key from the server admin

### Notifications Tab
- Uncheck all (PineTunnel uses webhooks, not TV notifications)

4. Click "Create"

## 2. Using PineScript alertcondition()

If you're writing your own PineScript indicator, use `alertcondition()` or `alert()`:

```pine
//@version=6
indicator("My Strategy", overlay=true)

// Your buy/sell logic
buySignal = ta.crossover(ta.rsi(close, 14), 30)
sellSignal = ta.crossunder(ta.rsi(close, 14), 70)

// Send alerts
alertcondition(buySignal, title="Buy", message="LICENSE_ID,buy,{{ticker}},lots=0.10,secret=YOUR_SECRET")
alertcondition(sellSignal, title="Sell", message="LICENSE_ID,sell,{{ticker}},lots=0.10,secret=YOUR_SECRET")
```

### Using {{ticker}} substitution

TradingView replaces `{{ticker}}` with the chart symbol (e.g. `EURUSD`). This lets you use the same alert for multiple symbols.

### Available substitution variables

| Variable | Replaced with |
|----------|---------------|
| `{{ticker}}` | Symbol (e.g. `EURUSD`) |
| `{{interval}}` | Timeframe (e.g. `15`, `60`, `D`) |
| `{{timenow}}` | Current timestamp |
| `{{price}}` | Current price |

## 3. Using the included PineScript helper

PineTunnel includes a PineScript helper at `apps/ea/pine/PineTunnel.pine` that provides a `sendPT()` function for constructing alert messages:

```pine
sendPT(licenseID, command, symbol, params, comment, secret, freq)
```

Import it in your indicator:
```pine
//@version=6
import TheFractalyst/PineTunnel/<version>/sendPT
```

Or copy the function into your script directly.

## 4. Testing your webhook

### Using curl

```bash
curl -X POST https://your-domain.com/ \
  -H "Content-Type: text/plain" \
  -d 'YOUR_KEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=YOUR_SECRET'
```

### Using TradingView's test alert

1. Create an alert as described above
2. Click "Test webhook" in the alert creation dialog
3. Check the server logs for the incoming request
4. Check the MT4/MT5 Experts tab for signal delivery

## 5. Alert message format

PineTunnel supports two formats:

### CSV format (recommended for TradingView alerts)
```
LICENSE_ID,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,comment=myTrade,secret=YOUR_SECRET
```

### JSON format (for programmatic integration)

The root endpoint also accepts JSON with a `message` field containing the CSV string:

```json
{
  "message": "YOUR_KEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,comment=myTrade,secret=YOUR_SECRET"
}
```

## 6. Signal Encryption (RC4)

PineTunnel supports RC4-drop256 stream cipher encryption for signal payloads. This prevents TradingView (or anyone inspecting webhook traffic) from seeing your license ID, command, or parameters.

### How it works

1. Generate a 64-char hex encryption key:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Set it in your server's `.env`:
   ```
   SIGNAL_ENCRYPTION_KEY=your-64-char-hex-key-here
   ```
3. Set the same key in your PineScript's `encKey` input field (see `apps/ea/pine/PineTunnel.pine`).

The server auto-detects encrypted vs plaintext signals. If a message starts with `RC4,`, the server decrypts it before parsing. Plaintext signals still work when no key is configured.

### Encrypted signal format

```
RC4,<nonce_hex>:<ciphertext_hex>:<checksum_hex>
```

The checksum detects tampering before decryption. The nonce is unique per signal to prevent replay attacks.

### Key rotation

Set `SIGNAL_ENCRYPTION_KEY_PREVIOUS` in `.env` to rotate keys without downtime. The server tries the current key first, then the previous key.

## 7. HTTPS via Cloudflare Tunnel

For production, use Cloudflare to expose your server with HTTPS:

```bash
pinetunnel setup-cloudflare  # Quick tunnel (no domain) or DNS setup (with domain)
```

This provides TLS termination and DDoS protection. TradingView requires HTTPS for webhook URLs.

## Troubleshooting

### Alert fires but no trade executes
- Check the server is running and the EA is connected (green dashboard)
- Verify the symbol in your alert matches MT4/MT5 Market Watch exactly
- Check the webhook secret in your alert matches the license's secret_key configured on the server
- Look at server logs for error messages

### "Webhook failed" in TradingView
- Verify the webhook URL is correct and uses HTTPS
- Check the server is reachable from the internet
- Verify Cloudflare or reverse proxy is forwarding correctly

### Duplicate trades
- PineTunnel has built-in idempotency (dedup window). Duplicate alerts within the window are rejected automatically.
- If you still see duplicates, check that your TradingView alert isn't firing multiple times per bar.

# Quick Start Guide

This guide walks you through setting up PineTunnel from zero to your first automated trade.

## Table of Contents

- [1. Prerequisites](#1-prerequisites)
- [2. Deploy the Server](#2-deploy-the-server)
- [3. Build the C++ DLL](#3-build-the-c-dll)
- [4. Install the EA on MetaTrader](#4-install-the-ea-on-metatrader)
- [5. Send Your First Test Alert](#5-send-your-first-test-alert)
- [6. Understand PineTunnel Syntax](#6-understand-pinetunnel-syntax)
- [7. Explore EA Input Parameters](#7-explore-ea-input-parameters)
- [8. Configure TradingView Alerts](#8-configure-tradingview-alerts)
- [9. Set Up the Telegram Admin Bot](#9-set-up-the-telegram-admin-bot)
- [10. Configure Security](#10-configure-security)
- [11. Test with a Real Strategy](#11-test-with-a-real-strategy)
- [12. Production Deployment](#12-production-deployment)
- [Troubleshooting](#troubleshooting)

---

## 1. Prerequisites

### Operating System

**MetaTrader terminal (EA side):**
- Windows 10 or 11
- Windows Server 2016, 2019, or 2022
- macOS/Linux: Use a Windows cloud instance (MetaTrader requires Windows)

**PineTunnel server:**
- Deployed on any VPS or cloud instance that runs Python 3.13+

### TradingView Plan

You need a TradingView plan that supports webhook alerts:
- Essential, Plus, Premium, Expert, Elite, or Ultimate
- Free plan does NOT support webhooks

Check your plan: [TradingView Settings > Account](https://www.tradingview.com/settings/account/)

### Software Requirements

| Software | Version | Purpose |
|----------|---------|---------|
| Python | 3.13+ | Server backend |
| Redis | 6+ | WebSocket state, rate limiting, pub/sub |
| MetaTrader | 4 or 5 | Trade execution |
| CMake | 3.20+ | DLL compilation (Windows only) |
| MSVC | 2022+ | C++ compiler (Windows only) |

### MetaTrader Terminal Hosting

For lowest latency, run your MetaTrader terminal on a Windows cloud instance located near your broker's server. The PineTunnel server runs on your VPS.

---

## 2. Deploy the Server

### Option A: VPS Deployment (pip install - Recommended)

```bash
pip install pinetunnel
redis-server  # Start Redis
pinetunnel setup  # One-command setup (or just run `pinetunnel` for auto-setup)
pinetunnel setup-cloudflare  # HTTPS via Cloudflare tunnel (quick tunnel or DNS)
pinetunnel start --daemon   # Start server in background
pinetunnel install-service  # Auto-start on boot (systemd on Linux, launchd on macOS)
```

Or use the interactive wizard instead of `pinetunnel setup`:

```bash
pinetunnel init   # Interactive wizard: .env, secrets, migrations
```

Edit `.env` with your values (the init wizard fills most automatically):

```bash
# Required: set these 3 secrets (32+ characters each)
WEBHOOK_SECRET=your-webhook-secret-here-min-32-chars
JWT_SECRET=your-jwt-secret-here-min-32-chars
ADMIN_API_KEY=your-admin-api-key-here-min-32

# Required: public URL of your server (set by setup-cloudflare)
SERVER_BASE_URL=https://your-domain.com

# Required: Telegram admin bot
TELEGRAM_BOT_TOKEN=your-bot-token-from-botfather
TELEGRAM_ADMIN_IDS=your-telegram-user-id

# Optional: RC4 signal encryption
SIGNAL_ENCRYPTION_KEY=  # 64-char hex key for encrypted alerts
```

Verify the server is running:
```bash
curl https://your-domain.com/health
# {"status": "ok"}
```

Visit `https://your-domain.com/docs` for interactive API documentation.

### Option B: Local Development

```bash
git clone https://github.com/TheFractalyst/PineTunnel.git
cd PineTunnel

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env` with your values, then:

```bash
alembic upgrade head
uvicorn apps.server.main:app --reload --host 127.0.0.1 --port 8000
```

### HTTPS via Cloudflare Tunnel

For production, use Cloudflare to get HTTPS without opening ports:

```bash
pinetunnel setup-cloudflare  # Quick tunnel (no domain needed) or DNS setup (with domain)
```

This handles TLS termination and DDoS protection. The server stays on localhost.

### 24/7 Auto-Start via OS Service

Install PineTunnel as a system service so it survives reboots:

```bash
pinetunnel install-service  # systemd (Linux) or launchd (macOS)
```

---

## 3. Build the C++ DLL

The DLL is NOT included in the repository. You need to compile it on Windows.

### Via GitHub Actions (Recommended)

Push to your fork's `main` branch. The CI pipeline (`.github/workflows/build-dll.yml`) will:
1. Compile the DLL for x64 (MT5) and Win32 (MT4) on a Windows runner
2. Commit the compiled DLLs back to your repo automatically

### Local Build (Windows only)

```cmd
cd apps\ea\dll\PTWebSocket
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
```

For MT4 (32-bit):
```cmd
cmake -B build32 -G "Visual Studio 17 2022" -A Win32
cmake --build build32 --config Release
```

Output:
- `build/bin/Release/PTWebSocket.dll` (x64, for MT5)
- `build32/bin/Release/PTWebSocket32.dll` (x86, for MT4)

---

## 4. Install the EA on MetaTrader

### Step 1: Locate Your Data Folder

In MetaTrader: **File > Open Data Folder**

### Step 2: Copy Files

**For MT5:**
```
PTWebSocket.dll          -> MQL5\Libraries\
PineTunnel_EA.mq5        -> MQL5\Experts\
PineTunnel_EA.ex5        -> MQL5\Experts\
*.mqh (all includes)     -> MQL5\Include\
```

**For MT4:**
```
PTWebSocket32.dll        -> MQL4\Libraries\
PineTunnel_EA_MT4.mq4    -> MQL4\Experts\
PineTunnel_EA_MT4.ex4    -> MQL4\Experts\
*_MT4.mqh (all includes) -> MQL4\Include\
```

### Step 3: Compile (if using .mq5/.mq4 source)

Press `F4` in MetaTrader to open MetaEditor:
1. Open `PineTunnel_EA.mq5` (or `PineTunnel_EA_MT4.mq4`)
2. Press `F7` to compile
3. Check the output tab for errors (should be 0 errors, 0 warnings)

### Step 4: Enable DLL Imports

**Tools > Options > Expert Advisors tab:**
- Check "Allow Algorithmic Trading"
- Check "Allow DLL imports"

### Step 5: Attach EA to a Chart

1. In the Navigator panel (left sidebar), find "Expert Advisors" > "PineTunnel_EA"
2. Drag it onto any chart
3. In the dialog that appears, configure the **Inputs** tab:

| Input | Value | Description |
|-------|-------|-------------|
| `InpServerURL` | `https://your-server.com` | Your PineTunnel server URL (HTTPS required) |
| `InpLicenseID` | `your-license-id` | Your license ID (set up on the server) |
| `InpShowDashboard` | `true` | Show on-chart dashboard |

4. Click OK

### Step 6: Verify Connection

The EA dashboard should appear on the chart showing:
- **Connection status**: Green = connected, Red = disconnected
- **Server URL**: Your server address
- **License ID**: Your ID
- **Account info**: Login, balance, broker

If it shows "Disconnected", check:
- `InpServerURL` is correct and uses `https://`
- Server is running and reachable
- DLL imports are enabled

---

## 5. Send Your First Test Alert

### Get Your License ID

Your license ID is configured on the server. If you're the admin, you can create one via the API:

```bash
# Authenticate first
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your-admin-api-key"}' | jq -r '.token')

# Create a license (admin endpoint)
curl -s -X POST http://127.0.0.1:8000/api/admin/licenses \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"license_key": "TEST123", "secret_key": "test-secret-min-32-chars-please"}'
```

### Send a Test Alert via curl

```bash
# Simple buy order
curl -X POST http://127.0.0.1:8000/ \
  -H "Content-Type: text/plain" \
  -d 'TEST123,buy,EURUSD,risk=0.10,secret=test-secret-min-32-chars-please'
```

If successful:
- Server responds with `200 OK`
- EA dashboard shows the signal arriving
- A buy position for EURUSD opens in MetaTrader

### Check the Server Logs

The server logs show each step:
```
PineTunnel signal: buy EURUSD | Risk: 0.10 | SL: None | TP: None
Signal queued: signal_id=abc123
Signal pushed to EA via WebSocket
Trade executed: BUY EURUSD 0.10 @ 1.0852
```

---

## 6. Understand PineTunnel Syntax

PineTunnel uses a CSV (comma-separated) format for alert messages:

```
LICENSE_ID,COMMAND,SYMBOL,parameters...,comment=X,secret=Y
```

### Essential Commands

| Command | Description | Example |
|---------|-------------|---------|
| `buy` | Market buy | `LICENSE,buy,EURUSD,risk=0.10,secret=Y` |
| `sell` | Market sell | `LICENSE,sell,EURUSD,risk=0.10,secret=Y` |
| `close_long` | Close all long positions | `LICENSE,close_long,EURUSD,secret=Y` |
| `close_short` | Close all short positions | `LICENSE,close_short,EURUSD,secret=Y` |
| `close_all` | Close all positions | `LICENSE,close_all,EURUSD,secret=Y` |

### Essential Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `risk=1` | Volume (interpretation depends on InpVolumeType) | `risk=1` |
| `lots=0.10` | Fixed lot size | `lots=0.10` |
| `usd=100` | Fixed dollar amount | `usd=100` |
| `sl=1.0850` | Stop loss (absolute price) | `sl=1.0850` |
| `tp=1.0950` | Take profit (absolute price) | `tp=1.0950` |
| `comment=X` | Trade comment | `comment=myTrade` |
| `secret=Y` | Your secret key (required) | `secret=abc123` |

For the full command reference (40+ commands), see [COMMANDS.md](COMMANDS.md).

### Try These Test Alerts

```bash
# Buy with SL and TP
curl -X POST http://127.0.0.1:8000/ \
  -H "Content-Type: text/plain" \
  -d 'TEST123,buy,EURUSD,risk=0.10,sl=1.0800,tp=1.1000,secret=YOUR_SECRET'

# Sell
curl -X POST http://127.0.0.1:8000/ \
  -H "Content-Type: text/plain" \
  -d 'TEST123,sell,GBPUSD,lots=0.20,secret=YOUR_SECRET'

# Close all positions for EURUSD
curl -X POST http://127.0.0.1:8000/ \
  -H "Content-Type: text/plain" \
  -d 'TEST123,close_all,EURUSD,secret=YOUR_SECRET'
```

---

## 7. Explore EA Input Parameters

Press `F7` on the chart to open the EA inputs dialog. Key parameters:

### Syntax Group

| Input | Options | Description |
|-------|---------|-------------|
| `InpTargetType` | Pips / Price / Percentage | How SL and TP values are interpreted |
| `InpVolumeType` | Lots / Dollar / Risk % / Margin % | How volume parameters are calculated |
| `InpPendingEntry` | Pips from market / Price from signal / % from market | How pending order entry prices work |

### General Group

| Input | Default | Description |
|-------|---------|-------------|
| `InpPyramiding` | On | Allow multiple positions on same symbol |
| `InpCloseOnReverse` | Off | Close existing position when opposite signal arrives |
| `InpMaxOpenPositions` | 0 (unlimited) | Maximum concurrent positions |
| `InpMaxOpenPositionsPerSymbol` | 0 (unlimited) | Max positions per symbol |
| `InpPartialClosePercentage` | 25% | Default partial close percentage |

### Account Group

| Input | Default | Description |
|-------|---------|-------------|
| `InpDailyProfit` | 0 (off) | Halt EA when daily profit reaches this amount |
| `InpDailyLoss` | 0 (off) | Halt EA when daily loss reaches this amount |
| `InpDailyTimezoneGMT` | 0 | GMT offset for daily reset |

Experiment with these settings and send test alerts to see how they affect trade execution.

---

## 8. Configure TradingView Alerts

### Step 1: Create an Alert

1. Open a chart in TradingView
2. Click the Alert icon (or press `Alt + A`)
3. Configure the **Condition** tab (your indicator/strategy trigger)

### Step 2: Set the Webhook URL

In the **Actions** tab:
- Check **Webhook URL**
- Enter your server URL: `https://your-server.com/`

### Step 3: Set the Alert Message

In the **Message** field, enter the PineTunnel CSV format:

```
YOUR_LICENSE,buy,{{ticker}},risk=0.10,sl=1.0850,tp=1.0950,comment=myTrade,secret=YOUR_SECRET
```

TradingView replaces `{{ticker}}` with the chart's symbol automatically.

### Step 4: Using PineScript alertcondition()

If you're writing your own PineScript:

```pine
//@version=6
indicator("My Strategy", overlay=true)

buySignal = ta.crossover(ta.rsi(close, 14), 30)
sellSignal = ta.crossunder(ta.rsi(close, 14), 70)

alertcondition(buySignal, title="Buy", 
  message="YOUR_LICENSE,buy,{{ticker}},risk=0.10,secret=YOUR_SECRET")
alertcondition(sellSignal, title="Sell", 
  message="YOUR_LICENSE,sell,{{ticker}},risk=0.10,secret=YOUR_SECRET")
```

### Step 5: Test the Alert

1. Create the alert in TradingView
2. Wait for the condition to trigger (or use a condition that fires immediately)
3. Check the server logs and EA dashboard for the incoming signal

For more details, see [TRADINGVIEW_ALERTS.md](TRADINGVIEW_ALERTS.md).

---

## 9. Set Up the Telegram Admin Bot

The Telegram bot provides real-time monitoring and alerts. It is mandatory for production deployments.

### Step 1: Create a Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### Step 2: Get Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot)
2. Copy your numeric user ID

### Step 3: Configure the Server

Add to your `.env`:
```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ADMIN_IDS=123456789
```

Restart the server. The bot will start automatically.

### Step 4: Use the Bot

Send these commands to your bot in Telegram:
- `/start` - Show main menu
- `/menu` - Open admin menu
- `/monitor` - View server status and connections
- `/help` - List all commands

The bot will also send you automatic notifications when:
- A trade is executed
- A trade execution fails
- The EA disconnects/reconnects
- Rate limits are hit

---

## 10. Configure Security

### Webhook Secret

The `WEBHOOK_SECRET` env var is your master secret. Each license ID also has its own `secret_key` that must be included in every alert message.

**Setting up a license with a secret:**

```bash
curl -X POST http://127.0.0.1:8000/api/admin/licenses \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "MYLICENSE",
    "secret_key": "my-secure-secret-key-min-32-chars"
  }'
```

**Including the secret in alerts:**

```
MYLICENSE,buy,EURUSD,risk=0.10,secret=my-secure-secret-key-min-32-chars
```

If the secret doesn't match, the server rejects the signal. The EA never sees it.

### Cloudflare Tunnel

In production, use Cloudflare as your reverse proxy for HTTPS and DDoS protection. The CLI handles setup automatically:

```bash
pinetunnel setup-cloudflare  # Quick tunnel (no domain) or DNS setup (with domain)
```

The server's IP validation middleware only allows requests from Cloudflare's IP ranges. This prevents direct-to-origin attacks.

Add Cloudflare IPs in your .env or server config (TRADINGVIEW_IPS).

### Rate Limiting

The server applies rate limiting per IP on webhook endpoints:
- Default: 1000 requests/minute per IP
- Configurable via `RATE_LIMIT_REQUESTS_PER_MINUTE` env var

Exceeded limits return `429 Too Many Requests`.

---

## 11. Test with a Real Strategy

### SuperTrend Example

1. In TradingView, add the "Supertrend" indicator to your chart
2. Create an alert:
   - **Condition:** Supertrend > close (for sell) or Supertrend < close (for buy)
   - **Webhook URL:** `https://your-server.com/`
   - **Message (buy):** `YOUR_LICENSE,buy,{{ticker}},risk=1,sl=1.0850,secret=YOUR_SECRET`
   - **Message (sell):** `YOUR_LICENSE,sell,{{ticker}},risk=1,sl=1.0950,secret=YOUR_SECRET`
3. Let the alert fire and watch the trade execute in MetaTrader

### Using the Included PineScript Helper

PineTunnel includes a PineScript helper at `apps/ea/pine/PineTunnel.pine` that provides a `sendPT()` function for constructing alert messages. See the file for usage examples.

---

## 12. Production Deployment

### Checklist

- [ ] Server deployed with HTTPS (Cloudflare tunnel recommended)
- [ ] All 3 secrets set (WEBHOOK_SECRET, JWT_SECRET, ADMIN_API_KEY - each 32+ chars)
- [ ] SERVER_BASE_URL set to your public URL
- [ ] CORS origins configured (SERVER_CORS_ORIGINS)
- [ ] EA connected (green dashboard status)
- [ ] Test alert executed successfully
- [ ] Telegram bot configured (mandatory for production)
- [ ] OS service installed (`pinetunnel install-service` for 24/7 uptime)
- [ ] Cloudflare tunnel configured for HTTPS
- [ ] Redis running and accessible

### Production Tips

- Run MetaTrader on a Windows cloud instance near your broker for lowest latency
- Use Cloudflare as your DNS/reverse proxy for DDoS protection and TLS
- Monitor the server health endpoint: `GET /health`

---

## Troubleshooting

### EA Shows "Disconnected"

1. Check `InpServerURL` uses `https://` (not `http://`)
2. Verify the server is running: `curl https://your-server.com/health`
3. Check DLL imports are enabled in MT4/MT5
4. Check the DLL is in the correct `Libraries\` folder
5. Look at the Experts tab in MT4/MT5 for error messages

### "DLL not found" or "DLL load failed"

1. Verify the DLL is in the correct folder:
   - MT5: `MQL5\Libraries\PTWebSocket.dll` (x64)
   - MT4: `MQL4\Libraries\PTWebSocket32.dll` (x86)
2. Check Tools > Options > Expert Advisors > "Allow DLL imports"
3. For MT5: ensure you're using the 64-bit DLL
4. For MT4: ensure you're using the 32-bit DLL

### "Invalid license ID"

1. Verify `InpLicenseID` matches the ID configured on the server
2. Check the server logs for the incoming connection
3. Verify the license is active (not expired or deactivated)

### Trades Not Executing

1. Check "AutoTrading" button is green in the MT4/MT5 toolbar
2. Verify the symbol in your alert matches Market Watch
3. Check the Experts tab for error messages
4. Verify your account has sufficient margin
5. Check `InpMaxOpenPositions` isn't set too low

### Webhook Returns 403

1. Check the `secret=` parameter in your alert matches the license's `secret_key` configured on the server
2. Verify the license ID is valid and active on the server
3. Check if your IP is rate-limited (wait 1 minute or reset via admin API)

### Webhook Returns 429 (Rate Limited)

1. You're sending too many alerts too fast
2. Wait 1 minute for the rate limit window to reset
3. Or increase the limit via `RATE_LIMIT_REQUESTS_PER_MINUTE` env var

### Duplicate Trades

PineTunnel has built-in idempotency. Duplicate alerts within the dedup window (5 minutes) are automatically rejected. If you still see duplicates:
1. Check that your TradingView alert isn't firing multiple times per bar
2. Verify the content hash is different for intentionally different signals

### Server Won't Start

1. Check Redis is running: `redis-cli ping` (should return `PONG`)
2. Check Python version: `python --version` (must be 3.13+)
3. Re-run setup: `pinetunnel init`
4. Check `.env` file exists and has all required variables
5. Check logs for specific error messages

# EA Setup Guide

This guide covers installing the PineTunnel Expert Advisor on MetaTrader 4 and 5.

## Prerequisites

- MetaTrader 4 or 5 terminal (installed and logged into your broker)
- PineTunnel server running (see [Quick Start](../README.md#quick-start))
- Your license ID (from the server admin)
- The compiled DLL (`PTWebSocket.dll` for MT5, `PTWebSocket32.dll` for MT4)

### Getting the DLL

DLLs are not included in the public repository. Build them via:

1. **GitHub Actions** - Push to `main` triggers the CI pipeline which compiles and commits the DLLs
2. **Local build** - Use CMake on Windows with MSVC:
   ```cmd
   cd apps\ea\dll\PTWebSocket
   cmake -B build -G "Visual Studio 17 2022" -A x64
   cmake --build build --config Release
   ```
   For MT4 (32-bit): replace `-A x64` with `-A Win32`

## MT5 Setup

### 1. Install the DLL

Copy `PTWebSocket.dll` to:
```
<MT5 Data Folder>\MQL5\Libraries\
```

To find your Data Folder: File > Open Data Folder

### 2. Install the EA

Copy these files:
```
PineTunnel_EA.mq5     -> <MT5 Data Folder>\MQL5\Experts\
PineTunnel_EA.ex5     -> <MT5 Data Folder>\MQL5\Experts\
*.mqh (all includes)  -> <MT5 Data Folder>\MQL5\Include\
```

### 3. Compile the EA (if using .mq5)

In MetaEditor (F4):
1. Open `PineTunnel_EA.mq5` from the Navigator panel
2. Press F7 to compile
3. Verify no errors in the output tab

### 4. Attach the EA to a chart

1. Open MT5 terminal
2. In Navigator panel, find "Expert Advisors" > "PineTunnel_EA"
3. Drag it onto a chart
4. In the dialog that appears, configure:

**Inputs tab:**
- `InpServerURL` - Your server URL (e.g. `https://your-domain.com`)
- `InpLicenseID` - Your license ID
- Other inputs (risk management, display, etc.) as desired

**Common tab:**
- Check "Allow Algorithmic Trading"
- Check "Allow DLL imports"

5. Click OK

### 5. Verify connection

The EA dashboard should appear on the chart showing:
- Connection status (green = connected)
- Server URL
- License ID
- Account info

If it shows "Disconnected":
- Check that the server is running
- Verify `InpServerURL` is correct and uses HTTPS
- Check Tools > Options > Expert Advisors > "Allow DLL imports"

## MT4 Setup

### 1. Install the DLL

Copy `PTWebSocket32.dll` to:
```
<MT4 Data Folder>\MQL4\Libraries\
```

To find your Data Folder: File > Open Data Folder

### 2. Install the EA

Copy these files:
```
PineTunnel_EA_MT4.mq4   -> <MT4 Data Folder>\MQL4\Experts\
PineTunnel_EA_MT4.ex4   -> <MT4 Data Folder>\MQL4\Experts\
*_MT4.mqh (all includes) -> <MT4 Data Folder>\MQL4\Include\
```

### 3. Compile the EA (if using .mq4)

In MetaEditor (F4):
1. Open `PineTunnel_EA_MT4.mq4`
2. Press F7 to compile
3. Verify no errors

### 4. Attach the EA to a chart

Same process as MT5. The input parameters are identical.

## EA Input Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `InpServerURL` | `https://your-server.com` | Your PineTunnel server URL (HTTPS required) |
| `InpLicenseID` | (empty) | Your license ID |
| `InpAutoRestart` | `true` | Auto-restart EA on DLL/EA update |
| `InpMaxOpenPositions` | `0` (unlimited) | Maximum concurrent positions |
| `InpMaxOpenPositionsPerSymbol` | `0` (unlimited) | Max positions per symbol |
| `InpMaxUniqueSymbols` | `0` (unlimited) | Maximum different symbols |
| `InpDefaultComment` | (empty) | Default trade comment |
| `InpFontSize` | `10` | Dashboard font size |
| `InpShowDashboard` | `true` | Show on-chart dashboard |

## Troubleshooting

### "DLL not found" or "DLL load failed"
- Verify the DLL is in the correct `Libraries\` folder
- Check Tools > Options > Expert Advisors > "Allow DLL imports"
- For MT5: ensure you're using the 64-bit DLL (`PTWebSocket.dll`)
- For MT4: ensure you're using the 32-bit DLL (`PTWebSocket32.dll`)

### "Connection failed" or "Disconnected"
- Verify `InpServerURL` uses `https://` (not `http://`)
- Check the server is running and reachable
- Check firewall isn't blocking outbound WebSocket connections
- Look at the Experts tab in MT4/MT5 terminal for error messages

### "Invalid license ID"
- Verify `InpLicenseID` matches what the server admin assigned
- Contact the server admin to confirm the license is active

### Trades not executing
- Check "AutoTrading" button is green in the MT4/MT5 toolbar
- Verify the symbol in your alert matches an available symbol in Market Watch
- Check the Experts tab for error messages
- Verify your account has sufficient margin

## 24/7 Uptime

### MetaTrader Terminal

Run MetaTrader on a Windows VPS for 24/7 uptime. Enable "Allow Algorithmic Trading" in Tools > Options.

### PineTunnel Server

Install the server as an OS service so it survives reboots:

```bash
pinetunnel install-service  # systemd (Linux) or launchd (macOS)
```

### HTTPS via Cloudflare Tunnel

Use Cloudflare to expose your server with HTTPS without opening ports:

```bash
pinetunnel setup-cloudflare  # Quick tunnel or DNS setup
```

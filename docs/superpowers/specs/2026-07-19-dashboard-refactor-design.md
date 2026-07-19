# PineTunnel Dashboard Refactor - Design Spec

**Date:** 2026-07-19
**Status:** Approved (user reviewed 2026-07-19, all decisions in section 12 resolved)
**Supersedes:** None (builds on 2026-07-18-cloudflare-remotely-managed-tunnel-design.md)
**Depends on:** PineTunnel v7.3.2 codebase

---

## 1. Problem Statement

### 1.1 Current State

PineTunnel v7.3.2 is a pip-installable Python package that bridges TradingView webhook alerts to MetaTrader 4/5 Expert Advisors. Users today must:

1. `pip install pinetunnel`
2. Run `pinetunnel` (11-step CLI wizard, 2869-line god file in `apps/cli/main.py`)
3. Manually start Redis
4. Run `pinetunnel setup-cloudflare` (3 sub-flows, manual Cloudflare dashboard work)
5. Manually create a Telegram bot via @BotFather, edit `.env` by hand, restart server
6. Run `pinetunnel install-ea` (1555-line module, requires Windows + MetaTrader GUI steps)
7. Configure TradingView alerts manually

**Observed drop-off:** ~30% of users who `pip install` reach a working first trade. The 4 manual context switches (Redis, Telegram, EA GUI, TradingView) are where users bail.

### 1.2 What We Want

`pip install pinetunnel` -> `pinetunnel` -> browser opens -> **everything else is a web GUI**.

The dashboard replaces the CLI wizard as the primary admin surface. Telegram becomes the OAuth login provider (not just a notification bot). Cloudflare is configured from inside the authenticated dashboard. The CLI shrinks to 5 commands (down from 15) and becomes optional for headless deployments.

### 1.3 Success Criteria

- A non-technical user can go from `pip install pinetunnel` to a working TradingView webhook URL in under 5 minutes, with no terminal commands beyond the initial `pinetunnel`.
- The dashboard can: configure Telegram bot, configure Cloudflare tunnel, install OS service, run migrations, edit `.env` settings, send test webhooks, download EA files, and do everything the Telegram bot does today (licenses, monitoring, signals, logs).
- The CLI god file (`apps/cli/main.py`, 2869 lines) is decomposed into focused modules under 500 lines each.
- The existing 75 admin API endpoints and the Telegram bot continue to work unchanged (backward compatibility).

---

## 2. Architecture

### 2.1 High-Level Model

```
┌─────────────────────────────────────────────────────────────────┐
│                    User's Machine (localhost)                    │
│                                                                  │
│  Browser ──HTTP──> [ FastAPI Server (127.0.0.1:8000) ]           │
│                     ├── /admin/*         (static SPA dashboard)  │
│                     ├── /api/admin/*     (75 existing endpoints)  │
│                     ├── /api/dashboard/* (new dashboard endpoints)│
│                     ├── /webhook         (TradingView ingress)    │
│                     ├── /ws/{key}        (EA WebSocket)           │
│                     └── Telegram bot     (long-polling, auth)     │
│                            │                                     │
│  [cloudflared] ─────────────┘ (tunnel to public hostname)        │
│       │                                                          │
└───────┼──────────────────────────────────────────────────────────┘
        │ HTTPS 443
        v
┌───────────────────┐        POST /webhook
│ TradingView       │ ──────────────────────────────> (via tunnel)
│ 52.89.214.238     │
│ 34.212.75.30      │
│ 54.218.53.128     │
│ 52.32.178.7       │
└───────────────────┘
```

**Key property:** The server binds `127.0.0.1:8000`. The only public ingress is via the Cloudflare tunnel, which routes `https://pinetunnel.example.com` -> `http://localhost:8000`. The dashboard is accessed locally at `http://localhost:8000/admin/`.

### 2.2 Single-Process Design

One process (uvicorn + FastAPI + Telegram bot via long-polling in a background thread). No separate supervisor, no second process. This matches the pattern used by Jupyter Notebook, Home Assistant, and Grafana.

**Why not a separate supervisor panel:**
- Two processes = twice the failure modes. If the supervisor crashes, the server is orphaned.
- The dashboard can restart its own process via the detached-child + `os._exit(0)` pattern (see section 6.4).
- Jupyter, Home Assistant, Grafana all chose single-process for the same reason.

### 2.3 Component Inventory

```
apps/
├── cli/
│   ├── main.py              # SLIMMED: ~250 lines. Commands: pinetunnel, start, stop, status, version
│   └── launcher.py          # NEW: ~60 lines. webbrowser.open + uvicorn start + first-run marker
├── server/
│   ├── admin_dashboard/     # NEW: static SPA (HTML/CSS/JS, no build step)
│   │   ├── __init__.py      # required for package-data
│   │   ├── index.html       # SPA entry point
│   │   ├── app.js           # vanilla JS, no framework
│   │   ├── styles.css       # single stylesheet
│   │   └── assets/          # icons, screenshots for EA install guide
│   ├── routes/
│   │   ├── admin.py         # EXISTING (unchanged): 75 endpoints
│   │   ├── dashboard.py     # NEW: dashboard-specific endpoints (auth, settings, restart, wizard)
│   │   └── ...              # other existing routes unchanged
│   ├── auth/                # NEW: auth module
│   │   ├── __init__.py
│   │   ├── telegram_auth.py # Telegram /login + one-time code flow
│   │   └── session.py       # SessionMiddleware setup, require_auth dependency
│   └── ...                  # rest of server unchanged
├── lib/                     # NEW: shared logic extracted from cli/*.py
│   ├── __init__.py
│   ├── cloudflare.py        # moved from apps/cli/cloudflare.py (unchanged logic)
│   ├── proxy.py             # moved from apps/cli/proxy.py
│   ├── service.py           # moved from apps/cli/service.py
│   ├── ea_install.py        # moved from apps/cli/ea_install.py (refactored: input() removed)
│   └── env_manager.py       # NEW: atomic .env read/write with validation
└── ...                      # ea/, migrations/, etc. unchanged
```

**Why `apps/lib/`:** The dashboard's new endpoints need to call the same logic the CLI uses (Cloudflare setup, service install, EA install, migrations). Today this logic lives in `apps/cli/*.py` and is coupled to CLI concerns (argparse, `input()`, `print()`). Extracting to `apps/lib/` makes it importable from both the CLI and the HTTP endpoints without duplication. The CLI becomes a thin wrapper over `apps/lib/`.

---

## 3. Authentication - Telegram OAuth

### 3.1 Chosen Flow: Bot `/login` + One-Time Code

**Decision:** Use the bot-based deep-link / one-time code flow (not the Telegram Login Widget OIDC flow).

**Rationale (from research):**
- The OIDC Login Widget requires a Client Secret from BotFather (a second secret to manage), registering Allowed URLs in BotFather (must stay in sync if port changes), JWKS fetch + RS256 JWT verification in FastAPI (more code), and a popup COOP header interaction that can break on localhost.
- The bot `/login` flow reuses existing secrets (bot token + admin user ID, both already in `.env`), works with zero public surface (bot uses long-polling), and is implemented with a single `CommandHandler("login", ...)` in python-telegram-bot.
- For a single-admin localhost dashboard, the bot flow's security properties are sufficient: single-use code, 90-second TTL, `user_id` whitelist, `secrets.token_urlsafe(8)` entropy (~64 bits).

### 3.2 Prerequisites

The user must provide during first-run setup:
- `TELEGRAM_BOT_TOKEN` - from @BotFather (already in `.env` today)
- `TELEGRAM_ADMIN_IDS` - the user's numeric Telegram user ID (already in `.env` today)

Both are collected in the dashboard's setup wizard (see section 5), not in the CLI.

### 3.3 Login Flow - Step by Step

```
┌────────────┐                    ┌────────────┐                 ┌────────────┐
│ Browser    │                    │ FastAPI    │                 │ Telegram   │
 │ (Dashboard)│                    │ (localhost)│                 │ Bot (poll) │
└─────┬──────┘                    └─────┬──────┘                 └─────┬──────┘
      │                                 │                              │
      │ GET /admin/ (no session)        │                              │
      │ ─────────────────────────────>  │                              │
      │  302 -> /admin/login            │                              │
      │ <─────────────────────────────  │                              │
      │                                 │                              │
      │ GET /admin/login                │                              │
      │ ─────────────────────────────>  │                              │
      │  "Send /login to @YourBot"      │                              │
      │ <─────────────────────────────  │                              │
      │                                 │                              │
      │ (user opens Telegram, sends /login to bot)                     │
      │                                 │                              │
      │                                 │  Update(message=/login)      │
      │                                 │ <────────────────────────────│
      │                                 │  verify user.id == ADMIN_ID  │
      │                                 │  code = token_urlsafe(8)     │
      │                                 │  store {code: (uid, exp+90s)}│
      │                                 │  reply "Code: 7fK2-9pQx"     │
      │                                 │ ────────────────────────────>│
      │                                 │                              │
      │ (user sees code in Telegram)    │                              │
      │                                 │                              │
      │ POST /api/dashboard/login       │                              │
      │  {code: "7fK2-9pQx"}            │                              │
      │ ─────────────────────────────>  │                              │
      │                                 │  lookup code                 │
      │                                 │  verify not expired          │
      │                                 │  verify uid == ADMIN_ID      │
      │                                 │  delete code (single-use)    │
      │                                 │  set session cookie          │
      │  200 + Set-Cookie: session=...  │                              │
      │ <─────────────────────────────  │                              │
      │                                 │                              │
      │ GET /admin/ (with session)      │                              │
      │ ─────────────────────────────>  │                              │
      │  200 SPA dashboard              │                              │
      │ <─────────────────────────────  │                              │
      │                                 │                              │
```

### 3.4 Implementation Details

**Code generation:** `code = secrets.token_urlsafe(8)` (~64 bits entropy)

**Code storage:** In-memory dict `{code: (user_id, expires_at)}` guarded by `asyncio.Lock`. TTL = 90 seconds. Deleted on successful redemption (single-use).

**Session cookie:** `SessionMiddleware` from Starlette, signed with a `SESSION_SECRET` stored in `.env` (generate once during first-run setup, persist across restarts). Cookie properties:
- `session_cookie = "pinetunnel_admin"`
- `max_age = 28800` (8 hours)
- `same_site = "lax"` (blocks CSRF cross-site form posts)
- `path = "/admin"` (scoped to dashboard only)
- `https_only = False` (localhost is HTTP)

**Bot handler:** `CommandHandler("login", login_handler)` running in a background asyncio task alongside FastAPI. The handler:
1. `if update.effective_user.id != ADMIN_ID: return` (silent reject, no reply, don't leak bot existence)
2. Generate code, store with TTL, reply with code + "expires in 90s, do not share"

**Route protection:** `require_auth` FastAPI dependency checks `request.session.get("authenticated") == True`. Applied to all `/api/dashboard/*` state-changing endpoints. Read-only dashboard pages may be served without auth when bound to localhost (see section 7.1).

**Bot + FastAPI coexistence:** python-telegram-bot v21+ `Application` started with `application.run_polling()` in a background thread launched from FastAPI's `lifespan` startup event. The bot uses its own asyncio loop in the thread; FastAPI uses the main loop. This is the documented pattern for co-hosting a bot with a web framework.

### 3.5 Security Properties

| Threat | Mitigation |
|--------|------------|
| Replay attack | Code is single-use, deleted on redemption, 90s TTL |
| Code guessing | `secrets.token_urlsafe(8)` = ~64 bits, infeasible to brute-force in 90s |
| Impersonation | Bot checks `update.effective_user.id == ADMIN_ID` before issuing code |
| Session hijack | Cookie signed with `SESSION_SECRET` (HMAC), `HttpOnly`, `SameSite=lax` |
| CSRF | `SameSite=lax` blocks cross-site form posts; custom `X-Admin-CSRF` header required on POST/PUT/DELETE (browsers block cross-origin custom headers without CORS preflight, and our CORS denies all cross-origin) |
| Session fixation | New session ID on successful login; old session cleared |

### 3.6 When to Switch to OIDC Widget

If PineTunnel later supports multi-admin or remote (non-localhost) dashboard access, switch to the Telegram Login Widget OIDC flow (section 1a of the research report). That flow supports PKCE, RS256-signed JWTs, and proper redirect-based auth. For single-admin localhost, the bot flow is correct and simpler.

---

## 4. Cloudflare Tunnel - From Dashboard

### 4.1 Chosen Approach: API Token (Approach C) with Token+URL Fallback (Approach A)

**Primary flow (Approach C):** User pastes a Cloudflare API token. Dashboard creates the tunnel, configures the ingress route, creates the DNS CNAME, and starts `cloudflared` -- all programmatically. User picks a domain from a dropdown and types a subdomain. Dashboard knows the final URL because it created the route.

**Fallback flow (Approach A):** For users who already created a tunnel manually (e.g., following a tutorial), dashboard accepts a tunnel token + public URL (2 fields).

**Rationale (from research):**
- Approach A requires the user to navigate the Cloudflare dashboard, create a tunnel, extract the token, then go to a different tab (Routes) and configure the hostname mapping with the right protocol and port. High support burden for zero-experience users.
- Approach C collapses all Cloudflare-side work into one credential. The dashboard fetches zones via `GET /zones`, presents a dropdown, auto-fills `http://localhost:8000` as the service (PineTunnel knows its own port), and orchestrates the full setup.
- The API token is more powerful than a tunnel token, but PineTunnel can use it only for the setup flow and then discard it (store only the tunnel token, which is narrowly scoped). The user can also delete the API token from Cloudflare after setup -- the tunnel token keeps working independently.

### 4.2 Primary Flow (Approach C) - Step by Step

```
Step 1: "Connect to Cloudflare"
  [ ] I have a domain on Cloudflare   -> Approach C (recommended)
  [x] I already have a tunnel token   -> Approach A (fallback)

--- Approach C ---

Step 2: Paste your Cloudflare API token
  "Create one at dash.cloudflare.com/profile/api-tokens"
  "Permissions needed: Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit"
  [_______________]
  [Verify token]  -> GET /user/tokens/verify, then GET /accounts, GET /zones

Step 3: Choose domain + subdomain
  Domain:   [example.com  v]   (populated from GET /zones)
  Subdomain: [pinetunnel]
  "Your webhook URL will be: https://pinetunnel.example.com"

Step 4: [Connect]
  Dashboard backend does (all via Cloudflare API):
  1. POST /accounts/{id}/cfd_tunnel  {name: "pinetunnel", config_src: "cloudflare"}
     -> returns {id, token: "eyJ..."}
  2. PUT /accounts/{id}/cfd_tunnel/{tid}/configurations
     {ingress: [{hostname: "pinetunnel.example.com", service: "http://localhost:8000"},
                {service: "http_status:404"}]}
  3. POST /zones/{zid}/dns_records
     {type: "CNAME", proxied: true, name: "pinetunnel.example.com",
      content: "{tid}.cfargotunnel.com"}
  4. Run cloudflared service install <TUNNEL_TOKEN>  (via asyncio.create_subprocess_exec)
     - Linux/macOS: sudo cloudflared service install <TOKEN>
     - Windows: cloudflared.exe service install <TOKEN>
     - Fallback if no sudo: detached `cloudflared tunnel run --token <TOKEN>` daemon
  5. Poll http://127.0.0.1:{20241-20245}/metrics until cloudflared_tunnel_ha_connections == 4
  6. HTTP GET https://pinetunnel.example.com/health until 200
  7. Update .env SERVER_BASE_URL = https://pinetunnel.example.com
  8. Discard API token from memory (store only tunnel token in .env for restarts)

Step 5: "Done. Your TradingView webhook URL is https://pinetunnel.example.com"
  [Copy URL]  [Open TradingView]
```

### 4.3 Fallback Flow (Approach A)

```
Step 1: Select "I already have a tunnel token"

Step 2:
  Tunnel token (eyJ...):  [_______________]
  Public URL:             [https://pinetunnel.example.com]

Step 3: [Connect]
  Dashboard backend does:
  1. Validate token starts with "eyJ" (JWT format)
  2. Run cloudflared service install <TOKEN> (or detached daemon fallback)
  3. Poll metrics for HA connections == 4
  4. HTTP GET {public_url}/health until 200
  5. Update .env SERVER_BASE_URL
```

### 4.4 Token Format and Validation

- Tunnel token is a **JWT** starting with `eyJ`. Validate with `raw.startswith("eyJ")` plus the existing length check in `apps/cli/cloudflare.py:_parse_tunnel_token`.
- API token is an opaque alphanumeric string (typically 40-60 chars). Validate by calling `GET /user/tokens/verify` -- if it returns `{"success": true}`, the token is valid.

### 4.5 Connection Verification

Two-stage verification (from research):

1. **Tunnel connected (no public URL needed):** Poll `http://127.0.0.1:{port}/metrics` where port is the first available in range 20241-20245. Check `cloudflared_tunnel_ha_connections` gauge == 4. This confirms `cloudflared` is wired to Cloudflare's edge.

2. **End-to-end (public URL works):** HTTP GET `{public_url}/health` expecting 200. This confirms the user's dashboard route mapping (Approach A) or the API-created ingress (Approach C) is correct. A 502 here means the tunnel connected but the hostname mapping is missing or wrong.

### 4.6 Persistence Across Reboots

`cloudflared service install <TOKEN>` is the correct persistence command on all three OSes (confirmed from Cloudflare docs):
- Linux: creates `cloudflared.service` (systemd)
- macOS: creates `com.cloudflare.cloudflared` (launchd plist)
- Windows: creates a Windows service (registry-based)

No additional service-manager logic needed in PineTunnel. If `service install` fails (no sudo), fall back to a detached `cloudflared tunnel run --token <TOKEN>` daemon with a PID file. This does not survive reboot -- warn the user.

### 4.7 Error Handling

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| Token invalid/expired | `cloudflared` exits within ~5s with auth error in logs | Show last 500 chars of cloudflared log; tell user to re-copy token |
| cloudflared not installed | `shutil.which("cloudflared") is None` | Auto-install (brew/apt/winget) or show download URL |
| Tunnel already running | Metrics port 20241-20245 already bound, or `systemctl is-active cloudflared` | Skip install, verify health, proceed |
| Hostname mapping missing (Approach A) | Tunnel connected (4 HA connections) but public URL returns 502 | Show: "Go to Networking > Tunnels > [tunnel] > Routes > Add route. Service URL = http://localhost:8000" |
| Port 7844 blocked (firewall) | cloudflared logs show connection refused to edge | Show: "Ensure outbound port 7844 is open" |
| Service install fails (no sudo) | Non-zero exit from `cloudflared service install` | Fall back to daemon mode, warn no reboot persistence |

### 4.8 New Endpoints

```
POST   /api/dashboard/cloudflare/setup          Approach C: body {api_token, zone_id, subdomain}
POST   /api/dashboard/cloudflare/setup-manual   Approach A: body {tunnel_token, public_url}
GET    /api/dashboard/cloudflare/status         {installed, running, tunnel_url, ha_connections}
POST   /api/dashboard/cloudflare/stop           Stop quick tunnel (if running)
GET    /api/dashboard/cloudflare/zones          List zones for a given API token (for dropdown)
```

All gated by `require_auth` + `X-Admin-CSRF` header. Long-running setup runs as a background task with SSE progress streaming to the dashboard.

---

## 5. Bootstrap Flow - pip install to Dashboard

### 5.1 The One Command

```bash
pip install pinetunnel
pinetunnel
```

### 5.2 What `pinetunnel` (no args) Does

The slimmed CLI (`apps/cli/main.py`, ~250 lines) does the **bare minimum** to get the dashboard open in a browser:

1. **Check first-run marker** (`~/.pinetunnel/initialized` or `%APPDATA%\PineTunnel\initialized` on Windows). If absent, this is first run.
2. **Generate minimal .env** (if missing):
   - `HOST=127.0.0.1` (localhost bind, safe default)
   - `PORT=8000`
   - `APP_ENV=production`
   - `WEBHOOK_SECRET=<auto-generated 32 chars>`
   - `JWT_SECRET=<auto-generated 48 chars>`
   - `ADMIN_API_KEY=<auto-generated 48 chars>`
   - `SESSION_SECRET=<auto-generated 32 chars>` (for SessionMiddleware)
   - `SIGNAL_ENCRYPTION_KEY=<auto-generated 64-char hex>`
   - `TELEGRAM_BOT_TOKEN=` (empty -- collected in dashboard)
   - `TELEGRAM_ADMIN_IDS=` (empty -- collected in dashboard)
   - `SERVER_BASE_URL=http://localhost:8000` (temporary, updated after Cloudflare setup)
   - `DATABASE_URL=sqlite:///pinetunnel.db`
   - chmod 600 on the file
3. **Run migrations** (alembic upgrade head, same subprocess pattern as current `cmd_setup`).
4. **Start uvicorn as a detached daemon** on `127.0.0.1:8000` (not foreground). Logs to `pinetunnel-daemon.log`. User can close the terminal -- server keeps running. Use `pinetunnel start --foreground` for debugging.
5. **In lifespan startup event, after uvicorn binds:**
   - If first run and not headless: `webbrowser.open("http://127.0.0.1:8000/admin/", new=2, autoraise=True)`
   - Create the first-run marker file.
6. **Server is now running as daemon.** Browser is open to the dashboard. User can close the terminal.

### 5.3 What the Dashboard Shows on First Run

The SPA detects "first run" via `GET /api/dashboard/setup-status` which returns `{initialized: false, telegram_configured: false, cloudflare_configured: false}`. The dashboard shows the **Setup Wizard**:

```
┌──────────────────────────────────────────────────────────┐
│  Welcome to PineTunnel                                   │
│                                                          │
│  Step 1: Configure Telegram bot (for login + alerts)     │
│  ┌────────────────────────────────────────────────┐      │
│  │ 1. Open Telegram, message @BotFather           │      │
│  │ 2. Send /newbot, follow prompts                │      │
│  │ 3. Paste the bot token here:                   │      │
│  │   [____________________________]                │      │
│  │ 4. Send /start to your new bot                 │      │
│  │ 5. Open @userinfobot to get your user ID       │      │
│  │ 6. Paste your user ID here:                    │      │
│  │   [____________________________]                │      │
│  │                                                │      │
│  │ [Verify & Save]                                │      │
│  │   -> validates token via getMe                 │      │
│  │   -> sends a test message to the user ID       │      │
│  │   -> writes to .env                            │      │
│  │   -> starts the bot in background              │      │
│  └────────────────────────────────────────────────┘      │
│                                                          │
│  Step 2: Connect Cloudflare tunnel (for public webhook)  │
│  ┌────────────────────────────────────────────────┐      │
│  │ [I have a domain on Cloudflare] (recommended)  │      │
│  │ [I already have a tunnel token]                │      │
│  │ [Skip - I'll do this later]                    │      │
│  └────────────────────────────────────────────────┘      │
│  (see section 4 for the Cloudflare flow)                 │
│                                                          │
│  Step 3: Your webhook URL is ready                       │
│  ┌────────────────────────────────────────────────┐      │
│  │ https://pinetunnel.example.com                 │      │
│  │ [Copy]  [Open TradingView]                     │      │
│  │                                                │      │
│  │ Next: Configure TradingView alerts             │      │
│  └────────────────────────────────────────────────┘      │
│                                                          │
│  Step 4: Install EA on MetaTrader (optional, later)      │
│  ┌────────────────────────────────────────────────┐      │
│  │ [Download EA for MT5]  [Download EA for MT4]   │      │
│  │ (step-by-step guide with screenshots)          │      │
│  └────────────────────────────────────────────────┘      │
│                                                          │
│  [Go to Dashboard]                                       │
└──────────────────────────────────────────────────────────┘
```

### 5.4 Returning User (Subsequent Runs)

`pinetunnel` (no args) on a subsequent run:
1. First-run marker exists.
2. `.env` exists.
3. Start uvicorn.
4. Open browser to `http://127.0.0.1:8000/admin/`.
5. Dashboard detects `initialized: true`, shows the main dashboard (Overview panel), not the wizard.

### 5.5 Headless / Service Mode

`pinetunnel start --daemon` (default behavior) or `pinetunnel start --no-open-browser`:
- Does not open a browser.
- Starts uvicorn as a detached daemon (existing `apps/lib/service.py` logic, moved from `apps/cli/service.py`).
- For service installs: `pinetunnel install-service` still works (wraps `apps/lib/service.py`), registers systemd/launchd/sc.exe. This is the production setup for boot-time autostart.

`pinetunnel start --foreground`:
- Starts uvicorn in the foreground for debugging. Logs inline, Ctrl+C to stop. Does not detach.
- Use this when diagnosing startup errors or testing config changes.

The dashboard is accessible remotely via SSH tunnel or Cloudflare tunnel (if already configured). Auth is required (see section 7.1).

---

## 6. Dashboard Information Architecture

### 6.1 Panels

Eight top-level panels + a setup wizard. Built from the 75 existing endpoints (research agent 5 mapping) plus the new dashboard endpoints.

```
┌─────────────────────────────────────────────────────────────┐
│  PineTunnel Dashboard                          [user] [logout]│
├──────────┬──────────────────────────────────────────────────┤
│ Sidebar  │  Main content area                                │
│          │                                                   │
│ Overview │  (panel content)                                  │
│ Licenses │                                                   │
│ Signals  │                                                   │
│ Trades   │                                                   │
│ EA Telem │                                                   │
│ Logs     │                                                   │
│ System   │                                                   │
│ Settings │                                                   │
│ Setup    │                                                   │
└──────────┴──────────────────────────────────────────────────┘
```

### 6.2 Panel-to-Endpoint Mapping

**Panel 1: Overview** (landing page after login)
- Server status: `GET /api/system/health` + `GET /health/ready`
- Uptime/CPU/RAM: `GET /api/system/health`
- Active licenses/connected: `GET /health/ea-check`
- Trades today / 7d success rate: `GET /api/system/stats`
- WS push latency: `GET /api/status`
- Top symbols / active today: `GET /api/trades/admin/dashboard`
- Blocked IPs: `GET /api/admin/rate-limits`
- Diagnostics: `GET /api/diagnostics`

**Panel 2: Licenses & Users** (parity with Telegram bot's Licenses section)
- User list with stats: `GET /api/ea/ws-telemetry/users`
- Per-user drill-down: `GET /api/ea/ws-telemetry/user/{email}`
- Per-license overview: `GET /api/ea/ws-telemetry/license-overview/{license_key}`
- License trade stats: `GET /api/trades/admin/license/{license_key}`
- Pipeline debug: `GET /api/debug/license/{license_key}`
- **NEW:** License CRUD: `GET/POST/PUT/DELETE /api/dashboard/licenses` (see section 6.5)

**Panel 3: Signals & Replay**
- Signal log per license: `GET /api/ea/ws-telemetry/signal-log/{license_key}`
- Replay test signal: `POST /api/admin/replay`
- Replay batch: `POST /api/admin/replay/batch`
- Replay results: `GET /api/admin/replay/results`
- **NEW:** Test webhook (server-side loopback): `POST /api/dashboard/test-webhook`

**Panel 4: Trades & Analytics**
- Recent trades: `GET /api/trades/recent`
- Trade search: `GET /api/trades/search`
- Statistics: `GET /api/statistics`
- Trade dashboard: `GET /api/trades/admin/dashboard`
- Risk status: `GET /api/risk-status`
- Webhook stats: `GET /api/webhooks/stats`
- Recent webhooks: `GET /api/webhooks/recent`
- DB cleanup: `POST /api/database/cleanup`

**Panel 5: EA Telemetry** (real-time EA state)
- All-license overview: `GET /api/ea/ws-telemetry/overview`
- Account stats: `GET /api/ea/ws-telemetry/account-stats/{license_key}`
- Open positions: `GET /api/ea/ws-telemetry/open-positions/{license_key}`
- Trade history: `GET /api/ea/ws-telemetry/trade-history/{license_key}`
- Health telemetry: `GET /api/ea/ws-telemetry/health/{license_key}`
- Connection map: `GET /health/ea-check`
- Active connections: `GET /api/connections`

**Panel 6: Logs & Audit**
- Error logs: `GET /api/logs/errors`
- Webhook activity: `GET /api/webhooks/recent`
- Audit trail: `GET /api/audit/actions`
- Auth logs: `GET /api/auth/logs`
- Active sessions: `GET /api/auth/sessions`
- Telegram bot status: `GET /health/bot`
- Support chat: `GET /api/admin/support-logs/users`
- Prometheus metrics: `GET /metrics` (iframe or parsed)

**Panel 7: System Health**
- Diagnostics: `GET /api/diagnostics`
- System health: `GET /api/system/health`
- System stats: `GET /api/system/stats`
- Health status: `GET /health/status`
- Rate-limit management: `GET /api/admin/rate-limits` + `DELETE /api/admin/rate-limits/{ip}` + `POST /api/admin/rate-limits/{ip}/reset`
- Version: `GET /api/version`

**Panel 8: Settings** (new -- the .env editor)
- View .env (redacted): `GET /api/dashboard/config`
- Edit .env values: `PUT /api/dashboard/config`
- Rotate secrets: `POST /api/dashboard/config/rotate`
- Settings schema: `GET /api/dashboard/config/schema`
- Restart server: `POST /api/dashboard/server/restart`
- Server logs: `GET /api/dashboard/server/logs`
- Migrations status: `GET /api/dashboard/migrations/status`
- Run migrations: `POST /api/dashboard/migrations/upgrade`

**Setup Wizard** (first-run only, also accessible from Settings)
- Telegram config: `PUT /api/dashboard/config` (writes TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS)
- Cloudflare setup: `POST /api/dashboard/cloudflare/setup` or `/setup-manual`
- Webhook URL display: `GET /api/dashboard/webhook-url`
- EA download: `GET /api/ea/download/{user_id}/{platform}/{sig}` (existing)
- OS service install: `POST /api/dashboard/service/install`

### 6.3 Frontend Stack

**Vanilla HTML/CSS/JS, no build step, no framework.**

- Single `index.html` + `app.js` + `styles.css`
- Shipped as static files in `apps/server/admin_dashboard/`
- Zero npm dependencies, zero build tooling
- Matches the "pip install and done" philosophy

**Why no framework:**
- The dashboard is a relatively small SPA (8 panels + wizard). Vanilla JS with `fetch()` and a simple router is sufficient.
- No framework = no build step = no node/npm dependency = pure Python pip package.
- Easy to maintain and audit. No concern about framework CVEs or deprecations.

**Why not Preact/Alpine via CDN:**
- Adds a runtime dependency on a CDN (or vendored JS). Breaks offline/air-gapped use.
- The reactivity gain is not worth the complexity for 8 panels.

**Why not React/Vue with build step:**
- Requires npm/node to build. Breaks the "pure Python pip install" model.
- Larger bundle, more complexity, harder to audit.

### 6.4 New Endpoints Summary

All new endpoints live under `/api/dashboard/*` and are gated by `require_auth` + `X-Admin-CSRF` header on mutating actions.

```
# Auth
POST   /api/dashboard/login              Verify one-time code, set session
POST   /api/dashboard/logout             Clear session
GET    /api/dashboard/setup-status       {initialized, telegram_configured, cloudflare_configured}

# Settings (.env editor)
GET    /api/dashboard/config             Read .env (secrets redacted: show first-4-chars + length)
PUT    /api/dashboard/config             Update one or more .env keys (body: {key: value, ...})
POST   /api/dashboard/config/rotate      Rotate a named secret (body: {key: "ADMIN_API_KEY"})
GET    /api/dashboard/config/schema      Return settings.py field schema for form rendering

# Server lifecycle
GET    /api/dashboard/server/status      {pid, is_running, uptime, log_path}
POST   /api/dashboard/server/restart     Spawn detached child + os._exit(0)
POST   /api/dashboard/server/stop        Stop daemon
GET    /api/dashboard/server/logs        Tail of pinetunnel-daemon.log (last 200 lines)

# Migrations
GET    /api/dashboard/migrations/status  Current alembic head + applied revisions
POST   /api/dashboard/migrations/upgrade Run alembic upgrade head

# Cloudflare (see section 4.8)
POST   /api/dashboard/cloudflare/setup
POST   /api/dashboard/cloudflare/setup-manual
GET    /api/dashboard/cloudflare/status
POST   /api/dashboard/cloudflare/stop
GET    /api/dashboard/cloudflare/zones

# OS service
GET    /api/dashboard/service/status     {installed, type, running}
POST   /api/dashboard/service/install    Install systemd/launchd/sc.exe
POST   /api/dashboard/service/uninstall

# Licenses CRUD (parity with Telegram bot)
GET    /api/dashboard/licenses           List all licenses (full data)
POST   /api/dashboard/licenses           Create license
PUT    /api/dashboard/licenses/{key}     Update license
DELETE /api/dashboard/licenses/{key}     Delete license (requires confirm: true)
POST   /api/dashboard/licenses/{key}/extend
POST   /api/dashboard/licenses/{key}/disable
POST   /api/dashboard/licenses/{key}/enable
POST   /api/dashboard/licenses/{key}/regenerate-secret

# Webhook URL + test
GET    /api/dashboard/webhook-url        {url: SERVER_BASE_URL + "/", message_format: "KEY,cmd,SYMBOL,..."}
POST   /api/dashboard/test-webhook       Server-side loopback POST to /webhook

# EA install
GET    /api/dashboard/ea/download-links  Return HMAC download URLs for current admin
POST   /api/dashboard/ea/install-remote  SSH install to remote Windows VPS
```

### 6.5 License CRUD (Biggest Telegram Parity Gap)

The Telegram bot can create/edit/extend/disable/delete licenses today. The HTTP API cannot. The dashboard must add these endpoints. Implementation wraps the existing `client_manager` (`apps/server/services/client_manager.py`):

```
GET    /api/dashboard/licenses           -> client_manager.clients (dict)
POST   /api/dashboard/licenses           -> client_manager.add_client(key, secret, name, email, expires_at)
PUT    /api/dashboard/licenses/{key}     -> client_manager.update_client(key, **fields)
DELETE /api/dashboard/licenses/{key}     -> client_manager.remove_client(key) [requires confirm: true]
POST   /api/dashboard/licenses/{key}/extend    -> client_manager.extend_client(key, days)
POST   /api/dashboard/licenses/{key}/disable   -> client_manager.set_status(key, "disabled")
POST   /api/dashboard/licenses/{key}/enable    -> client_manager.set_status(key, "active")
POST   /api/dashboard/licenses/{key}/regenerate-secret -> client_manager.regenerate_secret(key)
```

Every mutation logs to `admin_logger` (existing audit trail at `GET /api/audit/actions`).

### 6.6 Real-Time Updates

Panels 1, 5, 6 benefit from live updates. Two options:

1. **Polling (MVP):** Dashboard JS polls `GET /api/status` every 5-10s for Panel 1, granular endpoints on drill-down. Simple, no new server code.

2. **Admin WebSocket (future):** Expose `/api/dashboard/ws` that pushes events: new webhook, trade report, connection state change, diagnostic alert. Avoids polling. Larger effort.

**Recommendation:** Polling for v1. Add admin WebSocket if polling proves insufficient.

---

## 7. Security

### 7.1 Auth Policy Based on Bind Address

```
REQUIRE_AUTH = settings.server.host not in ("127.0.0.1", "::1", "localhost")
```

- **Loopback (127.0.0.1):** Static SPA served without auth (user sees login page). All `/api/dashboard/*` state-changing endpoints require auth. Read-only endpoints may be public (for monitoring tools like SwiftBar that already use `GET /api/status`).
- **Non-loopback (0.0.0.0 or any IP):** Force `require_auth` on everything including static files. Log a loud warning at startup. Recommend the user use Cloudflare tunnel + auth instead of direct public binding.

### 7.2 CSRF Protection

- `SessionMiddleware` with `same_site="lax"` blocks cross-site form posts.
- All POST/PUT/DELETE endpoints require `X-Admin-CSRF: 1` custom header. Browsers block cross-origin `fetch` from setting custom headers without CORS preflight, and our CORS config denies all cross-origin when `SERVER_CORS_ORIGINS` is empty.
- No need for a full CSRF middleware library on localhost.

### 7.3 Shell Command Safety

The dashboard triggers a **fixed set** of commands via `asyncio.create_subprocess_exec` (never `_shell`):

| Action | Command |
|--------|---------|
| Start cloudflared | `cloudflared tunnel run --token <TOKEN>` or `cloudflared service install <TOKEN>` |
| Run migrations | `sys.executable -m alembic upgrade head` |
| Install OS service | `sc.exe create ...` / systemd / launchd (via `apps/lib/service.py`) |
| Copy EA files | `shutil.copy2` (not subprocess) |

**Hard rules:**
- Never accept a command string from the client. Dashboard sends an *action name*, server maps to a hardcoded `create_subprocess_exec` call.
- User-supplied values (token, paths) passed as separate args, never interpolated into a shell string. `create_subprocess_exec` passes them directly to OS `exec*`, so no shell metacharacter interpretation. A token value of `; rm -rf /` is just a literal string argument.
- Validate user-supplied paths with `pathlib.Path.resolve()` and check the resolved path is within an allowed directory.
- Whitelist: `dict` mapping `action_name -> callable`. No `action: "arbitrary"`.
- Keep `Process` objects in a long-lived registry to prevent GC killing them and to allow stop.

### 7.4 .env Editing Safety

- Read: secrets redacted (show first 4 chars + length, e.g., `WEBHOOK_SECRET = "aB3x**** (32 chars)"`).
- Write: atomic write via temp file + rename. Preserve chmod 600.
- Rotate: generate new secret, write to .env, optionally write old value to `*_PREVIOUS` key for rotation grace period (existing pattern for `ADMIN_API_KEY_PREVIOUS`).
- Restart required after secret rotation: dashboard prompts "Secrets rotated. Restart server now? [Yes/No]".

### 7.5 Self-Restart Safety

The restart endpoint is the most dangerous in the dashboard. Guard with:
- Auth (always)
- Confirmation modal in UI ("Restarting will drop in-flight requests. Proceed?")
- Rate-limit: one restart per 30 seconds (in-memory timestamp)
- Audit log: record who restarted and when
- Refuse when `workers > 1` (document: restart the service externally)
- On Render: just `os._exit(0)` and let Render bring it back (detect via `RENDER_WEB_CONCURRENCY`)

### 7.6 Threat Model Summary

| Threat | Surface | Mitigation |
|--------|---------|------------|
| Remote attacker reaches dashboard | Public bind | Default 127.0.0.1; force auth if non-loopback |
| CSRF from malicious website | Browser session | SameSite=lax + X-Admin-CSRF header |
| Session hijack | Cookie | HttpOnly, signed (HMAC), 8h expiry |
| Command injection | Shell endpoints | create_subprocess_exec, action whitelist, no shell |
| Path traversal | EA install, .env write | pathlib.Path.resolve() + allowed-dir check |
| Secret leakage | .env read endpoint | Redact secrets (first 4 chars + length only) |
| Restart DoS | Restart endpoint | Rate-limit 1/30s, confirm modal, audit log |
| Bot token theft | .env at rest | chmod 600, redact in API, never log |
| Tunnel token theft | .env at rest | chmod 600, redact in API, never log |

---

## 8. Code Reorganization

### 8.1 CLI Slimming (apps/cli/main.py: 2869 lines -> ~250 lines)

**Commands kept:**
- `pinetunnel` (no args) -> launcher: generate minimal .env, start uvicorn, open browser
- `pinetunnel start [--daemon] [--no-open-browser]` -> start server (headless option)
- `pinetunnel stop` -> stop daemon
- `pinetunnel status` -> is daemon running?
- `pinetunnel version` -> version info

**Commands removed (moved to dashboard):**
- `pinetunnel setup` -> dashboard setup wizard
- `pinetunnel init` -> dashboard setup wizard
- `pinetunnel setup-cloudflare` -> dashboard Cloudflare panel
- `pinetunnel setup-proxy` -> dashboard proxy panel (future)
- `pinetunnel stop-cloudflare` -> dashboard Cloudflare panel
- `pinetunnel install-service` -> dashboard Settings panel
- `pinetunnel uninstall-service` -> dashboard Settings panel
- `pinetunnel install-ea` -> dashboard EA panel
- `pinetunnel migrate` -> dashboard Settings panel
- `pinetunnel test` -> dashboard Signals panel (replay)
- `pinetunnel guide` -> dashboard help/docs
- `pinetunnel check` -> dashboard System panel

**Commands kept but delegate to apps/lib/:**
- `start`, `stop`, `status` -> `apps/lib/service.py`

### 8.2 Extraction to apps/lib/

Move from `apps/cli/` to `apps/lib/`, decoupled from CLI concerns:

| Current location | New location | Refactor needed |
|------------------|--------------|-----------------|
| `apps/cli/cloudflare.py` (763L) | `apps/lib/cloudflare.py` | Remove `print()` calls, return structured results; keep functions pure |
| `apps/cli/proxy.py` (537L) | `apps/lib/proxy.py` | Same |
| `apps/cli/service.py` (442L) | `apps/lib/service.py` | Same |
| `apps/cli/ea_install.py` (1555L) | `apps/lib/ea_install.py` | Remove `input()` prompts (replace with `selected_index` param), remove `print()` calls, return structured results, decouple from `apps.cli.main` color helpers |

**New module:**
- `apps/lib/env_manager.py` (~150L): atomic .env read/write with validation. Functions: `read_env(path) -> dict`, `write_env(path, updates: dict)`, `redact_secret(value) -> str`, `rotate_secret(key) -> str`.

### 8.3 Dashboard Static Files

```
apps/server/admin_dashboard/
├── __init__.py          # required for package-data; contains DASHBOARD_VERSION = "1.0"
├── index.html           # SPA shell with sidebar + content area
├── app.js               # vanilla JS: router, fetch helpers, panel rendering
├── styles.css           # single stylesheet, dark theme
└── assets/
    ├── logo.svg
    └── ea-setup/        # screenshots for EA install guide
        ├── 01-open-data-folder.png
        ├── 02-paste-ea.png
        ├── 03-attach-ea.png
        ├── 04-enable-dll.png
        └── 05-verify-connection.png
```

**pyproject.toml addition:**
```toml
[tool.setuptools.package-data]
"apps.server.admin_dashboard" = ["*.html", "*.css", "*.js", "assets/*", "assets/ea-setup/*"]
```

**MANIFEST.in addition:**
```
recursive-include apps/server/admin_dashboard *
```

**Runtime path resolution:**
```python
from importlib.resources import files
import apps.server.admin_dashboard as _dash_pkg
DASHBOARD_PATH = str(files(_dash_pkg))
app.mount("/admin", StaticFiles(directory=DASHBOARD_PATH, html=True), name="admin_dashboard")
```

### 8.4 Dashboard Route Registration

In `apps/server/app_factory.py`, after existing routers:

```python
# Dashboard SPA (static files)
from importlib.resources import files
import apps.server.admin_dashboard as _dash_pkg
DASHBOARD_PATH = str(files(_dash_pkg))
app.mount("/admin", StaticFiles(directory=DASHBOARD_PATH, html=True), name="admin_dashboard")

# Redirect /admin -> /admin/ (trailing slash required by StaticFiles)
@app.get("/admin")
async def admin_redirect():
    return RedirectResponse("/admin/")

# Dashboard API endpoints
from apps.server.routes.dashboard import router as dashboard_router
app.include_router(dashboard_router)
```

### 8.5 SPA Fallback (for client-side routing)

On FastAPI 0.119.1 (no `app.frontend()`), add a catch-all that distinguishes missing assets (404) from browser navigation (return `index.html`):

```python
@app.get("/admin/{full_path:path}")
async def admin_spa_fallback(full_path: str, request: Request):
    accept = request.headers.get("accept", "")
    has_extension = "." in full_path.split("/")[-1]
    if "text/html" in accept and not has_extension:
        return FileResponse(f"{DASHBOARD_PATH}/index.html")
    raise HTTPException(404)
```

Register this **after** all `/api/*` routes to avoid shadowing.

---

## 9. Testing

### 9.1 Existing Tests (Must Not Break)

- `tests/test_parser.py`, `tests/test_validator.py`, `tests/test_metrics.py`, `tests/test_security.py`, `tests/test_crypto.py` (83 tests) -- all run unchanged.
- `scripts/test_migrations.sh` -- unchanged.

### 9.2 New Tests

- `tests/test_dashboard_auth.py`: Telegram login flow, code generation, code redemption, session cookie, require_auth dependency, CSRF header check.
- `tests/test_dashboard_config.py`: .env read (redaction), .env write (atomic), .env rotate.
- `tests/test_dashboard_licenses.py`: License CRUD endpoints, audit logging.
- `tests/test_dashboard_cloudflare.py`: Mock Cloudflare API responses, verify orchestration logic (tunnel creation, ingress config, DNS CNAME, cloudflared start).
- `tests/test_env_manager.py`: Atomic write, concurrent write safety, redaction logic.
- `tests/test_lib_ea_install.py`: Refactored ea_install functions (detect + act phases, no input() calls).

### 9.3 Manual Testing Checklist

- [ ] Fresh `pip install pinetunnel` on Windows Server (alyrium) -- dashboard opens, wizard completes
- [ ] Fresh `pip install pinetunnel` on Kali Linux (byfly) -- dashboard opens, wizard completes
- [ ] Telegram login flow: /login to bot, paste code, session set
- [ ] Cloudflare setup (Approach C): API token, domain dropdown, tunnel created, URL works
- [ ] Cloudflare setup (Approach A): tunnel token + URL, cloudflared starts, URL works
- [ ] Settings editor: edit TELEGRAM_BOT_TOKEN, save, restart, bot reconnects
- [ ] Secret rotation: rotate ADMIN_API_KEY, old key stops working, new key works
- [ ] Server restart: click restart, browser reconnects within 5s
- [ ] License CRUD: create, edit, extend, disable, enable, delete
- [ ] Test webhook: send test signal, see it in Signals panel
- [ ] EA download: download zip, contains .ex5 + .dll + README
- [ ] OS service install: install on Linux (systemd), verify starts on boot

---

## 10. Migration / Backward Compatibility

### 10.1 Existing Users

Users who installed v7.3.2 and have a working `.env` + daemon:
- `pinetunnel` (no args) detects existing `.env` and first-run marker -> starts server, opens dashboard (not wizard).
- Their existing Telegram bot, Cloudflare tunnel, EA setup all continue to work.
- The dashboard shows their current config in the Settings panel.
- No breaking changes to `.env` format or database schema.

### 10.2 CLI Backward Compatibility

The removed commands (`setup`, `init`, `setup-cloudflare`, etc.) should print a deprecation message pointing to the dashboard:

```
$ pinetunnel setup
  [DEPRECATED] `pinetunnel setup` has moved to the web dashboard.
  Run `pinetunnel` to open the dashboard, then use the Setup Wizard.
  Or run `pinetunnel start --no-open-browser` for headless mode.
```

This allows existing scripts/docs to not hard-fail while guiding users to the new flow.

### 10.3 Telegram Bot Backward Compatibility

The Telegram bot continues to work unchanged. Users who prefer the bot over the dashboard can use it. The dashboard is an **additional** admin surface, not a replacement for the bot.

---

## 11. Build Order (Phased Delivery)

### Phase 1: Core Dashboard + Auth (ship first)

1. Extract `apps/lib/` from `apps/cli/` (cloudflare, proxy, service, ea_install, env_manager)
2. Slim `apps/cli/main.py` to ~250 lines (keep start/stop/status/version + launcher)
3. Create `apps/server/admin_dashboard/` static SPA shell (index.html, app.js, styles.css)
4. Implement `apps/server/auth/telegram_auth.py` (bot /login + one-time code)
5. Implement `apps/server/auth/session.py` (SessionMiddleware, require_auth)
6. Implement `apps/server/routes/dashboard.py` (auth endpoints, setup-status, config read/write, webhook-url)
7. Mount dashboard in `app_factory.py`
8. Add `webbrowser.open` in lifespan startup (first-run only)

**Delivers:** User can `pip install pinetunnel`, browser opens, login via Telegram, see dashboard, edit settings.

### Phase 2: Cloudflare + Server Lifecycle (ship second)

1. Implement Cloudflare endpoints (Approach C + Approach A) in `routes/dashboard.py`
2. Implement server restart endpoint (detached child + os._exit)
3. Implement migrations endpoints (wrap alembic)
4. Implement OS service install endpoints (wrap `apps/lib/service.py`)
5. Add Cloudflare setup wizard step to dashboard SPA
6. Add Settings panel with .env editor + restart button

**Delivers:** User can configure Cloudflare from dashboard, get webhook URL, restart server, run migrations, install service -- all from browser.

### Phase 3: License CRUD + EA Install (ship third)

1. Implement license CRUD endpoints (wrap `client_manager`)
2. Implement EA download links endpoint (wrap existing `ea_download.py`)
3. Implement EA install-remote endpoint (wrap `apps/lib/ea_install.py` refactored)
4. Add Licenses panel with full CRUD UI
5. Add EA install panel with download buttons + screenshot guide + connection verification

**Delivers:** Dashboard matches Telegram bot feature parity. User can manage licenses and install EA from browser.

### Phase 4: Polish + Real-Time (ship fourth)

1. Add remaining panels (Signals, Trades, EA Telemetry, Logs, System Health) -- mostly wiring existing endpoints to UI
2. Add test-webhook endpoint
3. Add CSV alert message builder form
4. Add QR code for webhook URL
5. (Optional) Add admin WebSocket for real-time updates
6. Deprecate old CLI commands with migration messages

**Delivers:** Full dashboard, CLI optional, complete feature parity.

---

## 12. Resolved Design Decisions

All decisions confirmed with user on 2026-07-19.

1. **Multi-admin: Single-admin only for v1.** One `TELEGRAM_ADMIN_IDS` value. Bot /login flow (not OIDC). Revisit multi-admin + OIDC Login Widget if needed in a future version.

2. **Telegram bot role: Keep bot for alerts.** Dashboard is for management (settings, licenses, monitoring). Bot is for real-time push alerts (trade executed, margin warning, position closed). Both coexist. The bot is not deprecated.

3. **Run mode: Daemon by default, --foreground for debugging.** `pinetunnel` (no args) starts uvicorn as a detached daemon immediately. Browser opens to the dashboard on first run. User can close the terminal - server keeps running. Logs go to `pinetunnel-daemon.log` (viewable in dashboard System panel). Use `pinetunnel stop` to stop. Use `pinetunnel start --foreground` to see logs inline for debugging. Use `pinetunnel install-service` for boot-time autostart (systemd/launchd/sc.exe). This matches production patterns (Jupyter, Home Assistant, Grafana all default to detached).

4. **cloudflared: Auto-install, do not bundle.** Wizard auto-installs via brew/apt/winget. Falls back to download URL if package manager fails. Keeps pip package at current size (~no added 50MB).

5. **Public ingress: Cloudflare tunnel only for v1.** Keep `apps/lib/proxy.py` code (extracted from CLI) but no dashboard panel for nginx + Let's Encrypt. Add later if users request it. Cloudflare tunnel is the recommended and only documented path.

6. **Dashboard auth trust model: Auto-trust localhost, warn on public bind.** When `HOST=127.0.0.1`, static SPA served without auth (user sees login page). All `/api/dashboard/*` state-changing endpoints require auth regardless. When `HOST=0.0.0.0` or non-loopback, force auth on everything + log loud warning at startup.

---

## 13. References

- Telegram bot /login flow: research report from agent 1 (this session)
- Cloudflare remotely-managed tunnel: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/
- Cloudflare tunnel API: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/
- Cloudflare tunnel tokens: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/tunnel-tokens/
- Cloudflare metrics: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/metrics/
- TradingView webhooks: https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
- TradingView webhook errors: https://www.tradingview.com/support/solutions/43000776894-what-do-errors-mean-when-sending-webhooks/
- FastAPI static files: https://fastapi.tiangolo.com/tutorial/static-files/
- FastAPI frontend (0.138.0+): https://fastapi.tiangolo.com/tutorial/frontend/
- Starlette SessionMiddleware: https://starlette.dev/middleware/
- asyncio subprocess: https://docs.python.org/3/library/asyncio-subprocess.html
- importlib.resources: https://docs.python.org/3/library/importlib.resources.html
- webbrowser: https://docs.python.org/3/library/webbrowser.html
- Endpoint mapping: research report from agent 5 (this session)
- EA install analysis: research report from agent 4 (this session)
- FastAPI dashboard patterns: research report from agent 3 (this session)

---

## 14. Glossary

- **SPA** - Single Page Application (the dashboard frontend)
- **OIDC** - OpenID Connect (Telegram's official OAuth flow, not used in v1)
- **PKCE** - Proof Key for Code Exchange (part of OIDC, not used in v1)
- **HA connections** - High Availability connections (cloudflared's 4 connections to Cloudflare edge)
- **Tunnel token** - JWT starting with `eyJ` that authenticates a cloudflared process to a specific tunnel
- **API token** - Opaque Cloudflare credential with scoped permissions (Tunnel:Edit, DNS:Edit, etc.)
- **Remotely-managed tunnel** - A Cloudflare tunnel whose ingress config is stored in Cloudflare's control plane (not in a local config.yml)
- **Quick tunnel** - A temporary `trycloudflare.com` URL, not for production (random URL, 200 req limit, no SSE)
- **One-time code** - A short-lived secret (`secrets.token_urlsafe(8)`) issued by the Telegram bot for login

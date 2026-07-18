# Remotely-Managed Cloudflare Tunnel for PineTunnel CLI

**Date:** 2026-07-18
**Status:** Approved
**Scope:** `apps/cli/cloudflare.py`, `apps/cli/main.py`, `README.md`

## Problem

The current `pinetunnel setup-cloudflare` command offers three flows:

- **Flow A** (`setup_cloudflare_dns`): User has a domain + API token. Creates an A record `webhook.<domain>` pointing to the VPS public IP, proxied through Cloudflare. Requires opening port 8000 on the firewall. Subdomain is hardcoded to `"webhook"` by default.
- **Flow B** (`setup_cloudflare_tunnel`): Quick tunnel. No domain. Random `trycloudflare.com` URL. Temporary.
- **Flow C** (`setup_cloudflare_oauth`): Locally-managed named tunnel via `cloudflared tunnel login` browser OAuth. Hardcodes the subdomain as `"webhook"` (line 717). **Cloudflare deprecated this flow** for most use cases per their docs: "Cloudflare recommends creating a remotely-managed tunnel for most use cases. Locally-managed tunnels are intended for specific scenarios such as local development, testing, or legacy configurations."

The user wants:

1. Use the Cloudflare-recommended approach (learned from Cloudflare docs).
2. Let the user select the public address (subdomain + domain) on the Cloudflare dashboard website, not hardcode it in the CLI.
3. Remove the hardcoded `"webhook"` default subdomain.

## Solution

Replace Flow C (locally-managed OAuth) with a **remotely-managed tunnel** flow:

1. User creates the tunnel on the Cloudflare dashboard (`Networking > Tunnels > Create a tunnel`).
2. User configures the public hostname on the dashboard (e.g., `pinetunnel.example.com` -> `http://localhost:8000`).
3. User copies the tunnel token from the dashboard install command.
4. User runs `pinetunnel setup-cloudflare`, pastes the token + URL.
5. CLI installs `cloudflared` as an OS service using the token (`cloudflared service install <TOKEN>`), which starts on boot.
6. CLI verifies the tunnel is healthy by polling the public URL's `/health` endpoint.
7. CLI updates `.env` `SERVER_BASE_URL` to the public HTTPS URL.

Also change the default subdomain in Flow A from `"webhook"` to `"pinetunnel"` to reflect the project name (not a TradingView-specific term).

## CLI Surface

### `pinetunnel setup-cloudflare` interactive menu

```
Cloudflare Setup Options:
  A) DNS setup (domain + API token, creates pinetunnel.<domain> A record)
  B) Quick tunnel (no domain needed, instant HTTPS, temporary URL)
  C) Remotely-managed tunnel (create on Cloudflare dashboard, paste token)
Choose A/B/C [C]:
```

Default shifts to C (recommended production path). Previously there was no default - A was DNS, B was quick.

### New CLI flags

```
pinetunnel setup-cloudflare --tunnel-token TOKEN --tunnel-url https://pinetunnel.example.com
pinetunnel setup-cloudflare --tunnel-token TOKEN --tunnel-url https://pinetunnel.example.com --port 8000
```

Non-interactive mode: skip the menu, go directly to remotely-managed flow.

### Auto-wizard (`pinetunnel` first run) Step 2

Before:
```
A) Public IP  (HTTP, no domain needed, zero cost)
B) Cloudflare (HTTPS, persistent domain, DDoS protection)
```

After:
```
A) Public IP         (HTTP, no domain needed, zero cost)
B) Quick Tunnel      (HTTPS, no Cloudflare account, temporary URL)
C) Cloudflare Tunnel (HTTPS, persistent, requires Cloudflare account)
Choose [A/B/C] (default: A):
```

- A: unchanged (public IP + `HOST=0.0.0.0` + firewall open)
- B: calls `setup_cloudflare_tunnel()` (existing quick tunnel, unchanged)
- C: calls `setup_cloudflare_remotely_managed()` (new)

## `setup_cloudflare_remotely_managed()` Function

**File:** `apps/cli/cloudflare.py`
**Signature:**

```python
def setup_cloudflare_remotely_managed(
    tunnel_token: str,
    tunnel_url: str,
    port: int = 8000,
    yes: bool = False,
) -> str | None:
    """Set up Cloudflare remotely-managed tunnel (Cloudflare-recommended flow).

    User creates tunnel on Cloudflare dashboard (Networking > Tunnels),
    configures the public hostname there, then pastes the tunnel token
    and hostname into this function. Cloudflared is installed as an OS
    service that starts on boot.

    Returns the public HTTPS URL or None on failure.
    """
```

**Steps:**

| Step | Action |
|------|--------|
| 1. Install check | `is_cloudflared_installed()` -> `install_cloudflared()` if missing. Same helper as existing flows. |
| 2. Parse token | `_parse_tunnel_token(raw)`: accept raw token OR full `cloudflared service install <TOKEN>` command. Extract token via split. Validate length > 20. |
| 3. Parse URL | `_parse_tunnel_url(raw)`: accept `https://host` or `host`. Normalize to `https://host`. Validate hostname contains `.`. |
| 4. Install service | `cloudflared service install <TOKEN>` (Cloudflare's recommended production method). Installs systemd/launchd/sc.exe, starts on boot. If service install fails (non-zero exit): fallback to `cloudflared tunnel run --token <TOKEN>` as detached background process. Write PID to `cloudflared.pid` (same pattern as quick tunnel). Warn user it won't survive reboot. |
| 5. Verify | Poll `https://<hostname>/health` for 30s (max). If 200: print `[OK] Tunnel healthy`. If timeout: warn user to check dashboard status (don't fail setup - some tunnels take 60s+ to initialize). Use `urllib` (no httpx dep for CLI). |
| 6. Update .env | `update_env_server_url(https://<hostname>)` (reuse existing helper). |
| 7. Print summary | Tunnel URL, service status, next steps. TradingView webhook URL: `https://<hostname>/`. |

## Token & URL Parsing Helpers

```python
def _parse_tunnel_token(raw: str) -> str | None:
    """Accept raw token OR full 'cloudflared service install <TOKEN>' command.
    Returns the token string, or None if not found."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("cloudflared"):
        parts = raw.split()
        for p in parts[2:]:  # skip "cloudflared" "service"/"tunnel"
            if not p.startswith("-") and len(p) > 20:
                return p
        return None
    return raw if len(raw) > 20 else None


def _parse_tunnel_url(raw: str) -> str | None:
    """Accept 'https://host' or 'host'. Return 'https://host' normalized."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith("http"):
        raw = "https://" + raw
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    if not parsed.hostname or "." not in parsed.hostname:
        return None
    return raw
```

## Error Handling

| Failure | Behavior |
|---------|----------|
| cloudflared missing | Auto-install via `install_cloudflared()`. If install fails, return None + print manual install URL. |
| Token parse fail | Print `"Token looks invalid (expected 20+ char string)"`. Return None. |
| URL parse fail | Print `"URL looks invalid"`. Return None. |
| Service install fails | Warn + fallback to background daemon via `subprocess.Popen` + PID file. Print that service won't survive reboot. |
| Background daemon fails to start | Same handling as quick tunnel: PID file, log to `cloudflared-tunnel.log`, 3s wait, check `poll()`. |
| Health check timeout | Warn user tunnel may still be initializing. Don't fail setup - some tunnels take 60s+. Print dashboard URL for status check. |
| .env update fail | Print manual `SERVER_BASE_URL=` instruction. Same as existing flows. |

## Files Changed

| File | Changes |
|------|---------|
| `apps/cli/cloudflare.py` | REMOVE `setup_cloudflare_oauth()` (~300 lines). REMOVE `_extract_domain_from_cert()`. REMOVE `_extract_all_domains_from_cert()`. KEEP `setup_cloudflare_dns()` (Flow A), `setup_cloudflare_tunnel()` (Flow B), `_list_all_zones_via_api()`. ADD `setup_cloudflare_remotely_managed()`. ADD `_parse_tunnel_token()`, `_parse_tunnel_url()`. CHANGE default `subdomain` param in `setup_cloudflare_dns()` from `"webhook"` to `"pinetunnel"`. |
| `apps/cli/main.py` | REMOVE import of `setup_cloudflare_oauth`. ADD `--tunnel-token` flag. ADD `--tunnel-url` flag. CHANGE `--subdomain` default from `"webhook"` to `"pinetunnel"` (line 2494). CHANGE fallback `"webhook"` to `"pinetunnel"` (line 1992). CHANGE input prompt `[webhook]` to `[pinetunnel]` (line 1973). UPDATE `cmd_setup_cloudflare()` to dispatch to remotely-managed flow when option C chosen or `--tunnel-token` provided. UPDATE `_run_quick_setup()` Step 2 to offer 3 options (A/B/C) instead of 2 (A/B), default A. |
| `README.md` | Update Cloudflare section: mention 3 options (DNS / Quick / Remotely-managed). Replace `setup-cloudflare` description. |

Net: approximately -190 lines (removes ~300-line deprecated OAuth flow, adds ~110 lines for new flow + parsing helpers).

## Not Changed

- `setup_cloudflare_dns()` (Flow A) logic stays the same. Only the default subdomain param value changes from `"webhook"` to `"pinetunnel"`. Users who want `webhook.` can pass `--subdomain webhook`.
- `setup_cloudflare_tunnel()` (Flow B) quick tunnel logic unchanged.
- `stop_quick_tunnel()` unchanged. Still handles quick tunnel PID-based stop. Remotely-managed service users stop via `cloudflared service uninstall` or OS service manager (documented in summary printout).
- No new test files. No tests exist for `setup_cloudflare_*` functions currently; manual verification only.

## Verification

Manual verification steps after implementation:

1. `pinetunnel setup-cloudflare` -> interactive menu shows A/B/C with C as default
2. `pinetunnel setup-cloudflare --tunnel-token X --tunnel-url Y` -> non-interactive, skips menu
3. `pinetunnel` (first run) -> Step 2 shows A/B/C with A as default
4. `pip install -e . && pinetunnel --help` -> all commands present, new flags visible
5. `python -c "from apps.cli.cloudflare import setup_cloudflare_remotely_managed"` -> import OK
6. `python -c "from apps.cli.cloudflare import setup_cloudflare_oauth"` -> `ImportError` (removed)
7. `pinetunnel check` -> health checks still pass (no regression)

## Sources

- [Create a remotely-managed tunnel (Cloudflare docs)](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/)
- [Locally-managed tunnels (Cloudflare docs)](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/) - "Cloudflare recommends creating a remotely-managed tunnel for most use cases."
- [Useful tunnel terms](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/install-and-setup/tunnel-useful-terms/) - remotely-managed vs locally-managed definitions.

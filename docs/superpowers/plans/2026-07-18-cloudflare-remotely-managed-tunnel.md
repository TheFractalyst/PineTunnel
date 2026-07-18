# Remotely-Managed Cloudflare Tunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deprecated locally-managed Cloudflare OAuth tunnel flow with a Cloudflare-recommended remotely-managed tunnel flow, and change the default subdomain from `"webhook"` to `"pinetunnel"`.

**Architecture:** User creates the tunnel + public hostname on the Cloudflare dashboard website, then pastes the tunnel token and public URL into the PineTunnel CLI. The CLI installs cloudflared as an OS service (`cloudflared service install <TOKEN>`), verifies the tunnel via a `/health` poll, and updates `.env SERVER_BASE_URL`. Falls back to a background daemon process if service install fails.

**Tech Stack:** Python 3.11+, stdlib `urllib`, `subprocess`, `argparse`. No new dependencies.

## Global Constraints

- All user-facing strings in code MUST be ASCII-only (per AGENTS.md - deployment uses PowerShell `Out-File -Encoding ascii` which corrupts non-ASCII).
- No comments in code unless explicitly requested (per AGENTS.md).
- PineTunnel is a pip-installable package: `pinetunnel` entry point must continue to work after changes.
- No new test files. Existing test suite (`tests/`) has no Cloudflare tests; manual verification only.
- Follow existing code style: 4-space indent, double-quote strings, snake_case, line length 100.

---

### Task 1: Add `_parse_tunnel_token` and `_parse_tunnel_url` helpers to cloudflare.py

**Files:**
- Modify: `apps/cli/cloudflare.py` (add after the `_find_env_path` / `update_env_server_url` section, around line 234)

**Interfaces:**
- Consumes: `urllib.parse.urlparse` (stdlib)
- Produces: `_parse_tunnel_token(raw: str) -> str | None`, `_parse_tunnel_url(raw: str) -> str | None`

- [ ] **Step 1: Add the two helper functions after `update_env_server_url` (after line 233)**

Insert this code immediately after the `update_env_server_url` function (after its `return updated` line and before the `# cloudflared detection and install` comment block):

```python
def _parse_tunnel_token(raw: str) -> str | None:
    """Accept a raw tunnel token or a full 'cloudflared service install <TOKEN>'
    / 'cloudflared tunnel run --token <TOKEN>' command from the dashboard.
    Returns the token string, or None if not found."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("cloudflared"):
        parts = raw.split()
        for p in parts[2:]:
            if not p.startswith("-") and len(p) > 20:
                return p
        return None
    return raw if len(raw) > 20 else None


def _parse_tunnel_url(raw: str) -> str | None:
    """Accept 'https://host' or 'host'. Return 'https://host' normalized,
    or None if the hostname looks invalid."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith("http"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or "." not in parsed.hostname:
        return None
    return raw
```

Note: `urlparse` is already imported at the top of `apps/cli/main.py` but NOT in `cloudflare.py`. Add the import inside `_parse_tunnel_url` (local import) to avoid touching the module's import block, matching the existing pattern in `cloudflare.py` where `base64` is imported locally inside `_extract_domain_from_cert`.

Updated version with local import:

```python
def _parse_tunnel_url(raw: str) -> str | None:
    """Accept 'https://host' or 'host'. Return 'https://host' normalized,
    or None if the hostname looks invalid."""
    from urllib.parse import urlparse
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith("http"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or "." not in parsed.hostname:
        return None
    return raw
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from apps.cli.cloudflare import _parse_tunnel_token, _parse_tunnel_url; print(_parse_tunnel_token('eyJabc123xyz')); print(_parse_tunnel_url('pinetunnel.example.com'))"`
Expected: prints the token string, then `https://pinetunnel.example.com`

- [ ] **Step 3: Verify edge cases**

Run:
```bash
python -c "
from apps.cli.cloudflare import _parse_tunnel_token, _parse_tunnel_url
assert _parse_tunnel_token('cloudflared service install eyJabc123') == 'eyJabc123'
assert _parse_tunnel_token('cloudflared tunnel run --token eyJabc123') == 'eyJabc123'
assert _parse_tunnel_token('') is None
assert _parse_tunnel_token('short') is None
assert _parse_tunnel_url('https://pinetunnel.example.com/') == 'https://pinetunnel.example.com'
assert _parse_tunnel_url('pinetunnel.example.com') == 'https://pinetunnel.example.com'
assert _parse_tunnel_url('') is None
assert _parse_tunnel_url('localhost') is None
print('all edge cases pass')
"
```
Expected: `all edge cases pass`

- [ ] **Step 4: Commit**

```bash
git add apps/cli/cloudflare.py
git commit -m "feat(cli): add tunnel token and URL parsing helpers"
```

---

### Task 2: Add `setup_cloudflare_remotely_managed` function to cloudflare.py

**Files:**
- Modify: `apps/cli/cloudflare.py` (add after `setup_cloudflare_tunnel` function, before `setup_cloudflare_oauth` which will be removed in Task 4)

**Interfaces:**
- Consumes: `is_cloudflared_installed()`, `install_cloudflared()`, `update_env_server_url()`, `_parse_tunnel_token()`, `_parse_tunnel_url()` (all in same file)
- Produces: `setup_cloudflare_remotely_managed(tunnel_token: str, tunnel_url: str, port: int = 8000, yes: bool = False) -> str | None`

- [ ] **Step 1: Add the function after `setup_cloudflare_tunnel` (after line 563, before `setup_cloudflare_oauth` at line 565)**

Insert this code between `setup_cloudflare_tunnel` (ends at line 563 with `return tunnel_url`) and `setup_cloudflare_oauth` (starts at line 565):

```python
def setup_cloudflare_remotely_managed(
    tunnel_token: str,
    tunnel_url: str,
    port: int = 8000,
    yes: bool = False,
) -> str | None:
    """Set up a Cloudflare remotely-managed tunnel (Cloudflare-recommended flow).

    The user creates the tunnel on the Cloudflare dashboard
    (Networking > Tunnels > Create a tunnel), configures the public
    hostname there (e.g., pinetunnel.example.com -> http://localhost:8000),
    then pastes the tunnel token and public URL into this function.

    cloudflared is installed as an OS service (systemd/launchd/sc.exe)
    that starts on boot. Falls back to a background daemon if service
    install fails (e.g., no sudo).

    Returns the public HTTPS URL or None on failure.
    """
    parsed_token = _parse_tunnel_token(tunnel_token)
    if not parsed_token:
        print("  [FAIL] Tunnel token looks invalid (expected 20+ char string).")
        print("         Copy it from the Cloudflare dashboard install command.")
        return None

    parsed_url = _parse_tunnel_url(tunnel_url)
    if not parsed_url:
        print("  [FAIL] Tunnel URL looks invalid.")
        print("         Example: https://pinetunnel.example.com")
        return None

    if not is_cloudflared_installed():
        print("  cloudflared not found. Installing...")
        if not install_cloudflared(yes=yes):
            return None

    print(f"  Installing cloudflared as OS service...")
    svc_proc = subprocess.run(
        ["cloudflared", "service", "install", parsed_token],
        capture_output=True, text=True, timeout=60,
    )

    daemon_fallback = False
    if svc_proc.returncode != 0:
        print(f"  [WARN] Service install failed: {(svc_proc.stderr or '')[:200]}")
        print("  Falling back to background daemon (will NOT survive reboot).")
        daemon_fallback = True
    else:
        print("  [OK]   cloudflared service installed (starts on boot)")

    if daemon_fallback:
        log_path = Path.cwd() / "cloudflared-tunnel.log"
        tunnel_proc = subprocess.Popen(
            ["cloudflared", "tunnel", "run", "--token", parsed_token],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=(platform.system() != "Windows"),
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if platform.system() == "Windows" else 0,
        )
        pid_file = Path.cwd() / "cloudflared.pid"
        pid_file.write_text(str(tunnel_proc.pid))
        time.sleep(3)
        if tunnel_proc.poll() is not None:
            print("  [FAIL] cloudflared exited immediately.")
            try:
                log_content = log_path.read_text()[-500:]
                print(f"  Last log lines: {log_content}")
            except OSError:
                pass
            return None
        print(f"  [OK]   cloudflared running (PID {tunnel_proc.pid})")

    print(f"  Verifying tunnel at {parsed_url}...")
    healthy = False
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            health_req = Request(f"{parsed_url}/health", method="GET")
            with urlopen(health_req, timeout=5) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            pass
        time.sleep(2)

    if healthy:
        print("  [OK]   Tunnel healthy")
    else:
        print("  [WARN] Health check timed out (tunnel may still be initializing).")
        print(f"         Check status at: https://dash.cloudflare.com -> Networking -> Tunnels")

    webhook_url = parsed_url
    print(f"  Updating .env SERVER_BASE_URL to {webhook_url}...")
    if update_env_server_url(webhook_url):
        print("  [OK]   .env updated")
    else:
        print("  [WARN] Could not update .env. Set manually:")
        print(f"         SERVER_BASE_URL={webhook_url}")

    print()
    print("  ========================================")
    print("  Cloudflare Remotely-Managed Tunnel Active!")
    print("  ========================================")
    print()
    print(f"  Tunnel URL:   {webhook_url}")
    if daemon_fallback:
        print(f"  Mode:         Background daemon (PID file: cloudflared.pid)")
        print(f"  Limitation:   Does NOT survive reboot. Re-run setup or install service manually.")
    else:
        print(f"  Mode:         OS service (starts on boot, survives reboots)")
    print(f"  Features:     HTTPS, DDoS protection, WebSocket, no port opening needed")
    print()
    print(f"  TradingView webhook URL: {webhook_url}/")
    print(f"  API docs:                {webhook_url}/docs")
    print()
    if not daemon_fallback:
        print(f"  Stop:         cloudflared service uninstall (or OS service manager)")
    else:
        print(f"  Stop:         pinetunnel stop-cloudflare")
    print()

    return webhook_url
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from apps.cli.cloudflare import setup_cloudflare_remotely_managed; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Verify it returns None on invalid token (does not attempt install)**

Run:
```bash
python -c "
from apps.cli.cloudflare import setup_cloudflare_remotely_managed
result = setup_cloudflare_remotely_managed('short', 'https://pinetunnel.example.com')
assert result is None
print('invalid token returns None: OK')
"
```
Expected: prints the FAIL message about invalid token, then `invalid token returns None: OK`

- [ ] **Step 4: Verify it returns None on invalid URL**

Run:
```bash
python -c "
from apps.cli.cloudflare import setup_cloudflare_remotely_managed
result = setup_cloudflare_remotely_managed('eyJvalidtoken12345678901234567890', 'localhost')
assert result is None
print('invalid URL returns None: OK')
"
```
Expected: prints the FAIL message about invalid URL, then `invalid URL returns None: OK`

- [ ] **Step 5: Commit**

```bash
git add apps/cli/cloudflare.py
git commit -m "feat(cli): add setup_cloudflare_remotely_managed function"
```

---

### Task 3: Change default subdomain from `"webhook"` to `"pinetunnel"` in cloudflare.py

**Files:**
- Modify: `apps/cli/cloudflare.py:415` (the `subdomain: str = "webhook"` default in `setup_cloudflare_dns`)

**Interfaces:**
- Consumes: none
- Produces: `setup_cloudflare_dns` now defaults to `subdomain="pinetunnel"`

- [ ] **Step 1: Change the default value on line 415**

Find this line in `apps/cli/cloudflare.py`:
```python
    subdomain: str = "webhook",
```
Replace with:
```python
    subdomain: str = "pinetunnel",
```

- [ ] **Step 2: Verify the change**

Run: `python -c "import inspect; from apps.cli.cloudflare import setup_cloudflare_dns; sig = inspect.signature(setup_cloudflare_dns); print(sig.parameters['subdomain'].default)"`
Expected: `pinetunnel`

- [ ] **Step 3: Commit**

```bash
git add apps/cli/cloudflare.py
git commit -m "feat(cli): change default Cloudflare subdomain from webhook to pinetunnel"
```

---

### Task 4: Remove deprecated `setup_cloudflare_oauth` and cert-parsing helpers from cloudflare.py

**Files:**
- Modify: `apps/cli/cloudflare.py` (remove lines 565-953: `setup_cloudflare_oauth`, `_extract_domain_from_cert`, `_extract_all_domains_from_cert`)

**Interfaces:**
- Consumes: none
- Produces: `setup_cloudflare_oauth` no longer importable (will be verified in main.py update)

- [ ] **Step 1: Remove the three functions**

Delete these three functions from `apps/cli/cloudflare.py`:
- `setup_cloudflare_oauth` (line 565 to its `return webhook_url` around line 868)
- `_extract_domain_from_cert` (line 871 to line 908)
- `_extract_all_domains_from_cert` (line 911 to line 937)

Keep `_list_all_zones_via_api` (line 940 to 953) - it may be useful for future features and is referenced by no current code path after removal, but removing it is out of scope. Leave it in place.

The file should end with `_list_all_zones_via_api` as the last function.

- [ ] **Step 2: Verify removal**

Run: `python -c "from apps.cli.cloudflare import setup_cloudflare_remotely_managed; print('ok')"`
Expected: `ok` (the new function is unaffected)

Run: `python -c "from apps.cli.cloudflare import setup_cloudflare_oauth" 2>&1 | head -1`
Expected: `ImportError: cannot import name 'setup_cloudflare_oauth'`

- [ ] **Step 3: Commit**

```bash
git add apps/cli/cloudflare.py
git commit -m "refactor(cli): remove deprecated setup_cloudflare_oauth and cert parsing

Cloudflare docs recommend remotely-managed tunnels for production.
Locally-managed OAuth flow replaced by setup_cloudflare_remotely_managed."
```

---

### Task 5: Update `cmd_setup_cloudflare` in main.py to support the new remotely-managed flow

**Files:**
- Modify: `apps/cli/main.py:1947-1996` (the `cmd_setup_cloudflare` function)

**Interfaces:**
- Consumes: `setup_cloudflare_remotely_managed` (from Task 2), `setup_cloudflare_dns`, `setup_cloudflare_tunnel`
- Produces: `cmd_setup_cloudflare` now handles 3 options (A/B/C) and 2 new CLI flags (`--tunnel-token`, `--tunnel-url`)

- [ ] **Step 1: Replace the entire `cmd_setup_cloudflare` function (lines 1947-1996)**

Find the function starting at line 1947:
```python
def cmd_setup_cloudflare(args: argparse.Namespace) -> int:
    """Set up Cloudflare DNS or quick tunnel for HTTPS."""
    from apps.cli.cloudflare import setup_cloudflare_dns, setup_cloudflare_tunnel

    if args.quick:
        url = setup_cloudflare_tunnel(port=args.port, yes=args.yes)
        return 0 if url else 1

    domain = args.domain
    token = args.token

    if not domain and not args.quick:
        # Interactive: ask user which flow
        print()
        print("  Cloudflare Setup Options:")
        print("    A) DNS setup (you have a domain on Cloudflare + API token)")
        print("    B) Quick tunnel (no domain needed, instant HTTPS)")
        print()
        try:
            choice = input("  Choose A or B [B]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            choice = "b"
        if choice == "a":
            try:
                domain = input("  Domain (e.g., example.com): ").strip()
                token = input("  API token: ").strip()
                sub = input(f"  Subdomain [webhook]: ").strip() or "webhook"
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return 130
            args.subdomain = sub
        else:
            url = setup_cloudflare_tunnel(port=args.port, yes=args.yes)
            return 0 if url else 1

    if domain and not token:
        try:
            token = input("  Cloudflare API token: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 130

    url = setup_cloudflare_dns(
        domain=domain,
        api_token=token,
        subdomain=getattr(args, "subdomain", "webhook"),
        port=args.port,
        yes=args.yes,
    )
    return 0 if url else 1
```

Replace with this new version:

```python
def cmd_setup_cloudflare(args: argparse.Namespace) -> int:
    """Set up Cloudflare DNS, quick tunnel, or remotely-managed tunnel for HTTPS."""
    from apps.cli.cloudflare import (
        setup_cloudflare_dns,
        setup_cloudflare_remotely_managed,
        setup_cloudflare_tunnel,
    )

    tunnel_token = getattr(args, "tunnel_token", None)
    tunnel_url = getattr(args, "tunnel_url", None)

    if tunnel_token and tunnel_url:
        url = setup_cloudflare_remotely_managed(
            tunnel_token=tunnel_token,
            tunnel_url=tunnel_url,
            port=args.port,
            yes=args.yes,
        )
        return 0 if url else 1

    if args.quick:
        url = setup_cloudflare_tunnel(port=args.port, yes=args.yes)
        return 0 if url else 1

    domain = args.domain
    token = args.token

    if not domain and not tunnel_token:
        print()
        print("  Cloudflare Setup Options:")
        print("    A) DNS setup (domain + API token, creates pinetunnel.<domain> A record)")
        print("    B) Quick tunnel (no domain needed, instant HTTPS, temporary URL)")
        print("    C) Remotely-managed tunnel (create on Cloudflare dashboard, paste token)")
        print()
        try:
            choice = input("  Choose A/B/C [C]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            choice = "c"
        if choice not in ("a", "b", "c"):
            choice = "c"

        if choice == "a":
            try:
                domain = input("  Domain (e.g., example.com): ").strip()
                token = input("  API token: ").strip()
                sub = input(f"  Subdomain [pinetunnel]: ").strip() or "pinetunnel"
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return 130
            args.subdomain = sub
        elif choice == "b":
            url = setup_cloudflare_tunnel(port=args.port, yes=args.yes)
            return 0 if url else 1
        else:
            try:
                tunnel_token = input("  Paste tunnel token (from dashboard install command): ").strip()
                tunnel_url = input("  Public URL (e.g., https://pinetunnel.example.com): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return 130
            url = setup_cloudflare_remotely_managed(
                tunnel_token=tunnel_token,
                tunnel_url=tunnel_url,
                port=args.port,
                yes=args.yes,
            )
            return 0 if url else 1

    if domain and not token:
        try:
            token = input("  Cloudflare API token: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 130

    url = setup_cloudflare_dns(
        domain=domain,
        api_token=token,
        subdomain=getattr(args, "subdomain", "pinetunnel"),
        port=args.port,
        yes=args.yes,
    )
    return 0 if url else 1
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from apps.cli.main import cmd_setup_cloudflare; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add apps/cli/main.py
git commit -m "feat(cli): update cmd_setup_cloudflare for remotely-managed tunnel

Interactive menu now offers 3 options: A) DNS, B) Quick tunnel,
C) Remotely-managed tunnel. Default is C (Cloudflare-recommended).
Non-interactive: --tunnel-token + --tunnel-url flags."
```

---

### Task 6: Add `--tunnel-token` and `--tunnel-url` CLI flags in main.py

**Files:**
- Modify: `apps/cli/main.py:2488-2498` (the `setup-cloudflare` subparser block)

**Interfaces:**
- Consumes: `cmd_setup_cloudflare` reads `args.tunnel_token` and `args.tunnel_url`
- Produces: two new CLI flags on the `setup-cloudflare` subcommand

- [ ] **Step 1: Add the two new arguments and change the `--subdomain` default**

Find this block (lines 2488-2498):
```python
    # setup-cloudflare
    p_cf = subparsers.add_parser(
        "setup-cloudflare", help="Set up Cloudflare DNS or quick tunnel for HTTPS"
    )
    p_cf.add_argument("--domain", help="Your domain on Cloudflare (e.g., example.com)")
    p_cf.add_argument("--token", help="Cloudflare API token (Zone:DNS:Edit + Zone:Zone:Read)")
    p_cf.add_argument("--subdomain", default="webhook", help="Subdomain (default: webhook)")
    p_cf.add_argument("--quick", action="store_true", help="Use quick tunnel (no domain needed)")
    p_cf.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p_cf.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    p_cf.set_defaults(func=cmd_setup_cloudflare)
```

Replace with:
```python
    # setup-cloudflare
    p_cf = subparsers.add_parser(
        "setup-cloudflare", help="Set up Cloudflare DNS, quick tunnel, or remotely-managed tunnel for HTTPS"
    )
    p_cf.add_argument("--domain", help="Your domain on Cloudflare (e.g., example.com)")
    p_cf.add_argument("--token", help="Cloudflare API token (Zone:DNS:Edit + Zone:Zone:Read)")
    p_cf.add_argument("--subdomain", default="pinetunnel", help="Subdomain (default: pinetunnel)")
    p_cf.add_argument("--quick", action="store_true", help="Use quick tunnel (no domain needed)")
    p_cf.add_argument("--tunnel-token", dest="tunnel_token", help="Remotely-managed tunnel token (from Cloudflare dashboard)")
    p_cf.add_argument("--tunnel-url", dest="tunnel_url", help="Public URL of remotely-managed tunnel (e.g., https://pinetunnel.example.com)")
    p_cf.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p_cf.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    p_cf.set_defaults(func=cmd_setup_cloudflare)
```

- [ ] **Step 2: Verify the CLI shows the new flags**

Run: `python -m apps.cli.main setup-cloudflare --help 2>&1 | grep -E "tunnel-token|tunnel-url|subdomain"`
Expected output includes:
```
  --tunnel-token TUNNEL_TOKEN
                        Remotely-managed tunnel token (from Cloudflare dashboard)
  --tunnel-url TUNNEL_URL
                        Public URL of remotely-managed tunnel (e.g., https://pinetunnel.example.com)
  --subdomain PINETUNNEL
                        Subdomain (default: pinetunnel)
```

Note: `python -m apps.cli.main` may not work if the package needs install. Alternative: `python -c "from apps.cli.main import build_parser; p = build_parser(); p.parse_args(['setup-cloudflare','--help'])"` - but this prints help and exits. Simpler: `pinetunnel setup-cloudflare --help` if installed, else skip to Step 3.

- [ ] **Step 3: Verify the default subdomain changed**

Run:
```bash
python -c "
import argparse
from apps.cli.main import build_parser
parser = build_parser()
args = parser.parse_args(['setup-cloudflare'])
print('subdomain:', args.subdomain)
print('tunnel_token:', args.tunnel_token)
print('tunnel_url:', args.tunnel_url)
"
```
Expected:
```
subdomain: pinetunnel
tunnel_token: None
tunnel_url: None
```

Note: this requires that `build_parser` is the function name. Check the main.py file for the actual parser-building function name first. Looking at the code around line 2540, the function that returns the parser is likely called `build_parser` or similar. If the function is named differently, adjust the command. If unsure, use: `python -c "import apps.cli.main; print([n for n in dir(apps.cli.main) if 'parser' in n.lower()])"` to find the right name.

- [ ] **Step 4: Commit**

```bash
git add apps/cli/main.py
git commit -m "feat(cli): add --tunnel-token and --tunnel-url flags to setup-cloudflare

Also change --subdomain default from 'webhook' to 'pinetunnel'."
```

---

### Task 7: Update `_run_quick_setup` Step 2 in main.py to offer 3 options

**Files:**
- Modify: `apps/cli/main.py:2575-2636` (the Step 2 webhook URL block in `_run_quick_setup`)

**Interfaces:**
- Consumes: `setup_cloudflare_remotely_managed`, `setup_cloudflare_tunnel`, `get_public_ip`, `update_env_server_url` (from cloudflare module)
- Produces: auto-wizard Step 2 now offers A/B/C (IP/Quick/Remote)

- [ ] **Step 1: Replace the Step 2 block (lines 2575-2636)**

Find this block starting at line 2575:
```python
    # Step 2: Webhook URL setup
    print("  --- Step 2/5: Webhook URL ---")
    print()
    print("  TradingView needs a URL to send alerts to.")
    print()
    print("  A) Public IP  (HTTP, no domain needed, zero cost)")
    print("  B) Cloudflare (HTTPS, persistent domain, DDoS protection)")
    print()
    try:
        url_choice = input("  Choose [A/B] (default: A): ").strip().upper()
    except (KeyboardInterrupt, EOFError):
        url_choice = "A"
    if url_choice not in ("A", "B"):
        url_choice = "A"

    if url_choice == "B":
        print()
        print("  Launching Cloudflare browser login...")
        print("  (A browser will open - or copy the URL to your computer)")
        print()
        from apps.cli.cloudflare import setup_cloudflare_oauth
        result = setup_cloudflare_oauth(port=8000, yes=False)
        if not result:
            print("  [WARN] Cloudflare setup failed. Falling back to public IP.")
            url_choice = "A"

    if url_choice == "A":
        print()
        print("  Detecting public IP...")
        from apps.cli.cloudflare import get_public_ip, update_env_server_url
        public_ip = get_public_ip()
        if public_ip:
            webhook_url = f"http://{public_ip}:8000"
            print(f"  [OK]   Public IP: {public_ip}")
            update_env_server_url(webhook_url)
            # Override HOST to 0.0.0.0 for public IP access
            env_p = _find_env_path_for_update()
            if env_p:
                _update_env_var(env_p, "HOST", "0.0.0.0")
            print(f"  [OK]   .env updated: SERVER_BASE_URL={webhook_url}")
            print(f"  [OK]   HOST set to 0.0.0.0 (public access)")
            print()
            print(f"  Webhook URL: {webhook_url}/", "highlight" if _color_supported() else "")
            print(f"  TradingView: paste this URL in your alert webhook field")
            # Open firewall for public IP access
            print()
            print("  Opening firewall for port 8000...")
            _ensure_firewall(8000, yes=True)
            # Restart daemon on 0.0.0.0
            from apps.cli.service import stop_daemon, start_daemon
            stop_daemon()
            time.sleep(2)
            start_daemon("0.0.0.0", 8000, 1)
            print("  [OK]   Server rebound to 0.0.0.0 (public)")
        else:
            print("  [WARN] Could not detect public IP automatically.")
            print("         Set manually in .env: SERVER_BASE_URL=http://YOUR_IP:8000")
    else:
        # Cloudflare tunnel: server stays on localhost, no port opening needed
        print()
        print("  [OK]   Server stays on 127.0.0.1 - no firewall opening needed")
        print("  [OK]   Cloudflare tunnel handles all external traffic")
    print()
```

Replace with:
```python
    # Step 2: Webhook URL setup
    print("  --- Step 2/5: Webhook URL ---")
    print()
    print("  TradingView needs a URL to send alerts to.")
    print()
    print("  A) Public IP         (HTTP, no domain needed, zero cost)")
    print("  B) Quick Tunnel      (HTTPS, no Cloudflare account, temporary URL)")
    print("  C) Cloudflare Tunnel (HTTPS, persistent, requires Cloudflare account)")
    print()
    try:
        url_choice = input("  Choose [A/B/C] (default: A): ").strip().upper()
    except (KeyboardInterrupt, EOFError):
        url_choice = "A"
    if url_choice not in ("A", "B", "C"):
        url_choice = "A"

    if url_choice == "B":
        print()
        print("  Starting Cloudflare quick tunnel...")
        print()
        from apps.cli.cloudflare import setup_cloudflare_tunnel
        result = setup_cloudflare_tunnel(port=8000, yes=False)
        if not result:
            print("  [WARN] Quick tunnel failed. Falling back to public IP.")
            url_choice = "A"

    if url_choice == "C":
        print()
        print("  Create a tunnel on the Cloudflare dashboard:")
        print("    1. Go to https://dash.cloudflare.com -> Networking -> Tunnels")
        print("    2. Click 'Create a tunnel', name it (e.g., pinetunnel)")
        print("    3. On the Routes tab, add a Published application:")
        print("       - Subdomain: pinetunnel (or any name you want)")
        print("       - Domain: select your domain from the dropdown")
        print("       - Service URL: http://localhost:8000")
        print("    4. Copy the tunnel token from the install command shown")
        print()
        try:
            tunnel_token = input("  Paste tunnel token: ").strip()
            tunnel_url = input("  Public URL (e.g., https://pinetunnel.example.com): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled. Falling back to public IP.")
            url_choice = "A"
            tunnel_token = ""
        if url_choice == "C" and tunnel_token and tunnel_url:
            from apps.cli.cloudflare import setup_cloudflare_remotely_managed
            result = setup_cloudflare_remotely_managed(
                tunnel_token=tunnel_token,
                tunnel_url=tunnel_url,
                port=8000,
                yes=False,
            )
            if not result:
                print("  [WARN] Cloudflare tunnel setup failed. Falling back to public IP.")
                url_choice = "A"
        elif url_choice == "C":
            print("  [WARN] Missing token or URL. Falling back to public IP.")
            url_choice = "A"

    if url_choice == "A":
        print()
        print("  Detecting public IP...")
        from apps.cli.cloudflare import get_public_ip, update_env_server_url
        public_ip = get_public_ip()
        if public_ip:
            webhook_url = f"http://{public_ip}:8000"
            print(f"  [OK]   Public IP: {public_ip}")
            update_env_server_url(webhook_url)
            # Override HOST to 0.0.0.0 for public IP access
            env_p = _find_env_path_for_update()
            if env_p:
                _update_env_var(env_p, "HOST", "0.0.0.0")
            print(f"  [OK]   .env updated: SERVER_BASE_URL={webhook_url}")
            print(f"  [OK]   HOST set to 0.0.0.0 (public access)")
            print()
            print(f"  Webhook URL: {webhook_url}/", "highlight" if _color_supported() else "")
            print(f"  TradingView: paste this URL in your alert webhook field")
            # Open firewall for public IP access
            print()
            print("  Opening firewall for port 8000...")
            _ensure_firewall(8000, yes=True)
            # Restart daemon on 0.0.0.0
            from apps.cli.service import stop_daemon, start_daemon
            stop_daemon()
            time.sleep(2)
            start_daemon("0.0.0.0", 8000, 1)
            print("  [OK]   Server rebound to 0.0.0.0 (public)")
        else:
            print("  [WARN] Could not detect public IP automatically.")
            print("         Set manually in .env: SERVER_BASE_URL=http://YOUR_IP:8000")
    else:
        # Cloudflare tunnel: server stays on localhost, no port opening needed
        print()
        print("  [OK]   Server stays on 127.0.0.1 - no firewall opening needed")
        print("  [OK]   Cloudflare tunnel handles all external traffic")
    print()
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from apps.cli.main import _run_quick_setup; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Verify no reference to setup_cloudflare_oauth remains in main.py**

Run: `grep -n "setup_cloudflare_oauth" apps/cli/main.py`
Expected: no output (the import was removed in this task)

- [ ] **Step 4: Commit**

```bash
git add apps/cli/main.py
git commit -m "feat(cli): update auto-wizard Step 2 to offer 3 URL options

A) Public IP (unchanged), B) Quick tunnel, C) Remotely-managed
Cloudflare tunnel. Removes deprecated OAuth browser-login flow
from the auto-wizard. Defaults to A (zero-cost path)."
```

---

### Task 8: Update README.md Cloudflare references

**Files:**
- Modify: `README.md:143` (the CLI commands list `setup-cloudflare` description)

**Interfaces:**
- Consumes: none
- Produces: updated documentation

- [ ] **Step 1: Update the setup-cloudflare description line**

Find this line in `README.md` (line 143):
```
pinetunnel setup-cloudflare   Set up Cloudflare DNS or tunnel for HTTPS
```

Replace with:
```
pinetunnel setup-cloudflare   Set up Cloudflare DNS, quick tunnel, or remotely-managed tunnel for HTTPS
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README for remotely-managed Cloudflare tunnel option"
```

---

### Task 9: Final integration verification

**Files:**
- None (verification only)

- [ ] **Step 1: Verify all imports work end-to-end**

Run:
```bash
python -c "
from apps.cli.cloudflare import (
    setup_cloudflare_dns,
    setup_cloudflare_tunnel,
    setup_cloudflare_remotely_managed,
    _parse_tunnel_token,
    _parse_tunnel_url,
)
from apps.cli.main import cmd_setup_cloudflare, _run_quick_setup
print('all imports OK')
"
```
Expected: `all imports OK`

- [ ] **Step 2: Verify deprecated function is gone**

Run: `python -c "from apps.cli.cloudflare import setup_cloudflare_oauth" 2>&1 | head -1`
Expected: `ImportError: cannot import name 'setup_cloudflare_oauth'`

- [ ] **Step 3: Run the existing test suite (regression check)**

Run: `pytest tests/ -v 2>&1 | tail -20`
Expected: all existing tests pass (83 tests). No new tests added.

- [ ] **Step 4: Reinstall the package and verify CLI entry point**

Run:
```bash
pip install -e . 2>&1 | tail -3
pinetunnel --help 2>&1 | head -30
```
Expected: help output shows `setup-cloudflare` command. No import errors.

- [ ] **Step 5: Verify setup-cloudflare --help shows new flags**

Run: `pinetunnel setup-cloudflare --help 2>&1`
Expected: shows `--tunnel-token`, `--tunnel-url`, `--subdomain` (default: pinetunnel), `--quick`, `--domain`, `--token`, `--yes`, `--port`

- [ ] **Step 6: ASCII check on modified files**

Run:
```bash
rg -n '[^\x00-\x7F]' apps/cli/cloudflare.py apps/cli/main.py README.md
```
Expected: no output (all user-facing strings are ASCII-only per AGENTS.md)

- [ ] **Step 7: Commit (if any whitespace/cleanup needed)**

If steps 1-6 all pass without changes, no commit needed. If any cleanup was required:
```bash
git add -A
git commit -m "chore: final cleanup for Cloudflare remotely-managed tunnel"
```

---

## Self-Review Notes

**Spec coverage:**
- Spec section "CLI surface" -> Tasks 5, 6, 7 (cmd_setup_cloudflare, flags, auto-wizard)
- Spec section "setup_cloudflare_remotely_managed function" -> Task 2
- Spec section "Token & URL parsing helpers" -> Task 1
- Spec section "Error handling" -> Task 2 (all error paths in the function)
- Spec section "Files changed" -> Tasks 1-8 cover all listed files
- Spec section "Verification" -> Task 9 covers all manual verification steps

**Placeholder scan:** No TBD, TODO, or vague steps. All code blocks contain complete implementation.

**Type consistency:** `setup_cloudflare_remotely_managed(tunnel_token: str, tunnel_url: str, port: int, yes: bool) -> str | None` - signature matches across Task 2 (definition), Task 5 (cmd_setup_cloudflare call), Task 7 (auto-wizard call). `_parse_tunnel_token(raw: str) -> str | None` and `_parse_tunnel_url(raw: str) -> str | None` - signatures match across Task 1 (definition) and Task 2 (usage).

"""Cloudflare DNS and Tunnel integration for PineTunnel.

Two flows:
  Flow A: User has a Cloudflare-managed domain + API token.
          -> Creates webhook.domain.com A record pointing to VPS IP.
          -> Proxied through Cloudflare (HTTPS, DDoS protection).
          -> Updates .env SERVER_BASE_URL.

  Flow B: User has no domain. Quick tunnel via cloudflared.
          -> Installs cloudflared if needed.
          -> Starts: cloudflared tunnel --url http://localhost:8000
          -> Gets https://random-words.trycloudflare.com
          -> Updates .env SERVER_BASE_URL.
          -> Instant HTTPS, no domain needed.

API docs: https://developers.cloudflare.com/api/resources/dns/subresources/records/
Tunnel docs: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = "https://api.cloudflare.com/client/v4"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CloudflareError(Exception):
    """Base Cloudflare error."""
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class CloudflareAuthError(CloudflareError):
    """Invalid or expired API token."""
    pass


class ZoneNotFoundError(CloudflareError):
    """Domain not found in Cloudflare account."""
    pass


class RecordExistsError(CloudflareError):
    """DNS record already exists."""
    pass


# ---------------------------------------------------------------------------
# API helpers (sync urllib, no extra deps)
# ---------------------------------------------------------------------------

def _api_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make a Cloudflare API v4 request. Returns parsed JSON response."""
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("success"):
                errors = result.get("errors", [])
                code = errors[0].get("code", resp.status) if errors else resp.status
                msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                if code == 81053:
                    raise RecordExistsError(code, msg)
                raise CloudflareError(code, msg)
            return result
    except HTTPError as e:
        body_text = e.read().decode() if e.fp else "{}"
        try:
            data = json.loads(body_text)
            errors = data.get("errors", [])
            code = errors[0].get("code", e.code) if errors else e.code
            msg = errors[0].get("message", body_text[:200]) if errors else body_text[:200]
        except (json.JSONDecodeError, IndexError):
            code, msg = e.code, body_text[:200]
        if e.code == 401:
            raise CloudflareAuthError(code, "Invalid or expired API token")
        if e.code == 403:
            raise CloudflareAuthError(code, "Token lacks required permissions (need Zone:DNS:Edit + Zone:Zone:Read)")
        raise CloudflareError(code, msg)
    except URLError as e:
        raise CloudflareError(0, f"Network error: {e.reason}")


def get_zone_id(domain: str, token: str) -> str:
    """Find zone ID from a domain name (handles subdomains).

    Strips labels from the left until a zone matches.
    e.g., webhook.example.com -> tries webhook.example.com, then example.com
    """
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        try:
            result = _api_request("GET", f"/zones?name={candidate}", token)
            zones = result.get("result", [])
            if zones:
                return zones[0]["id"]
        except CloudflareError:
            continue
    raise ZoneNotFoundError(404, f"No Cloudflare zone found for {domain}. Ensure the domain is added to your Cloudflare account.")


def list_dns_records(zone_id: str, name: str, token: str, rtype: str | None = None) -> list[dict]:
    """List DNS records filtered by name (and optionally type)."""
    params = f"?name.exact={name}"
    if rtype:
        params += f"&type={rtype}"
    result = _api_request("GET", f"/zones/{zone_id}/dns_records{params}", token)
    return result.get("result", [])


def create_dns_record(zone_id: str, rtype: str, name: str, content: str, token: str, proxied: bool = True) -> dict:
    """Create a DNS record. ttl=1 means automatic (required for proxied)."""
    body = {
        "type": rtype,
        "name": name,
        "content": content,
        "proxied": proxied,
        "ttl": 1,
    }
    result = _api_request("POST", f"/zones/{zone_id}/dns_records", token, body)
    return result["result"]


def update_dns_record(zone_id: str, record_id: str, rtype: str, name: str, content: str, token: str, proxied: bool = True) -> dict:
    """Update (overwrite) an existing DNS record."""
    body = {
        "type": rtype,
        "name": name,
        "content": content,
        "proxied": proxied,
        "ttl": 1,
    }
    result = _api_request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", token, body)
    return result["result"]


def upsert_dns_record(zone_id: str, rtype: str, name: str, content: str, token: str, proxied: bool = True) -> dict:
    """Create or update a DNS record (idempotent).

    If the record exists with the same content+proxied, returns it as-is.
    If it exists with different content, updates it.
    If it doesn't exist, creates it.
    """
    existing = list_dns_records(zone_id, name, token, rtype)
    if existing:
        record = existing[0]
        if record["content"] == content and record.get("proxied") == proxied:
            return record
        return update_dns_record(zone_id, record["id"], rtype, name, content, token, proxied)
    return create_dns_record(zone_id, rtype, name, content, token, proxied)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def validate_token(token: str) -> dict:
    """Validate an API token. Returns token info if valid."""
    try:
        result = _api_request("GET", "/user/tokens/verify", token)
        return result.get("result", {})
    except CloudflareAuthError:
        return {}


# ---------------------------------------------------------------------------
# Public IP detection
# ---------------------------------------------------------------------------

def get_public_ip() -> str | None:
    """Detect the server's public IP address."""
    try:
        with urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        try:
            with urlopen("https://ifconfig.me", timeout=5) as resp:
                return resp.read().decode().strip()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# .env update helper
# ---------------------------------------------------------------------------

def _find_env_path() -> Path | None:
    """Find .env file in project root."""
    p = Path.cwd()
    while p != p.parent:
        if (p / ".env").exists():
            return p / ".env"
        p = p.parent
    return None


def update_env_server_url(new_url: str) -> bool:
    """Update SERVER_BASE_URL in .env file. Returns True if updated."""
    env_path = _find_env_path()
    if not env_path:
        return False
    lines = env_path.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("SERVER_BASE_URL="):
            lines[i] = f"SERVER_BASE_URL={new_url}"
            updated = True
            break
    if updated:
        env_path.write_text("\n".join(lines) + "\n")
    return updated


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


# ---------------------------------------------------------------------------
# cloudflared detection and install
# ---------------------------------------------------------------------------

def is_cloudflared_installed() -> bool:
    """Check if cloudflared binary is available."""
    return shutil.which("cloudflared") is not None


def install_cloudflared(yes: bool = False) -> bool:
    """Install cloudflared on the current OS."""
    os_name = platform.system()
    if os_name == "Darwin":
        print("  Installing cloudflared via Homebrew...")
        cmd = ["brew", "install", "cloudflared"]
    elif os_name == "Linux":
        # Try apt first, then yum/dnf
        if shutil.which("apt-get"):
            print("  Installing cloudflared via apt...")
            cmds = [
                _sudo_prefix() + ["apt-get", "update", "-y"],
                _sudo_prefix() + ["apt-get", "install", "-y", "cloudflared"],
            ]
            for c in cmds:
                r = subprocess.run(c, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    # Fallback: binary download
                    return _install_cloudflared_binary()
            return True
        elif shutil.which("yum"):
            print("  Installing cloudflared via yum...")
            cmd = _sudo_prefix() + ["yum", "install", "-y", "cloudflared"]
        elif shutil.which("dnf"):
            print("  Installing cloudflared via dnf...")
            cmd = _sudo_prefix() + ["dnf", "install", "-y", "cloudflared"]
        else:
            return _install_cloudflared_binary()
    elif os_name == "Windows":
        if shutil.which("winget"):
            print("  Installing cloudflared via winget...")
            cmd = ["winget", "install", "--id", "Cloudflare.cloudflared", "--accept-source-agreements", "--accept-package-agreements"]
        else:
            print("  Download cloudflared from:")
            print("    https://github.com/cloudflared/cloudflared/releases/latest")
            return False
    else:
        print(f"  [FAIL] Unsupported OS: {os_name}")
        return False

    if os_name != "Linux" or not shutil.which("apt-get"):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                print("  [OK]   cloudflared installed")
                return True
            else:
                print(f"  [FAIL] Install failed: {r.stderr[:200]}")
                return False
        except Exception as e:
            print(f"  [FAIL] Install error: {e}")
            return False
    return True


def _sudo_prefix() -> list[str]:
    """Return sudo prefix for the current OS."""
    if platform.system() == "Windows":
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    return []


def _install_cloudflared_binary() -> bool:
    """Download cloudflared binary directly (fallback)."""
    os_name = platform.system()
    machine = platform.machine()
    print("  Downloading cloudflared binary...")
    if os_name == "Linux" and "x86_64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    elif os_name == "Linux" and "aarch64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    elif os_name == "Darwin" and "arm64" in machine:
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
    elif os_name == "Darwin":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    else:
        print(f"  [FAIL] No binary available for {os_name} {machine}")
        return False
    print(f"  Download from: {url}")
    print(f"  Then: chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/")
    return False


# ---------------------------------------------------------------------------
# Quick tunnel (no Cloudflare account needed)
# ---------------------------------------------------------------------------

def start_quick_tunnel(port: int = 8000, yes: bool = False) -> str | None:
    """Start a Cloudflare quick tunnel. Returns the trycloudflare.com URL.

    This runs cloudflared in the background and captures the URL from stderr.
    The tunnel stays alive as long as the cloudflared process runs.
    """
    if not is_cloudflared_installed():
        print("  cloudflared not found. Installing...")
        if not install_cloudflared(yes=yes):
            return None

    print(f"  Starting Cloudflare quick tunnel for localhost:{port}...")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Read stderr until we find the trycloudflare.com URL
    tunnel_url = None
    deadline = time.time() + 30  # 30s timeout
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        match = TRYCLOUDFLARE_RE.search(line)
        if match:
            tunnel_url = match.group(0)
            break

    if tunnel_url:
        # Write PID file so we can stop it later
        pid_file = Path.cwd() / "cloudflared.pid"
        pid_file.write_text(str(proc.pid))
        print(f"  [OK]   Quick tunnel started: {tunnel_url}")
        print(f"  PID:   {pid_file} (PID {proc.pid})")
        print(f"  Note:  This URL is temporary. It changes on every restart.")
        print(f"  Stop:  pinetunnel stop-cloudflare")
        return tunnel_url
    else:
        print("  [FAIL] Could not start quick tunnel (timeout)")
        proc.terminate()
        return None


def stop_quick_tunnel() -> bool:
    """Stop a running cloudflared quick tunnel."""
    pid_file = Path.cwd() / "cloudflared.pid"
    if not pid_file.exists():
        print("  [WARN] No cloudflared tunnel running (no PID file)")
        return False
    try:
        pid = int(pid_file.read_text().strip())
        import signal
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
        pid_file.unlink()
        print(f"  [OK]   Cloudflare tunnel stopped (PID {pid})")
        return True
    except Exception as e:
        print(f"  [FAIL] Could not stop tunnel: {e}")
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Main orchestrators
# ---------------------------------------------------------------------------

def setup_cloudflare_dns(
    domain: str,
    api_token: str,
    subdomain: str = "webhook",
    port: int = 8000,
    yes: bool = False,
) -> str | None:
    """Set up Cloudflare DNS for PineTunnel (Flow A).

    1. Validate API token
    2. Find zone ID for the domain
    3. Detect public IP
    4. Create/update A record: {subdomain}.{domain} -> {ip} (proxied)
    5. Update .env SERVER_BASE_URL to https://{subdomain}.{domain}
    6. Return the final HTTPS URL

    Returns the webhook URL or None on failure.
    """
    # Validate token
    print("  Step 1: Validating Cloudflare API token...")
    token_info = validate_token(api_token)
    if not token_info:
        print("  [FAIL] Invalid API token. Get one at:")
        print("         https://dash.cloudflare.com/profile/api-tokens")
        print("         Required permissions: Zone:DNS:Edit + Zone:Zone:Read")
        return None
    print("  [OK]   API token is valid")

    # Find zone
    print(f"  Step 2: Finding Cloudflare zone for {domain}...")
    try:
        zone_id = get_zone_id(domain, api_token)
    except ZoneNotFoundError as e:
        print(f"  [FAIL] {e}")
        return None
    print(f"  [OK]   Zone ID: {zone_id}")

    # Detect public IP
    print("  Step 3: Detecting server public IP...")
    public_ip = get_public_ip()
    if not public_ip:
        print("  [FAIL] Could not detect public IP")
        return None
    print(f"  [OK]   Public IP: {public_ip}")

    # Create/update DNS record
    hostname = f"{subdomain}.{domain}"
    print(f"  Step 4: Creating DNS A record: {hostname} -> {public_ip} (proxied)...")
    try:
        record = upsert_dns_record(zone_id, "A", hostname, public_ip, api_token, proxied=True)
        print(f"  [OK]   DNS record ready: {record['name']} -> {record['content']} (proxied)")
    except RecordExistsError:
        # Record exists, try to update
        print("  [INFO] Record exists, updating...")
        existing = list_dns_records(zone_id, hostname, api_token, "A")
        if existing:
            record = update_dns_record(zone_id, existing[0]["id"], "A", hostname, public_ip, api_token, proxied=True)
            print(f"  [OK]   DNS record updated: {record['name']} -> {record['content']}")
        else:
            print("  [FAIL] Could not create or update DNS record")
            return None
    except CloudflareError as e:
        print(f"  [FAIL] {e}")
        return None

    # Verify
    print("  Step 5: Verifying DNS record...")
    time.sleep(2)
    records = list_dns_records(zone_id, hostname, api_token, "A")
    if records and records[0]["content"] == public_ip:
        print("  [OK]   DNS record verified")
    else:
        print("  [WARN] DNS propagation may take a few minutes")

    # Update .env
    webhook_url = f"https://{hostname}"
    print(f"  Step 6: Updating .env SERVER_BASE_URL to {webhook_url}...")
    if update_env_server_url(webhook_url):
        print("  [OK]   .env updated")
    else:
        print("  [WARN] Could not update .env (file not found). Set manually:")
        print(f"         SERVER_BASE_URL={webhook_url}")

    print()
    print(f"  ========================================")
    print(f"  Cloudflare DNS Setup Complete!")
    print(f"  ========================================")
    print()
    print(f"  Webhook URL:  {webhook_url}")
    print(f"  DNS Record:   {hostname} -> {public_ip} (proxied through Cloudflare)")
    print(f"  Features:     HTTPS, DDoS protection, WebSocket support")
    print()
    print(f"  TradingView webhook URL: {webhook_url}/")
    print(f"  API docs:                {webhook_url}/docs")
    print()
    print(f"  Next steps:")
    print(f"    1. Ensure PineTunnel server is running: pinetunnel start --daemon")
    print(f"    2. Open port {port} on your firewall: pinetunnel check")
    print(f"    3. Send a test signal: pinetunnel test")
    print()

    return webhook_url


def setup_cloudflare_tunnel(
    port: int = 8000,
    yes: bool = False,
) -> str | None:
    """Set up Cloudflare quick tunnel (Flow B - no domain needed).

    1. Ensure cloudflared is installed
    2. Start quick tunnel: cloudflared tunnel --url http://localhost:{port}
    3. Capture the trycloudflare.com URL
    4. Update .env SERVER_BASE_URL
    5. Return the HTTPS URL

    Returns the tunnel URL or None on failure.
    """
    print("  Cloudflare Quick Tunnel (no domain needed)")
    print("  This gives you an instant HTTPS URL via Cloudflare's edge network.")
    print()

    tunnel_url = start_quick_tunnel(port=port, yes=yes)
    if not tunnel_url:
        return None

    # Update .env
    print(f"  Updating .env SERVER_BASE_URL to {tunnel_url}...")
    if update_env_server_url(tunnel_url):
        print("  [OK]   .env updated")
    else:
        print("  [WARN] Could not update .env. Set manually:")
        print(f"         SERVER_BASE_URL={tunnel_url}")

    print()
    print(f"  ========================================")
    print(f"  Cloudflare Quick Tunnel Active!")
    print(f"  ========================================")
    print()
    print(f"  Tunnel URL:   {tunnel_url}")
    print(f"  Features:     HTTPS, WebSocket support")
    print(f"  Limitations:  Temporary URL (changes on restart)")
    print(f"                200 concurrent request limit")
    print()
    print(f"  TradingView webhook URL: {tunnel_url}/")
    print(f"  API docs:                {tunnel_url}/docs")
    print()
    print(f"  Stop tunnel:  pinetunnel stop-cloudflare")
    print()

    return tunnel_url


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


def setup_cloudflare_oauth(port: int = 8000, yes: bool = False) -> str | None:
    """Set up Cloudflare named tunnel via browser OAuth login.

    Handles all edge cases:
    - cloudflared not installed -> auto-install
    - No browser on remote VPS -> print URL for manual copy
    - Login timeout -> retry or skip
    - cert.pem not found -> check all known locations
    - Multiple domains in account -> list and let user choose
    - Tunnel already exists -> reuse
    - DNS record already exists -> reuse
    - Tunnel exits immediately -> show logs
    - .env not found -> print manual instructions
    - Service install fails -> warn, continue
    """
    # Step 1: Ensure cloudflared is installed
    if not is_cloudflared_installed():
        print("  cloudflared not found. Installing...")
        if not install_cloudflared(yes=yes):
            return None

    # Step 2: Check if already logged in (cert.pem exists)
    cert_dir = Path.home() / ".cloudflared"
    cert_path = cert_dir / "cert.pem"
    already_logged_in = cert_path.exists()

    if not already_logged_in:
        # Check alternative locations
        for p in [Path("/etc/cloudflared/cert.pem"), Path("/usr/local/etc/cloudflared/cert.pem")]:
            if p.exists():
                cert_path = p
                already_logged_in = True
                break

    if not already_logged_in:
        # Step 2a: Browser login
        print()
        print("  Opening Cloudflare login in your browser...")
        print("  If you are on a remote VPS, copy the URL and open it on your computer.")
        print("  You have 120 seconds to complete the login.")
        print()

        login_proc = subprocess.Popen(
            ["cloudflared", "tunnel", "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Stream stderr to show the URL immediately
        login_url = None
        import threading

        def _read_url():
            nonlocal login_url
            for line in login_proc.stderr:
                if "https://" in line and login_url is None:
                    login_url = line.strip()
                    print(f"  URL: {login_url}")

        url_thread = threading.Thread(target=_read_url, daemon=True)
        url_thread.start()

        try:
            login_proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            login_proc.terminate()
            print()
            print("  [FAIL] Login timed out (120s).")
            try:
                login_url_display = login_url or "Check cloudflared output above"
                print(f"  If you opened the URL but it didn't complete, try again.")
            except Exception:
                pass
            return None

        if login_proc.returncode != 0:
            print("  [FAIL] Login failed or was cancelled.")
            if login_url:
                print(f"  You can try opening this URL manually: {login_url}")
            return None

        # Verify cert.pem
        if not cert_path.exists():
            for p in [Path("/etc/cloudflared/cert.pem"), Path("/usr/local/etc/cloudflared/cert.pem")]:
                if p.exists():
                    cert_path = p
                    break
            else:
                print("  [FAIL] cert.pem not found after login. Check ~/.cloudflared/")
                return None

        print("  [OK]   Cloudflare login successful")
    else:
        print("  [OK]   Already logged in to Cloudflare (cert.pem found)")

    # Step 3: Auto-detect domain from cert.pem
    print()
    print("  Detecting domains...")

    # cert.pem is a base64-encoded JSON token containing zoneID + apiToken.
    # We decode it and use the API to get the zone name.
    # The user already selected ONE domain in the browser, so cert.pem
    # has that specific zoneID. We fetch its name automatically.
    detected_domain = _extract_domain_from_cert(cert_path)

    # If single domain detected, use it. If not, try listing all zones.
    available_domains = []
    if not detected_domain:
        available_domains = _extract_all_domains_from_cert(cert_path)
        if available_domains and len(available_domains) == 1:
            detected_domain = available_domains[0]

    if detected_domain:
        # Single domain found - use it automatically
        domain = detected_domain
        print(f"  [OK]   Domain: {domain}")
    elif available_domains and len(available_domains) > 1:
        # Multiple domains - let user choose
        print(f"  Found {len(available_domains)} domains:")
        for i, d in enumerate(available_domains, 1):
            print(f"    {i}) {d}")
        print()
        try:
            choice = input(f"  Select domain [1-{len(available_domains)}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available_domains):
                domain = available_domains[idx]
            else:
                print("  [FAIL] Invalid selection")
                return None
        except ValueError:
            print("  [FAIL] Invalid number")
            return None
    else:
        # Could not auto-detect - ask user
        print("  [INFO] Could not auto-detect domain.")
        try:
            domain = input("  Enter your domain (e.g., example.com): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return None

    if not domain:
        print("  [FAIL] Domain is required")
        return None

    # Subdomain is hardcoded as "webhook" - not a question
    subdomain = "webhook"
    print(f"  [OK]   Subdomain: {subdomain} (default)")

    hostname = f"{subdomain}.{domain}"

    # Step 5: Create named tunnel (handle existing)
    print()
    print(f"  Creating Cloudflare tunnel 'pinetunnel'...")
    create_proc = subprocess.run(
        ["cloudflared", "tunnel", "create", "pinetunnel"],
        capture_output=True, text=True, timeout=30,
    )
    if create_proc.returncode != 0:
        # Check if tunnel already exists
        list_proc = subprocess.run(
            ["cloudflared", "tunnel", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if "pinetunnel" in list_proc.stdout:
            print("  [OK]   Tunnel 'pinetunnel' already exists (reusing)")
        else:
            err_msg = create_proc.stderr.strip()[:200] if create_proc.stderr else "Unknown error"
            print(f"  [FAIL] Could not create tunnel: {err_msg}")
            return None
    else:
        print("  [OK]   Tunnel created")

    # Step 6: Extract tunnel ID
    tunnel_id = None
    if cert_dir.is_dir():
        # Find the most recent .json file that's not cert.pem
        json_files = sorted(
            [f for f in cert_dir.glob("*.json") if f.name != "cert.pem"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if json_files:
            tunnel_id = json_files[0].stem

    if not tunnel_id:
        # Parse from create output
        for line in create_proc.stdout.splitlines():
            parts = line.split()
            for p in parts:
                if len(p) == 36 and p.count("-") == 4:  # UUID format
                    tunnel_id = p
                    break

    if not tunnel_id:
        print("  [WARN] Could not extract tunnel ID. Using name 'pinetunnel'.")
        tunnel_id = "pinetunnel"

    # Step 7: Route DNS (handle existing)
    print(f"  Creating DNS record: {hostname} -> tunnel...")
    route_proc = subprocess.run(
        ["cloudflared", "tunnel", "route", "dns", "pinetunnel", hostname],
        capture_output=True, text=True, timeout=30,
    )
    if route_proc.returncode != 0:
        err_lower = route_proc.stderr.lower() if route_proc.stderr else ""
        if "already exists" in err_lower:
            print("  [OK]   DNS record already exists (reusing)")
        else:
            print(f"  [WARN] DNS route: {(route_proc.stderr or '')[:200]}")
            print("         You may need to create it manually in Cloudflare dashboard")
    else:
        print("  [OK]   DNS CNAME created (proxied, free SSL)")

    # Step 8: Write config.yml
    cred_file = cert_dir / f"{tunnel_id}.json"
    if not cred_file.exists():
        # Fallback: find any .json that matches
        for f in cert_dir.glob("*.json"):
            if f.name != "cert.pem":
                cred_file = f
                break

    config_path = cert_dir / "config.yml"
    config_content = f"""tunnel: {tunnel_id}
credentials-file: {cred_file}

ingress:
  - hostname: {hostname}
    service: http://localhost:{port}
  - service: http_status:404
"""
    config_path.write_text(config_content)
    print(f"  [OK]   Config written to {config_path}")

    # Step 9: Start tunnel (handle immediate exit)
    print(f"  Starting tunnel...")
    log_path = Path.cwd() / "cloudflared-tunnel.log"
    tunnel_proc = subprocess.Popen(
        ["cloudflared", "tunnel", "run", "pinetunnel"],
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
        print("  [FAIL] Tunnel exited immediately.")
        print(f"  Check log: {log_path}")
        try:
            log_content = log_path.read_text()[-500:]
            print(f"  Last log lines: {log_content}")
        except OSError:
            pass
        return None

    print(f"  [OK]   Tunnel running (PID {tunnel_proc.pid})")

    # Step 10: Update .env
    webhook_url = f"https://{hostname}"
    if update_env_server_url(webhook_url):
        print(f"  [OK]   .env updated: SERVER_BASE_URL={webhook_url}")
    else:
        print(f"  [WARN] Set manually in .env: SERVER_BASE_URL={webhook_url}")

    # Step 11: Install as service (handle failure)
    print()
    try:
        do_service = input("  Install cloudflared as OS service (auto-start on boot)? [Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        do_service = "n"
    if do_service in ("y", "yes", ""):
        svc_proc = subprocess.run(
            ["cloudflared", "service", "install"],
            capture_output=True, text=True, timeout=30,
        )
        if svc_proc.returncode == 0:
            print("  [OK]   cloudflared service installed (starts on boot)")
        else:
            print(f"  [WARN] Service install failed: {(svc_proc.stderr or '')[:200]}")
            print("         You can start tunnel manually: cloudflared tunnel run pinetunnel")

    print()
    print(f"  ========================================")
    print(f"  Cloudflare Tunnel Active!")
    print(f"  ========================================")
    print()
    print(f"  Webhook URL:  {webhook_url}/")
    print(f"  Features:     HTTPS, DDoS protection, WebSocket, no rate limits")
    print(f"  Persistent:   Survives reboots (if service installed)")
    print()

    return webhook_url


def _extract_domain_from_cert(cert_path: Path) -> str | None:
    """Extract the domain name from cert.pem created by cloudflared tunnel login.

    cert.pem is NOT a standard X.509 certificate. It's a base64-encoded JSON
    token (between "BEGIN ARGO TUNNEL TOKEN" markers) containing:
      {"zoneID": "...", "accountID": "...", "apiToken": "..."}

    We decode it, extract the apiToken, then call the Cloudflare API
    to get the zone name from the zoneID.
    """
    try:
        import base64

        content = cert_path.read_text()
        # Extract base64 content between PEM markers
        lines = [l.strip() for l in content.splitlines() if not l.startswith("-----")]
        if not lines:
            return None

        b64 = "".join(lines)
        decoded = base64.b64decode(b64).decode()
        data = json.loads(decoded)

        zone_id = data.get("zoneID") or data.get("zoneId")
        api_token = data.get("apiToken") or data.get("api_token")

        if not zone_id or not api_token:
            return None

        # Use API to get zone name from zone ID
        result = _api_request("GET", f"/zones/{zone_id}", api_token)
        zone_name = result.get("result", {}).get("name")
        if zone_name:
            return zone_name

    except Exception:
        pass
    return None


def _extract_all_domains_from_cert(cert_path: Path) -> list[str]:
    """Extract ALL available domains using the API token from cert.pem.

    Decodes the ARGO TUNNEL TOKEN, extracts the apiToken,
    then lists all zones in the account.
    """
    try:
        import base64

        content = cert_path.read_text()
        lines = [l.strip() for l in content.splitlines() if not l.startswith("-----")]
        if not lines:
            return []

        b64 = "".join(lines)
        decoded = base64.b64decode(b64).decode()
        data = json.loads(decoded)

        api_token = data.get("apiToken") or data.get("api_token")
        if not api_token:
            return []

        # List all active zones
        return _list_all_zones_via_api(api_token)

    except Exception:
        return []


def _list_all_zones_via_api(token: str) -> list[str]:
    """List all zones (domains) in the Cloudflare account using the API.

    Uses the API token extracted from cert.pem or provided by user.
    Returns list of domain names (active zones only).
    """
    domains = []
    try:
        result = _api_request("GET", "/zones?status=active&per_page=50", token)
        for zone in result.get("result", []):
            domains.append(zone["name"])
    except CloudflareError:
        pass
    return domains

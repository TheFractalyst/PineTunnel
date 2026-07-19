"""PineTunnel CLI - interactive setup wizard + server launcher.

Commands:
  pinetunnel             First run: setup wizard. Then: start server
  pinetunnel start       Start the server (--foreground for logs)
  pinetunnel stop        Stop the daemon
  pinetunnel status      Check if daemon is running
  pinetunnel version     Show version info
  pinetunnel setup       Re-run the setup wizard
"""

import argparse
import importlib
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from apps.cli import __version__
from apps.lib.env_manager import generate_secret, read_env, write_env_updates
from apps.lib.service import is_running, start_daemon, stop_daemon

_REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("sqlalchemy", "sqlalchemy"),
    ("alembic", "alembic"),
    ("redis", "redis"),
    ("httpx", "httpx"),
    ("psutil", "psutil"),
    ("telegram", "python-telegram-bot"),
]


def _clean_corrupted_installs():
    """Remove ~-prefixed corrupted distributions left by interrupted pip installs."""
    import site
    cleaned = []
    for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
        p = Path(site_dir)
        if not p.exists():
            continue
        for item in p.iterdir():
            name = item.name
            if name.startswith("~") and "inetunnel" in name.lower():
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    cleaned.append(str(item))
                except Exception:
                    pass
    return cleaned


def _check_dependencies():
    """Check if all required packages are importable. Returns list of missing."""
    missing = []
    for import_name, display_name in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(display_name)
    return missing


def _post_install_check():
    """Run on every CLI invocation: clean corrupted installs, check deps."""
    cleaned = _clean_corrupted_installs()
    if cleaned:
        print("[pinetunnel] Cleaned up corrupted install from previous update:")
        for c in cleaned:
            print(f"  - {c}")
        print()

    missing = _check_dependencies()
    if missing:
        print("[pinetunnel] Missing dependencies detected:")
        for m in missing:
            print(f"  - {m}")
        print()
        print("This can happen after a failed or interrupted update.")
        print("Fix it with:")
        print()
        print("  pip install --force-reinstall pinetunnel")
        print()
        return False
    return True

_MIN_ENV_TEMPLATE = """\
HOST=127.0.0.1
PORT=8000
APP_ENV=production
WEBHOOK_SECRET={webhook_secret}
JWT_SECRET={jwt_secret}
ADMIN_API_KEY={admin_api_key}
SESSION_SECRET={session_secret}
SIGNAL_ENCRYPTION_KEY={encryption_key}
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=
SERVER_BASE_URL=http://127.0.0.1:8000
DATABASE_URL=sqlite:///pinetunnel.db
"""

CF_CERT_DIR = Path.home() / ".cloudflared"
CF_CERT_FILE = CF_CERT_DIR / "cert.pem"


def _find_env_path() -> Path:
    p = Path.cwd()
    while p != p.parent:
        if (p / ".env").exists() or (p / "pyproject.toml").exists():
            return p / ".env"
        p = p.parent
    return Path.cwd() / ".env"


def _ensure_minimal_env() -> Path:
    env_path = _find_env_path()
    if env_path.exists():
        return env_path
    content = _MIN_ENV_TEMPLATE.format(
        webhook_secret=generate_secret(32),
        jwt_secret=generate_secret(48),
        admin_api_key=generate_secret(48),
        session_secret=generate_secret(32),
        encryption_key=secrets.token_hex(32),
    )
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(content)
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return env_path


def _project_root() -> Path:
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def _ensure_cloudflared() -> bool:
    if shutil.which("cloudflared"):
        return True
    system = platform.system().lower()
    print("\n[setup] cloudflared not found. Installing...")
    if system == "darwin":
        subprocess.run(["brew", "install", "cloudflared"], capture_output=True)
    elif system == "windows":
        subprocess.run(["winget", "install", "--id", "Cloudflare.cloudflared", "--accept-source-agreements", "--accept-package-agreements"], capture_output=True)
    else:
        subprocess.run(["bash", "-c", "curl -sL https://pkg.cloudflare.com/cloudflared-install.sh | sudo bash"], capture_output=True)
    if shutil.which("cloudflared"):
        print("[setup] cloudflared installed successfully.")
        return True
    print("[setup] WARNING: cloudflared could not be installed automatically.")
    print("[setup] Install manually from https://pkg.cloudflare.com")
    return False


def _run_migrations() -> int:
    root = _project_root()
    import importlib.util
    alembic_ini = root / "alembic.ini"
    if not alembic_ini.exists():
        spec = importlib.util.find_spec("migrations")
        if spec and spec.origin:
            alembic_ini = Path(spec.origin).parent / "alembic.ini"
    result = subprocess.run(
        [sys.executable, "-c",
         "import os,sys; sys.path=[p for p in sys.path if p not in ('','.',os.getcwd())]; "
         "from alembic.config import Config; from alembic import command; "
         f"cfg=Config('{alembic_ini}'); "
         "cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL','sqlite:///pinetunnel.db')); "
         "command.upgrade(cfg, 'head')"],
        cwd=str(root), capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"[pinetunnel] Migration warning: {result.stderr[:200]}")
    return result.returncode


def _print_banner():
    print()
    print("=" * 60)
    print("  PineTunnel Setup Wizard")
    print("  TradingView to MetaTrader Bridge")
    print("=" * 60)
    print()


def _step1_telegram(env_path: Path) -> bool:
    print("Step 1: Connect your Telegram bot")
    print("-" * 40)
    print("Used for login, trade alerts, and admin commands.")
    print("Create a bot at t.me/BotFather, then paste the token here.")
    print()

    env = read_env(env_path)
    current_token = env.get("TELEGRAM_BOT_TOKEN", "")

    while True:
        token = input("Bot token" + (f" [{current_token[:8]}...]" if current_token else "") + ": ").strip()
        if not token and current_token:
            token = current_token
        if not token:
            print("  ! Token is required. Get one from @BotFather on Telegram.")
            continue
        if not re.match(r"^\d+:[A-Za-z0-9_-]+$", token):
            print("  ! Invalid format. Token looks like 123456:ABC-DEF...")
            continue

        print("  Validating token...", end=" ", flush=True)
        try:
            import httpx
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = r.json()
            if data.get("ok"):
                bot_info = data.get("result", {})
                username = bot_info.get("username", "?")
                print(f"OK! Bot: @{username}")
                break
            else:
                desc = data.get("description", "Unknown error")
                print(f"FAILED: {desc}")
                print("  ! Check your token and try again.")
        except Exception as e:
            print(f"ERROR: {e}")
            print("  ! Could not reach Telegram. Check your internet.")
        continue

    print()
    admin_ids = input("Admin Telegram IDs (comma-separated, optional): ").strip()
    if not admin_ids:
        admin_ids = env.get("TELEGRAM_ADMIN_IDS", "")

    write_env_updates(env_path, {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ADMIN_IDS": admin_ids,
    })
    print("  -> Saved to .env")
    print()
    return True


def _step2_cloudflare(env_path: Path) -> bool:
    print("Step 2: Link your domain")
    print("-" * 40)
    print("Gives you a public URL for TradingView to send signals to.")
    print()

    env = read_env(env_path)
    current_url = env.get("SERVER_BASE_URL", "")
    if current_url.startswith("https://"):
        print(f"  Already configured: {current_url}")
        change = input("  Change it? (y/N): ").strip().lower()
        if change != "y":
            print()
            return True

    print("  Options:")
    print("    1. I have a Cloudflare domain (browser login)")
    print("    2. I have a tunnel token from Cloudflare dashboard")
    print("    3. Skip (configure later with 'pinetunnel setup')")
    print()
    choice = input("  Choose (1-3): ").strip()

    if choice == "3" or not choice:
        print("  Skipping domain setup.")
        print()
        return False

    if choice == "2":
        return _step2_cloudflare_token(env_path)

    has_cf = _ensure_cloudflared()
    if not has_cf:
        print("  cloudflared not available. Use option 2 (tunnel token) instead.")
        return _step2_cloudflare_token(env_path)

    if CF_CERT_FILE.exists():
        print("  Already logged in to Cloudflare.")
    else:
        print("  Starting Cloudflare login...")
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "login"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        login_url = None
        import time as _time
        _time.sleep(3)
        stdout = proc.stdout
        login_url = None
        if stdout is not None:
            while True:
                line = stdout.readline()
                if not line:
                    break
                line = line.strip()
                if "https://" in line and "dash.cloudflare.com" in line:
                    login_url = line
                    break
        if login_url:
            print()
            print("  Open this URL in your browser to log in and select your domain:")
            print()
            print(f"  {login_url}")
            print()
            print("  After selecting your domain in Cloudflare, come back here.")
            input("  Press Enter when done...")
            proc.wait(timeout=120)
        else:
            print("  Waiting for login... (a browser may have opened)")
            input("  Press Enter after you complete the login in your browser...")
            proc.wait(timeout=120)
        if not CF_CERT_FILE.exists():
            print("  ! Login not completed.")
            print("  You can use option 2 (tunnel token) instead.")
            return _step2_cloudflare_token(env_path)

    print()
    print("  Fetching available domains...")
    zones = _list_cf_zones()
    if not zones:
        print("  ! No domains found in your Cloudflare account.")
        return _step2_cloudflare_token(env_path)

    print()
    for i, z in enumerate(zones, 1):
        print(f"    {i}. {z}")
    print()

    while True:
        sel = input(f"Select domain (1-{len(zones)}): ").strip()
        try:
            idx = int(sel) - 1
            if 0 <= idx < len(zones):
                zone_name = zones[idx]
                break
        except ValueError:
            pass
        print(f"  ! Enter a number 1-{len(zones)}.")

    print()
    subdomain = input("Subdomain (e.g. signals): ").strip().lower()
    if not subdomain:
        subdomain = "signals"

    full_hostname = f"{subdomain}.{zone_name}"
    print()
    print(f"  Creating tunnel for {full_hostname}...")

    tunnel_id = _create_tunnel(full_hostname)
    if tunnel_id:
        write_env_updates(env_path, {
            "CLOUDFLARE_TUNNEL_ID": tunnel_id,
            "SERVER_BASE_URL": f"https://{full_hostname}",
        })
        print(f"  -> Tunnel created! URL: https://{full_hostname}")
        print(f"  -> Saved to .env")
        print()
        return True
    else:
        print("  ! Failed to create tunnel.")
        return _step2_cloudflare_token(env_path)


def _step2_cloudflare_token(env_path: Path) -> bool:
    print()
    print("  Paste your Cloudflare tunnel token.")
    print("  Get it from: dash.cloudflare.com > Zero Trust > Tunnels > Create")
    print()
    token = input("  Tunnel token: ").strip()
    if not token:
        print("  ! No token provided. Skipping.")
        print()
        return False

    url = input("  Full URL (e.g. https://signals.example.com): ").strip()
    if not url.startswith("https://"):
        print("  ! URL must start with https://")
        url = ""

    write_env_updates(env_path, {
        "CLOUDFLARE_TUNNEL_TOKEN": token,
        "SERVER_BASE_URL": url,
    })
    print(f"  -> Saved to .env")
    if url:
        print(f"  -> Webhook URL: {url.rstrip('/')}/webhook")
    print()
    return True


def _list_cf_zones() -> list:
    """Extract API token from cert.pem and list zones via Cloudflare API."""
    if not CF_CERT_FILE.exists():
        return []
    try:
        content = CF_CERT_FILE.read_text()
        import json as _json
        import re as _re
        for match in _re.finditer(r"-----BEGIN ([^-]+)-----\n(.*?)\n-----END", content, _re.DOTALL):
            block_type = match.group(1).strip()
            b64_content = match.group(2).strip()
            if "CERTIFICATE" in block_type or "PRIVATE KEY" in block_type:
                continue
            try:
                import base64 as _b64
                decoded = _b64.b64decode(b64_content).decode("utf-8", errors="replace")
                data = _json.loads(decoded)
                api_token = data.get("apiToken") or data.get("APIToken") or ""
                zone_id = data.get("zoneID") or data.get("ZoneID") or ""
                if api_token:
                    return _fetch_zones_from_api(api_token, zone_id)
            except Exception:
                continue
        json_part = content.split("-----BEGIN")[0].strip()
        if json_part:
            data = _json.loads(json_part)
            api_token = data.get("apiToken") or data.get("APIToken") or ""
            zone_id = data.get("zoneID") or data.get("ZoneID") or ""
            if api_token:
                return _fetch_zones_from_api(api_token, zone_id)
    except Exception:
        pass
    return []


def _fetch_zones_from_api(api_token: str, zone_id: str) -> list:
    """Use Cloudflare API token to list all zones, or resolve single zone."""
    if not api_token:
        return []
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_token}"}
        r = httpx.get("https://api.cloudflare.com/client/v4/zones?per_page=50", headers=headers, timeout=10)
        data = r.json()
        if data.get("success"):
            return [z.get("name", "") for z in data.get("result", []) if z.get("name")]
    except Exception:
        pass
    if zone_id:
        try:
            import httpx
            headers = {"Authorization": f"Bearer {api_token}"}
            r = httpx.get(f"https://api.cloudflare.com/client/v4/zones/{zone_id}", headers=headers, timeout=10)
            data = r.json()
            if data.get("success"):
                name = data.get("result", {}).get("name", "")
                if name:
                    return [name]
        except Exception:
            pass
    return []


def _create_tunnel(hostname: str) -> str:
    CF_CERT_DIR.mkdir(exist_ok=True)
    tunnel_name = "pinetunnel"
    tunnel_id = None

    print(f"  -> Checking for existing tunnel '{tunnel_name}'...")
    proc_list = subprocess.run(
        ["cloudflared", "tunnel", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if proc_list.returncode == 0:
        for line in proc_list.stdout.splitlines():
            if tunnel_name in line.lower():
                m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", line)
                if m:
                    tunnel_id = m.group(1)
                    print(f"  -> Found existing tunnel: {tunnel_id}")
                    break

    if not tunnel_id:
        print(f"  -> Creating tunnel '{tunnel_name}'...")
        proc = subprocess.run(
            ["cloudflared", "tunnel", "create", tunnel_name],
            capture_output=True, text=True, timeout=30,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            print(f"  ! Failed to create tunnel (exit {proc.returncode}):")
            for line in output.strip().splitlines():
                print(f"     {line}")
            if "already exists" in output.lower():
                m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", output)
                if m:
                    tunnel_id = m.group(1)
                    print(f"  -> Using existing tunnel ID from error: {tunnel_id}")
                else:
                    print(f"  -> Try deleting the tunnel in Cloudflare dashboard or use a different name.")
                    return ""
            else:
                return ""
        else:
            for line in output.splitlines():
                line = line.strip()
                if "Created tunnel" in line and "with id" in line:
                    m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", line)
                    if m:
                        tunnel_id = m.group(1)
                        break
                if "credentials written to" in line.lower():
                    m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", line)
                    if m:
                        tunnel_id = m.group(1)
                        break
            if not tunnel_id:
                m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", output)
                if m:
                    tunnel_id = m.group(1)
            if not tunnel_id:
                print(f"  ! Could not find tunnel ID in output:")
                for line in output.strip().splitlines():
                    print(f"     {line}")
                return ""
            print(f"  -> Tunnel created: {tunnel_id}")

    cred_file = CF_CERT_DIR / f"{tunnel_id}.json"
    if not cred_file.exists():
        print(f"  ! Credentials file not found at {cred_file}")
        print(f"  -> Check ~/.cloudflared/ for {tunnel_id}.json")
        return ""

    print(f"  -> Routing DNS for {hostname}...")
    proc2 = subprocess.run(
        ["cloudflared", "tunnel", "route", "dns", tunnel_id, hostname],
        capture_output=True, text=True, timeout=30,
    )
    if proc2.returncode != 0:
        err = (proc2.stderr + proc2.stdout).strip()
        if "already exists" in err.lower() or "record with that host" in err.lower():
            print(f"  -> DNS record already exists (OK)")
        else:
            print(f"  ! DNS routing failed:")
            for line in err.splitlines():
                print(f"     {line}")
            return ""

    cred_path_str = str(cred_file).replace("\\", "/")
    config_path = CF_CERT_DIR / "config.yml"
    config_path.write_text(
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {cred_path_str}\n"
        f"\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://localhost:8000\n"
        f"  - service: http_status:404\n"
    )
    print(f"  -> Config written to {config_path}")
    return tunnel_id


def _step3_webhook(env_path: Path) -> None:
    print("Step 3: Your webhook URL")
    print("-" * 40)
    env = read_env(env_path)
    base_url = env.get("SERVER_BASE_URL", "http://127.0.0.1:8000")
    webhook_url = base_url.rstrip("/") + "/webhook"
    print()
    print(f"  Your webhook URL:")
    print(f"  {webhook_url}")
    print()
    print("  Paste this into TradingView alert settings.")
    print()
    print("  Setup complete!")
    print()


def _run_setup_wizard(env_path: Path) -> None:
    _print_banner()
    env = read_env(env_path)
    tg = bool(env.get("TELEGRAM_BOT_TOKEN"))
    cf = env.get("SERVER_BASE_URL", "").startswith("https://")

    if not tg:
        _step1_telegram(env_path)
    else:
        print("Step 1: Telegram bot - already configured")
        print()
        skip = input("  Reconfigure? (y/N): ").strip().lower()
        if skip == "y":
            _step1_telegram(env_path)

    if not cf:
        _step2_cloudflare(env_path)
    else:
        print("Step 2: Domain - already configured")
        skip = input("  Reconfigure? (y/N): ").strip().lower()
        if skip == "y":
            _step2_cloudflare(env_path)

    _step3_webhook(env_path)


def _open_browser_after(delay: float, port: int) -> None:
    import threading
    import time

    def _open() -> None:
        time.sleep(delay)
        webbrowser.open(f"http://127.0.0.1:{port}/", new=2)

    threading.Thread(target=_open, daemon=True).start()


def cmd_start(args: argparse.Namespace) -> int:
    env_path = _ensure_minimal_env()
    _run_migrations()
    env = read_env(env_path)
    tg = bool(env.get("TELEGRAM_BOT_TOKEN"))
    cf = env.get("SERVER_BASE_URL", "").startswith("https://")

    if not tg or not cf:
        if not getattr(args, "skip_setup", False):
            _run_setup_wizard(env_path)

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if args.foreground:
        root = _project_root()
        os.chdir(root)
        cmd = [sys.executable, "-m", "uvicorn", "apps.server.main:app",
               "--host", host, "--port", str(port)]
        if not args.no_open_browser:
            _open_browser_after(2, port)
        return subprocess.call(cmd)
    if not args.no_open_browser:
        _open_browser_after(3, port)
    return start_daemon(host, port, 1)


def cmd_stop(args: argparse.Namespace) -> int:
    return stop_daemon()


def cmd_status(args: argparse.Namespace) -> int:
    pid = is_running()
    if pid:
        print(f"PineTunnel is running (PID {pid})")
        return 0
    print("PineTunnel is not running")
    return 1


def cmd_version(args: argparse.Namespace) -> int:
    print(f"PineTunnel v{__version__}")
    print(f"Python: {platform.python_version()}")
    print(f"OS: {platform.system()} {platform.release()}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    env_path = _ensure_minimal_env()
    _run_setup_wizard(env_path)
    return 0


_KNOWN_COMMANDS = {"start", "stop", "status", "version", "setup"}


def main() -> int:
    if not _post_install_check():
        return 1
    first = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if first is not None and first not in _KNOWN_COMMANDS:
        print(f"Unknown command '{first}'. Run 'pinetunnel --help' for options.")
        return 1
    parser = argparse.ArgumentParser(
        prog="pinetunnel",
        description="TradingView to MetaTrader bridge. Run 'pinetunnel' to start + setup.",
    )
    parser.add_argument("--version", action="version", version=f"PineTunnel v{__version__}")
    sub = parser.add_subparsers(dest="command")
    p_start = sub.add_parser("start", help="Start the server")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground (debug)")
    p_start.add_argument("--no-open-browser", action="store_true", help="Do not open browser")
    p_start.add_argument("--skip-setup", action="store_true", help="Skip setup wizard")
    p_start.set_defaults(func=cmd_start)
    p_stop = sub.add_parser("stop", help="Stop the daemon")
    p_stop.set_defaults(func=cmd_stop)
    p_status = sub.add_parser("status", help="Check daemon status")
    p_status.set_defaults(func=cmd_status)
    p_ver = sub.add_parser("version", help="Show version")
    p_ver.set_defaults(func=cmd_version)
    p_setup = sub.add_parser("setup", help="Re-run setup wizard")
    p_setup.set_defaults(func=cmd_setup)
    args = parser.parse_args()
    if not args.command:
        args.foreground = False
        args.no_open_browser = False
        args.skip_setup = False
        return cmd_start(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

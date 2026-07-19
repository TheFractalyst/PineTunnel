"""PineTunnel CLI - slim launcher. Full management is in the web dashboard.

Commands:
  pinetunnel             Start daemon + open dashboard (first run: setup)
  pinetunnel start       Start the server (--foreground for logs, --no-open-browser)
  pinetunnel stop        Stop the daemon
  pinetunnel status      Check if daemon is running
  pinetunnel version     Show version info
"""

import argparse
import os
import platform
import secrets
import subprocess
import sys
import webbrowser
from pathlib import Path

from apps.cli import __version__
from apps.lib.env_manager import generate_secret
from apps.lib.service import is_running, start_daemon, stop_daemon

_DEPRECATED_MSG = (
    "This command has moved to the web dashboard. "
    "Run 'pinetunnel' to open it."
)

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


def _ensure_cloudflared() -> None:
    """Check if cloudflared is installed, print instructions if not."""
    import shutil
    if shutil.which("cloudflared"):
        return
    system = platform.system().lower()
    if system == "darwin":
        print("[pinetunnel] cloudflared not found. Installing via Homebrew...")
        subprocess.run(["brew", "install", "cloudflared"], capture_output=True)
    elif system == "windows":
        print("[pinetunnel] cloudflared not found. Installing via winget...")
        subprocess.run(["winget", "install", "--id", "Cloudflare.cloudflared", "--accept-source-agreements", "--accept-package-agreements"], capture_output=True)
    else:
        print("[pinetunnel] cloudflared not found. Installing...")
        subprocess.run(["bash", "-c", "curl -sL https://pkg.cloudflare.com/cloudflared-install.sh | sudo bash"], capture_output=True)
    if not shutil.which("cloudflared"):
        print("[pinetunnel] WARNING: cloudflared could not be installed automatically.")
        print("[pinetunnel] Please install it manually from https://pkg.cloudflare.com")
        print("[pinetunnel] You can still use the dashboard without it, but Cloudflare tunnel setup will be unavailable.")


def _run_migrations() -> int:
    root = _project_root()
    result = subprocess.run(
        [sys.executable, "-c",
         "import os,sys; sys.path=[p for p in sys.path if p not in ('','.',os.getcwd())]; "
         "from alembic.config import Config; from alembic import command; "
         "cfg=Config('alembic.ini'); "
         "cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL','sqlite:///pinetunnel.db')); "
         "command.upgrade(cfg, 'head')"],
        cwd=str(root), capture_output=True, text=True, timeout=30,
    )
    return result.returncode


def _open_browser_after(delay: float, port: int) -> None:
    import threading
    import time

    def _open() -> None:
        time.sleep(delay)
        webbrowser.open(f"http://127.0.0.1:{port}/admin/", new=2)

    threading.Thread(target=_open, daemon=True).start()


def cmd_start(args: argparse.Namespace) -> int:
    _ensure_minimal_env()
    _ensure_cloudflared()
    _run_migrations()
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


_KNOWN_COMMANDS = {"start", "stop", "status", "version"}


def main() -> int:
    first = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if first is not None and first not in _KNOWN_COMMANDS:
        print(_DEPRECATED_MSG)
        return 0
    parser = argparse.ArgumentParser(
        prog="pinetunnel",
        description="TradingView to MetaTrader bridge. Run 'pinetunnel' to start + open dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"PineTunnel v{__version__}")
    sub = parser.add_subparsers(dest="command")
    p_start = sub.add_parser("start", help="Start the server")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground (debug)")
    p_start.add_argument("--no-open-browser", action="store_true", help="Do not open browser")
    p_start.add_argument("--daemon", action="store_true", help="Start as daemon (default)")
    p_start.set_defaults(func=cmd_start)
    p_stop = sub.add_parser("stop", help="Stop the daemon")
    p_stop.set_defaults(func=cmd_stop)
    p_status = sub.add_parser("status", help="Check daemon status")
    p_status.set_defaults(func=cmd_status)
    p_ver = sub.add_parser("version", help="Show version")
    p_ver.set_defaults(func=cmd_version)
    args = parser.parse_args()
    if not args.command:
        args.foreground = False
        args.no_open_browser = False
        args.daemon = True
        return cmd_start(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

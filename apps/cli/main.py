"""PineTunnel CLI - Interactive setup wizard and server management.

Commands:
  pinetunnel setup     One-command VPS setup (check, configure, start, verify)
  pinetunnel init      Interactive setup wizard (generates .env, runs migrations)
  pinetunnel start     Start the FastAPI server
  pinetunnel check     Run health checks (deps, Redis, database, server)
  pinetunnel migrate   Run Alembic database migrations
  pinetunnel test      Send a test webhook signal
  pinetunnel guide     Print the post-setup guide
  pinetunnel version   Show version info
"""

import argparse
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from apps.cli import __version__

# ---------------------------------------------------------------------------
# Color system - cross-platform ANSI with graceful degradation
# ---------------------------------------------------------------------------

_COLOR_ENABLED: bool | None = None


def _color_supported() -> bool:
    """Detect if the terminal supports ANSI color codes."""
    global _COLOR_ENABLED
    if _COLOR_ENABLED is not None:
        return _COLOR_ENABLED

    # Respect NO_COLOR convention (https://no-color.org/)
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return False

    # Must be a TTY
    if not sys.stdout.isatty():
        _COLOR_ENABLED = False
        return False

    # On Windows 10+, enable virtual terminal processing
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            _COLOR_ENABLED = True
            return True
        except Exception:
            _COLOR_ENABLED = False
            return False

    _COLOR_ENABLED = True
    return True


# ANSI escape sequences
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _c(text: str, color: str) -> str:
    """Wrap text in ANSI color codes if color is supported."""
    if _color_supported():
        return f"{color}{text}{_RESET}"
    return text


def _ok_label() -> str:
    return _c("OK", _GREEN)


def _warn_label() -> str:
    return _c("WARN", _YELLOW)


def _fail_label() -> str:
    return _c("FAIL", _RED)


def _skip_label() -> str:
    return _c("SKIP", _CYAN)


def print_ok(msg: str) -> None:
    print(f"  [{_ok_label()}]   {msg}")


def print_warn(msg: str) -> None:
    print(f"  [{_warn_label()}] {msg}")


def print_fail(msg: str) -> None:
    print(f"  [{_fail_label()}] {msg}")


def print_skip(msg: str) -> None:
    print(f"  [{_skip_label()}] {msg}")


# ---------------------------------------------------------------------------
# Progress spinner
# ---------------------------------------------------------------------------


class Spinner:
    """Simple terminal spinner for long-running operations."""

    _FRAMES = "|/-\\"

    def __init__(self, msg: str):
        self.msg = msg
        self._stop = False
        self._thread: threading.Thread | None = None
        self._is_tty = sys.stdout.isatty()

    def start(self) -> "Spinner":
        if self._is_tty:
            sys.stdout.write(f"  {self.msg}... ")
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self) -> None:
        i = 0
        while not self._stop:
            sys.stdout.write(
                f"\r  {self.msg}... {self._FRAMES[i % len(self._FRAMES)]}"
            )
            sys.stdout.flush()
            time.sleep(0.1)
            i += 1

    def stop(self, success: bool = True) -> None:
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)
        if self._is_tty:
            # Overwrite spinner line with spaces, then carriage return
            clear_len = len(self.msg) + 10
            sys.stdout.write(f"\r{' ' * clear_len}\r")
            sys.stdout.flush()
        if success:
            print_ok(self.msg)
        else:
            print_fail(self.msg)

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop(exc_type is None)
        return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BANNER = r"""
 ____  _       _   _                ___
 |  _ \(_) __ _| |_| |_ _   _       / _ \__ _ _ __ __ _ ___
 | |_) | |/ _` | __| __| | | |    / /_)/ _` | '__/ _` / _ \
 |  __/| | (_| | |_| |_| |_| |   / ___/ (_| | | | (_| |  __/
 |_|   |_|\__,_|\__|\__|\__, |   \/    \__,_|_|  \__, |\___|
                        |___/                    |___/
"""

ENV_TEMPLATE = """# PineTunnel configuration - generated by `pinetunnel init`
# Generated on {timestamp}

# Server
HOST=127.0.0.1
PORT=8000
APP_ENV=production
DEBUG=false
SERVER_BASE_URL={server_url}
SERVER_CORS_ORIGINS={server_url}
SERVER_WORKERS=1
SERVER_RELOAD=false
TRUSTED_PROXY_COUNT=1

# REQUIRED secrets (auto-generated)
WEBHOOK_SECRET={webhook_secret}
JWT_SECRET={jwt_secret}
ADMIN_API_KEY={admin_api_key}

# Database (SQLite by default, change to PostgreSQL for production)
DATABASE_URL=sqlite:///pinetunnel.db

# Redis
REDIS_URL=redis://localhost:6379

# Data and Logging
DATA_DIR=./data
LOG_LOG_DIR=./data/logs
LOG_LEVEL=INFO
LOG_FORMAT=json

# Telegram bot (required - get token from @BotFather)
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=
TELEGRAM_BOT_URL=

# Signal Encryption (optional - generated by setup if enabled)
SIGNAL_ENCRYPTION_KEY={encryption_key}
SIGNAL_ENCRYPTION_KEY_PREVIOUS=

# Security
SECURITY_EMAIL=security@your-server.com
SECURITY_URL={server_url}/.well-known/security.txt

# TradingView IP Allowlist (optional)
TRADINGVIEW_IP_ALLOWLIST=false
TRADINGVIEW_IPS=
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_sudo_warned = False


def _sudo_prefix() -> list[str]:
    """Return sudo prefix list for subprocess commands.

    Returns ["sudo"] on Linux/macOS, [] on Windows.
    Warns once if sudo is missing on a non-Windows system.
    """
    global _sudo_warned
    if platform.system() == "Windows":
        return []
    if not _can_sudo() and not _sudo_warned:
        print_warn("sudo is not available. Commands may fail without root privileges.")
        _sudo_warned = True
    return ["sudo"]


def _can_sudo() -> bool:
    """Check if sudo is available on this system."""
    return shutil.which("sudo") is not None


def _is_windows_admin() -> bool:
    """Check if running with administrator privileges on Windows."""
    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _ensure_python_311(yes: bool = False) -> bool:
    """Check if Python >= 3.11 is available, offer to install if not.

    Also checks for pip on fresh VPS installs where pip may be missing.

    Returns True if Python 3.11+ is available in the current process.
    If yes=True, auto-accepts the install prompt.
    """
    v = sys.version_info
    if v < (3, 11):
        print_warn(
            f"Python {v.major}.{v.minor}.{v.micro} detected. "
            f"PineTunnel requires Python 3.13+."
        )

        # Detect OS and suggest/install Python 3.13
        system = platform.system()
        can_auto = False
        install_cmd: str | None = None
        install_label = ""

        if system == "Darwin":
            can_auto = shutil.which("brew") is not None
            install_cmd = "brew install python@3.13"
            install_label = "Homebrew: brew install python@3.13"
        elif system == "Linux":
            if shutil.which("apt-get") or shutil.which("apt"):
                can_auto = True
                _sudo_str = "sudo " if _can_sudo() else ""
                install_cmd = (
                    f"{_sudo_str}add-apt-repository -y ppa:deadsnakes/ppa && "
                    f"{_sudo_str}apt-get update && "
                    f"{_sudo_str}apt-get install -y "
                    "python3.13 python3.13-venv python3.13-dev"
                )
                install_label = (
                    "apt (deadsnakes PPA):\n"
                    "    sudo add-apt-repository -y ppa:deadsnakes/ppa\n"
                    "    sudo apt update\n"
                    "    sudo apt install python3.13 python3.13-venv python3.13-dev"
                )
            elif shutil.which("dnf") or shutil.which("yum"):
                can_auto = False
                install_label = (
                    "Fedora/RHEL: Python 3.13 is not in default repos.\n"
                    "    Option A - pyenv (recommended):\n"
                    "      curl https://pyenv.run | bash && pyenv install 3.13\n"
                    "    Option B - Build from source:\n"
                    "      wget https://www.python.org/ftp/python/3.13.1/Python-3.13.1.tgz\n"
                    "      tar xzf Python-3.13.1.tgz\n"
                    "      cd Python-3.13.1\n"
                    "      ./configure --enable-optimizations\n"
                    "      make -j$(nproc)\n"
                    "      sudo make install"
                )
            else:
                can_auto = False
                install_label = (
                    "pyenv (generic Linux):\n"
                    "    curl https://pyenv.run | bash && pyenv install 3.13"
                )
        elif system == "Windows":
            can_auto = shutil.which("winget") is not None
            install_cmd = "winget install Python.Python.3.13"
            install_label = (
                "winget: winget install Python.Python.3.13\n"
                "    Or download from https://www.python.org/downloads/"
            )
        else:
            can_auto = False
            install_label = (
                "pyenv:\n"
                "    curl https://pyenv.run | bash && pyenv install 3.13"
            )

        print("  Install Python 3.13+:")
        print(f"    {install_label}")
        print()

        if can_auto:
            if yes:
                answer = "y"
            else:
                try:
                    answer = input(
                        "  Attempt automatic install now? [y/N]: "
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    answer = "n"
                    print()

            if answer not in ("y", "yes"):
                print_fail(
                    "Python 3.11+ is required. "
                    "Please install it and re-run: pinetunnel init"
                )
                return False

            spinner = Spinner("Installing Python 3.13").start()
            try:
                if system == "Darwin":
                    result = subprocess.run(
                        ["brew", "install", "python@3.13"], timeout=300
                    )
                elif system == "Windows":
                    result = subprocess.run(
                        ["winget", "install", "Python.Python.3.13",
                         "--accept-source-agreements",
                         "--accept-package-agreements"],
                        timeout=300,
                    )
                else:
                    assert install_cmd is not None
                    result = subprocess.run(
                        install_cmd, shell=True, timeout=300
                    )

                if result.returncode == 0:
                    spinner.stop(success=True)
                    print_ok("Python 3.13 installed")
                    if system == "Windows":
                        print(
                            "  Please restart your terminal and re-run: pinetunnel init"
                        )
                    else:
                        print(
                            "  Please re-run using python3.13: "
                            "python3.13 -m pinetunnel init"
                        )
                else:
                    spinner.stop(success=False)
                    print_fail(
                        f"Installation failed (exit code {result.returncode})"
                    )
            except subprocess.TimeoutExpired:
                spinner.stop(success=False)
                print_fail("Installation timed out after 5 minutes")
            except OSError as e:
                spinner.stop(success=False)
                print_fail(f"Installation failed: {e}")
            return False
        else:
            print(
                "  Please install Python 3.13+ using the instructions above,"
            )
            print("  then re-run: pinetunnel init")
            return False

    # Python 3.11+ is available
    print_ok(f"Python {v.major}.{v.minor}.{v.micro}")

    # On a fresh VPS, pip might be missing even if Python is present
    _ensure_pip(system=platform.system())

    return True


def _ensure_pip(system: str | None = None) -> None:
    """Check if pip is available and offer to install it if missing.

    This handles fresh VPS installs where Python is present but pip is not.
    """
    # Quick check: can we import pip?
    try:
        __import__("pip")
        return
    except ImportError:
        pass

    # Also check for pip binary
    if shutil.which("pip") or shutil.which("pip3"):
        return

    print_warn("pip is not installed")
    system = system or platform.system()

    if system == "Linux":
        if shutil.which("apt-get") or shutil.which("apt"):
            try:
                answer = input(
                    "  Install pip now? [Y/n]: "
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "n"
            if answer != "n":
                subprocess.run(
                    _sudo_prefix() + ["apt-get", "install", "-y", "python3-pip"],
                    timeout=120,
                )
                print_ok("pip installed")
            else:
                print("  Install manually: sudo apt install python3-pip")
        elif shutil.which("dnf"):
            try:
                answer = input(
                    "  Install pip now? [Y/n]: "
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "n"
            if answer != "n":
                subprocess.run(
                    _sudo_prefix() + ["dnf", "install", "-y", "python3-pip"],
                    timeout=120,
                )
                print_ok("pip installed")
            else:
                print("  Install manually: sudo dnf install python3-pip")
        else:
            print("  Install: curl https://bootstrap.pypa.io/get-pip.py | python3")
    elif system == "Darwin":
        print("  Try: brew install python (includes pip)")
    else:
        print("  Install: curl https://bootstrap.pypa.io/get-pip.py | python3")


def _ensure_firewall(port: int, yes: bool = False) -> None:
    """Detect firewall and offer to open the server port.

    Supports ufw (Ubuntu), firewalld (CentOS/RHEL), iptables (generic),
    and Windows Firewall (netsh advfirewall).
    Never fails the setup - only warns.
    If yes=True, auto-accepts port-opening prompts.
    """
    system = platform.system()

    if system == "Windows":
        # Windows Firewall via netsh advfirewall
        if not _is_windows_admin():
            print_warn(
                "Windows Firewall configuration requires administrator privileges."
            )
            print("         Run this command as Administrator:")
            print(
                f'           netsh advfirewall firewall add rule name="PineTunnel"'
                f' dir=in action=allow protocol=TCP localport={port}'
            )
            return

        # Check if rule already exists
        try:
            result = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 "name=PineTunnel"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "PineTunnel" in (result.stdout or ""):
                print_ok(
                    f"Windows Firewall rule 'PineTunnel' exists"
                    f" (port {port}/tcp allowed)"
                )
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        print_warn(f"Windows Firewall rule for port {port}/tcp not found")
        if yes:
            answer = "y"
        else:
            try:
                answer = input(
                    f"  Add Windows Firewall rule for port {port}/tcp? [Y/n]: "
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "n"

        if answer != "n":
            try:
                result = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "add", "rule",
                     "name=PineTunnel", "dir=in", "action=allow",
                     "protocol=TCP", f"localport={port}"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    print_ok(f"Windows Firewall rule added for port {port}/tcp")
                else:
                    err = result.stderr.strip() or result.stdout.strip()
                    print_warn(f"Could not add firewall rule: {err}")
            except (subprocess.TimeoutExpired, OSError) as e:
                print_warn(f"Could not add firewall rule: {e}")
        else:
            print_warn(f"Port {port}/tcp may be blocked by Windows Firewall")
        return

    if system not in ("Linux",):
        # macOS firewalls are GUI-managed; just inform
        if system == "Darwin":
            print_ok(f"No CLI firewall check needed (macOS pf is GUI-managed). Port {port}/tcp should be open.")
        else:
            print_ok(f"Port {port}/tcp - verify firewall settings manually on this OS.")
        return

    # Check for ufw (Ubuntu/Debian)
    ufw_bin = shutil.which("ufw")
    if ufw_bin:
        try:
            result = subprocess.run(
                ["ufw", "status"], capture_output=True, text=True, timeout=5
            )
            output = (result.stdout or "").lower()
            if "status: active" in output:
                if f"{port}/tcp" in output or f"{port} " in output:
                    print_ok(f"ufw is active and port {port}/tcp is allowed")
                    return
                print_warn(f"ufw is active but port {port}/tcp is not open")
                if yes:
                    answer = "y"
                else:
                    try:
                        answer = input(
                            f"  Open port {port}/tcp in ufw? [Y/n]: "
                        ).strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        answer = "n"
                if answer != "n":
                    subprocess.run(
                        _sudo_prefix() + ["ufw", "allow", f"{port}/tcp"],
                        timeout=10,
                    )
                    print_ok(f"ufw rule added for {port}/tcp")
                else:
                    print_warn(f"Port {port}/tcp may be blocked by ufw")
                return
            else:
                print_ok("ufw is installed but inactive (no firewall blocking)")
                return
        except (subprocess.TimeoutExpired, OSError):
            print_warn("Could not check ufw status")
            return

    # Check for firewalld (CentOS/RHEL/Fedora)
    fw_cmd = shutil.which("firewall-cmd")
    if fw_cmd:
        try:
            result = subprocess.run(
                ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "running" in (result.stdout or "").lower():
                # Check if port is already open
                result = subprocess.run(
                    ["firewall-cmd", "--list-ports"], capture_output=True, text=True, timeout=5
                )
                ports_output = result.stdout or ""
                if f"{port}/tcp" in ports_output:
                    print_ok(f"firewalld is running and port {port}/tcp is open")
                    return
                print_warn(f"firewalld is running but port {port}/tcp is not open")
                if yes:
                    answer = "y"
                else:
                    try:
                        answer = input(
                            f"  Open port {port}/tcp in firewalld? [Y/n]: "
                        ).strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        answer = "n"
                if answer != "n":
                    subprocess.run(
                        _sudo_prefix() + ["firewall-cmd", "--permanent", "--add-port", f"{port}/tcp"],
                        timeout=10,
                    )
                    subprocess.run(
                        _sudo_prefix() + ["firewall-cmd", "--reload"],
                        timeout=10,
                    )
                    print_ok(f"firewalld rule added for {port}/tcp")
                else:
                    print_warn(f"Port {port}/tcp may be blocked by firewalld")
                return
            else:
                print_ok("firewalld is installed but not running (no firewall blocking)")
                return
        except (subprocess.TimeoutExpired, OSError):
            print_warn("Could not check firewalld status")
            return

    # Check for iptables (generic fallback)
    iptables_bin = shutil.which("iptables")
    if iptables_bin:
        print_warn(
            f"iptables detected. Port {port}/tcp may need manual opening.\n"
            f"         Example: sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT"
        )
        return

    # No firewall detected
    print_ok(f"No firewall detected - port {port}/tcp should be accessible")


def _check_system_requirements() -> None:
    """Check RAM, disk space, and root status. Prints a summary table.

    Warns only - never blocks setup.
    """
    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    try:
        import psutil
    except ImportError:
        print_warn("psutil not installed - skipping system requirement checks")
        return

    # RAM check
    try:
        mem = psutil.virtual_memory()
        ram_mb = mem.available // (1024 * 1024)
        if ram_mb < 256:
            results.append(("RAM", "WARN", f"{ram_mb} MB available (< 256 MB)"))
        else:
            results.append(("RAM", "OK", f"{ram_mb} MB available"))
    except Exception:
        results.append(("RAM", "SKIP", "could not read memory"))

    # Disk space check
    try:
        disk = psutil.disk_usage(".")
        disk_mb = disk.free // (1024 * 1024)
        if disk_mb < 100:
            results.append(("Disk", "WARN", f"{disk_mb} MB free (< 100 MB)"))
        else:
            results.append(("Disk", "OK", f"{disk_mb} MB free"))
    except Exception:
        results.append(("Disk", "SKIP", "could not read disk usage"))

    # Root check (POSIX only)
    if hasattr(os, "geteuid"):
        if os.geteuid() == 0:
            results.append(
                ("User", "WARN", "running as root (use non-root user)")
            )
        else:
            results.append(("User", "OK", "non-root user"))
    else:
        results.append(("User", "SKIP", "cannot check on this OS"))

    # Print summary table
    col_name = max(len("Check"), max(len(r[0]) for r in results))
    col_status = max(len("Status"), max(len(r[1]) for r in results))
    col_detail = max(len("Detail"), max(len(r[2]) for r in results))

    border = (
        "  +"
        + "-" * (col_name + 2)
        + "+"
        + "-" * (col_status + 2)
        + "+"
        + "-" * (col_detail + 2)
        + "+"
    )
    header = (
        f"  | {'Check':<{col_name}} | {'Status':<{col_status}}"
        f" | {'Detail':<{col_detail}} |"
    )

    print()
    print(border)
    print(header)
    print(border)

    for name, status, detail in results:
        if status == "OK":
            status_colored = _c(status, _GREEN)
        elif status == "WARN":
            status_colored = _c(status, _YELLOW)
        else:
            status_colored = _c(status, _CYAN)
        status_field = status_colored + " " * (col_status - len(status))
        row = (
            f"  | {name:<{col_name}} | {status_field}"
            f" | {detail:<{col_detail}} |"
        )
        print(row)

    print(border)
    print()


def _redis_install_hints() -> list[str]:
    """Return OS-specific Redis install suggestions (fallback display)."""
    system = platform.system()
    if system == "Darwin":
        return [
            "brew install redis && redis-server --daemonize yes",
        ]
    elif system == "Windows":
        return [
            "Download from https://github.com/microsoftarchive/redis/releases",
            "Or use WSL: wsl --install -d Ubuntu, then sudo apt install redis-server",
        ]
    else:
        return [
            "sudo apt install redis-server (Ubuntu/Debian)",
            "sudo yum install redis (CentOS/RHEL)",
            "sudo dnf install redis (Fedora)",
        ]


def _redis_binary_locations() -> list[Path]:
    """Return OS-specific candidate paths for redis-server binary."""
    system = platform.system()
    candidates: list[Path] = []
    if system == "Darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin/redis-server"),
                Path("/usr/local/bin/redis-server"),
            ]
        )
    elif system == "Windows":
        candidates.extend(
            [
                Path("C:/Program Files/Redis/redis-server.exe"),
                Path("C:/Redis/redis-server.exe"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/bin/redis-server"),
                Path("/usr/local/bin/redis-server"),
            ]
        )
    return candidates


def _is_redis_running(host: str = "localhost", port: int = 6379) -> bool:
    """Check if Redis is reachable via TCP socket connection."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, port))
        s.close()
        return True
    except (socket.error, ConnectionRefusedError, OSError):
        return False


def _find_redis_server() -> str | None:
    """Find redis-server binary via PATH or known OS-specific locations."""
    found = shutil.which("redis-server")
    if found:
        return found
    for path in _redis_binary_locations():
        if path.exists():
            return str(path)
    return None


def _start_redis(binary: str, host: str = "localhost", port: int = 6379) -> bool:
    """Start redis-server as a daemon (POSIX) or in a new window (Windows)."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(
                [binary, "--port", str(port)],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        else:
            subprocess.run(
                [binary, "--daemonize", "yes", "--port", str(port)],
                capture_output=True,
                timeout=10,
            )
        time.sleep(2)
        return _is_redis_running(host, port)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _ensure_redis(yes: bool = False) -> bool:
    """Ensure Redis is running, auto-starting or auto-installing if needed.

    Returns True if Redis is running, False otherwise.
    Redis is optional for basic CLI commands, so this never blocks.
    If yes=True, auto-accepts all prompts (starts/installs Redis automatically).
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        parsed = urlparse(redis_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
    except Exception:
        host = "localhost"
        port = 6379

    # Step 1: Is Redis already running?
    if _is_redis_running(host, port):
        print_ok(f"Redis is running on {host}:{port}")
        return True

    # Step 2: Redis not running - check if binary exists
    redis_server = _find_redis_server()

    if redis_server:
        print_warn(f"Redis found at {redis_server} but not running on {host}:{port}")
        if yes:
            answer = "y"
        else:
            try:
                answer = input("  Start Redis now? [Y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "n"

        if answer != "n":
            spinner = Spinner(f"Starting Redis from {redis_server}").start()
            if _start_redis(redis_server, host, port):
                spinner.stop(success=True)
                print_ok(f"Redis started on {host}:{port}")
                return True
            else:
                spinner.stop(success=False)
                print_warn("Failed to start Redis automatically.")

        print_warn(
            "Redis is optional for basic CLI commands. Continuing without Redis."
        )
        return False

    # Step 3: Binary not found - offer to install based on OS
    print_warn(f"Redis is not installed and not running on {host}:{port}")

    system = platform.system()

    options: list[tuple[str, str]] = []

    if system == "Darwin":
        options.append(("brew", "Homebrew: brew install redis"))
    elif system == "Linux":
        if shutil.which("apt-get") or shutil.which("apt"):
            options.append(("apt", "apt: sudo apt install redis-server"))
        if shutil.which("yum"):
            options.append(("yum", "yum: sudo yum install redis"))
        if shutil.which("dnf"):
            options.append(("dnf", "dnf: sudo dnf install redis"))
    elif system == "Windows":
        options.append((
            "wsl",
            "WSL: wsl --install -d Ubuntu, then sudo apt install redis-server",
        ))
        options.append((
            "download",
            "Download from https://github.com/microsoftarchive/redis/releases",
        ))

    if not options:
        print("         Install options:")
        for hint in _redis_install_hints():
            print(f"           {hint}")
        print_warn(
            "Redis is optional for basic CLI commands. Continuing without Redis."
        )
        return False

    print("  How would you like to install Redis?")
    for i, (_key, desc) in enumerate(options, 1):
        print(f"    {i}. {desc}")
    print(f"    {len(options) + 1}. Skip (Redis is optional for basic commands)")

    if yes:
        choice = "1"
        print(f"  Auto-selecting: {options[0][1]}")
    else:
        try:
            choice = input(f"  Select option [1-{len(options) + 1}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            choice = str(len(options) + 1)

    try:
        choice_idx = int(choice) - 1
    except ValueError:
        choice_idx = len(options)

    if choice_idx < 0 or choice_idx >= len(options):
        print_skip("Redis installation (user skipped)")
        print_warn(
            "Redis is optional for basic CLI commands. Continuing without Redis."
        )
        return False

    selected_key = options[choice_idx][0]

    if selected_key == "brew":
        print("  Running: brew install redis")
        try:
            result = subprocess.run(["brew", "install", "redis"], timeout=120)
            if result.returncode == 0:
                redis_bin = _find_redis_server()
                if redis_bin:
                    spinner = Spinner("Starting Redis").start()
                    if _start_redis(redis_bin, host, port):
                        spinner.stop(success=True)
                        print_ok(f"Redis installed and started on {host}:{port}")
                        return True
                    else:
                        spinner.stop(success=False)
                        print_warn("Redis installed but failed to start automatically.")
                        print("         Try: brew services start redis")
                else:
                    print_warn("Redis installed but binary not found.")
                    print("         Try: brew services start redis")
            else:
                print_fail("brew install redis failed.")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print_fail(f"brew install failed: {e}")

    elif selected_key in ("apt", "yum", "dnf"):
        if selected_key == "apt":
            install_cmd = _sudo_prefix() + ["apt-get", "install", "-y", "redis-server"]
            service_name = "redis-server"
        elif selected_key == "yum":
            install_cmd = _sudo_prefix() + ["yum", "install", "-y", "redis"]
            service_name = "redis"
        else:
            install_cmd = _sudo_prefix() + ["dnf", "install", "-y", "redis"]
            service_name = "redis"

        print(f"  Running: {' '.join(install_cmd)}")
        try:
            result = subprocess.run(install_cmd, timeout=120)
            if result.returncode == 0:
                subprocess.run(
                    _sudo_prefix() + ["systemctl", "start", service_name],
                    capture_output=True,
                    timeout=10,
                )
                time.sleep(2)
                if _is_redis_running(host, port):
                    print_ok(f"Redis installed and started on {host}:{port}")
                    return True
                else:
                    redis_bin = _find_redis_server()
                    if redis_bin and _start_redis(redis_bin, host, port):
                        print_ok(f"Redis installed and started on {host}:{port}")
                        return True
                    else:
                        print_warn("Redis installed but failed to start automatically.")
                        print(f"         Try: sudo systemctl start {service_name}")
            else:
                print_fail(f"{selected_key} install failed.")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print_fail(f"Install failed: {e}")

    elif selected_key == "wsl":
        print("  Please install Redis via WSL:")
        print("    1. wsl --install -d Ubuntu")
        print("    2. In WSL: sudo apt install redis-server")
        print("    3. In WSL: redis-server --daemonize yes")

    elif selected_key == "download":
        print("  Download Redis for Windows from:")
        print("    https://github.com/microsoftarchive/redis/releases")

    print_warn(
        "Redis is optional for basic CLI commands. Continuing without Redis."
    )
    return False


_CORE_DEPS: dict[str, str] = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "redis": "redis",
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "psutil": "psutil",
}

_OPTIONAL_DEPS: list[tuple[str, str | None]] = [
    ("telegram", "Telegram bot features disabled (install with: pip install pinetunnel[telegram])"),
    ("psycopg", "PostgreSQL support disabled (install with: pip install pinetunnel[postgres])"),
    ("asyncpg", None),
]

_DEP_DISPLAY: dict[str, str] = {
    "fastapi": "FastAPI",
    "uvicorn": "uvicorn",
    "redis": "Redis",
    "sqlalchemy": "SQLAlchemy",
    "alembic": "Alembic",
    "pydantic": "Pydantic",
    "httpx": "httpx",
    "psutil": "psutil",
}


def _get_dep_version(module_name: str) -> str | None:
    """Try to get the version of an installed module."""
    try:
        import importlib.metadata

        return importlib.metadata.version(module_name)
    except Exception:
        try:
            mod = __import__(module_name)
            if hasattr(mod, "__version__"):
                return str(mod.__version__)
        except Exception:
            pass
    return None


def _ensure_dependencies(yes: bool = False) -> bool:
    """Check core dependencies and offer to install missing ones.

    Returns True if all core deps are available after checking/installing.
    If yes=True, auto-accepts installation prompts.
    """
    missing: list[tuple[str, str]] = []
    for module, pkg in _CORE_DEPS.items():
        try:
            __import__(module)
        except ImportError:
            missing.append((module, pkg))

    if missing:
        missing_pkgs = [pkg for _, pkg in missing]
        print_warn(f"Missing dependencies: {', '.join(missing_pkgs)}")

        if yes:
            response = "y"
        else:
            try:
                response = input("  Install missing dependencies now? [Y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                response = "n"
                print()

        if response in ("", "y", "yes"):
            print(f"  Installing: {', '.join(missing_pkgs)}...")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", *missing_pkgs],
                )
                if result.returncode == 0:
                    for module, pkg in missing:
                        try:
                            __import__(module)
                            version = _get_dep_version(module)
                            ver_str = f" v{version}" if version else ""
                            print_ok(f"{pkg}{ver_str} installed successfully")
                        except ImportError:
                            print_fail(f"{pkg} install verification failed (import error)")
                else:
                    print_fail(f"pip install exited with code {result.returncode}")
            except FileNotFoundError:
                print_fail("pip not found. Is it installed?")
            except Exception as e:
                print_fail(f"Could not run pip install: {e}")
        else:
            print(f"  Run manually: pip install {' '.join(missing_pkgs)}")

    # Verify all core deps are now available
    all_ok = True
    for module, _ in _CORE_DEPS.items():
        try:
            __import__(module)
        except ImportError:
            all_ok = False

    if all_ok:
        print_ok("All core dependencies available")

    # Check optional deps (warn only, no auto-install)
    for module, warn_msg in _OPTIONAL_DEPS:
        try:
            __import__(module)
        except ImportError:
            if warn_msg:
                print_warn(warn_msg)

    return all_ok


def _generate_secret(length: int = 32) -> str:
    """Generate a cryptographically secure random secret."""
    return secrets.token_urlsafe(length)


def _find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def _ensure_alembic_files(root: Path) -> Path | None:
    """Ensure alembic.ini and migration scripts are available.

    When pip-installed, alembic.ini is not in CWD. This function:
    1. If alembic.ini exists in root, returns root
    2. Otherwise, finds the installed migrations package, writes alembic.ini
       to root with absolute script_location, returns root

    Returns the directory containing alembic.ini, or None if unavailable.
    """
    ini_path = root / "alembic.ini"
    if ini_path.exists():
        return root

    # Find installed migrations package (contains our migration scripts)
    try:
        import importlib
        migrations_pkg = importlib.import_module("migrations")
        pkg_dir = Path(migrations_pkg.__file__).parent
        versions_dir = pkg_dir / "versions"
        if not versions_dir.exists() or not any(versions_dir.glob("*.py")):
            return None
    except Exception:
        return None

    # Write alembic.ini with absolute script_location
    ini_content = f"""# Alembic Configuration (auto-generated by PineTunnel)

[alembic]
script_location = {pkg_dir}
prepend_sys_path = .
version_path_separator = os

sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
"""
    try:
        ini_path.write_text(ini_content)
        return root
    except OSError:
        return None


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use on localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _find_free_port(start: int = 8000, max_tries: int = 100) -> int:
    """Find the first free port starting from `start`."""
    for port in range(start, start + max_tries):
        if not _is_port_in_use(port):
            return port
    return start


def _get_public_ip() -> str:
    """Detect the server's public IP address for webhook URL suggestions.

    Tries api.ipify.org with a 3-second timeout, falls back to
    socket.gethostname() if the request fails.
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request("https://api.ipify.org")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ip = resp.read().decode().strip()
            if ip:
                return ip
    except Exception:
        pass
    return socket.gethostname()


def _resolve_server_url() -> str:
    """Determine the best server URL for suggestions.

    Priority:
      1. SERVER_BASE_URL from env (production / cloud platform)
      2. http://<public-ip>:8000
    """
    env_url = os.environ.get("SERVER_BASE_URL", "")
    if env_url:
        return env_url.rstrip("/")
    public_ip = _get_public_ip()
    return f"http://{public_ip}:8000"


def _print_tradingview_setup(server_url: str) -> None:
    """Print detailed TradingView alert configuration guide."""
    webhook_url = server_url.rstrip("/") + "/"
    print()
    print("  --- TradingView Alert Configuration ---")
    print()
    print("  Webhook URL:")
    print(f"    {webhook_url}")
    print()
    print("  Alert Message Format (CSV):")
    print("    KEY,COMMAND,SYMBOL,PARAM=VALUE,PARAM=VALUE,...,secret=SECRET")
    print()
    print("  Example:")
    print("    YOUR_KEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=YOUR_SECRET")
    print()
    print("  Available Commands:")
    print()
    print("    Market Orders:")
    print("      buy              Open long position")
    print("      sell             Open short position")
    print()
    print("    Close Positions:")
    print("      close_long       Close all long positions")
    print("      close_short      Close all short positions")
    print("      close_all        Close all positions")
    print("      close_long_pct   Close X% of long (risk=50)")
    print("      close_short_pct  Close X% of short (risk=50)")
    print()
    print("    Pending Orders:")
    print("      buy_stop         Place buy stop order")
    print("      buy_limit        Place buy limit order")
    print("      sell_stop        Place sell stop order")
    print("      sell_limit       Place sell limit order")
    print()
    print("    See docs/TRADINGVIEW_ALERTS.md for the full command reference.")


def _print_setup_guide(server_url: str) -> None:
    """Print the post-setup guide with next steps 1-6."""
    print("  ========================================")
    print("  Setup Complete! Here's what to do next:")
    print("  ========================================")
    print()
    print("  1. Start the server:")
    print("     pinetunnel start              (foreground)")
    print("     pinetunnel start --daemon     (background)")
    print()
    print("  2. Verify it's running:")
    print("     pinetunnel status")
    print(f"     Or open: {server_url}/docs")
    print()
    print("  3. Send a test signal:")
    print("     pinetunnel test")
    print()
    print("  4. Download the EA for MetaTrader:")
    print(f"     MT5: {server_url}/api/ea/download/mt5")
    print(f"     MT4: {server_url}/api/ea/download/mt4")
    print("     (Requires X-License-Key header - use your ADMIN_API_KEY)")
    print()
    print("  5. Install the EA on MetaTrader:")
    print("     - Copy the .ex5/.ex4 file to MQL5/Experts or MQL4/Experts")
    print("     - Copy PTWebSocket.dll to MQL5/Libraries or MQL4/Libraries")
    print("     - Attach EA to chart, set InpLicenseID and InpServerURL")
    print("     - Enable DLL imports: Tools -> Options -> Expert Advisors")
    print()
    print("  6. Configure TradingView webhook:")
    print("     - Create an alert in TradingView")
    webhook_url = server_url.rstrip("/") + "/"
    print(f"     - Set webhook URL to: {webhook_url}")
    print("     - Set alert message to:")
    print("       YOUR_KEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=YOUR_SECRET")
    _print_tradingview_setup(server_url)
    print()
    print("  Full documentation: https://github.com/TheFractalyst/PineTunnel#documentation")


def _is_first_run() -> bool:
    """Check if this is the first time PineTunnel is run."""
    root = _find_project_root()
    return not (root / ".pinetunnel_initialized").exists()


def _server_is_healthy() -> bool:
    """Check if the PineTunnel server is running and responding."""
    port = int(os.environ.get("PORT", "8000"))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _mark_initialized() -> None:
    """Create the marker file indicating PineTunnel has been initialized."""
    root = _find_project_root()
    try:
        (root / ".pinetunnel_initialized").write_text(
            f"PineTunnel initialized on {datetime.now(timezone.utc).isoformat()}\n"
        )
    except OSError:
        pass


def _print_welcome() -> None:
    """Print a welcome banner for bare invocations."""
    print("""
===============================================
  PineTunnel - TradingView to MetaTrader Bridge

  Turn TradingView alerts into MT4/MT5 trades.

  Quick setup? Run: pinetunnel setup
  First time?  Run: pinetunnel init
  Need help?   Run: pinetunnel guide
  Check setup? Run: pinetunnel check
===============================================
""")


def _suggest_next_command(command: str) -> None:
    """Print a suggestion for what to do next after a command completes."""
    suggestions = {
        "init": "Next: pinetunnel setup (one-command) or pinetunnel start --daemon",
        "setup": "Next: pinetunnel setup-cloudflare (HTTPS) or pinetunnel install-service",
        "start": "To stop: pinetunnel stop",
        "migrate": "Next: pinetunnel start",
        "test": "Check server: pinetunnel status",
        "install-service": "Service installed. Start with: systemctl --user start pinetunnel",
        "setup-proxy": "Proxy configured. Start the server: pinetunnel start --daemon",
        "install-ea": "Restart MetaTrader, attach EA to chart, set InpLicenseID and InpServerURL",
    }
    msg = suggestions.get(command)
    if msg:
        print()
        print(f"  {_c(msg, _CYAN)}")


# ---------------------------------------------------------------------------
# Setup helpers (used by cmd_setup)
# ---------------------------------------------------------------------------


def _verify_server_running(host: str, port: int, timeout: int = 10) -> bool:
    """Poll the server's /health endpoint until it responds or timeout.

    Prints progress dots while waiting.
    Returns True if health check returns 200, False otherwise.
    """
    import urllib.request
    import urllib.error

    url = f"http://{host}:{port}/health"
    deadline = time.time() + timeout
    sys.stdout.write("  Waiting for server")
    sys.stdout.flush()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    sys.stdout.write(" ready!\n")
                    sys.stdout.flush()
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, OSError):
            pass
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write(" timeout!\n")
    sys.stdout.flush()
    return False


def _print_setup_summary(server_url: str, port: int) -> None:
    """Print comprehensive setup summary with all URLs and next steps."""
    webhook_url = server_url.rstrip("/") + "/"

    print()
    print("  ===============================================")
    print("  PineTunnel is running!")
    print("  ===============================================")
    print()
    print("  Server URL:")
    print(f"    {server_url}")
    print()
    print("  API Documentation (Swagger):")
    print(f"    {server_url}/docs")
    print()
    print("  Webhook URL for TradingView:")
    print(f"    {webhook_url}")
    print()
    print("  EA Download URLs:")
    print(f"    MT5: {server_url}/api/ea/download/mt5")
    print(f"    MT4: {server_url}/api/ea/download/mt4")
    print("    (Requires X-License-Key header - use ADMIN_API_KEY from .env)")
    print()
    print("  --- How to Install the EA ---")
    print()
    print("  1. Download the EA:")
    print(f"     curl -o PineTunnel.ex5 -H 'X-License-Key: YOUR_KEY' \\")
    print(f"       {server_url}/api/ea/download/mt5")
    print()
    print("  2. Copy files to MetaTrader:")
    print("     MT5: PineTunnel.ex5 -> MQL5/Experts/")
    print("          PTWebSocket.dll -> MQL5/Libraries/")
    print("     MT4: PineTunnel.ex4 -> MQL4/Experts/")
    print("          PTWebSocket.dll -> MQL4/Libraries/")
    print()
    print("  3. Enable DLL imports:")
    print("     Tools -> Options -> Expert Advisors -> Allow DLL imports")
    print()
    print("  4. Attach EA to chart, set:")
    print("     InpLicenseID = YOUR_KEY  (ADMIN_API_KEY from .env)")
    print(f"     InpServerURL = {server_url}")
    print()
    print("  --- TradingView Alert Configuration ---")
    print()
    print("  1. Create an alert in TradingView")
    print(f"  2. Set webhook URL to: {webhook_url}")
    print("  3. Set alert message to:")
    print("     YOUR_KEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=YOUR_SECRET")
    print()
    print("  --- Next Steps for Production ---")
    print()
    print("  HTTPS with nginx + SSL (recommended):")
    print("    pinetunnel setup-proxy")
    print()
    print("  Auto-start on boot (install as system service):")
    print("    pinetunnel install-service")
    print()
    print("  Check server health:")
    print("    pinetunnel check")
    print()
    print("  Full documentation:")
    print("    https://github.com/TheFractalyst/PineTunnel#documentation")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup wizard."""
    print(BANNER)
    print("  PineTunnel Setup Wizard")
    print("  This will configure your .env file and run database migrations.\n")

    root = _find_project_root()

    # Step 1: Check prerequisites
    print("  Step 1: Checking prerequisites...")
    ok_python = _ensure_python_311(yes=getattr(args, "yes", False))
    if not ok_python:
        return 1
    _check_system_requirements()
    ok_deps = _ensure_dependencies(yes=getattr(args, "yes", False))
    ok_redis = _ensure_redis(yes=getattr(args, "yes", False))
    print()

    # Step 2: Get server URL
    print("  Step 2: Server configuration...")
    default_url = _resolve_server_url()
    if getattr(args, "yes", False):
        server_url = default_url
        print(f"  Using default URL: {server_url}")
    else:
        try:
            server_url = (
                input(f"  Server URL [{default_url}]: ").strip() or default_url
            )
        except (KeyboardInterrupt, EOFError):
            server_url = default_url
            print(f"\n  {_c('Using default URL.', _CYAN)}")

    # Check firewall for the server port
    parsed_url = urlparse(server_url)
    fw_port = parsed_url.port or 8000
    _ensure_firewall(fw_port, yes=getattr(args, "yes", False))
    print()

    # Step 3: Generate secrets
    print("  Step 3: Generating secure secrets...")
    webhook_secret = _generate_secret()
    jwt_secret = _generate_secret(48)
    admin_api_key = _generate_secret(48)
    print_ok(
        f"WEBHOOK_SECRET ({len(webhook_secret)} chars), "
        f"JWT_SECRET ({len(jwt_secret)} chars), "
        f"ADMIN_API_KEY ({len(admin_api_key)} chars)"
    )
    print()

    # Step 4: Write .env file
    print("  Step 4: Writing .env file...")
    env_path = root / ".env"
    if env_path.exists() and not args.force:
        if getattr(args, "yes", False):
            args.force = True
        else:
            try:
                overwrite = (
                    input(
                        f"  .env already exists at {env_path}. Overwrite? [y/N]: "
                    )
                    .strip()
                    .lower()
                )
            except (KeyboardInterrupt, EOFError):
                overwrite = "n"
            if overwrite != "y":
                print_skip(".env generation (user declined overwrite)")
                print()
            else:
                args.force = True

    if not env_path.exists() or args.force:
        try:
            env_content = ENV_TEMPLATE.format(
                timestamp=datetime.now(timezone.utc).isoformat(),
                server_url=server_url,
                webhook_secret=webhook_secret,
                jwt_secret=jwt_secret,
                admin_api_key=admin_api_key,
                encryption_key=__import__('secrets').token_hex(32),
            )
            env_path.write_text(env_content)
            # Restrict .env permissions to owner only (chmod 600)
            try:
                os.chmod(env_path, 0o600)
            except OSError:
                pass
            print_ok(f".env written to {env_path} (permissions: 600)")
        except PermissionError:
            print_fail(f"Permission denied writing to {env_path}")
            print("         Check file permissions and try again.")
            return 1
        except OSError as e:
            print_fail(f"Could not write .env: {e}")
            return 1
    print()

    # Step 5: Create data directories
    print("  Step 5: Creating data directories...")
    data_dir = root / "data"
    logs_dir = data_dir / "logs"
    try:
        data_dir.mkdir(exist_ok=True)
        logs_dir.mkdir(exist_ok=True)
        print_ok(f"Created {data_dir} and {logs_dir}")
    except OSError as e:
        print_warn(f"Could not create data directories: {e}")
    print()

    # Step 6: Run migrations
    print("  Step 6: Running database migrations...")
    os.environ.setdefault("DATABASE_URL", "sqlite:///pinetunnel.db")
    _ensure_alembic_files(root)
    if (root / "alembic.ini").exists():
        spinner = Spinner("Running alembic upgrade head").start()
        try:
            os.chdir(root)
            os.environ.setdefault("DATABASE_URL", "sqlite:///pinetunnel.db")
            # Use Python API directly to avoid the migrations/ directory
            # shadowing the installed alembic package when using the CLI binary
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "import os,sys; sys.path=[p for p in sys.path if p not in ('','.',os.getcwd())]; "
                    "from alembic.config import Config; from alembic import command; "
                    "cfg=Config('alembic.ini'); "
                    "cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL','sqlite:///pinetunnel.db')); "
                    "command.upgrade(cfg, 'head')",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(root),
            )
            if result.returncode == 0:
                spinner.stop(success=True)
            else:
                spinner.stop(success=False)
                output = (result.stderr or result.stdout)[-300:]
                print(f"         Migration output: {output}")
                print("         You can retry: pinetunnel migrate")
        except subprocess.TimeoutExpired:
            spinner.stop(success=False)
            print("         Migration timed out after 30 seconds.")
            print("         You can retry: pinetunnel migrate")
        except FileNotFoundError:
            spinner.stop(success=False)
            print("         alembic not found. Install: pip install alembic")
        except Exception as e:
            spinner.stop(success=False)
            print(f"         Could not run migrations: {e}")
    else:
        print_skip("alembic.ini not found, skipping migrations")
    print()

    # Step 7: Summary
    _print_setup_guide(server_url)
    print()
    if not ok_redis:
        print_warn("Redis is not running. Start it before starting the server.")
        print()
    if not ok_deps:
        print_warn(
            "Some core dependencies are missing. Install with: pip install -r requirements.txt"
        )
        print()
    print("  For production, use HTTPS and a domain name.")

    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start the FastAPI server."""
    root = _find_project_root()

    # First-run onboarding: auto-trigger init if no .env exists
    if _is_first_run():
        env_path = root / ".env"
        if not env_path.exists():
            print("Welcome to PineTunnel! Let's set things up first.")
            print()
            args.force = False
            ret = cmd_init(args)
            if ret != 0:
                return ret
        _mark_initialized()

    os.chdir(root)
    os.environ.setdefault("DATABASE_URL", "sqlite:///pinetunnel.db")

    # Check dependencies before launching - prevents confusing ImportError
    # crashes during FastAPI lifespan startup
    if not _ensure_dependencies():
        print()
        print_fail("Cannot start: core dependencies are missing.")
        print("         Run: pinetunnel init  or  pip install -r requirements.txt")
        return 1

    # Fail-fast: validate .env exists and required env vars are set
    # BEFORE launching uvicorn. This gives clear error messages instead
    # of a confusing crash during FastAPI lifespan startup.
    from apps.server.config.startup_check import validate_startup

    errors = validate_startup()
    if errors:
        print()
        print_fail("Startup validation failed:")
        print()
        for err in errors:
            print(f"    - {err}")
        print()
        print("  Run `pinetunnel init` for guided setup.")
        print()
        return 1

    host = args.host or os.environ.get("HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("PORT", "8000"))

    # Auto-find a free port if the default is in use
    if _is_port_in_use(port):
        new_port = _find_free_port(port + 1)
        print_warn(f"Port {port} is already in use, using port {new_port} instead.")
        port = new_port

    # Ensure Redis is running (optional - warn but continue if unavailable)
    print("  Checking Redis...")
    if not _ensure_redis():
        print_warn("WebSocket and rate-limiting features will not work without Redis.")
    print()

    if args.daemon:
        # Start as background daemon
        from apps.cli.service import start_daemon
        workers = args.workers or 1
        return start_daemon(host, port, workers)

    print(f"  Starting PineTunnel server on {host}:{port}...")
    print(f"  API docs: http://localhost:{port}/docs")
    print(f"  Press Ctrl+C to stop.\n")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "apps.server.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if args.reload:
        cmd.append("--reload")
    if args.workers and args.workers > 1:
        cmd.extend(["--workers", str(args.workers)])

    try:
        result = subprocess.run(cmd)
        return result.returncode
    except KeyboardInterrupt:
        print(f"\n  {_c('Server stopped.', _YELLOW)}")
        return 0
    except FileNotFoundError:
        print_fail("Could not start uvicorn. Is it installed?")
        print("         Run: pip install uvicorn")
        return 1


def cmd_migrate(args: argparse.Namespace) -> int:
    """Run Alembic database migrations."""
    root = _find_project_root()
    os.chdir(root)
    os.environ.setdefault("DATABASE_URL", "sqlite:///pinetunnel.db")

    direction = "upgrade" if not args.downgrade else "downgrade"
    target = args.target or "head" if not args.downgrade else "-1"

    print(f"  Running alembic {direction} {target}...")
    try:
        result = subprocess.run(
            [
                sys.executable, "-c",
                f"import os,sys; sys.path=[p for p in sys.path if p not in ('','.',os.getcwd())]; "
                f"from alembic.config import Config; from alembic import command; "
                f"cfg=Config('alembic.ini'); "
                f"cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL','sqlite:///pinetunnel.db')); "
                f"command.{direction}(cfg, '{target}')",
            ],
            cwd=root,
        )
        if result.returncode == 0:
            print_ok(f"alembic {direction} {target} completed")
        else:
            print_fail(f"alembic {direction} {target} failed (exit {result.returncode})")
        return result.returncode
    except KeyboardInterrupt:
        print(f"\n  {_c('Migration interrupted.', _YELLOW)}")
        return 130
    except FileNotFoundError:
        print_fail("Could not run alembic. Is it installed?")
        print("         Run: pip install alembic")
        return 1


def cmd_test(args: argparse.Namespace) -> int:
    """Send a test webhook signal to the server."""
    import urllib.request
    import urllib.error

    host = args.host or "localhost"
    port = args.port or 8000
    url = f"http://{host}:{port}/"

    secret = os.environ.get("WEBHOOK_SECRET", "test-secret")
    key = args.key or "TESTKEY"

    payload = (
        f"{key},buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,"
        f"comment=cli-test,secret={secret}"
    )

    print(f"  Sending test signal to {url}")
    print(f"  Payload: {payload[:80]}...")

    try:
        req = urllib.request.Request(
            url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print_ok(f"Response: {resp.status}")
            print(f"  Body: {body[:200]}")
            return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print_fail(f"HTTP {e.code}: {body[:200]}")
        return 1
    except urllib.error.URLError as e:
        print_fail(f"Cannot connect: {e.reason}")
        print("         Is the server running? Start it with: pinetunnel start")
        return 1
    except KeyboardInterrupt:
        print(f"\n  {_c('Test interrupted.', _YELLOW)}")
        return 130


def cmd_install_ea(args: argparse.Namespace) -> int:
    """Auto-detect MetaTrader and install EA files, or copy to remote VPS."""
    from apps.cli.ea_install import (
        download_ea_files,
        handle_non_windows,
        install_ea_automated,
        install_ea_remote,
    )

    plat = args.platform or "both"

    # --download: just copy EA files to current directory
    if getattr(args, "download", False):
        print("  Downloading EA files to current directory...")
        ok = download_ea_files(dest_dir=str(Path.cwd()), plat=plat)
        return 0 if ok else 1

    # --remote: install to remote Windows VPS via SSH
    if args.remote:
        host = args.remote
        username = getattr(args, "username", None)
        key_file = getattr(args, "key_file", None)
        ok = install_ea_remote(
            host=host,
            username=username,
            key_file=key_file,
            plat=plat,
        )
        return 0 if ok else 1

    # Local install: requires Windows
    if platform.system() != "Windows":
        ok = handle_non_windows(plat)
        return 0 if ok else 1

    install_all = getattr(args, "all", False)
    ok = install_ea_automated(plat=plat, install_all=install_all)
    return 0 if ok else 1


def cmd_setup_proxy(args: argparse.Namespace) -> int:
    """Set up nginx reverse proxy and Let's Encrypt SSL."""
    from apps.cli.proxy import detect_domain, setup_reverse_proxy

    server_url = _resolve_server_url()
    port = int(os.environ.get("PORT", "8000"))
    domain = detect_domain(server_url)

    result = setup_reverse_proxy(domain, port)
    return 0 if result else 1


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


def cmd_stop_cloudflare(args: argparse.Namespace) -> int:
    """Stop Cloudflare quick tunnel."""
    from apps.cli.cloudflare import stop_quick_tunnel

    stop_quick_tunnel()
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Show version info with platform details."""
    print(f"  PineTunnel v{__version__}")
    print()
    print(f"  Python:     {platform.python_version()} ({platform.python_implementation()})")
    print(f"  OS:         {platform.system()} {platform.release()}")
    print(f"  Machine:    {platform.machine()}")
    print(f"  Platform:   {platform.platform()}")
    print()
    print(f"  https://github.com/TheFractalyst/PineTunnel")
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    """Print the post-setup guide without running the wizard."""
    server_url = _resolve_server_url()
    print(BANNER)
    _print_setup_guide(server_url)
    print()
    print("  For production, use HTTPS and a domain name.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run all health checks and print a summary table."""
    print(BANNER)
    print("  PineTunnel Health Check\n")

    root = _find_project_root()
    env_path = root / ".env"

    # Load .env if it exists (manual parse, no dep on python-dotenv)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    # 1. Python version
    v = sys.version_info
    py_ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        results.append(("Python", "OK", py_ver))
    else:
        results.append(("Python", "FAIL", f"{py_ver} (requires 3.11+)"))

    # 2. Core dependencies with version detection
    for module, pkg in _CORE_DEPS.items():
        try:
            __import__(module)
            version = _get_dep_version(module)
            display = _DEP_DISPLAY.get(pkg, pkg)
            if version:
                results.append((display, "OK", version))
            else:
                results.append((display, "OK", "installed"))
        except ImportError:
            display = _DEP_DISPLAY.get(pkg, pkg)
            results.append((display, "FAIL", "not installed"))

    # 3. Optional dependencies (warn, not fail)
    try:
        __import__("telegram")
        results.append(("Telegram", "OK", "available"))
    except ImportError:
        results.append(("Telegram", "WARN", "pip install pinetunnel[telegram]"))

    try:
        __import__("psycopg")
        results.append(("Postgres", "OK", "psycopg available"))
    except ImportError:
        try:
            __import__("asyncpg")
            results.append(("Postgres", "OK", "asyncpg available"))
        except ImportError:
            results.append(("Postgres", "WARN", "pip install pinetunnel[postgres]"))

    # 4. Redis connectivity
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        parsed = urlparse(redis_url)
        redis_host = parsed.hostname or "localhost"
        redis_port = parsed.port or 6379
    except Exception:
        redis_host = "localhost"
        redis_port = 6379

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((redis_host, redis_port))
        s.close()
        results.append(("Redis", "OK", f"{redis_host}:{redis_port}"))
    except (socket.error, ConnectionRefusedError, OSError):
        results.append(("Redis", "WARN", f"not running on {redis_host}:{redis_port}"))

    # 5. .env file existence and validation
    if env_path.exists():
        required_vars = ("WEBHOOK_SECRET", "JWT_SECRET", "ADMIN_API_KEY")
        env_content = env_path.read_text()
        missing_vars = []
        for var in required_vars:
            found = False
            for line in env_content.splitlines():
                line = line.strip()
                if line.startswith(f"{var}=") and len(line) > len(var) + 1:
                    found = True
                    break
            if not found:
                missing_vars.append(var)
        if missing_vars:
            results.append((".env", "WARN", f"missing: {', '.join(missing_vars)}"))
        else:
            results.append((".env", "OK", str(env_path)))
    else:
        results.append((".env", "WARN", "not found (run: pinetunnel init)"))

    # 6. Database connectivity
    db_url = os.environ.get("DATABASE_URL", "sqlite:///pinetunnel.db")
    if db_url.startswith("sqlite"):
        db_file = db_url.replace("sqlite:///", "", 1)
        db_path = Path(db_file)
        if not db_path.is_absolute():
            db_path = root / db_path
        if db_path.exists():
            results.append(("Database", "OK", f"SQLite at {db_path.name}"))
        else:
            results.append(("Database", "WARN", "file not found (run: pinetunnel migrate)"))
    else:
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(db_url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            display = db_url.split("@")[-1] if "@" in db_url else db_url
            results.append(("Database", "OK", display))
        except ImportError:
            results.append(("Database", "WARN", "sqlalchemy not installed"))
        except Exception as e:
            err_msg = str(e)[:50]
            results.append(("Database", "WARN", f"connection failed: {err_msg}"))

    # 7. Server health
    server_port = int(os.environ.get("PORT", "8000"))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", server_port))
        s.close()
        results.append(("Server", "OK", f"running on :{server_port}"))
    except (socket.error, ConnectionRefusedError, OSError):
        results.append(("Server", "WARN", "Not running (start with: pinetunnel start)"))

    # Print summary table
    col_name = max(len("Check"), max(len(r[0]) for r in results))
    col_status = max(len("Status"), max(len(r[1]) for r in results))
    col_detail = min(max(len("Detail"), max(len(r[2]) for r in results)), 50)

    border = "  +" + "-" * (col_name + 2) + "+" + "-" * (col_status + 2) + "+" + "-" * (col_detail + 2) + "+"
    header = f"  | {'Check':<{col_name}} | {'Status':<{col_status}} | {'Detail':<{col_detail}} |"

    print()
    print(border)
    print(header)
    print(border)

    fail_count = 0
    warn_count = 0
    for name, status, detail in results:
        if status == "OK":
            status_colored = _c(status, _GREEN)
        elif status == "WARN":
            status_colored = _c(status, _YELLOW)
            warn_count += 1
        else:
            status_colored = _c(status, _RED)
            fail_count += 1

        status_field = status_colored + " " * (col_status - len(status))
        detail_display = detail if len(detail) <= col_detail else detail[: col_detail - 3] + "..."
        row = f"  | {name:<{col_name}} | {status_field} | {detail_display:<{col_detail}} |"
        print(row)

    print(border)

    print()
    if fail_count > 0:
        print_fail(f"{fail_count} check(s) failed, {warn_count} warning(s)")
        return 1
    elif warn_count > 0:
        print_warn(f"All critical checks passed. {warn_count} warning(s).")
        return 0
    else:
        print_ok("All checks passed")
        return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """One-command VPS setup: check, configure, start, verify."""
    print(BANNER)
    print("  PineTunnel One-Command Setup")
    print("  Checks requirements, configures .env, runs migrations,")
    print("  starts the server, and verifies it's responding.\n")

    root = _find_project_root()
    host = args.host or "0.0.0.0"
    port = args.port or 8000
    yes = args.yes

    # Step 1: System requirements
    print("  Step 1/11: Checking system requirements...")
    _check_system_requirements()
    print()

    # Step 2: Python version
    print("  Step 2/11: Checking Python version...")
    if not _ensure_python_311(yes=yes):
        print_fail("Python 3.11+ is required and could not be installed.")
        return 1
    print()

    # Step 3: Dependencies
    print("  Step 3/11: Checking dependencies...")
    if not _ensure_dependencies(yes=yes):
        print_fail("Core dependencies are missing and could not be installed.")
        print("         Try: pip install -r requirements.txt")
        return 1
    print()

    # Step 4: Redis
    print("  Step 4/11: Checking Redis...")
    if not _ensure_redis(yes=yes):
        print_warn("Redis is not running. WebSocket features will not work.")
    print()

    # Step 5: Firewall (will be configured after we know the binding mode)
    print("  Step 5/11: Firewall will be configured after webhook URL selection.")
    print()

    # Step 6: Generate .env (always overwrite in setup)
    print("  Step 6/11: Generating .env configuration...")
    env_path = root / ".env"
    public_ip = _get_public_ip()
    server_url = f"http://{public_ip}:{port}"
    webhook_secret = _generate_secret()
    jwt_secret = _generate_secret(48)
    admin_api_key = _generate_secret(48)
    encryption_key = __import__('secrets').token_hex(32)
    env_content = ENV_TEMPLATE.format(
        timestamp=datetime.now(timezone.utc).isoformat(),
        server_url=server_url,
        webhook_secret=webhook_secret,
        jwt_secret=jwt_secret,
        admin_api_key=admin_api_key,
        encryption_key=encryption_key,
    )
    env_path.write_text(env_content)
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    print_ok(f".env written to {env_path} (permissions: 600)")
    print_ok(f"Server URL: {server_url}")
    print_ok(f"WEBHOOK_SECRET ({len(webhook_secret)} chars)")
    print_ok(f"JWT_SECRET ({len(jwt_secret)} chars)")
    print_ok(f"ADMIN_API_KEY ({len(admin_api_key)} chars)")
    print_ok(f"SIGNAL_ENCRYPTION_KEY ({len(encryption_key)} chars)")
    print()

    # Step 7: Migrations
    print("  Step 7/11: Running database migrations...")
    os.environ.setdefault("DATABASE_URL", "sqlite:///pinetunnel.db")
    _ensure_alembic_files(root)
    if (root / "alembic.ini").exists():
        spinner = Spinner("Running alembic upgrade head").start()
        try:
            os.chdir(root)
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "import os,sys; sys.path=[p for p in sys.path if p not in ('','.',os.getcwd())]; "
                    "from alembic.config import Config; from alembic import command; "
                    "cfg=Config('alembic.ini'); "
                    "cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL','sqlite:///pinetunnel.db')); "
                    "command.upgrade(cfg, 'head')",
                ],
                capture_output=True, text=True, timeout=30,
                cwd=str(root),
            )
            if result.returncode == 0:
                spinner.stop(success=True)
            else:
                spinner.stop(success=False)
                output = (result.stderr or result.stdout)[-300:]
                print(f"         Migration output: {output}")
                print_fail("Database migrations failed.")
                print("         Try: pinetunnel migrate")
                return 1
        except subprocess.TimeoutExpired:
            spinner.stop(success=False)
            print_fail("Migration timed out after 30 seconds.")
            return 1
        except FileNotFoundError:
            spinner.stop(success=False)
            print_fail("alembic not found. Install: pip install alembic")
            return 1
        except Exception as e:
            spinner.stop(success=False)
            print_fail(f"Could not run migrations: {e}")
            return 1
    else:
        print_skip("alembic.ini not found, skipping migrations")
    print()

    # Step 8: Data directories
    print("  Step 8/11: Creating data directories...")
    data_dir = root / "data"
    logs_dir = data_dir / "logs"
    try:
        data_dir.mkdir(exist_ok=True)
        logs_dir.mkdir(exist_ok=True)
        print_ok(f"Created {data_dir} and {logs_dir}")
    except OSError as e:
        print_warn(f"Could not create data directories: {e}")
    print()

    # Step 9: Start server
    print("  Step 9/11: Starting server as daemon...")
    if _is_port_in_use(port):
        new_port = _find_free_port(port + 1)
        print_warn(f"Port {port} is already in use, using port {new_port} instead.")
        port = new_port
        # Update .env with the new port
        env_path = root / ".env"
        if env_path.exists():
            content = env_path.read_text()
            import re
            content = re.sub(r'SERVER_URL=.*', f'SERVER_URL=http://{public_ip}:{port}', content)
            content = re.sub(r'PORT=.*', f'PORT={port}', content)
            env_path.write_text(content)

    from apps.cli.service import restart_daemon

    ret = restart_daemon(host, port, 1)
    if ret != 0:
        print_fail("Failed to start server daemon.")
        print("         Check: pinetunnel-daemon.log")
        print("         Or run: pinetunnel start (foreground to see errors)")
        return 1
    print()

    # Step 10: Verify server
    print("  Step 10/11: Verifying server is responding...")
    time.sleep(5)
    verify_host = "127.0.0.1" if host == "0.0.0.0" else host
    if not _verify_server_running(verify_host, port, timeout=10):
        print_fail("Server did not respond within 15 seconds.")
        print("         Check: pinetunnel-daemon.log")
        print("         Or run: pinetunnel start (foreground to see errors)")
        return 1
    print_ok("Server is responding on /health")
    print()

    # Step 11: Summary
    print("  Step 11/11: Setup complete!")
    _print_setup_summary(server_url, port)

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pinetunnel",
        description="TradingView to MetaTrader bridge.\n\n"
                    "Run 'pinetunnel' with no args for auto-setup (first run).\n"
                    "Run 'pinetunnel --help' to see all commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quick start:
  pip install pinetunnel
  pinetunnel                   First run: auto-setup (2 minutes)

Common commands:
  pinetunnel start --daemon    Start server in background
  pinetunnel status            Check if server is running
  pinetunnel stop              Stop the server
  pinetunnel test              Send a test webhook signal
  pinetunnel check             Run health checks

Full documentation: https://github.com/TheFractalyst/PineTunnel#documentation
""",
        add_help=True,
    )
    parser.add_argument("--version", action="version", version=f"PineTunnel v{__version__}")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    p_init = subparsers.add_parser("init", help="Interactive setup wizard")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .env")
    p_init.add_argument("--yes", action="store_true", help="Skip all prompts, use defaults")
    p_init.set_defaults(func=cmd_init)

    # start
    p_start = subparsers.add_parser("start", help="Start the FastAPI server")
    p_start.add_argument("--host", help="Bind address (default: 0.0.0.0)")
    p_start.add_argument("--port", type=int, help="Port (default: 8000)")
    p_start.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (development)"
    )
    p_start.add_argument("--workers", type=int, help="Number of workers (default: 1)")
    p_start.add_argument(
        "--daemon", action="store_true", help="Start as background daemon process"
    )
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop the running daemon")
    p_stop.set_defaults(func=lambda a: __import__("apps.cli.service", fromlist=["stop_daemon"]).stop_daemon())

    # status
    p_status = subparsers.add_parser("status", help="Check if daemon is running")
    p_status.set_defaults(func=lambda a: __import__("apps.cli.service", fromlist=["status_daemon"]).status_daemon())

    # install-service
    p_install = subparsers.add_parser("install-service", help="Install as OS-native service (systemd/launchd/Windows)")
    p_install.set_defaults(func=lambda a: __import__("apps.cli.service", fromlist=["install_service"]).install_service())

    # uninstall-service
    p_uninstall = subparsers.add_parser("uninstall-service", help="Remove OS-native service")
    p_uninstall.set_defaults(func=lambda a: __import__("apps.cli.service", fromlist=["uninstall_service"]).uninstall_service())

    # migrate
    p_migrate = subparsers.add_parser("migrate", help="Run Alembic database migrations")
    p_migrate.add_argument(
        "--downgrade", action="store_true", help="Rollback one migration"
    )
    p_migrate.add_argument("--target", help="Target revision (default: head)")
    p_migrate.set_defaults(func=cmd_migrate)

    # test
    p_test = subparsers.add_parser("test", help="Send a test webhook signal")
    p_test.add_argument("--host", help="Server host (default: localhost)")
    p_test.add_argument("--port", type=int, help="Server port (default: 8000)")
    p_test.add_argument("--key", help="License key (default: TESTKEY)")
    p_test.set_defaults(func=cmd_test)

    # setup-proxy
    p_proxy = subparsers.add_parser(
        "setup-proxy",
        help="Set up nginx reverse proxy and Let's Encrypt SSL",
    )
    p_proxy.set_defaults(func=cmd_setup_proxy)

    # version
    p_version = subparsers.add_parser("version", help="Show version info")
    p_version.set_defaults(func=cmd_version)

    # guide
    p_guide = subparsers.add_parser("guide", help="Print the post-setup guide")
    p_guide.set_defaults(func=cmd_guide)

    # check
    p_check = subparsers.add_parser("check", help="Run health checks (deps, Redis, database, server)")
    p_check.set_defaults(func=cmd_check)

    # setup
    p_setup = subparsers.add_parser(
        "setup", help="One-command VPS setup (check, configure, start, verify)"
    )
    p_setup.add_argument("--yes", action="store_true", help="Skip all confirmation prompts")
    p_setup.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    p_setup.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p_setup.set_defaults(func=cmd_setup)

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

    # stop-cloudflare
    p_cf_stop = subparsers.add_parser("stop-cloudflare", help="Stop Cloudflare quick tunnel")
    p_cf_stop.set_defaults(func=cmd_stop_cloudflare)

    # install-ea
    p_ea = subparsers.add_parser(
        "install-ea",
        help="Auto-detect MetaTrader and install EA + DLL files",
    )
    p_ea.add_argument(
        "--all",
        action="store_true",
        help="Install to ALL detected MetaTrader instances (no prompt)",
    )
    p_ea.add_argument(
        "--remote",
        metavar="HOST",
        help="Install to remote Windows VPS via SSH (e.g., user@1.2.3.4)",
    )
    p_ea.add_argument(
        "--username",
        help="Remote SSH username (if not included in --remote)",
    )
    p_ea.add_argument(
        "--key-file",
        help="SSH private key file for remote connection",
    )
    p_ea.add_argument(
        "--platform",
        choices=["mt5", "mt4", "both"],
        default="both",
        help="Which EA to install (default: both)",
    )
    p_ea.add_argument(
        "--download",
        action="store_true",
        help="Download EA files to current directory (non-Windows fallback)",
    )
    p_ea.set_defaults(func=cmd_install_ea)

    return parser


def _print_setup_menu() -> int:
    """First run: auto-setup everything, no menu."""
    return _run_quick_setup()


def _run_quick_setup() -> int:
    """One unified setup: server + Cloudflare + Telegram + service + EA.

    Steps:
      1. Server (deps, Redis, firewall, .env, migrations, start daemon, verify)
      2. HTTPS via Cloudflare quick tunnel (automatic)
      3. Telegram bot (bot token + admin IDs from @BotFather)
      4. OS service (auto-start on boot, always installed)
      5. EA install (auto-detect all MetaTrader instances, copy files)
    """
    print()
    print("  ============================================")
    print("  PineTunnel Setup")
    print("  ============================================")
    print()

    # Step 1: Server setup
    print("  --- Step 1/5: Server ---")
    print()
    setup_args = argparse.Namespace(yes=False, host="0.0.0.0", port=8000)
    ret = cmd_setup(setup_args)
    if ret != 0:
        print()
        print("  [FAIL] Setup did not complete. Check errors above.")
        return ret
    print()

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

    # Step 3: Telegram bot setup
    print("  --- Step 3/5: Telegram Bot ---")
    print()
    print("  Get a bot token from @BotFather on Telegram:")
    print("    1. Open Telegram, search @BotFather")
    print("    2. Send /newbot, follow prompts")
    print("    3. Copy the bot token (looks like: 123456:ABC-DEF...)")
    print()
    print("  To find your Telegram user ID:")
    print("    Send /start to @userinfobot on Telegram")
    print()
    try:
        bot_token = input("  Bot token (required): ").strip()
    except (KeyboardInterrupt, EOFError):
        bot_token = ""

    if not bot_token:
        print("  [FAIL] Telegram bot token is required. Get one from @BotFather.")
        print("         Run setup again: pinetunnel")
        return 1

    try:
        admin_ids = input("  Your Telegram user ID (required): ").strip()
    except (KeyboardInterrupt, EOFError):
        admin_ids = ""

    if not admin_ids:
        print("  [FAIL] Telegram user ID is required. Get it from @userinfobot.")
        print("         Run setup again: pinetunnel")
        return 1

    # Update .env with Telegram config
    env_path = _find_env_path_for_update()
    if env_path:
        _update_env_var(env_path, "TELEGRAM_BOT_TOKEN", bot_token)
        _update_env_var(env_path, "TELEGRAM_ADMIN_IDS", admin_ids)
        print()
        print("  [OK]   Telegram config saved to .env")
        print()
        # Show encryption key for PineScript input
        enc_key = os.environ.get("SIGNAL_ENCRYPTION_KEY", "")
        if not enc_key:
            for line in env_path.read_text().splitlines():
                if line.startswith("SIGNAL_ENCRYPTION_KEY=") and len(line) > len("SIGNAL_ENCRYPTION_KEY="):
                    enc_key = line.split("=", 1)[1].strip()
                    break
        if enc_key:
            print(f"  [IMPORTANT] Signal Encryption Key (set this in PineScript encKey input):")
            print(f"              {enc_key}")
            print(f"              This key is also in your .env file: SIGNAL_ENCRYPTION_KEY")
        print()
        print("  [INFO] Restart server to activate: pinetunnel stop && pinetunnel start --daemon")
    else:
        print("  [WARN] Could not find .env file. Set manually:")
        print(f"         TELEGRAM_BOT_TOKEN={bot_token}")
        print(f"         TELEGRAM_ADMIN_IDS={admin_ids}")
    print()

    # Step 4: OS service (always install)
    print("  --- Step 4/5: OS Service ---")
    print()
    print("  Installing as OS service (auto-start on boot)...")
    from apps.cli.service import install_service
    install_service()
    print()

    # Step 5: EA install
    print("  --- Step 5/5: EA Install ---")
    print()
    try:
        do_ea = input("  Install EA on MetaTrader? [Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        do_ea = "n"
    if do_ea in ("y", "yes", ""):
        ea_args = argparse.Namespace(
            all=False, remote=None, username=None, key_file=None,
            platform="both", download=False,
        )
        cmd_install_ea(ea_args)
    else:
        print("  Skipped. Run later: pinetunnel install-ea")
    print()

    # Done
    print("  ============================================")
    print("  Setup Complete!")
    print("  ============================================")
    print()
    print("  Useful commands:")
    print("    pinetunnel status     Check server")
    print("    pinetunnel test       Send test signal")
    print("    pinetunnel check      Health checks")
    print("    pinetunnel stop       Stop server")
    print()
    _mark_initialized()
    return 0


def _find_env_path_for_update() -> Path | None:
    """Find .env file path for updating."""
    p = Path.cwd()
    while p != p.parent:
        if (p / ".env").exists():
            return p / ".env"
        p = p.parent
    return None


def _update_env_var(env_path: Path, key: str, value: str) -> bool:
    """Update or add a variable in .env file. Returns True if updated."""
    lines = env_path.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    return True


def main() -> int:
    """CLI entry point.

    On first run with no command, auto-starts setup wizard.
    On subsequent bare invocations, shows the welcome banner + help.
    """
    parser = build_parser()
    args = parser.parse_args()

    # Handle --no-color by setting NO_COLOR env var
    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"

    if not args.command:
        if _is_first_run():
            return _print_setup_menu()
        if not _server_is_healthy():
            return _print_setup_menu()
        _print_welcome()
        parser.print_help()
        return 0

    try:
        ret = args.func(args)
        if ret == 0:
            _suggest_next_command(args.command)
            if args.command in ("setup", "init"):
                _mark_initialized()
        return ret
    except KeyboardInterrupt:
        print(f"\n  {_c('Interrupted.', _YELLOW)}")
        return 130
    except EOFError:
        print(f"\n  {_c('Input closed.', _YELLOW)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Cross-platform daemon and service management for PineTunnel.

Supports:
- Daemon mode (--daemon): background process with PID file
- Service install: OS-native service registration
  - Linux: systemd user unit
  - macOS: launchd plist
  - Windows: Windows Service via sc.exe + NSSM wrapper
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


_PID_FILE = "pinetunnel.pid"
_LOG_FILE = "pinetunnel-daemon.log"


def _pid_path() -> Path:
    """Get PID file path in project root."""
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p / _PID_FILE
        p = p.parent
    return Path.cwd() / _PID_FILE


def _log_path() -> Path:
    """Get daemon log file path in project root."""
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p / _LOG_FILE
        p = p.parent
    return Path.cwd() / _LOG_FILE


def _project_root() -> Path:
    """Find project root."""
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def is_running() -> int | None:
    """Check if daemon is running. Returns PID or None."""
    pidfile = _pid_path()
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None

    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if str(pid) in result.stdout:
                return pid
            return None
        else:
            os.kill(pid, 0)
            return pid
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        try:
            pidfile.unlink()
        except OSError:
            pass
        return None


def restart_daemon(host: str, port: int, workers: int = 1) -> int:
    """Stop any existing daemon, then start a fresh one."""
    if is_running():
        pid = is_running()
        logger.warning("Stopping existing daemon (PID %s)...", pid)
        stop_daemon()
    return start_daemon(host, port, workers)


def start_daemon(host: str, port: int, workers: int = 1) -> int:
    """Start the server as a background daemon process."""
    if is_running():
        pid = is_running()
        logger.error("PineTunnel is already running (PID %s)", pid)
        logger.error("Stop it first: pinetunnel stop")
        return 1
    root = _project_root()
    log_path = _log_path()

    env_path = root / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    cmd = [
        sys.executable, "-m", "uvicorn",
        "apps.server.main:app",
        "--host", host,
        "--port", str(port),
        "--workers", str(workers),
    ]

    log_fd = open(log_path, "a")

    if platform.system() == "Windows":
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    _pid_path().write_text(str(proc.pid))

    time.sleep(2)
    if proc.poll() is not None:
        logger.error("Server exited immediately (code %s)", proc.returncode)
        logger.error("Check log: %s", log_path)
        try:
            _pid_path().unlink()
        except OSError:
            pass
        return 1

    logger.info("PineTunnel daemon started (PID %s)", proc.pid)
    logger.info("Host: %s:%s", host, port)
    logger.info("Log: %s", log_path)
    logger.info("PID: %s", _pid_path())
    logger.info("Stop: pinetunnel stop | Status: pinetunnel status")
    return 0


def stop_daemon() -> int:
    """Stop the running daemon."""
    pid = is_running()
    if not pid:
        logger.warning("PineTunnel is not running")
        return 0

    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)

        logger.info("PineTunnel stopped (PID %s)", pid)
    except ProcessLookupError:
        logger.warning("Process %s not found (already stopped)", pid)
    except PermissionError:
        logger.error("Permission denied stopping PID %s", pid)
        return 1
    finally:
        try:
            _pid_path().unlink()
        except OSError:
            pass
    return 0


def status_daemon() -> int:
    """Show daemon status."""
    pid = is_running()
    if not pid:
        logger.warning("PineTunnel is not running")
        return 1

    log_path = _log_path()
    logger.info("PineTunnel is running (PID %s)", pid)
    logger.info("Log: %s", log_path)

    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:8000/health", timeout=3)
        if resp.status == 200:
            logger.info("Health: OK (server responding)")
        else:
            logger.info("Health: HTTP %s", resp.status)
    except Exception:
        logger.warning("Health: unreachable (server may still be starting)")

    return 0


def _systemd_unit_path() -> Path:
    """Get systemd user unit path."""
    return Path.home() / ".config" / "systemd" / "user" / "pinetunnel.service"


def _launchd_plist_path() -> Path:
    """Get launchd plist path."""
    return Path.home() / "Library" / "LaunchAgents" / "com.pinetunnel.plist"


def install_service() -> int:
    """Install PineTunnel as an OS-native service."""
    os_name = platform.system()
    root = _project_root()
    python_exe = sys.executable

    if os_name == "Linux":
        return _install_systemd(root, python_exe)
    elif os_name == "Darwin":
        return _install_launchd(root, python_exe)
    elif os_name == "Windows":
        return _install_windows_service(root, python_exe)
    else:
        logger.error("Unsupported OS: %s", os_name)
        logger.error("Use 'pinetunnel start --daemon' instead")
        return 1


def _install_systemd(root: Path, python_exe: str) -> int:
    """Install systemd user service on Linux."""
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    unit_content = f"""[Unit]
Description=PineTunnel - TradingView to MetaTrader bridge
After=network.target redis.service

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={python_exe} -m uvicorn apps.server.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
EnvironmentFile={root}/.env

[Install]
WantedBy=default.target
"""

    unit_path.write_text(unit_content)
    logger.info("systemd unit written to %s", unit_path)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "pinetunnel"], check=True)
        logger.info("Service enabled (starts on boot)")
        logger.info("Manage with: systemctl --user start|stop|status pinetunnel")
        logger.info("Logs: journalctl --user -u pinetunnel -f")
        return 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Could not auto-enable service: %s", e)
        logger.warning("Run manually: systemctl --user daemon-reload && systemctl --user enable pinetunnel")
        return 0


def _install_launchd(root: Path, python_exe: str) -> int:
    """Install launchd agent on macOS."""
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pinetunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>apps.server.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8000</string>
        <string>--workers</string>
        <string>1</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{root}/pinetunnel-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{root}/pinetunnel-daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""

    plist_path.write_text(plist_content)
    logger.info("launchd plist written to %s", plist_path)

    try:
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        logger.info("Service loaded (starts on login)")
        logger.info("Manage with: launchctl start|stop com.pinetunnel")
        logger.info("Remove: launchctl unload %s", plist_path)
        logger.info("Logs: tail -f %s/pinetunnel-daemon.log", root)
        return 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Could not auto-load service: %s", e)
        logger.warning("Run manually: launchctl load %s", plist_path)
        return 0


def _install_windows_service(root: Path, python_exe: str) -> int:
    """Install Windows Service via sc.exe."""
    bin_path = f'"{python_exe}" -m uvicorn apps.server.main:app --host 0.0.0.0 --port 8000 --workers 1'

    try:
        result = subprocess.run(
            ["sc.exe", "create", "PineTunnel",
             "binpath=", f'cmd /c {bin_path}',
             "start=", "auto",
             "displayname=", "PineTunnel Server"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Windows service 'PineTunnel' created")
            logger.info("Manage with: sc start|stop|delete|query PineTunnel")
            return 0
        else:
            logger.warning("sc.exe returned: %s", result.stderr.strip() or result.stdout.strip())
            logger.warning("Try running as Administrator")
            return 1
    except FileNotFoundError:
        logger.error("sc.exe not found")
        logger.error("Use 'pinetunnel start --daemon' instead")
        return 1


def uninstall_service() -> int:
    """Remove the OS-native service."""
    os_name = platform.system()

    if os_name == "Linux":
        unit_path = _systemd_unit_path()
        try:
            subprocess.run(["systemctl", "--user", "stop", "pinetunnel"], capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", "pinetunnel"], capture_output=True)
            if unit_path.exists():
                unit_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            logger.info("systemd service removed")
        except FileNotFoundError:
            logger.error("systemctl not found")
            return 1
        return 0

    elif os_name == "Darwin":
        plist_path = _launchd_plist_path()
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            if plist_path.exists():
                plist_path.unlink()
            logger.info("launchd agent removed")
        except FileNotFoundError:
            logger.error("launchctl not found")
            return 1
        return 0

    elif os_name == "Windows":
        try:
            subprocess.run(["sc.exe", "stop", "PineTunnel"], capture_output=True)
            subprocess.run(["sc.exe", "delete", "PineTunnel"], capture_output=True)
            logger.info("Windows service removed")
        except FileNotFoundError:
            logger.error("sc.exe not found")
            return 1
        return 0

    else:
        logger.error("Unsupported OS: %s", os_name)
        return 1

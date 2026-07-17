"""Cross-platform daemon and service management for PineTunnel.

Supports:
- Daemon mode (--daemon): background process with PID file
- Service install: OS-native service registration
  - Linux: systemd user unit
  - macOS: launchd plist
  - Windows: Windows Service via sc.exe + NSSM wrapper
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path


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

    # Check if process exists (cross-platform)
    try:
        if platform.system() == "Windows":
            # Windows: use tasklist
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if str(pid) in result.stdout:
                return pid
            return None
        else:
            # POSIX: send signal 0 (no-op, just checks existence)
            os.kill(pid, 0)
            return pid
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        # Process doesn't exist or can't be reached
        try:
            pidfile.unlink()
        except OSError:
            pass
        return None


def restart_daemon(host: str, port: int, workers: int = 1) -> int:
    """Stop any existing daemon, then start a fresh one."""
    if is_running():
        pid = is_running()
        print(f"  [WARN] Stopping existing daemon (PID {pid})...")
        stop_daemon()
    return start_daemon(host, port, workers)


def start_daemon(host: str, port: int, workers: int = 1) -> int:
    """Start the server as a background daemon process."""
    if is_running():
        pid = is_running()
        print(f"  [FAIL] PineTunnel is already running (PID {pid})")
        print(f"         Stop it first: pinetunnel stop")
        return 1
    root = _project_root()
    log_path = _log_path()

    # Load .env into os.environ so the daemon subprocess inherits it
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
        # Windows: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        # POSIX: start_new_session=True (setsid)
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Write PID file
    _pid_path().write_text(str(proc.pid))

    # Wait a moment and check if it's still alive
    time.sleep(2)
    if proc.poll() is not None:
        print(f"  [FAIL] Server exited immediately (code {proc.returncode})")
        print(f"         Check log: {log_path}")
        try:
            _pid_path().unlink()
        except OSError:
            pass
        return 1

    print(f"  [OK]   PineTunnel daemon started (PID {proc.pid})")
    print(f"  Host:  {host}:{port}")
    print(f"  Log:   {log_path}")
    print(f"  PID:   {_pid_path()}")
    print()
    print(f"  Stop:    pinetunnel stop")
    print(f"  Status:  pinetunnel status")
    return 0


def stop_daemon() -> int:
    """Stop the running daemon."""
    pid = is_running()
    if not pid:
        print(f"  [WARN] PineTunnel is not running")
        return 0

    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], timeout=10)
        else:
            # Send SIGTERM, wait, then SIGKILL if needed
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)

        print(f"  [OK]   PineTunnel stopped (PID {pid})")
    except ProcessLookupError:
        print(f"  [WARN] Process {pid} not found (already stopped)")
    except PermissionError:
        print(f"  [FAIL] Permission denied stopping PID {pid}")
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
        print(f"  [WARN] PineTunnel is not running")
        return 1

    log_path = _log_path()
    print(f"  [OK]   PineTunnel is running (PID {pid})")
    print(f"  Log:   {log_path}")

    # Try to get health check
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:8000/health", timeout=3)
        if resp.status == 200:
            print(f"  Health: OK (server responding)")
        else:
            print(f"  Health: HTTP {resp.status}")
    except Exception:
        print(f"  Health: unreachable (server may still be starting)")

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
        print(f"  [FAIL] Unsupported OS: {os_name}")
        print(f"         Use 'pinetunnel start --daemon' instead")
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
    print(f"  [OK]   systemd unit written to {unit_path}")

    # Reload systemd and enable
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "pinetunnel"], check=True)
        print(f"  [OK]   Service enabled (starts on boot)")
        print()
        print(f"  Manage with:")
        print(f"    systemctl --user start pinetunnel")
        print(f"    systemctl --user stop pinetunnel")
        print(f"    systemctl --user status pinetunnel")
        print(f"    journalctl --user -u pinetunnel -f  (view logs)")
        return 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  [WARN] Could not auto-enable service: {e}")
        print(f"         Run manually: systemctl --user daemon-reload && systemctl --user enable pinetunnel")
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
    print(f"  [OK]   launchd plist written to {plist_path}")

    try:
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        print(f"  [OK]   Service loaded (starts on login)")
        print()
        print(f"  Manage with:")
        print(f"    launchctl start com.pinetunnel")
        print(f"    launchctl stop com.pinetunnel")
        print(f"    launchctl unload {plist_path}  (remove)")
        print(f"    tail -f {root}/pinetunnel-daemon.log  (logs)")
        return 0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  [WARN] Could not auto-load service: {e}")
        print(f"         Run manually: launchctl load {plist_path}")
        return 0


def _install_windows_service(root: Path, python_exe: str) -> int:
    """Install Windows Service via sc.exe."""
    # On Windows, we use sc.exe to create a service that runs the Python command
    # We wrap it in a cmd /c to handle the Python path properly
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
            print(f"  [OK]   Windows service 'PineTunnel' created")
            print()
            print(f"  Manage with:")
            print(f"    sc start PineTunnel")
            print(f"    sc stop PineTunnel")
            print(f"    sc delete PineTunnel")
            print(f"    sc query PineTunnel  (status)")
            return 0
        else:
            print(f"  [WARN] sc.exe returned: {result.stderr.strip() or result.stdout.strip()}")
            print(f"         Try running as Administrator")
            return 1
    except FileNotFoundError:
        print(f"  [FAIL] sc.exe not found")
        print(f"         Use 'pinetunnel start --daemon' instead")
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
            print(f"  [OK]   systemd service removed")
        except FileNotFoundError:
            print(f"  [FAIL] systemctl not found")
            return 1
        return 0

    elif os_name == "Darwin":
        plist_path = _launchd_plist_path()
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            if plist_path.exists():
                plist_path.unlink()
            print(f"  [OK]   launchd agent removed")
        except FileNotFoundError:
            print(f"  [FAIL] launchctl not found")
            return 1
        return 0

    elif os_name == "Windows":
        try:
            subprocess.run(["sc.exe", "stop", "PineTunnel"], capture_output=True)
            subprocess.run(["sc.exe", "delete", "PineTunnel"], capture_output=True)
            print(f"  [OK]   Windows service removed")
        except FileNotFoundError:
            print(f"  [FAIL] sc.exe not found")
            return 1
        return 0

    else:
        print(f"  [FAIL] Unsupported OS: {os_name}")
        return 1

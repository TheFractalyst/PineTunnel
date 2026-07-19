"""Backward-compat shim. Logic moved to apps.lib.service."""

from apps.lib.service import (  # noqa: F401
    install_service,
    is_running,
    restart_daemon,
    start_daemon,
    status_daemon,
    stop_daemon,
    uninstall_service,
)

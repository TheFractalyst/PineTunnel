"""Disk usage utilities."""

import logging
import shutil
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CRITICAL_FREE_MB = 100
_BYTES_PER_MB = 1024 * 1024


def get_disk_usage(disk_path: str) -> dict[str, float | None | str]:
    """Get disk usage statistics for the given path.

    Returns a dict with free_mb, total_mb, used_percent, status, and path.
    On failure, returns None values for numeric fields and status='error'.
    """
    path = disk_path if Path(disk_path).exists() else "/"
    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free / _BYTES_PER_MB
        total_mb = usage.total / _BYTES_PER_MB
        used_percent = (usage.used / usage.total) * 100
        status = "critical" if free_mb < _CRITICAL_FREE_MB else "healthy"
        return {
            "free_mb": round(free_mb, 2),
            "total_mb": round(total_mb, 2),
            "used_percent": round(used_percent, 2),
            "status": status,
            "path": path,
        }
    except (OSError, PermissionError, FileNotFoundError) as e:
        _LOGGER.warning(
            "Failed to get disk usage for %s: %s: %s",
            path,
            type(e).__name__,
            e,
            extra={"context": {"disk_path": path, "operation": "disk_usage"}},
        )
        return {
            "free_mb": None,
            "total_mb": None,
            "used_percent": None,
            "status": "error",
            "path": path,
        }

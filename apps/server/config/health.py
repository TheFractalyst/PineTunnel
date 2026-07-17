"""Health check system with dependency verification and memory monitoring."""

import logging
import time
from datetime import datetime
from typing import Any

import psutil

from apps.server.utils import format_uptime

logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024

# Component name constants
COMPONENT_DATABASE = "database"
COMPONENT_CONFIG = "config"
COMPONENT_APP = "app"
COMPONENT_MEMORY = "memory"
COMPONENT_REDIS = "redis"


class HealthCheckManager:
    """Manages application health and readiness state with dependency verification."""

    def __init__(self) -> None:
        self.startup_time: float = time.time()
        self.is_ready: bool = False
        self.readiness_checks: dict[str, bool] = {
            COMPONENT_DATABASE: False,
            COMPONENT_CONFIG: False,
            COMPONENT_APP: False,
            COMPONENT_MEMORY: True,
            COMPONENT_REDIS: True,  # Optional — defaults to healthy
        }
        self.last_error: str | None = None
        self.error_count: int = 0

        # Memory thresholds (in MB)
        self.memory_warning_threshold: int = 500  # 500MB
        self.memory_critical_threshold: int = 1024  # 1GB

    def mark_component_ready(self, component: str) -> None:
        """Mark a component as ready and re-evaluate overall readiness."""
        if component in self.readiness_checks:
            self.readiness_checks[component] = True
            self._check_overall_readiness()

    def mark_component_not_ready(self, component: str) -> None:
        """Mark a component as not ready, immediately re-evaluating overall state."""
        if component in self.readiness_checks:
            self.readiness_checks[component] = False
            self.is_ready = all(self.readiness_checks.values())

    def _check_overall_readiness(self) -> None:
        """Check if all components are ready."""
        self.is_ready = all(self.readiness_checks.values())

    def mark_ready(self) -> None:
        """Mark all components and the application as fully ready."""
        for component in self.readiness_checks:
            self.readiness_checks[component] = True
        self.is_ready = True

    def record_error(self, error: str) -> None:
        """Record a transient error for health reporting."""
        self.error_count += 1
        self.last_error = error

    def get_uptime(self) -> float:
        """Return uptime in seconds since startup."""
        return time.time() - self.startup_time

    def check_memory_usage(self) -> dict[str, Any]:
        """Check current memory usage of the application.

        Returns:
            Dictionary with memory statistics and threshold status.
        """
        result: dict[str, Any] = {
            "status": "ok",
            "used_mb": 0,
            "percent": 0,
            "threshold_warning_mb": self.memory_warning_threshold,
            "threshold_critical_mb": self.memory_critical_threshold,
            "system_total_mb": 0,
            "system_available_mb": 0,
        }

        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            system_memory = psutil.virtual_memory()

            used_mb = memory_info.rss / _BYTES_PER_MB
            system_total_mb = system_memory.total / _BYTES_PER_MB
            system_available_mb = system_memory.available / _BYTES_PER_MB

            result["used_mb"] = round(used_mb, 2)
            result["percent"] = round((used_mb / system_total_mb) * 100, 2)
            result["system_total_mb"] = round(system_total_mb, 2)
            result["system_available_mb"] = round(system_available_mb, 2)

            if used_mb > self.memory_critical_threshold:
                result["status"] = "critical"
                logger.error(
                    "Memory usage CRITICAL: %.2fMB exceeds threshold of %dMB",
                    used_mb,
                    self.memory_critical_threshold,
                )
                self.mark_component_not_ready(COMPONENT_MEMORY)
                return result

            if used_mb > self.memory_warning_threshold:
                result["status"] = "warning"
                logger.warning(
                    "Memory usage WARNING: %.2fMB exceeds threshold of %dMB",
                    used_mb,
                    self.memory_warning_threshold,
                )
                return result

            self.mark_component_ready(COMPONENT_MEMORY)
        except Exception as e:
            result["status"] = "error"
            result["error"] = "Memory check failed"
            logger.error("Failed to check memory usage: %s", e)

        return result

    def get_health_status(self) -> dict[str, Any]:
        """Return detailed health status with all component checks."""
        uptime = self.get_uptime()
        memory_status = self.check_memory_usage()

        status: dict[str, Any] = {
            "status": "healthy" if self.is_ready else "starting",
            "ready": self.is_ready,
            "uptime_seconds": round(uptime, 2),
            "uptime_formatted": format_uptime(uptime),
            "components": self.readiness_checks,
            "memory": memory_status,
            "errors": {"count": self.error_count, "last": self.last_error},
            "timestamp": datetime.now().isoformat(),
        }

        if memory_status.get("status") == "critical":
            status["status"] = "critical"
        elif memory_status.get("status") == "warning" and status["status"] == "healthy":
            status["status"] = "warning"

        return status


# Global health manager instance
health_manager = HealthCheckManager()

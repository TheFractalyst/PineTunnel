"""Structured logging configuration for PineTunnel Server.

Production-grade logging with rotation, JSON format, and correlation IDs.
"""

import json
import logging
import logging.handlers
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger

from .settings import get_config

_CORRELATION_ID_LENGTH = 8

_REDACT_KEYS = frozenset(
    {
        "password",
        "secret",
        "secret_key",
        "token",
        "api_key",
        "webhook_secret",
        "jwt_secret",
        "admin_api_key",
        "bot_token",
        "authorization",
        "cookie",
        "license_key",
        "license",
    }
)
_REDACT_VALUE = "***REDACTED***"
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\r\n]")
_JSON_REMOVED_KEYS = ("asctime", "levelname", "name", "pathname", "lineno", "funcName")

# Match a license key: a run of >=13 alnum chars (13-digit numeric license keys
# and longer prefixed keys). signal_ids are uuid4()[:8] (8 chars) so they never match.
_LICENSE_KEY_RUN_RE = re.compile(r"[A-Za-z0-9]{13,}")


@lru_cache(maxsize=128)
def _mask_license_key(key: str) -> str:
    """Mask a license key for logs, keeping the first 8 chars for correlation."""
    if not key or len(key) <= 8:
        return "***"
    return f"{key[:8]}***"


@lru_cache(maxsize=128)
def _mask_path(path: str) -> str:
    """Mask license keys embedded anywhere in a URL path.

    Covers both the poll route ``/api/signals/{key}`` (trailing segment) and the
    ACK route ``DELETE /api/signals/{key}/{signal_id}`` (middle segment), plus any
    other path carrying a >=13-char alnum license key. signal_ids (8 chars) are
    never masked.
    """
    if not path:
        return path
    return _LICENSE_KEY_RUN_RE.sub(lambda m: _mask_license_key(m.group(0)), path)


_LOG_DIR_FILENAME = "pinetunnel.log"
_LOG_ERRORS_FILENAME = "pinetunnel_errors.log"

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str:
    """Return the current correlation ID, creating one if absent."""
    cid = correlation_id.get()
    if cid is None:
        cid = str(uuid.uuid4())[:_CORRELATION_ID_LENGTH]
        correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async context."""
    correlation_id.set(cid)


class CorrelationIdFilter(logging.Filter):
    """Add correlation ID to all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


class JSONFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter with correlation IDs and source location."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.utcnow().isoformat() + "Z"
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["correlation_id"] = getattr(record, "correlation_id", "N/A")
        log_record["source"] = {
            "file": record.pathname,
            "line": record.lineno,
            "function": record.funcName,
        }
        for key in _JSON_REMOVED_KEYS:
            log_record.pop(key, None)


def setup_logging() -> logging.Logger:
    """Configure production-grade logging with rotation and JSON formatting.

    Returns:
        Configured root logger.
    """
    config = get_config()

    log_dir = Path(config.logging.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(config.get_log_level())
    root_logger.handlers = []

    formatter: logging.Formatter
    if config.logging.format == "json":
        formatter = JSONFormatter(
            "%(timestamp)s %(level)s %(name)s %(message)s %(correlation_id)s"
        )  # type: ignore[no-untyped-call]
    else:
        formatter = logging.Formatter(
            "%(asctime)s - [%(correlation_id)s] - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / _LOG_DIR_FILENAME,
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(config.get_log_level())
    file_handler.setFormatter(formatter)
    file_handler.addFilter(CorrelationIdFilter())
    root_logger.addHandler(file_handler)

    # Error log file (errors only)
    error_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / _LOG_ERRORS_FILENAME,
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(CorrelationIdFilter())
    root_logger.addHandler(error_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(config.get_log_level())
    console_handler.setFormatter(formatter)
    console_handler.addFilter(CorrelationIdFilter())
    root_logger.addHandler(console_handler)

    logger = logging.getLogger(__name__)
    logger.info(
        "Logging initialized",
        extra={
            "log_level": config.logging.level,
            "log_format": config.logging.format,
            "log_dir": str(log_dir),
            "max_bytes": config.logging.max_bytes,
            "backup_count": config.logging.backup_count,
        },
    )

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the given name."""
    return logging.getLogger(name)


def _sanitize_log_data(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive keys and strip control characters from log data."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k.lower() in _REDACT_KEYS:
            out[k] = _REDACT_VALUE
            continue
        if isinstance(v, dict):
            out[k] = _sanitize_log_data(v)
            continue
        if isinstance(v, str):
            # Mask license keys embedded in query strings / query params
            # (e.g. "license_key=XXXXXXXXXXXXX" logged from middleware).
            if k.lower() in ("query_params", "query_string"):
                v = _LICENSE_KEY_RUN_RE.sub(lambda m: _mask_license_key(m.group(0)), v)
            out[k] = _CTRL_CHAR_RE.sub(" ", v)
            continue
        out[k] = v
    return out


def log_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_ip: str | None = None,
    user_agent: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log an HTTP request with structured data."""
    logger = logging.getLogger("http.request")

    log_data: dict[str, Any] = {
        "event": "http_request",
        "method": method,
        "path": _mask_path(path),
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
        "client_ip": client_ip,
        "user_agent": user_agent,
        "correlation_id": get_correlation_id(),
    }

    if extra:
        log_data.update(extra)

    log_data = _sanitize_log_data(log_data)

    if status_code >= 500:
        logger.error(json.dumps(log_data))
        return
    if status_code >= 400:
        logger.warning(json.dumps(log_data))
        return
    logger.info(json.dumps(log_data))


def log_security_event(
    event_type: str,
    description: str,
    severity: str = "warning",
    source_ip: str | None = None,
    user_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a security-related event."""
    logger = logging.getLogger("security")

    log_data: dict[str, Any] = {
        "event": "security_event",
        "event_type": event_type,
        "description": description,
        "severity": severity,
        "source_ip": source_ip,
        "user_id": user_id,
        "correlation_id": get_correlation_id(),
    }

    if extra:
        log_data.update(extra)

    log_data = _sanitize_log_data(log_data)

    level_map: dict[str, int] = {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
    }
    level = level_map.get(severity, logging.INFO)
    logger.log(level, json.dumps(log_data))

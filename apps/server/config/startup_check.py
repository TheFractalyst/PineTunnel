"""
PineTunnel Server -- Startup validation.

Fail-fast checks for required environment variables and .env file existence.
Called from both the CLI (cmd_start) and the FastAPI lifespan to prevent the
server from silently starting with missing or invalid configuration.

Key design decisions (per official docs):
- os.environ is a mapping that raises KeyError on missing keys; we use
  os.environ.get() with explicit defaults so missing vars are detected as
  None/empty, not crashed with KeyError at import time.
  (https://docs.python.org/3/library/os.html#os.environ)
- sys.exit(1) is used in CLI context to terminate with a non-zero exit code.
  (https://docs.python.org/3/library/sys.html#sys.exit)
- FastAPI/pydantic-settings load env vars into typed settings objects; this
  validator runs BEFORE pydantic instantiation so errors are caught earlier
  and with clearer messages than Pydantic's ValidationError output.
  (https://fastapi.tiangolo.com/advanced/settings/)
  (https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env file if python-dotenv is available so validate_startup()
# sees vars from .env without requiring the caller to load_dotenv() first.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_MIN_SECRET_LENGTH = 32

_REQUIRED_VARS: list[tuple[str, str]] = [
    ("WEBHOOK_SECRET", "Webhook validation secret (min 32 chars). Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""),
    ("JWT_SECRET", "JWT signing secret (min 32 chars). Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""),
    ("ADMIN_API_KEY", "Admin API key (min 32 chars). Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""),
    ("SERVER_BASE_URL", "Public base URL for download links, must start with http:// or https:// (e.g. https://your-server.com)"),
]

_SECRET_VARS: list[str] = ["WEBHOOK_SECRET", "JWT_SECRET", "ADMIN_API_KEY"]

_OPTIONAL_VARS: list[tuple[str, str]] = [
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token from @BotFather (enables admin bot)"),
    ("TELEGRAM_ADMIN_IDS", "Comma-separated Telegram admin user IDs"),
    ("REDIS_URL", "Redis connection URL for sessions and rate limiting (redis:// or rediss://)"),
    ("SIGNAL_ENCRYPTION_KEY", "RC4 signal encryption key (64-char hex). Required if PineScript sends encrypted signals."),
]


def _find_project_root() -> Path:
    """Find the project root by searching for pyproject.toml."""
    p = Path.cwd()
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def check_env_file_exists(project_root: Path | None = None) -> bool:
    """Check if .env file exists in the project root.

    Returns True if .env exists, False otherwise.
    In production, .env may not exist (env vars set by OS or service manager).
    """
    _env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "")).lower()
    if _env in ("production", "staging"):
        return True

    root = project_root or _find_project_root()
    return (root / ".env").exists()


def validate_startup() -> list[str]:
    """Run all startup validation checks.

    Returns a list of error messages. An empty list means validation passed.
    Warnings for optional vars are printed to stderr but do not count as errors.
    """
    errors: list[str] = []

    # --- .env file existence ---
    if not check_env_file_exists():
        errors.append("No .env file found. Run `pinetunnel init` first.")

    # --- Required env vars: presence ---
    for var_name, description in _REQUIRED_VARS:
        value = os.environ.get(var_name, "")
        if not value:
            errors.append(f"{var_name} is not set. Expected: {description}")

    # --- Secret length checks ---
    for var_name in _SECRET_VARS:
        value = os.environ.get(var_name, "")
        if value and len(value) < _MIN_SECRET_LENGTH:
            errors.append(
                f"{var_name} is only {len(value)} chars long. "
                f"Must be at least {_MIN_SECRET_LENGTH} chars. "
                f"Generate a secure value with: "
                f"python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

    # --- SERVER_BASE_URL format ---
    base_url = os.environ.get("SERVER_BASE_URL", "")
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors.append(
            f"SERVER_BASE_URL must start with http:// or https://. "
            f"Current value: '{base_url}'"
        )

    # --- DATABASE_URL format ---
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url and not (db_url.startswith("sqlite://") or db_url.startswith("postgresql://")):
        errors.append(
            f"DATABASE_URL must start with sqlite:// or postgresql://. "
            f"Current value starts with: '{db_url[:20]}...'"
        )

    # --- REDIS_URL format ---
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url and not (redis_url.startswith("redis://") or redis_url.startswith("rediss://")):
        errors.append(
            f"REDIS_URL must start with redis:// or rediss://. "
            f"Current value starts with: '{redis_url[:20]}...'"
        )

    # --- Optional vars: warnings only ---
    for var_name, description in _OPTIONAL_VARS:
        value = os.environ.get(var_name, "")
        if not value:
            print(f"  [WARN] {var_name} not set. {description}", file=sys.stderr)

    # --- Security: warn if running as root ---
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  [WARN] Server is running as root. Consider using a non-root user.", file=sys.stderr)

    # --- Security: warn if .env is world-readable ---
    env_path = _find_project_root() / ".env"
    if env_path.exists():
        try:
            stat = env_path.stat()
            if stat.st_mode & 0o077:
                print(f"  [WARN] .env is readable by group/others (mode {oct(stat.st_mode & 0o777)}). Run: chmod 600 .env", file=sys.stderr)
        except OSError:
            pass

    # --- Security: warn if binding to 0.0.0.0 without firewall ---
    host = os.environ.get("HOST", "0.0.0.0")
    if host == "0.0.0.0":
        base_url = os.environ.get("SERVER_BASE_URL", "")
        if base_url.startswith("https://"):
            print("  [WARN] Server binds to 0.0.0.0 but HTTPS is configured. Consider binding to 127.0.0.1 if using a reverse proxy or tunnel.", file=sys.stderr)

    return errors


def run_startup_checks() -> bool:
    """Run startup validation and print results.

    Returns True if all checks passed, False if any errors were found.
    If errors are found, prints them to stderr and exits with code 1.
    This is intended for CLI use (cmd_start).
    """
    errors = validate_startup()

    if errors:
        print("\n  [FAIL] Startup validation failed:", file=sys.stderr)
        print(file=sys.stderr)
        for err in errors:
            print(f"    - {err}", file=sys.stderr)
        print(file=sys.stderr)
        print("  Fix these issues and try again.", file=sys.stderr)
        print("  Run `pinetunnel init` for guided setup.", file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(1)

    return True


def assert_startup_ok() -> None:
    """Run startup validation and raise RuntimeError on failure.

    Intended for use inside the FastAPI lifespan, where raising an exception
    prevents the server from starting.
    """
    errors = validate_startup()
    if errors:
        msg = "Startup validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise RuntimeError(msg)

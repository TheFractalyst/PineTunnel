"""Atomic .env file read/write with secret redaction."""

import os
import secrets
import tempfile
from pathlib import Path

_SECRET_KEY_PATTERNS = (
    "SECRET",
    "TOKEN",
    "KEY",
    "PASSWORD",
    "PASSPHRASE",
    "CREDENTIAL",
)


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(p in upper for p in _SECRET_KEY_PATTERNS)


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    current = read_env(path)
    current.update(updates)
    lines = [f"{k}={v}" for k, v in current.items()]
    content = "\n".join(lines) + "\n"
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".env.tmp.")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def redact_value(key: str, value: str) -> str:
    if not _is_secret_key(key):
        return value
    masked = value[:4] + "****" if len(value) > 4 else value + "****"
    return f"{masked} ({len(value)} chars)"


def generate_secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)[:length]

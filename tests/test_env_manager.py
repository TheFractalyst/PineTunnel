"""Tests for apps.lib.env_manager - atomic .env read/write/redact."""

import os
from pathlib import Path

import pytest

from apps.lib.env_manager import (
    generate_secret,
    read_env,
    redact_value,
    write_env_updates,
)


@pytest.fixture
def tmp_env(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("WEBHOOK_SECRET=abc123\n# comment\nPORT=8000\n")
    os.chmod(p, 0o600)
    return p


def test_read_env_returns_dict(tmp_env: Path):
    result = read_env(tmp_env)
    assert result == {"WEBHOOK_SECRET": "abc123", "PORT": "8000"}


def test_read_env_skips_comments_and_blanks(tmp_env: Path):
    result = read_env(tmp_env)
    assert "# comment" not in result
    assert "" not in result


def test_write_env_updates_preserves_existing(tmp_env: Path):
    write_env_updates(tmp_env, {"PORT": "9000", "NEW_KEY": "newval"})
    result = read_env(tmp_env)
    assert result["PORT"] == "9000"
    assert result["NEW_KEY"] == "newval"
    assert result["WEBHOOK_SECRET"] == "abc123"


def test_write_env_updates_preserves_permissions(tmp_env: Path):
    write_env_updates(tmp_env, {"PORT": "9000"})
    mode = os.stat(tmp_env).st_mode & 0o777
    assert mode == 0o600


def test_write_env_updates_is_atomic(tmp_env: Path):
    original = tmp_env.read_text()
    try:
        write_env_updates(tmp_env, {"PORT": "9000"})
    except Exception:
        assert tmp_env.read_text() == original
    assert tmp_env.read_text() != original


def test_redact_value_masks_secrets():
    assert redact_value("WEBHOOK_SECRET", "abcdefghijklmnop") == "abcd**** (16 chars)"


def test_redact_value_shows_non_secrets():
    assert redact_value("PORT", "8000") == "8000"


def test_redact_value_handles_short_secrets():
    assert redact_value("JWT_SECRET", "ab") == "ab**** (2 chars)"


def test_generate_secret_default_length():
    s = generate_secret()
    assert len(s) == 32
    assert s.isascii()


def test_generate_secret_custom_length():
    s = generate_secret(48)
    assert len(s) == 48

"""Tests for Telegram bot helpers - secret key masking in format_license_info.

The telegram package (python-telegram-bot) is not installed in the test
environment, so we stub the minimal imports required to load helpers.py
and verify that secret keys are masked (first 4 chars + ****), matching
the frontend's maskKey() behavior.
"""

import importlib.util
import sys
import types

import pytest


@pytest.fixture
def helpers_module():
    """Load helpers.py with stubbed telegram/dateutil imports."""
    tg = types.ModuleType("telegram")
    tg.helpers = types.ModuleType("telegram.helpers")
    tg.helpers.escape_markdown = lambda s, version=1: s
    sys.modules["telegram"] = tg
    sys.modules["telegram.helpers"] = tg.helpers

    du = types.ModuleType("dateutil")
    du_parser = types.ModuleType("dateutil.parser")
    du_parser.parse = lambda s: s
    du.parser = du_parser
    sys.modules["dateutil"] = du
    sys.modules["dateutil.parser"] = du_parser

    ws_conn = types.ModuleType("apps.server.ws.connection")
    ws_conn.HTTP_POLLING_TIMEOUT = 60
    sys.modules["apps.server.ws.connection"] = ws_conn

    spec = importlib.util.spec_from_file_location(
        "_helpers_test", "apps/server/services/telegram/helpers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    for k in ("telegram", "telegram.helpers", "dateutil", "dateutil.parser",
              "apps.server.ws.connection", "_helpers_test"):
        sys.modules.pop(k, None)


def test_mask_secret_long_key(helpers_module):
    assert helpers_module.mask_secret("abcd123456") == "abcd****"


def test_mask_secret_short_key(helpers_module):
    assert helpers_module.mask_secret("ab") == "ab****"


def test_mask_secret_empty(helpers_module):
    assert helpers_module.mask_secret("") == "****"


def test_mask_secret_four_chars(helpers_module):
    assert helpers_module.mask_secret("abcd") == "abcd****"


def test_mask_secret_exact_boundary(helpers_module):
    assert helpers_module.mask_secret("abcde") == "abcd****"


def test_format_license_info_masks_secret_key(helpers_module):
    data = {
        "name": "Test User",
        "email": "test@example.com",
        "status": "active",
        "secret_key": "supersecret123",
        "enabled": True,
    }
    out = helpers_module.format_license_info("lic_key_123", data)
    assert "supersecret123" not in out, "RAW secret key leaked in bot output!"
    assert "supe****" in out, "Secret key not masked in bot output!"


def test_format_license_info_short_secret_masked(helpers_module):
    data = {
        "name": "Short",
        "email": "s@x.com",
        "status": "active",
        "secret_key": "ab",
        "enabled": True,
    }
    out = helpers_module.format_license_info("k", data)
    assert "ab****" in out
    assert "ab" in out


def test_format_license_info_empty_secret(helpers_module):
    data = {
        "name": "Empty",
        "email": "e@x.com",
        "status": "active",
        "secret_key": "",
        "enabled": True,
    }
    out = helpers_module.format_license_info("k", data)
    assert "****" in out

"""Tests for Telegram dashboard formatting helpers.

The telegram package is not installed in test env, so we stub minimal
imports and test the pure-function helpers in dashboards.py.
"""

import importlib.util
import sys
import types

import pytest


@pytest.fixture
def dash_module():
    """Load dashboards.py with stubbed telegram imports."""
    tg = types.ModuleType("telegram")
    tg.helpers = types.ModuleType("telegram.helpers")
    tg.helpers.escape_markdown = lambda s, version=1: s
    class _StubButton:
        def __init__(self, text, callback_data=None, **kwargs):
            self.text = text
            self.callback_data = callback_data
    tg.InlineKeyboardButton = _StubButton
    sys.modules["telegram"] = tg
    sys.modules["telegram.helpers"] = tg.helpers

    spec = importlib.util.spec_from_file_location(
        "_dashboards_test",
        "apps/server/services/telegram/dashboards.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    for k in ("telegram", "telegram.helpers", "_dashboards_test"):
        sys.modules.pop(k, None)


def test_mask_license_key_long(dash_module):
    assert dash_module.mask_license_key("abcd1234efgh5678") == "abcd1234..."


def test_mask_license_key_short(dash_module):
    assert dash_module.mask_license_key("abc") == "abc..."


def test_mask_license_key_empty(dash_module):
    assert dash_module.mask_license_key("") == "****"


def test_mask_secret_long(dash_module):
    assert dash_module.mask_secret("abcd123456") == "abcd****"


def test_mask_secret_short(dash_module):
    assert dash_module.mask_secret("ab") == "ab****"


def test_mask_secret_empty(dash_module):
    assert dash_module.mask_secret("") == "****"


def test_calc_pagination_basic(dash_module):
    page, total_pages, start = dash_module.calc_pagination(0, 42, 8)
    assert page == 0
    assert total_pages == 6
    assert start == 0


def test_calc_pagination_last_page(dash_module):
    page, total_pages, start = dash_module.calc_pagination(5, 42, 8)
    assert page == 5
    assert total_pages == 6
    assert start == 40


def test_calc_pagination_clamps_negative(dash_module):
    page, total_pages, start = dash_module.calc_pagination(-1, 42, 8)
    assert page == 0
    assert start == 0


def test_calc_pagination_empty(dash_module):
    page, total_pages, start = dash_module.calc_pagination(0, 0, 8)
    assert page == 0
    assert total_pages == 1
    assert start == 0


def test_sep_is_ascii(dash_module):
    assert dash_module.SEP == "-" * 20
    assert dash_module.SEP.isascii()


class FakeClientManager:
    def __init__(self, clients):
        self.clients = clients


class FakeDbManager:
    def __init__(self, rows=None):
        self._rows = rows or []

    def execute_query(self, sql, params=None):
        return self._rows


class FakeBot:
    def __init__(self, clients=None, db_rows=None):
        self.client_manager = FakeClientManager(clients or {})
        self.db_manager = FakeDbManager(db_rows)
        self.ws_manager = None
        self.http_polling_clients = {}
        self.alerts_enabled = True
        self.signal_queues = {}


def test_overview_screen_basic(dash_module):
    bot = FakeBot(
        clients={"key1": {"name": "Test", "status": "active", "expires_at": "2026-08-07"}},
        db_rows=[{"timestamp": "2026-07-20T14:30:00", "action": "buy", "symbol": "EURUSD", "response_code": 200}],
    )
    text, keyboard = dash_module.overview_screen(bot)
    assert "Overview" in text
    assert "Licenses" in text or "No licenses" in text
    assert len(keyboard) > 0
    assert text.isascii(), "Overview text must be ASCII-only"


def test_overview_screen_no_licenses(dash_module):
    bot = FakeBot()
    text, keyboard = dash_module.overview_screen(bot)
    assert "No licenses" in text or "0" in text
    assert text.isascii()

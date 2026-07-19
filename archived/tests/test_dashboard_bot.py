"""Tests for dashboard bot-test and bot-info endpoints."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.server.auth.session import setup_session_middleware
from apps.server.auth.telegram_auth import TelegramAuthStore
from apps.server.routes.dashboard import create_dashboard_router

CSRF_HEADERS = {"X-Admin-CSRF": "1"}


class _FakeBot:
    def __init__(self, started=True, admin_ids=None, app=None, alerts_enabled=True):
        self._started = started
        self.admin_ids = admin_ids if admin_ids is not None else [123]
        self.app = app
        self.alerts_enabled = alerts_enabled
        self._cached_bot_username = None
        self._cached_bot_first_name = None


class _FakeApp:
    def __init__(self, handlers=None, bot=None):
        self.handlers = handlers if handlers is not None else {0: []}
        self.bot = bot


class _FakeTelegramBot:
    def __init__(self, username="pinetunnel_bot", first_name="PineTunnel"):
        self.username = username
        self.first_name = first_name
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def get_me(self):
        class _Me:
            def __init__(self, username, first_name):
                self.username = username
                self.first_name = first_name

        return _Me(self.username, self.first_name)


@pytest.fixture
def dashboard_app(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ADMIN_IDS=123\n")
    store = TelegramAuthStore(ttl_seconds=90)
    app = FastAPI()
    setup_session_middleware(app, secret_key="test-secret-key-for-tests-32chars")
    app.include_router(create_dashboard_router(auth_store=store, admin_ids=[123], env_path=env_path))
    return app, store


def _login(client, store, user_id=123):
    code = store.issue_code(user_id=user_id)
    client.post("/api/dashboard/login", json={"code": code, "user_id": user_id}, headers=CSRF_HEADERS)


def test_bot_info_when_no_bot(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    monkeypatch.setattr(state, "telegram_bot", None)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/bot-info")
    assert r.status_code == 200
    data = r.json()
    assert data["started"] is False
    assert data["username"] is None
    assert data["first_name"] is None
    assert data["handler_count"] == 0
    assert data["admin_ids"] == []
    assert data["alerts_enabled"] is False


def test_bot_info_with_running_bot(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    tg_bot = _FakeTelegramBot()
    fake_app = _FakeApp(handlers={0: ["h1", "h2"], 1: ["h3"]}, bot=tg_bot)
    bot = _FakeBot(started=True, admin_ids=[123, 456], app=fake_app, alerts_enabled=True)
    monkeypatch.setattr(state, "telegram_bot", bot)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/bot-info")
    assert r.status_code == 200
    data = r.json()
    assert data["started"] is True
    assert data["username"] == "pinetunnel_bot"
    assert data["first_name"] == "PineTunnel"
    assert data["handler_count"] == 3
    assert data["admin_ids"] == [123, 456]
    assert data["alerts_enabled"] is True


def test_bot_info_requires_auth(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.get("/api/dashboard/bot-info")
    assert r.status_code == 401


def test_bot_test_when_bot_not_running(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    bot = _FakeBot(started=False, admin_ids=[123])
    monkeypatch.setattr(state, "telegram_bot", bot)
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert data["error"] == "Bot not running"


def test_bot_test_when_no_bot(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    monkeypatch.setattr(state, "telegram_bot", None)
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert data["error"] == "Bot not running"


def test_bot_test_when_no_admin_ids(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    fake_app = _FakeApp(bot=_FakeTelegramBot())
    monkeypatch.setattr(state, "telegram_bot", _FakeBot(started=True, admin_ids=[], app=fake_app))
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "")
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert data["error"] == "No admin IDs configured"


def test_bot_test_sends_message_to_first_admin(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    tg_bot = _FakeTelegramBot()
    fake_app = _FakeApp(bot=tg_bot)
    bot = _FakeBot(started=True, admin_ids=[123, 456], app=fake_app)
    monkeypatch.setattr(state, "telegram_bot", bot)
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["message"] == "Test message sent"
    assert len(tg_bot.sent) == 1
    chat_id, text = tg_bot.sent[0]
    assert chat_id == 123
    assert text.startswith("Test message from PineTunnel dashboard at ")
    assert "T" in text and ":" in text


def test_bot_test_sanitizes_error_paths(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    class _ErrorBot:
        async def send_message(self, chat_id, text):
            raise RuntimeError("failed at /Users/fractalyst/secret/bot.py:42")

    tg_bot = _ErrorBot()
    fake_app = _FakeApp(bot=tg_bot)
    bot = _FakeBot(started=True, admin_ids=[123], app=fake_app)
    monkeypatch.setattr(state, "telegram_bot", bot)
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert "<path>" in data["error"]
    assert "/Users/fractalyst/secret" not in data["error"]


def test_bot_test_requires_auth(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 401


def test_bot_test_requires_csrf(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    tg_bot = _FakeTelegramBot()
    fake_app = _FakeApp(bot=tg_bot)
    bot = _FakeBot(started=True, admin_ids=[123], app=fake_app)
    monkeypatch.setattr(state, "telegram_bot", bot)
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test")
    assert r.status_code == 403


def test_bot_test_falls_back_to_env_admin_ids(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    tg_bot = _FakeTelegramBot()
    fake_app = _FakeApp(bot=tg_bot)
    bot = _FakeBot(started=True, admin_ids=[], app=fake_app)
    monkeypatch.setattr(state, "telegram_bot", bot)
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "789")
    client = TestClient(app)
    _login(client, store)
    r = client.post("/api/dashboard/bot-test", headers=CSRF_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert tg_bot.sent[0][0] == 789

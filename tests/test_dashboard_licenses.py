"""Tests for dashboard /api/dashboard/users endpoint and license data wiring.

Verifies the License Manager panel reads from client_manager.clients
(the same dict the Telegram bot uses) with the correct data shape.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.server.auth.session import setup_session_middleware
from apps.server.auth.telegram_auth import TelegramAuthStore
from apps.server.routes.dashboard import create_dashboard_router

CSRF_HEADERS = {"X-Admin-CSRF": "1"}


class _FakeClientManager:
    def __init__(self, clients):
        self.clients = clients


class _FakeWSManager:
    def __init__(self, counts):
        self._counts = counts

    def get_connection_count(self, key):
        return self._counts.get(key, 0)


class _FakeDBManager:
    def __init__(self):
        self._tables = {}

    def get_connection(self):
        class _Session:
            def __init__(self, tables):
                self._tables = tables

            def execute(self, stmt, params=None):
                stmt_str = str(stmt)
                if "ws_signal_log" in stmt_str:
                    rows = self._tables.get("ws_signal_log", {})
                    return [(k, v) for k, v in rows.items() if k in (params or {}).values()]
                if "trades" in stmt_str:
                    rows = self._tables.get("trades", {})
                    return [(k, v) for k, v in rows.items() if k in (params or {}).values()]
                if "ws_open_positions" in stmt_str:
                    rows = self._tables.get("ws_open_positions", {})
                    return [(k, v) for k, v in rows.items() if k in (params or {}).values()]
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Session(self._tables)


@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
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


def _setup_state(monkeypatch, clients, ws_counts=None, db_tables=None):
    from apps.server import state

    cm = _FakeClientManager(clients)
    ws = _FakeWSManager(ws_counts or {})
    db = _FakeDBManager()
    db._tables = db_tables or {}
    monkeypatch.setattr(state, "client_manager", cm)
    monkeypatch.setattr(state, "ws_manager", ws)
    monkeypatch.setattr(state, "db_manager", db)
    return cm, ws, db


def test_users_endpoint_returns_correct_shape(dashboard_app, monkeypatch):
    app, store = dashboard_app
    clients = {
        "key111111111111": {
            "name": "Alice",
            "email": "alice@example.com",
            "status": "active",
            "enabled": True,
            "expires_at": "2026-12-31T23:59:59",
            "secret_key": "supersecretkey123",
            "last_activity": "2026-07-19T12:00:00",
        },
    }
    _setup_state(monkeypatch, clients, ws_counts={"key111111111111": 2})
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    data = r.json()
    assert data["total_users"] == 1
    users = data["users"]
    assert len(users) == 1
    u = users[0]
    assert u["email"] == "alice@example.com"
    assert u["name"] == "Alice"
    assert len(u["licenses"]) == 1
    lic = u["licenses"][0]
    assert lic["license_key"] == "key111111111111"
    assert lic["status"] == "active"
    assert lic["enabled"] is True
    assert lic["expires_at"] == "2026-12-31T23:59:59"
    assert lic["secret_key"] == "supersecretkey123"
    assert lic["last_activity"] == "2026-07-19T12:00:00"
    stats = u["stats"]
    assert stats["connected_eas"] == 2
    assert "total_trades" in stats
    assert "total_signals" in stats
    assert "total_positions" in stats


def test_users_endpoint_groups_by_email(dashboard_app, monkeypatch):
    app, store = dashboard_app
    clients = {
        "key_aaaaaaaaaaaa": {
            "name": "Bob",
            "email": "bob@example.com",
            "status": "active",
            "enabled": True,
            "secret_key": "secret_a",
        },
        "key_bbbbbbbbbbbb": {
            "name": "Bob",
            "email": "bob@example.com",
            "status": "active",
            "enabled": True,
            "secret_key": "secret_b",
        },
    }
    _setup_state(monkeypatch, clients)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    data = r.json()
    assert data["total_users"] == 1
    u = data["users"][0]
    assert len(u["licenses"]) == 2
    keys = {lic["license_key"] for lic in u["licenses"]}
    assert keys == {"key_aaaaaaaaaaaa", "key_bbbbbbbbbbbb"}


def test_users_endpoint_requires_auth(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 401


def test_users_endpoint_503_when_no_client_manager(dashboard_app, monkeypatch):
    app, store = dashboard_app
    from apps.server import state

    monkeypatch.setattr(state, "client_manager", None)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 503


def test_users_endpoint_reads_same_dict_as_bot(dashboard_app, monkeypatch):
    """The endpoint must read from client_manager.clients - the same dict the bot uses."""
    app, store = dashboard_app
    clients = {
        "shared_key_12345": {
            "name": "Shared",
            "email": "shared@example.com",
            "status": "active",
            "enabled": True,
            "secret_key": "shared_secret",
        },
    }
    cm, _, _ = _setup_state(monkeypatch, clients)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    data = r.json()
    lic_key = data["users"][0]["licenses"][0]["license_key"]
    assert lic_key in cm.clients
    assert lic_key == "shared_key_12345"


def test_users_endpoint_connected_eas_from_ws_manager(dashboard_app, monkeypatch):
    """connected_eas must come from ws_manager.get_connection_count - same as bot."""
    app, store = dashboard_app
    clients = {
        "k1" * 12: {"name": "A", "email": "a@x.com", "status": "active", "enabled": True},
    }
    key = list(clients.keys())[0]
    _setup_state(monkeypatch, clients, ws_counts={key: 5})
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    data = r.json()
    assert data["users"][0]["stats"]["connected_eas"] == 5


def test_users_endpoint_disabled_license(dashboard_app, monkeypatch):
    app, store = dashboard_app
    clients = {
        "disabled_key123": {
            "name": "Disabled",
            "email": "dis@x.com",
            "status": "active",
            "enabled": False,
            "secret_key": "s",
        },
    }
    _setup_state(monkeypatch, clients)
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    lic = r.json()["users"][0]["licenses"][0]
    assert lic["enabled"] is False


def test_users_endpoint_empty_clients(dashboard_app, monkeypatch):
    app, store = dashboard_app
    _setup_state(monkeypatch, {})
    client = TestClient(app)
    _login(client, store)
    r = client.get("/api/dashboard/users")
    assert r.status_code == 200
    data = r.json()
    assert data["total_users"] == 0
    assert data["users"] == []

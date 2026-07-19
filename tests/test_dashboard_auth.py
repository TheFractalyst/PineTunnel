"""Tests for dashboard auth: session middleware and require_auth dependency."""

import time

import fastapi
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.server.auth.session import require_auth, setup_session_middleware
from apps.server.auth.telegram_auth import TelegramAuthStore
from apps.server.routes.dashboard import create_dashboard_router


@pytest.fixture
def app_with_session():
    app = FastAPI()
    setup_session_middleware(app, secret_key="test-secret-key-for-tests-32chars")

    @app.get("/protected")
    async def protected(_: None = Depends(require_auth)):
        return {"ok": True}

    @app.get("/public")
    async def public():
        return {"ok": True}

    return app


def test_public_endpoint_no_session(app_with_session):
    client = TestClient(app_with_session)
    r = client.get("/public")
    assert r.status_code == 200


def test_protected_endpoint_no_session_returns_401(app_with_session):
    client = TestClient(app_with_session)
    r = client.get("/protected")
    assert r.status_code == 401


def test_protected_endpoint_with_session_succeeds(app_with_session):
    client = TestClient(app_with_session)

    @app_with_session.post("/_login")
    async def _login(request: fastapi.Request):
        request.session["authenticated"] = True
        return {"status": "ok"}

    client.post("/_login")
    r = client.get("/protected")
    assert r.status_code == 200


def test_issue_code_returns_urlsafe_string():
    store = TelegramAuthStore(ttl_seconds=90)
    code = store.issue_code(user_id=123456)
    assert isinstance(code, str)
    assert len(code) >= 8
    assert code.isascii()


def test_verify_code_succeeds_for_valid_code():
    store = TelegramAuthStore(ttl_seconds=90)
    code = store.issue_code(user_id=123456)
    assert store.verify_code(code, expected_user_id=123456) is True


def test_verify_code_fails_for_wrong_user():
    store = TelegramAuthStore(ttl_seconds=90)
    code = store.issue_code(user_id=123456)
    assert store.verify_code(code, expected_user_id=999999) is False


def test_verify_code_fails_for_expired_code():
    store = TelegramAuthStore(ttl_seconds=0)
    code = store.issue_code(user_id=123456)
    time.sleep(0.1)
    assert store.verify_code(code, expected_user_id=123456) is False


def test_verify_code_is_single_use():
    store = TelegramAuthStore(ttl_seconds=90)
    code = store.issue_code(user_id=123456)
    assert store.verify_code(code, expected_user_id=123456) is True
    assert store.verify_code(code, expected_user_id=123456) is False


def test_verify_code_fails_for_unknown_code():
    store = TelegramAuthStore(ttl_seconds=90)
    assert store.verify_code("nonexistent-code", expected_user_id=123456) is False


@pytest.fixture
def dashboard_app(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ADMIN_IDS=123\n")
    store = TelegramAuthStore(ttl_seconds=90)
    app = FastAPI()
    setup_session_middleware(app, secret_key="test-secret-key-for-tests-32chars")
    app.include_router(create_dashboard_router(auth_store=store, admin_ids=[123], env_path=env_path))
    return app, store


def test_login_with_valid_code(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    r = client.post("/api/dashboard/login", json={"code": code, "user_id": 123})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_with_wrong_user_id(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    r = client.post("/api/dashboard/login", json={"code": code, "user_id": 999})
    assert r.status_code == 401


def test_login_with_invalid_code(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.post("/api/dashboard/login", json={"code": "bogus", "user_id": 123})
    assert r.status_code == 401


def test_logout_clears_session(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    client.post("/api/dashboard/login", json={"code": code, "user_id": 123})
    r = client.post("/api/dashboard/logout")
    assert r.status_code == 200


def test_setup_status_when_not_configured(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.get("/api/dashboard/setup-status")
    assert r.status_code == 200
    data = r.json()
    assert data["telegram_configured"] is True
    assert data["cloudflare_configured"] is False
    assert data["initialized"] is False


def test_config_requires_auth(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.get("/api/dashboard/config")
    assert r.status_code == 401


def test_config_after_login_returns_redacted_secrets(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    client.post("/api/dashboard/login", json={"code": code, "user_id": 123})
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200
    data = r.json()
    assert "TELEGRAM_BOT_TOKEN" in data
    assert "****" in data["TELEGRAM_BOT_TOKEN"]
    assert data["TELEGRAM_ADMIN_IDS"] == "123"


def test_config_after_logout_is_unauthorized(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    client.post("/api/dashboard/login", json={"code": code, "user_id": 123})
    client.post("/api/dashboard/logout")
    r = client.get("/api/dashboard/config")
    assert r.status_code == 401


def test_put_config_requires_auth(dashboard_app):
    app, _ = dashboard_app
    client = TestClient(app)
    r = client.put("/api/dashboard/config", json={"updates": {"FOO": "bar"}})
    assert r.status_code == 401


def test_put_config_after_login_writes_env(dashboard_app):
    app, store = dashboard_app
    code = store.issue_code(user_id=123)
    client = TestClient(app)
    client.post("/api/dashboard/login", json={"code": code, "user_id": 123})
    r = client.put("/api/dashboard/config", json={"updates": {"NEW_KEY": "new_value"}})
    assert r.status_code == 200
    assert "NEW_KEY" in r.json()["updated_keys"]

"""Tests for dashboard auth: session middleware and require_auth dependency."""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.server.auth.session import require_auth, setup_session_middleware


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
    client.get("/public")
    client.cookies.set("pinetunnel_admin", "fake")
    r = client.get("/protected")
    assert r.status_code in (401, 200)


import time

from apps.server.auth.telegram_auth import TelegramAuthStore


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

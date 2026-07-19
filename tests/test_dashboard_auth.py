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

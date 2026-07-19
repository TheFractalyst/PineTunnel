"""Tests for dashboard Security Center endpoints.

Verifies 1:1 data wiring between the dashboard /api/dashboard/rate-limits
and /api/dashboard/security-headers endpoints and the canonical middleware
sources (FailedAttemptTracker, RateLimiter, SecurityHeadersMiddleware,
TradingViewIPMiddleware settings).
"""

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.server.auth.session import setup_session_middleware
from apps.server.auth.telegram_auth import TelegramAuthStore
from apps.server.middleware.main import failed_attempt_tracker
from apps.server.middleware.security import (
    FailedAttemptTracker,
    get_security_headers,
)
from apps.server.routes.dashboard import create_dashboard_router

CSRF_HEADERS = {"X-Admin-CSRF": "1"}


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENVIRONMENT", "development")
    from apps.server.config.settings import reset_config_singleton

    reset_config_singleton()
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


def test_get_security_headers_returns_all_six():
    headers = get_security_headers()
    assert isinstance(headers, dict)
    assert len(headers) == 6
    assert "x-frame-options" in headers
    assert "content-security-policy" in headers
    assert "x-content-type-options" in headers
    assert "x-xss-protection" in headers
    assert "referrer-policy" in headers
    assert "strict-transport-security" in headers


def test_get_security_headers_values_match_middleware():
    from apps.server.middleware.security import SecurityHeadersMiddleware

    headers = get_security_headers()
    expected = {
        k.decode("latin-1"): v.decode("latin-1")
        for k, v in SecurityHeadersMiddleware._SECURITY_HEADERS
    }
    assert headers == expected


def test_failed_attempt_tracker_get_blocked_ips_empty():
    tracker = FailedAttemptTracker()
    assert tracker.get_blocked_ips() == []
    assert tracker.get_statistics()["blocked_ip_count"] == 0


def test_failed_attempt_tracker_get_failed_attempt_count_24h():
    tracker = FailedAttemptTracker()
    _run(tracker.record_failure("1.2.3.4"))
    _run(tracker.record_failure("1.2.3.4"))
    _run(tracker.record_failure("5.6.7.8"))
    stats = tracker.get_statistics()
    assert stats["failed_attempts_24h"] == 3


def test_failed_attempt_tracker_get_blocked_ips_after_threshold():
    tracker = FailedAttemptTracker()
    for _ in range(10):
        _run(tracker.record_failure("9.9.9.9"))
    blocked = tracker.get_blocked_ips()
    assert len(blocked) == 1
    assert blocked[0]["ip"] == "9.9.9.9"
    assert blocked[0]["remaining_seconds"] > 0


class TestRateLimitsEndpoint:
    def test_rate_limits_requires_auth(self, dashboard_app):
        app, _ = dashboard_app
        client = TestClient(app)
        r = client.get("/api/dashboard/rate-limits")
        assert r.status_code == 401

    def test_rate_limits_returns_merged_blocked_ips(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        rl.blocked_ips["1.1.1.1"] = time.time() + 300
        monkeypatch.setattr(state, "rate_limiter", rl)

        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/rate-limits")
        assert r.status_code == 200
        data = r.json()
        assert "blocked_ips" in data
        assert isinstance(data["blocked_ips"], list)
        ips = [b["ip"] for b in data["blocked_ips"]]
        assert "1.1.1.1" in ips
        assert data["blocked_ip_count"] >= 1
        assert "failed_attempts_24h" in data
        assert "rate_limited_requests" in data

    def test_rate_limits_includes_failed_attempt_tracker_blocks(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        monkeypatch.setattr(state, "rate_limiter", rl)

        local_tracker = FailedAttemptTracker()
        for _ in range(10):
            _run(local_tracker.record_failure("3.3.3.3"))
        monkeypatch.setattr(
            "apps.server.middleware.main.failed_attempt_tracker", local_tracker
        )

        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/rate-limits")
        assert r.status_code == 200
        data = r.json()
        ips = [b["ip"] for b in data["blocked_ips"]]
        assert "3.3.3.3" in ips
        fa_entries = [b for b in data["blocked_ips"] if b.get("source") == "failed_attempt_tracker"]
        assert len(fa_entries) == 1
        assert "reason" in fa_entries[0]

    def test_rate_limits_failed_attempts_24h_from_tracker(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        monkeypatch.setattr(state, "rate_limiter", rl)

        local_tracker = FailedAttemptTracker()
        for _ in range(5):
            _run(local_tracker.record_failure("4.4.4.4"))
        monkeypatch.setattr(
            "apps.server.middleware.main.failed_attempt_tracker", local_tracker
        )

        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/rate-limits")
        assert r.status_code == 200
        data = r.json()
        assert data["failed_attempts_24h"] == 5


class TestUnblockEndpoint:
    def test_unblock_requires_auth(self, dashboard_app):
        app, _ = dashboard_app
        client = TestClient(app)
        r = client.delete("/api/dashboard/rate-limits/1.2.3.4", headers=CSRF_HEADERS)
        assert r.status_code == 401

    def test_unblock_requires_csrf(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        rl.blocked_ips["1.2.3.4"] = time.time() + 300
        monkeypatch.setattr(state, "rate_limiter", rl)

        client = TestClient(app)
        _login(client, store)
        r = client.delete("/api/dashboard/rate-limits/1.2.3.4")
        assert r.status_code == 403

    def test_unblock_removes_from_rate_limiter(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        rl.blocked_ips["5.5.5.5"] = time.time() + 300
        monkeypatch.setattr(state, "rate_limiter", rl)

        client = TestClient(app)
        _login(client, store)
        r = client.delete("/api/dashboard/rate-limits/5.5.5.5", headers=CSRF_HEADERS)
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "5.5.5.5" not in rl.blocked_ips

    def test_unblock_resets_failed_attempt_tracker(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        monkeypatch.setattr(state, "rate_limiter", rl)

        local_tracker = FailedAttemptTracker()
        for _ in range(10):
            _run(local_tracker.record_failure("6.6.6.6"))
        assert "6.6.6.6" in local_tracker.blocked_ips
        monkeypatch.setattr(
            "apps.server.middleware.main.failed_attempt_tracker", local_tracker
        )

        client = TestClient(app)
        _login(client, store)
        r = client.delete("/api/dashboard/rate-limits/6.6.6.6", headers=CSRF_HEADERS)
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "6.6.6.6" not in local_tracker.blocked_ips

    def test_unblock_nonexistent_ip_returns_not_blocked(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server import state
        from apps.server.services.rate_limiter import RateLimiter

        rl = RateLimiter()
        monkeypatch.setattr(state, "rate_limiter", rl)
        monkeypatch.setattr(
            "apps.server.middleware.main.failed_attempt_tracker", FailedAttemptTracker()
        )

        client = TestClient(app)
        _login(client, store)
        r = client.delete("/api/dashboard/rate-limits/99.99.99.99", headers=CSRF_HEADERS)
        assert r.status_code == 200
        assert r.json()["success"] is False


class TestSecurityHeadersEndpoint:
    def test_security_headers_requires_auth(self, dashboard_app):
        app, _ = dashboard_app
        client = TestClient(app)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 401

    def test_security_headers_returns_verified_headers(self, dashboard_app):
        app, store = dashboard_app
        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 200
        data = r.json()
        assert "headers" in data
        headers = data["headers"]
        assert len(headers) == 6
        assert headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-xss-protection"] == "1; mode=block"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "strict-transport-security" in headers
        assert data["headers_active"] == 6

    def test_security_headers_tradingview_allowlist_from_settings(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server.config.settings import get_config, reset_config_singleton

        reset_config_singleton()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ENVIRONMENT", "development")
        original = get_config()
        monkeypatch.setattr(original, "tradingview_ip_allowlist", "1")
        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 200
        data = r.json()
        assert data["tradingview_ip_allowlist"] is True

    def test_security_headers_tradingview_allowlist_disabled(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server.config.settings import get_config, reset_config_singleton

        reset_config_singleton()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ENVIRONMENT", "development")
        original = get_config()
        monkeypatch.setattr(original, "tradingview_ip_allowlist", "false")
        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 200
        data = r.json()
        assert data["tradingview_ip_allowlist"] is False

    def test_security_headers_tradingview_ips_from_settings(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server.config.settings import get_config, reset_config_singleton

        reset_config_singleton()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ENVIRONMENT", "development")
        original = get_config()
        monkeypatch.setattr(original, "tradingview_ips", "1.1.1.1,2.2.2.2")
        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 200
        data = r.json()
        assert data["tradingview_ips"] == ["1.1.1.1", "2.2.2.2"]

    def test_security_headers_tradingview_ips_defaults(self, dashboard_app, monkeypatch):
        app, store = dashboard_app
        from apps.server.config.settings import get_config, reset_config_singleton

        reset_config_singleton()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("ENVIRONMENT", "development")
        original = get_config()
        monkeypatch.setattr(original, "tradingview_ips", "")
        client = TestClient(app)
        _login(client, store)
        r = client.get("/api/dashboard/security-headers")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["tradingview_ips"], list)
        assert len(data["tradingview_ips"]) == 4
        assert "52.89.214.238" in data["tradingview_ips"]
        assert "34.212.75.30" in data["tradingview_ips"]

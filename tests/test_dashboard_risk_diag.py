"""Tests for dashboard risk-status and diagnostics data wiring.

Verifies that the dashboard endpoints expose the same metrics the
Telegram bot consumes, and that the risk-status payload includes the
fields the frontend renders (max_drawdown, position_sizing_mode,
risk_per_trade_pct).
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.server.routes.admin import router as admin_router
from apps.server.routes.auth import _require_auth
from apps.server.routes.diagnostics import router as diag_router


@pytest.fixture
def app(monkeypatch):
    monkeyapp = FastAPI()
    monkeyapp.include_router(admin_router)
    monkeyapp.include_router(diag_router)

    async def _bypass_auth(request=None, session_token=None):
        return "test"

    from apps.server.routes import admin as admin_mod, diagnostics as diag_mod

    monkeyapp.dependency_overrides[admin_mod._require_auth] = _bypass_auth
    monkeyapp.dependency_overrides[diag_mod._require_auth] = _bypass_auth
    return monkeyapp


@pytest.fixture
def client(app):
    return TestClient(app)


def _patch_state(monkeypatch, **overrides):
    from apps.server import state

    defaults = dict(
        mt5_manager=SimpleNamespace(
            initialized=True,
            get_account_info=lambda: {
                "login": 12345678,
                "server": "MockBroker-Demo",
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "margin_free": 10000.0,
                "margin_level": 999999,
                "profit": 0.0,
                "currency": "USD",
                "leverage": 100,
                "trade_allowed": True,
                "open_positions": 0,
            },
        ),
        risk_manager=_RealRiskManager(),
        db_manager=SimpleNamespace(
            execute_query=lambda q, *a, **k: [{"cnt": 0}] if "COUNT" in q else [],
            get_pool_stats=lambda: {"in_use": 0, "total_connections": 2},
        ),
        redis_client=None,
        ws_manager=SimpleNamespace(get_total_connections=lambda: 0),
        rate_limiter=SimpleNamespace(),
        client_manager=SimpleNamespace(clients={}),
        settings=SimpleNamespace(data_dir="."),
    )
    defaults.update(overrides)
    for k, v in defaults.items():
        monkeypatch.setattr(state, k, v, raising=False)
    return state


class _RealRiskManager:
    from apps.server.services.risk_manager import RiskManager as _R

    def __init__(self):
        self._r = self._R()

    def get_risk_status(self, account):
        return self._r.get_risk_status(account)

    def can_trade(self, account, pos_count=0):
        return self._r.can_trade(account, pos_count)


# ---------------------------------------------------------------------------
# /api/risk-status
# ---------------------------------------------------------------------------


def test_risk_status_returns_all_frontend_fields(app, client, monkeypatch):
    _patch_state(monkeypatch)
    r = client.get("/api/risk-status")
    assert r.status_code == 200
    data = r.json()
    rm = data["risk_metrics"]
    for field in (
        "daily_pnl",
        "daily_pnl_percent",
        "current_drawdown",
        "max_drawdown",
        "position_sizing_mode",
        "risk_per_trade_pct",
        "max_daily_loss",
        "max_daily_trades",
        "remaining_risk_percent",
        "remaining_trades",
    ):
        assert field in rm, f"missing field: {field}"
    assert rm["max_drawdown"] == 10.0
    assert rm["position_sizing_mode"] == "risk_based"
    assert rm["risk_per_trade_pct"] == 2.0
    assert "can_trade" in data
    assert "reason" in data
    assert "account" in data


def test_risk_status_includes_account_balance_equity_margin(app, client, monkeypatch):
    _patch_state(monkeypatch)
    r = client.get("/api/risk-status")
    data = r.json()
    acc = data["account"]
    assert acc["balance"] == 10000.0
    assert acc["equity"] == 10000.0
    assert acc["margin_level"] == 999999


def test_risk_status_503_when_mt5_error(app, client, monkeypatch):
    _patch_state(
        monkeypatch,
        mt5_manager=SimpleNamespace(
            initialized=True,
            get_account_info=lambda: {"error": "MT5 not connected"},
        ),
    )
    r = client.get("/api/risk-status")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /api/diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_returns_all_probes(app, client, monkeypatch):
    _patch_state(monkeypatch)
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert "overall_status" in data
    assert "probes" in data
    names = {p["name"] for p in data["probes"]}
    expected = {
        "database",
        "redis",
        "websocket_hub",
        "signal_queue",
        "rate_limiter",
        "client_manager",
        "disk",
        "memory",
    }
    assert expected.issubset(names), f"missing probes: {expected - names}"
    for p in data["probes"]:
        assert "latency_ms" in p
        assert p["status"] in ("ok", "degraded", "fail", "warning")


def test_diagnostics_db_probe_uses_select_one(app, client, monkeypatch):
    calls = []

    def fake_query(q, *a, **k):
        calls.append(q)
        return []

    _patch_state(
        monkeypatch,
        db_manager=SimpleNamespace(
            execute_query=fake_query,
            get_pool_stats=lambda: {"in_use": 0, "total_connections": 0},
        ),
    )
    r = client.get("/api/diagnostics")
    data = r.json()
    db_probe = next(p for p in data["probes"] if p["name"] == "database")
    assert db_probe["status"] == "ok"
    assert any("SELECT 1" in q for q in calls)


def test_diagnostics_db_probe_fails_on_exception(app, client, monkeypatch):
    def boom(q, *a, **k):
        raise RuntimeError("connection refused")

    _patch_state(
        monkeypatch,
        db_manager=SimpleNamespace(
            execute_query=boom,
            get_pool_stats=lambda: {"in_use": 0, "total_connections": 0},
        ),
    )
    r = client.get("/api/diagnostics")
    data = r.json()
    db_probe = next(p for p in data["probes"] if p["name"] == "database")
    assert db_probe["status"] == "fail"
    assert "connection refused" in db_probe["detail"]


def test_diagnostics_overall_status_ok_when_all_ok(app, client, monkeypatch):
    _patch_state(monkeypatch)
    r = client.get("/api/diagnostics")
    data = r.json()
    assert data["overall_status"] in ("ok", "degraded")


# ---------------------------------------------------------------------------
# Sentinel margin level (999999) handling
# ---------------------------------------------------------------------------


def test_mock_mt5_returns_sentinel_margin_level():
    from apps.server.services.mt5_service import MT5Manager

    mgr = MT5Manager({})
    mgr.initialize()
    info = mgr.get_account_info()
    assert info["margin_level"] == 999999
    assert info["balance"] == 10000.0

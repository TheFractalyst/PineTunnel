"""Tests for the signal validation layer (apps/server/webhook/validator.py).

Tests cover:
- Valid signals that should pass
- SL/TP direction validation (buy SL<TP, sell SL>TP)
- Volume bounds (lots, risk)
- Symbol format validation
- Pending order entry price requirement
- SL/TP distance sanity checks
"""

import pytest

from apps.server.webhook.validator import validate_signal, ValidationResult


class TestValidationPass:
    """Signals that should pass validation."""

    def test_valid_buy(self, buy_signal_dict):
        result = validate_signal(buy_signal_dict)
        assert result.valid

    def test_valid_sell(self, sell_signal_dict):
        result = validate_signal(sell_signal_dict)
        assert result.valid

    def test_valid_close_long(self):
        result = validate_signal({"action": "close_long", "symbol": "EURUSD"})
        assert result.valid

    def test_valid_buy_stop_with_pending(self):
        result = validate_signal({
            "action": "buy_stop",
            "symbol": "EURUSD",
            "pending": 1.0900,
            "sl": 1.0850,
            "tp": 1.0950,
        })
        assert result.valid

    def test_valid_ea_on(self):
        result = validate_signal({"action": "ea_on", "symbol": "ea_on"})
        assert result.valid


class TestValidationSLTPDirection:
    """SL/TP direction checks."""

    def test_buy_sl_above_tp_rejected(self):
        result = validate_signal({
            "action": "buy",
            "symbol": "EURUSD",
            "lots": 0.10,
            "sl": 1.0950,
            "tp": 1.0850,
        })
        assert not result.valid
        assert "SL" in result.reason

    def test_sell_sl_below_tp_rejected(self):
        result = validate_signal({
            "action": "sell",
            "symbol": "EURUSD",
            "lots": 0.10,
            "sl": 1.0850,
            "tp": 1.0950,
        })
        assert not result.valid
        assert "SL" in result.reason

    def test_buy_with_pending_sl_above_entry_rejected(self):
        result = validate_signal({
            "action": "buy_stop",
            "symbol": "EURUSD",
            "pending": 1.0900,
            "sl": 1.1000,
            "tp": 1.0950,
        })
        assert not result.valid

    def test_sell_with_pending_sl_below_entry_rejected(self):
        result = validate_signal({
            "action": "sell_stop",
            "symbol": "EURUSD",
            "pending": 1.0900,
            "sl": 1.0800,
            "tp": 1.0850,
        })
        assert not result.valid


class TestValidationVolume:
    """Volume and risk bounds."""

    def test_negative_lots_rejected(self):
        result = validate_signal({
            "action": "buy",
            "symbol": "EURUSD",
            "lots": -0.10,
        })
        assert not result.valid

    def test_excessive_lots_rejected(self):
        result = validate_signal({
            "action": "buy",
            "symbol": "EURUSD",
            "lots": 99999,
        })
        assert not result.valid

    def test_excessive_risk_rejected(self):
        result = validate_signal({
            "action": "buy",
            "symbol": "EURUSD",
            "risk": 150,
        })
        assert not result.valid


class TestValidationSymbol:
    """Symbol format validation."""

    def test_invalid_symbol_rejected(self):
        result = validate_signal({
            "action": "buy",
            "symbol": "!!!invalid!!!",
        })
        assert not result.valid

    def test_missing_symbol_rejected(self):
        result = validate_signal({"action": "buy", "symbol": ""})
        assert not result.valid

    def test_missing_action_rejected(self):
        result = validate_signal({"action": "", "symbol": "EURUSD"})
        assert not result.valid


class TestValidationPendingOrders:
    """Pending order specific validation."""

    def test_pending_without_entry_price_rejected(self):
        result = validate_signal({
            "action": "buy_stop",
            "symbol": "EURUSD",
            "lots": 0.10,
        })
        assert not result.valid
        assert "pending" in result.reason.lower()

    def test_negative_pending_price_rejected(self):
        result = validate_signal({
            "action": "buy_stop",
            "symbol": "EURUSD",
            "pending": -1.0,
        })
        assert not result.valid


class TestValidationResult:
    """ValidationResult class behavior."""

    def test_valid_result_is_truthy(self):
        r = ValidationResult(True)
        assert bool(r)

    def test_invalid_result_is_falsy(self):
        r = ValidationResult(False, "test reason")
        assert not bool(r)

    def test_result_stores_reason(self):
        r = ValidationResult(False, "my reason")
        assert r.reason == "my reason"

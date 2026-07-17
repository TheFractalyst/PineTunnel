"""Tests for the CSV signal parser (apps/server/webhook/parser.py).

Tests cover:
- All command types (buy, sell, close, pending, SLTP, cancel, EA management)
- Parameter parsing (lots, sl, tp, pending, comment, secret)
- Edge cases (empty input, missing fields, invalid commands)
- Signal dict output format
"""

import pytest

from apps.server.webhook.parser import PineTunnelParser, CommandType


class TestParserBasicCommands:
    """Test basic market order parsing."""

    def test_parse_buy(self, parser, valid_buy_csv):
        signal = parser.parse(valid_buy_csv)
        assert signal is not None
        assert signal.command == CommandType.BUY
        assert signal.symbol == "EURUSD"

    def test_parse_sell(self, parser, valid_sell_csv):
        signal = parser.parse(valid_sell_csv)
        assert signal is not None
        assert signal.command == CommandType.SELL
        assert signal.symbol == "GBPUSD"

    def test_parse_close_long(self, parser):
        signal = parser.parse("TESTKEY,close_long,EURUSD,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CLOSE_LONG

    def test_parse_close_short(self, parser):
        signal = parser.parse("TESTKEY,close_short,EURUSD,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CLOSE_SHORT

    def test_parse_close_all(self, parser):
        signal = parser.parse("TESTKEY,close_all,EURUSD,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CLOSE_ALL


class TestParserPendingOrders:
    """Test pending order parsing."""

    def test_parse_buy_stop(self, parser):
        signal = parser.parse("TESTKEY,buy_stop,EURUSD,pending=1.0900,lots=0.10,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.BUY_STOP

    def test_parse_buy_limit(self, parser):
        signal = parser.parse("TESTKEY,buy_limit,EURUSD,pending=1.0800,lots=0.10,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.BUY_LIMIT

    def test_parse_sell_stop(self, parser):
        signal = parser.parse("TESTKEY,sell_stop,GBPUSD,pending=1.2700,lots=0.05,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.SELL_STOP

    def test_parse_sell_limit(self, parser):
        signal = parser.parse("TESTKEY,sell_limit,GBPUSD,pending=1.2900,lots=0.05,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.SELL_LIMIT


class TestParserSLTP:
    """Test SL/TP modification commands."""

    def test_parse_sltp_long(self, parser):
        signal = parser.parse("TESTKEY,sltp_long,EURUSD,sl=1.0800,tp=1.1000,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.SLTP_LONG

    def test_parse_sltp_short(self, parser):
        signal = parser.parse("TESTKEY,sltp_short,EURUSD,sl=1.1000,tp=1.0800,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.SLTP_SHORT


class TestParserCancelCommands:
    """Test cancel commands."""

    def test_parse_cancel_long(self, parser):
        signal = parser.parse("TESTKEY,cancel_long,EURUSD,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CANCEL_LONG

    def test_parse_cancel_short(self, parser):
        signal = parser.parse("TESTKEY,cancel_short,EURUSD,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CANCEL_SHORT


class TestParserEAManagement:
    """Test EA management commands."""

    def test_parse_ea_on(self, parser):
        signal = parser.parse("TESTKEY,ea_on,ea_on,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.EA_ON

    def test_parse_ea_off(self, parser):
        signal = parser.parse("TESTKEY,ea_off,ea_off,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.EA_OFF

    def test_parse_close_all_off(self, parser):
        signal = parser.parse("TESTKEY,close_all_off,close_all_off,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.command == CommandType.CLOSE_ALL_OFF


class TestParserParameters:
    """Test parameter extraction."""

    def test_parse_lots(self, parser):
        signal = parser.parse("TESTKEY,buy,EURUSD,lots=0.50,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.lots == 0.50

    def test_parse_sl_tp(self, parser):
        signal = parser.parse("TESTKEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.sl == 1.085
        assert signal.tp == 1.095

    def test_parse_comment(self, parser):
        signal = parser.parse("TESTKEY,buy,EURUSD,lots=0.10,comment=myTrade,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.comment == "myTrade"

    def test_parse_pending_price(self, parser):
        signal = parser.parse("TESTKEY,buy_stop,EURUSD,pending=1.0900,lots=0.10,secret=test-webhook-secret-min-32-chars")
        assert signal is not None
        assert signal.pending == 1.090


class TestParserEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_input(self, parser):
        signal = parser.parse("")
        assert signal is None

    def test_single_field(self, parser):
        signal = parser.parse("TESTKEY")
        assert signal is None

    def test_missing_symbol(self, parser):
        signal = parser.parse("TESTKEY,buy,secret=test-webhook-secret-min-32-chars")
        assert signal is None

    def test_missing_secret(self, parser):
        signal = parser.parse("TESTKEY,buy,EURUSD,lots=0.10")
        assert signal is not None
        assert signal.secret is None

    def test_to_dict_format(self, parser, valid_buy_csv):
        signal = parser.parse(valid_buy_csv)
        assert signal is not None
        d = signal.to_dict()
        assert isinstance(d, dict)
        assert "action" in d or "command" in d
        assert "symbol" in d

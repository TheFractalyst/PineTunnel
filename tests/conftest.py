"""Pytest configuration and fixtures for PineTunnel tests."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set test environment variables before importing app modules
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret-min-32-chars")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-min-32-chars-aaaaaa")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-api-key-min-32-chars")
os.environ.setdefault("SERVER_BASE_URL", "http://127.0.0.1:8000")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from apps.server.webhook.parser import PineTunnelParser
from apps.server.webhook.validator import validate_signal, ValidationResult


@pytest.fixture
def parser():
    """Parser instance for signal parsing tests."""
    return PineTunnelParser()


@pytest.fixture
def valid_buy_csv():
    """Valid buy signal in CSV format."""
    return "TESTKEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,comment=test,secret=test-webhook-secret-min-32-chars"


@pytest.fixture
def valid_sell_csv():
    """Valid sell signal in CSV format."""
    return "TESTKEY,sell,GBPUSD,lots=0.05,sl=1.2750,tp=1.2850,secret=test-webhook-secret-min-32-chars"


@pytest.fixture
def buy_signal_dict():
    """Parsed buy signal as dict (for validator tests)."""
    return {
        "action": "buy",
        "symbol": "EURUSD",
        "lots": 0.10,
        "sl": 1.0850,
        "tp": 1.0950,
        "secret": "test-webhook-secret-min-32-chars",
    }


@pytest.fixture
def sell_signal_dict():
    """Parsed sell signal as dict (for validator tests)."""
    return {
        "action": "sell",
        "symbol": "GBPUSD",
        "lots": 0.05,
        "sl": 1.2850,
        "tp": 1.2750,
        "secret": "test-webhook-secret-min-32-chars",
    }

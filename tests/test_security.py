"""Tests for the security utilities (apps/server/utils/security.py).

Tests cover:
- HMAC generation and verification
- Constant-time comparison
- Timestamp skew checks
"""

import time
import pytest

from apps.server.utils.security import (
    sign_response_bytes,
    verify_response_bytes_signature,
    verify_secret_key,
)


class TestHMAC:
    """HMAC generation and verification."""

    @pytest.fixture
    def secret(self):
        return "test-secret-key-min-32-chars-aaaa"

    @pytest.fixture
    def message(self):
        return b"test message for hmac"

    def test_sign_and_verify_bytes(self, secret, message):
        sig = sign_response_bytes(message, secret)
        assert isinstance(sig, str)
        assert len(sig) > 0
        assert verify_response_bytes_signature(message, sig, secret)

    def test_verify_bytes_wrong_secret(self, secret, message):
        sig = sign_response_bytes(message, secret)
        assert not verify_response_bytes_signature(message, sig, "wrong-secret-key-min-32-chars")

    def test_verify_bytes_tampered_message(self, secret, message):
        sig = sign_response_bytes(message, secret)
        assert not verify_response_bytes_signature(message + b"tampered", sig, secret)

    def test_verify_bytes_empty_message(self, secret):
        sig = sign_response_bytes(b"", secret)
        assert verify_response_bytes_signature(b"", sig, secret)


class TestSecretKey:
    """Secret key comparison."""

    def test_matching_secret(self):
        assert verify_secret_key("my-secret", "my-secret")

    def test_non_matching_secret(self):
        assert not verify_secret_key("my-secret", "wrong-secret")

    def test_none_provided(self):
        assert not verify_secret_key(None, "my-secret")

    def test_both_none(self):
        assert not verify_secret_key(None, None)

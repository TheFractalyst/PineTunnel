"""Tests for RC4 signal encryption/decryption (apps/server/crypto/signal_crypto.py).

Tests cover:
- RC4 encrypt/decrypt round-trip (Python encrypt, Python decrypt)
- Checksum verification (tampering detection)
- Wrong key rejection
- Plaintext auto-detection
- Format parsing
"""

import pytest

from apps.server.crypto.signal_crypto import (
    _rc4_ksa,
    _rc4_prga,
    _xor_fold,
    _try_rc4,
    is_encrypted_message,
    is_encryption_configured,
    parse_rc4_payload,
    decrypt_rc4_signal,
    generate_encryption_key,
    _RC4_DROP_BYTES,
)


# Test key (64-char hex = 32 bytes)
TEST_KEY_HEX = "a" * 64
TEST_KEY = bytes.fromhex(TEST_KEY_HEX)


def _rc4_encrypt(key: bytes, nonce: bytes, plaintext: str) -> bytes:
    """Encrypt plaintext with RC4-drop256 (matches PineScript _rc4Encrypt)."""
    combined = key + nonce
    s = _rc4_ksa(combined)
    _rc4_prga(s, _RC4_DROP_BYTES)
    pt_bytes = plaintext.encode("ascii")
    keystream = _rc4_prga(s, len(pt_bytes))
    return bytes(a ^ b for a, b in zip(pt_bytes, keystream))


class TestRC4RoundTrip:
    """RC4 encrypt then decrypt should recover original plaintext."""

    def test_basic_round_trip(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        plaintext = "TESTKEY,buy,EURUSD,lots=0.10,sl=1.0850,tp=1.0950,secret=mysecret"
        nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"

        ct = _rc4_encrypt(TEST_KEY, nonce, plaintext)
        checksum = _xor_fold(ct, 4)

        nonce_hex = nonce.hex()
        ct_hex = ct.hex()
        checksum_hex = checksum.hex()

        result = decrypt_rc4_signal(nonce_hex, ct_hex, checksum_hex)
        assert result == plaintext

    def test_empty_plaintext(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        # Empty ciphertext should fail (not empty plaintext)
        result = decrypt_rc4_signal("0102030405060708", "", "00000000")
        assert result is None

    def test_long_signal(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        plaintext = "MYKEY,buy,EURUSD,lots=0.50,sl=1.0850,tp=1.0950,comment=very-long-comment-string-here,secret=mysecret"
        nonce = b"\xab\xcd\xef\x01\x23\x45\x67\x89"

        ct = _rc4_encrypt(TEST_KEY, nonce, plaintext)
        checksum = _xor_fold(ct, 4)

        result = decrypt_rc4_signal(nonce.hex(), ct.hex(), checksum.hex())
        assert result == plaintext


class TestRC4Checksum:
    """Checksum verification detects tampering."""

    def test_tampered_ciphertext(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        plaintext = "TESTKEY,buy,EURUSD,secret=mypass"
        nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"

        ct = _rc4_encrypt(TEST_KEY, nonce, plaintext)
        checksum = _xor_fold(ct, 4)

        # Tamper with ciphertext
        tampered_ct = bytearray(ct)
        tampered_ct[0] ^= 0xFF
        tampered_ct = bytes(tampered_ct)

        result = decrypt_rc4_signal(nonce.hex(), tampered_ct.hex(), checksum.hex())
        assert result is None  # Checksum mismatch -> rejected

    def test_tampered_checksum(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        plaintext = "TESTKEY,buy,EURUSD,secret=mypass"
        nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"

        ct = _rc4_encrypt(TEST_KEY, nonce, plaintext)
        bad_checksum = b"\x00\x00\x00\x00"  # Wrong checksum

        result = decrypt_rc4_signal(nonce.hex(), ct.hex(), bad_checksum.hex())
        assert result is None


class TestRC4WrongKey:
    """Wrong key should fail to decrypt."""

    def test_wrong_key(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        wrong_key = bytes.fromhex("b" * 64)
        plaintext = "TESTKEY,buy,EURUSD,secret=mypass"
        nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"

        ct = _rc4_encrypt(wrong_key, nonce, plaintext)
        checksum = _xor_fold(ct, 4)

        result = decrypt_rc4_signal(nonce.hex(), ct.hex(), checksum.hex())
        assert result is None  # Wrong key -> non-ASCII -> rejected


class TestMessageDetection:
    """Auto-detection of encrypted vs plaintext messages."""

    def test_encrypted_message(self):
        assert is_encrypted_message("RC4,0102030405060708:abcdef:12345678")

    def test_plaintext_message(self):
        assert not is_encrypted_message("TESTKEY,buy,EURUSD,lots=0.10,secret=mypass")

    def test_empty_message(self):
        assert not is_encrypted_message("")

    def test_case_insensitive(self):
        assert is_encrypted_message("rc4,0102030405060708:abcdef:12345678")
        assert is_encrypted_message("Rc4,0102030405060708:abcdef:12345678")


class TestEncryptionConfigured:
    """Check if encryption key is configured."""

    def test_key_configured(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", TEST_KEY_HEX)
        assert is_encryption_configured()

    def test_key_not_configured(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_ENCRYPTION_KEY", raising=False)
        assert not is_encryption_configured()

    def test_key_wrong_length(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_ENCRYPTION_KEY", "abc123")
        assert not is_encryption_configured()


class TestPayloadParsing:
    """RC4 payload format parsing."""

    def test_valid_payload(self):
        result = parse_rc4_payload("0102030405060708:abcdef1234:12345678")
        assert result is not None
        assert result[0] == "0102030405060708"
        assert result[1] == "abcdef1234"
        assert result[2] == "12345678"

    def test_missing_parts(self):
        assert parse_rc4_payload("0102030405060708:abcdef") is None

    def test_wrong_nonce_length(self):
        assert parse_rc4_payload("0102:abcdef:12345678") is None

    def test_wrong_checksum_length(self):
        assert parse_rc4_payload("0102030405060708:abcdef:1234") is None


class TestKeyGeneration:
    """Encryption key generation."""

    def test_generates_64_chars(self):
        key = generate_encryption_key()
        assert len(key) == 64

    def test_generates_hex(self):
        key = generate_encryption_key()
        assert all(c in "0123456789abcdef" for c in key)

    def test_generates_unique(self):
        key1 = generate_encryption_key()
        key2 = generate_encryption_key()
        assert key1 != key2

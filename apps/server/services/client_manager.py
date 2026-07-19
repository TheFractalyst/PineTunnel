"""Client Manager - manages client licenses with JSON-backed persistence.

Simple JSON file storage for client/license data. Each client has a
license_key, secret_key, status, and optional metadata.
"""

import json
import logging
import os
import secrets as _secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


def generate_license_key() -> str:
    """Generate a 24-char hex license key."""
    return _secrets.token_hex(12)


def generate_secret_key() -> str:
    """Generate a 24-char hex secret key."""
    return _secrets.token_hex(12)


class ClientManager:
    """Manages client licenses with JSON file persistence."""

    def __init__(
        self,
        license_file: str | None = None,
        data_dir: str | None = None,
        redis_cache: Any | None = None,
    ) -> None:
        if not data_dir:
            data_dir = "/data" if Path("/data").exists() else str(Path.cwd())

        self._data_dir = data_dir
        self._redis_cache = redis_cache

        # Determine JSON file path
        if license_file and os.path.exists(license_file):
            self._json_path = license_file
        else:
            self._json_path = os.path.join(data_dir, "licenses.json")

        self._cache: dict[str, dict[str, Any]] = {}
        self._load_from_file()

        logger.info("ClientManager initialized: %d licenses, file at %s", len(self._cache), self._json_path)

    def _load_from_file(self) -> None:
        """Load clients from JSON file."""
        if os.path.exists(self._json_path):
            try:
                with open(self._json_path, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
                    logger.info("Loaded %d licenses from %s", len(data), self._json_path)
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load licenses from %s: %s", self._json_path, e)
                self._cache = {}

    def _save_to_file(self) -> bool:
        """Save clients to JSON file."""
        try:
            os.makedirs(os.path.dirname(self._json_path) or ".", exist_ok=True)
            with open(self._json_path, "w") as f:
                json.dump(self._cache, f, indent=2)
            return True
        except (OSError, TypeError) as e:
            logger.error("Failed to save licenses to %s: %s", self._json_path, e)
            return False

    @property
    def clients(self) -> dict[str, dict[str, Any]]:
        return self._cache

    @clients.setter
    def clients(self, value: dict[str, Any]) -> None:
        """Replace entire license set."""
        self._cache = value
        self._save_to_file()

    def get_client_by_license(self, license_key: str) -> dict[str, Any] | None:
        """Get client by license key."""
        return self._cache.get(license_key)

    def validate_license(self, license_key: str) -> tuple[bool, str]:
        """Validate license key with expiration check.

        Checks:
        1. License exists
        2. Status is active
        3. expires_at not passed (if set)
        """
        client = self.get_client_by_license(license_key)
        if not client:
            return False, "Invalid license key"

        if client.get("status") != "active":
            return False, "License is not active"

        expires_at_str = client.get("expires_at")
        if expires_at_str:
            try:
                expiry_dt = date_parser.parse(expires_at_str)
                if datetime.now() > expiry_dt:
                    logger.warning("License %s expired on %s", license_key, expiry_dt.isoformat())
                    return False, f"License expired on {expiry_dt.isoformat()}"
            except Exception as e:
                logger.error("Error parsing license expires_at for %s: %s", license_key, e)

        return True, "License valid"

    def validate_api_key(self, api_key: str) -> tuple[bool, str | None, str]:
        """Validate API key (license key) and return client ID.

        Returns:
            Tuple of (is_valid, client_id, error_message).
        """
        client = self.get_client_by_license(api_key)
        if not client:
            return False, None, "Invalid license key"

        if client.get("status") != "active":
            return False, None, "License is not active"

        expires_at_str = client.get("expires_at")
        if expires_at_str:
            try:
                expiry_dt = date_parser.parse(expires_at_str)
                if datetime.now() > expiry_dt:
                    return False, None, "License has expired"
            except Exception as e:
                logger.error("Error parsing expires_at for %s: %s", api_key, e)

        return True, api_key, ""

    def is_symbol_allowed(self, client_id: str, symbol: str) -> bool:
        """Check if symbol is allowed for client."""
        client = self.get_client_by_license(client_id)
        if not client:
            return False

        features = client.get("features", [])
        if "unlimited_symbols" in features:
            return True

        allowed_symbols = client.get("allowed_symbols", [])
        if allowed_symbols and symbol not in allowed_symbols:
            return False

        return True

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        """Get client by ID (alias for get_client_by_license)."""
        return self.get_client_by_license(client_id)

    def get_all_clients(self) -> dict[str, Any]:
        """Get all clients."""
        return self._cache

    _SANITIZE_KEYS = frozenset({"secret_key", "webhook_secret"})

    def export_sanitized(self) -> dict[str, Any]:
        """Export all clients with sensitive fields stripped."""
        sanitized = {}
        for key, client in self._cache.items():
            entry = {k: v for k, v in client.items() if k not in self._SANITIZE_KEYS}
            sanitized[key] = entry
        return sanitized

    def add_client(self, license_key: str, client_data: dict[str, Any]) -> bool:
        """Add a new client license."""
        try:
            self._cache[license_key] = client_data
            return self._save_to_file()
        except (TypeError, KeyError) as e:
            logger.error("Failed to add client %s: %s: %s", license_key, type(e).__name__, e)
            return False

    def update_client(self, license_key: str, **fields: Any) -> bool:
        """Update fields on an existing client. Returns False if not found."""
        client = self._cache.get(license_key)
        if client is None:
            return False
        for k, v in fields.items():
            if v is not None:
                client[k] = v
        return self._save_to_file()

    def remove_client(self, license_key: str) -> bool:
        """Remove a client license. Returns False if not found."""
        if license_key not in self._cache:
            return False
        del self._cache[license_key]
        return self._save_to_file()

    def extend_client(self, license_key: str, days: int) -> str | None:
        """Extend a client's expires_at by N days. Returns new expires_at or None."""
        client = self._cache.get(license_key)
        if client is None:
            return None
        current = client.get("expires_at")
        base = datetime.now()
        if current:
            try:
                base = date_parser.parse(current)
            except Exception:
                base = datetime.now()
        new_expiry = base + timedelta(days=days)
        new_iso = new_expiry.isoformat()
        client["expires_at"] = new_iso
        self._save_to_file()
        return new_iso

    def set_status(self, license_key: str, status: str, enabled: bool | None = None) -> bool:
        """Set a client's status (and optionally enabled flag). Returns False if not found."""
        client = self._cache.get(license_key)
        if client is None:
            return False
        client["status"] = status
        if enabled is not None:
            client["enabled"] = enabled
        return self._save_to_file()

    def save_clients(self) -> bool:
        """Persist clients to file."""
        return self._save_to_file()

    def refresh_cache(self) -> None:
        """Reload the in-memory cache from file."""
        self._load_from_file()

    def generate_tokens(self, user_id: int, expires_at: str | None) -> tuple[str, str]:
        """Generate license_id and secret_key tokens.

        Args:
            user_id: Integer user identifier.
            expires_at: ISO 8601 expiry string, or None for lifetime.

        Returns:
            (license_token, secret_token) - 24-char hex strings.
        """
        license_token = generate_license_key()
        secret_token = generate_secret_key()
        return license_token, secret_token

    @property
    def store(self):
        """Compatibility property - returns self for code that accesses store."""
        return self

    def get_connection(self):
        return None

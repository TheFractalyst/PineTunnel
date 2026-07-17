"""Admin activity logger — silent monitoring of admin actions."""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

from apps.server.utils import mask_string as _mask

logger = logging.getLogger(__name__)

_DB_PERMISSIONS = 0o600

_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL,
        user TEXT,
        ip_address TEXT,
        details TEXT
    )
"""


class AdminLogger:
    """Logs admin activities for monitoring."""

    def __init__(self, db_path: str = "admin_activity.db") -> None:
        self.db_path = db_path
        self.init_database()

    def init_database(self) -> None:
        """Initialize database tables."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute(_CREATE_TABLE_SQL)
            conn.commit()
            conn.close()
            os.chmod(self.db_path, _DB_PERMISSIONS)
            logger.info("Admin logger database initialized: %s", self.db_path)
        except (sqlite3.OperationalError, sqlite3.IntegrityError, OSError, PermissionError) as e:
            logger.error(
                "Failed to initialize admin logger database at %s in init_database: %s: %s",
                self.db_path,
                type(e).__name__,
                e,
                extra={"context": {"db_path": self.db_path, "operation": "init_database"}},
            )

    def log_activity(
        self,
        action: str,
        user: str | None = None,
        ip_address: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log admin activity."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO admin_logs (timestamp, action, user, ip_address, details) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now().isoformat(),
                    action,
                    user,
                    ip_address,
                    json.dumps(details) if details else None,
                ),
            )
            conn.commit()
            conn.close()
        except (
            sqlite3.OperationalError,
            sqlite3.IntegrityError,
            OSError,
            PermissionError,
            TypeError,
        ) as e:
            logger.error(
                "Failed to log admin activity in log_activity: %s: %s",
                type(e).__name__,
                e,
                extra={
                    "context": {
                        "action": action,
                        "user": user,
                        "db_path": self.db_path,
                        "operation": "log_activity",
                    }
                },
            )

    def log_webhook(self, license_key: str, message: str, ip_address: str | None = None) -> None:
        """Log webhook activity."""
        self.log_activity(
            action="webhook",
            user=_mask(license_key),
            ip_address=ip_address,
            details={"message": message},
        )

    def get_client_stats(self, client_id: str) -> dict[str, Any]:
        """Get statistics for a specific client."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()

            cursor.execute(
                "SELECT COUNT(*) FROM admin_logs WHERE user = ? AND action = 'webhook'",
                (client_id,),
            )
            webhook_count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT timestamp FROM admin_logs WHERE user = ? ORDER BY id DESC LIMIT 1",
                (client_id,),
            )
            last_activity = cursor.fetchone()

            conn.close()

            return {
                "webhooks": webhook_count,
                "last_activity": last_activity[0] if last_activity else None,
            }
        except (sqlite3.OperationalError, sqlite3.IntegrityError, OSError, PermissionError) as e:
            logger.error(
                "Failed to get client stats in get_client_stats: %s: %s",
                type(e).__name__,
                e,
                extra={
                    "context": {
                        "client_id": client_id,
                        "db_path": self.db_path,
                        "operation": "get_client_stats",
                    }
                },
            )
            return {"webhooks": 0, "last_activity": None}

    def get_all_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get all recent activity."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            )

            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Failed to get all activity: %s", e)
            return []

    def get_recent_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        """Alias for get_all_activity."""
        return self.get_all_activity(limit)

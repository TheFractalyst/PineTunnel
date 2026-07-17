"""Shared base class for database managers.

Eliminates duplication between SqliteDatabaseManager and PostgresDatabaseManager
by providing concrete implementations of methods whose logic is identical across
backends. Each backend supplies dialect-specific SQL via ``execute_query()``
and the ``sql_*()`` helper methods.

The ``ws_signal_log`` table has the same schema in both backends, so all
signal-log methods (insert, acknowledge, update execution, query) are shared.
Cleanup methods share the same SQL structure with ``:param`` placeholders that
SQLite's ``execute_query`` auto-converts to ``?`` positional parameters.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseBase:
    """Abstract base class for database managers.

    Subclasses MUST implement:
    - ``execute_query(sql, params) -> list[dict]``
    - ``init_database() -> None``
    - ``get_connection()``
    - ``close() -> None``
    - ``dialect`` property
    - ``sql_now()``, ``sql_today()``
    - ``sql_interval_days()``, ``sql_interval_hours()``, ``sql_interval_minutes()``
    - ``sql_json_extract()``, ``sql_age_seconds()``
    - ``_ea_conn_conflict_target`` class attribute
    """

    # ------------------------------------------------------------------
    # Abstract methods - each backend MUST implement these
    # ------------------------------------------------------------------

    def execute_query(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def init_database(self) -> None:
        raise NotImplementedError

    def get_connection(self) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def dialect(self) -> str:
        raise NotImplementedError

    @staticmethod
    def sql_now() -> str:
        raise NotImplementedError

    @staticmethod
    def sql_today() -> str:
        raise NotImplementedError

    @staticmethod
    def sql_interval_days(days: int) -> str:
        raise NotImplementedError

    @staticmethod
    def sql_interval_hours(hours: int) -> str:
        raise NotImplementedError

    @staticmethod
    def sql_interval_minutes(minutes: int) -> str:
        raise NotImplementedError

    @staticmethod
    def sql_json_extract(column: str, key: str) -> str:
        raise NotImplementedError

    @staticmethod
    def sql_age_seconds(created_col: str) -> str:
        raise NotImplementedError

    # Conflict target for ea_connections upsert - differs by backend schema
    _ea_conn_conflict_target: str = ""

    # ------------------------------------------------------------------
    # Shared type-coercion helpers (used by signal-log and telemetry methods)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value: Any, default: int | None = None) -> int | None:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float | None = None) -> float | None:
        if value is None:
            return default
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_bool(value: Any, default: bool | None = None) -> bool | None:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    @staticmethod
    def _safe_str(value: Any, default: str = "", max_len: int = 255) -> str:
        if value is None:
            return default
        s = str(value)
        return s[:max_len]

    # ------------------------------------------------------------------
    # Trade statistics (from TradeStatisticsMixin)
    # ------------------------------------------------------------------

    def get_trade_statistics(
        self, days: int = 30, symbol: str | None = None, magic: int | None = None
    ) -> dict:
        """Get comprehensive trade statistics for the given period."""
        date_expr = self.sql_interval_days(days)
        query = (
            f"SELECT "
            f"COUNT(*) as total_trades, "
            f"COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades, "
            f"COUNT(CASE WHEN profit < 0 THEN 1 END) as losing_trades, "
            f"COUNT(CASE WHEN profit = 0 THEN 1 END) as breakeven_trades, "
            f"COALESCE(SUM(volume), 0) as total_volume, "
            f"COALESCE(SUM(CASE WHEN profit > 0 THEN profit ELSE 0 END), 0) as gross_profit, "
            f"COALESCE(SUM(CASE WHEN profit < 0 THEN profit ELSE 0 END), 0) as gross_loss, "
            f"COALESCE(SUM(profit), 0) as net_profit, "
            f"COALESCE(AVG(profit), 0) as avg_profit, "
            f"COALESCE(AVG(CASE WHEN profit > 0 THEN profit END), 0) as avg_win, "
            f"COALESCE(AVG(CASE WHEN profit < 0 THEN profit END), 0) as avg_loss, "
            f"COALESCE(MAX(profit), 0) as best_trade, "
            f"COALESCE(MIN(profit), 0) as worst_trade "
            f"FROM trades WHERE timestamp >= {date_expr} AND status = 'success'"
        )
        params: dict = {}
        if symbol:
            query += " AND symbol = :symbol"
            params["symbol"] = symbol
        if magic:
            query += " AND magic = :magic"
            params["magic"] = magic
        rows = self.execute_query(query, params or None)
        stats = dict(rows[0]) if rows else {}
        for key in (
            "total_trades",
            "winning_trades",
            "losing_trades",
            "breakeven_trades",
            "total_volume",
            "gross_profit",
            "gross_loss",
            "net_profit",
            "avg_profit",
            "avg_win",
            "avg_loss",
            "best_trade",
            "worst_trade",
        ):
            stats.setdefault(key, 0)
        total = stats["total_trades"]
        if total > 0:
            win_rate_decimal = stats["winning_trades"] / total
            stats["win_rate"] = win_rate_decimal * 100
            if stats["gross_loss"] and stats["gross_loss"] != 0:
                stats["profit_factor"] = abs(stats["gross_profit"] / stats["gross_loss"])
            else:
                stats["profit_factor"] = float("inf") if stats["gross_profit"] > 0 else 0
            loss_rate = max(1.0 - win_rate_decimal, 0.0)
            stats["expectancy"] = (win_rate_decimal * stats["avg_win"]) + (
                loss_rate * stats["avg_loss"]
            )
            profit_rows = self._get_profit_series(days, symbol, magic)
            stats["recovery_factor"] = self._calculate_recovery_factor(
                days, symbol, magic, rows=profit_rows
            )
            stats["sharpe_ratio"] = self._calculate_sharpe_ratio(
                days, symbol, magic, rows=profit_rows
            )
        else:
            stats["win_rate"] = 0
            stats["profit_factor"] = 0
            stats["expectancy"] = 0
            stats["recovery_factor"] = 0
            stats["sharpe_ratio"] = 0

        recent_rows = self.execute_query(
            f"SELECT timestamp, symbol, action, volume, profit, status "
            f"FROM trades WHERE timestamp >= {date_expr} AND status = 'success' "
            f"ORDER BY timestamp DESC LIMIT 10",
            params or None,
        )
        stats["recent_trades"] = recent_rows
        return stats

    def _get_profit_series(
        self, days: int, symbol: str | None = None, magic: int | None = None
    ) -> list[dict]:
        """Fetch the timestamp-ordered profit series for a period."""
        date_expr = self.sql_interval_days(days)
        query = f"SELECT profit FROM trades WHERE timestamp >= {date_expr} AND status = 'success'"
        params: dict = {}
        if symbol:
            query += " AND symbol = :symbol"
            params["symbol"] = symbol
        if magic:
            query += " AND magic = :magic"
            params["magic"] = magic
        query += " ORDER BY timestamp"
        return self.execute_query(query, params or None)

    def _calculate_recovery_factor(
        self,
        days: int,
        symbol: str | None = None,
        magic: int | None = None,
        rows: list[dict] | None = None,
    ) -> float:
        """Calculate recovery factor (net profit / max drawdown)."""
        if rows is None:
            rows = self._get_profit_series(days, symbol, magic)
        if not rows:
            return 0.0
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for row in rows:
            equity += row["profit"] or 0
            peak = max(peak, equity)
            drawdown = peak - equity
            max_dd = max(max_dd, drawdown)
        if max_dd > 0:
            return equity / max_dd
        return 0.0

    def _calculate_sharpe_ratio(
        self,
        days: int,
        symbol: str | None = None,
        magic: int | None = None,
        rows: list[dict] | None = None,
    ) -> float:
        """Calculate Sharpe ratio approximation (annualised)."""
        if rows is None:
            rows = self._get_profit_series(days, symbol, magic)
        profits = [row["profit"] for row in rows if row["profit"] is not None]
        if len(profits) < 2:
            return 0.0
        avg_return = sum(profits) / len(profits)
        variance = sum((p - avg_return) ** 2 for p in profits) / len(profits)
        std_dev = variance**0.5
        if std_dev > 0:
            return (avg_return / std_dev) * (252**0.5)
        return 0.0

    def get_signal_count(self, license_key: str, status: str = "pending") -> int:
        """Get count of signals by status for a license key."""
        if status not in ("pending", "acknowledged", "failed", "processed"):
            raise ValueError(
                f"Invalid status: {status}. Must be one of: pending, acknowledged, failed, processed"
            )
        rows = self.execute_query(
            "SELECT COUNT(*) as count FROM signal_queue "
            "WHERE license_key = :key AND status = :status",
            {"key": license_key, "status": status},
        )
        return rows[0]["count"] if rows else 0

    def get_signal_stats_by_license(self, license_key: str) -> dict:
        """Get signal counts grouped by status for a license key."""
        rows = self.execute_query(
            "SELECT "
            "COUNT(*) as total, "
            "COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending, "
            "COUNT(CASE WHEN status = 'acknowledged' THEN 1 END) as acknowledged "
            "FROM signal_queue WHERE license_key = :key",
            {"key": license_key},
        )
        return dict(rows[0]) if rows else {"total": 0, "pending": 0, "acknowledged": 0}

    # ------------------------------------------------------------------
    # Signal log — permanent record in ws_signal_log (same schema both backends)
    # ------------------------------------------------------------------

    def _log_signal_permanent(
        self, license_key: str, signal_id: str, signal_hash: str, signal_data: dict
    ) -> None:
        """Log a signal to the permanent ws_signal_log table (infinite retention).

        Non-critical: failures are logged but don't block signal delivery.
        Parsed fields (action, symbol, volume, sl, tp) extracted for querying.
        """
        parsed_data = signal_data if isinstance(signal_data, dict) else {}
        self.execute_query(
            "INSERT INTO ws_signal_log "
            "(license_key, signal_id, signal_hash, action, symbol, volume, sl, tp, signal_data) "
            "VALUES (:lk, :sid, :hash, :action, :symbol, :volume, :sl, :tp, :data)",
            {
                "lk": license_key,
                "sid": signal_id,
                "hash": signal_hash,
                "action": self._safe_str(
                    parsed_data.get("action", parsed_data.get("command")), max_len=50
                ),
                "symbol": self._safe_str(parsed_data.get("symbol"), max_len=50),
                "volume": self._safe_float(parsed_data.get("volume", parsed_data.get("amount"))),
                "sl": self._safe_float(parsed_data.get("sl")),
                "tp": self._safe_float(parsed_data.get("tp")),
                "data": json.dumps(signal_data, separators=(",", ":")),
            },
        )

    def acknowledge_signal_log(self, signal_id: str, delivered_via: str = "http") -> None:
        """Mark a signal as delivered/acknowledged in the permanent log."""
        now_expr = self.sql_now()
        self.execute_query(
            f"UPDATE ws_signal_log "
            f"SET acknowledged = TRUE, acknowledged_at = {now_expr}, "
            f"delivered_via = :via, execution_status = 'delivered' "
            f"WHERE signal_id = :sid AND acknowledged = FALSE",
            {"sid": signal_id, "via": delivered_via},
        )

    def _update_signal_execution(
        self, signal_id: str, execution_status: str, execution_detail: str, ticket: str = ""
    ) -> None:
        """Update signal execution status in permanent log after EA reports result."""
        now_expr = self.sql_now()
        self.execute_query(
            f"UPDATE ws_signal_log "
            f"SET execution_status = :status, execution_detail = :detail, "
            f"executed_at = {now_expr}, ticket = :ticket "
            f"WHERE signal_id = :sid",
            {
                "status": self._safe_str(execution_status, max_len=20),
                "detail": execution_detail,
                "ticket": self._safe_str(ticket, max_len=50),
                "sid": signal_id,
            },
        )

    def get_signal_log_for_license(
        self, license_key: str, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return recent signal log entries for a license (newest first)."""
        rows = self.execute_query(
            "SELECT signal_id, timestamp, action, symbol, volume, sl, tp, "
            "acknowledged, execution_status, execution_detail, ticket "
            "FROM ws_signal_log WHERE license_key = :lk "
            "ORDER BY timestamp DESC LIMIT :lim OFFSET :off",
            {"lk": license_key, "lim": limit, "off": offset},
        )
        for row in rows:
            row["acknowledged"] = bool(row.get("acknowledged", 0))
        return rows

    # ------------------------------------------------------------------
    # EA connections (shared logic, dialect-specific SQL via helpers)
    # ------------------------------------------------------------------

    def register_ea_connection(
        self, license_key: str, conn_type: str, worker_id: int, metadata: str = ""
    ) -> None:
        """Register or update an EA connection (upsert)."""
        now_expr = self.sql_now()
        try:
            self.execute_query(
                f"INSERT INTO ea_connections "
                f"(license_key, connection_type, worker_id, last_seen, metadata) "
                f"VALUES (:lk, :ct, :wid, {now_expr}, :meta) "
                f"ON CONFLICT{self._ea_conn_conflict_target} "
                f"DO UPDATE SET last_seen = {now_expr}, worker_id = :wid, metadata = :meta",
                {"lk": license_key, "ct": conn_type, "wid": worker_id, "meta": metadata},
            )
        except Exception as e:
            logger.warning("register_ea_connection failed: %s", e)

    def get_active_ea_connections(self, stale_seconds: int = 120) -> list[dict[str, Any]]:
        """Get active EA connections from last N seconds."""
        cutoff = datetime.now() - timedelta(seconds=stale_seconds)
        try:
            return self.execute_query(
                "SELECT license_key, connection_type, worker_id, last_seen, metadata "
                "FROM ea_connections WHERE last_seen >= :cutoff ORDER BY last_seen DESC",
                {"cutoff": cutoff},
            )
        except Exception as e:
            logger.warning("get_active_ea_connections failed: %s", e)
            return []

    def cleanup_stale_ea_connections(self, stale_seconds: int = 300) -> int:
        """Delete EA connections older than N seconds. Returns count deleted."""
        cutoff = datetime.now() - timedelta(seconds=stale_seconds)
        try:
            rows = self.execute_query(
                "DELETE FROM ea_connections WHERE last_seen < :cutoff RETURNING license_key",
                {"cutoff": cutoff},
            )
            return len(rows)
        except Exception as e:
            logger.warning("cleanup_stale_ea_connections failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Cleanup — same SQL structure with :param placeholders
    # ------------------------------------------------------------------

    def cleanup_old_signals(self, days_to_keep: int = 7, stale_hours: int = 24) -> dict[str, int]:
        """Remove old acknowledged/expired signals.

        Returns a dict with counts: acknowledged, stale_pending, stale_claimed, total.
        """
        now = datetime.now()
        cutoff = now - timedelta(days=days_to_keep)
        stale_cutoff = now - timedelta(hours=stale_hours)

        ack_rows = self.execute_query(
            "DELETE FROM signal_queue WHERE status = 'acknowledged' "
            "AND created_at < :cutoff RETURNING id",
            {"cutoff": cutoff},
        )
        deleted_acknowledged = len(ack_rows)

        stale_claimed_rows = self.execute_query(
            "DELETE FROM signal_queue WHERE status IN ('pending', 'claimed') "
            "AND created_at < :stale_cutoff RETURNING id, status",
            {"stale_cutoff": stale_cutoff},
        )
        deleted_stale = sum(1 for r in stale_claimed_rows if r.get("status") == "pending")
        deleted_claimed = sum(1 for r in stale_claimed_rows if r.get("status") == "claimed")

        total = deleted_acknowledged + deleted_stale + deleted_claimed
        if total > 0:
            logger.warning(
                "Cleanup: Removed %d acknowledged (>%dd), "
                "%d stale pending (>%dh), "
                "%d orphaned claimed (>%dh)",
                deleted_acknowledged,
                days_to_keep,
                deleted_stale,
                stale_hours,
                deleted_claimed,
                stale_hours,
            )
        return {
            "acknowledged": deleted_acknowledged,
            "stale_pending": deleted_stale,
            "stale_claimed": deleted_claimed,
            "total": total,
        }

    def cleanup_old_data(self, days_to_keep: int = 90) -> dict[str, int]:
        """Remove old records (trades, alerts, account stats) beyond retention period."""
        cutoff = datetime.now() - timedelta(days=days_to_keep)

        trades_rows = self.execute_query(
            "DELETE FROM trades WHERE timestamp < :cutoff RETURNING id",
            {"cutoff": cutoff},
        )
        alerts_rows = self.execute_query(
            "DELETE FROM alert_history WHERE timestamp < :cutoff RETURNING id",
            {"cutoff": cutoff},
        )
        stats_rows = self.execute_query(
            "DELETE FROM account_stats WHERE timestamp < :cutoff RETURNING id",
            {"cutoff": cutoff},
        )
        return {
            "trades_deleted": len(trades_rows),
            "alerts_deleted": len(alerts_rows),
            "account_stats_deleted": len(stats_rows),
        }

    def cleanup_old_account_stats(self, days_to_keep: int = 30) -> None:
        """Delete account stats older than N days."""
        cutoff = datetime.now() - timedelta(days=days_to_keep)
        self.execute_query(
            "DELETE FROM account_stats WHERE timestamp < :cutoff",
            {"cutoff": cutoff},
        )

    # ---------------------------------------------------------------------------
    # Support chat log (shared - same schema in SQLite and PostgreSQL)
    # ---------------------------------------------------------------------------

    def log_support_message(
        self,
        chat_id: int,
        username: str,
        full_name: str,
        license_key_masked: str,
        role: str,
        message: str,
    ) -> None:
        """Log a support chat message (user question or AI response)."""
        self.execute_query(
            "INSERT INTO support_chat_log "
            "(chat_id, username, full_name, license_key_masked, role, message) "
            "VALUES (:chat_id, :username, :full_name, :license_key_masked, :role, :message)",
            {
                "chat_id": chat_id,
                "username": username or "",
                "full_name": full_name or "",
                "license_key_masked": license_key_masked or "",
                "role": role,
                "message": message,
            },
        )

    def get_support_logs(
        self, chat_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Get support chat logs, optionally filtered by chat_id."""
        cols = "id, chat_id, username, full_name, license_key_masked, " "role, message, timestamp"
        if chat_id:
            rows = self.execute_query(
                f"SELECT {cols} FROM support_chat_log WHERE chat_id = :chat_id "
                "ORDER BY timestamp ASC LIMIT :limit OFFSET :offset",
                {"chat_id": chat_id, "limit": limit, "offset": offset},
            )
        else:
            rows = self.execute_query(
                f"SELECT {cols} FROM support_chat_log "
                "ORDER BY timestamp DESC LIMIT :limit OFFSET :offset",
                {"limit": limit, "offset": offset},
            )
        return rows

    def get_support_chat_users(self) -> list[dict[str, Any]]:
        """Get summary of all users who have chatted with AI support."""
        rows = self.execute_query(
            "SELECT chat_id, MAX(username) as username, MAX(full_name) as full_name, "
            "MAX(license_key_masked) as license_key_masked, "
            "COUNT(*) as message_count, "
            "MIN(timestamp) as first_message, "
            "MAX(timestamp) as last_message "
            "FROM support_chat_log GROUP BY chat_id ORDER BY last_message DESC"
        )
        return rows

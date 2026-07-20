"""SQLite Database Manager - lightweight single-file backend for small deployments.

Mirrors the public interface of PostgresDatabaseManager so the app can
switch backends without changing call sites. Designed for single-worker
deployments (Render Starter, local dev). Not suitable for multi-worker
concurrency (SQLite single-writer limitation).
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.server.db.base import DatabaseBase

logger = logging.getLogger(__name__)

# SQLite serialises all writes through a single lock. We use a reentrant lock
# (RLock) because some methods like save_signal() acquire the lock and then
# call _log_signal_permanent() which also needs the lock.
_write_lock = threading.RLock()


class SqliteDatabaseManager(DatabaseBase):
    """SQLite database manager mirroring PostgresDatabaseManager interface."""

    _ea_conn_conflict_target = "(license_key, connection_type)"

    def __init__(
        self,
        db_path: str,
        **kwargs: Any,
    ) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Enable WAL mode for better read concurrency (writes still serialised).
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")

        # Restrict DB file permissions to owner only (chmod 600)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

        # SQLAlchemy engine for get_connection() - used by routes that write
        # SQLAlchemy-style queries (ws_telemetry, trade_analytics). Uses
        # StaticPool so the single sqlite3 connection is shared (SQLite is
        # single-writer anyway). check_same_thread=False matches the raw conn.
        self._sa_engine = create_engine(
            f"sqlite:///{db_path}",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self._SessionLocal = sessionmaker(bind=self._sa_engine, expire_on_commit=False)

        logger.info("SQLite database manager initialized: %s", db_path)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    @contextmanager
    def get_connection(self) -> Any:
        """Get a SQLAlchemy session (compatibility with PostgresDatabaseManager).

        Yields a SQLAlchemy Session so routes using ``text()`` + ``session.execute()``
        work on both backends. PG-specific SQL (DISTINCT ON, NULLS LAST) will
        raise ProgrammingError on SQLite - callers catch that for graceful
        degradation.
        """
        session = self._SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, *args: Any) -> None:
        pass  # single connection, no per-use commit/rollback

    def execute_query(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Execute a query and return rows as dicts.

        TECH DEBT: This method regex-translates PostgreSQL SQL to SQLite
        (NOW()→datetime('now'), INTERVAL→datetime offset, ::TYPE casts, etc).
        This is fragile - any new PG-specific SQL in callers will break SQLite.
        Long-term fix: write native SQLite queries in each method instead of
        sharing PG SQL strings. Tracked as a separate refactor effort.

        Accepts both SQLite ? placeholders and SQLAlchemy-style :param placeholders.
        Converts :param to ? automatically for SQLite compatibility.
        Also converts common PostgreSQL functions to SQLite equivalents.
        """
        sqlite_sql = sql
        sqlite_params: list[Any] = []

        if params:
            # Convert :param style to ? style
            def _replace(match: re.Match) -> str:
                name = match.group(1)
                if name in params:
                    sqlite_params.append(params[name])
                    return "?"
                return match.group(0)

            sqlite_sql = re.sub(r":(\w+)", _replace, sqlite_sql)

        # Convert PG functions to SQLite equivalents
        sqlite_sql = sqlite_sql.replace("NOW()", "datetime('now')")
        sqlite_sql = sqlite_sql.replace("INTERVAL '3 minutes'", "datetime('now', '-3 minutes')")
        # Handle INTERVAL 'N minutes' pattern generically
        sqlite_sql = re.sub(
            r"INTERVAL '(\d+) minutes'",
            lambda m: f"datetime('now', '-{m.group(1)} minutes')",
            sqlite_sql,
        )
        sqlite_sql = re.sub(
            r"INTERVAL '(\d+) hours'",
            lambda m: f"datetime('now', '-{m.group(1)} hours')",
            sqlite_sql,
        )
        sqlite_sql = re.sub(
            r"INTERVAL '(\d+) days'",
            lambda m: f"datetime('now', '-{m.group(1)} days')",
            sqlite_sql,
        )
        # pg_database_size not available in SQLite - return 0
        sqlite_sql = re.sub(
            r"pg_database_size\(current_database\(\)\)[^\d]*",
            "0",
            sqlite_sql,
        )
        # COALESCE(x, NOW()) → COALESCE(x, datetime('now'))
        sqlite_sql = sqlite_sql.replace(
            "COALESCE(acknowledged_at, NOW())", "COALESCE(acknowledged_at, datetime('now'))"
        )
        # EXTRACT(EPOCH FROM (a - b))::INTEGER → strftime-based seconds diff (approximate)
        sqlite_sql = re.sub(
            r"EXTRACT\(EPOCH FROM \(COALESCE\(acknowledged_at, datetime\('now'\)\) - created_at\)\)::INTEGER",
            "CAST((julianday(COALESCE(acknowledged_at, datetime('now'))) - julianday(created_at)) * 86400 AS INTEGER)",
            sqlite_sql,
        )
        # Strip PG type casts ::TYPE (e.g., ::INTEGER, ::varchar)
        sqlite_sql = re.sub(r"::\w+", "", sqlite_sql)
        # CAST(x AS varchar) → CAST(x AS TEXT) for SQLite
        sqlite_sql = re.sub(r"CAST\(([^,]+) AS varchar\)", r"CAST(\1 AS TEXT)", sqlite_sql)
        # CURRENT_DATE → date('now') for SQLite
        sqlite_sql = sqlite_sql.replace("CURRENT_DATE", "date('now')")
        # NULLS LAST → remove (SQLite already sorts NULLs last for DESC)
        sqlite_sql = sqlite_sql.replace(" NULLS LAST", "")

        cur = self._conn.execute(sqlite_sql, sqlite_params)
        rows = cur.fetchall()
        # Auto-commit for INSERT/UPDATE/DELETE/CREATE statements
        if any(
            sqlite_sql.strip().upper().startswith(kw)
            for kw in ("INSERT", "UPDATE", "DELETE", "CREATE")
        ):
            self._conn.commit()
        return [dict(r) for r in rows]

    def get_pool_stats(self) -> dict[str, Any]:
        """Return pool stats (mock - SQLite has no pool)."""
        return {
            "type": "sqlite",
            "total_connections": 1,
            "in_use": 0,
            "available": 1,
            "overflow": 0,
            "pool_size": 1,
            "max_overflow": 0,
            "db_path": self.db_path,
        }

    # ------------------------------------------------------------------
    # Init / schema
    # ------------------------------------------------------------------

    def init_database(self) -> None:
        """Create all required tables if they don't exist.

        Handles existing databases with stale schemas by creating tables
        and indexes individually - CREATE INDEX on a missing column in an
        existing table is skipped with a warning.
        """
        cur = self._conn

        # Phase 1: Create tables (safe - IF NOT EXISTS is a no-op on existing)
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS signal_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT UNIQUE,
                license_key TEXT NOT NULL,
                signal_data TEXT NOT NULL,
                signal_hash TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed_at TIMESTAMP,
                claimed_by TEXT,
                acknowledged_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS account_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                account INTEGER,
                broker TEXT,
                currency TEXT,
                leverage INTEGER,
                balance REAL,
                equity REAL,
                profit REAL,
                margin REAL,
                margin_free REAL,
                margin_level REAL,
                open_positions INTEGER,
                pending_orders INTEGER,
                ea_version TEXT,
                dll_version TEXT,
                account_name TEXT,
                magic INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT,
                symbol TEXT,
                action TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT,
                signal_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ticket INTEGER,
                deal INTEGER,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                volume REAL,
                price REAL,
                entry_price REAL,
                exit_price REAL,
                sl REAL,
                tp REAL,
                profit REAL,
                profit_percent REAL,
                commission REAL,
                swap REAL,
                comment TEXT,
                magic INTEGER,
                duration_minutes INTEGER,
                status TEXT,
                error TEXT,
                ip_address TEXT,
                execution_time_ms REAL,
                spread_points REAL,
                slippage REAL
            );
            CREATE TABLE IF NOT EXISTS ws_signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                signal_id TEXT NOT NULL,
                signal_hash TEXT,
                action TEXT,
                symbol TEXT,
                volume REAL,
                sl REAL,
                tp REAL,
                signal_data TEXT NOT NULL,
                delivered_via TEXT,
                acknowledged INTEGER DEFAULT 0,
                acknowledged_at TIMESTAMP,
                execution_status TEXT DEFAULT 'pending',
                execution_detail TEXT,
                executed_at TIMESTAMP,
                ticket TEXT
            );
            CREATE TABLE IF NOT EXISTS ws_account_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL UNIQUE,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ws_open_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ws_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ws_health_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL UNIQUE,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ea_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL UNIQUE,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ea_connections (
                license_key TEXT NOT NULL,
                connection_type TEXT NOT NULL,
                worker_id INTEGER NOT NULL,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                PRIMARY KEY (license_key, connection_type)
            );
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                user_agent TEXT,
                payload TEXT,
                action TEXT,
                symbol TEXT,
                volume REAL,
                response_code INTEGER,
                response_message TEXT,
                execution_time_ms REAL,
                rate_limited INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS worker_connections (
                worker_id INTEGER PRIMARY KEY,
                connection_count INTEGER NOT NULL DEFAULT 0,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                breakeven_trades INTEGER DEFAULT 0,
                total_volume REAL DEFAULT 0,
                gross_profit REAL DEFAULT 0,
                gross_loss REAL DEFAULT 0,
                net_profit REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                average_win REAL DEFAULT 0,
                average_loss REAL DEFAULT 0,
                largest_win REAL DEFAULT 0,
                largest_loss REAL DEFAULT 0,
                total_alerts_received INTEGER DEFAULT 0,
                failed_trades INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS symbol_performance (
                symbol TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                total_volume REAL DEFAULT 0,
                net_profit REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                average_profit REAL DEFAULT 0,
                best_trade REAL DEFAULT 0,
                worst_trade REAL DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS support_chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                license_key_masked TEXT DEFAULT '',
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

        # Phase 2: Create indexes individually (tolerate missing columns in
        # pre-existing tables from older schema versions)
        _indexes = [
            "idx_sq_license ON signal_queue (license_key)",
            "idx_sq_status ON signal_queue (status)",
            "idx_sq_hash ON signal_queue (signal_hash)",
            "idx_as_license ON account_stats (license_key)",
            "idx_as_ts ON account_stats (timestamp)",
            "idx_trades_license ON trades (license_key)",
            "idx_trades_timestamp ON trades (timestamp)",
            "idx_trades_symbol ON trades (symbol)",
            "idx_trades_status ON trades (status)",
            "idx_wsl_license ON ws_signal_log (license_key)",
            "idx_wsl_timestamp ON ws_signal_log (timestamp)",
            "idx_wsl_license_ts ON ws_signal_log (license_key, timestamp)",
            "idx_wsl_symbol ON ws_signal_log (symbol)",
            "idx_wsl_action ON ws_signal_log (action)",
            "idx_wsl_sid ON ws_signal_log (signal_id)",
            "idx_wop_license ON ws_open_positions (license_key)",
            "idx_wth_license ON ws_trade_history (license_key)",
            "idx_ea_conn_last_seen ON ea_connections (last_seen)",
            "idx_ah_ts ON alert_history (timestamp)",
        ]
        for idx in _indexes:
            try:
                cur.execute(f"CREATE INDEX IF NOT EXISTS {idx}")
            except sqlite3.OperationalError as e:
                logger.debug("Skipping index %s: %s", idx.split()[0], e)
        self._conn.commit()

        # Phase 3: Ensure columns added in later versions exist
        self._ensure_account_stats_columns()

        logger.info("SQLite tables initialized")

    # ------------------------------------------------------------------
    # Safety-net column ensures (idempotent, match PostgresDatabaseManager)
    # ------------------------------------------------------------------

    def _ensure_account_stats_columns(self) -> None:
        """Ensure ALL expected columns exist in account_stats.

        Handles databases created by older schema versions that may be
        missing columns added later. SQLite's ALTER TABLE ADD COLUMN
        is safe to call idempotently (we check first).
        """
        expected = {
            "license_key": "TEXT NOT NULL DEFAULT ''",
            "account": "INTEGER",
            "broker": "TEXT",
            "currency": "TEXT",
            "leverage": "INTEGER",
            "balance": "REAL",
            "equity": "REAL",
            "profit": "REAL",
            "margin": "REAL",
            "margin_free": "REAL",
            "margin_level": "REAL",
            "open_positions": "INTEGER",
            "pending_orders": "INTEGER",
            "ea_version": "TEXT",
            "dll_version": "TEXT",
            "account_name": "TEXT",
            "magic": "INTEGER",
            "timestamp": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(account_stats)")}
        for col, coltype in expected.items():
            if col not in cols:
                try:
                    self._conn.execute(f"ALTER TABLE account_stats ADD COLUMN {col} {coltype}")
                except sqlite3.OperationalError:
                    pass
        self._conn.commit()

    def _ensure_ws_telemetry_tables(self) -> None:
        """Tables are created in init_database() - no-op for SQLite."""
        pass

    def _ensure_ea_connections_table(self) -> None:
        """Table is created in init_database() - no-op for SQLite."""
        pass

    def _ensure_bigint_columns(self) -> None:
        """No-op for SQLite (dynamic typing)."""
        pass

    # ------------------------------------------------------------------
    # Signal queue
    # ------------------------------------------------------------------

    def save_signal(
        self,
        license_key: str,
        signal_data: dict[str, Any],
        duplicate_window_minutes: int = 5,
        _retry_count: int = 0,
        status: str = "pending",
    ) -> str | None:
        """Save signal with duplicate detection. Returns signal_id or None if duplicate.

        ``status`` defaults to 'pending' (queue path, polled by EA). The execute path
        passes 'acknowledged' so the row is recorded for idempotency/dedup but is
        invisible to the EA poll (which only reads 'pending') and is cleaned up by the
        existing acknowledged-row reaper - no schema or cleanup change needed.
        """
        hash_data = {
            k: v for k, v in signal_data.items() if k not in ["queued_at", "timestamp", "signal_id"]
        }
        signal_hash = hashlib.md5(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()
        signal_id = str(uuid.uuid4())[:8]
        dedup_cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=duplicate_window_minutes)
        ).strftime("%Y-%m-%d %H:%M:%S")

        with _write_lock:
            dup = self._conn.execute(
                "SELECT 1 FROM signal_queue WHERE license_key = ? AND signal_hash = ? "
                "AND created_at >= ?",
                (license_key, signal_hash, dedup_cutoff),
            ).fetchone()
            if dup:
                return None

            self._conn.execute(
                "INSERT INTO signal_queue (signal_id, license_key, signal_data, signal_hash, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    signal_id,
                    license_key,
                    json.dumps(signal_data, separators=(",", ":")),
                    signal_hash,
                    status,
                ),
            )

            # Permanent log (non-critical)
            try:
                self._log_signal_permanent(license_key, signal_id, signal_hash, signal_data)
            except Exception as e:
                logger.warning("Failed to log signal to ws_signal_log: %s", e)

            self._conn.commit()
            return signal_id

    async def get_pending_signals_async(
        self,
        license_key: str,
        max_age_minutes: int = 60,
        claimed_by: str | None = None,
        stale_claim_minutes: int = 5,
    ) -> list[dict]:
        """Claim and return pending signals for a license."""
        # Use SQLite-native timestamp format (space separator, no microseconds)
        # because created_at is stored via DEFAULT CURRENT_TIMESTAMP
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        max_age_cutoff = (now_dt - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        stale_cutoff = (now_dt - timedelta(minutes=stale_claim_minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        with _write_lock:
            # Re-claim stale claimed signals
            self._conn.execute(
                "UPDATE signal_queue SET status = 'claimed', claimed_at = ?, claimed_by = ? "
                "WHERE license_key = ? AND status = 'claimed' AND claimed_at < ?",
                (now, claimed_by, license_key, stale_cutoff),
            )

            # Claim pending signals within age window
            cur = self._conn.execute(
                "UPDATE signal_queue SET status = 'claimed', claimed_at = ?, claimed_by = ? "
                "WHERE license_key = ? AND status = 'pending' AND created_at >= ? "
                "RETURNING signal_id, signal_data, created_at",
                (now, claimed_by, license_key, max_age_cutoff),
            )
            rows = cur.fetchall()
            self._conn.commit()

        signals: list[dict] = []
        for row in rows:
            try:
                data = json.loads(row[1]) if row[1] else None
                if not data:
                    continue
                data["signal_id"] = row[0]
                created_str = row[2] if row[2] else now
                try:
                    created = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    created = now_dt
                data["age_minutes"] = (now_dt - created).total_seconds() / 60.0
                signals.append(data)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return signals

    async def acknowledge_signal_async(self, signal_id: str, license_key: str) -> bool:
        """Mark signal as acknowledged."""
        now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with _write_lock:
            cur = self._conn.execute(
                "UPDATE signal_queue SET status = 'acknowledged', acknowledged_at = ? "
                "WHERE signal_id = ? AND license_key = ? AND status IN ('pending', 'claimed')",
                (now, signal_id, license_key),
            )
            self._conn.commit()
            return cur.rowcount > 0

    async def acknowledge_signals_batch_async(
        self, signal_ids: list[str], license_key: str
    ) -> dict[str, Any]:
        """Acknowledge multiple signals."""
        if not signal_ids:
            return {"acknowledged": [], "failed": []}
        now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" * len(signal_ids))
        with _write_lock:
            cur = self._conn.execute(
                f"UPDATE signal_queue SET status = 'acknowledged', acknowledged_at = ? "
                f"WHERE signal_id IN ({placeholders}) AND license_key = ? "
                f"AND status IN ('pending', 'claimed') RETURNING signal_id",
                (now, *signal_ids, license_key),
            )
            acknowledged = [row[0] for row in cur.fetchall()]
            self._conn.commit()
        ack_set = set(acknowledged)
        failed = [sid for sid in signal_ids if sid not in ack_set]
        return {"acknowledged": acknowledged, "failed": failed}

    def acknowledge_signal(self, signal_id: str, license_key: str | None = None) -> bool:
        """Sync acknowledge - only pending signals (matches PostgresDatabaseManager)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with _write_lock:
            if license_key:
                cur = self._conn.execute(
                    "UPDATE signal_queue SET status = 'acknowledged', acknowledged_at = ? "
                    "WHERE signal_id = ? AND license_key = ? AND status = 'pending'",
                    (now, signal_id, license_key),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE signal_queue SET status = 'acknowledged', acknowledged_at = ? "
                    "WHERE signal_id = ? AND status = 'pending'",
                    (now, signal_id),
                )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Signal queries
    # ------------------------------------------------------------------

    def get_signals_by_license(
        self,
        license_key: str,
        status_filter: str = "all",
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get signals for a license with optional status filter.

        Mirrors PostgresDatabaseManager.get_signals_by_license - default limit 10.
        """
        query = (
            "SELECT signal_id, license_key, signal_data, status, "
            "created_at, acknowledged_at FROM signal_queue WHERE license_key = ?"
        )
        params: list[Any] = [license_key]
        if status_filter in ("pending", "acknowledged"):
            query += " AND status = ?"
            params.append(status_filter)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            try:
                entry["signal_data"] = json.loads(entry["signal_data"])
            except (json.JSONDecodeError, TypeError):
                entry["signal_data"] = {}
            results.append(entry)
        return results

    def get_pending_signals(
        self, license_key: str, max_age_minutes: int = 60, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get pending signals (sync, no claiming)."""
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=max_age_minutes)
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._conn.execute(
            "SELECT signal_id, signal_data, created_at, "
            f"({self.sql_age_seconds('created_at')}) / 60.0 as age_minutes "
            "FROM signal_queue "
            "WHERE license_key = ? AND status = 'pending' AND created_at >= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (license_key, cutoff, limit),
        ).fetchall()
        signals: list[dict] = []
        for row in rows:
            try:
                data = json.loads(row["signal_data"]) if row["signal_data"] else None
                if data:
                    data["signal_id"] = row["signal_id"]
                    data["age_minutes"] = row["age_minutes"]
                    signals.append(data)
            except (json.JSONDecodeError, TypeError):
                continue
        return signals

    def delete_signals_by_license(self, license_key: str) -> int:
        """Delete all signals for a license."""
        with _write_lock:
            cur = self._conn.execute(
                "DELETE FROM signal_queue WHERE license_key = ?", (license_key,)
            )
            self._conn.commit()
            return cur.rowcount

    def ensure_pool_initialized(self) -> None:
        """No-op for SQLite (no pool)."""
        pass

    # ------------------------------------------------------------------
    # Account stats
    # ------------------------------------------------------------------

    def save_account_stats(self, stats: dict) -> None:
        """Insert an account stats snapshot from an EA (legacy HTTP endpoint).

        Mirrors PostgresDatabaseManager.save_account_stats - inserts a full row
        with all account_stats columns.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _write_lock:
            self._conn.execute(
                "INSERT INTO account_stats "
                "(license_key, account, account_name, broker, currency, leverage, "
                "balance, equity, profit, margin, margin_free, margin_level, "
                "open_positions, pending_orders, ea_version, dll_version, magic, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stats.get("license_key"),
                    _safe_int(stats.get("account")),
                    stats.get("account_name"),
                    stats.get("broker"),
                    stats.get("currency"),
                    _safe_int(stats.get("leverage")),
                    _safe_float(stats.get("balance")),
                    _safe_float(stats.get("equity")),
                    _safe_float(stats.get("profit")),
                    _safe_float(stats.get("margin")),
                    _safe_float(stats.get("margin_free")),
                    _safe_float(stats.get("margin_level")),
                    _safe_int(stats.get("open_positions")),
                    _safe_int(stats.get("pending_orders")),
                    stats.get("ea_version"),
                    stats.get("dll_version"),
                    _safe_int(stats.get("magic")),
                    stats.get("timestamp") or now,
                ),
            )
            self._conn.commit()

    def get_latest_account_stats(self, license_key: str | None = None) -> list[dict]:
        """Get latest account stats.

        With license_key: returns the most recent snapshot for that license.
        Without license_key: returns the most recent snapshot per license
        (mirrors PostgresDatabaseManager using an INNER JOIN with max timestamp).
        """
        if license_key:
            rows = self._conn.execute(
                "SELECT * FROM account_stats WHERE license_key = ? ORDER BY timestamp DESC LIMIT 1",
                (license_key,),
            ).fetchall()
            return [dict(r) for r in rows]
        # Latest snapshot per license using a subquery join (same logic as PG)
        rows = self._conn.execute(
            "SELECT a.* FROM account_stats a "
            "INNER JOIN ("
            "  SELECT license_key, MAX(timestamp) as max_ts "
            "  FROM account_stats GROUP BY license_key"
            ") b ON a.license_key = b.license_key AND a.timestamp = b.max_ts"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # WS telemetry (account stats, positions, trade history, health)
    # ------------------------------------------------------------------

    def save_ws_account_stats(self, license_key: str, data: dict) -> None:
        """Save WS account stats (upsert)."""
        with _write_lock:
            now_iso = datetime.now().isoformat()
            data_json = json.dumps(data)
            self._conn.execute(
                "INSERT INTO ws_account_stats (license_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(license_key) DO UPDATE SET data = ?, updated_at = ?",
                (license_key, data_json, now_iso, data_json, now_iso),
            )
            self._conn.commit()

    def save_ws_open_positions(self, license_key: str, positions: list[dict]) -> None:
        """Save WS open positions."""
        with _write_lock:
            now_iso = datetime.now().isoformat()
            pos_json = json.dumps(positions)
            self._conn.execute(
                "INSERT INTO ws_open_positions (license_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(license_key) DO UPDATE SET data = ?, updated_at = ?",
                (license_key, pos_json, now_iso, pos_json, now_iso),
            )
            self._conn.commit()

    def save_ws_trade_history(self, license_key: str, deals: list[dict]) -> None:
        """Save WS trade history."""
        with _write_lock:
            now_iso = datetime.now().isoformat()
            deals_json = json.dumps(deals)
            self._conn.execute(
                "INSERT INTO ws_trade_history (license_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(license_key) DO UPDATE SET data = ?, updated_at = ?",
                (license_key, deals_json, now_iso, deals_json, now_iso),
            )
            self._conn.commit()

    def save_ws_health_telemetry(self, license_key: str, data: dict) -> None:
        """Save WS health telemetry."""
        with _write_lock:
            now_iso = datetime.now().isoformat()
            data_json = json.dumps(data)
            self._conn.execute(
                "INSERT INTO ws_health_telemetry (license_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(license_key) DO UPDATE SET data = ?, updated_at = ?",
                (license_key, data_json, now_iso, data_json, now_iso),
            )
            self._conn.commit()

    def save_ea_audit(self, license_key: str, data: dict) -> None:
        """Save EA audit (upsert)."""
        with _write_lock:
            now_iso = datetime.now().isoformat()
            data_json = json.dumps(data)
            self._conn.execute(
                "INSERT INTO ea_audit (license_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(license_key) DO UPDATE SET data = ?, updated_at = ?",
                (license_key, data_json, now_iso, data_json, now_iso),
            )
            self._conn.commit()

    def get_ea_audit_for_license(self, license_key: str) -> dict[str, Any] | None:
        """Return the most recent EA audit record for a license (JSON blob)."""
        row = self._conn.execute(
            "SELECT data FROM ea_audit WHERE license_key = ?", (license_key,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def get_latest_open_positions(self, license_key: str) -> list[dict[str, Any]]:
        """Return the most recent snapshot of open positions for a license."""
        row = self._conn.execute(
            "SELECT data FROM ws_open_positions WHERE license_key = ?", (license_key,)
        ).fetchone()
        return json.loads(row["data"]) if row else []

    def get_trade_history_for_license(
        self, license_key: str, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return recent closed trade history for a license (newest first)."""
        row = self._conn.execute(
            "SELECT data FROM ws_trade_history WHERE license_key = ?", (license_key,)
        ).fetchone()
        if not row:
            return []
        try:
            deals = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(deals, list):
            return []
        # Sort by close_time descending (newest first), then paginate
        deals.sort(key=lambda d: d.get("close_time", d.get("time", "")), reverse=True)
        return deals[offset : offset + limit]

    def cleanup_ws_telemetry(
        self,
        stats_days: int = 0,
        health_days: int = 0,
        positions_days: int = 0,
        audit_days: int = 0,
    ) -> int:
        """Delete old telemetry data - MANUAL USE ONLY, never auto-scheduled.

        By default all arguments are 0, meaning NO data is deleted.
        Returns total number of rows deleted.
        """
        if stats_days <= 0 and health_days <= 0 and positions_days <= 0 and audit_days <= 0:
            logger.warning(
                "WS telemetry cleanup called with no thresholds - skipping (data retained infinitely)"
            )
            return 0

        total_deleted = 0
        table_days = {
            "ws_account_stats": stats_days,
            "ws_health_telemetry": health_days,
            "ws_open_positions": positions_days,
            "ws_trade_history": positions_days,
            "ws_signal_log": positions_days,
            "ea_audit": audit_days,
        }
        with _write_lock:
            for table, days in table_days.items():
                if days <= 0:
                    continue
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                cur = self._conn.execute(f"DELETE FROM {table} WHERE updated_at < ?", (cutoff,))
                total_deleted += cur.rowcount
            self._conn.commit()
        return total_deleted

    # ------------------------------------------------------------------
    # Alert / trade logging
    # ------------------------------------------------------------------

    def log_alert(self, alert_data: dict) -> int:
        """Log webhook alert to alert_history table.

        Mirrors PostgresDatabaseManager.log_alert - inserts a full row with
        ip_address, user_agent, payload, action, symbol, volume,
        response_code, response_message, execution_time_ms, rate_limited.
        """
        with _write_lock:
            cur = self._conn.execute(
                "INSERT INTO alert_history "
                "(ip_address, user_agent, payload, action, symbol, volume, "
                "response_code, response_message, execution_time_ms, rate_limited) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    alert_data.get("ip_address"),
                    alert_data.get("user_agent"),
                    json.dumps(alert_data.get("payload", {}), default=str),
                    alert_data.get("action"),
                    alert_data.get("symbol"),
                    alert_data.get("volume"),
                    alert_data.get("response_code"),
                    alert_data.get("response_message"),
                    alert_data.get("execution_time_ms"),
                    1 if alert_data.get("rate_limited", False) else 0,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def log_trade(self, trade_data: dict) -> int:
        """Log trade to database and return the trade ID.

        Mirrors PostgresDatabaseManager.log_trade - inserts a full row with
        all trade columns and updates signal execution tracking + symbol performance.
        """
        with _write_lock:
            cur = self._conn.execute(
                "INSERT INTO trades "
                "(license_key, signal_id, ticket, deal, symbol, action, volume, price, "
                "entry_price, exit_price, sl, tp, profit, profit_percent, commission, swap, "
                "comment, magic, duration_minutes, status, error, ip_address, "
                "execution_time_ms, spread_points, slippage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_data.get("license_key"),
                    trade_data.get("signal_id"),
                    _safe_int(trade_data.get("ticket")),
                    _safe_int(trade_data.get("deal")),
                    trade_data.get("symbol"),
                    trade_data.get("action"),
                    _safe_float(trade_data.get("volume")),
                    _safe_float(trade_data.get("price")),
                    _safe_float(trade_data.get("entry_price")),
                    _safe_float(trade_data.get("exit_price")),
                    _safe_float(trade_data.get("sl")),
                    _safe_float(trade_data.get("tp")),
                    _safe_float(trade_data.get("profit", 0)),
                    _safe_float(trade_data.get("profit_percent", 0)),
                    _safe_float(trade_data.get("commission", 0)),
                    _safe_float(trade_data.get("swap", 0)),
                    trade_data.get("comment"),
                    _safe_int(trade_data.get("magic")),
                    _safe_int(trade_data.get("duration_minutes")),
                    trade_data.get("status"),
                    trade_data.get("error"),
                    trade_data.get("ip_address"),
                    _safe_float(trade_data.get("execution_time_ms")),
                    _safe_float(trade_data.get("spread_points")),
                    _safe_float(trade_data.get("slippage", 0)),
                ),
            )
            self._conn.commit()
            trade_id = cur.lastrowid or 0

        # Update ws_signal_log execution tracking if signal_id provided
        signal_id = trade_data.get("signal_id")
        if signal_id:
            try:
                exec_status = "executed" if trade_data.get("status") == "success" else "failed"
                exec_detail = trade_data.get("error") or trade_data.get("status", "")
                ticket = str(trade_data.get("ticket", ""))
                self._update_signal_execution(signal_id, exec_status, exec_detail, ticket)
            except Exception as e:
                logger.warning("Failed to update signal execution status: %s", e)

        # Update symbol performance metrics after successful insert
        if trade_data.get("status") == "success" and trade_data.get("symbol"):
            try:
                self._update_symbol_performance(trade_data["symbol"])
            except Exception:
                logger.warning(
                    "Failed to update symbol_performance for %s", trade_data.get("symbol")
                )
        return trade_id

    # ------------------------------------------------------------------
    # Statistics / summaries
    # ------------------------------------------------------------------

    def get_daily_summary(self, date: str | None = None) -> dict:
        """Get daily trading summary for a given date (defaults to today)."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        rows = self.execute_query("SELECT * FROM daily_stats WHERE date = :date", {"date": date})
        if rows:
            return dict(rows[0])
        # No cached row - compute on the fly from trades table
        next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        rows = self.execute_query(
            "SELECT "
            "COUNT(*) as total_trades, "
            "COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades, "
            "COUNT(CASE WHEN profit < 0 THEN 1 END) as losing_trades, "
            "COALESCE(SUM(volume), 0) as total_volume, "
            "COALESCE(SUM(profit), 0) as net_profit "
            "FROM trades WHERE timestamp >= :date_start AND timestamp < :date_end "
            "AND status = 'success'",
            {"date_start": date + " 00:00:00", "date_end": next_day + " 00:00:00"},
        )
        if rows and rows[0].get("total_trades", 0) > 0:
            r = rows[0]
            total = r["total_trades"]
            return {
                "date": date,
                "total_trades": total,
                "winning_trades": r["winning_trades"],
                "losing_trades": r["losing_trades"],
                "net_profit": r["net_profit"],
                "win_rate": (r["winning_trades"] / total) * 100 if total > 0 else 0,
            }
        return {}

    def get_symbol_performance(self) -> list[dict]:
        """Get performance metrics for all traded symbols."""
        # Try cached table first
        rows = self.execute_query(
            "SELECT symbol, total_trades, winning_trades, total_volume, "
            "net_profit, win_rate, profit_factor, average_profit, "
            "best_trade, worst_trade, last_updated "
            "FROM symbol_performance ORDER BY net_profit DESC"
        )
        if rows:
            return rows
        # Compute on the fly from trades table
        return self.execute_query(
            "SELECT symbol, "
            "COUNT(*) as total_trades, "
            "COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades, "
            "COALESCE(SUM(volume), 0) as total_volume, "
            "COALESCE(SUM(profit), 0) as net_profit, "
            "CASE WHEN COUNT(*) > 0 "
            "THEN CAST(COUNT(CASE WHEN profit > 0 THEN 1 END) AS REAL) / COUNT(*) * 100 "
            "ELSE 0 END as win_rate, "
            "COALESCE(AVG(profit), 0) as average_profit, "
            "COALESCE(MAX(profit), 0) as best_trade, "
            "COALESCE(MIN(profit), 0) as worst_trade "
            "FROM trades WHERE status = 'success' "
            "GROUP BY symbol ORDER BY net_profit DESC"
        )

    def get_alert_statistics(self, hours: int = 24) -> dict:
        """Get webhook alert statistics.

        Returns a dict matching PostgresDatabaseManager format:
        ``{"total_alerts", "unique_ips", "successful", "failed", "rate_limited",
           "avg_execution_time", "top_symbols", "top_ips"}``.
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self._conn.execute(
            "SELECT "
            "COUNT(*) as total_alerts, "
            "COUNT(DISTINCT ip_address) as unique_ips, "
            "COUNT(CASE WHEN response_code = 200 THEN 1 END) as successful, "
            "COUNT(CASE WHEN response_code != 200 THEN 1 END) as failed, "
            "COUNT(CASE WHEN rate_limited = 1 THEN 1 END) as rate_limited, "
            "COALESCE(AVG(execution_time_ms), 0) as avg_execution_time "
            "FROM alert_history WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        stats = dict(row) if row else {}
        for key in ("total_alerts", "unique_ips", "successful", "failed", "rate_limited"):
            stats.setdefault(key, 0)
        stats.setdefault("avg_execution_time", 0)
        # Top symbols
        symbol_rows = self._conn.execute(
            "SELECT symbol, COUNT(*) as count FROM alert_history "
            "WHERE timestamp >= ? AND symbol IS NOT NULL "
            "GROUP BY symbol ORDER BY count DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
        stats["top_symbols"] = [dict(r) for r in symbol_rows]
        # Top IPs
        ip_rows = self._conn.execute(
            "SELECT ip_address, COUNT(*) as count FROM alert_history "
            "WHERE timestamp >= ? GROUP BY ip_address "
            "ORDER BY count DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
        stats["top_ips"] = [dict(r) for r in ip_rows]
        return stats

    def _update_symbol_performance(self, symbol: str) -> None:
        """Update symbol performance metrics after a trade."""
        if not symbol:
            return
        with _write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO symbol_performance "
                "(symbol, total_trades, winning_trades, total_volume, "
                "net_profit, win_rate, profit_factor, average_profit, "
                "best_trade, worst_trade, last_updated) "
                "SELECT ?, "
                "COUNT(*), "
                "COUNT(CASE WHEN profit > 0 THEN 1 END), "
                "COALESCE(SUM(volume), 0), "
                "COALESCE(SUM(profit), 0), "
                "CASE WHEN COUNT(*) > 0 "
                "THEN CAST(COUNT(CASE WHEN profit > 0 THEN 1 END) AS REAL) / COUNT(*) * 100 "
                "ELSE 0 END, "
                "CASE WHEN COALESCE(SUM(CASE WHEN profit < 0 THEN profit END), 0) != 0 "
                "THEN ABS(COALESCE(SUM(CASE WHEN profit > 0 THEN profit END), 0) / "
                "SUM(CASE WHEN profit < 0 THEN profit END)) ELSE 0 END, "
                "COALESCE(AVG(profit), 0), "
                "COALESCE(MAX(profit), 0), "
                "COALESCE(MIN(profit), 0), "
                "datetime('now') "
                "FROM trades WHERE symbol = ? AND status = 'success'",
                (symbol, symbol),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # SQL helper properties (match PostgresDatabaseManager interface)
    # ------------------------------------------------------------------

    @property
    def dialect(self) -> str:
        return "sqlite"

    @staticmethod
    def sql_now() -> str:
        return "datetime('now')"

    @staticmethod
    def sql_today() -> str:
        return "date('now')"

    @staticmethod
    def sql_interval_days(days: int) -> str:
        return f"datetime('now', '-{int(days)} days')"

    @staticmethod
    def sql_interval_hours(hours: int) -> str:
        return f"datetime('now', '-{int(hours)} hours')"

    @staticmethod
    def sql_interval_minutes(minutes: int) -> str:
        return f"datetime('now', '-{int(minutes)} minutes')"

    _SAFE_COLUMNS = frozenset(
        {
            "id",
            "license_key",
            "signal_data",
            "status",
            "created_at",
            "received_at",
            "action",
            "symbol",
            "command",
            "type",
            "payload",
            "acknowledged",
        }
    )
    _SAFE_JSON_KEYS = frozenset({"action", "symbol", "command", "type"})

    @staticmethod
    def sql_json_extract(column: str, key: str) -> str:
        if column not in SqliteDatabaseManager._SAFE_COLUMNS:
            raise ValueError(f"Unsafe column name: {column}")
        if key not in SqliteDatabaseManager._SAFE_JSON_KEYS:
            raise ValueError(f"Unsafe JSON key: {key}")
        return f"json_extract({column}, '$.{key}')"

    @staticmethod
    def sql_age_seconds(created_col: str) -> str:
        if created_col not in SqliteDatabaseManager._SAFE_COLUMNS:
            raise ValueError(f"Unsafe column name: {created_col}")
        return f"(julianday(datetime('now')) - julianday({created_col})) * 86400"

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    async def close_async(self) -> None:
        """Close database connection."""
        self._sa_engine.dispose()
        self._conn.close()
        logger.info("SQLite connection closed")

    def close(self) -> None:
        """Close database connection."""
        self._sa_engine.dispose()
        self._conn.close()
        logger.info("SQLite connection closed")


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def create_database_manager(database_url: str | None = None, **kwargs: Any) -> Any:
    """Factory: return SQLite manager if no DATABASE_URL, else PostgreSQL manager.

    If DATABASE_URL is set (PostgreSQL), returns PostgresDatabaseManager.
    If DATABASE_URL is None/empty, returns SqliteDatabaseManager using data_dir.
    """
    if database_url and (
        database_url.startswith("postgresql://")
        or database_url.startswith("postgresql+asyncpg://")
        or database_url.startswith("postgresql+psycopg://")
    ):
        from apps.server.db.postgres import create_database_manager as _pg

        return _pg(database_url, **kwargs)

    # SQLite fallback
    data_dir = kwargs.pop("data_dir", None)
    if data_dir:
        db_path = str(Path(data_dir) / "pinetunnel.db")
    else:
        db_path = str(Path.cwd() / "pinetunnel.db")
    return SqliteDatabaseManager(db_path=db_path, **kwargs)

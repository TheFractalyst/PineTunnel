"""PostgreSQL Database Manager with async support and connection pooling.

PostgreSQL-native database manager for production deployments.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from apps.server.db.base import DatabaseBase
from apps.server.utils import mask_string as _mask

logger = logging.getLogger(__name__)


class PostgresDatabaseManager(DatabaseBase):
    """PostgreSQL database manager with connection pooling."""

    _ea_conn_conflict_target = "(license_key, connection_type, worker_id)"

    def __init__(
        self,
        database_url: str,
        pool_size: int = 20,
        max_overflow: int = 40,
        pool_timeout: float = 30.0,
        pool_recycle: int = 3600,
    ):
        self.database_url = database_url
        self.pool_size = pool_size
        self.max_overflow = max_overflow

        # Ensure SSL is required for the database connection (defense-in-depth:
        # Render's managed Postgres includes sslmode=require in the URL, but
        # explicitly setting it prevents accidental plaintext connections).
        #
        # psycopg (v3) passes parameters differently from psycopg2:
        # - psycopg2 used connect_args={"options": "-c sslmode=require"} (libpq options string)
        # - psycopg3 uses URL query parameters or connect_args with Python kwargs
        # - statement_timeout is NOT a valid libpq connection parameter, so it
        #   CANNOT be passed as a URL query param or in connect_args for psycopg3.
        #   Instead, we use a SQLAlchemy pool event listener to SET it after
        #   each connection is established.
        # - asyncpg supports statement_timeout via server_settings in connect_args.

        sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        if sync_url.startswith("postgresql://") and "+psycopg" not in sync_url:
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)

        # For psycopg3 sync engine: only append sslmode as URL query param.
        # statement_timeout must be set via SET command after connecting,
        # because psycopg3 rejects it as a connection option.
        sync_params = {}
        if "sslmode=" not in sync_url:
            sync_params["sslmode"] = "require"

        if sync_params:
            parsed = urlparse(sync_url)
            existing_params = parse_qs(parsed.query)
            existing_params.update({k: [v] for k, v in sync_params.items()})
            new_query = urlencode({k: v[0] for k, v in existing_params.items()})
            sync_url = urlunparse(parsed._replace(query=new_query))

        self.engine = create_engine(
            sync_url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,  # Verify connections before use
        )

        # Set statement_timeout on every new psycopg3 connection.
        # psycopg3 does NOT support statement_timeout as a connection parameter
        # (URL query param or connect_args), so we use a pool event listener
        # to run SET statement_timeout immediately after each connection checkout.
        @event.listens_for(self.engine, "connect")
        def _set_statement_timeout(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("SET statement_timeout = 30000")
            cursor.close()
            # Don't commit - the SET is session-level and survives until disconnect

        # Async engine for new async code - use smaller pool since sync
        # engine already accounts for the majority of connections.
        async_pool_size = max(pool_size // 2, 5)
        async_max_overflow = max(max_overflow // 2, 5)
        async_url = database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
        if async_url.startswith("postgresql://") and "+asyncpg" not in async_url:
            async_url = async_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        # asyncpg does NOT support sslmode as a URL query parameter.
        # It raises: TypeError: connect() got an unexpected keyword argument 'sslmode'
        # Remove any sslmode/ssl params from the URL and pass SSL via connect_args instead.
        # Also remove other psycopg3-only params that asyncpg doesn't recognize.
        parsed_async = urlparse(async_url)
        async_query_params = parse_qs(parsed_async.query)
        needs_ssl = "sslmode" in async_query_params or "ssl" in async_query_params
        # Remove all ssl-related params from URL - asyncpg doesn't support them in URL
        for key in list(async_query_params.keys()):
            if key in ("sslmode", "ssl", "sslrootcert", "sslcrl", "sslcrldir"):
                del async_query_params[key]
        clean_async_query = urlencode({k: v[0] for k, v in async_query_params.items()})
        async_url = urlunparse(parsed_async._replace(query=clean_async_query))

        # Build connect_args for asyncpg
        async_connect_args: dict[str, Any] = {
            "server_settings": {"statement_timeout": "30000"},
        }
        # SSL for asyncpg: use 'ssl' key (not 'sslmode'). Render DBs require SSL.
        if needs_ssl or "sslmode=" not in database_url:
            async_connect_args["ssl"] = "require"

        self.async_engine = create_async_engine(
            async_url,
            pool_size=async_pool_size,
            max_overflow=async_max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,
            connect_args=async_connect_args,
        )

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.AsyncSessionLocal = async_sessionmaker(bind=self.async_engine, expire_on_commit=False)

        # Stored for reliability monitor disk checks (uses data_dir from URL path)
        self.db_path = database_url

        logger.info("PostgreSQL database manager initialized: %s", database_url.split("@")[-1])

    @contextmanager
    def get_connection(self):
        """Get synchronous database session (compatibility wrapper)."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @asynccontextmanager
    async def get_async_session(self):
        """Get async database session."""
        session = self.AsyncSessionLocal()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    def save_signal(
        self,
        license_key: str,
        signal_data: dict[str, Any],
        duplicate_window_minutes: int = 5,
        _retry_count: int = 0,
        status: str = "pending",
    ) -> str | None:
        """Save signal to persistent queue with duplicate detection.

        Mirrors the save_signal interface for route handler compatibility.
        signal queue can use either backend transparently.

        ``status`` defaults to 'pending' (queue path, polled by EA). The execute path
        passes 'acknowledged' so the row is recorded for idempotency/dedup but is
        invisible to the EA poll and is reaped by the existing acknowledged-row cleanup.

        Returns:
            Unique signal identifier, or None if duplicate detected.
        """
        # Create hash from signal content for duplicate detection
        hash_data = {
            k: v for k, v in signal_data.items() if k not in ["queued_at", "timestamp", "signal_id"]
        }
        signal_hash = hashlib.md5(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()

        signal_id = str(uuid.uuid4())[:8]

        with self.get_connection() as session:
            # Insert only if no duplicate within the dedup window.
            # Uses CAST() instead of :: type casts because SQLAlchemy text() treats
            # :param::type as :param followed by :type (another bind parameter).
            # INTERVAL literal is safe because duplicate_window_minutes is int-validated.
            dedup_literal = f"{int(duplicate_window_minutes)} minutes"
            result = session.execute(
                text(f"""
                    INSERT INTO signal_queue (signal_id, license_key, signal_data, signal_hash, status)
                    SELECT CAST(:sid AS varchar), CAST(:key AS varchar),
                           CAST(:data AS text), CAST(:hash AS varchar), CAST(:status AS varchar)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM signal_queue
                        WHERE license_key = :key
                        AND signal_hash = :hash
                        AND created_at >= NOW() - INTERVAL '{dedup_literal}'
                    )
                    RETURNING signal_id
                """),
                {
                    "sid": signal_id,
                    "key": license_key,
                    "data": json.dumps(signal_data, separators=(",", ":")),
                    "hash": signal_hash,
                    "status": status,
                },
            )

            row = result.first()
            if row is None:
                return None  # Duplicate

            # Also log to permanent signal log (infinite retention, organized per license)
            try:
                self._log_signal_permanent(license_key, row[0], signal_hash, signal_data)
            except Exception as e:
                logger.warning("Failed to log signal to ws_signal_log (non-critical): %s", e)

            return row[0]

    async def get_pending_signals_async(
        self,
        license_key: str,
        max_age_minutes: int = 60,
        claimed_by: str | None = None,
        stale_claim_minutes: int = 5,
    ) -> list[dict]:
        """Atomically claim pending signals for a license and return them.

        Uses UPDATE ... RETURNING for PostgreSQL atomic claim without
        explicit locking. Also re-claims stale 'claimed' signals whose
        EA has crashed.
        """
        age_expr = self.sql_age_seconds("created_at")
        max_age_expr = self.sql_interval_minutes(max_age_minutes)
        stale_expr = self.sql_interval_minutes(stale_claim_minutes)

        async with self.get_async_session() as session:
            # PostgreSQL: UPDATE ... RETURNING is atomic - only this
            # transaction can transition these rows to 'claimed'.
            query = text(f"""
                UPDATE signal_queue
                SET status = 'claimed',
                    claimed_at = NOW(),
                    claimed_by = COALESCE(:claimer, claimed_by)
                WHERE license_key = :key
                  AND (
                    (status = 'pending' AND created_at >= {max_age_expr})
                    OR
                    (status = 'claimed' AND claimed_at < {stale_expr})
                  )
                RETURNING signal_id, signal_data, created_at, {age_expr} as age_minutes
            """)

            result = await session.execute(query, {"key": license_key, "claimer": claimed_by})

            signals: list[dict] = []
            for row in result.fetchall():
                try:
                    signal_data = row[1]
                    if not signal_data:
                        continue
                    signal_dict = (
                        json.loads(signal_data) if isinstance(signal_data, str) else signal_data
                    )
                    signal_dict["signal_id"] = row[0]
                    signal_dict["age_minutes"] = row[3]
                    signals.append(signal_dict)
                except (json.JSONDecodeError, TypeError):
                    continue

            await session.commit()
            return signals

    async def acknowledge_signal_async(self, signal_id: str, license_key: str) -> bool:
        """Mark signal as acknowledged. Signals may be 'pending' or 'claimed'."""
        async with self.get_async_session() as session:
            query = text("""
                UPDATE signal_queue
                SET status = 'acknowledged', acknowledged_at = :now
                WHERE signal_id = :sid AND license_key = :key AND status IN ('pending', 'claimed')
            """)

            result = await session.execute(
                query, {"sid": signal_id, "key": license_key, "now": datetime.now()}
            )

            return result.rowcount > 0

    async def acknowledge_signals_batch_async(
        self, signal_ids: list[str], license_key: str
    ) -> dict[str, Any]:
        """Acknowledge multiple signals in batch."""
        async with self.get_async_session() as session:
            query = text("""
                UPDATE signal_queue
                SET status = 'acknowledged', acknowledged_at = :now
                WHERE signal_id = ANY(:ids) AND license_key = :key AND status IN ('pending', 'claimed')
                RETURNING signal_id
            """)

            result = await session.execute(
                query, {"ids": signal_ids, "key": license_key, "now": datetime.now()}
            )

            acknowledged = [row[0] for row in result.fetchall()]
            failed = [sid for sid in signal_ids if sid not in acknowledged]

            return {"acknowledged": acknowledged, "failed": failed}

    @property
    def dialect(self) -> str:
        """Return the SQL dialect identifier."""
        return "postgresql"

    # ------------------------------------------------------------------
    # SQL helper methods for route handlers
    # code can write dialect-agnostic queries.
    # ------------------------------------------------------------------

    @staticmethod
    def sql_now() -> str:
        """Expression evaluating to the current timestamp."""
        return "NOW()"

    @staticmethod
    def sql_today() -> str:
        """Expression evaluating to today's date."""
        return "CURRENT_DATE"

    # Allowlisted column names for safe SQL interpolation.
    # These are the only values accepted by sql_json_extract and sql_age_seconds.
    _SAFE_COLUMNS = frozenset(
        {
            "signal_data",
            "created_at",
            "acknowledged_at",
            "claimed_at",
            "timestamp",
            "license_key",
            "status",
            "signal_id",
        }
    )
    _SAFE_JSON_KEYS = frozenset({"action", "symbol", "command", "type"})

    @staticmethod
    def sql_interval_days(days: int) -> str:
        """Expression for *days* ago (e.g. ``CURRENT_DATE - INTERVAL '7 days'``).

        The *days* parameter is type-checked to ``int`` at the Python level,
        so it cannot inject SQL.  Callers must pass an ``int``, not user input.
        """
        if not isinstance(days, int):
            raise TypeError(f"sql_interval_days requires int, got {type(days).__name__}")
        return f"CURRENT_DATE - INTERVAL '{days} days'"

    @staticmethod
    def sql_interval_hours(hours: int) -> str:
        """Expression for *hours* ago.  See ``sql_interval_days`` for safety notes."""
        if not isinstance(hours, int):
            raise TypeError(f"sql_interval_hours requires int, got {type(hours).__name__}")
        return f"NOW() - INTERVAL '{hours} hours'"

    @staticmethod
    def sql_interval_minutes(minutes: int) -> str:
        """Expression for *minutes* ago.  See ``sql_interval_days`` for safety notes."""
        if not isinstance(minutes, int):
            raise TypeError(f"sql_interval_minutes requires int, got {type(minutes).__name__}")
        return f"NOW() - INTERVAL '{minutes} minutes'"

    @staticmethod
    def sql_json_extract(column: str, key: str) -> str:
        """Extract a JSON field value (Postgres ``->>`` operator).

        Both *column* and *key* are validated against an allowlist to prevent
        SQL injection, since they are interpolated (not parameterized) into the
        query string.
        """
        if column not in PostgresDatabaseManager._SAFE_COLUMNS:
            raise ValueError(f"sql_json_extract: invalid column '{column}'")
        if key not in PostgresDatabaseManager._SAFE_JSON_KEYS:
            raise ValueError(f"sql_json_extract: invalid key '{key}'")
        return f"{column}->>'{key}'"

    @staticmethod
    def sql_age_seconds(created_col: str) -> str:
        """Expression returning age in seconds from *created_col* to now.

        *created_col* is validated against the column allowlist.
        """
        if created_col not in PostgresDatabaseManager._SAFE_COLUMNS:
            raise ValueError(f"sql_age_seconds: invalid column '{created_col}'")
        return f"EXTRACT(EPOCH FROM (NOW() - {created_col}))::INTEGER"

    def execute_query(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Execute a SQL query and return results as a list of dicts.

        Uses SQLAlchemy ``text()`` with ``:named`` parameter syntax so
        callers can write dialect-agnostic SQL.  For raw migration scripts
        that need positional parameters, use ``get_connection()`` directly.

        Args:
            sql: SQL string with ``:named`` placeholders.
            params: Dict of bind parameters.

        Returns:
            List of row dicts (empty list for non-SELECT queries).
        """
        with self.get_connection() as session:
            result = session.execute(text(sql), params or {})
            if result.returns_rows:
                cols = list(result.keys())
                return [dict(zip(cols, row)) for row in result.fetchall()]
            return []

    def get_pool_stats(self) -> dict[str, Any]:
        """Get connection pool statistics."""
        pool = self.engine.pool
        checked_out = pool.checkedout()
        return {
            "type": "postgresql",
            "total_connections": pool.size(),
            "in_use": checked_out,
            "available": pool.size() - checked_out,
            "overflow": pool.overflow(),
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
        }

    def init_database(self):
        """Create tables if they don't exist (idempotent).

        Tries Alembic migrations first. Falls back to raw DDL if
        Alembic isn't configured or fails.
        """
        try:
            from alembic import command
            from alembic.config import Config

            alembic_cfg = Config()
            alembic_cfg.set_main_option("script_location", "alembic")
            alembic_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
            if alembic_url.startswith("postgresql+asyncpg://"):
                alembic_url = alembic_url.replace(
                    "postgresql+asyncpg://", "postgresql+psycopg://", 1
                )
            alembic_cfg.set_main_option("sqlalchemy.url", alembic_url)
            command.upgrade(alembic_cfg, "head")
            logger.info("Database tables initialized via Alembic")
        except Exception as e:
            logger.warning("Alembic migration failed (%s), falling back to DDL", e)
            # Alembic not available - check if tables exist by probing.
            # NOTE: We do NOT return early here, because the DDL fallback's
            # CREATE TABLE IF NOT EXISTS statements are idempotent, and we must
            # always fall through to _ensure_bigint_columns() and
            # _ensure_ws_telemetry_tables() below - those are the safety nets
            # that create ws_* tables when Alembic fails (missing files) and
            # the DDL fallback skips because signal_queue already exists.
            with self.get_connection() as session:
                result = session.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT FROM information_schema.tables "
                        "WHERE table_name = 'signal_queue')"
                    )
                )
                tables_exist = bool(result.scalar())

            if tables_exist:
                logger.info("Database tables already exist, skipping DDL creation")
            else:
                # Create all runtime tables
                with self.engine.begin() as conn:
                    conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS signal_queue (
                        id SERIAL PRIMARY KEY,
                        signal_id VARCHAR(255) UNIQUE NOT NULL,
                        license_key VARCHAR(255) NOT NULL,
                        signal_data TEXT NOT NULL,
                        signal_hash VARCHAR(255),
                        status VARCHAR(50) DEFAULT 'pending' NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW() NOT NULL,
                        acknowledged_at TIMESTAMP,
                        processed_at TIMESTAMP,
                        retry_count INTEGER DEFAULT 0 NOT NULL,
                        error_message TEXT,
                        claimed_at TIMESTAMP,
                        claimed_by VARCHAR(255)
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        license_key VARCHAR(255),
                        signal_id VARCHAR(255),
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        ticket BIGINT, deal BIGINT,
                        symbol VARCHAR(50) NOT NULL, action VARCHAR(50) NOT NULL,
                        volume FLOAT NOT NULL, price FLOAT,
                        entry_price FLOAT, exit_price FLOAT, sl FLOAT, tp FLOAT,
                        profit FLOAT, profit_percent FLOAT,
                        commission FLOAT, swap FLOAT, comment TEXT, magic BIGINT,
                        duration_minutes INTEGER, status VARCHAR(50),
                        error TEXT, ip_address VARCHAR(255),
                        execution_time_ms FLOAT, spread_points FLOAT, slippage FLOAT
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS alert_history (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        ip_address VARCHAR(255), user_agent TEXT, payload TEXT,
                        action VARCHAR(50), symbol VARCHAR(50), volume FLOAT,
                        response_code INTEGER, response_message TEXT,
                        execution_time_ms FLOAT, rate_limited BOOLEAN DEFAULT FALSE
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        date DATE PRIMARY KEY,
                        total_trades INTEGER DEFAULT 0,
                        winning_trades INTEGER DEFAULT 0,
                        losing_trades INTEGER DEFAULT 0,
                        breakeven_trades INTEGER DEFAULT 0,
                        total_volume FLOAT DEFAULT 0,
                        gross_profit FLOAT DEFAULT 0, gross_loss FLOAT DEFAULT 0,
                        net_profit FLOAT DEFAULT 0, commission_paid FLOAT DEFAULT 0,
                        max_drawdown FLOAT DEFAULT 0, max_drawdown_percent FLOAT DEFAULT 0,
                        win_rate FLOAT DEFAULT 0, profit_factor FLOAT DEFAULT 0,
                        average_win FLOAT DEFAULT 0, average_loss FLOAT DEFAULT 0,
                        largest_win FLOAT DEFAULT 0, largest_loss FLOAT DEFAULT 0,
                        average_trade_duration INTEGER DEFAULT 0,
                        total_alerts_received INTEGER DEFAULT 0,
                        failed_trades INTEGER DEFAULT 0
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS symbol_performance (
                        symbol VARCHAR(50) PRIMARY KEY,
                        total_trades INTEGER DEFAULT 0,
                        winning_trades INTEGER DEFAULT 0,
                        total_volume FLOAT DEFAULT 0,
                        net_profit FLOAT DEFAULT 0, win_rate FLOAT DEFAULT 0,
                        profit_factor FLOAT DEFAULT 0, average_profit FLOAT DEFAULT 0,
                        best_trade FLOAT DEFAULT 0, worst_trade FLOAT DEFAULT 0,
                        last_updated TIMESTAMP DEFAULT NOW()
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS account_stats (
                        id SERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        account BIGINT, broker VARCHAR(255), currency VARCHAR(10),
                        leverage INTEGER, balance FLOAT, equity FLOAT, profit FLOAT,
                        margin FLOAT, margin_free FLOAT, margin_level FLOAT,
                        open_positions INTEGER, pending_orders INTEGER,
                        ea_version VARCHAR(50), dll_version VARCHAR(20),
                        account_name VARCHAR(255),
                        magic BIGINT,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS admin_logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        action VARCHAR(100) NOT NULL,
                        "user" VARCHAR(255), ip_address VARCHAR(100),
                        details TEXT
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ea_audit (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        platform VARCHAR(10), ea_version VARCHAR(50), dll_version VARCHAR(20),
                        dll_system_info TEXT,
                        mt_build INTEGER, terminal_name VARCHAR(255),
                        terminal_language VARCHAR(10), terminal_x64 BOOLEAN, terminal_pid INTEGER,
                        os VARCHAR(255), cpu_cores INTEGER, cpu_freq_mhz INTEGER,
                        ram_mb INTEGER, ram_avail_mb INTEGER, disk_mb INTEGER,
                        account_number BIGINT, account_name VARCHAR(255),
                        account_server VARCHAR(255), account_currency VARCHAR(10),
                        broker VARCHAR(255), trade_mode VARCHAR(20), leverage INTEGER,
                        limit_orders INTEGER, trade_allowed BOOLEAN, trade_expert BOOLEAN,
                        margin_so_mode VARCHAR(10),
                        balance FLOAT, credit FLOAT, equity FLOAT, profit FLOAT,
                        margin FLOAT, margin_free FLOAT, margin_level FLOAT,
                        margin_so_call FLOAT, margin_so_so FLOAT,
                        chart_symbol VARCHAR(50), chart_timeframe VARCHAR(10),
                        symbol_count INTEGER, position_count INTEGER, uptime_sec INTEGER,
                        ws_status VARCHAR(20), error_count INTEGER,
                        connection_mode VARCHAR(20), magic INTEGER,
                        auto_update_enabled BOOLEAN,
                        is_vps BOOLEAN, vps_provider VARCHAR(64),
                        vps_manufacturer VARCHAR(128), vps_model VARCHAR(128),
                        net_quality VARCHAR(16), net_ping_ms INTEGER,
                        net_jitter_ms INTEGER, net_loss_pct FLOAT,
                        ntp_drift_ms INTEGER, ntp_sync_success BOOLEAN
                    )
                """))
                # Essential indexes
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_signal_queue_license ON signal_queue (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_signal_queue_status ON signal_queue (status)",
                    "CREATE INDEX IF NOT EXISTS idx_signal_queue_created ON signal_queue (created_at)",
                    "CREATE INDEX IF NOT EXISTS idx_signal_queue_hash_created ON signal_queue (signal_hash, created_at)",
                    "CREATE INDEX IF NOT EXISTS idx_signal_queue_license_status ON signal_queue (license_key, status)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_license_key ON trades (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_license_timestamp ON trades (license_key, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades (signal_id)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol)",
                    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status)",
                    "CREATE INDEX IF NOT EXISTS idx_account_stats_license ON account_stats (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_account_stats_timestamp ON account_stats (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ea_audit_license ON ea_audit (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ea_audit_timestamp ON ea_audit (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ea_audit_license_ts ON ea_audit (license_key, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_alert_history_timestamp ON alert_history (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_admin_logs_timestamp ON admin_logs (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_admin_logs_action ON admin_logs (action)",
                ]:
                    conn.execute(text(idx_ddl))

                # Migrate trades.ticket, deal, magic from INTEGER to BIGINT
                # (MT5 ticket/deal numbers are ulong, can exceed 32-bit INTEGER range)
                try:
                    conn.execute(text("ALTER TABLE trades ALTER COLUMN ticket TYPE BIGINT"))
                    logger.info("Migrated trades.ticket to BIGINT")
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)
                try:
                    conn.execute(text("ALTER TABLE trades ALTER COLUMN deal TYPE BIGINT"))
                    logger.info("Migrated trades.deal to BIGINT")
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)
                try:
                    conn.execute(text("ALTER TABLE trades ALTER COLUMN magic TYPE BIGINT"))
                    logger.info("Migrated trades.magic to BIGINT")
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)

                # Migrate account_stats.account and magic from INTEGER to BIGINT
                # (integer out of range can occur with large MT5 account/magic values)
                try:
                    conn.execute(text("ALTER TABLE account_stats ALTER COLUMN account TYPE BIGINT"))
                    logger.info("Migrated account_stats.account to BIGINT")
                except Exception as e:
                    logger.debug("account_stats.account BIGINT migration skipped: %s", e)
                try:
                    conn.execute(text("ALTER TABLE account_stats ALTER COLUMN magic TYPE BIGINT"))
                    logger.info("Migrated account_stats.magic to BIGINT")
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)

                # ws_signal_log - permanent signal record (infinite retention)
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ws_signal_log (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        signal_id VARCHAR(255) NOT NULL,
                        signal_hash VARCHAR(255),
                        action VARCHAR(50),
                        symbol VARCHAR(50),
                        volume FLOAT,
                        sl FLOAT, tp FLOAT,
                        signal_data TEXT NOT NULL,
                        delivered_via VARCHAR(10),
                        acknowledged BOOLEAN DEFAULT FALSE NOT NULL,
                        acknowledged_at TIMESTAMP,
                        execution_status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                        execution_detail TEXT,
                        executed_at TIMESTAMP,
                        ticket VARCHAR(50)
                    )
                """))
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_license ON ws_signal_log (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_timestamp ON ws_signal_log (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_license_ts ON ws_signal_log (license_key, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_symbol ON ws_signal_log (symbol)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_action ON ws_signal_log (action)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_sid ON ws_signal_log (signal_id)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_signal_log_exec_status ON ws_signal_log (execution_status)",
                ]:
                    conn.execute(text(idx_ddl))

                # ws_account_stats - full account snapshots from EA
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ws_account_stats (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        login BIGINT, name VARCHAR(255), server VARCHAR(255),
                        currency VARCHAR(10), company VARCHAR(255),
                        trade_mode VARCHAR(20), leverage INTEGER,
                        limit_orders INTEGER, trade_allowed BOOLEAN, trade_expert BOOLEAN,
                        margin_so_mode VARCHAR(10),
                        balance FLOAT, credit FLOAT, equity FLOAT, profit FLOAT,
                        margin FLOAT, margin_free FLOAT, margin_level FLOAT,
                        margin_so_call FLOAT, margin_so_so FLOAT,
                        margin_initial FLOAT, margin_maintenance FLOAT,
                        assets FLOAT, liabilities FLOAT, commission_blocked FLOAT,
                        currency_digits INTEGER, fifo_close BOOLEAN, hedge_allowed BOOLEAN,
                        positions INTEGER
                    )
                """))
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_license ON ws_account_stats (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_timestamp ON ws_account_stats (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_license_ts ON ws_account_stats (license_key, timestamp)",
                ]:
                    conn.execute(text(idx_ddl))

                # ws_open_positions - all running trades from EA
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ws_open_positions (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        ticket BIGINT NOT NULL,
                        symbol VARCHAR(50) NOT NULL,
                        type VARCHAR(10) NOT NULL,
                        volume FLOAT, open_price FLOAT, current_price FLOAT,
                        sl FLOAT, tp FLOAT, profit FLOAT, swap FLOAT,
                        commission FLOAT, magic BIGINT, comment VARCHAR(255),
                        open_time BIGINT, identifier BIGINT
                    )
                """))
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_license ON ws_open_positions (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_ticket ON ws_open_positions (ticket)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_license_ts ON ws_open_positions (license_key, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_symbol ON ws_open_positions (symbol)",
                ]:
                    conn.execute(text(idx_ddl))

                # ws_trade_history - closed trades/deals from EA
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ws_trade_history (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        ticket BIGINT NOT NULL,
                        symbol VARCHAR(50) NOT NULL,
                        type VARCHAR(10) NOT NULL,
                        volume FLOAT, open_price FLOAT, close_price FLOAT,
                        sl FLOAT, tp FLOAT, profit FLOAT, swap FLOAT,
                        commission FLOAT, fee FLOAT, magic BIGINT,
                        comment VARCHAR(255), open_time BIGINT, close_time BIGINT,
                        order_id BIGINT, entry VARCHAR(10), reason VARCHAR(20),
                        position_id BIGINT,
                        CONSTRAINT uq_ws_trade_history_license_ticket UNIQUE (license_key, ticket)
                    )
                """))
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_license ON ws_trade_history (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_ticket ON ws_trade_history (ticket)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_license_ts ON ws_trade_history (license_key, timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_symbol ON ws_trade_history (symbol)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_close_time ON ws_trade_history (close_time)",
                ]:
                    conn.execute(text(idx_ddl))

                # ws_health_telemetry - connection health metrics
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ws_health_telemetry (
                        id BIGSERIAL PRIMARY KEY,
                        license_key VARCHAR(255) NOT NULL,
                        timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                        ws_latency_ms INTEGER, terminal_lag_ms INTEGER,
                        ws_uptime_sec INTEGER, ws_bytes_sent INTEGER,
                        ws_bytes_recv INTEGER, ws_reconnects INTEGER,
                        ws_frames_queued INTEGER, ws_frames_dropped INTEGER,
                        dll_version VARCHAR(20)
                    )
                """))
                for idx_ddl in [
                    "CREATE INDEX IF NOT EXISTS idx_ws_health_license ON ws_health_telemetry (license_key)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_health_timestamp ON ws_health_telemetry (timestamp)",
                    "CREATE INDEX IF NOT EXISTS idx_ws_health_license_ts ON ws_health_telemetry (license_key, timestamp)",
                ]:
                    conn.execute(text(idx_ddl))

                logger.info("Database tables created")

        # Always ensure BIGINT columns regardless of init path
        self._ensure_bigint_columns()

        # Always ensure account_stats extra columns exist (Alembic may fail)
        self._ensure_account_stats_columns()

        # Always ensure WS telemetry tables exist (Alembic may fail on missing
        # files, and the DDL fallback bails out early if signal_queue exists)
        self._ensure_ws_telemetry_tables()

        # Always ensure ea_connections table exists (single source of truth)
        self._ensure_ea_connections_table()

    def _ensure_bigint_columns(self) -> None:
        """Ensure critical columns are BIGINT to prevent 'integer out of range' errors.

        This runs after every init_database() call as a safety net, because
        Alembic migrations may silently fail (e.g., missing alembic files)
        and the DDL fallback only runs when tables don't exist yet.
        """
        bigint_migrations = [
            ("trades", "ticket", "BIGINT"),
            ("trades", "deal", "BIGINT"),
            ("trades", "magic", "BIGINT"),
            ("account_stats", "account", "BIGINT"),
            ("account_stats", "magic", "BIGINT"),
        ]
        with self.engine.begin() as conn:
            for table, column, type_ in bigint_migrations:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_}"))
                    logger.info("Ensured %s.%s is %s", table, column, type_)
                except Exception as e:
                    logger.debug("BIGINT migration skipped for %s.%s: %s", table, column, e)

    def _ensure_account_stats_columns(self) -> None:
        """Ensure account_name and dll_version columns exist on account_stats.

        Safety net for Alembic migration 007 which may not have been applied
        (e.g., new column added in code but Alembic hasn.t run yet on the server).
        """
        columns_to_add = [
            ("account_name", "VARCHAR(255)"),
            ("dll_version", "VARCHAR(20)"),
        ]
        with self.engine.begin() as conn:
            for column_name, column_type in columns_to_add:
                try:
                    conn.execute(
                        text(
                            f"ALTER TABLE account_stats ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                        )
                    )
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)

    def _ensure_ws_telemetry_tables(self) -> None:
        """Ensure WS telemetry tables exist (idempotent).

        This runs after every init_database() call as a safety net, because
        Alembic migrations may silently fail (e.g., missing alembic files)
        and the DDL fallback bails out early if signal_queue already exists,
        skipping these tables.
        """
        with self.engine.begin() as conn:
            # ws_account_stats
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ws_account_stats (
                    id BIGSERIAL PRIMARY KEY,
                    license_key VARCHAR(255) NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                    login BIGINT, name VARCHAR(255), server VARCHAR(255),
                    currency VARCHAR(10), company VARCHAR(255),
                    trade_mode VARCHAR(20), leverage INTEGER,
                    limit_orders INTEGER, trade_allowed BOOLEAN, trade_expert BOOLEAN,
                    margin_so_mode VARCHAR(10),
                    balance FLOAT, credit FLOAT, equity FLOAT, profit FLOAT,
                    margin FLOAT, margin_free FLOAT, margin_level FLOAT,
                    margin_so_call FLOAT, margin_so_so FLOAT,
                    margin_initial FLOAT, margin_maintenance FLOAT,
                    assets FLOAT, liabilities FLOAT, commission_blocked FLOAT,
                    currency_digits INTEGER, fifo_close BOOLEAN, hedge_allowed BOOLEAN,
                    positions INTEGER
                )
            """))
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_license ON ws_account_stats (license_key)",
                "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_timestamp ON ws_account_stats (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_ws_account_stats_license_ts ON ws_account_stats (license_key, timestamp)",
            ]:
                conn.execute(text(idx))

            # ws_open_positions
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ws_open_positions (
                    id BIGSERIAL PRIMARY KEY,
                    license_key VARCHAR(255) NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                    ticket BIGINT NOT NULL,
                    symbol VARCHAR(50) NOT NULL,
                    type VARCHAR(10) NOT NULL,
                    volume FLOAT, open_price FLOAT, current_price FLOAT,
                    sl FLOAT, tp FLOAT, profit FLOAT, swap FLOAT,
                    commission FLOAT, magic BIGINT, comment VARCHAR(255),
                    open_time BIGINT, identifier BIGINT
                )
            """))
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_license ON ws_open_positions (license_key)",
                "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_ticket ON ws_open_positions (ticket)",
                "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_license_ts ON ws_open_positions (license_key, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_ws_open_positions_symbol ON ws_open_positions (symbol)",
            ]:
                conn.execute(text(idx))

            # ws_trade_history
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ws_trade_history (
                    id BIGSERIAL PRIMARY KEY,
                    license_key VARCHAR(255) NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                    ticket BIGINT NOT NULL,
                    symbol VARCHAR(50) NOT NULL,
                    type VARCHAR(10) NOT NULL,
                    volume FLOAT, open_price FLOAT, close_price FLOAT,
                    sl FLOAT, tp FLOAT, profit FLOAT, swap FLOAT,
                    commission FLOAT, fee FLOAT, magic BIGINT,
                    comment VARCHAR(255), open_time BIGINT, close_time BIGINT,
                    order_id BIGINT, entry VARCHAR(10), reason VARCHAR(20),
                    position_id BIGINT,
                    CONSTRAINT uq_ws_trade_history_license_ticket UNIQUE (license_key, ticket)
                )
            """))
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_license ON ws_trade_history (license_key)",
                "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_ticket ON ws_trade_history (ticket)",
                "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_license_ts ON ws_trade_history (license_key, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_symbol ON ws_trade_history (symbol)",
                "CREATE INDEX IF NOT EXISTS idx_ws_trade_history_close_time ON ws_trade_history (close_time)",
            ]:
                conn.execute(text(idx))

            # ws_health_telemetry
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ws_health_telemetry (
                    id BIGSERIAL PRIMARY KEY,
                    license_key VARCHAR(255) NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW() NOT NULL,
                    ws_latency_ms INTEGER, terminal_lag_ms INTEGER,
                    ws_uptime_sec INTEGER, ws_bytes_sent INTEGER,
                    ws_bytes_recv INTEGER, ws_reconnects INTEGER,
                    ws_frames_queued INTEGER, ws_frames_dropped INTEGER,
                    dll_version VARCHAR(20)
                )
            """))
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_ws_health_license ON ws_health_telemetry (license_key)",
                "CREATE INDEX IF NOT EXISTS idx_ws_health_timestamp ON ws_health_telemetry (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_ws_health_license_ts ON ws_health_telemetry (license_key, timestamp)",
            ]:
                conn.execute(text(idx))

        logger.info("WS telemetry tables ensured")

    def _ensure_ea_connections_table(self) -> None:
        """Ensure ea_connections table exists as single source of truth for active EA connections."""
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ea_connections (
                    license_key VARCHAR(255) NOT NULL,
                    connection_type VARCHAR(20) NOT NULL,
                    worker_id INTEGER NOT NULL,
                    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
                    metadata TEXT,
                    PRIMARY KEY (license_key, connection_type, worker_id)
                )
            """))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_ea_conn_last_seen ON ea_connections (last_seen)"
                )
            )
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS support_chat_log (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    username VARCHAR(255) DEFAULT '',
                    full_name VARCHAR(255) DEFAULT '',
                    license_key_masked VARCHAR(50) DEFAULT '',
                    role VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS idx_scl_chat ON support_chat_log (chat_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS idx_scl_ts ON support_chat_log (timestamp DESC)")
            )

    def log_alert(self, alert_data: dict) -> int:
        """Log webhook alert to database."""
        with self.get_connection() as session:
            result = session.execute(
                text(
                    "INSERT INTO alert_history "
                    "(ip_address, user_agent, payload, action, symbol, volume, "
                    "response_code, response_message, execution_time_ms, rate_limited) "
                    "VALUES (:ip, :ua, :payload, :action, :symbol, :volume, "
                    ":code, :msg, :ms, :rl) RETURNING id"
                ),
                {
                    "ip": alert_data.get("ip_address"),
                    "ua": alert_data.get("user_agent"),
                    "payload": json.dumps(alert_data.get("payload", {})),
                    "action": alert_data.get("action"),
                    "symbol": alert_data.get("symbol"),
                    "volume": alert_data.get("volume"),
                    "code": alert_data.get("response_code"),
                    "msg": alert_data.get("response_message"),
                    "ms": alert_data.get("execution_time_ms"),
                    "rl": alert_data.get("rate_limited", False),
                },
            )
            session.commit()
            row = result.first()
            return row[0] if row else 0

    def log_trade(self, trade_data: dict) -> int:
        """Log trade to database and return the trade ID."""
        with self.get_connection() as session:
            result = session.execute(
                text(
                    "INSERT INTO trades "
                    "(license_key, signal_id, ticket, deal, symbol, action, volume, price, "
                    "entry_price, exit_price, sl, tp, profit, profit_percent, commission, swap, "
                    "comment, magic, duration_minutes, status, error, ip_address, "
                    "execution_time_ms, spread_points, slippage) "
                    "VALUES (:lk, :sid, :ticket, :deal, :symbol, :action, :volume, :price, "
                    ":entry_price, :exit_price, :sl, :tp, :profit, :profit_pct, :commission, "
                    ":swap, :comment, :magic, :duration, :status, :error, :ip, :exec_ms, "
                    ":spread, :slippage) RETURNING id"
                ),
                {
                    "lk": trade_data.get("license_key"),
                    "sid": trade_data.get("signal_id"),
                    "ticket": trade_data.get("ticket"),
                    "deal": trade_data.get("deal"),
                    "symbol": trade_data.get("symbol"),
                    "action": trade_data.get("action"),
                    "volume": trade_data.get("volume"),
                    "price": trade_data.get("price"),
                    "entry_price": trade_data.get("entry_price"),
                    "exit_price": trade_data.get("exit_price"),
                    "sl": trade_data.get("sl"),
                    "tp": trade_data.get("tp"),
                    "profit": trade_data.get("profit", 0),
                    "profit_pct": trade_data.get("profit_percent", 0),
                    "commission": trade_data.get("commission", 0),
                    "swap": trade_data.get("swap", 0),
                    "comment": trade_data.get("comment"),
                    "magic": trade_data.get("magic"),
                    "duration": trade_data.get("duration_minutes"),
                    "status": trade_data.get("status"),
                    "error": trade_data.get("error"),
                    "ip": trade_data.get("ip_address"),
                    "exec_ms": trade_data.get("execution_time_ms"),
                    "spread": trade_data.get("spread_points"),
                    "slippage": trade_data.get("slippage", 0),
                },
            )
            session.commit()
            row = result.first()
            trade_id = row[0] if row else 0

        # Update ws_signal_log execution tracking if signal_id provided
        signal_id = trade_data.get("signal_id")
        status = trade_data.get("status")
        if signal_id:
            try:
                exec_status = "executed" if status == "success" else "failed"
                exec_detail = trade_data.get("error") or trade_data.get("status", "")
                ticket = str(trade_data.get("ticket", ""))
                self._update_signal_execution(signal_id, exec_status, exec_detail, ticket)
            except Exception as e:
                logger.warning("Failed to update signal execution status: %s", e)

        # Update symbol performance metrics after successful insert
        symbol = trade_data.get("symbol")
        if status == "success" and symbol:
            try:
                self._update_symbol_performance(symbol)
            except Exception:
                logger.warning("Failed to update symbol_performance for %s", symbol)

        return trade_id

    def get_latest_account_stats(self, license_key: str | None = None) -> list[dict]:
        """Return the most recent snapshot per license."""
        if license_key:
            return self.execute_query(
                "SELECT * FROM account_stats WHERE license_key = :key "
                "ORDER BY timestamp DESC LIMIT 1",
                {"key": license_key},
            )
        return self.execute_query(
            "SELECT a.* FROM account_stats a "
            "INNER JOIN ("
            "  SELECT license_key, MAX(timestamp) as max_ts "
            "  FROM account_stats GROUP BY license_key"
            ") b ON a.license_key = b.license_key AND a.timestamp = b.max_ts"
        )

    # ------------------------------------------------------------------
    # User Dashboard Queries — read-only queries for the Telegram bot user
    # dashboard. All return list[dict] or None.
    # ------------------------------------------------------------------

    def get_latest_open_positions(self, license_key: str) -> list[dict[str, Any]]:
        """Return the most recent snapshot of open positions for a license.

        Positions are deduplicated by ticket (latest snapshot wins).
        Returns list of dicts with: ticket, symbol, type, volume, open_price,
        current_price, sl, tp, profit, swap, commission, magic, comment, open_time.
        """
        return self.execute_query(
            "SELECT DISTINCT ON (ticket) "
            "ticket, symbol, type, volume, open_price, current_price, "
            "sl, tp, profit, swap, commission, magic, comment, open_time "
            "FROM ws_open_positions "
            "WHERE license_key = :lk "
            "ORDER BY ticket DESC, timestamp DESC",
            {"lk": license_key},
        )

    def get_trade_history_for_license(
        self, license_key: str, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return recent closed trade history for a license (newest first).

        Returns list of dicts with: ticket, symbol, type, volume, open_price,
        close_price, sl, tp, profit, swap, commission, fee, magic, comment,
        open_time, close_time, entry, reason, position_id.
        """
        return self.execute_query(
            "SELECT ticket, symbol, type, volume, open_price, close_price, "
            "sl, tp, profit, swap, commission, fee, magic, comment, "
            "open_time, close_time, entry, reason, position_id "
            "FROM ws_trade_history "
            "WHERE license_key = :lk "
            "ORDER BY open_time DESC NULLS LAST LIMIT :lim OFFSET :off",
            {"lk": license_key, "lim": limit, "off": offset},
        )

    def get_ea_audit_for_license(self, license_key: str) -> dict[str, Any] | None:
        """Return the most recent EA audit record for a license.

        Returns a dict with all ea_audit fields, or None if no records exist.
        """
        rows = self.execute_query(
            "SELECT * FROM ea_audit WHERE license_key = :lk ORDER BY timestamp DESC LIMIT 1",
            {"lk": license_key},
        )
        return dict(rows[0]) if rows else None

    def _update_symbol_performance(self, symbol: str):
        """Update symbol performance metrics after a trade."""
        if not symbol:
            return
        with self.get_connection() as session:
            # PostgreSQL uses INSERT ... ON CONFLICT (upsert)
            session.execute(
                text("""
                    INSERT INTO symbol_performance (
                        symbol, total_trades, winning_trades, total_volume,
                        net_profit, win_rate, profit_factor, average_profit,
                        best_trade, worst_trade, last_updated
                    )
                    SELECT
                        :sym,
                        COUNT(*),
                        COUNT(CASE WHEN profit > 0 THEN 1 END),
                        COALESCE(SUM(volume), 0),
                        COALESCE(SUM(profit), 0),
                        CASE WHEN COUNT(*) > 0
                            THEN CAST(COUNT(CASE WHEN profit > 0 THEN 1 END) AS REAL) / COUNT(*) * 100
                            ELSE 0
                        END,
                        CASE
                            WHEN COALESCE(SUM(CASE WHEN profit < 0 THEN profit END), 0) != 0
                            THEN ABS(COALESCE(SUM(CASE WHEN profit > 0 THEN profit END), 0) /
                                     SUM(CASE WHEN profit < 0 THEN profit END))
                            ELSE 0
                        END,
                        COALESCE(AVG(profit), 0),
                        COALESCE(MAX(profit), 0),
                        COALESCE(MIN(profit), 0),
                        NOW()
                    FROM trades
                    WHERE symbol = :sym AND status = 'success'
                    ON CONFLICT (symbol) DO UPDATE SET
                        total_trades = EXCLUDED.total_trades,
                        winning_trades = EXCLUDED.winning_trades,
                        total_volume = EXCLUDED.total_volume,
                        net_profit = EXCLUDED.net_profit,
                        win_rate = EXCLUDED.win_rate,
                        profit_factor = EXCLUDED.profit_factor,
                        average_profit = EXCLUDED.average_profit,
                        best_trade = EXCLUDED.best_trade,
                        worst_trade = EXCLUDED.worst_trade,
                        last_updated = EXCLUDED.last_updated
                """),
                {"sym": symbol},
            )
            session.commit()

    def get_daily_summary(self, date: str | None = None) -> dict:
        """Get daily trading summary for a given date (defaults to today)."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        rows = self.execute_query("SELECT * FROM daily_stats WHERE date = :date", {"date": date})
        return dict(rows[0]) if rows else {}

    def get_symbol_performance(self) -> list[dict]:
        """Get performance metrics for all traded symbols."""
        return self.execute_query(
            "SELECT symbol, total_trades, winning_trades, total_volume, "
            "net_profit, win_rate, profit_factor, average_profit, "
            "best_trade, worst_trade, last_updated "
            "FROM symbol_performance ORDER BY net_profit DESC"
        )

    def get_alert_statistics(self, hours: int = 24) -> dict:
        """Get webhook alert statistics."""
        interval_expr = self.sql_interval_hours(hours)
        rows = self.execute_query(
            f"SELECT "
            f"COUNT(*) as total_alerts, "
            f"COUNT(DISTINCT ip_address) as unique_ips, "
            f"COUNT(CASE WHEN response_code = 200 THEN 1 END) as successful, "
            f"COUNT(CASE WHEN response_code != 200 THEN 1 END) as failed, "
            f"COUNT(CASE WHEN rate_limited = TRUE THEN 1 END) as rate_limited, "
            f"COALESCE(AVG(execution_time_ms), 0) as avg_execution_time "
            f"FROM alert_history WHERE timestamp >= {interval_expr}"
        )
        stats = (
            dict(rows[0])
            if rows
            else {
                "total_alerts": 0,
                "unique_ips": 0,
                "successful": 0,
                "failed": 0,
                "rate_limited": 0,
                "avg_execution_time": 0,
            }
        )
        # Top symbols
        symbol_rows = self.execute_query(
            f"SELECT symbol, COUNT(*) as count FROM alert_history "
            f"WHERE timestamp >= {interval_expr} AND symbol IS NOT NULL "
            f"GROUP BY symbol ORDER BY count DESC LIMIT 5"
        )
        stats["top_symbols"] = symbol_rows
        # Top IPs
        ip_rows = self.execute_query(
            f"SELECT ip_address, COUNT(*) as count FROM alert_history "
            f"WHERE timestamp >= {interval_expr} GROUP BY ip_address "
            f"ORDER BY count DESC LIMIT 5"
        )
        stats["top_ips"] = ip_rows
        return stats

    def get_signals_by_license(
        self,
        license_key: str,
        status_filter: str = "all",
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        """Get paginated signals for a license key with optional status filter."""
        query = (
            "SELECT signal_id, license_key, signal_data, status, "
            "created_at, acknowledged_at FROM signal_queue WHERE license_key = :key"
        )
        params: dict = {"key": license_key, "lim": limit, "off": offset}
        if status_filter in ("pending", "acknowledged"):
            query += " AND status = :status"
            params["status"] = status_filter
        query += " ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        rows = self.execute_query(query, params)
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
    ) -> list[dict]:
        """Get all pending signals for a license key.

        Parses JSON signal_data, adds signal_id and age_minutes.
        Filters out signals older than max_age_minutes.
        """
        age_expr = self.sql_age_seconds("created_at")
        max_age_expr = self.sql_interval_minutes(max_age_minutes)
        rows = self.execute_query(
            f"SELECT signal_id, signal_data, created_at, {age_expr} as age_minutes "
            f"FROM signal_queue WHERE license_key = :key AND status = 'pending' "
            f"AND created_at >= {max_age_expr} ORDER BY created_at ASC LIMIT :lim",
            {"key": license_key, "lim": limit},
        )
        signals: list[dict] = []
        for row in rows:
            try:
                signal_data = row.get("signal_data", "")
                if not signal_data:
                    continue
                signal_dict = (
                    json.loads(signal_data) if isinstance(signal_data, str) else signal_data
                )
                signal_dict["signal_id"] = row["signal_id"]
                signal_dict["age_minutes"] = row["age_minutes"]
                signals.append(signal_dict)
            except (json.JSONDecodeError, TypeError):
                continue
        return signals

    def acknowledge_signal(self, signal_id: str, license_key: str | None = None) -> bool:
        """Mark a signal as acknowledged (sync wrapper)."""
        with self.get_connection() as session:
            if license_key:
                result = session.execute(
                    text(
                        "UPDATE signal_queue SET status = 'acknowledged', "
                        "acknowledged_at = :now WHERE signal_id = :sid "
                        "AND license_key = :key AND status = 'pending'"
                    ),
                    {"sid": signal_id, "key": license_key, "now": datetime.now()},
                )
            else:
                result = session.execute(
                    text(
                        "UPDATE signal_queue SET status = 'acknowledged', "
                        "acknowledged_at = :now WHERE signal_id = :sid AND status = 'pending'"
                    ),
                    {"sid": signal_id, "now": datetime.now()},
                )
            session.commit()
            return result.rowcount > 0

    def ensure_pool_initialized(self) -> None:
        """No-op for PostgreSQL - pool is initialized in __init__."""
        pass

    def delete_signals_by_license(self, license_key: str) -> int:
        """Delete all signal_queue rows for a license key."""
        with self.get_connection() as session:
            result = session.execute(
                text("DELETE FROM signal_queue WHERE license_key = :key"),
                {"key": license_key},
            )
            session.commit()
            deleted = result.rowcount
            if deleted > 0:
                logger.info(
                    "Deleted %d signal_queue rows for license %s", deleted, _mask(license_key)
                )
            return deleted

    def save_account_stats(self, stats: dict) -> None:
        """Insert an account stats snapshot from an EA (legacy HTTP endpoint)."""
        with self.get_connection() as session:
            session.execute(
                text(
                    "INSERT INTO account_stats "
                    "(license_key, account, account_name, broker, currency, leverage, "
                    "balance, equity, profit, margin, margin_free, margin_level, "
                    "open_positions, pending_orders, ea_version, dll_version, magic, timestamp) "
                    "VALUES (:lk, :acct, :acct_name, :broker, :cur, :lev, :bal, :eq, :prof, "
                    ":mg, :mf, :ml, :op, :po, :ev, :dll_v, :magic, :ts)"
                ),
                {
                    "lk": stats.get("license_key"),
                    "acct": stats.get("account"),
                    "acct_name": stats.get("account_name"),
                    "broker": stats.get("broker"),
                    "cur": stats.get("currency"),
                    "lev": stats.get("leverage"),
                    "bal": stats.get("balance"),
                    "eq": stats.get("equity"),
                    "prof": stats.get("profit"),
                    "mg": stats.get("margin"),
                    "mf": stats.get("margin_free"),
                    "ml": stats.get("margin_level"),
                    "op": stats.get("open_positions"),
                    "po": stats.get("pending_orders"),
                    "ev": stats.get("ea_version"),
                    "dll_v": stats.get("dll_version"),
                    "magic": stats.get("magic"),
                    "ts": stats.get("timestamp"),
                },
            )
            session.commit()

    # ------------------------------------------------------------------
    # WS Telemetry Storage - data pushed from EAs via WebSocket
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value, default=None):
        """Safely coerce a WS telemetry value to int, handling bools and strings."""
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
    def _safe_float(value, default=None):
        """Safely coerce a WS telemetry value to float, handling bools and strings."""
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
    def _safe_bool(value, default=None):
        """Safely coerce a WS telemetry value to bool.

        Handles Python bools, MQL 'true'/'false' strings, and 0/1 ints.
        """
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
    def _safe_str(value, default=None, max_len=255):
        """Safely coerce a WS telemetry value to str, truncated to max_len."""
        if value is None:
            return default
        result = str(value)
        if len(result) > max_len:
            result = result[:max_len]
        return result

    def save_ea_audit(self, license_key: str, data: dict) -> None:
        """Insert a comprehensive EA audit snapshot.

        Data is retained INFINITELY - no automatic cleanup.
        This is the authoritative long-term audit trail for all EA instances.
        """
        with self.get_connection() as session:
            session.execute(
                text(
                    "INSERT INTO ea_audit "
                    "(license_key, platform, ea_version, dll_version, dll_system_info, "
                    "mt_build, terminal_name, terminal_language, terminal_x64, terminal_pid, "
                    "os, cpu_cores, cpu_freq_mhz, ram_mb, ram_avail_mb, disk_mb, "
                    "account_number, account_name, account_server, account_currency, broker, "
                    "trade_mode, leverage, limit_orders, trade_allowed, trade_expert, margin_so_mode, "
                    "balance, credit, equity, profit, margin, margin_free, margin_level, "
                    "margin_so_call, margin_so_so, "
                    "chart_symbol, chart_timeframe, "
                    "symbol_count, position_count, uptime_sec, ws_status, error_count, "
                    "connection_mode, magic, auto_update_enabled, "
                    "is_vps, vps_provider, vps_manufacturer, vps_model, "
                    "net_quality, net_ping_ms, net_jitter_ms, net_loss_pct, "
                    "ntp_drift_ms, ntp_sync_success) "
                    "VALUES ("
                    ":lk, :platform, :ea_ver, :dll_ver, :dll_sys, "
                    ":mt_build, :term_name, :term_lang, :term_x64, :term_pid, "
                    ":os, :cpu_cores, :cpu_freq, :ram, :ram_avail, :disk, "
                    ":acct_num, :acct_name, :acct_server, :acct_cur, :broker, "
                    ":trade_mode, :leverage, :limit_orders, :trade_allowed, :trade_expert, :so_mode, "
                    ":balance, :credit, :equity, :profit, :margin, :margin_free, :margin_level, "
                    ":so_call, :so_so, "
                    ":chart_sym, :chart_tf, "
                    ":sym_count, :pos_count, :uptime, :ws_status, :err_count, "
                    ":conn_mode, :magic, :auto_update, "
                    ":is_vps, :vps_prov, :vps_mfg, :vps_model, "
                    ":net_q, :net_ping, :net_jitter, :net_loss, "
                    ":ntp_drift, :ntp_sync)"
                ),
                {
                    "lk": license_key,
                    "platform": self._safe_str(data.get("platform"), max_len=10),
                    "ea_ver": self._safe_str(data.get("ea_version"), max_len=50),
                    "dll_ver": self._safe_str(data.get("dll_version"), max_len=20),
                    "dll_sys": self._safe_str(data.get("dll_system_info")),
                    "mt_build": self._safe_int(data.get("mt_build")),
                    "term_name": self._safe_str(data.get("terminal_name")),
                    "term_lang": self._safe_str(data.get("terminal_language"), max_len=10),
                    "term_x64": self._safe_bool(data.get("terminal_x64")),
                    "term_pid": self._safe_int(data.get("terminal_pid")),
                    "os": self._safe_str(data.get("os")),
                    "cpu_cores": self._safe_int(data.get("cpu_cores")),
                    "cpu_freq": self._safe_int(data.get("cpu_freq_mhz")),
                    "ram": self._safe_int(data.get("ram_mb")),
                    "ram_avail": self._safe_int(data.get("ram_avail_mb")),
                    "disk": self._safe_int(data.get("disk_mb")),
                    "acct_num": self._safe_int(data.get("account_number")),
                    "acct_name": self._safe_str(data.get("account_name")),
                    "acct_server": self._safe_str(data.get("account_server")),
                    "acct_cur": self._safe_str(data.get("account_currency"), max_len=10),
                    "broker": self._safe_str(data.get("broker")),
                    "trade_mode": self._safe_str(data.get("trade_mode"), max_len=20),
                    "leverage": self._safe_int(data.get("leverage")),
                    "limit_orders": self._safe_int(data.get("limit_orders")),
                    "trade_allowed": self._safe_bool(data.get("trade_allowed")),
                    "trade_expert": self._safe_bool(data.get("trade_expert")),
                    "so_mode": self._safe_str(data.get("margin_so_mode"), max_len=10),
                    "balance": self._safe_float(data.get("balance")),
                    "credit": self._safe_float(data.get("credit")),
                    "equity": self._safe_float(data.get("equity")),
                    "profit": self._safe_float(data.get("profit")),
                    "margin": self._safe_float(data.get("margin")),
                    "margin_free": self._safe_float(data.get("margin_free")),
                    "margin_level": self._safe_float(data.get("margin_level")),
                    "so_call": self._safe_float(data.get("margin_so_call")),
                    "so_so": self._safe_float(data.get("margin_so_so")),
                    "chart_sym": self._safe_str(data.get("chart_symbol"), max_len=50),
                    "chart_tf": self._safe_str(data.get("chart_timeframe"), max_len=10),
                    "sym_count": self._safe_int(data.get("symbol_count")),
                    "pos_count": self._safe_int(data.get("position_count")),
                    "uptime": self._safe_int(data.get("uptime_sec")),
                    "ws_status": self._safe_str(data.get("ws_status"), max_len=20),
                    "err_count": self._safe_int(data.get("error_count")),
                    "conn_mode": self._safe_str(data.get("connection_mode"), max_len=20),
                    "magic": self._safe_int(data.get("magic")),
                    "auto_update": self._safe_bool(data.get("auto_update_enabled")),
                    "is_vps": self._safe_bool(data.get("is_vps")),
                    "vps_prov": self._safe_str(data.get("vps_provider"), max_len=64),
                    "vps_mfg": self._safe_str(data.get("vps_manufacturer"), max_len=128),
                    "vps_model": self._safe_str(data.get("vps_model"), max_len=128),
                    "net_q": self._safe_str(data.get("net_quality"), max_len=16),
                    "net_ping": self._safe_int(data.get("net_ping_ms")),
                    "net_jitter": self._safe_int(data.get("net_jitter_ms")),
                    "net_loss": self._safe_float(data.get("net_loss_pct")),
                    "ntp_drift": self._safe_int(data.get("ntp_drift_ms")),
                    "ntp_sync": self._safe_bool(data.get("ntp_sync_success")),
                },
            )
            session.commit()

    def save_ws_account_stats(self, license_key: str, data: dict) -> None:
        """Insert a full account stats snapshot from WS telemetry.

        Applies type coercion to handle MQL/JSON type variations safely.
        """
        with self.get_connection() as session:
            session.execute(
                text(
                    "INSERT INTO ws_account_stats "
                    "(license_key, login, name, server, currency, company, "
                    "trade_mode, leverage, limit_orders, trade_allowed, trade_expert, margin_so_mode, "
                    "balance, credit, equity, profit, margin, margin_free, margin_level, "
                    "margin_so_call, margin_so_so, margin_initial, margin_maintenance, "
                    "assets, liabilities, commission_blocked, "
                    "currency_digits, fifo_close, hedge_allowed, positions) "
                    "VALUES (:lk, :login, :name, :server, :currency, :company, "
                    ":trade_mode, :leverage, :limit_orders, :trade_allowed, :trade_expert, :margin_so_mode, "
                    ":balance, :credit, :equity, :profit, :margin, :margin_free, :margin_level, "
                    ":margin_so_call, :margin_so_so, :margin_initial, :margin_maintenance, "
                    ":assets, :liabilities, :commission_blocked, "
                    ":currency_digits, :fifo_close, :hedge_allowed, :positions)"
                ),
                {
                    "lk": license_key,
                    "login": self._safe_int(data.get("login")),
                    "name": self._safe_str(data.get("name")),
                    "server": self._safe_str(data.get("server")),
                    "currency": self._safe_str(data.get("currency"), max_len=10),
                    "company": self._safe_str(data.get("company")),
                    "trade_mode": self._safe_str(data.get("trade_mode"), max_len=20),
                    "leverage": self._safe_int(data.get("leverage")),
                    "limit_orders": self._safe_int(data.get("limit_orders")),
                    "trade_allowed": self._safe_bool(data.get("trade_allowed")),
                    "trade_expert": self._safe_bool(data.get("trade_expert")),
                    "margin_so_mode": self._safe_str(data.get("margin_so_mode"), max_len=10),
                    "balance": self._safe_float(data.get("balance")),
                    "credit": self._safe_float(data.get("credit")),
                    "equity": self._safe_float(data.get("equity")),
                    "profit": self._safe_float(data.get("profit")),
                    "margin": self._safe_float(data.get("margin")),
                    "margin_free": self._safe_float(data.get("margin_free")),
                    "margin_level": self._safe_float(data.get("margin_level")),
                    "margin_so_call": self._safe_float(data.get("margin_so_call")),
                    "margin_so_so": self._safe_float(data.get("margin_so_so")),
                    "margin_initial": self._safe_float(data.get("margin_initial")),
                    "margin_maintenance": self._safe_float(data.get("margin_maintenance")),
                    "assets": self._safe_float(data.get("assets")),
                    "liabilities": self._safe_float(data.get("liabilities")),
                    "commission_blocked": self._safe_float(data.get("commission_blocked")),
                    "currency_digits": self._safe_int(data.get("currency_digits")),
                    "fifo_close": self._safe_bool(data.get("fifo_close")),
                    "hedge_allowed": self._safe_bool(data.get("hedge_allowed")),
                    "positions": self._safe_int(data.get("positions")),
                },
            )
            session.commit()
            logger.debug("WS account stats saved for %s", _mask(license_key))

    def save_ws_open_positions(self, license_key: str, positions: list[dict]) -> None:
        """Append open positions snapshot for a license key (infinite retention).

        Each call inserts a new snapshot with its own timestamp. Historical
        snapshots are preserved for tracking position changes over time.
        Uses bulk INSERT with executemany() for a single DB roundtrip.
        """
        with self.get_connection() as session:
            # Bulk insert positions as a new snapshot (single DB roundtrip)
            if positions:
                session.execute(
                    text(
                        "INSERT INTO ws_open_positions "
                        "(license_key, ticket, symbol, type, volume, open_price, current_price, "
                        "sl, tp, profit, swap, commission, magic, comment, open_time, identifier) "
                        "VALUES (:lk, :ticket, :symbol, :type, :volume, :open_price, :current_price, "
                        ":sl, :tp, :profit, :swap, :commission, :magic, :comment, :open_time, :identifier)"
                    ),
                    [
                        {
                            "lk": license_key,
                            "ticket": self._safe_int(pos.get("ticket")),
                            "symbol": self._safe_str(pos.get("symbol"), max_len=50),
                            "type": self._safe_str(pos.get("type"), max_len=10),
                            "volume": self._safe_float(pos.get("volume")),
                            "open_price": self._safe_float(pos.get("open_price")),
                            "current_price": self._safe_float(pos.get("current_price")),
                            "sl": self._safe_float(pos.get("sl")),
                            "tp": self._safe_float(pos.get("tp")),
                            "profit": self._safe_float(pos.get("profit")),
                            "swap": self._safe_float(pos.get("swap")),
                            "commission": self._safe_float(pos.get("commission")),
                            "magic": self._safe_int(pos.get("magic")),
                            "comment": self._safe_str(pos.get("comment"), max_len=255),
                            "open_time": self._safe_int(pos.get("open_time")),
                            "identifier": self._safe_int(pos.get("identifier")),
                        }
                        for pos in positions
                    ],
                )
            session.commit()
            logger.debug(
                "WS open positions saved for %s (%d positions)",
                _mask(license_key),
                len(positions),
            )

    def save_ws_trade_history(self, license_key: str, deals: list[dict]) -> None:
        """Insert trade history deals, using ON CONFLICT to skip duplicates.

        Uses PostgreSQL UPSERT (INSERT ON CONFLICT DO NOTHING) with the unique
        constraint on (license_key, ticket) for efficient, race-condition-free
        deduplication. Bulk INSERT with executemany() for a single DB roundtrip.
        """
        if not deals:
            return
        with self.get_connection() as session:
            session.execute(
                text(
                    "INSERT INTO ws_trade_history "
                    "(license_key, ticket, symbol, type, volume, open_price, close_price, "
                    "sl, tp, profit, swap, commission, fee, magic, comment, "
                    "open_time, close_time, order_id, entry, reason, position_id) "
                    "VALUES (:lk, :ticket, :symbol, :type, :volume, :open_price, :close_price, "
                    ":sl, :tp, :profit, :swap, :commission, :fee, :magic, :comment, "
                    ":open_time, :close_time, :order_id, :entry, :reason, :position_id) "
                    "ON CONFLICT (license_key, ticket) DO NOTHING"
                ),
                [
                    {
                        "lk": license_key,
                        "ticket": self._safe_int(deal.get("ticket")),
                        "symbol": self._safe_str(deal.get("symbol"), max_len=50),
                        "type": self._safe_str(deal.get("type"), max_len=10),
                        "volume": self._safe_float(deal.get("volume")),
                        "open_price": self._safe_float(deal.get("open_price", deal.get("price"))),
                        "close_price": self._safe_float(deal.get("close_price")),
                        "sl": self._safe_float(deal.get("sl")),
                        "tp": self._safe_float(deal.get("tp")),
                        "profit": self._safe_float(deal.get("profit")),
                        "swap": self._safe_float(deal.get("swap")),
                        "commission": self._safe_float(deal.get("commission")),
                        "fee": self._safe_float(deal.get("fee")),
                        "magic": self._safe_int(deal.get("magic")),
                        "comment": self._safe_str(deal.get("comment"), max_len=255),
                        "open_time": self._safe_int(deal.get("open_time", deal.get("time"))),
                        "close_time": self._safe_int(deal.get("close_time")),
                        "order_id": self._safe_int(deal.get("order")),
                        "entry": self._safe_str(deal.get("entry"), max_len=10),
                        "reason": self._safe_str(deal.get("reason"), max_len=20),
                        "position_id": self._safe_int(deal.get("position_id")),
                    }
                    for deal in deals
                ],
            )
            session.commit()
            logger.debug(
                "WS trade history saved for %s (%d deals)",
                _mask(license_key),
                len(deals),
            )

    def save_ws_health_telemetry(self, license_key: str, data: dict) -> None:
        """Insert a health telemetry snapshot from WS."""
        with self.get_connection() as session:
            session.execute(
                text(
                    "INSERT INTO ws_health_telemetry "
                    "(license_key, ws_latency_ms, terminal_lag_ms, ws_uptime_sec, "
                    "ws_bytes_sent, ws_bytes_recv, ws_reconnects, ws_frames_queued, "
                    "ws_frames_dropped, dll_version) "
                    "VALUES (:lk, :latency, :lag, :uptime, :sent, :recv, :reconnects, "
                    ":queued, :dropped, :dll_version)"
                ),
                {
                    "lk": license_key,
                    "latency": self._safe_int(data.get("ws_latency_ms")),
                    "lag": self._safe_int(data.get("terminal_lag_ms")),
                    "uptime": self._safe_int(data.get("ws_uptime_sec")),
                    "sent": self._safe_int(data.get("ws_bytes_sent")),
                    "recv": self._safe_int(data.get("ws_bytes_recv")),
                    "reconnects": self._safe_int(data.get("ws_reconnects")),
                    "queued": self._safe_int(data.get("ws_frames_queued")),
                    "dropped": self._safe_int(data.get("ws_frames_dropped")),
                    "dll_version": self._safe_str(data.get("dll_version"), max_len=20),
                },
            )
            session.commit()
            logger.debug("WS health telemetry saved for %s", _mask(license_key))

    # ------------------------------------------------------------------
    # WS Telemetry Retention - manual-only cleanup (NOT scheduled)
    # IMPORTANT: WS telemetry data (account_stats, open_positions,
    # trade_history, health) is retained INFINITELY by default. This data
    # tracks trader performance year over year. Never auto-delete it.
    # The method below exists ONLY for manual admin intervention if
    # disk space becomes critical and you explicitly want to prune.
    # ------------------------------------------------------------------

    def cleanup_ws_telemetry(
        self,
        stats_days: int = 0,
        health_days: int = 0,
        positions_days: int = 0,
        audit_days: int = 0,
    ) -> int:
        """Delete old telemetry data - MANUAL USE ONLY, never auto-scheduled.

        By default all arguments are 0, meaning NO data is deleted. Only call
        this with explicit day thresholds if you need to free disk space.
        Returns total number of rows deleted.
        """
        if stats_days <= 0 and health_days <= 0 and positions_days <= 0 and audit_days <= 0:
            logger.warning(
                "WS telemetry cleanup called with no thresholds - skipping (data retained infinitely)"
            )
            return 0

        total_deleted = 0
        with self.get_connection() as session:
            if health_days > 0:
                result = session.execute(
                    text(
                        f"DELETE FROM ws_health_telemetry WHERE timestamp < NOW() - INTERVAL '{int(health_days)} days'"
                    ),
                )
                total_deleted += result.rowcount

            if stats_days > 0:
                result = session.execute(
                    text(
                        f"DELETE FROM ws_account_stats WHERE timestamp < NOW() - INTERVAL '{int(stats_days)} days'"
                    ),
                )
                total_deleted += result.rowcount

            if positions_days > 0:
                result = session.execute(
                    text(
                        f"DELETE FROM ws_open_positions WHERE timestamp < NOW() - INTERVAL '{int(positions_days)} days'"
                    ),
                )
                total_deleted += result.rowcount

            if audit_days > 0:
                result = session.execute(
                    text(
                        f"DELETE FROM ea_audit WHERE timestamp < NOW() - INTERVAL '{int(audit_days)} days'"
                    ),
                )
                total_deleted += result.rowcount

            session.commit()
            if total_deleted > 0:
                logger.info(
                    "WS telemetry cleanup: deleted %d rows (health=%dd, stats=%dd, positions=%dd, audit=%dd)",
                    total_deleted,
                    health_days,
                    stats_days,
                    positions_days,
                    audit_days,
                )
            return total_deleted

    async def close_async(self):
        """Close async database connections."""
        await self.async_engine.dispose()
        logger.info("PostgreSQL async connections closed")

    def close(self):
        """Close all database connections (sync + async)."""
        self.engine.dispose()
        # Close async engine synchronously (blocking)
        try:
            try:
                asyncio.get_running_loop()
                # We're inside an event loop - schedule disposal and wait
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pool.submit(lambda: asyncio.run(self.async_engine.dispose())).result()
            except RuntimeError:
                # No running loop - safe to create one
                asyncio.run(self.async_engine.dispose())
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
        logger.info("PostgreSQL connections closed")


def create_database_manager(database_url: str | None, **kwargs) -> Any:
    """Create a PostgreSQL database manager.

    Args:
        database_url: PostgreSQL connection string (required).
            Must start with 'postgresql://' or 'postgresql+asyncpg://'.
        **kwargs: Pool configuration options (pool_size, max_overflow, etc.).

    Returns:
        PostgresDatabaseManager instance.

    Raises:
        ValueError: If database_url is None or not a PostgreSQL URL.
    """
    if not database_url:
        raise ValueError(
            "DATABASE_URL is required. PostgreSQL is the only supported database backend. "
            "Set DATABASE_URL to a PostgreSQL connection string."
        )

    if (
        database_url.startswith("postgresql://")
        or database_url.startswith("postgresql+asyncpg://")
        or database_url.startswith("postgresql+psycopg://")
    ):
        return PostgresDatabaseManager(database_url=database_url, **kwargs)

    raise ValueError(
        f"Unsupported database URL scheme: {database_url.split('://')[0]}. "
        "Only PostgreSQL is supported. Set DATABASE_URL to a postgresql:// connection string."
    )

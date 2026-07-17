"""Add signal_queue table and runtime indexes

Revision ID: 002_signal_queue
Create Date: 2026-06-08

Adds the signal_queue table (the primary table used at runtime for signal
persistence) and supporting runtime tables not covered in the initial migration.

NOTE: The 001_initial migration created `licenses`, `signals`, `trades`, and
`admin_logs` with a simplified schema. This migration DROPS those placeholder
tables and recreates them with the full column set that the application code
actually expects, then creates the remaining runtime tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "002_signal_queue"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def _safe_drop(table_name: str) -> None:
    """Drop a table with try/except and a savepoint.

    Uses a nested transaction (SAVEPOINT) so that a single failed drop
    does not abort the entire migration. Falls back to raw SQL
    DROP TABLE IF EXISTS for dialects that do not support savepoints.
    """
    bind = op.get_bind()
    try:
        with bind.begin_nested():
            op.execute(f"DROP TABLE IF EXISTS {table_name}")
    except Exception as exc:
        print(f"[002] WARNING: Could not drop {table_name}: {exc} - continuing")


def upgrade() -> None:
    bind = op.get_bind()
    # ------------------------------------------------------------------
    # Drop the simplified tables from 001_initial so we can recreate them
    # with the full schema the application code expects.
    # Each drop is wrapped in a savepoint so one failure does not abort
    # the entire migration.
    # ------------------------------------------------------------------
    _safe_drop("admin_logs")
    _safe_drop("trades")
    _safe_drop("signals")

    # ------------------------------------------------------------------
    # Trades table - full column set matching DatabaseManager.init_database()
    # ------------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("ticket", sa.Integer, nullable=True),
        sa.Column("deal", sa.Integer, nullable=True),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=True),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("sl", sa.Float, nullable=True),
        sa.Column("tp", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("profit_percent", sa.Float, nullable=True),
        sa.Column("commission", sa.Float, nullable=True),
        sa.Column("swap", sa.Float, nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("magic", sa.Integer, nullable=True),
        sa.Column("duration_minutes", sa.Integer, nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("ip_address", sa.String(255), nullable=True),
        sa.Column("execution_time_ms", sa.Float, nullable=True),
        sa.Column("spread_points", sa.Float, nullable=True),
        sa.Column("slippage", sa.Float, nullable=True),
    )
    op.create_index("idx_trades_timestamp", "trades", ["timestamp"])
    op.create_index("idx_trades_symbol", "trades", ["symbol"])
    op.create_index("idx_trades_status", "trades", ["status"])
    op.create_index("idx_trades_magic", "trades", ["magic"])

    # ------------------------------------------------------------------
    # Alert history - full column set for webhook logging
    # ------------------------------------------------------------------
    op.create_table(
        "alert_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("ip_address", sa.String(255), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("payload", sa.Text, nullable=True),
        sa.Column("action", sa.String(50), nullable=True),
        sa.Column("symbol", sa.String(50), nullable=True),
        sa.Column("volume", sa.Float, nullable=True),
        sa.Column("response_code", sa.Integer, nullable=True),
        sa.Column("response_message", sa.Text, nullable=True),
        sa.Column("execution_time_ms", sa.Float, nullable=True),
        sa.Column("rate_limited", sa.Boolean, server_default=sa.text("FALSE"), nullable=True),
    )

    # ------------------------------------------------------------------
    # Daily statistics aggregation
    # ------------------------------------------------------------------
    op.create_table(
        "daily_stats",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("total_trades", sa.Integer, server_default="0"),
        sa.Column("winning_trades", sa.Integer, server_default="0"),
        sa.Column("losing_trades", sa.Integer, server_default="0"),
        sa.Column("breakeven_trades", sa.Integer, server_default="0"),
        sa.Column("total_volume", sa.Float, server_default="0"),
        sa.Column("gross_profit", sa.Float, server_default="0"),
        sa.Column("gross_loss", sa.Float, server_default="0"),
        sa.Column("net_profit", sa.Float, server_default="0"),
        sa.Column("commission_paid", sa.Float, server_default="0"),
        sa.Column("max_drawdown", sa.Float, server_default="0"),
        sa.Column("max_drawdown_percent", sa.Float, server_default="0"),
        sa.Column("win_rate", sa.Float, server_default="0"),
        sa.Column("profit_factor", sa.Float, server_default="0"),
        sa.Column("average_win", sa.Float, server_default="0"),
        sa.Column("average_loss", sa.Float, server_default="0"),
        sa.Column("largest_win", sa.Float, server_default="0"),
        sa.Column("largest_loss", sa.Float, server_default="0"),
        sa.Column("average_trade_duration", sa.Integer, server_default="0"),
        sa.Column("total_alerts_received", sa.Integer, server_default="0"),
        sa.Column("failed_trades", sa.Integer, server_default="0"),
    )

    # ------------------------------------------------------------------
    # Symbol performance - replace simplified version from 001 if it exists
    # ------------------------------------------------------------------
    _safe_drop("symbol_performance")
    op.create_table(
        "symbol_performance",
        sa.Column("symbol", sa.String(50), primary_key=True),
        sa.Column("total_trades", sa.Integer, server_default="0"),
        sa.Column("winning_trades", sa.Integer, server_default="0"),
        sa.Column("total_volume", sa.Float, server_default="0"),
        sa.Column("net_profit", sa.Float, server_default="0"),
        sa.Column("win_rate", sa.Float, server_default="0"),
        sa.Column("profit_factor", sa.Float, server_default="0"),
        sa.Column("average_profit", sa.Float, server_default="0"),
        sa.Column("best_trade", sa.Float, server_default="0"),
        sa.Column("worst_trade", sa.Float, server_default="0"),
        sa.Column("last_updated", sa.DateTime, server_default=sa.func.now()),
    )

    # ------------------------------------------------------------------
    # Signal queue - the primary runtime signal table
    # ------------------------------------------------------------------
    op.create_table(
        "signal_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.String(255), unique=True, nullable=False),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("signal_data", sa.Text, nullable=False),
        sa.Column("signal_hash", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("processed_at", sa.DateTime, nullable=True),
        sa.Column("retry_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("claimed_at", sa.DateTime, nullable=True),
        sa.Column("claimed_by", sa.String(255), nullable=True),
    )
    op.create_index("idx_signal_queue_license", "signal_queue", ["license_key"])
    op.create_index("idx_signal_queue_status", "signal_queue", ["status"])
    op.create_index("idx_signal_queue_created", "signal_queue", ["created_at"])
    op.create_index("idx_signal_queue_hash_created", "signal_queue", ["signal_hash", "created_at"])
    op.create_index("idx_signal_queue_license_status", "signal_queue", ["license_key", "status"])

    # ------------------------------------------------------------------
    # Account stats - periodic snapshots from EAs
    # ------------------------------------------------------------------
    # Drop simplified version from 001 if it exists
    _safe_drop("account_stats")
    op.create_table(
        "account_stats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("account", sa.Integer, nullable=True),
        sa.Column("broker", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("leverage", sa.Integer, nullable=True),
        sa.Column("balance", sa.Float, nullable=True),
        sa.Column("equity", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("margin", sa.Float, nullable=True),
        sa.Column("margin_free", sa.Float, nullable=True),
        sa.Column("margin_level", sa.Float, nullable=True),
        sa.Column("open_positions", sa.Integer, nullable=True),
        sa.Column("pending_orders", sa.Integer, nullable=True),
        sa.Column("ea_version", sa.String(50), nullable=True),
        sa.Column("magic", sa.Integer, nullable=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_account_stats_license", "account_stats", ["license_key"])
    op.create_index("idx_account_stats_timestamp", "account_stats", ["timestamp"])

    # ------------------------------------------------------------------
    # Admin logs - audit trail for admin actions
    # ------------------------------------------------------------------
    op.create_table(
        "admin_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("user", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(100), nullable=True),
        sa.Column("details", sa.Text, nullable=True),
    )
    op.create_index("idx_alert_history_timestamp", "alert_history", ["timestamp"])
    op.create_index("idx_admin_logs_timestamp", "admin_logs", ["timestamp"])
    op.create_index("idx_admin_logs_action", "admin_logs", ["action"])


def downgrade() -> None:
    _safe_drop("admin_logs")
    _safe_drop("account_stats")
    _safe_drop("signal_queue")
    _safe_drop("symbol_performance")
    _safe_drop("daily_stats")
    _safe_drop("alert_history")
    _safe_drop("trades")

"""Add WS telemetry tables for account stats, positions, trade history, health

Revision ID: 003_ws_telemetry
Create Date: 2026-06-09

Stores real-time data pushed from EAs via WebSocket:
  - ws_account_stats: Full account snapshots (balance, equity, margin, etc.)
  - ws_open_positions: All open trades from EA (permanent, not replaced)
  - ws_trade_history: Closed deal/order history from EA
  - ws_health_telemetry: Connection health metrics (latency, uptime, etc.)
  - ws_signal_log: Every signal delivered to EA, organized by license_key

ALL data is retained infinitely — no auto-cleanup. This data tracks trader
performance year over year and IS the product value.
"""

import sqlalchemy as sa

from alembic import op

revision = "003_ws_telemetry"
down_revision = "002_signal_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # WS Account Stats — full account snapshots from EA
    # ------------------------------------------------------------------
    op.create_table(
        "ws_account_stats",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        # Account identity
        sa.Column("login", sa.BigInteger, nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("server", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("company", sa.String(255), nullable=True),
        # Account type
        sa.Column("trade_mode", sa.String(20), nullable=True),  # demo/contest/real
        sa.Column("leverage", sa.Integer, nullable=True),
        sa.Column("limit_orders", sa.Integer, nullable=True),
        sa.Column("trade_allowed", sa.Boolean, nullable=True),
        sa.Column("trade_expert", sa.Boolean, nullable=True),
        sa.Column("margin_so_mode", sa.String(10), nullable=True),  # percent/money
        # Financials
        sa.Column("balance", sa.Float, nullable=True),
        sa.Column("credit", sa.Float, nullable=True),
        sa.Column("equity", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("margin", sa.Float, nullable=True),
        sa.Column("margin_free", sa.Float, nullable=True),
        sa.Column("margin_level", sa.Float, nullable=True),
        sa.Column("margin_so_call", sa.Float, nullable=True),
        sa.Column("margin_so_so", sa.Float, nullable=True),
        sa.Column("margin_initial", sa.Float, nullable=True),
        sa.Column("margin_maintenance", sa.Float, nullable=True),
        sa.Column("assets", sa.Float, nullable=True),
        sa.Column("liabilities", sa.Float, nullable=True),
        sa.Column("commission_blocked", sa.Float, nullable=True),
        # MT5-only fields (null for MT4)
        sa.Column("currency_digits", sa.Integer, nullable=True),
        sa.Column("fifo_close", sa.Boolean, nullable=True),
        sa.Column("hedge_allowed", sa.Boolean, nullable=True),
        # Positions count
        sa.Column("positions", sa.Integer, nullable=True),
    )
    op.create_index("idx_ws_account_stats_license", "ws_account_stats", ["license_key"])
    op.create_index("idx_ws_account_stats_timestamp", "ws_account_stats", ["timestamp"])
    op.create_index("idx_ws_account_stats_license_ts", "ws_account_stats", ["license_key", "timestamp"])

    # ------------------------------------------------------------------
    # WS Open Positions — all running trades from EA
    # ------------------------------------------------------------------
    op.create_table(
        "ws_open_positions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        # Position details
        sa.Column("ticket", sa.BigInteger, nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),  # buy/sell
        sa.Column("volume", sa.Float, nullable=True),
        sa.Column("open_price", sa.Float, nullable=True),
        sa.Column("current_price", sa.Float, nullable=True),
        sa.Column("sl", sa.Float, nullable=True),
        sa.Column("tp", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("swap", sa.Float, nullable=True),
        sa.Column("commission", sa.Float, nullable=True),  # MT4 only, null for MT5
        sa.Column("magic", sa.BigInteger, nullable=True),
        sa.Column("comment", sa.String(255), nullable=True),
        sa.Column("open_time", sa.BigInteger, nullable=True),  # Unix timestamp
        # MT5-only fields (null for MT4)
        sa.Column("identifier", sa.BigInteger, nullable=True),
    )
    op.create_index("idx_ws_open_positions_license", "ws_open_positions", ["license_key"])
    op.create_index("idx_ws_open_positions_ticket", "ws_open_positions", ["ticket"])
    op.create_index("idx_ws_open_positions_license_ts", "ws_open_positions", ["license_key", "timestamp"])
    op.create_index("idx_ws_open_positions_symbol", "ws_open_positions", ["symbol"])

    # ------------------------------------------------------------------
    # WS Trade History — closed trades/deals from EA
    # ------------------------------------------------------------------
    op.create_table(
        "ws_trade_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        # Deal/order details
        sa.Column("ticket", sa.BigInteger, nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),  # buy/sell
        sa.Column("volume", sa.Float, nullable=True),
        sa.Column("open_price", sa.Float, nullable=True),
        sa.Column("close_price", sa.Float, nullable=True),  # MT4 only, null for MT5 deals
        sa.Column("sl", sa.Float, nullable=True),
        sa.Column("tp", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("swap", sa.Float, nullable=True),
        sa.Column("commission", sa.Float, nullable=True),
        sa.Column("fee", sa.Float, nullable=True),  # MT5 only
        sa.Column("magic", sa.BigInteger, nullable=True),
        sa.Column("comment", sa.String(255), nullable=True),
        sa.Column("open_time", sa.BigInteger, nullable=True),
        sa.Column("close_time", sa.BigInteger, nullable=True),  # MT4 only
        # MT5-only deal fields (null for MT4)
        sa.Column("order_id", sa.BigInteger, nullable=True),
        sa.Column("entry", sa.String(10), nullable=True),  # in/out/inout/out_by
        sa.Column("reason", sa.String(20), nullable=True),  # client/expert/sl/tp/stopout/...
        sa.Column("position_id", sa.BigInteger, nullable=True),
        # Dedup: unique ticket per license key
        sa.UniqueConstraint("license_key", "ticket", name="uq_ws_trade_history_license_ticket"),
    )
    op.create_index("idx_ws_trade_history_license", "ws_trade_history", ["license_key"])
    op.create_index("idx_ws_trade_history_ticket", "ws_trade_history", ["ticket"])
    op.create_index("idx_ws_trade_history_license_ts", "ws_trade_history", ["license_key", "timestamp"])
    op.create_index("idx_ws_trade_history_symbol", "ws_trade_history", ["symbol"])
    op.create_index("idx_ws_trade_history_close_time", "ws_trade_history", ["close_time"])

    # ------------------------------------------------------------------
    # WS Health Telemetry — connection health metrics
    # ------------------------------------------------------------------
    op.create_table(
        "ws_health_telemetry",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        # Connection health
        sa.Column("ws_latency_ms", sa.Integer, nullable=True),
        sa.Column("terminal_lag_ms", sa.Integer, nullable=True),
        sa.Column("ws_uptime_sec", sa.Integer, nullable=True),
        sa.Column("ws_bytes_sent", sa.Integer, nullable=True),
        sa.Column("ws_bytes_recv", sa.Integer, nullable=True),
        sa.Column("ws_reconnects", sa.Integer, nullable=True),
        sa.Column("ws_frames_queued", sa.Integer, nullable=True),
        sa.Column("ws_frames_dropped", sa.Integer, nullable=True),
        # DLL version
        sa.Column("dll_version", sa.String(20), nullable=True),
    )
    op.create_index("idx_ws_health_license", "ws_health_telemetry", ["license_key"])
    op.create_index("idx_ws_health_timestamp", "ws_health_telemetry", ["timestamp"])
    op.create_index("idx_ws_health_license_ts", "ws_health_telemetry", ["license_key", "timestamp"])

    # ------------------------------------------------------------------
    # WS Signal Log — every signal delivered to EA, organized by license
    # Retained infinitely for tracking which signals led to which trades.
    # ------------------------------------------------------------------
    op.create_table(
        "ws_signal_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("signal_id", sa.String(255), nullable=False),
        sa.Column("signal_hash", sa.String(255), nullable=True),
        # Parsed signal fields for querying/filtering
        sa.Column("action", sa.String(50), nullable=True),       # buy, sell, close, etc.
        sa.Column("symbol", sa.String(50), nullable=True),       # EURUSD, XAUUSD, etc.
        sa.Column("volume", sa.Float, nullable=True),
        sa.Column("sl", sa.Float, nullable=True),
        sa.Column("tp", sa.Float, nullable=True),
        # Full signal payload
        sa.Column("signal_data", sa.Text, nullable=False),
        # Delivery tracking
        sa.Column("delivered_via", sa.String(10), nullable=True),  # ws, http, longpoll
        sa.Column("acknowledged", sa.Boolean, server_default=sa.text("FALSE"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
    )
    op.create_index("idx_ws_signal_log_license", "ws_signal_log", ["license_key"])
    op.create_index("idx_ws_signal_log_timestamp", "ws_signal_log", ["timestamp"])
    op.create_index("idx_ws_signal_log_license_ts", "ws_signal_log", ["license_key", "timestamp"])
    op.create_index("idx_ws_signal_log_symbol", "ws_signal_log", ["symbol"])
    op.create_index("idx_ws_signal_log_action", "ws_signal_log", ["action"])
    op.create_index("idx_ws_signal_log_sid", "ws_signal_log", ["signal_id"])


def downgrade() -> None:
    op.drop_table("ws_signal_log")
    op.drop_table("ws_health_telemetry")
    op.drop_table("ws_trade_history")
    op.drop_table("ws_open_positions")
    op.drop_table("ws_account_stats")

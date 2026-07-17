"""Add license_key + signal_id to trades, execution tracking to ws_signal_log

Revision ID: 004_trades_licensing
Create Date: 2026-06-09

Critical data organization fixes:
1. Add license_key to trades table — every trade MUST belong to a license.
   Without this, trades are anonymous; can't be organized per user.
2. Add signal_id to trades table — links signal → execution result.
   Without this, can't tell which signal led to which trade.
3. Add execution tracking to ws_signal_log — a signal being delivered
   (acknowledged) is NOT the same as being executed. Add:
   - execution_status: pending/delivered/executing/executed/failed/error
   - execution_detail: error message or result summary
   - executed_at: when the EA actually executed (or failed) the signal
   - ticket: the MT4/5 ticket number from execution
"""

import sqlalchemy as sa

from alembic import op

revision = "004_trades_licensing"
down_revision = "003_ws_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Add license_key to trades table
    # ------------------------------------------------------------------
    op.add_column("trades", sa.Column("license_key", sa.String(255), nullable=True))
    op.create_index("idx_trades_license_key", "trades", ["license_key"])
    op.create_index("idx_trades_license_timestamp", "trades", ["license_key", "timestamp"])

    # ------------------------------------------------------------------
    # 2. Add signal_id to trades table
    #    Links a trade execution back to the signal that triggered it.
    # ------------------------------------------------------------------
    op.add_column("trades", sa.Column("signal_id", sa.String(255), nullable=True))
    op.create_index("idx_trades_signal_id", "trades", ["signal_id"])

    # ------------------------------------------------------------------
    # 3. Add execution tracking to ws_signal_log
    #    A signal being delivered (acknowledged) ≠ executed successfully.
    # ------------------------------------------------------------------
    op.add_column(
        "ws_signal_log",
        sa.Column(
            "execution_status",
            sa.String(20),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "ws_signal_log",
        sa.Column("execution_detail", sa.Text, nullable=True),
    )
    op.add_column(
        "ws_signal_log",
        sa.Column("executed_at", sa.DateTime, nullable=True),
    )
    op.add_column(
        "ws_signal_log",
        sa.Column("ticket", sa.String(50), nullable=True),
    )
    op.create_index("idx_ws_signal_log_exec_status", "ws_signal_log", ["execution_status"])


def downgrade() -> None:
    op.drop_index("idx_ws_signal_log_exec_status", "ws_signal_log")
    op.drop_column("ws_signal_log", "ticket")
    op.drop_column("ws_signal_log", "executed_at")
    op.drop_column("ws_signal_log", "execution_detail")
    op.drop_column("ws_signal_log", "execution_status")

    op.drop_index("idx_trades_signal_id", "trades")
    op.drop_column("trades", "signal_id")

    op.drop_index("idx_trades_license_timestamp", "trades")
    op.drop_index("idx_trades_license_key", "trades")
    op.drop_column("trades", "license_key")

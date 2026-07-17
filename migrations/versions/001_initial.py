"""Initial schema with indexes

Revision ID: 001_initial
Create Date: 2026-06-08
"""

import sqlalchemy as sa

from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Licenses table
    op.create_table(
        "licenses",
        sa.Column("license_key", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("features", sa.JSON, nullable=True),
        sa.Column("max_symbols", sa.Integer, nullable=False, server_default="25"),
        sa.Column("max_volume", sa.Float, nullable=False, server_default="100.0"),
        sa.Column("secret_key", sa.String(255), nullable=True),
        sa.Column("require_secret", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("allowed_symbols", sa.JSON, nullable=True),
        sa.Column("max_daily_trades", sa.Integer, nullable=False, server_default="100"),
        sa.Column("max_daily_loss", sa.Float, nullable=False, server_default="1000.0"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("added_by", sa.String(255), nullable=True),
    )
    op.create_index("idx_licenses_email", "licenses", ["email"])
    op.create_index("idx_licenses_status", "licenses", ["status"])

    # Signals table
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("signal_data", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("queued_at", sa.DateTime, nullable=False),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
    )
    op.create_index("idx_signals_license_key", "signals", ["license_key"])
    op.create_index("idx_signals_status", "signals", ["status"])
    op.create_index("idx_signals_queued_at", "signals", ["queued_at"])
    op.create_index("idx_signals_license_status", "signals", ["license_key", "status"])

    # Trades table
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("lots", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=True),
        sa.Column("sl", sa.Float, nullable=True),
        sa.Column("tp", sa.Float, nullable=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("ticket", sa.String(100), nullable=True),
        sa.Column("comment", sa.String(255), nullable=True),
    )
    op.create_index("idx_trades_license_key", "trades", ["license_key"])
    op.create_index("idx_trades_timestamp", "trades", ["timestamp"])
    op.create_index("idx_trades_license_timestamp", "trades", ["license_key", "timestamp"])

    # Admin logs table
    op.create_table(
        "admin_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("user", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(100), nullable=True),
        sa.Column("details", sa.Text, nullable=True),
    )
    op.create_index("idx_admin_logs_timestamp", "admin_logs", ["timestamp"])
    op.create_index("idx_admin_logs_action", "admin_logs", ["action"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS admin_logs")
    op.execute("DROP TABLE IF EXISTS trades")
    op.execute("DROP TABLE IF EXISTS signals")
    op.execute("DROP TABLE IF EXISTS licenses")

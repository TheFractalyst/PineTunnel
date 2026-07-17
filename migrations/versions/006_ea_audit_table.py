"""EA audit table - comprehensive account/system/telemetry persistence

Revision ID: 006_ea_audit
Revises: 005_bigint_columns
Create Date: 2026-06-10

Stores periodic audit snapshots from EA instances with full account,
terminal, system, VPS, network, and NTP data.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "006_ea_audit"
down_revision = "005_bigint_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ea_audit",
        # Identity
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("license_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False),
        # EA & DLL
        sa.Column("platform", sa.String(10), nullable=True),          # mt5/mt4
        sa.Column("ea_version", sa.String(50), nullable=True),
        sa.Column("dll_version", sa.String(20), nullable=True),
        sa.Column("dll_system_info", sa.Text, nullable=True),          # JSON from DLL
        # Terminal
        sa.Column("mt_build", sa.Integer, nullable=True),
        sa.Column("terminal_name", sa.String(255), nullable=True),
        sa.Column("terminal_language", sa.String(10), nullable=True),
        sa.Column("terminal_x64", sa.Boolean, nullable=True),
        sa.Column("terminal_pid", sa.Integer, nullable=True),
        sa.Column("os", sa.String(255), nullable=True),
        sa.Column("cpu_cores", sa.Integer, nullable=True),
        sa.Column("cpu_freq_mhz", sa.Integer, nullable=True),
        sa.Column("ram_mb", sa.Integer, nullable=True),
        sa.Column("ram_avail_mb", sa.Integer, nullable=True),
        sa.Column("disk_mb", sa.Integer, nullable=True),
        # Account identity
        sa.Column("account_number", sa.BigInteger, nullable=True),
        sa.Column("account_name", sa.String(255), nullable=True),
        sa.Column("account_server", sa.String(255), nullable=True),
        sa.Column("account_currency", sa.String(10), nullable=True),
        sa.Column("broker", sa.String(255), nullable=True),
        sa.Column("trade_mode", sa.String(20), nullable=True),        # demo/contest/real
        sa.Column("leverage", sa.Integer, nullable=True),
        sa.Column("limit_orders", sa.Integer, nullable=True),
        sa.Column("trade_allowed", sa.Boolean, nullable=True),
        sa.Column("trade_expert", sa.Boolean, nullable=True),
        sa.Column("margin_so_mode", sa.String(10), nullable=True),     # percent/money
        # Account financials
        sa.Column("balance", sa.Float, nullable=True),
        sa.Column("credit", sa.Float, nullable=True),
        sa.Column("equity", sa.Float, nullable=True),
        sa.Column("profit", sa.Float, nullable=True),
        sa.Column("margin", sa.Float, nullable=True),
        sa.Column("margin_free", sa.Float, nullable=True),
        sa.Column("margin_level", sa.Float, nullable=True),
        sa.Column("margin_so_call", sa.Float, nullable=True),
        sa.Column("margin_so_so", sa.Float, nullable=True),
        # Chart
        sa.Column("chart_symbol", sa.String(50), nullable=True),
        sa.Column("chart_timeframe", sa.String(10), nullable=True),
        # Runtime
        sa.Column("symbol_count", sa.Integer, nullable=True),
        sa.Column("position_count", sa.Integer, nullable=True),
        sa.Column("uptime_sec", sa.Integer, nullable=True),
        sa.Column("ws_status", sa.String(20), nullable=True),
        sa.Column("error_count", sa.Integer, nullable=True),
        sa.Column("connection_mode", sa.String(20), nullable=True),
        sa.Column("magic", sa.Integer, nullable=True),
        sa.Column("auto_update_enabled", sa.Boolean, nullable=True),
        # VPS detection
        sa.Column("is_vps", sa.Boolean, nullable=True),
        sa.Column("vps_provider", sa.String(64), nullable=True),
        sa.Column("vps_manufacturer", sa.String(128), nullable=True),
        sa.Column("vps_model", sa.String(128), nullable=True),
        # Network diagnostics
        sa.Column("net_quality", sa.String(16), nullable=True),       # Good/Moderate/Bad
        sa.Column("net_ping_ms", sa.Integer, nullable=True),
        sa.Column("net_jitter_ms", sa.Integer, nullable=True),
        sa.Column("net_loss_pct", sa.Float, nullable=True),
        # NTP time sync
        sa.Column("ntp_drift_ms", sa.Integer, nullable=True),
        sa.Column("ntp_sync_success", sa.Boolean, nullable=True),
    )
    op.create_index("idx_ea_audit_license", "ea_audit", ["license_key"])
    op.create_index("idx_ea_audit_timestamp", "ea_audit", ["timestamp"])
    op.create_index("idx_ea_audit_license_ts", "ea_audit", ["license_key", "timestamp"])


def downgrade() -> None:
    op.drop_index("idx_ea_audit_license_ts", table_name="ea_audit")
    op.drop_index("idx_ea_audit_timestamp", table_name="ea_audit")
    op.drop_index("idx_ea_audit_license", table_name="ea_audit")
    op.drop_table("ea_audit")

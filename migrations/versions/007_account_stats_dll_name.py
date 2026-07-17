"""Add dll_version and account_name columns to account_stats

Revision ID: 007_account_stats_dll_name
Revises: 006_ea_audit
Create Date: 2026-06-10
"""

from alembic import op
import sqlalchemy as sa

revision = "007_account_stats_dll_name"
down_revision = "006_ea_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_stats", sa.Column("dll_version", sa.String(20), nullable=True))
    op.add_column("account_stats", sa.Column("account_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("account_stats", "account_name")
    op.drop_column("account_stats", "dll_version")

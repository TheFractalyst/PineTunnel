"""Change INTEGER to BIGINT for MT5 ulong-compatible columns

Revision ID: 005_bigint_columns
Create Date: 2026-06-09

MT5 ticket, deal, and account numbers are ulong (unsigned 64-bit) which can
exceed PostgreSQL INTEGER range (max 2,147,483,647). The account_stats INSERT
was failing with "integer out of range" for account values like 4,000,071,647.

Changes:
- trades: ticket INTEGER -> BIGINT, deal INTEGER -> BIGINT, magic INTEGER -> BIGINT
- account_stats: account INTEGER -> BIGINT, magic INTEGER -> BIGINT
"""

import sqlalchemy as sa

from alembic import op

revision = "005_bigint_columns"
down_revision = "004_trades_licensing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # SQLite does not support ALTER COLUMN TYPE and already stores 64-bit integers
    # in INTEGER columns, so this migration is a no-op on SQLite.
    if dialect == "sqlite":
        return

    # trades: MT5 ticket/deal are ulong, magic can be large
    op.alter_column("trades", "ticket", type_=sa.BigInteger, existing_type=sa.Integer)
    op.alter_column("trades", "deal", type_=sa.BigInteger, existing_type=sa.Integer)
    op.alter_column("trades", "magic", type_=sa.BigInteger, existing_type=sa.Integer)

    # account_stats: account numbers can exceed 32-bit INTEGER range
    op.alter_column("account_stats", "account", type_=sa.BigInteger, existing_type=sa.Integer)
    op.alter_column("account_stats", "magic", type_=sa.BigInteger, existing_type=sa.Integer)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    op.alter_column("account_stats", "magic", type_=sa.Integer, existing_type=sa.BigInteger)
    op.alter_column("account_stats", "account", type_=sa.Integer, existing_type=sa.BigInteger)

    op.alter_column("trades", "magic", type_=sa.Integer, existing_type=sa.BigInteger)
    op.alter_column("trades", "deal", type_=sa.Integer, existing_type=sa.BigInteger)
    op.alter_column("trades", "ticket", type_=sa.Integer, existing_type=sa.BigInteger)

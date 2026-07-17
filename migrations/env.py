"""Alembic environment configuration.

Offline mode  : generate SQL scripts without a live DB connection
Online mode   : connect to the database and execute migrations directly
"""

import sys

# Block psycopg2 - incompatible with Python 3.13 (cached .so in venv)
sys.modules["psycopg2"] = None  # type: ignore[assignment]

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.exc import OperationalError

from apps.server.config.settings import get_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Get database URL from app config - PostgreSQL is required
app_config = get_config()
database_url = app_config.database.url
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is required for Alembic migrations. "
        "Set the DATABASE_URL environment variable to a PostgreSQL connection string."
    )
# Use psycopg (v3) dialect for Python 3.13+ compatibility
if database_url.startswith("postgresql://") and "+psycopg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
# Ensure sslmode=require is in URL for psycopg3 (can't use options string)
# Only applies to PostgreSQL - SQLite ignores it but may error on connect
if database_url.startswith("postgresql") and "sslmode=" not in database_url:
    separator = "&" if "?" in database_url else "?"
    database_url += f"{separator}sslmode=require"
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL DDL without requiring a database connection.
    The output can be reviewed and applied manually to production.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("No sqlalchemy.url configured for offline migration")
    print(f"[alembic] OFFLINE mode - generating SQL for URL: {url.split('@')[-1] if '@' in url else '(unset)'}")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
    print("[alembic] OFFLINE migration SQL generated successfully")


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Connects to the database and executes migrations directly.
    Includes error handling for connection failures.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    db_url = config.get_main_option("sqlalchemy.url") or ""
    target = db_url.split("@")[-1] if "@" in db_url else "(unknown host)"
    print(f"[alembic] ONLINE mode - connecting to: {target}")
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    except OperationalError as exc:
        print(f"[alembic] FATAL: Could not connect to database at {target}")
        print(f"[alembic] Error: {exc.orig}")
        raise RuntimeError(
            f"Database connection failed: {exc.orig}\n"
            "Check DATABASE_URL, network connectivity, and credentials."
        ) from exc
    except Exception as exc:
        print(f"[alembic] FATAL: Migration failed - {exc}")
        raise
    print("[alembic] ONLINE migration completed successfully")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

#!/usr/bin/env bash
# Test script: verify all Alembic migrations work on a clean SQLite database.
# Creates a fresh DB, runs all migrations up, verifies tables, runs downgrade,
# verifies tables are gone.
#
# Usage: ./scripts/test_migrations.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEST_DB="/tmp/pinetunnel_migration_test.db"

cd "$PROJECT_DIR"

# Verify alembic is available
if ! python -c "import alembic" 2>/dev/null; then
    echo "FAIL: alembic is not installed. Install with: pip install alembic"
    exit 1
fi

# Clean up any previous test DB
rm -f "$TEST_DB"

# Use SQLite for testing (PostgreSQL-only sslmode is skipped by env.py)
export DATABASE_URL="sqlite:///$TEST_DB"

echo "========================================"
echo "  PineTunnel Migration Test"
echo "  Database: $TEST_DB"
echo "========================================"

# Step 1: Run all migrations up
echo ""
echo "[1/4] Running alembic upgrade head..."
if ! alembic upgrade head 2>&1; then
    echo "FAIL: alembic upgrade head failed"
    rm -f "$TEST_DB"
    exit 1
fi
echo "PASS: All migrations applied successfully"

# Step 2: Verify expected tables exist
echo ""
echo "[2/4] Verifying tables exist..."

EXPECTED_TABLES=(
    "licenses"
    "trades"
    "admin_logs"
    "alert_history"
    "daily_stats"
    "symbol_performance"
    "signal_queue"
    "account_stats"
    "ws_account_stats"
    "ws_open_positions"
    "ws_trade_history"
    "ws_health_telemetry"
    "ws_signal_log"
    "ea_audit"
)

ALL_OK=1
for table in "${EXPECTED_TABLES[@]}"; do
    count=$(python -c "
import sqlite3
conn = sqlite3.connect('$TEST_DB')
result = conn.execute(\"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='$table'\").fetchone()
print(result[0])
conn.close()
" 2>/dev/null)
    if [ "$count" = "1" ]; then
        echo "  OK: $table"
    else
        echo "  MISSING: $table (count=$count)"
        ALL_OK=0
    fi
done

if [ "$ALL_OK" = "0" ]; then
    echo "FAIL: Some expected tables are missing"
    rm -f "$TEST_DB"
    exit 1
fi
echo "PASS: All ${#EXPECTED_TABLES[@]} expected tables exist"

# Step 3: Run downgrade to base (rolls back ALL migrations)
echo ""
echo "[3/4] Running alembic downgrade base..."
if ! alembic downgrade base 2>&1; then
    echo "FAIL: alembic downgrade base failed"
    rm -f "$TEST_DB"
    exit 1
fi
echo "PASS: All migrations rolled back successfully"

# Step 4: Verify all application tables are gone
# (alembic_version table may or may not remain - that is OK)
echo ""
echo "[4/4] Verifying tables are gone..."
TABLE_COUNT=$(python -c "
import sqlite3
conn = sqlite3.connect('$TEST_DB')
result = conn.execute(\"SELECT count(*) FROM sqlite_master WHERE type='table' AND name != 'alembic_version'\").fetchone()
print(result[0])
conn.close()
" 2>/dev/null)

if [ "$TABLE_COUNT" = "0" ]; then
    echo "PASS: All application tables removed after downgrade"
else
    echo "FAIL: $TABLE_COUNT application tables still exist after downgrade"
    python -c "
import sqlite3
conn = sqlite3.connect('$TEST_DB')
for row in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'\").fetchall():
    print(f'  remaining: {row[0]}')
conn.close()
" 2>/dev/null
    rm -f "$TEST_DB"
    exit 1
fi

# Cleanup
rm -f "$TEST_DB"

echo ""
echo "========================================"
echo "  ALL MIGRATION TESTS PASSED"
echo "========================================"

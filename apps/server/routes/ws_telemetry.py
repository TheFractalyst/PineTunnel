"""WS Telemetry API — admin endpoints for querying stored EA telemetry data.

v2: graceful degradation for missing ws_* tables (200 + empty data instead of 500).

Security architecture (HMAC-signed, no MITM vulnerability):

  EA → Server:   WSS (TLS 1.2+) with cert revocation checking + fallback prevention
  Server → DB:   PostgreSQL with sslmode=require (enforced in connect_args)
  Admin → Server: HTTPS via Cloudflare (TLS termination) + HSTS (1-year max-age)
                 + X-Admin-Key auth (constant-time comparison via hmac.compare_digest)
  DB queries:    Parameterized SQL (no injection risk)
  License data:  License key validated on WS connect; stored data scoped to that key

All endpoints require X-Admin-Key header.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from .auth import _verify_admin_key

logger = logging.getLogger(__name__)


def _serialize_datetimes(d: dict[str, Any]) -> None:
    """Convert datetime values in ``d`` to ISO-format strings in-place."""
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()


router = APIRouter(prefix="/api/ea/ws-telemetry", tags=["ws-telemetry"])


def _get_db(request: Request) -> Any:
    """Get database manager from app state."""
    from apps.server.state import db_manager

    db = db_manager
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _query_ws_table(
    db: Any,
    sql: str,
    params: dict[str, Any],
    license_key: str,
    result_key: str,
    programming_error_msg: str,
) -> dict[str, Any]:
    """Execute a WS telemetry query with graceful degradation for missing tables.

    Returns ``{license_key, result_key: [...]}`` on success,
    or ``{license_key, result_key: []}`` if the table doesn't exist yet.
    """
    try:
        with db.get_connection() as session:
            rows = session.execute(text(sql), params).fetchall()
            results = []
            for row in rows:
                d = dict(row._mapping)
                _serialize_datetimes(d)
                results.append(d)
            return {"license_key": license_key, result_key: results}
    except ProgrammingError as e:
        logger.warning("%s: %s", programming_error_msg, e)
        return {"license_key": license_key, result_key: []}
    except Exception as e:
        logger.error("%s: %s", programming_error_msg, e)
        return {"license_key": license_key, result_key: []}


@router.get("/account-stats/{license_key}")
async def get_ws_account_stats(
    license_key: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get WS account stats history for a license key."""
    db = _get_db(request)
    return _query_ws_table(
        db,
        "SELECT * FROM ws_account_stats WHERE license_key = :lk "
        "ORDER BY timestamp DESC LIMIT :limit",
        {"lk": license_key, "limit": limit},
        license_key,
        "stats",
        "ws_account_stats query failed (table may not exist)",
    )


@router.get("/open-positions/{license_key}")
async def get_ws_open_positions(
    license_key: str,
    request: Request,
    snapshot: str = Query(default="latest", description="latest, or ISO datetime for historical"),
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get open positions for a license key.

    By default returns the latest snapshot. Pass snapshot=ISO-datetime to
    query historical positions (e.g. snapshot=2026-01-15T12:00:00).
    """
    db = _get_db(request)
    if snapshot == "latest":
        sql = (
            "SELECT * FROM ws_open_positions WHERE license_key = :lk "
            "AND timestamp = ("
            "  SELECT MAX(timestamp) FROM ws_open_positions WHERE license_key = :lk"
            ") ORDER BY ticket"
        )
        params: dict[str, Any] = {"lk": license_key}
    else:
        sql = (
            "SELECT * FROM ws_open_positions WHERE license_key = :lk "
            "AND timestamp <= :ts "
            "AND timestamp = ("
            "  SELECT MAX(timestamp) FROM ws_open_positions "
            "  WHERE license_key = :lk AND timestamp <= :ts"
            ") ORDER BY ticket"
        )
        params = {"lk": license_key, "ts": snapshot}
    return _query_ws_table(
        db,
        sql,
        params,
        license_key,
        "positions",
        "ws_open_positions query failed (table may not exist)",
    )


@router.get("/trade-history/{license_key}")
async def get_ws_trade_history(
    license_key: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=2000),
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get WS trade history for a license key."""
    db = _get_db(request)
    return _query_ws_table(
        db,
        "SELECT * FROM ws_trade_history WHERE license_key = :lk "
        "ORDER BY close_time DESC NULLS LAST, open_time DESC LIMIT :limit",
        {"lk": license_key, "limit": limit},
        license_key,
        "deals",
        "ws_trade_history query failed (table may not exist)",
    )


@router.get("/health/{license_key}")
async def get_ws_health(
    license_key: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get WS health telemetry history for a license key."""
    db = _get_db(request)
    return _query_ws_table(
        db,
        "SELECT * FROM ws_health_telemetry WHERE license_key = :lk "
        "ORDER BY timestamp DESC LIMIT :limit",
        {"lk": license_key, "limit": limit},
        license_key,
        "health",
        "ws_health_telemetry query failed (table may not exist)",
    )


@router.get("/overview")
async def get_ws_telemetry_overview(
    request: Request,
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get overview: all license keys with their latest account stats + health."""
    db = _get_db(request)
    stats_map: dict[str, dict] = {}
    health_map: dict[str, dict] = {}
    pos_map: dict[str, int] = {}

    try:
        with db.get_connection() as session:

            # Latest account stats per license key
            try:
                stats_rows = session.execute(
                    text(
                        "SELECT DISTINCT ON (license_key) license_key, login, name, server, "
                        "currency, company, trade_mode, leverage, balance, equity, profit, "
                        "margin, margin_free, margin_level, positions, timestamp "
                        "FROM ws_account_stats ORDER BY license_key, timestamp DESC"
                    ),
                ).fetchall()
                for row in stats_rows:
                    d = dict(row._mapping)
                    _serialize_datetimes(d)
                    stats_map[d["license_key"]] = d
            except ProgrammingError as e:
                logger.warning(
                    "overview: ws_account_stats query skipped (table may not exist): %s", e
                )

            # Latest health per license key
            try:
                health_rows = session.execute(
                    text(
                        "SELECT DISTINCT ON (license_key) license_key, ws_latency_ms, "
                        "terminal_lag_ms, ws_uptime_sec, ws_frames_dropped, dll_version, "
                        "timestamp "
                        "FROM ws_health_telemetry ORDER BY license_key, timestamp DESC"
                    ),
                ).fetchall()
                for row in health_rows:
                    d = dict(row._mapping)
                    _serialize_datetimes(d)
                    health_map[d["license_key"]] = d
            except ProgrammingError as e:
                logger.warning(
                    "overview: ws_health_telemetry query skipped (table may not exist): %s", e
                )

            # Current open positions count per license key (latest snapshot)
            try:
                pos_rows = session.execute(
                    text(
                        "SELECT license_key, COUNT(*) as position_count FROM ("
                        "  SELECT DISTINCT ON (license_key, ticket) * FROM ws_open_positions "
                        "  ORDER BY license_key, ticket, timestamp DESC"
                        ") sub GROUP BY license_key"
                    ),
                ).fetchall()
                pos_map = {row[0]: row[1] for row in pos_rows}
            except ProgrammingError as e:
                logger.warning(
                    "overview: ws_open_positions query skipped (table may not exist): %s", e
                )
    except Exception as e:
        logger.error("overview: database session failed: %s", e)

    # Merge all data
    all_keys = stats_map.keys() | health_map.keys() | pos_map.keys()
    overview = []
    for lk in sorted(all_keys):
        entry: dict[str, Any] = {"license_key": lk}
        if lk in stats_map:
            entry["account"] = stats_map[lk]
        if lk in health_map:
            entry["health"] = health_map[lk]
        entry["open_position_count"] = pos_map.get(lk, 0)
        overview.append(entry)

    return {"total_licenses": len(overview), "licenses": overview}


@router.get("/signal-log/{license_key}")
async def get_ws_signal_log(
    license_key: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    action: str | None = Query(
        default=None, description="Filter by action (buy, sell, close, etc.)"
    ),
    symbol: str | None = Query(default=None, description="Filter by symbol (EURUSD, XAUUSD, etc.)"),
    status: str | None = Query(
        default=None, description="Filter by execution status: pending/delivered/executed/failed"
    ),
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Get signal log for a license key with execution tracking.

    Shows the full lifecycle: signal received → delivered → executed/failed.
    Filter by execution_status to find signals that weren't executed.
    """
    db = _get_db(request)
    try:
        with db.get_connection() as session:

            where_clause = "WHERE license_key = :lk"
            params: dict[str, Any] = {"lk": license_key, "limit": limit}
            if action:
                where_clause += " AND action = :action"
                params["action"] = action
            if symbol:
                where_clause += " AND symbol = :symbol"
                params["symbol"] = symbol
            if status:
                where_clause += " AND execution_status = :status"
                params["status"] = status

            rows = session.execute(
                text(
                    f"SELECT id, license_key, timestamp, signal_id, signal_hash, "
                    f"action, symbol, volume, sl, tp, "
                    f"delivered_via, acknowledged, acknowledged_at, "
                    f"execution_status, execution_detail, executed_at, ticket "
                    f"FROM ws_signal_log {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT :limit"
                ),
                params,
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row._mapping)
                _serialize_datetimes(d)
                results.append(d)
            return {"license_key": license_key, "signals": results, "count": len(results)}
    except ProgrammingError as e:
        logger.warning("ws_signal_log query failed (table may not exist): %s", e)
        return {"license_key": license_key, "signals": [], "count": 0}
    except Exception as e:
        logger.error("ws_signal_log query failed: %s", e)
        return {"license_key": license_key, "signals": [], "count": 0}


@router.get("/license-overview/{license_key}")
async def get_license_overview(
    license_key: str,
    request: Request,
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Comprehensive per-license overview: signals, execution status, trades, positions.

    This joins all data for a single license to show the full lifecycle:
    signal received → delivered → executed/failed → resulting trade.
    """
    db = _get_db(request)
    result: dict[str, Any] = {"license_key": license_key}

    try:
        with db.get_connection() as session:

            # Signal execution summary (from ws_signal_log — always available)
            signal_counts: dict[str, int] = {}
            try:
                signal_summary = session.execute(
                    text(
                        "SELECT execution_status, COUNT(*) as count "
                        "FROM ws_signal_log WHERE license_key = :lk "
                        "GROUP BY execution_status"
                    ),
                    {"lk": license_key},
                ).fetchall()
                signal_counts = {row[0]: row[1] for row in signal_summary}
            except ProgrammingError as e:
                logger.warning("license-overview: ws_signal_log query skipped: %s", e)

            # Latest account stats
            stats_row = None
            try:
                stats_row = session.execute(
                    text(
                        "SELECT * FROM ws_account_stats WHERE license_key = :lk "
                        "ORDER BY timestamp DESC LIMIT 1"
                    ),
                    {"lk": license_key},
                ).first()
            except ProgrammingError as e:
                logger.warning("license-overview: ws_account_stats query skipped: %s", e)

            # Current open positions (latest snapshot)
            pos_rows = []
            try:
                pos_rows = session.execute(
                    text(
                        "SELECT * FROM ws_open_positions WHERE license_key = :lk "
                        "AND timestamp = ("
                        "  SELECT MAX(timestamp) FROM ws_open_positions WHERE license_key = :lk"
                        ") ORDER BY ticket"
                    ),
                    {"lk": license_key},
                ).fetchall()
            except ProgrammingError as e:
                logger.warning("license-overview: ws_open_positions query skipped: %s", e)

            # Recent failed signals (last 50)
            failed_signals = []
            try:
                failed_signals = session.execute(
                    text(
                        "SELECT signal_id, action, symbol, timestamp, execution_detail, ticket "
                        "FROM ws_signal_log WHERE license_key = :lk "
                        "AND execution_status = 'failed' "
                        "ORDER BY timestamp DESC LIMIT 50"
                    ),
                    {"lk": license_key},
                ).fetchall()
            except ProgrammingError as e:
                logger.warning("license-overview: ws_signal_log failed query skipped: %s", e)

            # Recent trades (last 20)
            recent_trades = []
            try:
                recent_trades = session.execute(
                    text(
                        "SELECT id, signal_id, ticket, symbol, action, volume, profit, "
                        "status, error, timestamp FROM trades WHERE license_key = :lk "
                        "ORDER BY timestamp DESC LIMIT 20"
                    ),
                    {"lk": license_key},
                ).fetchall()
            except ProgrammingError as e:
                logger.warning("license-overview: trades query skipped: %s", e)

            # Latest health
            health_row = None
            try:
                health_row = session.execute(
                    text(
                        "SELECT * FROM ws_health_telemetry WHERE license_key = :lk "
                        "ORDER BY timestamp DESC LIMIT 1"
                    ),
                    {"lk": license_key},
                ).first()
            except ProgrammingError as e:
                logger.warning("license-overview: ws_health_telemetry query skipped: %s", e)

            # Signal execution stats
            result["signals"] = {
                "total": sum(signal_counts.values()),
                "executed": signal_counts.get("executed", 0),
                "failed": signal_counts.get("failed", 0),
                "delivered": signal_counts.get("delivered", 0),
                "pending": signal_counts.get("pending", 0),
                "error": signal_counts.get("error", 0),
                "partial": signal_counts.get("partial", 0),
            }

            # Failed signals (actionable items)
            result["failed_signals"] = [
                {
                    "signal_id": row[0],
                    "action": row[1],
                    "symbol": row[2],
                    "timestamp": (
                        row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3])
                    ),
                    "error": row[4],
                    "ticket": row[5],
                }
                for row in failed_signals
            ]

            # Account stats
            if stats_row:
                d = dict(stats_row._mapping)
                _serialize_datetimes(d)
                result["account"] = d

            # Positions
            positions = []
            for row in pos_rows:
                d = dict(row._mapping)
                if hasattr(d.get("timestamp"), "isoformat"):
                    d["timestamp"] = d["timestamp"].isoformat()
                positions.append(d)
            result["open_positions"] = positions

            # Recent trades (linked to signals when signal_id exists)
            result["recent_trades"] = [
                {
                    "id": row[0],
                    "signal_id": row[1],
                    "ticket": row[2],
                    "symbol": row[3],
                    "action": row[4],
                    "volume": row[5],
                    "profit": row[6],
                    "status": row[7],
                    "error": row[8],
                    "timestamp": (
                        row[9].isoformat() if hasattr(row[9], "isoformat") else str(row[9])
                    ),
                }
                for row in recent_trades
            ]

            # Health
            if health_row:
                d = dict(health_row._mapping)
                _serialize_datetimes(d)
                result["health"] = d

            return result
    except Exception as e:
        logger.error("license-overview: unexpected error: %s", e)
        result["error"] = "Failed to retrieve license overview"
        return result


# =======================================================================
# Per-User Endpoints (aggregate across all licenses for a user)
# =======================================================================


def _get_licenses_for_email(email: str, db: Any) -> list[str]:
    """Look up all license keys belonging to a user (by email) from client manager."""
    from apps.server.state import client_manager

    if not client_manager:
        return []

    try:
        keys = [
            key
            for key, data in client_manager.clients.items()
            if isinstance(data, dict) and (data.get("email", "") or "").lower() == email.lower()
        ]
        return keys
    except Exception:
        logger.warning("Failed to query licenses for email %s", email)
        return []


def _serialize_rows(rows: list, max_decimals: int = 6) -> list[dict]:
    """Convert SQLAlchemy rows to dicts, serializing datetimes."""
    results = []
    for row in rows:
        d = dict(row._mapping)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif isinstance(v, float):
                d[k] = round(v, max_decimals)
        results.append(d)
    return results


@router.get("/users")
async def list_all_users(request: Request, _: None = Depends(_verify_admin_key)) -> dict[str, Any]:
    """List all users (grouped by email) with their license keys and aggregated stats.

    A user may have multiple licenses (accounts). This endpoint shows the full
    user->licenses map with high-level telemetry summaries.
    """
    from apps.server.state import client_manager

    db = _get_db(request)
    if not client_manager:
        raise HTTPException(status_code=503, detail="Client manager not available")

    # Get all licenses grouped by email from client_manager
    try:
        users: dict[str, dict] = {}
        for key, data in client_manager.clients.items():
            if not isinstance(data, dict):
                continue
            email = data.get("email", "") or ""
            if email not in users:
                users[email] = {"email": email, "name": data.get("name", ""), "licenses": []}
            users[email]["licenses"].append(
                {"license_key": key, "status": data.get("status", "active"), "enabled": data.get("enabled", True)}
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query licenses: {e}")

    # Batch all license keys into 5 queries instead of N*5 per-user queries.
    user_license_map: dict[str, list[str]] = {}
    all_license_keys: list[str] = []
    for email, user_data in users.items():
        keys = [lk["license_key"] for lk in user_data["licenses"]]
        user_license_map[email] = keys
        all_license_keys.extend(keys)

    try:
        with db.get_connection() as session:
            if not all_license_keys:
                for user_data in users.values():
                    user_data["stats"] = {
                        "total_signals": 0,
                        "total_trades": 0,
                        "total_positions": 0,
                    }
            else:
                placeholders = ", ".join(f":lk{i}" for i in range(len(all_license_keys)))
                params = {f"lk{i}": lk for i, lk in enumerate(all_license_keys)}

                signal_counts_by_lk: dict[str, int] = {}
                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(*) FROM ws_signal_log "
                            f"WHERE license_key IN ({placeholders}) GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    signal_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    logger.warning("users: ws_signal_log count skipped: %s", e)

                exec_by_lk: dict[str, dict[str, int]] = {}
                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, execution_status, COUNT(*) "
                            f"FROM ws_signal_log WHERE license_key IN ({placeholders}) "
                            f"GROUP BY license_key, execution_status"
                        ),
                        params,
                    ).fetchall()
                    for row in rows:
                        exec_by_lk.setdefault(row[0], {})[row[1]] = row[2]
                except ProgrammingError as e:
                    logger.warning("users: ws_signal_log exec breakdown skipped: %s", e)

                trade_counts_by_lk: dict[str, int] = {}
                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(*) FROM trades "
                            f"WHERE license_key IN ({placeholders}) GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    trade_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    logger.warning("users: trades count skipped: %s", e)

                pos_counts_by_lk: dict[str, int] = {}
                try:
                    rows = session.execute(
                        text(
                            f"SELECT license_key, COUNT(DISTINCT (license_key, ticket)) "
                            f"FROM ws_open_positions WHERE license_key IN ({placeholders}) "
                            f"GROUP BY license_key"
                        ),
                        params,
                    ).fetchall()
                    pos_counts_by_lk = {row[0]: row[1] for row in rows}
                except ProgrammingError as e:
                    logger.warning("users: ws_open_positions count skipped: %s", e)

                accounts_by_lk: dict[str, list[dict]] = {}
                try:
                    account_rows = session.execute(
                        text(
                            f"SELECT DISTINCT ON (license_key) license_key, balance, equity, "
                            f"profit, margin, margin_level, positions, timestamp "
                            f"FROM ws_account_stats WHERE license_key IN ({placeholders}) "
                            f"ORDER BY license_key, timestamp DESC"
                        ),
                        params,
                    ).fetchall()
                    all_accounts = _serialize_rows(account_rows)
                    for acc in all_accounts:
                        accounts_by_lk.setdefault(acc.get("license_key", ""), []).append(acc)
                except ProgrammingError as e:
                    logger.warning("users: ws_account_stats skipped: %s", e)

                for email, user_data in users.items():
                    keys = user_license_map[email]
                    if not keys:
                        user_data["stats"] = {
                            "total_signals": 0,
                            "total_trades": 0,
                            "total_positions": 0,
                        }
                        continue

                    user_exec_stats: dict[str, int] = {}
                    for k in keys:
                        for status, count in exec_by_lk.get(k, {}).items():
                            user_exec_stats[status] = user_exec_stats.get(status, 0) + count

                    user_accounts: list[dict] = []
                    for k in keys:
                        user_accounts.extend(accounts_by_lk.get(k, []))

                    user_data["stats"] = {
                        "total_signals": sum(signal_counts_by_lk.get(k, 0) for k in keys),
                        "execution_breakdown": user_exec_stats,
                        "total_trades": sum(trade_counts_by_lk.get(k, 0) for k in keys),
                        "total_positions": sum(pos_counts_by_lk.get(k, 0) for k in keys),
                        "accounts": user_accounts,
                    }
    except Exception as e:
        logger.error("users: telemetry summary failed: %s", e)
        for user_data in users.values():
            if "stats" not in user_data:
                user_data["stats"] = {"total_signals": 0, "total_trades": 0, "total_positions": 0}

    return {
        "total_users": len(users),
        "users": list(users.values()),
    }


@router.get("/user/{email:path}")
async def get_user_overview(
    email: str,
    request: Request,
    _: None = Depends(_verify_admin_key),
) -> dict[str, Any]:
    """Full per-user overview: aggregate all telemetry across all their licenses.

    This is the admin's single-pane-of-glass view for a user. Shows:
    - All their license keys (accounts)
    - Aggregated signal execution stats (how many executed vs failed)
    - Latest account stats per license
    - Current open positions per license
    - Recent failed signals with details
    - Recent trades with signal linkage
    - Health per license
    """
    db = _get_db(request)
    license_keys = _get_licenses_for_email(email, db)

    if not license_keys:
        raise HTTPException(status_code=404, detail=f"No licenses found for email: {email}")

    placeholders = ", ".join(f":lk{i}" for i in range(len(license_keys)))
    params = {f"lk{i}": lk for i, lk in enumerate(license_keys)}

    result: dict[str, Any] = {
        "email": email,
        "license_count": len(license_keys),
        "licenses": license_keys,
    }

    try:
        with db.get_connection() as session:

            # Signal execution breakdown
            try:
                exec_rows = session.execute(
                    text(
                        f"SELECT execution_status, COUNT(*) FROM ws_signal_log "
                        f"WHERE license_key IN ({placeholders}) GROUP BY execution_status"
                    ),
                    params,
                ).fetchall()
                result["signal_execution"] = {row[0]: row[1] for row in exec_rows}
            except ProgrammingError as e:
                logger.warning("user-overview: ws_signal_log exec skipped: %s", e)
                result["signal_execution"] = {}

            # Latest account stats per license
            try:
                account_rows = session.execute(
                    text(
                        f"SELECT DISTINCT ON (license_key) * FROM ws_account_stats "
                        f"WHERE license_key IN ({placeholders}) "
                        f"ORDER BY license_key, timestamp DESC"
                    ),
                    params,
                ).fetchall()
                result["accounts"] = _serialize_rows(account_rows)
            except ProgrammingError as e:
                logger.warning("user-overview: ws_account_stats skipped: %s", e)
                result["accounts"] = []

            # Current open positions per license
            try:
                pos_rows = session.execute(
                    text(
                        f"SELECT license_key, COUNT(*) as position_count FROM ("
                        f"  SELECT DISTINCT ON (license_key, ticket) * FROM ws_open_positions "
                        f"  WHERE license_key IN ({placeholders}) "
                        f"  ORDER BY license_key, ticket, timestamp DESC"
                        f") sub GROUP BY license_key"
                    ),
                    params,
                ).fetchall()
                result["positions"] = {row[0]: row[1] for row in pos_rows}
            except ProgrammingError as e:
                logger.warning("user-overview: ws_open_positions skipped: %s", e)
                result["positions"] = {}

            # Failed signals across all licenses (last 50)
            try:
                failed_signals = session.execute(
                    text(
                        f"SELECT license_key, signal_id, action, symbol, timestamp, "
                        f"execution_detail, ticket "
                        f"FROM ws_signal_log WHERE license_key IN ({placeholders}) "
                        f"AND execution_status = 'failed' "
                        f"ORDER BY timestamp DESC LIMIT 50"
                    ),
                    params,
                ).fetchall()
                result["failed_signals"] = [
                    {
                        "license_key": row[0],
                        "signal_id": row[1],
                        "action": row[2],
                        "symbol": row[3],
                        "timestamp": (
                            row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4])
                        ),
                        "error": row[5],
                        "ticket": row[6],
                    }
                    for row in failed_signals
                ]
            except ProgrammingError as e:
                logger.warning("user-overview: ws_signal_log failed signals skipped: %s", e)
                result["failed_signals"] = []

            # Recent trades across all licenses (last 50)
            try:
                trade_rows = session.execute(
                    text(
                        f"SELECT license_key, signal_id, ticket, symbol, action, volume, "
                        f"profit, status, error, timestamp "
                        f"FROM trades WHERE license_key IN ({placeholders}) "
                        f"ORDER BY timestamp DESC LIMIT 50"
                    ),
                    params,
                ).fetchall()
                result["recent_trades"] = [
                    {
                        "license_key": row[0],
                        "signal_id": row[1],
                        "ticket": row[2],
                        "symbol": row[3],
                        "action": row[4],
                        "volume": row[5],
                        "profit": row[6],
                        "status": row[7],
                        "error": row[8],
                        "timestamp": (
                            row[9].isoformat() if hasattr(row[9], "isoformat") else str(row[9])
                        ),
                    }
                    for row in trade_rows
                ]
            except ProgrammingError as e:
                logger.warning("user-overview: trades skipped: %s", e)
                result["recent_trades"] = []

            # Health per license
            try:
                health_rows = session.execute(
                    text(
                        f"SELECT DISTINCT ON (license_key) * FROM ws_health_telemetry "
                        f"WHERE license_key IN ({placeholders}) "
                        f"ORDER BY license_key, timestamp DESC"
                    ),
                    params,
                ).fetchall()
                result["health"] = _serialize_rows(health_rows)
            except ProgrammingError as e:
                logger.warning("user-overview: ws_health_telemetry skipped: %s", e)
                result["health"] = []

            return result
    except Exception as e:
        logger.error("user-overview: unexpected error: %s", e)
        return result

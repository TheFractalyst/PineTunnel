"""FastAPI APIRouter definitions for trade analytics endpoints."""

import asyncio
import html
import hmac
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from apps.server.config.settings import get_config
from apps.server.db.analytics_store import (
    _EQUITY_DROP_THRESHOLD,
    _MARGIN_WARNING_THRESHOLD,
    _STATS_ALERT_COOLDOWN,
    _stats_alert_cooldowns,
    account_stats_latest,
    get_trade_db_manager,
    license_stats,
    trade_reports,
)
from apps.server.models.trade import AccountStats, CloseReport, TradeReport
from apps.server.services.analytics_service import update_license_stats
from apps.server.utils import mask_string as _mask_key
from apps.server.utils.trade_auth import (
    _get_client_manager,
    get_current_user,
    get_current_user_optional,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["Trade Analytics"])
admin_router = APIRouter(tags=["Admin Dashboard"])
license_router = APIRouter(tags=["License Management"])


def _verify_license_and_secret(license_key: str, secret_key: str | None) -> None:
    """Verify license_key (always) and secret_key (if provided). Raises HTTPException on failure."""
    cm = _get_client_manager()
    if not cm:
        raise HTTPException(status_code=503, detail="License service unavailable")
    valid, msg = cm.validate_license(license_key)
    if not valid:
        raise HTTPException(status_code=403, detail=f"Invalid license: {msg}")

    # If secret_key is provided, verify it. If not, allow (backward compat with EAs < v1.2.0)
    if secret_key:
        client_data = cm.get_client_by_license(license_key)
        if not client_data or not client_data.get("secret_key"):
            raise HTTPException(
                status_code=403, detail="Secret key not configured for this license"
            )
        if not hmac.compare_digest(str(client_data.get("secret_key", "")), secret_key):
            raise HTTPException(status_code=403, detail="Invalid secret key")


@router.get("/health")
async def health_check(user: dict = Depends(get_current_user)):
    """Health check — requires webhook secret"""
    return {
        "status": "healthy",
        "total_reports": len(trade_reports),
        "licenses_tracked": len(license_stats),
        "database_available": get_trade_db_manager() is not None,
    }


@router.post("/report")
async def receive_trade_report(report: TradeReport):
    """
    Receive trade execution report from EA
    This is called by the EA after every trade attempt (success or failure)

    Expected fields from EA:
    - license_key: License identifier
    - action: BUY, SELL, CLOSE
    - symbol: Trading pair
    - volume: Position size
    - price: Entry/exit price
    - ticket: MT5 ticket number
    - success: Whether trade succeeded
    - error_msg: Error details if failed
    - magic: EA magic number
    - profit: Current profit (for open positions)
    - sl: Stop loss price
    - tp: Take profit price
    - commission: Trade commission
    - swap: Swap fees
    - timestamp: Trade timestamp
    - broker_time: Broker server time
    - account: MT5 account number
    - broker: Broker name
    """
    try:
        _verify_license_and_secret(report.license_key, report.secret_key)

        from apps.server.state import telegram_bot

        # Store in memory (always do this for fast access)
        trade_data = report.dict()
        trade_data["received_at"] = datetime.now().isoformat()
        trade_reports.append(trade_data)

        # Update license statistics
        update_license_stats(report)

        # Store in database if available (permanent record per license)
        trade_db_manager = get_trade_db_manager()
        if trade_db_manager:
            try:
                db_data = dict(trade_data)
                db_data["status"] = "success" if report.success else "failed"
                db_data["error"] = report.error_msg or ""
                trade_db_manager.log_trade(db_data)
            except Exception as e:
                logger.warning("Failed to log trade to database: %s", e)

        if report.success:
            logger.info(
                "[TradeReport] [OK] %s %s %s lots @ %s | " "License: %s | Account: %s | Ticket: %s",
                report.action,
                report.symbol,
                report.volume,
                report.price,
                report.license_key,
                report.account,
                report.ticket,
            )
        else:
            error_detail = report.error_msg if report.error_msg else "No error details provided"
            logger.warning(
                "[TradeReport] [FAIL] FAILED %s %s %s lots @ %s | "
                "License: %s | Account: %s | ERROR: %s",
                report.action,
                report.symbol,
                report.volume,
                report.price,
                report.license_key,
                report.account,
                error_detail,
            )
            # Fire-and-forget Telegram notification for trade failures
            try:
                if telegram_bot:
                    asyncio.create_task(
                        telegram_bot.on_trade_failure(report.license_key, error_detail)
                    )
            except Exception as e:
                logger.debug("Unexpected error: %s", e)

        # User-facing notification (fire-and-forget)
        try:
            if telegram_bot and telegram_bot._started:
                if report.success:
                    asyncio.create_task(telegram_bot.on_trade_executed(report))
                else:
                    asyncio.create_task(telegram_bot.on_trade_execution_failed(report))
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

        return {
            "status": "received",
            "message": "Trade report logged successfully",
            "ticket": report.ticket,
            "received_at": trade_data["received_at"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing trade report: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/close")
async def receive_close_report(report: CloseReport):
    """
    Receive position close report from EA
    This is called when a position is closed

    Expected fields from EA:
    - license_key: License identifier
    - action: Always "CLOSE"
    - symbol: Trading pair
    - ticket: MT5 ticket number
    - close_price: Price at which position was closed
    - profit: Final profit/loss of the trade
    - magic: EA magic number
    - timestamp: Close timestamp
    - account: MT5 account number
    """
    try:
        _verify_license_and_secret(report.license_key, report.secret_key)

        # Store close report
        close_data = report.dict()
        close_data["received_at"] = datetime.now().isoformat()
        close_data["success"] = True  # Close reports are always successful (trade closed)
        trade_reports.append(close_data)

        # Update profit statistics for the license
        if report.license_key not in license_stats:
            license_stats[report.license_key] = {
                "total_trades": 0,
                "successful_trades": 0,
                "failed_trades": 0,
                "total_volume": 0.0,
                "total_profit": 0.0,
                "win_rate": 0.0,
                "avg_profit": 0.0,
                "last_trade": None,
                "account_numbers": [],
                "active_symbols": [],
            }

        license_stats[report.license_key]["total_profit"] += report.profit

        logger.info(
            "[CloseReport] %s ticket #%s | Profit: $%.2f | License: %s | Close Price: %s",
            report.symbol,
            report.ticket,
            report.profit,
            report.license_key,
            report.close_price,
        )

        # User-facing position closed notification
        try:
            from apps.server.state import telegram_bot

            if telegram_bot and telegram_bot._started:
                asyncio.create_task(telegram_bot.on_position_closed(report))
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

        return {
            "status": "received",
            "message": "Close report logged successfully",
            "ticket": report.ticket,
            "profit": report.profit,
            "received_at": close_data["received_at"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing close report: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/stats")
async def receive_account_stats(
    stats: AccountStats,
    background_tasks: BackgroundTasks,
):
    """Receive periodic account stats snapshot from EA."""
    try:
        _verify_license_and_secret(stats.license_key, stats.secret_key)

        from apps.server.state import telegram_bot

        stats_data = stats.dict()
        stats_data["received_at"] = datetime.now().isoformat()

        account_stats_latest[stats.license_key] = stats_data

        trade_db_manager = get_trade_db_manager()
        if trade_db_manager and background_tasks:
            background_tasks.add_task(trade_db_manager.save_account_stats, stats_data)

        # Register this EA as active in PostgreSQL (single source of truth)
        try:
            from apps.server.state import db_manager

            if db_manager and hasattr(db_manager, "register_ea_connection"):
                await asyncio.to_thread(
                    db_manager.register_ea_connection, stats.license_key, "stats", os.getpid()
                )
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

        # Account stats alerts (fire-and-forget, with cooldown)
        try:
            now = time.time()

            # Margin call warning (margin level below threshold)
            if stats.margin_level > 0 and stats.margin_level < _MARGIN_WARNING_THRESHOLD:
                cooldown_key = f"{stats.license_key}:margin"
                if now - _stats_alert_cooldowns.get(cooldown_key, 0) >= _STATS_ALERT_COOLDOWN:
                    _stats_alert_cooldowns[cooldown_key] = now
                    try:
                        if telegram_bot:
                            asyncio.create_task(
                                telegram_bot.notify_admin(
                                    f"[!] Margin Warning: `{stats.license_key}` "
                                    f"margin_level={stats.margin_level:.0f}%"
                                )
                            )
                    except Exception as e:
                        logger.debug("Unexpected error: %s", e)

            # Equity drop (drawdown exceeding threshold)
            if stats.balance > 0 and stats.equity / stats.balance < _EQUITY_DROP_THRESHOLD:
                cooldown_key = f"{stats.license_key}:equity_drop"
                if now - _stats_alert_cooldowns.get(cooldown_key, 0) >= _STATS_ALERT_COOLDOWN:
                    _stats_alert_cooldowns[cooldown_key] = now
                    drop_pct = (1 - stats.equity / stats.balance) * 100
                    try:
                        if telegram_bot:
                            asyncio.create_task(
                                telegram_bot.notify_admin(
                                    f"[!] Equity Drop: `{stats.license_key}` "
                                    f"balance={stats.balance:.2f} equity={stats.equity:.2f} "
                                    f"({drop_pct:.1f}% drop)"
                                )
                            )
                    except Exception as e:
                        logger.debug("Unexpected error: %s", e)
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

        logger.info(
            "[AccountStats] %s | Balance: %.2f | Equity: %.2f | Positions: %s | Account: %s",
            stats.license_key,
            stats.balance,
            stats.equity,
            stats.open_positions,
            stats.account,
        )

        return {
            "status": "received",
            "license": stats.license_key,
            "received_at": stats_data["received_at"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing account stats: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/account-stats")
async def get_account_stats(
    license_key: str | None = Query(None, description="Filter by license key"),
    user: dict = Depends(get_current_user),
):
    """Admin endpoint: Get latest account stats for all or specific licenses."""
    try:
        if license_key:
            snapshot = account_stats_latest.get(license_key)
            if not snapshot:
                # Try database
                trade_db_manager = get_trade_db_manager()
                if trade_db_manager:
                    rows = trade_db_manager.get_latest_account_stats(license_key)
                    if rows:
                        return {"license_key": license_key, "snapshot": rows[0]}
                raise HTTPException(status_code=404, detail="No stats found for license")
            return {"license_key": license_key, "snapshot": snapshot}

        # Return all latest snapshots (in-memory first, supplement from DB)
        result = dict(account_stats_latest)
        if not result:
            trade_db_manager = get_trade_db_manager()
            if trade_db_manager:
                rows = trade_db_manager.get_latest_account_stats()
                for row in rows:
                    lk = row.get("license_key", "")
                    if lk not in result:
                        row["received_at"] = row.get("timestamp", "")
                        result[lk] = row

        return {
            "total_licenses": len(result),
            "stats": result,
            "last_updated": datetime.now().isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching account stats: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch account stats")


@router.get("/admin/all")
async def get_all_licenses_trades(
    limit: int = Query(100, description="Number of trades to return"),
    offset: int = Query(0, description="Offset for pagination"),
    user: dict = Depends(get_current_user),
):
    """
    Admin endpoint: Get all trades from all licenses
    Access at /api/trades/admin/all
    Returns empty data structure when no trades exist
    """
    try:
        # Handle empty case
        if not trade_reports:
            logger.info("No trade reports available - returning empty structure")
            return {
                "total_reports": 0,
                "licenses_count": 0,
                "licenses_summary": {},
                "recent_trades": [],
                "pagination": {"limit": limit, "offset": offset, "has_more": False},
            }

        # Sort by timestamp (most recent first)
        sorted_trades = sorted(trade_reports, key=lambda x: x.get("received_at", ""), reverse=True)

        # Apply pagination
        paginated_trades = sorted_trades[offset : offset + limit]

        # Group by license for summary
        licenses_summary = {}
        for trade in trade_reports:
            lic = trade.get("license_key", "UNKNOWN")
            if lic not in licenses_summary:
                licenses_summary[lic] = {
                    "total": 0,
                    "successful": 0,
                    "failed": 0,
                    "volume": 0,
                    "symbols": set(),
                    "accounts": set(),
                }

            licenses_summary[lic]["total"] += 1
            if trade.get("success"):
                licenses_summary[lic]["successful"] += 1
            else:
                licenses_summary[lic]["failed"] += 1
            licenses_summary[lic]["volume"] += trade.get("volume", 0)
            licenses_summary[lic]["symbols"].add(trade.get("symbol", ""))
            licenses_summary[lic]["accounts"].add(trade.get("account", 0))

        # Convert sets to lists for JSON serialization
        for lic in licenses_summary:
            licenses_summary[lic]["symbols"] = list(licenses_summary[lic]["symbols"])
            licenses_summary[lic]["accounts"] = list(licenses_summary[lic]["accounts"])

        return {
            "total_reports": len(trade_reports),
            "licenses_count": len(licenses_summary),
            "licenses_summary": licenses_summary,
            "recent_trades": paginated_trades,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < len(trade_reports),
            },
        }

    except Exception as e:
        logger.error("Error fetching all trades: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch trades")


@router.get("/admin/license/{license_key}")
async def get_license_trades(
    license_key: str,
    limit: int = Query(50, description="Number of trades to return"),
    success_only: bool = Query(False, description="Filter successful trades only"),
    user: dict = Depends(get_current_user),
):
    """
    Admin endpoint: Get all trades for a specific license
    Access at /api/trades/admin/license/{license_key}
    """
    try:
        # Filter trades by license
        license_trades = [t for t in trade_reports if t.get("license_key") == license_key]

        # Apply success filter if requested
        if success_only:
            license_trades = [t for t in license_trades if t.get("success")]

        # Sort by timestamp (most recent first)
        license_trades.sort(key=lambda x: x.get("received_at", ""), reverse=True)

        # Calculate statistics in a single accumulating pass (was 6 passes).
        total_volume = 0
        successful = 0
        failed = 0
        total_profit = 0
        symbols_seen: set = set()
        accounts_seen: set = set()
        for t in license_trades:
            total_volume += t.get("volume", 0)
            if t.get("success"):
                successful += 1
            else:
                failed += 1
            total_profit += t.get("profit", 0)
            symbols_seen.add(t.get("symbol", ""))
            accounts_seen.add(t.get("account", 0))
        symbols = list(symbols_seen)
        accounts = list(accounts_seen)

        return {
            "license_key": license_key,
            "statistics": {
                "total_trades": len(license_trades),
                "successful_trades": successful,
                "failed_trades": failed,
                "success_rate": (successful / len(license_trades) * 100) if license_trades else 0,
                "total_volume": total_volume,
                "total_profit": total_profit,
                "avg_volume": total_volume / len(license_trades) if license_trades else 0,
                "active_symbols": symbols,
                "accounts": accounts,
            },
            "recent_trades": license_trades[:limit],
        }

    except Exception as e:
        logger.error("Error fetching license trades: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch license trades")


@router.get("/admin/stats")
async def get_all_licenses_stats(user: dict = Depends(get_current_user)):
    """
    Admin endpoint: Get statistics for all licenses
    Shows performance metrics for each license
    """
    try:
        return {
            "total_licenses": len(license_stats),
            "total_trades": len(trade_reports),
            "license_stats": license_stats,
            "last_updated": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("Error fetching stats: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch statistics")


@router.get("/admin/failures")
async def get_failed_trades(
    limit: int = Query(50, description="Number of failures to return"),
    user: dict = Depends(get_current_user),
):
    """
    Admin endpoint: Get all failed trade executions
    Useful for debugging why trades fail on certain licenses
    """
    try:
        # Filter failed trades
        failed_trades = [t for t in trade_reports if not t.get("success")]

        # Sort by timestamp (most recent first)
        failed_trades.sort(key=lambda x: x.get("received_at", ""), reverse=True)

        # Group failures by error message
        error_summary = {}
        for trade in failed_trades:
            error = trade.get("error_msg", "UNKNOWN_ERROR")
            if error not in error_summary:
                error_summary[error] = {"count": 0, "licenses": set(), "symbols": set()}
            error_summary[error]["count"] += 1
            error_summary[error]["licenses"].add(trade.get("license_key", ""))
            error_summary[error]["symbols"].add(trade.get("symbol", ""))

        # Convert sets to lists
        for error in error_summary:
            error_summary[error]["licenses"] = list(error_summary[error]["licenses"])
            error_summary[error]["symbols"] = list(error_summary[error]["symbols"])

        return {
            "total_failures": len(failed_trades),
            "error_summary": error_summary,
            "recent_failures": failed_trades[:limit],
        }

    except Exception as e:
        logger.error("Error fetching failures: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch failure data")


@router.get("/admin/dashboard")
async def get_admin_dashboard(user: dict = Depends(get_current_user)):
    """
    Admin dashboard endpoint with complete overview
    Access at /api/trades/admin/dashboard
    """
    try:
        # Calculate time-based statistics
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        today_trades = [
            t for t in trade_reports if t.get("received_at", "") >= today_start.isoformat()
        ]

        # Active licenses (traded today)
        active_licenses_today = set(t.get("license_key") for t in today_trades)

        # Most active symbols
        symbol_counts = {}
        for trade in trade_reports:
            symbol = trade.get("symbol", "UNKNOWN")
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "overview": {
                "total_licenses": len(license_stats),
                "active_licenses_today": len(active_licenses_today),
                "total_trades": len(trade_reports),
                "trades_today": len(today_trades),
                "success_rate": (
                    sum(1 for t in trade_reports if t.get("success")) / len(trade_reports) * 100
                    if trade_reports
                    else 0
                ),
            },
            "top_symbols": dict(top_symbols),
            "active_licenses": list(active_licenses_today),
            "recent_activity": today_trades[:20],
            "timestamp": now.isoformat(),
        }

    except Exception as e:
        logger.error("Error generating dashboard: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate dashboard")


# ============================================================================
# End of trade analytics routes
# ============================================================================

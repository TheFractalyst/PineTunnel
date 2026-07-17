"""Calculation logic for trade analytics stats and charts."""

from apps.server.db.analytics_store import license_stats
from apps.server.models.trade import TradeReport


def update_license_stats(report: TradeReport):
    """Update license statistics after each trade report"""
    if report.license_key not in license_stats:
        license_stats[report.license_key] = {
            "license_key": report.license_key,
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

    stats = license_stats[report.license_key]
    stats["total_trades"] += 1

    if report.success:
        stats["successful_trades"] += 1
    else:
        stats["failed_trades"] += 1

    stats["total_volume"] += report.volume
    stats["total_profit"] += report.profit
    stats["win_rate"] = (stats["successful_trades"] / stats["total_trades"]) * 100
    stats["avg_profit"] = stats["total_profit"] / stats["total_trades"]
    stats["last_trade"] = report.timestamp

    # Update unique accounts and symbols (membership-guarded append — bounded
    # small unique sets, avoids rebuilding list+set on every trade report).
    if report.account not in stats["account_numbers"]:
        stats["account_numbers"].append(report.account)
    if report.symbol not in stats["active_symbols"]:
        stats["active_symbols"].append(report.symbol)

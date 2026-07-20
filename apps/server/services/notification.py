"""Telegram notification event-type constants.

The reference repo ships a full ``NotificationEngine`` here (burst detection, quiet
hours, per-user dispatch). The public bot is admin-only and notifies admins directly
via ``PineTunnelTelegramBot.notify_admin``, so only the canonical event-type
constants are required here — they are consumed by the Settings mixin (notification
presets / custom toggles) and by the bot's ``_should_notify`` gating.
"""

# All notification event types — canonical source of truth
NOTIF_TYPES = [
    "exec_success",
    "exec_failed",
    "position_closed",
    "margin_warning",
    "equity_drawdown",
    "ea_lost",
    "ea_reconnected",
]

DEFAULT_PREFS = {t: True for t in NOTIF_TYPES}

# Human-readable labels for each type (kept in sync with NOTIF_TYPES)
NOTIF_LABELS = {
    "exec_success": "Trade Executed",
    "exec_failed": "Execution Failed",
    "position_closed": "Position Closed",
    "margin_warning": "Margin Warning",
    "equity_drawdown": "Equity Drawdown",
    "ea_lost": "EA Disconnected",
    "ea_reconnected": "EA Reconnected",
}

# Events that bypass quiet hours
CRITICAL_EVENTS = {"exec_failed", "margin_warning", "equity_drawdown"}

"""Shared module-level state for PineTunnel webhook modules.

Holds the dependency singletons (mt5_manager, client_manager, etc.) that are
wired by ``app_factory.set_dependencies`` and read by both the route handlers
in ``pinetunnel_webhook.py`` and the execution functions in ``executor.py``.
"""

from __future__ import annotations

mt5_manager = None
client_manager = None
risk_manager = None
rate_limiter = None
db_manager = None


def set_dependencies(mt5, client, risk, rate, db):
    """Set global dependencies from main app."""
    global mt5_manager, client_manager, risk_manager, rate_limiter, db_manager
    mt5_manager = mt5
    client_manager = client
    risk_manager = risk
    rate_limiter = rate
    db_manager = db

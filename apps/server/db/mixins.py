"""Backward-compat re-export.

TradeStatisticsMixin logic has been merged into DatabaseBase in base.py.
This module re-exports TradeStatisticsMixin for existing imports.
"""

from __future__ import annotations

from apps.server.db.base import DatabaseBase as TradeStatisticsMixin

__all__ = ["TradeStatisticsMixin"]

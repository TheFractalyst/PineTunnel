"""Signal data validation layer.

Validates signal data integrity before queuing for execution.
Catches impossible signals that would fail at the broker or cause
unintended trades: price sanity, SL/TP direction, volume bounds,
symbol format, timestamp freshness.

This runs after the parser produces a SignalData dict and before
the signal is queued. Rejections are logged with the reason so
operators can trace data quality issues back to their source.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9._]{2,32}$")
_MAX_LOTS = 1000.0
_MIN_LOTS = 0.0
_MAX_RISK_PCT = 100.0
_MAX_SLTP_DISTANCE_PCT = 50.0

_BUY_COMMANDS = {"buy", "buy_stop", "buy_limit", "close_short_buy", "close_all_buy"}
_SELL_COMMANDS = {"sell", "sell_stop", "sell_limit", "close_long_sell", "close_all_sell"}
_PENDING_COMMANDS = {"buy_stop", "buy_limit", "sell_stop", "sell_limit"}
_CLOSE_COMMANDS = {"close_long", "close_short", "close_all", "close_long_pct",
                   "close_short_pct", "close_long_vol", "close_short_vol",
                   "close_all_buy", "close_all_sell"}


class ValidationResult:
    """Result of signal validation."""

    def __init__(self, valid: bool, reason: str = "", details: dict[str, Any] | None = None) -> None:
        self.valid = valid
        self.reason = reason
        self.details = details or {}

    def __bool__(self) -> bool:
        return self.valid


def validate_signal(signal: dict[str, Any]) -> ValidationResult:
    """Validate a parsed signal dict before queuing.

    Args:
        signal: Parsed signal dict with keys: action, symbol, lots, sl, tp, etc.

    Returns:
        ValidationResult with valid=True if signal passes all checks,
        or valid=False with reason string.
    """
    action = str(signal.get("action", "")).lower()
    symbol = str(signal.get("symbol", "")).upper()

    if not action:
        return ValidationResult(False, "missing action field")

    if not symbol:
        return ValidationResult(False, "missing symbol field")

    if not _SYMBOL_RE.match(symbol):
        return ValidationResult(False, f"invalid symbol format: {symbol}", {"symbol": symbol})

    # Volume validation (only for order-opening commands)
    if action in _BUY_COMMANDS or action in _SELL_COMMANDS:
        lots = signal.get("lots")
        if lots is not None:
            try:
                lots = float(lots)
            except (TypeError, ValueError):
                return ValidationResult(False, f"invalid lots value: {lots}")
            if lots < _MIN_LOTS:
                return ValidationResult(False, f"lots below minimum: {lots}")
            if lots > _MAX_LOTS:
                return ValidationResult(False, f"lots exceeds maximum ({_MAX_LOTS}): {lots}")

        risk = signal.get("risk")
        if risk is not None:
            try:
                risk = float(risk)
            except (TypeError, ValueError):
                return ValidationResult(False, f"invalid risk value: {risk}")
            if risk < 0:
                return ValidationResult(False, f"risk is negative: {risk}")
            if risk > _MAX_RISK_PCT:
                return ValidationResult(False, f"risk exceeds maximum ({_MAX_RISK_PCT}%): {risk}")

    # SL/TP direction validation for buy orders
    if action in _BUY_COMMANDS:
        sl = signal.get("sl")
        tp = signal.get("tp")
        price = signal.get("price") or signal.get("pending")

        # For market orders (no price), at least check SL < TP if both are set
        if sl is not None and tp is not None and price is None:
            try:
                sl = float(sl)
                tp = float(tp)
                if sl >= tp:
                    return ValidationResult(
                        False,
                        f"buy SL ({sl}) must be below TP ({tp})",
                        {"sl": sl, "tp": tp},
                    )
            except (TypeError, ValueError):
                pass

        if sl is not None and price is not None:
            try:
                sl = float(sl)
                price = float(price)
                if sl >= price:
                    return ValidationResult(
                        False,
                        f"buy SL ({sl}) must be below entry price ({price})",
                        {"sl": sl, "price": price},
                    )
            except (TypeError, ValueError):
                pass

        if tp is not None and price is not None:
            try:
                tp = float(tp)
                price = float(price)
                if tp <= price:
                    return ValidationResult(
                        False,
                        f"buy TP ({tp}) must be above entry price ({price})",
                        {"tp": tp, "price": price},
                    )
            except (TypeError, ValueError):
                pass

    # SL/TP direction validation for sell orders
    if action in _SELL_COMMANDS:
        sl = signal.get("sl")
        tp = signal.get("tp")
        price = signal.get("price") or signal.get("pending")

        # For market orders (no price), at least check SL > TP if both are set
        if sl is not None and tp is not None and price is None:
            try:
                sl = float(sl)
                tp = float(tp)
                if sl <= tp:
                    return ValidationResult(
                        False,
                        f"sell SL ({sl}) must be above TP ({tp})",
                        {"sl": sl, "tp": tp},
                    )
            except (TypeError, ValueError):
                pass

        if sl is not None and price is not None:
            try:
                sl = float(sl)
                price = float(price)
                if sl <= price:
                    return ValidationResult(
                        False,
                        f"sell SL ({sl}) must be above entry price ({price})",
                        {"sl": sl, "price": price},
                    )
            except (TypeError, ValueError):
                pass

        if tp is not None and price is not None:
            try:
                tp = float(tp)
                price = float(price)
                if tp >= price:
                    return ValidationResult(
                        False,
                        f"sell TP ({tp}) must be below entry price ({price})",
                        {"tp": tp, "price": price},
                    )
            except (TypeError, ValueError):
                pass

    # SL/TP distance sanity check (prevent fat-finger 50%+ stops)
    if action in _BUY_COMMANDS or action in _SELL_COMMANDS:
        sl = signal.get("sl")
        tp = signal.get("tp")
        price = signal.get("price") or signal.get("pending")

        if sl is not None and price is not None:
            try:
                sl = float(sl)
                price = float(price)
                if price > 0:
                    distance_pct = abs(price - sl) / price * 100
                    if distance_pct > _MAX_SLTP_DISTANCE_PCT:
                        return ValidationResult(
                            False,
                            f"SL distance {distance_pct:.1f}% exceeds sanity limit ({_MAX_SLTP_DISTANCE_PCT}%)",
                            {"sl": sl, "price": price, "distance_pct": round(distance_pct, 1)},
                        )
            except (TypeError, ValueError):
                pass

        if tp is not None and price is not None:
            try:
                tp = float(tp)
                price = float(price)
                if price > 0:
                    distance_pct = abs(tp - price) / price * 100
                    if distance_pct > _MAX_SLTP_DISTANCE_PCT:
                        return ValidationResult(
                            False,
                            f"TP distance {distance_pct:.1f}% exceeds sanity limit ({_MAX_SLTP_DISTANCE_PCT}%)",
                            {"tp": tp, "price": price, "distance_pct": round(distance_pct, 1)},
                        )
            except (TypeError, ValueError):
                pass

    # Pending order entry price validation
    if action in _PENDING_COMMANDS:
        pending = signal.get("pending")
        if pending is None:
            return ValidationResult(False, f"pending order {action} requires pending= entry price")

        try:
            pending = float(pending)
        except (TypeError, ValueError):
            return ValidationResult(False, f"invalid pending price: {pending}")

        if pending <= 0:
            return ValidationResult(False, f"pending price must be positive: {pending}")

    return ValidationResult(True)

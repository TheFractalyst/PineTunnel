"""PineTunnel Signal Parser

Supports all PineTunnel commands, parameters, and aliases.

Author: PineTunnel
Version: 1.0.0
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

# Maximum comma-separated parts in a webhook message
_MAX_MESSAGE_PARTS = 50

# License ID format: alphanumeric, dashes, underscores, 4-64 chars
_LICENSE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# Commands whose "symbol" field must stay lowercase (EA checks exact match)
_SPECIAL_SYMBOLS = frozenset({"ea_on", "ea_off", "close_all_off"})

# Legacy param -> type_key mapping (avoids dict construction in hot loop)
_LEGACY_TYPE_KEY_MAP = {"risk": "vol_type", "sl": "sl_type", "tp": "tp_type"}

logger = logging.getLogger(__name__)


class CommandType(Enum):
    BUY = "buy"
    SELL = "sell"

    # Pending orders
    BUY_STOP = "buy_stop"
    BUY_LIMIT = "buy_limit"
    SELL_STOP = "sell_stop"
    SELL_LIMIT = "sell_limit"

    # Position closing
    CLOSE_ALL = "close_all"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"

    # Partial closing
    CLOSE_LONG_PCT = "close_long_pct"
    CLOSE_SHORT_PCT = "close_short_pct"
    CLOSE_LONG_VOL = "close_long_vol"
    CLOSE_SHORT_VOL = "close_short_vol"

    # Order cancellation
    CANCEL_LONG = "cancel_long"
    CANCEL_SHORT = "cancel_short"

    # Position modification
    SLTP_LONG = "sltp_long"
    SLTP_SHORT = "sltp_short"

    # Pending order modification
    SLTP_BUY_STOP = "sltp_buy_stop"
    SLTP_BUY_LIMIT = "sltp_buy_limit"
    SLTP_SELL_STOP = "sltp_sell_stop"
    SLTP_SELL_LIMIT = "sltp_sell_limit"

    # Combined actions
    CLOSE_LONG_BUY = "close_long_buy"
    CLOSE_LONG_SELL = "close_long_sell"
    CLOSE_SHORT_BUY = "close_short_buy"
    CLOSE_SHORT_SELL = "close_short_sell"
    CLOSE_ALL_BUY = "close_all_buy"
    CLOSE_ALL_SELL = "close_all_sell"

    # Cancel and open pending
    CANCEL_LONG_BUY_STOP = "cancel_long_buy_stop"
    CANCEL_LONG_BUY_LIMIT = "cancel_long_buy_limit"
    CANCEL_SHORT_SELL_STOP = "cancel_short_sell_stop"
    CANCEL_SHORT_SELL_LIMIT = "cancel_short_sell_limit"

    # EA management
    EA_OFF = "ea_off"
    EA_ON = "ea_on"
    CLOSE_ALL_OFF = "close_all_off"


# Command categories used in validation (defined after CommandType)
_MARKET_ORDER_COMMANDS: tuple[CommandType, ...] = (
    CommandType.BUY,
    CommandType.SELL,
    CommandType.CLOSE_LONG_BUY,
    CommandType.CLOSE_LONG_SELL,
    CommandType.CLOSE_SHORT_BUY,
    CommandType.CLOSE_SHORT_SELL,
    CommandType.CLOSE_ALL_BUY,
    CommandType.CLOSE_ALL_SELL,
)

_PENDING_ORDER_COMMANDS: tuple[CommandType, ...] = (
    CommandType.BUY_STOP,
    CommandType.BUY_LIMIT,
    CommandType.SELL_STOP,
    CommandType.SELL_LIMIT,
)

_CANCEL_REPLACE_COMMANDS: tuple[CommandType, ...] = (
    CommandType.CANCEL_LONG_BUY_STOP,
    CommandType.CANCEL_LONG_BUY_LIMIT,
    CommandType.CANCEL_SHORT_SELL_STOP,
    CommandType.CANCEL_SHORT_SELL_LIMIT,
)

# Upper bound on fixed-lot volume at the parse layer. Defense-in-depth backstop
# for the EA execution path (which otherwise caps only at broker SYMBOL_VOLUME_MAX
# — frequently thousands of lots for stocks/CFDs). Matches the server's direct-lots
# cap (pinetunnel_webhook.py) so the parser rejects what the server-direct path
# already rejects, and extends that same gate to the EA path. The MSFT incident
# (risk=1 on a $387 stock → 1328 lots / $500K+) slipped through because no layer
# enforced a sane magnitude; this is the chokepoint that sees every param.
_MAX_LOTS = 100.0


@dataclass(slots=True)
class PineTunnelSignal:
    """Parsed PineTunnel signal."""

    # Core components (required)
    license_id: str
    command: CommandType
    symbol: str

    # Common parameters (legacy)
    risk: float | None = None
    sl: float | None = None
    tp: float | None = None
    comment: str | None = None

    # Explicit volume parameters (new)
    lots: float | None = None
    usd: float | None = None
    risk_bal_pct: float | None = None
    risk_eq_pct: float | None = None
    margin_bal_pct: float | None = None
    margin_eq_pct: float | None = None

    # Explicit SL parameters (new)
    sl_points: float | None = None
    sl_price: float | None = None
    sl_pct: float | None = None

    # Explicit TP parameters (new)
    tp_points: float | None = None
    tp_price: float | None = None
    tp_pct: float | None = None

    # Type indicators for EA (new)
    vol_type: str | None = None  # 'lots', 'dollar', 'bal_loss', 'eq_loss', 'bal_margin'
    sl_type: str | None = None  # 'pips', 'price', 'pct'
    tp_type: str | None = None  # 'pips', 'price', 'pct'

    # Explicit entry type indicator for EA (Jan 2026 update)
    entry_type: str | None = None  # 'price', 'pips', 'pct'

    # Pending order parameters
    pending: float | None = None

    # Explicit entry parameters (Jan 2026 PineTunnel update)
    entry_price: float | None = None
    entry_points: float | None = None
    entry_pct: float | None = None

    # Breakeven parameters
    be_trigger: float | None = None
    be_offset: float | None = None

    # Pip trailing parameters
    trail_trigger: float | None = None
    trail_distance: float | None = None
    trail_step: float | None = None

    # ATR trailing parameters
    atr_timeframe: str | None = None
    atr_period: int | None = None
    atr_multiplier: float | None = None
    atr_shift: int | None = None
    atr_trigger: float | None = None

    # Entry filters
    spread: float | None = None
    acc_filter: float | None = None

    # Authentication
    secret: str | None = None

    # Near-Market flag: enables limit order conversion for price improvement
    # Sent as nm=true in PineTunnel syntax
    nm: bool = False

    # Raw message for logging
    raw_message: str = ""

    def to_dict(self) -> dict:
        """Convert signal to dictionary for JSON serialization.

        Outputs ONLY keys consumed by the MT4/MT5 EA. See class docstring
        for the key-mapping spec.
        """
        result: dict = {
            "license_id": self.license_id,
            "command": self.command.value,
            "action": self.command.value,
            "symbol": self.symbol,
        }

        # Volume — special: vol_type="lots" emits both "lots" and "risk"
        vt = self.vol_type
        if vt is not None:
            if vt == "lots":
                val = self.lots
            elif vt == "dollar":
                val = self.usd
            elif vt == "bal_loss":
                val = self.risk_bal_pct
            elif vt == "eq_loss":
                val = self.risk_eq_pct
            elif vt == "bal_margin":
                val = self.margin_bal_pct
            else:
                val = self.margin_eq_pct
            if val is not None:
                result["risk"] = val
                if vt == "lots":
                    result["lots"] = val
            elif self.risk is not None:
                result["risk"] = self.risk
            result["vol_type"] = vt
        elif self.risk is not None:
            result["risk"] = self.risk
            if self.risk > 0:
                result["lots"] = self.risk

        # SL — typed-emit pattern (unrolled: eliminates dict construction and
        # getattr on every signal)
        t = self.sl_type
        if t is not None:
            if t == "pips":
                v = self.sl_points
            elif t == "price":
                v = self.sl_price
            else:
                v = self.sl_pct
            if v is not None:
                result["sl"] = v
            elif self.sl is not None:
                result["sl"] = self.sl
            result["sl_type"] = t
        elif self.sl is not None and "sl" not in result:
            result["sl"] = self.sl

        # TP
        t = self.tp_type
        if t is not None:
            if t == "pips":
                v = self.tp_points
            elif t == "price":
                v = self.tp_price
            else:
                v = self.tp_pct
            if v is not None:
                result["tp"] = v
            elif self.tp is not None:
                result["tp"] = self.tp
            result["tp_type"] = t
        elif self.tp is not None and "tp" not in result:
            result["tp"] = self.tp

        # Entry (emits as "pending")
        t = self.entry_type
        if t is not None:
            if t == "price":
                v = self.entry_price
            elif t == "pips":
                v = self.entry_points
            else:
                v = self.entry_pct
            if v is not None:
                result["pending"] = v
            elif self.pending is not None:
                result["pending"] = self.pending
            result["entry_type"] = t
        elif self.pending is not None and "pending" not in result:
            result["pending"] = self.pending

        # pending — emitted before entry block above (entry overwrites if set)
        if self.pending is not None and "pending" not in result:
            result["pending"] = self.pending

        # Optional scalar params — emit if set (unrolled: direct attr access
        # eliminates getattr overhead and tuple construction on every signal)
        v = self.comment
        if v is not None:
            result["comment"] = v
        v = self.be_trigger
        if v is not None:
            result["be_trigger"] = v
        v = self.be_offset
        if v is not None:
            result["be_offset"] = v
        v = self.trail_trigger
        if v is not None:
            result["trail_trigger"] = v
        v = self.trail_distance
        if v is not None:
            result["trail_distance"] = v
        v = self.trail_step
        if v is not None:
            result["trail_step"] = v
        v = self.atr_timeframe
        if v is not None:
            result["atr_timeframe"] = v
        v = self.atr_period
        if v is not None:
            result["atr_period"] = v
        v = self.atr_multiplier
        if v is not None:
            result["atr_multiplier"] = v
        v = self.atr_shift
        if v is not None:
            result["atr_shift"] = v
        v = self.atr_trigger
        if v is not None:
            result["atr_trigger"] = v
        v = self.spread
        if v is not None:
            result["spread"] = v
        v = self.acc_filter
        if v is not None:
            result["acc_filter"] = v
        v = self.secret
        if v is not None:
            result["secret"] = v

        if self.nm:
            result["nm"] = "true"

        if self.command in (CommandType.CLOSE_LONG_PCT, CommandType.CLOSE_SHORT_PCT):
            if self.risk is not None:
                result["pct"] = self.risk

        return result


class PineTunnelParser:
    """PineTunnel syntax parser.

    Format: LicenseID,Command,Symbol,Parameters
    Example: 123456789,buy,EURUSD,risk=0.01,sl=50,tp=100
    """

    # Command aliases (PineTunnel spec)
    COMMAND_ALIASES = {
        # Buy aliases
        "long": "buy",
        "bull": "buy",
        "bullish": "buy",
        # Sell aliases
        "short": "sell",
        "bear": "sell",
        "bearish": "sell",
        # Combined action short forms (PineTunnel spec)
    }

    # Explicit parameter type mappings (PineTunnel spec)
    # param_name -> (field_name, type_indicator)
    EXPLICIT_VOL_PARAMS = {
        "lots": ("lots", "lots"),
        "usd": ("usd", "dollar"),
        "risk_bal_pct": ("risk_bal_pct", "bal_loss"),
        "risk_eq_pct": ("risk_eq_pct", "eq_loss"),
        "margin_bal_pct": ("margin_bal_pct", "bal_margin"),
        "margin_eq_pct": ("margin_eq_pct", "eq_margin"),
    }
    EXPLICIT_SL_PARAMS = {
        "sl_points": ("sl_points", "pips"),
        "sl_price": ("sl_price", "price"),
        "sl_pct": ("sl_pct", "pct"),
    }
    EXPLICIT_TP_PARAMS = {
        "tp_points": ("tp_points", "pips"),
        "tp_price": ("tp_price", "price"),
        "tp_pct": ("tp_pct", "pct"),
    }
    EXPLICIT_ENTRY_PARAMS = {
        "entry_price": ("entry_price", "price"),
        "entry_points": ("entry_points", "pips"),
        "entry_pct": ("entry_pct", "pct"),
    }

    # Unified lookup: param_name -> (field_name, type_val, type_key, legacy_key, label)
    _ALL_EXPLICIT: dict[str, tuple[str, str, str, str, str]] = {
        "lots": ("lots", "lots", "vol_type", "risk", "volume"),
        "usd": ("usd", "dollar", "vol_type", "risk", "volume"),
        "risk_bal_pct": ("risk_bal_pct", "bal_loss", "vol_type", "risk", "volume"),
        "risk_eq_pct": ("risk_eq_pct", "eq_loss", "vol_type", "risk", "volume"),
        "margin_bal_pct": (
            "margin_bal_pct",
            "bal_margin",
            "vol_type",
            "risk",
            "volume",
        ),
        "margin_eq_pct": ("margin_eq_pct", "eq_margin", "vol_type", "risk", "volume"),
        "sl_points": ("sl_points", "pips", "sl_type", "sl", "SL"),
        "sl_price": ("sl_price", "price", "sl_type", "sl", "SL"),
        "sl_pct": ("sl_pct", "pct", "sl_type", "sl", "SL"),
        "tp_points": ("tp_points", "pips", "tp_type", "tp", "TP"),
        "tp_price": ("tp_price", "price", "tp_type", "tp", "TP"),
        "tp_pct": ("tp_pct", "pct", "tp_type", "tp", "TP"),
        "entry_price": ("entry_price", "price", "entry_type", "pending", "entry"),
        "entry_points": ("entry_points", "pips", "entry_type", "pending", "entry"),
        "entry_pct": ("entry_pct", "pct", "entry_type", "pending", "entry"),
    }

    # Numeric parameter keys that accept float values
    _FLOAT_PARAMS = frozenset(
        {
            "pending",
            "be_trigger",
            "be_offset",
            "trail_trigger",
            "trail_distance",
            "trail_step",
            "atr_multiplier",
            "atr_trigger",
            "spread",
            "acc_filter",
        }
    )

    # Numeric parameter keys that accept int values
    _INT_PARAMS = frozenset({"atr_period", "atr_shift"})

    # String parameter keys
    _STRING_PARAMS = frozenset({"comment", "secret", "atr_timeframe"})

    def parse(self, message: str) -> PineTunnelSignal | None:
        """Parse PineTunnel alert message.

        Args:
            message: Alert message from TradingView.

        Returns:
            PineTunnelSignal or None if invalid.
        """
        try:
            # Remove extra whitespace and normalize
            message = message.strip()

            # Split by commas (spaces around commas are acceptable)
            parts = [p.strip() for p in message.split(",", _MAX_MESSAGE_PARTS + 1)]

            num_parts = len(parts)
            if num_parts > _MAX_MESSAGE_PARTS:
                logger.error(
                    "Message has too many parts: %s (max %s)", num_parts, _MAX_MESSAGE_PARTS
                )
                return None

            if num_parts < 3:
                logger.error(
                    "Invalid format: need at least 3 parts (License,Command,Symbol), got %s",
                    num_parts,
                )
                return None

            # Extract core components
            license_id = parts[0]
            if not _LICENSE_ID_PATTERN.match(license_id):
                logger.error("Invalid license ID format: %r", license_id)
                return None
            command_str = parts[1].lower()
            # Normalize symbol: uppercase base, lowercase extension.
            # Exception: special commands (ea_on/ea_off/close_all_off) must stay
            # lowercase — the EA checks symbol == "ea_off" etc. at execution time.
            raw_symbol = parts[2]
            raw_lower = raw_symbol.lower()
            if raw_lower in _SPECIAL_SYMBOLS:
                symbol = raw_lower
            elif "." in raw_symbol:
                symbol_parts = raw_symbol.rsplit(".", 1)
                symbol = symbol_parts[0].upper() + "." + symbol_parts[1].lower()
            else:
                symbol = raw_symbol.upper()

            # Resolve command aliases
            if command_str in self.COMMAND_ALIASES:
                command_str = self.COMMAND_ALIASES[command_str]

            # Parse command
            try:
                command = CommandType(command_str)
            except ValueError:
                logger.error("Unknown command: %s", command_str)
                return None

            # Parse parameters (everything after symbol)
            params = self._parse_parameters(parts[3:] if num_parts > 3 else [])

            # _parse_parameters returns None on conflicting explicit params
            if params is None:
                return None

            # Create signal
            signal = PineTunnelSignal(
                license_id=license_id,
                command=command,
                symbol=symbol,
                raw_message=message,
                **params,
            )

            # Validate required parameters
            if not self._validate_signal(signal):
                return None

            logger.info(
                "PineTunnel signal: %s %s | Risk: %s | SL: %s | TP: %s",
                command.value.upper(),
                symbol,
                signal.risk,
                signal.sl,
                signal.tp,
            )

            return signal

        except (ValueError, AttributeError, IndexError) as e:
            logger.error(
                "Failed to parse PineTunnel message in parse: %s: %s",
                type(e).__name__,
                e,
                extra={"context": {"message": message, "operation": "parse"}},
            )
            return None

    def _parse_parameters(self, param_parts: list[str]) -> dict | None:
        """Parse parameter key=value pairs with explicit parameter support.

        Explicit parameters (vol_*, sl_*, tp_*) take priority over legacy parameters
        (risk=, sl=, tp=) regardless of order in the message.
        """
        params: dict = {}
        seen: dict[str, set] = {
            "vol_type": set(),
            "sl_type": set(),
            "tp_type": set(),
            "entry_type": set(),
        }

        # Cache class attributes in locals for faster lookup in the hot loop
        all_explicit = self._ALL_EXPLICIT
        float_params = self._FLOAT_PARAMS
        int_params = self._INT_PARAMS
        string_params = self._STRING_PARAMS

        for part in param_parts:
            eq_idx = part.find("=")
            if eq_idx < 0:
                continue

            key = part[:eq_idx].strip().lower()
            value = part[eq_idx + 1 :].strip()

            # Explicit typed parameters (vol_*, sl_*, tp_*, entry_*)
            explicit_match = all_explicit.get(key)
            if explicit_match is not None:
                field_name, type_val, type_key, legacy_key, label = explicit_match
                if seen[type_key]:
                    logger.error(
                        "Conflicting explicit %s parameters: %s and %s. "
                        "Only one %s parameter is allowed per signal.",
                        label,
                        seen[type_key],
                        key,
                        label,
                    )
                    return None
                try:
                    params[field_name] = float(value)
                    params[type_key] = type_val
                    seen[type_key].add(key)
                    if legacy_key in params:
                        logger.info("Explicit %s overrides legacy %s=", key, legacy_key)
                except ValueError:
                    logger.warning("Invalid numeric value for %s: %s", key, value)
                continue

            # Legacy risk/sl/tp — float-parse with override log if explicit type already set
            if key in ("risk", "sl", "tp"):
                type_key = _LEGACY_TYPE_KEY_MAP[key]
                if type_key in params:
                    logger.info("Explicit %s overrides legacy %s=", params[type_key], key)
                try:
                    params[key] = float(value)
                except ValueError:
                    logger.warning("Invalid numeric value for %s: %s", key, value)
            elif key in float_params:
                try:
                    params[key] = float(value)
                except ValueError:
                    logger.warning("Invalid numeric value for %s: %s", key, value)
            elif key in int_params:
                try:
                    params[key] = int(value)
                except ValueError:
                    logger.warning("Invalid integer value for %s: %s", key, value)
            elif key in string_params:
                if key == "comment" and len(value) > 20:
                    logger.warning(
                        "Comment too long (%s chars) - setting to blank (max 20)", len(value)
                    )
                    params[key] = ""
                else:
                    params[key] = value
            elif key == "nm":
                params["nm"] = value.lower() in ("true", "1", "yes")
            else:
                logger.debug("Ignoring unknown parameter: %s=%s", key, value)

        return params

    def _validate_signal(self, signal: PineTunnelSignal) -> bool:
        """Validate signal has required parameters for its command.

        Includes validation for explicit parameters:
        - usd, risk_bal_pct, risk_eq_pct require SL
        - No negative values for explicit parameters
        - Warn on sl_pct or tp_pct > 100
        - Explicit params now supported for pending orders (PineTunnel v3.51.1+)

        Requirements: 1.6, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 7.2
        """
        lots = signal.lots
        usd = signal.usd
        risk_bal_pct = signal.risk_bal_pct
        risk_eq_pct = signal.risk_eq_pct
        margin_bal_pct = signal.margin_bal_pct
        margin_eq_pct = signal.margin_eq_pct
        risk = signal.risk
        sl = signal.sl
        tp = signal.tp
        sl_points = signal.sl_points
        sl_price = signal.sl_price
        sl_pct = signal.sl_pct
        tp_points = signal.tp_points
        tp_price = signal.tp_price
        tp_pct = signal.tp_pct
        entry_price = signal.entry_price
        entry_points = signal.entry_points
        entry_pct = signal.entry_pct

        has_volume = (
            risk is not None
            or lots is not None
            or usd is not None
            or risk_bal_pct is not None
            or risk_eq_pct is not None
            or margin_bal_pct is not None
            or margin_eq_pct is not None
        )

        has_sl = sl is not None or sl_points is not None or sl_price is not None or sl_pct is not None

        # Requirement 8.5: Validate no negative values for explicit parameters
        for _name, _val in (
            ("lots", lots),
            ("usd", usd),
            ("risk_bal_pct", risk_bal_pct),
            ("risk_eq_pct", risk_eq_pct),
            ("margin_bal_pct", margin_bal_pct),
            ("margin_eq_pct", margin_eq_pct),
            ("sl_points", sl_points),
            ("sl_price", sl_price),
            ("sl_pct", sl_pct),
            ("tp_points", tp_points),
            ("tp_price", tp_price),
            ("tp_pct", tp_pct),
            ("entry_price", entry_price),
            ("entry_points", entry_points),
            ("entry_pct", entry_pct),
        ):
            if _val is not None and _val < 0:
                logger.error("%s cannot be negative: %s", _name, _val)
                return False

        # Magnitude validation — the parser is the one chokepoint that sees every
        # param before the fork into the two execution paths (server-direct and EA).
        # Reject meaningless magnitudes that would otherwise produce a silent
        # EA-default lot (lots=0), a zero-lot order (risk=0), an oversized
        # position (lots > cap), or an impossible percent (volume_pct_* outside
        # (0, 100]). Legacy absolute sl/tp cannot be negative.
        if lots is not None and lots <= 0:
            logger.error("lots must be > 0: %s", lots)
            return False
        if lots is not None and lots > _MAX_LOTS:
            logger.error("lots %s exceeds maximum %.1f", lots, _MAX_LOTS)
            return False
        if risk is not None and risk <= 0:
            logger.error("risk must be > 0: %s", risk)
            return False
        for _pct_name, _pct_val in (
            ("risk_bal_pct", risk_bal_pct),
            ("risk_eq_pct", risk_eq_pct),
            ("margin_bal_pct", margin_bal_pct),
            ("margin_eq_pct", margin_eq_pct),
        ):
            if _pct_val is not None and not (0 < _pct_val <= 100):
                logger.error("%s must be in (0, 100]: %s", _pct_name, _pct_val)
                return False
        if sl is not None and sl < 0:
            logger.error("sl cannot be negative: %s", sl)
            return False
        if tp is not None and tp < 0:
            logger.error("tp cannot be negative: %s", tp)
            return False

        # Requirements 1.6, 8.2, 8.3, 8.4: usd, risk_bal_pct,
        # risk_eq_pct require SL
        if usd is not None and not has_sl:
            logger.error(
                "usd requires stop-loss parameter (sl_points, sl_price, sl_pct, or sl=)"
            )
            return False

        if risk_bal_pct is not None and not has_sl:
            logger.error("risk_bal_pct requires stop-loss parameter")
            return False

        if risk_eq_pct is not None and not has_sl:
            logger.error("risk_eq_pct requires stop-loss parameter")
            return False

        # Requirement 8.6: Warn on sl_pct or tp_pct > 100
        if sl_pct is not None and sl_pct > 100:
            logger.warning("sl_pct=%s exceeds 100%% - processing anyway", sl_pct)

        if tp_pct is not None and tp_pct > 100:
            logger.warning("tp_pct=%s exceeds 100%% - processing anyway", tp_pct)

        # Market orders require volume (risk or explicit vol_*)
        # This includes combined close-and-open commands (Requirements 7.3, 4.4)
        if signal.command in _MARKET_ORDER_COMMANDS:
            if not has_volume:
                logger.error(
                    "%s requires risk= or explicit volume parameter (lots, usd, etc.)",
                    signal.command.value,
                )
                return False

        # Pending orders require volume and entry (pending= or explicit entry_* params)
        has_entry = (
            signal.pending is not None
            or entry_price is not None
            or entry_points is not None
            or entry_pct is not None
        )
        if signal.command in _PENDING_ORDER_COMMANDS:
            if not has_volume:
                logger.error("%s requires risk= or explicit volume parameter", signal.command.value)
                return False
            if not has_entry:
                logger.error(
                    "%s requires pending= or entry parameter (entry_price=, entry_points=, entry_pct=)",
                    signal.command.value,
                )
                return False

        # Partial volume close requires risk
        if signal.command in (CommandType.CLOSE_LONG_VOL, CommandType.CLOSE_SHORT_VOL):
            if signal.risk is None:
                logger.error("%s requires risk= parameter", signal.command.value)
                return False

        # Breakeven requires both be_trigger and be_offset
        if signal.be_trigger is not None or signal.be_offset is not None:
            if signal.be_trigger is None or signal.be_offset is None:
                logger.error("Breakeven requires both be_trigger= and be_offset=")
                return False
            if signal.be_offset >= signal.be_trigger:
                logger.error("be_offset= must be less than be_trigger=")
                return False

        # Trailing requires all three parameters (trail_trigger=0 and negative values
        # are valid per PineTunnel spec: immediate trailing activation)
        _tt = signal.trail_trigger
        _td = signal.trail_distance
        _ts = signal.trail_step
        if _tt is not None or _td is not None or _ts is not None:
            if not (_tt is not None and _td is not None and _ts is not None):
                logger.error("Trailing requires trail_trigger=, trail_distance=, and trail_step=")
                return False

        # ATR trailing requires atr_timeframe= and atr_period= (PineTunnel spec)
        has_atr = (
            signal.atr_timeframe is not None
            or signal.atr_period is not None
            or signal.atr_multiplier is not None
            or signal.atr_shift is not None
            or signal.atr_trigger is not None
        )
        if has_atr:
            if signal.atr_timeframe is None or signal.atr_period is None:
                logger.error("ATR trailing requires both atr_timeframe= and atr_period= parameters")
                return False

        # Cancel+replace commands require entry + volume (PineTunnel spec)
        if signal.command in _CANCEL_REPLACE_COMMANDS:
            if not has_volume:
                logger.error("%s requires risk= or explicit volume parameter", signal.command.value)
                return False
            if not has_entry:
                logger.error(
                    "%s requires pending= or entry parameter (entry_price=, entry_points=, entry_pct=)",
                    signal.command.value,
                )
                return False

        return True

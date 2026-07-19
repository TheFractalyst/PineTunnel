"""
Risk Management Module for MT5 Webhook Server.

Handles position sizing, daily limits, and risk calculations.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Named constants for magic numbers
DEFAULT_RISK_PER_TRADE_PCT = 2.0
DEFAULT_MAX_DAILY_LOSS_PCT = 5.0
DEFAULT_MAX_DAILY_TRADES = 20
DEFAULT_MAX_CONCURRENT_POSITIONS = 5
DEFAULT_MAX_POSITION_SIZE_LOTS = 10.0

DEFAULT_STOP_LOSS_POINTS = 200
MIN_MARGIN_LEVEL_PCT = 200
MIN_FREE_MARGIN_RATIO = 0.2
MAX_DRAWDOWN_PCT = 10.0
MAX_SESSION_DRAWDOWN_PCT = 15.0


class RiskManager:
    """Advanced risk management system."""

    def __init__(self, config: dict | None = None) -> None:
        """Initialize risk manager with configuration.

        Args:
            config: Configuration dictionary with risk parameters.
        """
        config = config or {}
        self.max_risk_per_trade = config.get("max_risk_per_trade", DEFAULT_RISK_PER_TRADE_PCT)
        self.max_daily_loss = config.get("max_daily_loss", DEFAULT_MAX_DAILY_LOSS_PCT)
        self.max_daily_trades = config.get("max_daily_trades", DEFAULT_MAX_DAILY_TRADES)
        self.max_concurrent_positions = config.get(
            "max_concurrent_positions", DEFAULT_MAX_CONCURRENT_POSITIONS
        )
        self.max_position_size = config.get("max_position_size", DEFAULT_MAX_POSITION_SIZE_LOTS)

        # Daily tracking
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.last_reset: datetime = datetime.now().date()  # type: ignore[assignment]

        # Session tracking
        self.session_high_balance: float = 0.0

        logger.info(
            "Risk Manager initialized - Max risk: %s%%, Max daily loss: %s%%",
            self.max_risk_per_trade,
            self.max_daily_loss,
        )

    def reset_daily_stats(self) -> None:
        """Reset daily statistics when the calendar day changes."""
        current_date = datetime.now().date()
        if current_date != self.last_reset:
            logger.info(
                "Resetting daily stats. Previous: P&L=$%.2f, Trades=%d",
                self.daily_pnl,
                self.daily_trades,
            )
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset = current_date

    def can_trade(self, account_info: dict, position_count: int = 0) -> tuple[bool, str]:
        """Check if trading is allowed based on risk rules.

        Args:
            account_info: Current account information.
            position_count: Current number of open positions.

        Returns:
            Tuple of (allowed, reason).
        """
        self.reset_daily_stats()

        # Check if trading is enabled on account
        if not account_info.get("trade_allowed", False):
            return False, "Trading not allowed on account"

        # Check daily trade limit
        if self.daily_trades >= self.max_daily_trades:
            return False, f"Daily trade limit reached ({self.max_daily_trades})"

        # Check concurrent positions
        if position_count >= self.max_concurrent_positions:
            return False, f"Max concurrent positions reached ({self.max_concurrent_positions})"

        # Check daily loss limit
        balance = account_info.get("balance", 0)
        if balance > 0:
            daily_loss_pct = (self.daily_pnl / balance) * 100
            if daily_loss_pct <= -self.max_daily_loss:
                return False, f"Daily loss limit reached: {daily_loss_pct:.2f}%"

        # Check margin level
        margin_level = account_info.get("margin_level", 0)
        if margin_level > 0 and margin_level < MIN_MARGIN_LEVEL_PCT:
            return (
                False,
                f"Margin level too low: {margin_level:.2f}% (min: {MIN_MARGIN_LEVEL_PCT}%)",
            )

        # Check free margin
        free_margin = account_info.get("margin_free", 0)
        if free_margin < balance * MIN_FREE_MARGIN_RATIO:
            return False, f"Insufficient free margin: ${free_margin:.2f}"

        # Check drawdown
        equity = account_info.get("equity", balance)
        if balance > 0:
            current_drawdown = ((balance - equity) / balance) * 100
            if current_drawdown > MAX_DRAWDOWN_PCT:
                return False, f"Account in significant drawdown: {current_drawdown:.2f}%"

        # Update session high
        if balance > self.session_high_balance:
            self.session_high_balance = balance

        # Check max drawdown from session high
        if self.session_high_balance > 0:
            session_dd = ((self.session_high_balance - equity) / self.session_high_balance) * 100
            if session_dd > MAX_SESSION_DRAWDOWN_PCT:
                return False, f"Max session drawdown exceeded: {session_dd:.2f}%"

        return True, "Risk checks passed"

    def calculate_position_size(
        self,
        account_info: dict,
        symbol_info: dict,
        stop_loss_points: float | None = None,
        risk_percent: float | None = None,
    ) -> tuple[float, dict]:
        """Calculate optimal position size based on risk parameters.

        Args:
            account_info: Account information.
            symbol_info: Symbol specifications.
            stop_loss_points: Stop loss distance in points.
            risk_percent: Risk percentage override.

        Returns:
            Tuple of (lot_size, calculation_details).
        """
        # Get account balance
        balance = account_info.get("balance", 0)

        if balance <= 0:
            return symbol_info.get("volume_min", 0.01), {"error": "Invalid account balance"}

        # Use provided risk or default
        risk_pct = risk_percent if risk_percent else self.max_risk_per_trade

        # Calculate risk amount in account currency
        risk_amount = balance * (risk_pct / 100)

        # Get symbol parameters
        tick_value = symbol_info.get("trade_tick_value", 1)
        tick_size = symbol_info.get("trade_tick_size", 0.00001)
        point = symbol_info.get("point", 0.00001)
        volume_min = symbol_info.get("volume_min", 0.01)
        volume_max = min(symbol_info.get("volume_max", 100), self.max_position_size)
        volume_step = symbol_info.get("volume_step", 0.01)

        # If no stop loss provided, use default
        if not stop_loss_points:
            stop_loss_points = DEFAULT_STOP_LOSS_POINTS

        # Calculate value per point per lot
        point_value = tick_value * (point / tick_size) if tick_size > 0 else tick_value

        # Calculate lot size
        if stop_loss_points > 0 and point_value > 0:
            lot_size = risk_amount / (stop_loss_points * point_value)
        else:
            lot_size = volume_min

        # Round to volume step
        if volume_step > 0:
            lot_size = round(lot_size / volume_step) * volume_step

        # Apply constraints
        lot_size = max(volume_min, min(lot_size, volume_max))

        # Calculate actual risk
        actual_risk = lot_size * stop_loss_points * point_value
        actual_risk_pct = (actual_risk / balance) * 100

        calculation_details = {
            "risk_amount": risk_amount,
            "risk_percent": risk_pct,
            "stop_loss_points": stop_loss_points,
            "point_value": point_value,
            "calculated_lots": lot_size,
            "actual_risk": actual_risk,
            "actual_risk_percent": actual_risk_pct,
            "volume_constraints": {
                "min": volume_min,
                "max": volume_max,
                "step": volume_step,
            },
        }

        logger.info(
            "Position size calculated: %s lots, Risk: $%.2f (%.2f%%)",
            lot_size,
            actual_risk,
            actual_risk_pct,
        )

        return lot_size, calculation_details

    def get_risk_status(self, account_info: dict) -> dict:
        """Get current risk management status.

        Args:
            account_info: Current account information.

        Returns:
            Dictionary with risk metrics.
        """
        self.reset_daily_stats()

        balance = account_info.get("balance", 0)
        equity = account_info.get("equity", balance)

        # Calculate metrics
        has_balance = balance > 0
        daily_pnl_pct = (self.daily_pnl / balance * 100) if has_balance else 0
        current_dd = ((balance - equity) / balance * 100) if has_balance else 0

        remaining_risk = max(0, self.max_daily_loss + daily_pnl_pct)
        remaining_trades = max(0, self.max_daily_trades - self.daily_trades)

        return {
            "daily_pnl": self.daily_pnl,
            "daily_pnl_percent": daily_pnl_pct,
            "daily_trades": self.daily_trades,
            "current_drawdown": current_dd,
            "max_drawdown": MAX_DRAWDOWN_PCT,
            "position_sizing_mode": "risk_based",
            "risk_per_trade_pct": self.max_risk_per_trade,
            "remaining_risk_percent": remaining_risk,
            "remaining_trades": remaining_trades,
            "max_daily_loss": self.max_daily_loss,
            "max_daily_trades": self.max_daily_trades,
            "last_reset": self.last_reset.isoformat(),
        }

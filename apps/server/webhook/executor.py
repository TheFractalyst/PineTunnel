"""PineTunnel command execution functions.

All MT5 trade execution logic extracted from pinetunnel_webhook.py:
- execute_pinetunnel_command (command dispatcher)
- _calculate_volume
- execute_market_order / execute_pending_order
- close_all_positions / close_positions_by_type / close_positions_partial
- cancel_pending_orders
- modify_position_sltp / modify_pending_order_sltp
- execute_combined_action / execute_cancel_and_pending
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks

from apps.server.webhook import _state as deps
from apps.server.webhook.parser import CommandType, PineTunnelSignal

# Optional MT5 import for cloud deployment
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

logger = logging.getLogger(__name__)

_PENDING_ORDER_TYPE_MAP = {
    CommandType.BUY_STOP: "buy_stop",
    CommandType.BUY_LIMIT: "buy_limit",
    CommandType.SELL_STOP: "sell_stop",
    CommandType.SELL_LIMIT: "sell_limit",
}

_CANCEL_ALLOWED_TYPES = {
    "buy": ("buy_stop", "buy_limit"),
    "sell": ("sell_stop", "sell_limit"),
}


async def execute_pinetunnel_command(
    signal: PineTunnelSignal,
    license_key: str,
    account_info: dict,
    background_tasks: BackgroundTasks,
) -> dict:
    """Execute PineTunnel command based on command type.

    Handles all 40+ PineTunnel command types.
    """
    command = signal.command
    symbol = signal.symbol

    # Market orders (buy, sell and aliases)
    if command in (CommandType.BUY, CommandType.SELL):
        return await execute_market_order(signal, license_key, account_info)

    # Pending orders (buy_stop, buy_limit, sell_stop, sell_limit)
    if command in (
        CommandType.BUY_STOP,
        CommandType.BUY_LIMIT,
        CommandType.SELL_STOP,
        CommandType.SELL_LIMIT,
    ):
        return await execute_pending_order(signal, license_key, account_info)

    # Close positions
    if command == CommandType.CLOSE_ALL:
        return close_all_positions(symbol, comment=signal.comment)

    if command == CommandType.CLOSE_LONG:
        return close_positions_by_type(symbol, "buy", comment=signal.comment)

    if command == CommandType.CLOSE_SHORT:
        return close_positions_by_type(symbol, "sell", comment=signal.comment)

    # Partial close
    if command == CommandType.CLOSE_LONG_PCT:
        return close_positions_partial(symbol, "buy", percent=signal.risk, comment=signal.comment)

    if command == CommandType.CLOSE_SHORT_PCT:
        return close_positions_partial(symbol, "sell", percent=signal.risk, comment=signal.comment)

    if command == CommandType.CLOSE_LONG_VOL:
        return close_positions_partial(symbol, "buy", volume=signal.risk, comment=signal.comment)

    if command == CommandType.CLOSE_SHORT_VOL:
        return close_positions_partial(symbol, "sell", volume=signal.risk, comment=signal.comment)

    # Cancel orders
    if command == CommandType.CANCEL_LONG:
        return cancel_pending_orders(symbol, "buy")

    if command == CommandType.CANCEL_SHORT:
        return cancel_pending_orders(symbol, "sell")

    # Modify SL/TP of positions
    if command in (CommandType.SLTP_LONG, CommandType.SLTP_SHORT):
        position_type = "buy" if "long" in command.value else "sell"
        return modify_position_sltp(symbol, position_type, signal.sl, signal.tp)

    # Modify SL/TP of pending orders
    if command in (
        CommandType.SLTP_BUY_STOP,
        CommandType.SLTP_BUY_LIMIT,
        CommandType.SLTP_SELL_STOP,
        CommandType.SLTP_SELL_LIMIT,
    ):
        order_type = command.value.replace("sltp", "")
        return modify_pending_order_sltp(symbol, order_type, signal.sl, signal.tp)

    # Combined actions
    if command in (
        CommandType.CLOSE_LONG_BUY,
        CommandType.CLOSE_LONG_SELL,
        CommandType.CLOSE_SHORT_BUY,
        CommandType.CLOSE_SHORT_SELL,
        CommandType.CLOSE_ALL_BUY,
        CommandType.CLOSE_ALL_SELL,
    ):
        return await execute_combined_action(signal, license_key, account_info)

    if command in (
        CommandType.CANCEL_LONG_BUY_STOP,
        CommandType.CANCEL_LONG_BUY_LIMIT,
        CommandType.CANCEL_SHORT_SELL_STOP,
        CommandType.CANCEL_SHORT_SELL_LIMIT,
    ):
        return await execute_cancel_and_pending(signal, license_key, account_info)

    # EA management - not implemented (EA auto-toggle requires server->EA control channel)
    if command == CommandType.EA_OFF:
        return {"success": False, "error": "EA_OFF not implemented - disable the EA in the terminal"}

    if command == CommandType.EA_ON:
        return {"success": False, "error": "EA_ON not implemented - enable the EA in the terminal"}

    if command == CommandType.CLOSE_ALL_OFF:
        result = close_all_positions(symbol, comment=signal.comment)
        result["ea_disabled"] = False
        result["message"] = (
            "Closed all positions. EA auto-disable not implemented - disable manually"
        )
        return result

    return {"success": False, "error": f"Command {command.value} not yet implemented"}


def _calculate_volume(
    signal: PineTunnelSignal, account_info: dict, entry_price: float
) -> tuple[float, dict | None]:
    """Calculate position volume based on risk management.

    Uses risk_manager for percentage-based sizing (risk <= 10); treats
    risk > 10 as direct lot size. Falls back to simplified calc when
    risk_manager is unavailable or SL distance is invalid.
    """
    volume = 0.01
    calculation_details = None

    if signal.sl and signal.risk and deps.risk_manager:
        try:
            symbol_info = mt5.symbol_info(signal.symbol)
            if not symbol_info:
                logger.warning("Could not get symbol info for %s", signal.symbol)
            else:
                point = symbol_info.point
                sl_distance_points = abs(entry_price - signal.sl) / point if point > 0 else 0

                if sl_distance_points > 0:
                    if signal.risk <= 10:
                        volume, calculation_details = deps.risk_manager.calculate_position_size(
                            account_info=account_info,
                            symbol_info=symbol_info._asdict(),
                            stop_loss_points=sl_distance_points,
                            risk_percent=signal.risk,
                        )
                        logger.info(
                            "Position sizing: %s | Risk: %s%% | SL Distance: %s points | "
                            "Lot Size: %s | Actual Risk: $%.2f (%.2f%%)",
                            signal.symbol,
                            signal.risk,
                            sl_distance_points,
                            volume,
                            calculation_details.get("actual_risk", 0),
                            calculation_details.get("actual_risk_percent", 0),
                        )
                    else:
                        volume = signal.risk
                else:
                    logger.warning(
                        "Invalid SL distance for %s, using simplified calculation",
                        signal.symbol,
                    )
        except Exception as e:
            logger.error("Error calculating position size: %s, using simplified calculation", e)

    if volume == 0.01 and signal.risk:
        volume = 0.01 * signal.risk if signal.risk <= 10 else signal.risk

    volume = max(0.01, min(volume, 100.0))
    return volume, calculation_details


async def execute_market_order(
    signal: PineTunnelSignal, license_key: str, account_info: dict
) -> dict:
    """Execute market buy/sell order with risk management."""
    order_type = "buy" if signal.command == CommandType.BUY else "sell"

    volume = 0.01
    calculation_details = None

    if signal.risk and signal.sl and deps.risk_manager:
        tick = mt5.symbol_info_tick(signal.symbol)
        if not tick:
            logger.warning("Could not get price for %s, using default volume", signal.symbol)
        else:
            entry_price = tick.ask if order_type == "buy" else tick.bid
            volume, calculation_details = _calculate_volume(signal, account_info, entry_price)
    elif signal.risk:
        volume = 0.01 * signal.risk if signal.risk <= 10 else signal.risk
        volume = max(0.01, min(volume, 100.0))

    if deps.mt5_manager:
        result = deps.mt5_manager.execute_order(
            symbol=signal.symbol,
            action=order_type,
            volume=volume,
            sl=signal.sl,
            tp=signal.tp,
            comment=signal.comment[:20] if signal.comment else "PineTunnel",
        )

        # Add calculation details to result if available
        if result and calculation_details:
            result["position_sizing"] = calculation_details

        return result if result else {"success": False, "error": "Order execution failed"}

    return {"success": False, "error": "MT5 manager not available"}


async def execute_pending_order(
    signal: PineTunnelSignal, license_key: str, account_info: dict
) -> dict:
    """Execute pending order (stop/limit)."""
    if not signal.pending:
        return {"success": False, "error": "Pending price required"}

    # Check for volume - can be explicit (vol_*) or legacy (risk)
    has_explicit_volume = (
        signal.lots is not None
        or signal.usd is not None
        or signal.risk_bal_pct is not None
        or signal.risk_eq_pct is not None
    )

    if not signal.risk and not has_explicit_volume:
        return {
            "success": False,
            "error": "Risk or explicit volume parameter required (lots, usd, risk_bal_pct, risk_eq_pct)",
        }

    order_type = _PENDING_ORDER_TYPE_MAP.get(signal.command)

    # Entry price is known for pending orders
    volume, calculation_details = _calculate_volume(signal, account_info, signal.pending)

    if deps.mt5_manager:
        result = deps.mt5_manager.place_pending_order(
            symbol=signal.symbol,
            order_type=order_type,
            volume=volume,
            price=signal.pending,
            sl=signal.sl,
            tp=signal.tp,
            comment=signal.comment[:20] if signal.comment else "PineTunnel",
        )

        # Add calculation details to result if available
        if result and calculation_details:
            result["position_sizing"] = calculation_details

        return result if result else {"success": False, "error": "Pending order failed"}

    return {"success": False, "error": "MT5 manager not available"}


def close_all_positions(symbol: str, comment: str | None = None) -> dict:
    """Close all positions for symbol, optionally filtered by comment."""
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    # If comment filter specified, use close_positions_by_type for both buy and sell
    if comment:
        result_long = close_positions_by_type(symbol, "buy", comment=comment)
        result_short = close_positions_by_type(symbol, "sell", comment=comment)
        total_closed = result_long.get("positions_closed", 0) + result_short.get(
            "positions_closed", 0
        )
        total_failed = result_long.get("positions_failed", 0) + result_short.get(
            "positions_failed", 0
        )
        return {
            "success": total_closed > 0,
            "positions_closed": total_closed,
            "positions_failed": total_failed,
            "comment_filter": comment,
        }

    result = deps.mt5_manager.close_positions(symbol=symbol)
    return result if result else {"success": False, "error": "No positions to close"}


def close_positions_by_type(symbol: str, position_type: str, comment: str | None = None) -> dict:
    """Close positions by type (buy/sell), optionally filtered by comment.

    PineTunnel Multi-Strategy Feature:
    Multi-strategy: comment-based position filtering.
    """
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    # Get positions filtered by type
    positions = deps.mt5_manager.get_positions(symbol=symbol, position_type=position_type)

    if not positions:
        return {
            "success": True,
            "message": f"No {position_type} positions",
            "positions_closed": 0,
        }

    # Filter by comment if provided (Multi-Strategy feature)
    if comment:
        positions = [pos for pos in positions if pos.get("comment", "") == comment]
        logger.info(
            "Multi-Strategy filter '%s': %s matching positions found", comment, len(positions)
        )
        if not positions:
            return {
                "success": True,
                "message": f"No {position_type} positions with comment '{comment}'",
                "positions_closed": 0,
                "comment_filter": comment,
            }

    closed_count = 0
    failed_count = 0

    for pos in positions:
        result = deps.mt5_manager.close_position_partial(pos["ticket"], pos["volume"])
        if result.get("success"):
            closed_count += 1
            logger.info(
                "Closed position %s (comment: %s)", pos["ticket"], pos.get("comment", "N/A")
            )
        else:
            failed_count += 1

    response = {
        "success": closed_count > 0,
        "positions_closed": closed_count,
        "positions_failed": failed_count,
        "position_type": position_type,
    }

    if comment:
        response["comment_filter"] = comment

    return response


def close_positions_partial(
    symbol: str,
    position_type: str,
    percent: float | None = None,
    volume: float | None = None,
    comment: str | None = None,
) -> dict:
    """Close positions partially - FIFO order, optionally filtered by comment."""
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    # Get positions filtered by type (FIFO - oldest first)
    positions = deps.mt5_manager.get_positions(symbol=symbol, position_type=position_type)

    if not positions:
        return {"success": True, "message": "No positions", "positions_closed": 0}

    # Filter by comment if provided
    if comment:
        positions = [pos for pos in positions if pos.get("comment", "") == comment]
        logger.info(
            "Comment filter '%s': %s matching positions for partial close", comment, len(positions)
        )
        if not positions:
            return {
                "success": True,
                "message": f"No {position_type} positions with comment '{comment}'",
                "positions_closed": 0,
                "comment_filter": comment,
            }

    # Sort by ticket (oldest first - FIFO)
    positions.sort(key=lambda x: x["ticket"])

    closed_count = 0
    total_closed_volume = 0

    if percent:
        # Close percentage of TOTAL volume
        total_volume = sum(pos["volume"] for pos in positions)
        volume_to_close = total_volume * (percent / 100)
    elif volume:
        volume_to_close = volume
    else:
        return {"success": False, "error": "Either percent or volume required"}

    remaining_to_close = volume_to_close

    for pos in positions:
        if remaining_to_close <= 0:
            break

        close_volume = min(pos["volume"], remaining_to_close)

        result = deps.mt5_manager.close_position_partial(pos["ticket"], close_volume)

        if result.get("success"):
            total_closed_volume += close_volume
            remaining_to_close -= close_volume
            if close_volume >= pos["volume"]:
                closed_count += 1

    response = {
        "success": total_closed_volume > 0,
        "positions_modified": closed_count,
        "close_type": "percentage" if percent else "volume",
        "close_value": percent or volume,
        "total_closed_volume": total_closed_volume,
    }

    if comment:
        response["comment_filter"] = comment

    return response


def cancel_pending_orders(symbol: str, order_type: str) -> dict:
    """Cancel pending orders by type."""
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    orders = deps.mt5_manager.get_orders(symbol=symbol)

    if not orders:
        return {"success": True, "message": "No pending orders", "orders_cancelled": 0}

    allowed_types = _CANCEL_ALLOWED_TYPES.get(order_type, ())

    cancelled_count = 0
    failed_count = 0

    for order in orders:
        if order["type"] in allowed_types:
            result = deps.mt5_manager.cancel_order(order["ticket"])
            if result.get("success"):
                cancelled_count += 1
            else:
                failed_count += 1

    return {
        "success": cancelled_count > 0,
        "orders_cancelled": cancelled_count,
        "orders_failed": failed_count,
    }


def modify_position_sltp(
    symbol: str, position_type: str, sl: float | None = None, tp: float | None = None
) -> dict:
    """Modify SL/TP of positions."""
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    # Get positions filtered by type
    positions = deps.mt5_manager.get_positions(symbol=symbol, position_type=position_type)

    if not positions:
        return {"success": True, "message": "No positions", "positions_modified": 0}

    modified_count = 0
    failed_count = 0

    for pos in positions:
        result = deps.mt5_manager.modify_position(ticket=pos["ticket"], sl=sl, tp=tp)

        if result.get("success"):
            modified_count += 1
        else:
            failed_count += 1

    return {
        "success": modified_count > 0,
        "positions_modified": modified_count,
        "positions_failed": failed_count,
        "new_sl": sl,
        "new_tp": tp,
    }


def modify_pending_order_sltp(
    symbol: str, order_type: str, sl: float | None = None, tp: float | None = None
) -> dict:
    """Modify SL/TP of pending orders."""
    if not deps.mt5_manager:
        return {"success": False, "error": "MT5 manager not available"}

    # Get pending orders
    orders = deps.mt5_manager.get_orders(symbol=symbol)

    if not orders:
        return {"success": True, "message": "No pending orders", "orders_modified": 0}

    modified_count = 0
    failed_count = 0

    for order in orders:
        if order["type"] == order_type:
            result = deps.mt5_manager.modify_order(
                ticket=order["ticket"],
                price=order["price_open"],  # Keep same price
                sl=sl,
                tp=tp,
            )

            if result.get("success"):
                modified_count += 1
            else:
                failed_count += 1

    return {
        "success": modified_count > 0,
        "orders_modified": modified_count,
        "orders_failed": failed_count,
        "new_sl": sl,
        "new_tp": tp,
    }


async def execute_combined_action(
    signal: PineTunnelSignal, license_key: str, account_info: dict
) -> dict:
    """Execute combined close and open actions."""
    command = signal.command.value
    close_result = {"success": False}
    open_result = {"success": False}

    # Parse command to determine close action (with comment filter)
    if "close_long" in command and "short" not in command:
        close_result = close_positions_by_type(signal.symbol, "buy", comment=signal.comment)
    elif "close_short" in command:
        close_result = close_positions_by_type(signal.symbol, "sell", comment=signal.comment)
    elif "close_all" in command:
        close_result = close_all_positions(signal.symbol, comment=signal.comment)

    # Determine open action
    if command.endswith("openlong"):
        signal.command = CommandType.BUY
        open_result = await execute_market_order(signal, license_key, account_info)
    elif command.endswith("openshort"):
        signal.command = CommandType.SELL
        open_result = await execute_market_order(signal, license_key, account_info)

    return {
        "success": close_result.get("success", False) or open_result.get("success", False),
        "close_result": close_result,
        "open_result": open_result,
    }


async def execute_cancel_and_pending(
    signal: PineTunnelSignal, license_key: str, account_info: dict
) -> dict:
    """Execute cancel and place pending order."""
    command = signal.command.value

    # Cancel existing orders
    if "cancel_long" in command:
        cancel_result = cancel_pending_orders(signal.symbol, "buy")
    else:
        cancel_result = cancel_pending_orders(signal.symbol, "sell")

    # Determine pending order type
    if "buy_stop" in command:
        signal.command = CommandType.BUY_STOP
    elif "buy_limit" in command:
        signal.command = CommandType.BUY_LIMIT
    elif "sell_stop" in command:
        signal.command = CommandType.SELL_STOP
    elif "sell_limit" in command:
        signal.command = CommandType.SELL_LIMIT

    # Place new pending order
    pending_result = await execute_pending_order(signal, license_key, account_info)

    return {
        "success": cancel_result.get("success", False) or pending_result.get("success", False),
        "cancel_result": cancel_result,
        "pending_result": pending_result,
    }

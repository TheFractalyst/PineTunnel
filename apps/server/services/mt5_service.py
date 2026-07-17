"""MetaTrader 5 Connection Manager.

Handles MT5 initialization, order execution, position management,
and account queries. Operates in mock mode when MT5 is unavailable
(e.g., cloud deployment on ARM64).
"""

import logging
import random
import time
from typing import Optional

try:
    import MetaTrader5 as mt5

    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None

logger = logging.getLogger(__name__)


class MT5Manager:
    """MetaTrader 5 connection manager"""

    def __init__(self, config: dict):
        self.initialized = False
        self.magic_number = config.get("magic_number", 234000)
        self.deviation = config.get("deviation", 20)
        self.allowed_symbols = config.get("allowed_symbols", [])
        self.config = config

    def initialize(self) -> bool:
        """Initialize MT5 connection"""
        if self.initialized:
            return True

        if not MT5_AVAILABLE:
            logger.warning("MT5 package not available - running in MOCK mode")
            logger.info("Mock MT5: Account: 12345678, Balance: $10,000.00")
            self.initialized = True
            return True

        try:
            mt5_path = self.config.get("mt5_path")
            if mt5_path:
                success = mt5.initialize(path=mt5_path, timeout=5000)
            else:
                success = mt5.initialize(timeout=5000)

            if not success:
                logger.error("MT5 init failed: %s", mt5.last_error())
                return False

            if (
                self.config.get("login")
                and self.config.get("password")
                and self.config.get("server")
            ):
                if not mt5.login(
                    login=self.config["login"],
                    password=self.config["password"],
                    server=self.config["server"],
                ):
                    logger.error("MT5 login failed: %s", mt5.last_error())
                    return False

            self.initialized = True
            account = mt5.account_info()
            logger.info(
                f"MT5 connected - Account: {account.login}, Balance: ${account.balance:.2f}"
            )
            return True

        except (OSError, RuntimeError, AttributeError) as e:
            logger.error(
                f"MT5 init exception in initialize: {type(e).__name__}: {e}",
                extra={"context": {"operation": "mt5_initialize"}},
            )
            return False

    def shutdown(self):
        """Shutdown MT5 connection"""
        if self.initialized and MT5_AVAILABLE and mt5:
            mt5.shutdown()
            self.initialized = False
            logger.info("MT5 disconnected")

    def validate_symbol(self, symbol: str) -> tuple[bool, Optional[str], Optional[dict]]:
        """Validate trading symbol"""
        if self.allowed_symbols and symbol not in self.allowed_symbols:
            return False, f"Symbol {symbol} not allowed", None

        if not MT5_AVAILABLE or not mt5:
            return (
                True,
                None,
                {
                    "name": symbol,
                    "point": 0.00001,
                    "digits": 5,
                    "trade_contract_size": 100000,
                    "volume_min": 0.01,
                    "volume_max": 100.0,
                    "volume_step": 0.01,
                },
            )

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False, f"Symbol {symbol} not found", None

        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return False, f"Failed to select {symbol}", None

        return True, None, symbol_info._asdict()

    def get_filling_type(self, symbol: str, symbol_info=None) -> int:
        """Get appropriate filling type"""
        if not MT5_AVAILABLE or not mt5:
            return 0

        info = symbol_info or mt5.symbol_info(symbol)
        if not info:
            return mt5.ORDER_FILLING_FOK

        filling = info["filling_mode"] if isinstance(info, dict) else info.filling_mode

        if filling & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        elif filling & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        else:
            return mt5.ORDER_FILLING_RETURN

    def execute_order(
        self,
        symbol: str,
        action: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: Optional[int] = None,
    ) -> dict:
        """Execute market order"""
        valid, error, symbol_info = self.validate_symbol(symbol)
        if not valid:
            return {"success": False, "error": error}

        if not MT5_AVAILABLE or not mt5:
            return {
                "success": True,
                "ticket": random.randint(100000, 999999),
                "price": 1.085,
                "volume": volume * 10,
            }

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return {"success": False, "error": "Failed to get price"}

        if action == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": self.deviation,
            "magic": magic or self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.get_filling_type(symbol, symbol_info),
        }

        if sl:
            request["sl"] = sl
        if tp:
            request["tp"] = tp

        start_time = time.time()
        result = mt5.order_send(request)
        execution_time = (time.time() - start_time) * 1000

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = f"Order failed: {result.retcode} - {result.comment}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "retcode": result.retcode}

        logger.info("Order executed: %s %s %s @ %s", symbol, action, volume, result.price)

        return {
            "success": True,
            "ticket": result.order,
            "deal": result.deal,
            "volume": result.volume,
            "price": result.price,
            "execution_time_ms": execution_time,
        }

    def close_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> dict:
        """Close positions"""
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()

        if not positions:
            return {"success": True, "message": "No positions", "count": 0}

        closed = 0
        errors = []
        _filling_cache: dict[str, int] = {}

        for pos in positions:
            if magic and pos.magic != magic:
                continue

            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                errors.append(f"No price for {pos.symbol}")
                continue

            if pos.symbol not in _filling_cache:
                _filling_cache[pos.symbol] = self.get_filling_type(pos.symbol)

            close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if pos.type == 0 else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": close_price,
                "deviation": self.deviation,
                "magic": pos.magic,
                "comment": "Close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_cache[pos.symbol],
            }

            result = mt5.order_send(request)

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
                logger.info("Closed position %s", pos.ticket)
            else:
                errors.append(f"Failed to close {pos.ticket}: {result.comment}")

        return {
            "success": closed > 0,
            "closed_count": closed,
            "total": len(positions),
            "errors": errors if errors else None,
        }

    def get_account_info(self) -> dict:
        """Get account information"""
        if not self.initialized:
            return {"error": "MT5 not initialized"}

        if not MT5_AVAILABLE or not mt5:
            return {
                "login": 12345678,
                "server": "MockBroker-Demo",
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "margin_free": 10000.0,
                "margin_level": 999999,
                "profit": 0.0,
                "currency": "USD",
                "leverage": 100,
                "trade_allowed": True,
                "open_positions": 0,
            }

        account = mt5.account_info()
        if not account:
            return {"error": "Failed to get account info"}

        positions = mt5.positions_get()

        return {
            "login": account.login,
            "server": account.server,
            "balance": account.balance,
            "equity": account.equity,
            "margin": account.margin,
            "margin_free": account.margin_free,
            "margin_level": account.margin_level if account.margin > 0 else 0,
            "profit": account.profit,
            "currency": account.currency,
            "leverage": account.leverage,
            "trade_allowed": account.trade_allowed,
            "open_positions": len(positions) if positions else 0,
        }

    def get_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        position_type: Optional[str] = None,
    ) -> list:
        """Get open positions with filtering"""
        if not MT5_AVAILABLE or not mt5:
            return []

        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if not positions:
            return []

        result = []
        for pos in positions:
            if magic and pos.magic != magic:
                continue

            pos_type = "buy" if pos.type == 0 else "sell"
            if position_type and pos_type != position_type:
                continue

            result.append(
                {
                    "ticket": pos.ticket,
                    "symbol": pos.symbol,
                    "type": pos_type,
                    "volume": pos.volume,
                    "price_open": pos.price_open,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "profit": pos.profit,
                    "magic": pos.magic,
                    "comment": pos.comment,
                }
            )

        return result

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> list:
        """Get pending orders"""
        if not MT5_AVAILABLE or not mt5:
            return []

        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if not orders:
            return []

        result = []
        order_type_map = {
            mt5.ORDER_TYPE_BUY_LIMIT: "buy_limit",
            mt5.ORDER_TYPE_SELL_LIMIT: "sell_limit",
            mt5.ORDER_TYPE_BUY_STOP: "buy_stop",
            mt5.ORDER_TYPE_SELL_STOP: "sell_stop",
        }

        for order in orders:
            if magic and order.magic != magic:
                continue

            result.append(
                {
                    "ticket": order.ticket,
                    "symbol": order.symbol,
                    "type": order_type_map.get(order.type, "unknown"),
                    "volume": order.volume_current,
                    "price_open": order.price_open,
                    "sl": order.sl,
                    "tp": order.tp,
                    "magic": order.magic,
                    "comment": order.comment,
                }
            )

        return result

    def place_pending_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        price: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: Optional[int] = None,
    ) -> dict:
        """Place pending order"""
        valid, error, symbol_info = self.validate_symbol(symbol)
        if not valid:
            return {"success": False, "error": error}

        if not MT5_AVAILABLE or not mt5:
            return {
                "success": True,
                "ticket": random.randint(100000, 999999),
                "type": order_type,
                "price": price,
            }

        type_map = {
            "buy_stop": mt5.ORDER_TYPE_BUY_STOP,
            "buy_limit": mt5.ORDER_TYPE_BUY_LIMIT,
            "sell_stop": mt5.ORDER_TYPE_SELL_STOP,
            "sell_limit": mt5.ORDER_TYPE_SELL_LIMIT,
        }

        mt5_order_type = type_map.get(order_type)
        if not mt5_order_type:
            return {"success": False, "error": f"Invalid order type: {order_type}"}

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": mt5_order_type,
            "price": price,
            "deviation": self.deviation,
            "magic": magic or self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.get_filling_type(symbol, symbol_info),
        }

        if sl:
            request["sl"] = sl
        if tp:
            request["tp"] = tp

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = f"Pending order failed: {result.retcode} - {result.comment}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "retcode": result.retcode}

        logger.info("Pending order placed: %s %s %s @ %s", symbol, order_type, volume, price)

        return {
            "success": True,
            "ticket": result.order,
            "type": order_type,
            "price": price,
            "volume": volume,
        }

    def close_position_partial(self, ticket: int, volume: float) -> dict:
        """Close partial position"""
        if not MT5_AVAILABLE or not mt5:
            return {"success": True, "closed_volume": volume}

        position = mt5.positions_get(ticket=ticket)
        if not position:
            return {"success": False, "error": "Position not found"}

        pos = position[0]

        if volume > pos.volume:
            volume = pos.volume

        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            return {"success": False, "error": "Failed to get price"}

        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if pos.type == 0 else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": volume,
            "type": close_type,
            "position": pos.ticket,
            "price": close_price,
            "deviation": self.deviation,
            "magic": pos.magic,
            "comment": "Partial close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.get_filling_type(pos.symbol),
        }

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "error": f"Failed: {result.comment}"}

        return {
            "success": True,
            "ticket": result.deal,
            "closed_volume": volume,
            "remaining_volume": pos.volume - volume,
        }

    def cancel_order(self, ticket: int) -> dict:
        """Cancel pending order"""
        if not MT5_AVAILABLE or not mt5:
            return {"success": True, "ticket": ticket}

        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "error": f"Failed: {result.comment}"}

        logger.info("Cancelled order %s", ticket)
        return {"success": True, "ticket": ticket}

    def modify_position(
        self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> dict:
        """Modify position SL/TP"""
        if not MT5_AVAILABLE or not mt5:
            return {"success": True, "ticket": ticket, "sl": sl, "tp": tp}

        position = mt5.positions_get(ticket=ticket)
        if not position:
            return {"success": False, "error": "Position not found"}

        pos = position[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": sl if sl is not None else pos.sl,
            "tp": tp if tp is not None else pos.tp,
        }

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "error": f"Failed: {result.comment}"}

        logger.info("Modified position %s: SL=%s, TP=%s", ticket, sl, tp)
        return {"success": True, "ticket": ticket, "sl": sl, "tp": tp}

    def modify_order(
        self,
        ticket: int,
        price: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        """Modify pending order"""
        if not MT5_AVAILABLE or not mt5:
            return {"success": True, "ticket": ticket, "price": price}

        order = mt5.orders_get(ticket=ticket)
        if not order:
            return {"success": False, "error": "Order not found"}

        pending_order = order[0]

        request = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": ticket,
            "price": price,
            "sl": sl if sl is not None else pending_order.sl,
            "tp": tp if tp is not None else pending_order.tp,
        }

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "error": f"Failed: {result.comment}"}

        logger.info("Modified order %s: price=%s, SL=%s, TP=%s", ticket, price, sl, tp)
        return {"success": True, "ticket": ticket, "price": price}

"""Grid trading engine — calculates levels and manages buy/sell logic."""

import logging
from dataclasses import dataclass, field
from enum import Enum

from exchange.symbol_info import SymbolInfo

logger = logging.getLogger(__name__)


class LevelState(str, Enum):
    EMPTY = "empty"        # No position — waiting to place buy
    BUY_PENDING = "buy_pending"    # Buy order placed, waiting for fill
    HOLDING = "holding"    # Bought, waiting to place sell
    SELL_PENDING = "sell_pending"  # Sell order placed, waiting for fill


@dataclass
class GridLevel:
    index: int
    buy_price: float       # Price at which we buy
    sell_price: float      # Price at which we sell (one grid step above)
    state: LevelState = LevelState.EMPTY
    order_id: int | None = None
    buy_fill_price: float | None = None
    quantity: float = 0.0


@dataclass
class GridConfig:
    symbol: str
    upper_price: float
    lower_price: float
    num_levels: int
    total_capital: float


class GridEngine:
    def __init__(self, config: GridConfig, symbol_info: SymbolInfo):
        self.config = config
        self.symbol_info = symbol_info
        self.levels: list[GridLevel] = []
        self.grid_step: float = 0.0
        self.total_profit: float = 0.0
        self.completed_cycles: int = 0
        self._build_grid()

    def _build_grid(self):
        self.grid_step = (self.config.upper_price - self.config.lower_price) / self.config.num_levels
        self.levels = []
        for i in range(self.config.num_levels):
            buy_price = self.config.lower_price + (i * self.grid_step)
            sell_price = buy_price + self.grid_step
            self.levels.append(GridLevel(
                index=i,
                buy_price=round(buy_price, 8),
                sell_price=round(sell_price, 8),
            ))
        logger.info(
            "Grid built: %d levels | %s | range %.8f - %.8f | step %.8f",
            len(self.levels),
            self.config.symbol,
            self.config.lower_price,
            self.config.upper_price,
            self.grid_step,
        )

    def capital_per_level(self) -> float:
        return self.config.total_capital / self.config.num_levels

    def is_price_in_range(self, price: float) -> bool:
        return self.config.lower_price <= price <= self.config.upper_price

    def get_levels_to_buy(self, current_price: float) -> list[GridLevel]:
        """Return EMPTY levels with buy_price at or below current price."""
        result = []
        for level in self.levels:
            if level.state == LevelState.EMPTY and level.buy_price <= current_price:
                result.append(level)
        return result

    def get_levels_to_sell(self, current_price: float) -> list[GridLevel]:
        """Return HOLDING levels with sell_price at or below current price."""
        result = []
        for level in self.levels:
            if level.state == LevelState.HOLDING and level.sell_price <= current_price:
                result.append(level)
        return result

    def prepare_buy_order(self, level: GridLevel) -> dict | None:
        """Prepare a buy order for a grid level. Returns order params or None if invalid."""
        spend = self.capital_per_level()
        qty = self.symbol_info.quantity_for_spend(spend, level.buy_price)
        price_str = self.symbol_info.format_price(level.buy_price)
        qty_str = self.symbol_info.format_quantity(qty)

        valid, reason = self.symbol_info.validate_order(level.buy_price, qty)
        if not valid:
            logger.warning("Skip buy at level %d (%.8f): %s", level.index, level.buy_price, reason)
            return None

        return {
            "symbol": self.config.symbol,
            "side": "BUY",
            "price": price_str,
            "quantity": qty_str,
            "level_index": level.index,
        }

    def prepare_sell_order(self, level: GridLevel) -> dict | None:
        """Prepare a sell order for a grid level. Returns order params or None if invalid."""
        qty = level.quantity
        price_str = self.symbol_info.format_price(level.sell_price)
        qty_str = self.symbol_info.format_quantity(qty)

        valid, reason = self.symbol_info.validate_order(level.sell_price, qty)
        if not valid:
            logger.warning("Skip sell at level %d (%.8f): %s", level.index, level.sell_price, reason)
            return None

        return {
            "symbol": self.config.symbol,
            "side": "SELL",
            "price": price_str,
            "quantity": qty_str,
            "level_index": level.index,
        }

    def on_buy_placed(self, level_index: int, order_id: int):
        level = self.levels[level_index]
        level.state = LevelState.BUY_PENDING
        level.order_id = order_id
        logger.info("BUY order placed: level %d @ %.8f (order %s)", level_index, level.buy_price, order_id)

    def on_buy_filled(self, order_id: int, filled_price: float, filled_qty: float):
        level = self._find_level_by_order(order_id)
        if not level:
            logger.warning("Buy fill for unknown order %s", order_id)
            return None
        level.state = LevelState.HOLDING
        level.buy_fill_price = filled_price
        level.quantity = filled_qty
        level.order_id = None
        logger.info("BUY filled: level %d @ %.8f (qty: %.8f)", level.index, filled_price, filled_qty)
        return level

    def on_sell_placed(self, level_index: int, order_id: int):
        level = self.levels[level_index]
        level.state = LevelState.SELL_PENDING
        level.order_id = order_id
        logger.info("SELL order placed: level %d @ %.8f (order %s)", level_index, level.sell_price, order_id)

    def on_sell_filled(self, order_id: int, filled_price: float, filled_qty: float) -> float | None:
        """Returns profit from the completed cycle, or None if order not found."""
        level = self._find_level_by_order(order_id)
        if not level:
            logger.warning("Sell fill for unknown order %s", order_id)
            return None

        profit = (filled_price - level.buy_fill_price) * filled_qty
        self.total_profit += profit
        self.completed_cycles += 1

        logger.info(
            "SELL filled: level %d @ %.8f (bought @ %.8f) | profit: %.4f | total: %.4f | cycles: %d",
            level.index, filled_price, level.buy_fill_price, profit, self.total_profit, self.completed_cycles,
        )

        # Reset level for next cycle
        level.state = LevelState.EMPTY
        level.order_id = None
        level.buy_fill_price = None
        level.quantity = 0.0

        return profit

    def on_order_cancelled(self, order_id: int):
        level = self._find_level_by_order(order_id)
        if not level:
            return
        if level.state == LevelState.BUY_PENDING:
            level.state = LevelState.EMPTY
        elif level.state == LevelState.SELL_PENDING:
            level.state = LevelState.HOLDING
        level.order_id = None
        logger.info("Order cancelled: level %d reverted to %s", level.index, level.state.value)

    def cancel_all_pending(self) -> list[int]:
        """Return order IDs of all pending orders."""
        order_ids = []
        for level in self.levels:
            if level.order_id is not None:
                order_ids.append(level.order_id)
        return order_ids

    def get_status_summary(self) -> dict:
        states = {}
        for level in self.levels:
            states[level.state.value] = states.get(level.state.value, 0) + 1
        return {
            "symbol": self.config.symbol,
            "levels": len(self.levels),
            "grid_step": self.grid_step,
            "capital_per_level": self.capital_per_level(),
            "total_profit": self.total_profit,
            "completed_cycles": self.completed_cycles,
            "level_states": states,
        }

    def _find_level_by_order(self, order_id: int) -> GridLevel | None:
        for level in self.levels:
            if level.order_id == order_id:
                return level
        return None

"""Scalp trading engine — momentum and mean reversion modes."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from exchange.binance_adapter import BinanceAdapter
from exchange.symbol_info import SymbolInfo

logger = logging.getLogger(__name__)


class ScalpMode(str, Enum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"


class ScalpState(str, Enum):
    SCANNING = "scanning"
    ENTERING = "entering"
    IN_POSITION = "in_position"
    EXITING = "exiting"
    COOLDOWN = "cooldown"
    STOPPED = "stopped"


@dataclass
class ScalpTrade:
    symbol: str
    direction: str       # "BUY"
    entry_price: float
    entry_time: float
    quantity: float
    exit_price: float | None = None
    exit_time: float | None = None
    profit: float | None = None
    exit_reason: str | None = None  # "tp", "sl", "timeout"
    order_id: str | None = None


@dataclass
class ScalpStats:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_profit: float = 0.0
    total_duration: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

    @property
    def avg_profit(self) -> float:
        return (self.total_profit / self.total_trades) if self.total_trades > 0 else 0.0

    @property
    def avg_duration(self) -> float:
        return (self.total_duration / self.total_trades) if self.total_trades > 0 else 0.0


class ScalpEngine:
    def __init__(
        self,
        symbol: str,
        symbol_info: SymbolInfo,
        exchange: BinanceAdapter,
        mode: ScalpMode = ScalpMode.MOMENTUM,
        trigger_pct: float = 0.5,
        trigger_window: int = 30,
        tp_pct: float = 0.4,
        sl_pct: float = 0.3,
        time_limit: int = 120,
        capital: float = 10.0,
        trade_pct: float = 50.0,
        cooldown: int = 5,
    ):
        self.symbol = symbol
        self.symbol_info = symbol_info
        self.exchange = exchange
        self.mode = mode
        self.trigger_pct = trigger_pct / 100  # convert to decimal
        self.trigger_window = trigger_window
        self.tp_pct = tp_pct / 100
        self.sl_pct = sl_pct / 100
        self.time_limit = time_limit
        self.capital = capital
        self.trade_pct = trade_pct / 100
        self.cooldown = cooldown

        self.state = ScalpState.SCANNING
        self.current_trade: ScalpTrade | None = None
        self.stats = ScalpStats()
        self.recent_trades: deque[ScalpTrade] = deque(maxlen=50)
        self._price_window: deque[tuple[float, float]] = deque()  # (timestamp, price)
        self._last_price: float = 0.0
        self._cooldown_until: float = 0.0
        self._running = False

        # Callbacks — set by bot.py
        self.on_trade_complete: callable | None = None  # async callback(ScalpTrade)
        self.on_state_change: callable | None = None    # async callback(ScalpState)

    async def start(self):
        """Subscribe to price stream and start scanning."""
        self._running = True
        self.state = ScalpState.SCANNING
        await self.exchange.subscribe_price_stream(self.symbol, self._on_tick)
        logger.info("Scalp engine started: %s mode=%s trigger=%.1f%%/%.0fs",
                     self.symbol, self.mode.value, self.trigger_pct * 100, self.trigger_window)

    async def stop(self):
        """Stop the engine, close any open position."""
        self._running = False
        if self.state == ScalpState.IN_POSITION and self.current_trade:
            await self._exit_position("stopped")
        self.state = ScalpState.STOPPED
        logger.info("Scalp engine stopped")

    async def _on_tick(self, data: dict):
        """Handle each price tick."""
        if not self._running:
            return

        price = data["price"]
        now = time.time()
        self._last_price = price

        # Always update price window
        self._price_window.append((now, price))
        # Trim old entries
        cutoff = now - self.trigger_window
        while self._price_window and self._price_window[0][0] < cutoff:
            self._price_window.popleft()

        if self.state == ScalpState.SCANNING:
            await self._check_trigger(now, price)
        elif self.state == ScalpState.IN_POSITION:
            await self._check_exit(now, price)
        elif self.state == ScalpState.COOLDOWN:
            if now >= self._cooldown_until:
                self.state = ScalpState.SCANNING

    async def _check_trigger(self, now: float, price: float):
        """Check if price movement triggers an entry."""
        if len(self._price_window) < 2:
            return

        oldest_price = self._price_window[0][1]
        if oldest_price <= 0:
            return

        pct_change = (price - oldest_price) / oldest_price

        should_enter = False

        if self.mode == ScalpMode.MOMENTUM:
            # Enter in direction of move
            if pct_change >= self.trigger_pct:
                should_enter = True  # price going up, buy to ride
            # For spot trading, we can only buy (no shorting)
        elif self.mode == ScalpMode.MEAN_REVERSION:
            # Enter opposite to move — buy the dip
            if pct_change <= -self.trigger_pct:
                should_enter = True  # price dropped, buy expecting revert

        if should_enter:
            await self._enter_position(price)

    async def _enter_position(self, price: float):
        """Place market buy order."""
        self.state = ScalpState.ENTERING

        trade_capital = self.capital * self.trade_pct
        quantity = self.symbol_info.quantity_for_spend(trade_capital, price)
        qty_str = self.symbol_info.format_quantity(quantity)

        valid, reason = self.symbol_info.validate_order(price, quantity)
        if not valid:
            logger.warning("Scalp entry rejected: %s", reason)
            self.state = ScalpState.SCANNING
            return

        try:
            result = await self.exchange.place_market_order(
                symbol=self.symbol,
                side="BUY",
                quantity=float(qty_str),
            )

            filled_price = float(result.get("fills", [{}])[0].get("price", price)) if result.get("fills") else price
            filled_qty = float(result.get("executedQty", qty_str))

            self.current_trade = ScalpTrade(
                symbol=self.symbol,
                direction="BUY",
                entry_price=filled_price,
                entry_time=time.time(),
                quantity=filled_qty,
                order_id=str(result.get("orderId", "")),
            )
            self.state = ScalpState.IN_POSITION
            logger.info("SCALP ENTRY: %s BUY @ %.8f qty=%.8f (mode=%s)",
                         self.symbol, filled_price, filled_qty, self.mode.value)

        except Exception:
            logger.exception("Scalp entry failed")
            self.state = ScalpState.SCANNING

    async def _check_exit(self, now: float, price: float):
        """Check exit conditions for current position."""
        if not self.current_trade:
            return

        entry = self.current_trade.entry_price
        elapsed = now - self.current_trade.entry_time

        # Take profit
        if price >= entry * (1 + self.tp_pct):
            await self._exit_position("tp")
            return

        # Stop loss
        if price <= entry * (1 - self.sl_pct):
            await self._exit_position("sl")
            return

        # Time limit
        if elapsed >= self.time_limit:
            await self._exit_position("timeout")
            return

    async def _exit_position(self, reason: str):
        """Place market sell order and record results."""
        if not self.current_trade:
            return

        self.state = ScalpState.EXITING

        try:
            qty_str = self.symbol_info.format_quantity(self.current_trade.quantity)
            result = await self.exchange.place_market_order(
                symbol=self.symbol,
                side="SELL",
                quantity=float(qty_str),
            )

            exit_price = float(result.get("fills", [{}])[0].get("price", self._last_price)) if result.get("fills") else self._last_price
            now = time.time()

            self.current_trade.exit_price = exit_price
            self.current_trade.exit_time = now
            self.current_trade.exit_reason = reason
            self.current_trade.profit = (exit_price - self.current_trade.entry_price) * self.current_trade.quantity

            # Update stats
            self.stats.total_trades += 1
            self.stats.total_profit += self.current_trade.profit
            self.stats.total_duration += now - self.current_trade.entry_time
            if self.current_trade.profit >= 0:
                self.stats.wins += 1
            else:
                self.stats.losses += 1

            self.recent_trades.append(self.current_trade)

            logger.info(
                "SCALP EXIT [%s]: %s @ %.8f → %.8f | profit=%.6f | duration=%.1fs",
                reason.upper(), self.symbol, self.current_trade.entry_price,
                exit_price, self.current_trade.profit, now - self.current_trade.entry_time,
            )

            # Notify via callback
            if self.on_trade_complete:
                await self.on_trade_complete(self.current_trade)

        except Exception:
            logger.exception("Scalp exit failed")

        self.current_trade = None
        self._cooldown_until = time.time() + self.cooldown
        self.state = ScalpState.COOLDOWN

    def get_status(self) -> dict:
        result = {
            "state": self.state.value,
            "mode": self.mode.value,
            "symbol": self.symbol,
            "last_price": self._last_price,
            "capital": self.capital,
            "stats": {
                "total_trades": self.stats.total_trades,
                "wins": self.stats.wins,
                "losses": self.stats.losses,
                "win_rate": round(self.stats.win_rate, 1),
                "total_profit": round(self.stats.total_profit, 6),
                "avg_profit": round(self.stats.avg_profit, 6),
                "avg_duration": round(self.stats.avg_duration, 1),
            },
        }
        if self.current_trade:
            elapsed = time.time() - self.current_trade.entry_time
            unrealised = (self._last_price - self.current_trade.entry_price) * self.current_trade.quantity
            result["current_trade"] = {
                "entry_price": self.current_trade.entry_price,
                "quantity": self.current_trade.quantity,
                "elapsed": round(elapsed, 1),
                "unrealised_pnl": round(unrealised, 6),
            }
        return result

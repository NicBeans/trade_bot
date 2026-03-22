"""Scalp trading engine — volume spike (recommended), momentum, and mean reversion modes."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from exchange.binance_adapter import BinanceAdapter
from exchange.symbol_info import SymbolInfo

logger = logging.getLogger(__name__)

VOLUME_BUCKET_SECONDS = 10
VOLUME_BASELINE_BUCKETS = 360  # 60 minutes of 10s buckets


class ScalpMode(str, Enum):
    VOLUME_SPIKE = "volume_spike"
    MOMENTUM = "momentum"            # deprecated
    MEAN_REVERSION = "mean_reversion"  # deprecated


class ScalpState(str, Enum):
    WARMING_UP = "warming_up"
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
    false_signals: int = 0
    volume_spikes_detected: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

    @property
    def avg_profit(self) -> float:
        return (self.total_profit / self.total_trades) if self.total_trades > 0 else 0.0

    @property
    def avg_duration(self) -> float:
        return (self.total_duration / self.total_trades) if self.total_trades > 0 else 0.0


# --- Volume Tracker ---

@dataclass
class VolumeBucket:
    timestamp: float
    total_volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0


class VolumeTracker:
    """Tracks rolling volume in 10-second buckets and detects spikes."""

    def __init__(self, spike_multiplier: float = 3.0, direction_pct: float = 65.0):
        self.spike_multiplier = spike_multiplier
        self.direction_threshold = direction_pct / 100
        self._buckets: deque[VolumeBucket] = deque(maxlen=VOLUME_BASELINE_BUCKETS)
        self._current_bucket: VolumeBucket | None = None
        self._baseline_volume: float = 0.0
        self._seeded = False

    def seed_from_klines(self, klines: list[list]):
        """Seed baseline from historical 1m klines. Each candle split into 6 buckets."""
        for k in klines:
            candle_volume = float(k[5])  # volume field
            bucket_volume = candle_volume / 6
            for _ in range(6):
                self._buckets.append(VolumeBucket(
                    timestamp=0,
                    total_volume=bucket_volume,
                    buy_volume=bucket_volume * 0.5,  # assume 50/50 for historical
                    sell_volume=bucket_volume * 0.5,
                ))
        self._update_baseline()
        self._seeded = True
        logger.info("Volume baseline seeded: %.4f avg per 10s bucket (%d buckets)",
                     self._baseline_volume, len(self._buckets))

    def on_trade(self, quantity: float, is_buyer_maker: bool, timestamp: float):
        """Feed a trade into the current bucket."""
        now = timestamp

        # Start new bucket if needed
        if self._current_bucket is None:
            self._current_bucket = VolumeBucket(timestamp=now)

        # Check if current bucket has expired (10s boundary)
        if now - self._current_bucket.timestamp >= VOLUME_BUCKET_SECONDS:
            # Finalize current bucket
            completed = self._current_bucket
            self._buckets.append(completed)
            self._update_baseline()
            # Start new bucket
            self._current_bucket = VolumeBucket(timestamp=now)
            return completed  # Return completed bucket for spike checking

        # Accumulate into current bucket
        self._current_bucket.total_volume += quantity
        if is_buyer_maker:
            self._current_bucket.sell_volume += quantity
        else:
            self._current_bucket.buy_volume += quantity

        return None  # No completed bucket yet

    def check_spike(self, bucket: VolumeBucket) -> dict | None:
        """Check if a completed bucket is a volume spike. Returns signal dict or None."""
        if not self._seeded or self._baseline_volume <= 0:
            return None

        if bucket.total_volume < self._baseline_volume * self.spike_multiplier:
            return None

        # Spike detected — check direction
        if bucket.total_volume <= 0:
            return None

        buy_ratio = bucket.buy_volume / bucket.total_volume
        multiplier = bucket.total_volume / self._baseline_volume

        if buy_ratio >= self.direction_threshold:
            return {
                "direction": "BUY",
                "multiplier": round(multiplier, 1),
                "buy_ratio": round(buy_ratio * 100, 1),
                "volume": bucket.total_volume,
                "baseline": self._baseline_volume,
            }

        # Can't short on spot, so skip sell-dominant spikes
        return None

    def _update_baseline(self):
        if not self._buckets:
            return
        self._baseline_volume = sum(b.total_volume for b in self._buckets) / len(self._buckets)

    def get_status(self) -> dict:
        current_vol = self._current_bucket.total_volume if self._current_bucket else 0
        return {
            "baseline": round(self._baseline_volume, 4),
            "current_bucket_volume": round(current_vol, 4),
            "multiplier": round(current_vol / self._baseline_volume, 1) if self._baseline_volume > 0 else 0,
            "buckets_tracked": len(self._buckets),
            "seeded": self._seeded,
        }


# --- Scalp Engine ---

class ScalpEngine:
    def __init__(
        self,
        symbol: str,
        symbol_info: SymbolInfo,
        exchange: BinanceAdapter,
        mode: ScalpMode = ScalpMode.VOLUME_SPIKE,
        # Price-based trigger settings (deprecated modes)
        trigger_pct: float = 0.5,
        trigger_window: int = 30,
        time_limit: int = 120,
        # Volume spike settings
        volume_multiplier: float = 3.0,
        volume_direction_pct: float = 65.0,
        volume_timeout: int = 45,
        false_signal_cooldown: int = 20,
        # Common settings
        tp_pct: float = 0.4,
        sl_pct: float = 0.3,
        capital: float = 10.0,
        trade_pct: float = 50.0,
        cooldown: int = 5,
    ):
        self.symbol = symbol
        self.symbol_info = symbol_info
        self.exchange = exchange
        self.mode = mode

        # Price-based (deprecated)
        self.trigger_pct = trigger_pct / 100
        self.trigger_window = trigger_window
        self.time_limit = time_limit

        # Volume spike
        self.volume_timeout = volume_timeout
        self.false_signal_cooldown = false_signal_cooldown

        # Common
        self.tp_pct = tp_pct / 100
        self.sl_pct = sl_pct / 100
        self.capital = capital
        self.trade_pct = trade_pct / 100
        self.cooldown = cooldown

        self.state = ScalpState.SCANNING
        self.current_trade: ScalpTrade | None = None
        self.stats = ScalpStats()
        self.recent_trades: deque[ScalpTrade] = deque(maxlen=50)
        self._price_window: deque[tuple[float, float]] = deque()
        self._last_price: float = 0.0
        self._cooldown_until: float = 0.0
        self._running = False

        # Volume tracker (only for volume_spike mode)
        self._volume_tracker: VolumeTracker | None = None
        if mode == ScalpMode.VOLUME_SPIKE:
            self._volume_tracker = VolumeTracker(
                spike_multiplier=volume_multiplier,
                direction_pct=volume_direction_pct,
            )

        # Callbacks
        self.on_trade_complete: callable | None = None
        self.on_state_change: callable | None = None

    async def start(self):
        """Subscribe to price stream and start scanning."""
        self._running = True

        # Seed volume baseline if in volume_spike mode
        if self._volume_tracker:
            self.state = ScalpState.WARMING_UP
            try:
                klines = await self.exchange.get_klines(symbol=self.symbol, interval="1m", limit=60)
                self._volume_tracker.seed_from_klines(klines)
            except Exception:
                logger.exception("Failed to seed volume baseline, falling back to live warmup")

        self.state = ScalpState.SCANNING
        await self.exchange.subscribe_price_stream(self.symbol, self._on_tick)

        mode_info = self.mode.value
        if self.mode == ScalpMode.VOLUME_SPIKE:
            mode_info += f" (multiplier={self._volume_tracker.spike_multiplier}x)"
        else:
            mode_info += f" [DEPRECATED] (trigger={self.trigger_pct*100:.1f}%/{self.trigger_window}s)"

        logger.info("Scalp engine started: %s mode=%s", self.symbol, mode_info)

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

        if self.state == ScalpState.SCANNING:
            if self.mode == ScalpMode.VOLUME_SPIKE:
                await self._check_volume_trigger(data, now, price)
            else:
                # Deprecated price-based modes
                self._price_window.append((now, price))
                cutoff = now - self.trigger_window
                while self._price_window and self._price_window[0][0] < cutoff:
                    self._price_window.popleft()
                await self._check_price_trigger(now, price)
        elif self.state == ScalpState.IN_POSITION:
            # Feed volume tracker even while in position (keeps baseline current)
            if self._volume_tracker:
                quantity = data.get("quantity", 0)
                is_buyer_maker = data.get("side", "") == "BUY"  # isBuyerMaker
                self._volume_tracker.on_trade(quantity, is_buyer_maker, now)
            await self._check_exit(now, price)
        elif self.state == ScalpState.COOLDOWN:
            # Keep volume tracker running during cooldown
            if self._volume_tracker:
                quantity = data.get("quantity", 0)
                is_buyer_maker = data.get("side", "") == "BUY"
                self._volume_tracker.on_trade(quantity, is_buyer_maker, now)
            if now >= self._cooldown_until:
                self.state = ScalpState.SCANNING

    async def _check_volume_trigger(self, data: dict, now: float, price: float):
        """Check for volume spike trigger."""
        quantity = data.get("quantity", 0)
        # In Binance trade stream, "side" == "BUY" means isBuyerMaker=True (seller initiated)
        is_buyer_maker = data.get("side", "") == "BUY"

        completed_bucket = self._volume_tracker.on_trade(quantity, is_buyer_maker, now)
        if completed_bucket is None:
            return

        signal = self._volume_tracker.check_spike(completed_bucket)
        if signal is None:
            return

        self.stats.volume_spikes_detected += 1
        logger.info(
            "VOLUME SPIKE: %s | %.1fx baseline | buy ratio: %.1f%% | vol: %.4f",
            self.symbol, signal["multiplier"], signal["buy_ratio"], signal["volume"],
        )

        if signal["direction"] == "BUY":
            await self._enter_position(price)

    async def _check_price_trigger(self, now: float, price: float):
        """Check if price movement triggers an entry (deprecated modes)."""
        if len(self._price_window) < 2:
            return

        oldest_price = self._price_window[0][1]
        if oldest_price <= 0:
            return

        pct_change = (price - oldest_price) / oldest_price
        should_enter = False

        if self.mode == ScalpMode.MOMENTUM:
            if pct_change >= self.trigger_pct:
                should_enter = True
        elif self.mode == ScalpMode.MEAN_REVERSION:
            if pct_change <= -self.trigger_pct:
                should_enter = True

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

        # Time limit — use volume timeout for volume_spike mode
        timeout = self.volume_timeout if self.mode == ScalpMode.VOLUME_SPIKE else self.time_limit
        if elapsed >= timeout:
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

            # Track false signals
            is_false_signal = reason == "timeout" and self.current_trade.profit <= 0
            if is_false_signal:
                self.stats.false_signals += 1

            self.recent_trades.append(self.current_trade)

            logger.info(
                "SCALP EXIT [%s]: %s @ %.8f -> %.8f | profit=%.6f | duration=%.1fs%s",
                reason.upper(), self.symbol, self.current_trade.entry_price,
                exit_price, self.current_trade.profit, now - self.current_trade.entry_time,
                " (false signal)" if is_false_signal else "",
            )

            # Notify via callback
            if self.on_trade_complete:
                await self.on_trade_complete(self.current_trade)

        except Exception:
            logger.exception("Scalp exit failed")

        # Cooldown — longer after false signals
        is_false = reason == "timeout" and (self.current_trade and self.current_trade.profit and self.current_trade.profit <= 0)
        if is_false and self.mode == ScalpMode.VOLUME_SPIKE:
            cooldown_time = self.false_signal_cooldown
        else:
            cooldown_time = self.cooldown

        self.current_trade = None
        self._cooldown_until = time.time() + cooldown_time
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
                "false_signals": self.stats.false_signals,
                "volume_spikes_detected": self.stats.volume_spikes_detected,
            },
        }
        if self._volume_tracker:
            result["volume"] = self._volume_tracker.get_status()
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

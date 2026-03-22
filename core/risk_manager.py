"""Risk manager — validates trades against preset rules."""

import time
import logging
from config.presets import RiskPreset

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, preset: RiskPreset, total_capital: float):
        self.preset = preset
        self.total_capital = total_capital
        self.available_capital = total_capital
        self.cumulative_loss = 0.0
        self.cumulative_profit = 0.0
        self._last_grid_reset_time: float = 0.0

    def can_place_order(self, order_value: float, min_order_size: float) -> tuple[bool, str]:
        max_per_level = self.total_capital * self.preset.max_capital_per_level_pct
        if order_value > max_per_level:
            return False, f"Order value {order_value:.4f} exceeds max per level {max_per_level:.4f}"

        if order_value < min_order_size:
            return False, f"Order value {order_value:.4f} below minimum {min_order_size:.4f}"

        if order_value > self.available_capital:
            return False, f"Order value {order_value:.4f} exceeds available capital {self.available_capital:.4f}"

        if self.is_stop_loss_triggered():
            max_loss = self.total_capital * self.preset.stop_loss_pct
            return False, f"Stop-loss active: cumulative loss {self.cumulative_loss:.4f} >= {max_loss:.4f}"

        return True, "OK"

    def reserve_capital(self, amount: float):
        """Reserve capital for a pending order."""
        self.available_capital -= amount
        logger.debug("Capital reserved: %.4f (available: %.4f)", amount, self.available_capital)

    def release_capital(self, amount: float):
        """Release reserved capital (e.g., order cancelled)."""
        self.available_capital += amount
        logger.debug("Capital released: %.4f (available: %.4f)", amount, self.available_capital)

    def record_trade(self, profit: float, capital_returned: float):
        """Record a completed trade cycle."""
        if profit >= 0:
            self.cumulative_profit += profit
        else:
            self.cumulative_loss += abs(profit)
        self.available_capital += capital_returned
        logger.info(
            "Trade recorded: profit=%.4f | total_profit=%.4f | total_loss=%.4f | available=%.4f",
            profit, self.cumulative_profit, self.cumulative_loss, self.available_capital,
        )

    def record_loss(self, amount: float):
        self.cumulative_loss += amount
        logger.warning("Loss recorded: %.4f (cumulative: %.4f)", amount, self.cumulative_loss)

    def is_stop_loss_triggered(self) -> bool:
        if self.preset.stop_loss_pct is None:
            return False
        return self.cumulative_loss >= (self.total_capital * self.preset.stop_loss_pct)

    def can_reset_grid(self) -> tuple[bool, str]:
        """Check if grid reset is allowed (cooldown enforced)."""
        elapsed = time.time() - self._last_grid_reset_time
        if elapsed < self.preset.grid_reset_cooldown_seconds:
            remaining = self.preset.grid_reset_cooldown_seconds - elapsed
            return False, f"Grid reset cooldown: {remaining:.0f}s remaining"
        return True, "OK"

    def record_grid_reset(self):
        """Record that a grid reset occurred."""
        self._last_grid_reset_time = time.time()
        logger.info("Grid reset recorded at %s", time.ctime(self._last_grid_reset_time))

    def get_status(self) -> dict:
        return {
            "total_capital": self.total_capital,
            "available_capital": self.available_capital,
            "cumulative_profit": self.cumulative_profit,
            "cumulative_loss": self.cumulative_loss,
            "net_pnl": self.cumulative_profit - self.cumulative_loss,
            "stop_loss_triggered": self.is_stop_loss_triggered(),
            "preset": self.preset.name,
        }

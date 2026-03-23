"""Parse Binance symbol trading rules and format orders with correct precision."""

import math
import logging

logger = logging.getLogger(__name__)


class SymbolInfo:
    def __init__(self, raw: dict):
        self.symbol = raw["symbol"]
        self.status = raw["status"]
        self.base_asset = raw["baseAsset"]
        self.quote_asset = raw["quoteAsset"]

        self.tick_size = 0.0
        self.price_precision = 8
        self.step_size = 0.0
        self.quantity_precision = 8
        self.min_qty = 0.0
        self.max_qty = 0.0
        self.min_notional = 0.0
        self.bid_multiplier_down = 0.0  # PERCENT_PRICE_BY_SIDE
        self.ask_multiplier_up = 0.0

        for f in raw.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                self.tick_size = float(f["tickSize"])
                self.price_precision = self._precision_from_step(self.tick_size)
            elif f["filterType"] == "LOT_SIZE":
                self.step_size = float(f["stepSize"])
                self.quantity_precision = self._precision_from_step(self.step_size)
                self.min_qty = float(f["minQty"])
                self.max_qty = float(f["maxQty"])
            elif f["filterType"] == "NOTIONAL":
                self.min_notional = float(f.get("minNotional", 0))
            elif f["filterType"] == "MIN_NOTIONAL":
                self.min_notional = float(f.get("minNotional", 0))
            elif f["filterType"] == "PERCENT_PRICE_BY_SIDE":
                self.bid_multiplier_down = float(f.get("bidMultiplierDown", 0))
                self.ask_multiplier_up = float(f.get("askMultiplierUp", 0))

    @staticmethod
    def _precision_from_step(step: float) -> int:
        if step >= 1.0:
            return 0
        return max(0, int(round(-math.log10(step))))

    def format_price(self, price: float) -> str:
        adjusted = self._round_down(price, self.tick_size)
        return f"{adjusted:.{self.price_precision}f}"

    def format_quantity(self, qty: float) -> str:
        adjusted = self._round_down(qty, self.step_size)
        return f"{adjusted:.{self.quantity_precision}f}"

    def quantity_for_spend(self, spend_amount: float, price: float) -> float:
        """Calculate max quantity you can buy for a given spend amount at a given price."""
        raw_qty = spend_amount / price
        return self._round_down(raw_qty, self.step_size)

    def validate_order(self, price: float, quantity: float, current_price: float = 0, side: str = "BUY") -> tuple[bool, str]:
        if quantity < self.min_qty:
            return False, f"Quantity {quantity} below min {self.min_qty}"
        if quantity > self.max_qty:
            return False, f"Quantity {quantity} above max {self.max_qty}"
        notional = price * quantity
        if notional < self.min_notional:
            return False, f"Notional {notional:.4f} below min {self.min_notional}"
        # PERCENT_PRICE_BY_SIDE check
        if current_price > 0 and self.bid_multiplier_down > 0:
            if side == "BUY":
                min_price = current_price * self.bid_multiplier_down
                if price < min_price:
                    return False, f"Price {price:.8f} below bid limit {min_price:.8f} (PERCENT_PRICE_BY_SIDE)"
            elif side == "SELL" and self.ask_multiplier_up > 0:
                max_price = current_price * self.ask_multiplier_up
                if price > max_price:
                    return False, f"Price {price:.8f} above ask limit {max_price:.8f} (PERCENT_PRICE_BY_SIDE)"
        return True, "OK"

    @staticmethod
    def _round_down(value: float, step: float) -> float:
        if step <= 0:
            return value
        return math.floor(value / step) * step

    def __repr__(self):
        return (
            f"SymbolInfo({self.symbol}: tick={self.tick_size}, step={self.step_size}, "
            f"min_qty={self.min_qty}, min_notional={self.min_notional})"
        )

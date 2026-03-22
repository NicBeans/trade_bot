"""Scalp-optimized coin screener — ranks pairs by short-term volatility."""

import logging
import math

from exchange.binance_adapter import BinanceAdapter

logger = logging.getLogger(__name__)


class ScalpScreener:
    def __init__(self, exchange: BinanceAdapter, min_volume_usd: float = 1_000_000, trade_capital: float = 5.0):
        self.exchange = exchange
        self.min_volume_usd = min_volume_usd
        self.trade_capital = trade_capital

    async def screen(
        self,
        quote_asset: str = "USDT",
        top_n: int = 5,
        exclude_symbols: list[str] | None = None,
    ) -> list[dict]:
        """Screen pairs for scalping suitability. Prioritizes high volatility and volume."""
        logger.info("Scalp screening %s pairs (trade capital=$%.2f)...", quote_asset, self.trade_capital)
        exclude = set(exclude_symbols or [])

        tickers = await self.exchange.get_all_tickers()
        symbol_infos = await self.exchange.get_all_symbol_info()
        candidates = []

        for t in tickers:
            symbol = t["symbol"]
            if not symbol.endswith(quote_asset) or symbol in exclude:
                continue

            try:
                price = float(t["lastPrice"])
                if price <= 0:
                    continue

                high = float(t["highPrice"])
                low = float(t["lowPrice"])
                volume = float(t["quoteVolume"])
                count = int(t["count"])

                if volume < self.min_volume_usd:
                    continue

                # Filter: trade capital must exceed min notional
                sinfo = symbol_infos.get(symbol)
                if sinfo:
                    min_notional = 0.0
                    for f in sinfo.get("filters", []):
                        if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                            min_notional = float(f.get("minNotional", 0))
                            break
                    if min_notional > 0 and self.trade_capital < min_notional:
                        continue

                volatility_pct = ((high - low) / price) * 100

                # Scalping wants HIGH volatility — more movement = more opportunities
                # Weight: 50% volatility, 30% volume, 20% trade count
                vol_score = min(50, volatility_pct * 5)  # 10% daily vol = max score
                volume_score = min(30, max(0, (math.log10(max(volume, 1)) - 6) * 10))
                count_score = min(20, count / 10000)

                score = round(vol_score + volume_score + count_score, 1)

                candidates.append({
                    "symbol": symbol,
                    "price": price,
                    "volume_24h": volume,
                    "volatility_pct": round(volatility_pct, 2),
                    "trade_count": count,
                    "score": score,
                })
            except (ValueError, KeyError, ZeroDivisionError):
                continue

        candidates.sort(key=lambda c: c["score"], reverse=True)
        top = candidates[:top_n]

        for i, c in enumerate(top):
            logger.info("  Scalp #%d %s — score: %.1f | vol: $%.0f | volatility: %.1f%%",
                         i + 1, c["symbol"], c["score"], c["volume_24h"], c["volatility_pct"])

        return top

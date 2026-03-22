"""Coin screener — ranks trading pairs by suitability for grid trading."""

import logging
from dataclasses import dataclass

from exchange.binance_adapter import BinanceAdapter

logger = logging.getLogger(__name__)


@dataclass
class CoinCandidate:
    symbol: str
    price: float
    volume_24h: float           # Quote volume in USDT
    volatility_pct: float       # (high - low) / price * 100
    spread_pct: float           # (ask - bid) / price * 100
    price_change_pct: float
    trade_count: int
    score: float = 0.0
    reason: str = ""


class CoinScreener:
    # Ideal volatility range for grid trading (daily %)
    IDEAL_VOL_MIN = 2.0
    IDEAL_VOL_MAX = 8.0
    IDEAL_VOL_MID = 5.0

    def __init__(
        self,
        exchange: BinanceAdapter,
        min_volume_usd: float = 1_000_000,
        min_capital: float = 20.0,
    ):
        self.exchange = exchange
        self.min_volume_usd = min_volume_usd
        self.min_capital = min_capital

    async def screen(
        self,
        quote_asset: str = "USDT",
        top_n: int = 5,
        num_grid_levels: int = 10,
    ) -> list[CoinCandidate]:
        """Screen and rank pairs. Returns top N candidates."""
        logger.info("Screening %s pairs (capital=$%.2f, %d levels)...", quote_asset, self.min_capital, num_grid_levels)

        tickers = await self.exchange.get_all_tickers()

        # Fetch exchange info for min notional filtering
        symbol_infos = await self.exchange.get_all_symbol_info()

        capital_per_level = self.min_capital / num_grid_levels
        candidates = []

        for t in tickers:
            symbol = t["symbol"]
            if not symbol.endswith(quote_asset):
                continue

            try:
                price = float(t["lastPrice"])
                if price <= 0:
                    continue

                high = float(t["highPrice"])
                low = float(t["lowPrice"])
                volume = float(t["quoteVolume"])
                bid = float(t["bidPrice"])
                ask = float(t["askPrice"])
                change_pct = float(t["priceChangePercent"])
                count = int(t["count"])

                # Filter: minimum volume
                if volume < self.min_volume_usd:
                    continue

                # Filter: must have bid/ask (liquid market)
                if bid <= 0 or ask <= 0:
                    continue

                volatility_pct = ((high - low) / price) * 100 if price > 0 else 0
                spread_pct = ((ask - bid) / price) * 100 if price > 0 else 0

                # Filter: need some volatility for grid trading
                if volatility_pct < 0.5:
                    continue

                # Filter: capital per level must exceed min notional
                sinfo = symbol_infos.get(symbol)
                if sinfo:
                    min_notional = 0.0
                    for f in sinfo.get("filters", []):
                        if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                            min_notional = float(f.get("minNotional", 0))
                            break
                    if min_notional > 0 and capital_per_level < min_notional:
                        continue

                candidates.append(CoinCandidate(
                    symbol=symbol,
                    price=price,
                    volume_24h=volume,
                    volatility_pct=round(volatility_pct, 2),
                    spread_pct=round(spread_pct, 4),
                    price_change_pct=round(change_pct, 2),
                    trade_count=count,
                ))
            except (ValueError, KeyError, ZeroDivisionError):
                continue

        # Score candidates
        for c in candidates:
            c.score = self._calculate_score(c)
            c.reason = self._explain_score(c)

        # Sort by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)

        top = candidates[:top_n]
        for i, c in enumerate(top):
            logger.info(
                "  #%d %s — score: %.1f | vol: $%.0f | volatility: %.1f%% | spread: %.4f%% | %s",
                i + 1, c.symbol, c.score, c.volume_24h, c.volatility_pct, c.spread_pct, c.reason,
            )

        return top

    def _calculate_score(self, c: CoinCandidate) -> float:
        """Score a candidate 0-100. Higher = better for grid trading."""
        score = 0.0

        # Volume score (0-30): higher volume = better liquidity
        # Log scale — $1M = 0, $100M+ = 30
        import math
        vol_log = math.log10(max(c.volume_24h, 1))
        vol_score = min(30, max(0, (vol_log - 6) * 10))  # 6 = log10(1M)
        score += vol_score

        # Volatility score (0-35): sweet spot around 2-8%
        if self.IDEAL_VOL_MIN <= c.volatility_pct <= self.IDEAL_VOL_MAX:
            # Peak score at midpoint
            dist = abs(c.volatility_pct - self.IDEAL_VOL_MID)
            vol_range = (self.IDEAL_VOL_MAX - self.IDEAL_VOL_MIN) / 2
            vol_score = 35 * (1 - dist / vol_range)
        elif c.volatility_pct < self.IDEAL_VOL_MIN:
            vol_score = 35 * (c.volatility_pct / self.IDEAL_VOL_MIN) * 0.5
        else:
            # Too volatile — penalize
            excess = c.volatility_pct - self.IDEAL_VOL_MAX
            vol_score = max(0, 35 * (1 - excess / 10))
        score += vol_score

        # Spread score (0-20): tighter spread = better
        # <0.01% = perfect, >0.5% = bad
        if c.spread_pct <= 0.01:
            spread_score = 20
        elif c.spread_pct >= 0.5:
            spread_score = 0
        else:
            spread_score = 20 * (1 - c.spread_pct / 0.5)
        score += spread_score

        # Trade count score (0-15): more trades = more active market
        count_score = min(15, c.trade_count / 10000)
        score += count_score

        return round(score, 1)

    def _explain_score(self, c: CoinCandidate) -> str:
        """One-line explanation of the score."""
        parts = []
        if c.volatility_pct < self.IDEAL_VOL_MIN:
            parts.append("low volatility")
        elif c.volatility_pct > self.IDEAL_VOL_MAX:
            parts.append("high volatility")
        else:
            parts.append("good volatility")

        if c.spread_pct < 0.05:
            parts.append("tight spread")
        elif c.spread_pct > 0.2:
            parts.append("wide spread")

        if c.volume_24h > 50_000_000:
            parts.append("high volume")
        elif c.volume_24h < 5_000_000:
            parts.append("low volume")

        return ", ".join(parts)

    def format_results_discord(self, candidates: list[CoinCandidate]) -> str:
        """Format screening results for Discord."""
        if not candidates:
            return "**Coin Screener** — No suitable pairs found."

        lines = ["**Coin Screener Results**\n"]
        for i, c in enumerate(candidates):
            emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"][i] if i < 5 else f"#{i+1}"
            lines.append(
                f"{emoji} **{c.symbol}** — Score: {c.score}/100\n"
                f"   Price: ${c.price:.4f} | Vol: ${c.volume_24h:,.0f} | "
                f"Volatility: {c.volatility_pct}% | Spread: {c.spread_pct}%\n"
                f"   _{c.reason}_"
            )
        lines.append("\nReply with the number (1-5) to select a pair.")
        return "\n".join(lines)

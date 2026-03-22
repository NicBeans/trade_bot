"""Scalp-optimized coin screener — ranks pairs by actual short-term spike frequency."""

import asyncio
import logging
import math
import time

from exchange.binance_adapter import BinanceAdapter

logger = logging.getLogger(__name__)

CACHE_TTL = 1800  # 30 minutes
PRE_FILTER_TOP_N = 20
MIN_TRADE_COUNT = 10_000
MAX_SPREAD_PCT = 0.1


class ScalpScreener:
    def __init__(
        self,
        exchange: BinanceAdapter,
        min_volume_usd: float = 5_000_000,
        trade_capital: float = 5.0,
        trigger_pct: float = 0.5,
    ):
        self.exchange = exchange
        self.min_volume_usd = min_volume_usd
        self.trade_capital = trade_capital
        self.trigger_pct = trigger_pct / 100  # convert to decimal
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    async def screen(
        self,
        quote_asset: str = "USDT",
        top_n: int = 5,
    ) -> list[dict]:
        """Screen pairs for scalping. Two-phase: pre-filter by ticker, then deep scan klines."""

        # Check cache
        cache_key = f"{quote_asset}:{self.trigger_pct}"
        if cache_key in self._cache:
            cached_time, cached_results = self._cache[cache_key]
            age = time.time() - cached_time
            if age < CACHE_TTL:
                logger.info("Scalp screener: using cached results (%.0fs old)", age)
                return cached_results[:top_n]

        logger.info(
            "Scalp screening %s pairs (vol>$%.0f, trigger>=%.2f%%, capital=$%.5f)...",
            quote_asset, self.min_volume_usd, self.trigger_pct * 100, self.trade_capital,
        )

        # Phase 1: Pre-filter using 24h ticker data
        shortlist = await self._pre_filter(quote_asset)
        if not shortlist:
            logger.warning("Scalp screener: no candidates passed pre-filter")
            return []

        logger.info("Scalp pre-filter: %d candidates for deep scan", len(shortlist))

        # Phase 2: Deep scan klines for shortlisted coins
        results = await self._deep_scan(shortlist)

        # Filter out zero-spike coins
        results = [r for r in results if r["spike_count"] > 0]
        results.sort(key=lambda r: r["score"], reverse=True)

        # Cache results
        self._cache[cache_key] = (time.time(), results)

        top = results[:top_n]
        for i, c in enumerate(top):
            logger.info(
                "  Scalp #%d %s — score: %.1f | spikes: %d (avg %.2f%%) | vol: $%.0f | trades: %d",
                i + 1, c["symbol"], c["score"], c["spike_count"],
                c["avg_spike_pct"], c["volume_24h"], c["trade_count"],
            )

        if not top:
            logger.warning("Scalp screener: no coins had spikes >= %.2f%% in recent klines", self.trigger_pct * 100)

        return top

    async def _pre_filter(self, quote_asset: str) -> list[dict]:
        """Phase 1: Fast filter using 24h ticker data. Returns top candidates by volume."""
        tickers = await self.exchange.get_all_tickers()
        symbol_infos = await self.exchange.get_all_symbol_info()

        candidates = []
        for t in tickers:
            symbol = t["symbol"]
            if not symbol.endswith(quote_asset):
                continue

            try:
                price = float(t["lastPrice"])
                if price <= 0:
                    continue

                volume = float(t["quoteVolume"])
                bid = float(t["bidPrice"])
                ask = float(t["askPrice"])
                count = int(t["count"])

                # Volume filter
                if volume < self.min_volume_usd:
                    continue

                # Trade count filter (active market)
                if count < MIN_TRADE_COUNT:
                    continue

                # Spread filter (liquid, not gapped)
                if bid <= 0 or ask <= 0:
                    continue
                spread_pct = ((ask - bid) / price) * 100
                if spread_pct > MAX_SPREAD_PCT:
                    continue

                # Min notional filter
                sinfo = symbol_infos.get(symbol)
                if sinfo:
                    min_notional = 0.0
                    for f in sinfo.get("filters", []):
                        if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                            min_notional = float(f.get("minNotional", 0))
                            break
                    if min_notional > 0 and self.trade_capital < min_notional:
                        continue

                candidates.append({
                    "symbol": symbol,
                    "price": price,
                    "volume_24h": volume,
                    "trade_count": count,
                    "spread_pct": round(spread_pct, 4),
                })
            except (ValueError, KeyError, ZeroDivisionError):
                continue

        # Sort by volume, take top N for deep scan
        candidates.sort(key=lambda c: c["volume_24h"], reverse=True)
        return candidates[:PRE_FILTER_TOP_N]

    async def _deep_scan(self, shortlist: list[dict]) -> list[dict]:
        """Phase 2: Fetch klines and measure actual spike frequency."""
        tasks = [self._analyze_symbol(c) for c in shortlist]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scored = []
        for r in results:
            if isinstance(r, Exception):
                continue
            if r is not None:
                scored.append(r)
        return scored

    async def _analyze_symbol(self, candidate: dict) -> dict | None:
        """Fetch klines for a symbol and count spikes."""
        symbol = candidate["symbol"]

        # Try 8h (480 candles), fallback to 4h (240), then 1h (60)
        klines = None
        for limit in (480, 240, 60):
            try:
                klines = await self.exchange.get_klines(symbol=symbol, interval="1m", limit=limit)
                if klines and len(klines) > 10:
                    break
            except Exception:
                continue

        if not klines or len(klines) < 10:
            return None

        # Count minute-to-minute moves >= trigger threshold
        spike_count = 0
        spike_magnitudes = []
        total_candles = len(klines) - 1
        recent_cutoff = len(klines) * 0.75  # last 25% of data = "recent"

        for i in range(total_candles):
            close_prev = float(klines[i][4])
            close_now = float(klines[i + 1][4])

            if close_prev <= 0:
                continue

            pct_change = abs(close_now - close_prev) / close_prev

            if pct_change >= self.trigger_pct:
                # Weight recent spikes double
                weight = 2 if i >= recent_cutoff else 1
                spike_count += weight
                spike_magnitudes.append(pct_change * 100)

        avg_spike = sum(spike_magnitudes) / len(spike_magnitudes) if spike_magnitudes else 0
        hours_scanned = total_candles / 60

        # Score components (0-100)
        # Spike frequency: 40% — spikes per hour, capped at ~6/hr = max
        spikes_per_hour = spike_count / hours_scanned if hours_scanned > 0 else 0
        freq_score = min(40, spikes_per_hour * (40 / 6))

        # Spike magnitude: 20% — bigger average spikes = better
        mag_score = min(20, avg_spike * 10)

        # Volume: 20% — log scaled
        vol_log = math.log10(max(candidate["volume_24h"], 1))
        vol_score = min(20, max(0, (vol_log - 6) * 5))

        # Liquidity (trade count + spread): 20%
        count_score = min(10, candidate["trade_count"] / 50_000 * 10)
        spread_score = min(10, (1 - candidate["spread_pct"] / MAX_SPREAD_PCT) * 10)
        liq_score = count_score + spread_score

        score = round(freq_score + mag_score + vol_score + liq_score, 1)

        return {
            "symbol": symbol,
            "price": candidate["price"],
            "volume_24h": candidate["volume_24h"],
            "trade_count": candidate["trade_count"],
            "spread_pct": candidate["spread_pct"],
            "spike_count": len(spike_magnitudes),  # raw count, not weighted
            "spikes_per_hour": round(spikes_per_hour, 1),
            "avg_spike_pct": round(avg_spike, 2),
            "hours_scanned": round(hours_scanned, 1),
            "score": score,
        }

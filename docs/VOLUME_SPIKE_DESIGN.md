# Volume Spike Scalping - Design Document

## Understanding Summary

- **What:** Add `volume_spike` as a third scalp mode in the existing scalp engine
- **Why:** Price-based triggers (momentum/mean_reversion) fire too rarely in quiet markets. Volume spikes detect moves before they show up in price.
- **Who:** Same bot, same user, same capital
- **Key constraints:** Reuses existing scalp engine state machine, exits, dashboard, notifications. Binance spot only, $10-20 capital.
- **Non-goals:** Not a separate engine, not removing old modes (deprecated but kept)

## Assumptions

- Volume baseline seeded from 60 x 1m klines at startup, then rolling average from live trades
- 10-second volume buckets compared against baseline
- Spike = bucket volume >= 3x baseline (configurable)
- Direction from buy/sell ratio, 65% threshold (configurable)
- Market buy on buy-dominant spikes, skip sell-dominant (can't short spot)
- Same TP/SL as current scalper, shorter timeout (45s)
- 5s cooldown after successful trade, 20s after false signal/timeout
- Trade stream isBuyerMaker flag determines buy vs sell volume

## Volume Tracker

### Initialization
1. Fetch 60 x 1m klines
2. Calculate average volume per 10-second bucket (candle volume / 6)
3. Seed rolling deque with 360 buckets (60 min worth)

### Live Tracking
- Incoming trades grouped into 10-second buckets
- Each bucket tracks: total_volume, buy_volume, sell_volume
- On bucket completion: append to rolling deque, drop oldest if > 360

### Spike Detection (every 10s on bucket completion)
1. `bucket_volume >= baseline_avg * spike_multiplier` → spike detected
2. `buy_volume / total_volume >= direction_threshold` → buy signal
3. Enter position via existing `_enter_position()`

## Config

```env
SCALP_MODE=volume_spike
SCALP_VOLUME_MULTIPLIER=3.0
SCALP_VOLUME_DIRECTION_PCT=65
SCALP_VOLUME_TIMEOUT=45
SCALP_FALSE_SIGNAL_COOLDOWN=20
```

## Changes

| File | Change |
|---|---|
| `core/scalp_engine.py` | Add VolumeTracker class, volume_spike mode routing, volume-specific timeout/cooldown |
| `config/settings.py` | Add 4 new config vars |
| `config/.env.example` | Add volume spike defaults |

## Decision Log

| # | Decision | Alternatives | Reason |
|---|---|---|---|
| 1 | Third scalp mode | Separate engine | Reuses existing infrastructure |
| 2 | Deprecate old modes | Remove or keep equal | Volume spike is better, old modes kept for testing |
| 3 | Seed from klines + rolling | Rolling only, klines only | Immediate readiness with accurate data |
| 4 | 10s buckets | 5s (noisy), 30s (slow) | Balance responsiveness and noise |
| 5 | 3x multiplier, configurable | Fixed values | Start sensitive, tune up |
| 6 | 65% direction threshold | 60% (weak), 70% (strict) | Meaningful conviction |
| 7 | 45s timeout | 120s (too long), 30s (too short) | Volume moves happen fast or not at all |
| 8 | 20s false signal cooldown | 5s (same), 30s (too long) | Avoid re-entering on noise |

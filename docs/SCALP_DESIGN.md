# Scalping Strategy - Design Document

## Understanding Summary

- **What:** Add a swappable scalping strategy (momentum + mean reversion modes) running alongside grid trading on a separate pair
- **Why:** Grid trading is slow to show results. Scalping gives fast, visible profit/loss within seconds to minutes at higher risk
- **Who:** Same user, same bot, same infrastructure
- **Key constraints:** Must integrate into existing architecture, dedicated async task for speed, per-strategy USD capital caps, configurable triggers and exits
- **Non-goals:** Not replacing grid trading, not running both scalp modes simultaneously, not doing arbitrage or indicator-based strategies

## Assumptions

- Scalper uses its own BinanceAdapter instance and WebSocket connections
- Market orders for entry/exit (speed over price)
- Binance spot only — no shorting, so mean reversion only works on dip-buying
- Single scalp pair at a time, selected by volatility-optimized screener
- 5-second cooldown between trades to avoid re-entering on same move
- Scalp trades persisted to same PostgreSQL via TradeRepository

## Capital Model

```env
GRID_CAPITAL=10.0    # USD cap for grid trading. 0 = disabled.
SCALP_CAPITAL=10.0   # USD cap for scalping. 0 = disabled.
```

Replaces the old `TRADING_CAPITAL` + percentage split. Each strategy independently capped.

## Scalp Engine

### Two Modes (Swappable via `SCALP_MODE`)

**Momentum:** Detects sudden price move, enters in same direction (rides the wave).
**Mean Reversion:** Detects sudden price move, enters opposite direction (bets on snapback).

### State Machine

```
SCANNING → ENTERING → IN_POSITION → EXITING → (cooldown) → SCANNING
```

### Configurable Defaults

| Setting | Env Var | Default |
|---|---|---|
| Scalp mode | `SCALP_MODE` | `momentum` |
| Trigger threshold | `SCALP_TRIGGER_PCT` | `0.5` (%) |
| Trigger window | `SCALP_TRIGGER_WINDOW` | `30` (seconds) |
| Take profit | `SCALP_TP_PCT` | `0.4` (%) |
| Stop loss | `SCALP_SL_PCT` | `0.3` (%) |
| Time limit | `SCALP_TIME_LIMIT` | `120` (seconds) |
| Max capital per trade | `SCALP_TRADE_PCT` | `50` (% of scalp allocation) |
| Cooldown | `SCALP_COOLDOWN` | `5` (seconds) |
| Symbol override | `SCALP_SYMBOL` | _(empty, use screener)_ |

### Entry Logic

- Rolling price window: deque of `(timestamp, price)` for last N seconds
- On each trade tick: calculate `pct_change = (latest - oldest) / oldest`
- Momentum: if `pct_change >= threshold` → market buy; if `pct_change <= -threshold` → market sell existing position
- Mean reversion: if `pct_change <= -threshold` → market buy (buy the dip); if `pct_change >= threshold` and holding → market sell

### Exit Logic (first condition wins)

1. Take profit: price ≥ entry * (1 + TP%)
2. Stop loss: price ≤ entry * (1 - SL%)
3. Time limit: elapsed ≥ max seconds → market sell

### Tracking

- `total_trades`, `wins`, `losses`, `win_rate`, `total_profit`, `avg_duration`

## Architecture

### Runtime

```
bot.start()
  ├── grid task (BinanceAdapter #1 + WebSocket) → pair A
  └── scalp task (BinanceAdapter #2 + WebSocket) → pair B
```

### Files

| File | Change |
|---|---|
| `core/scalp_engine.py` | NEW — state machine, price window, entry/exit |
| `core/scalp_screener.py` | NEW — volatility-optimized pair selection |
| `core/bot.py` | MODIFIED — spawn scalp task, capital split |
| `config/settings.py` | MODIFIED — scalp config vars |
| `config/.env.example` | MODIFIED — scalp defaults |
| `dashboard/app.py` | MODIFIED — new page route |
| `dashboard/templates/index.html` | MODIFIED — scalp summary card |
| `dashboard/templates/scalping.html` | NEW — real-time scalp page |
| `dashboard/routes/partials.py` | MODIFIED — scalp partials |
| `notifications/discord.py` | MODIFIED — scalp trade notifications |

### Shared Resources (with locking)

- `TradeRepository` — async-safe by design (separate sessions)
- `DiscordNotifier` — aiohttp session is thread-safe
- Capital tracking — each strategy has its own allocation, no shared risk manager needed

## Dashboard

### Overview page — new card

Scalp mode, pair, state, win rate, scalp P&L

### New scalping page (auto-refresh 1-2s)

1. **Live Status** — state, pair, price, entry price, unrealised P&L, time in trade
2. **Trade Log** — last 20 trades: time, direction, entry, exit, profit, duration, result
3. **Stats** — totals, win rate, avg profit, avg duration

### API Partials

- `/api/partials/scalp-summary`
- `/api/partials/scalp-status`
- `/api/partials/scalp-log`
- `/api/partials/scalp-stats`

## Decision Log

| # | Decision | Alternatives | Reason |
|---|---|---|---|
| 1 | Side-by-side different pairs | Either/or, same pair | Run both simultaneously |
| 2 | Seconds-to-minutes scalping | Longer timeframes | Fast visible results |
| 3 | Momentum + mean reversion, swappable | Single mode, indicators, arbitrage | Best speed/risk fit |
| 4 | Per-strategy USD cap, 0 disables | Percentage split | More intuitive, direct control |
| 5 | 50% scalp capital per trade default | All-in, fixed small % | Allows 2 positions |
| 6 | Fixed TP/SL + time limit exits | Trailing stop only | Time limit prevents dead trades |
| 7 | Volatility-optimized screener for scalp | Same screener, manual | Different pair characteristics needed |
| 8 | Dedicated async task with own WebSocket | Shared loop, separate process | Speed without over-engineering |
| 9 | Parallel module approach | Separate process, plugin system | Same patterns, minimal infrastructure |
| 10 | Market orders for scalp | Limit orders | Speed critical |
| 11 | Moderate trigger default (0.5%/30s) | Hair trigger, conservative | Balance frequency and quality |
| 12 | New dashboard page + overview card | Overview only | User requested real-time scalp view |
| 13 | Per-strategy USD cap, 0 disables | Percentage split, single cap | More intuitive, cleanly disables either |

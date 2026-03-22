# Trade Bot - Design Document

## Understanding Summary

- **What:** A Python-based crypto grid trading bot running on Binance
- **Why:** Continuously generate small profits from price oscillation, starting with $20 capital
- **Who:** South Africa-based trader wanting an automated but controllable system
- **Core features:** Grid trading, semi-automatic coin selection, configurable risk presets with overrides, web dashboard (FastAPI + HTMX), Discord notifications, supervised/autonomous mode toggle
- **Deployment:** Local-first via Docker Compose, designed for migration to Oracle Cloud free tier (ARM)
- **Data:** PostgreSQL
- **Testing:** Binance testnet for paper trading before going live

## Assumptions

- Python 3.11+
- User has (or will create) a Binance account with API access
- Discord webhook for notifications (no bot account needed)
- Dashboard is functional, not polished — utility over aesthetics
- Single trading pair at a time (no simultaneous multi-grid)
- BNB fee discount will be used (0.075% vs 0.1%)
- USDT pairs as default quote asset

---

## Architecture

### Overview

Single async Python application (modular monolith) using `asyncio`. All components run in one process, communicating via shared state and the database.

### Project Structure

```
trade_bot/
├── config/
│   ├── settings.py          # App config (env vars, defaults)
│   ├── presets.py            # Risk presets (conservative, moderate, aggressive)
│   └── .env.example          # Template for secrets
├── core/
│   ├── bot.py                # Main bot loop, orchestrates everything
│   ├── grid_engine.py        # Grid calculation, level management
│   ├── coin_screener.py      # Pair analysis and ranking
│   └── risk_manager.py       # Pre-trade validation, stop-loss logic
├── exchange/
│   ├── binance_adapter.py    # Binance API wrapper (testnet/mainnet)
│   └── models.py             # Order, trade, ticker data models
├── notifications/
│   └── discord.py            # Discord webhook sender
├── dashboard/
│   ├── app.py                # FastAPI app
│   ├── routes/               # API endpoints
│   └── templates/            # HTMX-powered HTML frontend
├── db/
│   ├── database.py           # PostgreSQL connection (async SQLAlchemy)
│   ├── models.py             # DB models (trades, grid state, P&L)
│   └── migrations/           # Alembic migrations
├── main.py                   # Entry point
├── requirements.txt
├── Dockerfile
└── docker-compose.yml        # Bot + PostgreSQL + pgAdmin
```

### Key Design Principle

The `exchange/` module is fully isolated. All Binance-specific code lives there. Testnet vs mainnet is a config toggle — different base URL and API keys, same interface.

---

## Grid Engine

### Setup

1. Define: **upper price**, **lower price**, and **number of grid levels**
2. Engine divides the range into equal intervals
3. Places **buy orders** below current price, **sell orders** above
4. Example: $20 capital, 10 levels, coin at $1.00, range $0.90-$1.10 = $0.02 per step, ~$2 per level

### Execution Cycle

1. Price feed via Binance WebSocket (real-time, no polling)
2. Price crosses grid level downward → **buy** at that level
3. Price crosses grid level upward → **sell** at the level above the buy
4. Each buy→sell pair captures grid spacing minus fees as profit
5. Bot tracks level state: "holding" (bought, waiting to sell) vs "empty" (waiting to buy)

### Grid Reset Conditions

- Price exits grid range entirely
- **Supervised mode:** pause, notify via Discord, wait for approval
- **Autonomous mode:** auto-recalculate grid around current price after configurable cooldown

### Order Approach

- Limit orders preferred (maker fees are lower on Binance)
- Open orders monitored via WebSocket user data stream

---

## Coin Screener

Runs periodically (default: every 6 hours) or on-demand via dashboard.

### Screening Criteria

1. **24h Volume** — minimum threshold (e.g., $1M daily) to filter illiquid pairs
2. **Volatility score** — recent price range as % of price. Sweet spot: ~2-8% daily range
3. **Spread** — tighter bid/ask = less slippage
4. **Min order size** — must allow orders small enough for available capital

### Output

- Ranked top 5 candidates with scores
- Sent to Discord + displayed on dashboard
- **Supervised mode:** waits for user selection
- **Autonomous mode:** selects top-ranked pair

### Defaults

- Quote asset: USDT (configurable)
- Pairs filtered by Binance trading rules (LOT_SIZE, MIN_NOTIONAL)

---

## Risk Manager & Presets

### Built-in Presets

| Setting                  | Conservative | Moderate       | Aggressive     |
|--------------------------|-------------|----------------|----------------|
| Grid levels              | 5           | 10             | 20             |
| Grid range (% from price)| ±3%        | ±5%            | ±10%           |
| Stop-loss                | -5% capital | -10% capital   | None           |
| Max capital per level    | 15%         | 10%            | 5%             |
| Grid reset cooldown      | 30 min      | 10 min         | 2 min          |
| Pause on range exit      | Yes         | No (auto-reset)| No (auto-reset)|

All values are overridable per-session or via config.

### Pre-Trade Checks (Always Active)

- Reject if order exceeds max capital per level
- Reject if remaining balance below Binance minimum order size
- Reject if cumulative loss hits stop-loss threshold
- Every rejection logged with reason

### Stop-Loss Behavior

1. Cancel all open orders
2. Notify via Discord
3. **Supervised mode:** halt and wait for user
4. **Autonomous mode:** halt for configurable cooldown, re-screen coins, restart

---

## Dashboard

### Tech Stack

- **Backend:** FastAPI (async, serves API + templates)
- **Frontend:** HTMX + vanilla JS (no heavy framework)
- **Runs in the same process as the bot**

### Pages

| Page            | Purpose                                                       |
|-----------------|---------------------------------------------------------------|
| Overview        | Current pair, grid visualization, P&L, balance                |
| Trade History   | Table of all trades with timestamps, pair, side, price, profit|
| Coin Screener   | Ranked candidates, button to switch pair (supervised mode)    |
| Settings        | Risk parameters, mode toggle, testnet/mainnet switch          |
| Bot Controls    | Start, stop, pause, force grid reset                          |

---

## Discord Notifications

Sent via webhook (no bot account required).

### Events

| Event                     | Frequency        |
|---------------------------|------------------|
| Trade executed (buy/sell) | Every trade      |
| Grid level completed      | Every completion |
| Grid reset triggered      | On occurrence    |
| Stop-loss hit             | On occurrence    |
| Bot error/crash           | On occurrence    |
| Daily P&L summary         | Once daily       |
| Coin screener results     | On screening     |
| Approval request          | On occurrence    |

### Supervised Mode Approvals

Bot sends Discord message with action details. User reacts with checkmark or X. Bot watches for the reaction via a lightweight Discord listener.

---

## Testnet / Mainnet Switching

- Single env var: `TRADING_MODE=testnet` or `TRADING_MODE=mainnet`
- Each mode has its own API keys, base URL, and database schema
- Dashboard shows clear **TESTNET** or **LIVE** badge
- Switching requires bot restart (safety — no accidental hot-swap)

---

## Deployment

### Local (Docker Compose)

```yaml
services:
  bot:       # Python app (bot + dashboard)
  postgres:  # Database
  pgadmin:   # Optional DB admin UI
```

### OCI Migration Path

1. Push Docker image to GitHub Container Registry (free)
2. Pull and run on OCI ARM free-tier instance
3. PostgreSQL on the same instance (sufficient for this scale)
4. Auto-restart via systemd or Watchtower
5. Crash notifications via Discord

---

## Decision Log

| # | Decision | Alternatives Considered | Reason |
|---|----------|------------------------|--------|
| 1 | Grid trading strategy | DCA+scalping, arbitrage, momentum | Best balance of simplicity, frequency, and profitability for small capital |
| 2 | Centralized exchange (Binance) | DEX, KuCoin, Kraken, Bybit | Best fees, testnet support, liquidity, accessible from South Africa |
| 3 | Python | JS/TS, Rust, Go | Best library ecosystem for trading bots, fastest to prototype |
| 4 | Semi-auto coin selection | Manual, fully automatic | Bot does analysis, user retains control |
| 5 | Configurable risk with presets | Fixed conservative/aggressive only | Presets for quick start, full override for fine-tuning |
| 6 | Supervised/autonomous toggle | Fixed autonomy level | Build trust early with supervision, go hands-off later |
| 7 | Discord notifications | Telegram, both | User preference |
| 8 | FastAPI + HTMX dashboard | Flask, React SPA | Async aligns with bot architecture, HTMX keeps frontend simple |
| 9 | PostgreSQL | SQLite, SQLite→Postgres migration | Better for dashboard queries, works on OCI, no migration pain later |
| 10 | Modular async monolith | Microservices, Celery workers | Simplest to build, deploy, and reason about at this scale |
| 11 | Docker Compose from day one | Bare metal, venv only | Consistent local/production parity, easy OCI deployment |
| 12 | Testnet/mainnet via env var + restart | Hot-swap, separate deployments | Safety — prevents accidental live trading |

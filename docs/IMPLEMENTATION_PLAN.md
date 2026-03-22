# Implementation Plan

## Phase 1: Foundation
**Goal:** Runnable skeleton with config, database, and Binance connectivity.

1. Project scaffolding — directory structure, `requirements.txt`, `Dockerfile`, `docker-compose.yml`
2. Config system — `settings.py` with pydantic-settings, `.env.example`, `TRADING_MODE` toggle
3. Database setup — async SQLAlchemy models, Alembic migrations, `docker-compose` PostgreSQL
4. Binance adapter — connect to testnet, fetch account info, subscribe to WebSocket price stream
5. Verify: `docker-compose up` starts bot + postgres, bot connects to Binance testnet and logs price ticks

## Phase 2: Grid Engine
**Goal:** Bot places and manages grid orders on testnet.

1. Grid engine — calculate grid levels from upper/lower price + number of levels
2. Order placement — place limit buy/sell orders via Binance adapter
3. Order monitoring — WebSocket user data stream to track fills
4. Grid state management — track level states (empty/holding), persist to database
5. Grid reset logic — detect range exit, recalculate grid
6. Verify: bot runs a grid on testnet, executes buy/sell cycles, logs profit per completed level

## Phase 3: Risk Management
**Goal:** Pre-trade checks and stop-loss protection.

1. Risk presets — define conservative/moderate/aggressive in `presets.py`
2. Pre-trade validation — capital per level, minimum order, cumulative loss checks
3. Stop-loss handler — cancel orders, halt bot, notify
4. Settings override — allow runtime config overrides via env or config file
5. Verify: bot rejects orders that violate risk rules, halts on stop-loss trigger

## Phase 4: Coin Screener
**Goal:** Bot recommends trading pairs.

1. Data fetching — pull 24h ticker data for all USDT pairs from Binance
2. Scoring algorithm — rank by volume, volatility, spread, min order compatibility
3. Output — top 5 candidates with scores
4. Semi-auto flow — present candidates, wait for selection (in supervised mode)
5. Verify: screener returns sensible rankings, filters out unsuitable pairs

## Phase 5: Discord Notifications
**Goal:** Bot reports activity to Discord.

1. Webhook sender — generic Discord webhook client
2. Event hooks — wire up trade, grid reset, stop-loss, error events
3. Daily summary — scheduled P&L report
4. Supervised mode approvals — send action request, listen for reaction
5. Verify: all events produce Discord messages, approval flow works end-to-end

## Phase 6: Dashboard
**Goal:** Web UI for monitoring and control.

1. FastAPI app setup — mount alongside bot, serve templates
2. Overview page — grid visualization, P&L, balance (HTMX for live updates)
3. Trade history page — paginated table from database
4. Coin screener page — display rankings, pair switch button
5. Settings page — edit risk params, toggle supervised/autonomous, testnet/mainnet badge
6. Bot controls — start/stop/pause/reset endpoints + UI
7. Verify: dashboard reflects live bot state, controls work

## Phase 7: Polish & Deploy
**Goal:** Production-ready for OCI deployment.

1. Error handling — graceful crash recovery, auto-reconnect to WebSocket
2. Logging — structured logging with levels, file + console output
3. Health checks — endpoint for monitoring bot status
4. Dockerfile optimization — multi-stage build, slim image
5. OCI deployment docs — step-by-step guide for free-tier ARM instance
6. Verify: bot survives WebSocket disconnects, restarts cleanly, runs stable for 24h+ on testnet

---

## Key Libraries

| Library | Purpose |
|---------|---------|
| `python-binance` | Binance REST + WebSocket API |
| `sqlalchemy[asyncio]` + `asyncpg` | Async PostgreSQL ORM |
| `alembic` | Database migrations |
| `fastapi` + `uvicorn` | Dashboard web server |
| `jinja2` | HTML templating |
| `pydantic-settings` | Config management |
| `aiohttp` | Discord webhook HTTP client |
| `python-dotenv` | Env file loading |

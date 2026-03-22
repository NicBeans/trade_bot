# Trade Bot

A Python-based crypto trading bot for Binance with two strategies: **grid trading** for steady accumulation and **scalping** for fast, high-risk trades. Both run simultaneously on different pairs.

## Features

- **Grid Trading** — automated buy/sell at preset price intervals, profits from sideways oscillation
- **Scalping** — momentum or mean reversion mode, catches short-term spikes for quick trades
- **Smart Screeners** — grid screener ranks by volume/volatility/spread; scalp screener analyzes actual minute-to-minute spike frequency from kline data
- **Risk Management** — configurable presets (conservative/moderate/aggressive) with per-field overrides
- **Per-Strategy Capital** — set USD caps independently, set to 0 to disable either strategy
- **Supervised/Autonomous Modes** — toggle between manual approval and fully automated
- **Web Dashboard** — live grid visualization, real-time scalp status, P&L tracking, bot controls
- **Discord Notifications** — trade alerts, daily summaries, error reports
- **Testnet/Mainnet Toggle** — safe testing before going live

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose (for PostgreSQL)
- Binance account with API keys
- Discord webhook URL (optional)

### Setup

```bash
cd trade_bot

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/.env.example config/.env
# Edit config/.env with your API keys and settings

# Start PostgreSQL
docker compose up postgres -d

# Run the bot
python main.py
```

Dashboard available at `http://localhost:8080`

### Docker (Full Stack)

```bash
cp config/.env.example config/.env
# Edit config/.env

docker compose up -d
```

Starts the bot, PostgreSQL, and dashboard. Add `--profile tools` to include pgAdmin.

## Configuration

All config via environment variables in `config/.env`:

### General

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `testnet` | `testnet` or `mainnet` |
| `BOT_MODE` | `supervised` | `supervised` or `autonomous` |
| `RISK_PRESET` | `moderate` | `conservative`, `moderate`, `aggressive` |
| `BINANCE_TESTNET_API_KEY` | | Testnet API key |
| `BINANCE_TESTNET_API_SECRET` | | Testnet API secret |
| `BINANCE_MAINNET_API_KEY` | | Mainnet API key |
| `BINANCE_MAINNET_API_SECRET` | | Mainnet API secret |
| `DISCORD_WEBHOOK_URL` | | Discord webhook for notifications |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |

### Strategy Capital

| Variable | Default | Description |
|---|---|---|
| `GRID_CAPITAL` | `10.0` | USD allocated to grid trading. `0` = disabled |
| `SCALP_CAPITAL` | `10.0` | USD allocated to scalping. `0` = disabled |
| `TRADING_SYMBOL` | _(empty)_ | Override grid screener (e.g., `DOGEUSDT`) |
| `SCALP_SYMBOL` | _(empty)_ | Override scalp screener (e.g., `ETHUSDT`) |

### Scalp Settings

| Variable | Default | Description |
|---|---|---|
| `SCALP_MODE` | `momentum` | `momentum` or `mean_reversion` |
| `SCALP_TRIGGER_PCT` | `0.5` | % price move to trigger entry |
| `SCALP_TRIGGER_WINDOW` | `30` | Seconds to measure the move over |
| `SCALP_TP_PCT` | `0.4` | Take profit % |
| `SCALP_SL_PCT` | `0.3` | Stop loss % |
| `SCALP_TIME_LIMIT` | `120` | Max seconds in a position |
| `SCALP_TRADE_PCT` | `50` | % of scalp capital per trade |
| `SCALP_COOLDOWN` | `5` | Seconds between trades |
| `SCALP_MIN_VOLUME` | `5000000` | Min 24h volume for scalp candidates |

### Grid Risk Overrides

Override individual preset values:

```
OVERRIDE_GRID_LEVELS=5
OVERRIDE_GRID_RANGE_PCT=0.03
OVERRIDE_STOP_LOSS_PCT=0.05
OVERRIDE_MAX_CAPITAL_PER_LEVEL_PCT=0.10
OVERRIDE_GRID_RESET_COOLDOWN_SECONDS=600
OVERRIDE_PAUSE_ON_RANGE_EXIT=false
```

## Architecture

Single async Python process running two strategies in parallel:

```
main.py                   Entry point (bot + dashboard)
├── core/
│   ├── bot.py            Main orchestrator (spawns grid + scalp tasks)
│   ├── grid_engine.py    Grid level calculation & order logic
│   ├── scalp_engine.py   Scalp state machine (scan → enter → exit)
│   ├── coin_screener.py  Grid pair ranking (volume/volatility/spread)
│   ├── scalp_screener.py Scalp pair ranking (spike frequency from klines)
│   ├── risk_manager.py   Pre-trade validation & stop-loss
│   └── approval.py       Supervised mode approval queue
├── exchange/
│   ├── binance_adapter.py  Binance REST + WebSocket
│   └── symbol_info.py     Order precision helpers
├── dashboard/
│   ├── app.py            FastAPI app + API endpoints
│   ├── routes/           HTMX partials + controls
│   └── templates/        HTML pages (overview, scalping, trades, settings)
├── notifications/
│   └── discord.py        Discord webhook client
└── db/
    ├── models.py         SQLAlchemy models
    └── repository.py     Trade persistence
```

Each strategy gets its own `BinanceAdapter` instance with independent WebSocket connections for speed.

## Strategies

### Grid Trading

Places buy and sell limit orders at fixed intervals around the current price. Profits from price oscillating within the range. Best for sideways markets.

### Scalping

Two swappable modes:
- **Momentum** — detects sudden price spikes, enters in the same direction to ride the wave
- **Mean Reversion** — detects sudden price drops, buys expecting a snapback

Uses market orders for speed. Exits on take-profit, stop-loss, or time limit (whichever hits first).

## OCI Deployment (Oracle Cloud Free Tier)

See [docs/OCI_DEPLOY.md](docs/OCI_DEPLOY.md) for step-by-step instructions.

## Risk Warning

This bot trades real cryptocurrency when in mainnet mode. Both grid trading and scalping carry risk — you can lose money. Scalping is particularly high-risk. Start with testnet, understand the strategies, and only risk what you can afford to lose.

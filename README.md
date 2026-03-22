# Trade Bot

A Python-based crypto grid trading bot running on Binance. Continuously generates small profits from price oscillation using a grid trading strategy.

## Features

- **Grid Trading** — automated buy/sell at preset price intervals
- **Coin Screener** — ranks trading pairs by volume, volatility, and spread
- **Risk Management** — configurable presets (conservative/moderate/aggressive) with overrides
- **Supervised/Autonomous Modes** — toggle between manual approval and fully automated
- **Web Dashboard** — live grid visualization, P&L tracking, bot controls
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
# Clone and enter directory
cd trade_bot

# Create virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
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

This starts the bot, PostgreSQL, and dashboard. Add `--profile tools` to include pgAdmin.

## Configuration

All config via environment variables in `config/.env`:

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `testnet` | `testnet` or `mainnet` |
| `BOT_MODE` | `supervised` | `supervised` or `autonomous` |
| `RISK_PRESET` | `moderate` | `conservative`, `moderate`, `aggressive` |
| `TRADING_CAPITAL` | `20.0` | Max capital to use (USDT, mainnet only) |
| `TRADING_SYMBOL` | _(empty)_ | Override coin screener (e.g., `DOGEUSDT`) |
| `BINANCE_TESTNET_API_KEY` | | Testnet API key |
| `BINANCE_TESTNET_API_SECRET` | | Testnet API secret |
| `BINANCE_MAINNET_API_KEY` | | Mainnet API key |
| `BINANCE_MAINNET_API_SECRET` | | Mainnet API secret |
| `DISCORD_WEBHOOK_URL` | | Discord webhook for notifications |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |

### Risk Preset Overrides

Override individual preset values:

```
OVERRIDE_GRID_LEVELS=5
OVERRIDE_GRID_RANGE_PCT=0.03
OVERRIDE_STOP_LOSS_PCT=0.05
```

## Architecture

Single async Python process:

```
main.py                 Entry point (bot + dashboard)
├── core/
│   ├── bot.py          Main orchestrator
│   ├── grid_engine.py  Grid level calculation & order logic
│   ├── coin_screener.py  Pair ranking algorithm
│   ├── risk_manager.py   Pre-trade validation & stop-loss
│   └── approval.py     Supervised mode approval queue
├── exchange/
│   ├── binance_adapter.py  Binance REST + WebSocket
│   └── symbol_info.py     Order precision helpers
├── dashboard/
│   ├── app.py          FastAPI app + API endpoints
│   ├── routes/         HTMX partials + controls
│   └── templates/      HTML pages
├── notifications/
│   └── discord.py      Discord webhook client
└── db/
    ├── models.py       SQLAlchemy models
    └── repository.py   Trade persistence
```

## OCI Deployment (Oracle Cloud Free Tier)

See [docs/OCI_DEPLOY.md](docs/OCI_DEPLOY.md) for step-by-step instructions.

## Risk Warning

This bot trades real cryptocurrency when in mainnet mode. Grid trading carries risk — you can lose money if the price moves significantly against your positions. Start with testnet, understand the strategy, and only risk what you can afford to lose.

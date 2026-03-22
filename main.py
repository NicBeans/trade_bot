"""Trade Bot entry point."""

import asyncio
import logging
import logging.handlers
import signal
import traceback
from pathlib import Path

import uvicorn

from config import settings, get_effective_preset
from core.bot import TradeBot
from dashboard.app import app, set_bot


def setup_logging():
    """Configure logging with console + rotating file output."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file (10MB, keep 5 files)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "tradebot.log", maxBytes=10_000_000, backupCount=5,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("binance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)

bot: TradeBot | None = None


async def run_bot():
    global bot
    preset = get_effective_preset()
    bot = TradeBot(settings, preset)
    set_bot(bot)

    logger.info("Mode: %s | Preset: %s | Bot: %s",
                settings.trading_mode.value,
                preset.name,
                settings.bot_mode.value)

    try:
        symbol = settings.trading_symbol if settings.trading_symbol else None
        await bot.start(symbol=symbol)
    except asyncio.CancelledError:
        logger.info("Bot task cancelled")
    except Exception:
        error_msg = traceback.format_exc()
        logger.exception("Bot crashed")
        try:
            await bot.notifier.notify_error(error_msg[-500:])
        except Exception:
            pass
    finally:
        await bot.stop()


async def main():
    bot_task = asyncio.create_task(run_bot())

    config = uvicorn.Config(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: bot_task.cancel())

    await server.serve()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())

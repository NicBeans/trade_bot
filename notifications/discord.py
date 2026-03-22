"""Discord webhook notifications."""

import logging
import time
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

# Embed colors
COLOR_GREEN = 0x00CC66
COLOR_ORANGE = 0xFF9900
COLOR_RED = 0xFF3333
COLOR_BLUE = 0x3399FF
COLOR_PURPLE = 0x9933FF
COLOR_GREY = 0x808080


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, content: str, embed: dict | None = None):
        """Send a message to Discord via webhook."""
        if not self.webhook_url:
            logger.debug("Discord webhook not configured, skipping notification")
            return

        session = await self._get_session()
        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]

        try:
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    logger.error("Discord webhook failed: %d", resp.status)
        except Exception:
            logger.exception("Failed to send Discord notification")

    async def notify_trade(self, symbol: str, side: str, price: float, quantity: float, profit: float | None = None):
        is_buy = side == "BUY"
        embed = {
            "title": f"{'Buy' if is_buy else 'Sell'} {symbol}",
            "color": COLOR_GREEN if is_buy else COLOR_ORANGE,
            "fields": [
                {"name": "Price", "value": f"${price:,.4f}", "inline": True},
                {"name": "Quantity", "value": f"{quantity:.6f}", "inline": True},
                {"name": "Value", "value": f"${price * quantity:,.4f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        if profit is not None:
            color = COLOR_GREEN if profit >= 0 else COLOR_RED
            embed["color"] = color
            embed["fields"].append({"name": "Profit", "value": f"${profit:,.4f}", "inline": True})
        await self.send("", embed=embed)

    async def notify_grid_cycle_complete(self, symbol: str, buy_price: float, sell_price: float, quantity: float, profit: float, total_profit: float, cycles: int):
        embed = {
            "title": f"Grid Cycle Complete - {symbol}",
            "color": COLOR_GREEN if profit >= 0 else COLOR_RED,
            "fields": [
                {"name": "Bought @", "value": f"${buy_price:,.4f}", "inline": True},
                {"name": "Sold @", "value": f"${sell_price:,.4f}", "inline": True},
                {"name": "Quantity", "value": f"{quantity:.6f}", "inline": True},
                {"name": "Cycle Profit", "value": f"${profit:,.4f}", "inline": True},
                {"name": "Total Profit", "value": f"${total_profit:,.4f}", "inline": True},
                {"name": "Cycles", "value": str(cycles), "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_grid_reset(self, symbol: str, old_range: tuple[float, float], new_range: tuple[float, float], capital: float):
        embed = {
            "title": f"Grid Reset - {symbol}",
            "color": COLOR_BLUE,
            "fields": [
                {"name": "Old Range", "value": f"${old_range[0]:,.2f} - ${old_range[1]:,.2f}", "inline": True},
                {"name": "New Range", "value": f"${new_range[0]:,.2f} - ${new_range[1]:,.2f}", "inline": True},
                {"name": "Capital", "value": f"${capital:,.2f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_screener_results(self, candidates: list, selected: str | None = None):
        if not candidates:
            await self.send("", embed={
                "title": "Coin Screener",
                "description": "No suitable pairs found.",
                "color": COLOR_GREY,
            })
            return

        lines = []
        for i, c in enumerate(candidates):
            marker = " **<--**" if c.symbol == selected else ""
            lines.append(
                f"**{i+1}. {c.symbol}** — Score: {c.score}/100{marker}\n"
                f"Price: ${c.price:,.4f} | Vol: ${c.volume_24h:,.0f} | "
                f"Volatility: {c.volatility_pct}% | _{c.reason}_"
            )

        embed = {
            "title": "Coin Screener Results",
            "description": "\n\n".join(lines),
            "color": COLOR_PURPLE,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if selected:
            embed["footer"] = {"text": f"Selected: {selected}"}
        await self.send("", embed=embed)

    async def notify_bot_started(self, mode: str, symbol: str, price: float, grid_range: tuple[float, float], levels: int, capital: float, preset: str):
        embed = {
            "title": f"Bot Started ({mode})",
            "color": COLOR_BLUE,
            "fields": [
                {"name": "Symbol", "value": symbol, "inline": True},
                {"name": "Price", "value": f"${price:,.2f}", "inline": True},
                {"name": "Preset", "value": preset, "inline": True},
                {"name": "Grid Range", "value": f"${grid_range[0]:,.2f} - ${grid_range[1]:,.2f}", "inline": True},
                {"name": "Levels", "value": str(levels), "inline": True},
                {"name": "Capital", "value": f"${capital:,.2f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_bot_stopped(self, total_profit: float, net_pnl: float, cycles: int):
        embed = {
            "title": "Bot Stopped",
            "color": COLOR_GREY,
            "fields": [
                {"name": "Total Profit", "value": f"${total_profit:,.4f}", "inline": True},
                {"name": "Net P&L", "value": f"${net_pnl:,.4f}", "inline": True},
                {"name": "Completed Cycles", "value": str(cycles), "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_daily_summary(self, symbol: str, total_profit: float, net_pnl: float, cycles: int, capital: float, available: float):
        embed = {
            "title": f"Daily Summary - {symbol}",
            "color": COLOR_GREEN if net_pnl >= 0 else COLOR_RED,
            "fields": [
                {"name": "Total Profit", "value": f"${total_profit:,.4f}", "inline": True},
                {"name": "Net P&L", "value": f"${net_pnl:,.4f}", "inline": True},
                {"name": "Cycles", "value": str(cycles), "inline": True},
                {"name": "Capital", "value": f"${capital:,.2f}", "inline": True},
                {"name": "Available", "value": f"${available:,.2f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_error(self, error: str):
        embed = {
            "title": "Bot Error",
            "description": f"```{error}```",
            "color": COLOR_RED,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def notify_stop_loss(self, cumulative_loss: float):
        embed = {
            "title": "STOP-LOSS TRIGGERED",
            "description": f"Cumulative loss: ${cumulative_loss:.4f}\nBot has been **halted**.",
            "color": COLOR_RED,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("@here", embed=embed)

    async def notify_range_exit(self, symbol: str, price: float, grid_range: tuple[float, float], paused: bool):
        embed = {
            "title": f"Grid Range Exit - {symbol}",
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Price", "value": f"${price:,.4f}", "inline": True},
                {"name": "Grid Range", "value": f"${grid_range[0]:,.2f} - ${grid_range[1]:,.2f}", "inline": True},
                {"name": "Action", "value": "**Paused** - awaiting input" if paused else "Auto-resetting grid", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("", embed=embed)

    async def request_approval(self, action: str, details: str) -> None:
        """Send an approval request. Returns the message for reaction tracking."""
        embed = {
            "title": f"Approval Required: {action}",
            "description": details,
            "color": COLOR_PURPLE,
            "footer": {"text": "This action is waiting for approval via the dashboard."},
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.send("@here", embed=embed)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

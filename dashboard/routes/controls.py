"""Bot control endpoints."""

import asyncio
from fastapi import APIRouter
from dashboard.state import get_bot

router = APIRouter(prefix="/api/controls")


@router.post("/stop")
async def stop_bot():
    bot = get_bot()
    if not bot or not bot.is_running:
        return {"error": "Bot is not running"}
    bot._running = False
    return {"success": True, "message": "Bot stop initiated"}


@router.post("/force-reset")
async def force_reset():
    bot = get_bot()
    if not bot or not bot.is_running or not bot.grid:
        return {"error": "Bot is not running"}

    price = bot._last_price
    if price <= 0:
        return {"error": "No current price available"}

    await bot._reset_grid(price)
    return {"success": True, "message": f"Grid reset around {price:.5f}"}

"""Bot control and settings endpoints."""

import asyncio
from fastapi import APIRouter, Request
from dashboard.state import get_bot

router = APIRouter(prefix="/api")


# --- Controls ---

@router.post("/controls/stop")
async def stop_bot():
    bot = get_bot()
    if not bot or not bot.is_running:
        return {"error": "Bot is not running"}
    bot._running = False
    return {"success": True, "message": "Bot stop initiated"}


@router.post("/controls/force-reset")
async def force_reset():
    bot = get_bot()
    if not bot or not bot.is_running or not bot.grid:
        return {"error": "Bot is not running"}

    price = bot._last_price
    if price <= 0:
        return {"error": "No current price available"}

    await bot._reset_grid(price)
    return {"success": True, "message": f"Grid reset around {price:.5f}"}


@router.post("/controls/reset-pnl")
async def reset_pnl():
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}
    return await bot.reset_pnl()


@router.post("/controls/force-sell-positions")
async def force_sell():
    bot = get_bot()
    if not bot or not bot.is_running or not bot.grid:
        return {"error": "Bot is not running"}

    result = await bot.force_sell_positions()
    return result


# --- Settings ---

@router.get("/settings")
async def get_settings():
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}
    return {
        "current": bot.runtime.get_all(),
        "overrides": bot.runtime.get_changes(),
    }


@router.post("/settings")
async def update_settings(request: Request):
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}

    body = await request.json()
    if not body:
        return {"error": "No settings provided"}

    results = await bot.apply_settings(body)
    return {
        "success": True,
        "results": results,
        "current": bot.runtime.get_all(),
    }

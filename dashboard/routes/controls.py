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


@router.post("/controls/fresh-grid")
async def fresh_grid():
    """Deactivate saved grid, sell held coins, and restart with a fresh grid."""
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}

    symbol = bot.grid.config.symbol if bot.grid else None
    results = []

    # 1. Cancel all open orders
    if bot.grid:
        async with bot._order_lock:
            pending_ids = bot.grid.cancel_all_pending()
            for oid in pending_ids:
                try:
                    await bot.exchange.cancel_order(symbol, oid)
                except Exception:
                    pass
            results.append(f"Cancelled {len(pending_ids)} orders")

    # 2. Market sell any held coins
    if symbol and bot.symbol_info:
        try:
            balance = await bot.exchange.get_account_balance(bot.symbol_info.base_asset)
            if balance >= bot.symbol_info.min_qty:
                qty_str = bot.symbol_info.format_quantity(balance)
                await bot.exchange.place_market_order(symbol=symbol, side="SELL", quantity=float(qty_str))
                results.append(f"Sold {qty_str} {bot.symbol_info.base_asset}")
        except Exception as e:
            results.append(f"Sell failed: {e}")

    # 3. Deactivate grid in DB
    try:
        await bot.trade_repo.deactivate_grid(bot.settings.trading_mode.value)
        results.append("Grid state deactivated in DB")
    except Exception:
        results.append("Could not deactivate grid in DB")

    # 4. Restart grid fresh
    bot.grid = None
    bot.risk_manager = None
    usdt = await bot.exchange.get_account_balance("USDT")
    grid_cap = bot.runtime.get("grid_capital")
    capital = min(usdt, grid_cap)

    if capital > 0:
        await bot._start_grid(None, capital)
        results.append(f"Fresh grid started with ${capital:.5f}")
    else:
        results.append("No capital available for new grid")

    await bot.notifier.send(f"**Fresh Grid** | {', '.join(results)}")
    return {"success": True, "results": results}


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

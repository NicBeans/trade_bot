"""FastAPI dashboard application."""

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from config.settings import settings
from dashboard.state import get_bot, set_bot  # noqa: F401 — re-exported for main.py

app = FastAPI(title="Trade Bot Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- Auth middleware ---

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    key = settings.dashboard_key
    if not key:
        return await call_next(request)

    # Allow health check without auth
    if request.url.path == "/health":
        return await call_next(request)

    # Allow login page and login POST
    if request.url.path in ("/login", "/api/login"):
        return await call_next(request)

    # Check cookie
    token = request.cookies.get("dashboard_token")
    if token == key:
        return await call_next(request)

    # Check query param (for API access)
    if request.query_params.get("key") == key:
        return await call_next(request)

    # Not authenticated
    if request.url.path.startswith("/api/"):
        return Response(content='{"error": "unauthorized"}', status_code=401, media_type="application/json")
    return RedirectResponse(url="/login")


# Register routers
from dashboard.routes.partials import router as partials_router
from dashboard.routes.controls import router as controls_router
app.include_router(partials_router)
app.include_router(controls_router)


# --- Auth ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/api/login")
async def login(request: Request):
    form = await request.form()
    key = form.get("key", "")
    if key == settings.dashboard_key:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie("dashboard_token", key, httponly=True, max_age=86400 * 30)
        return response
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid key"})


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    return templates.TemplateResponse(request=request, name="trades.html")


@app.get("/scalping", response_class=HTMLResponse)
async def scalping_page(request: Request):
    return templates.TemplateResponse(request=request, name="scalping.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html")


# --- JSON API Endpoints ---

@app.get("/api/status")
async def api_status():
    bot = get_bot()
    if not bot:
        return {"running": False, "message": "Bot not initialized"}

    result = {
        "running": bot.is_running,
        "mode": bot.settings.trading_mode.value,
        "bot_mode": bot.settings.bot_mode.value,
        "testnet": bot.settings.is_testnet,
    }

    if bot.grid:
        result["symbol"] = bot.grid.config.symbol
        result["last_price"] = bot._last_price
        result["grid"] = bot.grid.get_status_summary()

    if bot.risk_manager:
        result["risk"] = bot.risk_manager.get_status()

    return result


@app.get("/api/grid")
async def api_grid():
    bot = get_bot()
    if not bot or not bot.grid:
        return {"levels": [], "config": None}

    levels = []
    for lv in bot.grid.levels:
        levels.append({
            "index": lv.index,
            "buy_price": lv.buy_price,
            "sell_price": lv.sell_price,
            "state": lv.state.value,
            "quantity": lv.quantity,
            "buy_fill_price": lv.buy_fill_price,
        })

    return {
        "levels": levels,
        "config": {
            "symbol": bot.grid.config.symbol,
            "upper_price": bot.grid.config.upper_price,
            "lower_price": bot.grid.config.lower_price,
            "num_levels": bot.grid.config.num_levels,
            "total_capital": bot.grid.config.total_capital,
            "grid_step": bot.grid.grid_step,
        },
        "total_profit": bot.grid.total_profit,
        "completed_cycles": bot.grid.completed_cycles,
        "last_price": bot._last_price,
    }


@app.get("/api/approvals")
async def api_approvals():
    bot = get_bot()
    if not bot:
        return {"pending": []}

    pending = [
        {"id": r.id, "action": r.action, "details": r.details, "created_at": r.created_at.isoformat()}
        for r in bot.approvals.get_pending()
    ]
    return {"pending": pending}


@app.post("/api/approvals/{request_id}/approve")
async def api_approve(request_id: str):
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}
    ok = bot.approvals.approve(request_id)
    return {"success": ok}


@app.post("/api/approvals/{request_id}/reject")
async def api_reject(request_id: str):
    bot = get_bot()
    if not bot:
        return {"error": "Bot not initialized"}
    ok = bot.approvals.reject(request_id)
    return {"success": ok}


@app.get("/health")
async def health():
    return {"status": "ok"}

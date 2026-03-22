"""HTMX partial HTML endpoints for live dashboard updates."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from dashboard.state import get_bot

router = APIRouter(prefix="/api/partials")


@router.get("/badges", response_class=HTMLResponse)
async def badges():
    bot = get_bot()
    parts = []
    if bot and bot.settings.is_testnet:
        parts.append('<span class="badge badge-testnet">TESTNET</span>')
    elif bot:
        parts.append('<span class="badge badge-live">LIVE</span>')

    if bot and bot.is_running:
        parts.append('<span class="badge badge-running">Running</span>')
    else:
        parts.append('<span class="badge badge-stopped">Stopped</span>')

    return " ".join(parts)


@router.get("/stats", response_class=HTMLResponse)
async def stats():
    bot = get_bot()
    if not bot or not bot.grid:
        return '<div class="grid-4">' + _stat("--", "Price") + _stat("--", "Profit") + _stat("--", "Cycles") + _stat("--", "Capital") + '</div>'

    price = bot._last_price
    summary = bot.grid.get_status_summary()
    risk = bot.risk_manager.get_status() if bot.risk_manager else {}

    profit = summary["total_profit"]
    net_pnl = risk.get("net_pnl", 0)
    cycles = summary["completed_cycles"]
    capital = risk.get("available_capital", 0)

    profit_class = "green" if net_pnl >= 0 else "red"

    return f'''<div class="grid-4">
        {_stat(f"${price:,.2f}", bot.grid.config.symbol, "blue")}
        {_stat(f"${net_pnl:,.4f}", "Net P&L", profit_class)}
        {_stat(str(cycles), "Completed Cycles")}
        {_stat(f"${capital:,.2f}", "Available Capital")}
    </div>'''


@router.get("/grid", response_class=HTMLResponse)
async def grid():
    bot = get_bot()
    if not bot or not bot.grid:
        return '<p style="color: var(--text-dim);">Bot not running</p>'

    levels = bot.grid.levels
    current_price = bot._last_price
    html_parts = ['<div class="grid-levels">']

    # Render levels from top to bottom (highest price first)
    price_inserted = False
    for level in reversed(levels):
        # Insert current price marker
        if not price_inserted and current_price >= level.buy_price:
            html_parts.append(
                f'<div class="price-marker">PRICE ${current_price:,.2f}</div>'
            )
            price_inserted = True

        state = level.state.value
        state_label = state.replace("_", " ")
        price_display = f"${level.buy_price:,.2f}" if state in ("empty", "buy_pending") else f"${level.sell_price:,.2f}"
        extra = ""
        if level.buy_fill_price:
            extra = f' (bought @ ${level.buy_fill_price:,.2f})'

        html_parts.append(
            f'<div class="grid-level {state}">'
            f'<span class="level-idx">#{level.index}</span>'
            f'<span class="level-price">{price_display}{extra}</span>'
            f'<span class="level-state">{state_label}</span>'
            f'</div>'
        )

    if not price_inserted:
        html_parts.insert(1, f'<div class="price-marker">PRICE ${current_price:,.2f}</div>')

    html_parts.append('</div>')
    return "\n".join(html_parts)


@router.get("/approvals", response_class=HTMLResponse)
async def approvals():
    bot = get_bot()
    if not bot:
        return '<p style="color: var(--text-dim);">Bot not running</p>'

    pending = bot.approvals.get_pending()
    if not pending:
        return '<p style="color: var(--text-dim);">No pending approvals</p>'

    html_parts = []
    for req in pending:
        html_parts.append(f'''
            <div class="approval">
                <div>
                    <strong>{req.action}</strong><br>
                    <small style="color: var(--text-dim);">{req.details}</small>
                </div>
                <div class="approval-actions">
                    <button class="btn btn-green" hx-post="/api/approvals/{req.id}/approve" hx-swap="none"
                            hx-on::after-request="htmx.trigger('#approvals', 'htmx:load')">Approve</button>
                    <button class="btn btn-red" hx-post="/api/approvals/{req.id}/reject" hx-swap="none"
                            hx-on::after-request="htmx.trigger('#approvals', 'htmx:load')">Reject</button>
                </div>
            </div>
        ''')
    return "\n".join(html_parts)


@router.get("/risk", response_class=HTMLResponse)
async def risk():
    bot = get_bot()
    if not bot or not bot.risk_manager:
        return '<p style="color: var(--text-dim);">Bot not running</p>'

    r = bot.risk_manager.get_status()
    stop_class = "red" if r["stop_loss_triggered"] else "green"
    stop_text = "TRIGGERED" if r["stop_loss_triggered"] else "OK"

    return f'''
        <table>
            <tr><td style="color: var(--text-dim);">Total Capital</td><td>${r["total_capital"]:,.2f}</td></tr>
            <tr><td style="color: var(--text-dim);">Available</td><td>${r["available_capital"]:,.2f}</td></tr>
            <tr><td style="color: var(--text-dim);">Cumulative Profit</td><td style="color: var(--green);">${r["cumulative_profit"]:,.4f}</td></tr>
            <tr><td style="color: var(--text-dim);">Cumulative Loss</td><td style="color: var(--red);">${r["cumulative_loss"]:,.4f}</td></tr>
            <tr><td style="color: var(--text-dim);">Net P&L</td><td style="color: var(--{"green" if r["net_pnl"] >= 0 else "red"});">${r["net_pnl"]:,.4f}</td></tr>
            <tr><td style="color: var(--text-dim);">Stop-Loss</td><td style="color: var(--{stop_class});">{stop_text}</td></tr>
            <tr><td style="color: var(--text-dim);">Preset</td><td>{r["preset"]}</td></tr>
        </table>
    '''


@router.get("/trade-history", response_class=HTMLResponse)
async def trade_history():
    bot = get_bot()
    if not bot:
        return '<p style="color: var(--text-dim);">No trade data yet</p>'

    # Try loading from database
    try:
        trades = await bot.trade_repo.get_trades(
            trading_mode=bot.settings.trading_mode.value, limit=50
        )
    except Exception:
        trades = []

    if not trades:
        cycles = bot.grid.completed_cycles if bot.grid else 0
        if cycles > 0:
            return f'<p style="color: var(--text-dim);">Cycles completed this session: {cycles} (DB not connected)</p>'
        return '<p style="color: var(--text-dim);">No completed trades yet.</p>'

    rows = []
    for t in trades:
        side_color = "green" if t.side == "BUY" else "orange"
        profit_cell = ""
        if t.profit is not None:
            p_color = "green" if t.profit >= 0 else "red"
            profit_cell = f'<td style="color: var(--{p_color});">${t.profit:,.4f}</td>'
        else:
            profit_cell = '<td style="color: var(--text-dim);">—</td>'
        time_str = t.created_at.strftime("%m-%d %H:%M") if t.created_at else ""
        rows.append(
            f'<tr>'
            f'<td>{time_str}</td>'
            f'<td style="color: var(--{side_color});">{t.side}</td>'
            f'<td>{t.symbol}</td>'
            f'<td>${t.price:,.4f}</td>'
            f'<td>{t.quantity:.6f}</td>'
            f'<td>${t.fee:,.6f}</td>'
            f'{profit_cell}'
            f'</tr>'
        )

    return f'''
        <table>
            <thead><tr>
                <th>Time</th><th>Side</th><th>Symbol</th><th>Price</th><th>Qty</th><th>Fee</th><th>Profit</th>
            </tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    '''


@router.get("/config", response_class=HTMLResponse)
async def config():
    bot = get_bot()
    if not bot:
        return '<p style="color: var(--text-dim);">Bot not initialized</p>'

    rows = [
        ("Trading Mode", bot.settings.trading_mode.value),
        ("Bot Mode", bot.settings.bot_mode.value),
        ("Preset", bot.preset.name),
        ("Grid Levels", str(bot.preset.grid_levels)),
        ("Grid Range", f"{bot.preset.grid_range_pct * 100:.1f}%"),
        ("Stop-Loss", f"{bot.preset.stop_loss_pct * 100:.1f}%" if bot.preset.stop_loss_pct else "Disabled"),
        ("Max Capital/Level", f"{bot.preset.max_capital_per_level_pct * 100:.1f}%"),
        ("Reset Cooldown", f"{bot.preset.grid_reset_cooldown_seconds}s"),
        ("Pause on Range Exit", "Yes" if bot.preset.pause_on_range_exit else "No"),
        ("Trading Capital", f"${bot.settings.trading_capital:.2f}"),
    ]

    if bot.grid:
        rows.append(("Symbol", bot.grid.config.symbol))
        rows.append(("Grid Range", f"${bot.grid.config.lower_price:,.2f} - ${bot.grid.config.upper_price:,.2f}"))

    table_rows = "\n".join(
        f'<tr><td style="color: var(--text-dim);">{k}</td><td>{v}</td></tr>' for k, v in rows
    )
    return f"<table>{table_rows}</table>"


def _stat(value: str, label: str, color: str = "") -> str:
    cls = f' {color}' if color else ''
    return f'''<div class="card"><div class="stat">
        <div class="stat-value{cls}">{value}</div>
        <div class="stat-label">{label}</div>
    </div></div>'''

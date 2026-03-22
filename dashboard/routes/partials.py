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
        {_stat(f"${price:,.5f}", bot.grid.config.symbol, "blue")}
        {_stat(f"${net_pnl:,.5f}", "Net P&L", profit_class)}
        {_stat(str(cycles), "Completed Cycles")}
        {_stat(f"${capital:,.5f}", "Available Capital")}
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
                f'<div class="price-marker">PRICE ${current_price:,.5f}</div>'
            )
            price_inserted = True

        state = level.state.value
        state_label = state.replace("_", " ")
        price_display = f"${level.buy_price:,.5f}" if state in ("empty", "buy_pending") else f"${level.sell_price:,.5f}"
        extra = ""
        if level.buy_fill_price:
            extra = f' (bought @ ${level.buy_fill_price:,.5f})'

        html_parts.append(
            f'<div class="grid-level {state}">'
            f'<span class="level-idx">#{level.index}</span>'
            f'<span class="level-price">{price_display}{extra}</span>'
            f'<span class="level-state">{state_label}</span>'
            f'</div>'
        )

    if not price_inserted:
        html_parts.insert(1, f'<div class="price-marker">PRICE ${current_price:,.5f}</div>')

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
            <tr><td style="color: var(--text-dim);">Total Capital</td><td>${r["total_capital"]:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Available</td><td>${r["available_capital"]:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Cumulative Profit</td><td style="color: var(--green);">${r["cumulative_profit"]:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Cumulative Loss</td><td style="color: var(--red);">${r["cumulative_loss"]:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Net P&L</td><td style="color: var(--{"green" if r["net_pnl"] >= 0 else "red"});">${r["net_pnl"]:,.5f}</td></tr>
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
            profit_cell = f'<td style="color: var(--{p_color});">${t.profit:,.5f}</td>'
        else:
            profit_cell = '<td style="color: var(--text-dim);">—</td>'
        time_str = t.created_at.strftime("%m-%d %H:%M") if t.created_at else ""
        rows.append(
            f'<tr>'
            f'<td>{time_str}</td>'
            f'<td style="color: var(--{side_color});">{t.side}</td>'
            f'<td>{t.symbol}</td>'
            f'<td>${t.price:,.5f}</td>'
            f'<td>{t.quantity:.6f}</td>'
            f'<td>${t.fee:,.5f}</td>'
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
        ("Trading Capital", f"${bot.settings.trading_capital:.5f}"),
    ]

    if bot.grid:
        rows.append(("Symbol", bot.grid.config.symbol))
        rows.append(("Grid Range", f"${bot.grid.config.lower_price:,.5f} - ${bot.grid.config.upper_price:,.5f}"))

    table_rows = "\n".join(
        f'<tr><td style="color: var(--text-dim);">{k}</td><td>{v}</td></tr>' for k, v in rows
    )
    return f"<table>{table_rows}</table>"


@router.get("/scalp-summary", response_class=HTMLResponse)
async def scalp_summary():
    bot = get_bot()
    if not bot or not bot.scalp_engine:
        return '<p style="color: var(--text-dim);">Scalping disabled</p>'

    s = bot.scalp_engine.get_status()
    state = s["state"]
    state_colors = {"scanning": "blue", "in_position": "orange", "cooldown": "text-dim", "entering": "green", "exiting": "red", "stopped": "text-dim"}
    state_color = state_colors.get(state, "text-dim")

    html = f'''
        <table>
            <tr><td style="color: var(--text-dim);">Pair</td><td>{s["symbol"]}</td></tr>
            <tr><td style="color: var(--text-dim);">Mode</td><td>{s["mode"]}</td></tr>
            <tr><td style="color: var(--text-dim);">State</td><td style="color: var(--{state_color});">{state.upper()}</td></tr>
            <tr><td style="color: var(--text-dim);">Trades</td><td>{s["stats"]["total_trades"]}</td></tr>
            <tr><td style="color: var(--text-dim);">Win Rate</td><td>{s["stats"]["win_rate"]}%</td></tr>
            <tr><td style="color: var(--text-dim);">Profit</td><td style="color: var(--{"green" if s["stats"]["total_profit"] >= 0 else "red"});">${s["stats"]["total_profit"]:,.5f}</td></tr>
        </table>
    '''
    if s.get("current_trade"):
        ct = s["current_trade"]
        pnl_color = "green" if ct["unrealised_pnl"] >= 0 else "red"
        html += f'''
            <div style="margin-top: 0.5rem; padding: 0.5rem; background: #1c2128; border-radius: 4px;">
                <small style="color: var(--text-dim);">In Position:</small><br>
                Entry: ${ct["entry_price"]:,.5f} | P&L: <span style="color: var(--{pnl_color});">${ct["unrealised_pnl"]:,.5f}</span> | {ct["elapsed"]:.0f}s
            </div>
        '''
    return html


@router.get("/scalp-status", response_class=HTMLResponse)
async def scalp_status():
    bot = get_bot()
    if not bot or not bot.scalp_engine:
        return '<div class="card"><p style="color: var(--text-dim);">Scalping not active</p></div>'

    s = bot.scalp_engine.get_status()
    state = s["state"]
    state_colors = {"scanning": "blue", "in_position": "orange", "cooldown": "text-dim", "entering": "green", "exiting": "red", "stopped": "text-dim"}
    state_color = state_colors.get(state, "text-dim")

    stats_html = f'''<div class="grid-4">
        {_stat(f"${s['last_price']:,.5f}", s['symbol'], "blue")}
        {_stat(state.upper(), "State", state_color)}
        {_stat(s['mode'].replace('_', ' ').title(), "Mode")}
        {_stat(f"${s['capital']:,.5f}", "Capital")}
    </div>'''

    position_html = ""
    if s.get("current_trade"):
        ct = s["current_trade"]
        pnl_color = "green" if ct["unrealised_pnl"] >= 0 else "red"
        position_html = f'''
        <div class="card" style="border-color: var(--orange);">
            <div class="card-title">Active Position</div>
            <div class="grid-4">
                {_stat(f"${ct['entry_price']:,.5f}", "Entry Price")}
                {_stat(f"{ct['quantity']:.6f}", "Quantity")}
                {_stat(f"${ct['unrealised_pnl']:,.5f}", "Unrealised P&L", pnl_color)}
                {_stat(f"{ct['elapsed']:.0f}s", "Duration")}
            </div>
        </div>'''

    return stats_html + position_html


@router.get("/scalp-log", response_class=HTMLResponse)
async def scalp_log():
    bot = get_bot()
    if not bot or not bot.scalp_engine:
        return '<p style="color: var(--text-dim);">No scalp trades yet</p>'

    trades = list(bot.scalp_engine.recent_trades)
    if not trades:
        return '<p style="color: var(--text-dim);">No scalp trades yet. Waiting for triggers...</p>'

    rows = []
    for t in reversed(trades):
        duration = (t.exit_time - t.entry_time) if t.exit_time else 0
        profit = t.profit or 0
        p_color = "green" if profit >= 0 else "red"
        result = "WIN" if profit >= 0 else "LOSS"
        reason = (t.exit_reason or "").upper()
        rows.append(
            f'<tr>'
            f'<td>{t.symbol}</td>'
            f'<td>${t.entry_price:,.5f}</td>'
            f'<td>${t.exit_price:,.5f}</td>' if t.exit_price else '<td>—</td>'
            f'<td style="color: var(--{p_color});">${profit:,.5f}</td>'
            f'<td>{duration:.1f}s</td>'
            f'<td>{reason}</td>'
            f'<td style="color: var(--{p_color});">{result}</td>'
            f'</tr>'
        )

    return f'''
        <table>
            <thead><tr>
                <th>Pair</th><th>Entry</th><th>Exit</th><th>Profit</th><th>Duration</th><th>Reason</th><th>Result</th>
            </tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    '''


@router.get("/scalp-stats", response_class=HTMLResponse)
async def scalp_stats():
    bot = get_bot()
    if not bot or not bot.scalp_engine:
        return '<p style="color: var(--text-dim);">Scalping not active</p>'

    st = bot.scalp_engine.stats
    pnl_color = "green" if st.total_profit >= 0 else "red"

    return f'''
        <table>
            <tr><td style="color: var(--text-dim);">Total Trades</td><td>{st.total_trades}</td></tr>
            <tr><td style="color: var(--text-dim);">Wins</td><td style="color: var(--green);">{st.wins}</td></tr>
            <tr><td style="color: var(--text-dim);">Losses</td><td style="color: var(--red);">{st.losses}</td></tr>
            <tr><td style="color: var(--text-dim);">Win Rate</td><td>{st.win_rate:.1f}%</td></tr>
            <tr><td style="color: var(--text-dim);">Total Profit</td><td style="color: var(--{pnl_color});">${st.total_profit:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Avg Profit/Trade</td><td>${st.avg_profit:,.5f}</td></tr>
            <tr><td style="color: var(--text-dim);">Avg Duration</td><td>{st.avg_duration:.1f}s</td></tr>
        </table>
    '''


def _stat(value: str, label: str, color: str = "") -> str:
    cls = f' {color}' if color else ''
    return f'''<div class="card"><div class="stat">
        <div class="stat-value{cls}">{value}</div>
        <div class="stat-label">{label}</div>
    </div></div>'''

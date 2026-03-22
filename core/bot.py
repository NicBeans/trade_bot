"""Main bot orchestrator — coordinates grid engine, screener, risk manager."""

import asyncio
import logging
import time

from config.presets import RiskPreset
from core.approval import ApprovalQueue
from core.coin_screener import CoinScreener
from core.grid_engine import GridEngine, GridConfig, LevelState
from core.risk_manager import RiskManager
from db.database import async_session
from db.repository import TradeRepository
from exchange.binance_adapter import BinanceAdapter
from exchange.symbol_info import SymbolInfo
from notifications.discord import DiscordNotifier

logger = logging.getLogger(__name__)

# Throttle: only evaluate grid actions this often
PRICE_EVAL_INTERVAL = 1.0  # seconds


class TradeBot:
    def __init__(self, settings, preset: RiskPreset):
        self.settings = settings
        self.preset = preset
        self._running = False
        self.exchange = BinanceAdapter(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.is_testnet,
        )
        self.notifier = DiscordNotifier(settings.discord_webhook_url)
        self.screener: CoinScreener | None = None
        self.grid: GridEngine | None = None
        self.risk_manager: RiskManager | None = None
        self.symbol_info: SymbolInfo | None = None
        self._last_price: float = 0.0
        self._last_eval_price: float = 0.0
        self._order_lock = asyncio.Lock()
        self._last_daily_summary: float = 0.0
        self.approvals = ApprovalQueue()
        self.trade_repo = TradeRepository(async_session)

    async def start(self, symbol: str | None = None):
        logger.info(
            "Starting TradeBot in %s mode (%s)",
            self.settings.trading_mode.value,
            self.settings.bot_mode.value,
        )
        self._running = True

        await self.exchange.connect()

        # Initialize database tables
        try:
            await self.trade_repo.init_tables()
        except Exception:
            logger.warning("Database not available — trade history will not be persisted")

        # Get account balance
        usdt_balance = await self.exchange.get_account_balance("USDT")
        logger.info("USDT balance: %.4f", usdt_balance)

        # Determine trading capital
        capital = min(usdt_balance, self.settings.trading_capital) if not self.settings.is_testnet else min(usdt_balance, 100.0)

        # Run coin screener if no symbol specified
        self.screener = CoinScreener(
            self.exchange,
            min_volume_usd=1_000_000,
            min_capital=capital,
        )

        if symbol is None:
            symbol = await self._select_symbol(capital)
            if symbol is None:
                logger.error("No suitable trading pair found. Exiting.")
                await self.notifier.send("**No suitable pairs found.** Bot shutting down.")
                self._running = False
                return

        # Get symbol info for order precision
        raw_info = await self.exchange.get_symbol_info(symbol)
        if not raw_info:
            logger.error("Symbol %s not found on exchange", symbol)
            return
        self.symbol_info = SymbolInfo(raw_info)
        logger.info("Symbol info: %s", self.symbol_info)

        # Get current price and set up grid
        current_price = await self.exchange.get_symbol_price(symbol)
        logger.info("Current %s price: %.8f", symbol, current_price)

        grid_range = current_price * self.preset.grid_range_pct
        config = GridConfig(
            symbol=symbol,
            upper_price=current_price + grid_range,
            lower_price=current_price - grid_range,
            num_levels=self.preset.grid_levels,
            total_capital=capital,
        )

        self.grid = GridEngine(config, self.symbol_info)
        self.risk_manager = RiskManager(self.preset, config.total_capital)

        await self.notifier.notify_bot_started(
            mode=self.settings.trading_mode.value,
            symbol=symbol,
            price=current_price,
            grid_range=(config.lower_price, config.upper_price),
            levels=self.preset.grid_levels,
            capital=config.total_capital,
            preset=self.preset.name,
        )

        # Subscribe to user data for order fills
        await self.exchange.subscribe_user_data(self._on_order_update)

        # Place initial grid orders
        await self._place_initial_orders(current_price)

        # Subscribe to price stream
        await self.exchange.subscribe_price_stream(symbol, self._on_price_update)

        logger.info("Bot is running. Grid active on %s", symbol)

        # Keep alive + periodic tasks
        self._last_daily_summary = time.time()
        while self._running:
            await asyncio.sleep(10)
            await self._check_periodic_tasks()

    async def _select_symbol(self, capital: float) -> str | None:
        """Run screener and select a symbol. Returns None if no candidates."""
        candidates = await self.screener.screen(
            quote_asset="USDT",
            top_n=5,
            num_grid_levels=self.preset.grid_levels,
        )

        if not candidates:
            return None

        # Auto-select top candidate
        selected = candidates[0]
        logger.info("Auto-selected: %s (score: %.1f)", selected.symbol, selected.score)

        # Send results to Discord
        await self.notifier.notify_screener_results(candidates, selected=selected.symbol)

        return selected.symbol

    async def _check_periodic_tasks(self):
        """Run periodic background tasks."""
        now = time.time()
        # Daily summary every 24h
        if now - self._last_daily_summary >= 86400:
            self._last_daily_summary = now
            await self._send_daily_summary()

    async def _send_daily_summary(self):
        """Send daily P&L summary to Discord."""
        if not self.grid or not self.risk_manager:
            return
        summary = self.grid.get_status_summary()
        risk = self.risk_manager.get_status()
        await self.notifier.notify_daily_summary(
            symbol=self.grid.config.symbol,
            total_profit=summary["total_profit"],
            net_pnl=risk["net_pnl"],
            cycles=summary["completed_cycles"],
            capital=risk["total_capital"],
            available=risk["available_capital"],
        )
        logger.info("Daily summary sent")

    async def _place_initial_orders(self, current_price: float):
        """Place buy orders below current price for all eligible levels."""
        levels_to_buy = self.grid.get_levels_to_buy(current_price)
        logger.info("Placing initial buy orders for %d levels below price %.8f",
                     len(levels_to_buy), current_price)

        for level in levels_to_buy:
            await self._try_place_buy(level)

    async def _on_price_update(self, data: dict):
        """Handle incoming price ticks."""
        price = data["price"]
        self._last_price = price

        # Throttle grid evaluation — only act if price moved meaningfully
        if self.grid and self.grid.grid_step > 0:
            price_diff = abs(price - self._last_eval_price)
            if price_diff < self.grid.grid_step * 0.1:
                return  # Price hasn't moved enough to matter

        self._last_eval_price = price
        await self._evaluate_grid(price)

    async def _evaluate_grid(self, current_price: float):
        """Check if any grid levels need new orders."""
        if not self.grid or not self._running:
            return

        async with self._order_lock:
            # Check for range exit
            if not self.grid.is_price_in_range(current_price):
                logger.warning("Price %.8f exited grid range!", current_price)
                grid_range = (self.grid.config.lower_price, self.grid.config.upper_price)
                if self.preset.pause_on_range_exit:
                    await self.notifier.notify_range_exit(
                        self.grid.config.symbol, current_price, grid_range, paused=True,
                    )
                    self._running = False
                    return

                # Check cooldown
                can_reset, reason = self.risk_manager.can_reset_grid()
                if not can_reset:
                    logger.info("Grid reset blocked: %s", reason)
                    return

                from config.settings import BotMode
                if self.settings.bot_mode == BotMode.SUPERVISED:
                    # Request approval via dashboard
                    await self.notifier.notify_range_exit(
                        self.grid.config.symbol, current_price, grid_range, paused=False,
                    )
                    await self.notifier.request_approval(
                        "Grid Reset",
                        f"Price {current_price:.2f} exited grid range.\n"
                        f"Approve reset around new price?",
                    )
                    req = self.approvals.create_request(
                        "grid_reset",
                        f"Price exited range. Reset grid around {current_price:.2f}?",
                    )
                    # Don't block the price stream — handle in periodic tasks
                    return

                # Autonomous mode: auto-reset
                await self._reset_grid(current_price)
                return

            # Place buy orders for empty levels at or below price
            for level in self.grid.get_levels_to_buy(current_price):
                await self._try_place_buy(level)

            # Place sell orders for holding levels at or below price
            for level in self.grid.get_levels_to_sell(current_price):
                await self._try_place_sell(level)

    async def _try_place_buy(self, level):
        """Attempt to place a buy order for a grid level."""
        order_params = self.grid.prepare_buy_order(level)
        if not order_params:
            return

        # Risk check
        spend = float(order_params["price"]) * float(order_params["quantity"])
        ok, reason = self.risk_manager.can_place_order(spend, self.symbol_info.min_notional)
        if not ok:
            logger.warning("Risk rejected buy at level %d: %s", level.index, reason)
            return

        try:
            result = await self.exchange.place_limit_order(
                symbol=order_params["symbol"],
                side="BUY",
                price=float(order_params["price"]),
                quantity=float(order_params["quantity"]),
            )
            self.grid.on_buy_placed(level.index, result["orderId"])
            self.risk_manager.reserve_capital(spend)
        except Exception:
            logger.exception("Failed to place buy order at level %d", level.index)

    async def _try_place_sell(self, level):
        """Attempt to place a sell order for a grid level."""
        order_params = self.grid.prepare_sell_order(level)
        if not order_params:
            return

        try:
            result = await self.exchange.place_limit_order(
                symbol=order_params["symbol"],
                side="SELL",
                price=float(order_params["price"]),
                quantity=float(order_params["quantity"]),
            )
            self.grid.on_sell_placed(level.index, result["orderId"])
        except Exception:
            logger.exception("Failed to place sell order at level %d", level.index)

    async def _on_order_update(self, data: dict):
        """Handle order fill events from user data stream."""
        if not self.grid:
            return

        order_id = data["order_id"]
        status = data["order_status"]
        side = data["side"]
        exec_type = data["execution_type"]

        if exec_type == "TRADE" and status == "FILLED":
            filled_price = data["last_filled_price"]
            filled_qty = data["cumulative_qty"]
            commission = data["commission"]

            async with self._order_lock:
                if side == "BUY":
                    level = self.grid.on_buy_filled(order_id, filled_price, filled_qty)
                    if level:
                        await self._try_place_sell(level)
                        await self.notifier.notify_trade(
                            self.grid.config.symbol, "BUY", filled_price, filled_qty
                        )
                        await self._save_trade("BUY", filled_price, filled_qty, order_id, commission, data.get("commission_asset", ""))

                elif side == "SELL":
                    level = self.grid._find_level_by_order(order_id)
                    buy_price = level.buy_fill_price if level else 0.0

                    profit = self.grid.on_sell_filled(order_id, filled_price, filled_qty)
                    if profit is not None:
                        capital_returned = filled_price * filled_qty
                        self.risk_manager.record_trade(profit, capital_returned)

                        await self.notifier.notify_grid_cycle_complete(
                            symbol=self.grid.config.symbol,
                            buy_price=buy_price,
                            sell_price=filled_price,
                            quantity=filled_qty,
                            profit=profit,
                            total_profit=self.grid.total_profit,
                            cycles=self.grid.completed_cycles,
                        )
                        await self._save_trade("SELL", filled_price, filled_qty, order_id, commission, data.get("commission_asset", ""), profit)

                        if self.risk_manager.is_stop_loss_triggered():
                            await self._handle_stop_loss()

        elif status == "CANCELED":
            async with self._order_lock:
                self.grid.on_order_cancelled(order_id)

    async def _save_trade(self, side: str, price: float, qty: float, order_id, commission: float, commission_asset: str, profit: float | None = None):
        """Persist trade to database (best effort)."""
        try:
            await self.trade_repo.save_trade(
                symbol=self.grid.config.symbol,
                side=side,
                price=price,
                quantity=qty,
                order_id=str(order_id),
                trading_mode=self.settings.trading_mode.value,
                fee=commission,
                fee_asset=commission_asset or "BNB",
                profit=profit,
            )
        except Exception:
            logger.debug("Could not save trade to DB (database may not be running)")

    async def _reset_grid(self, current_price: float):
        """Cancel all orders and rebuild grid around current price."""
        logger.info("Resetting grid around price %.8f", current_price)

        # Cancel all pending orders
        pending_ids = self.grid.cancel_all_pending()
        for oid in pending_ids:
            try:
                await self.exchange.cancel_order(self.grid.config.symbol, oid)
            except Exception:
                logger.exception("Failed to cancel order %s during grid reset", oid)

        # Rebuild grid around new price
        grid_range = current_price * self.preset.grid_range_pct
        new_config = GridConfig(
            symbol=self.grid.config.symbol,
            upper_price=current_price + grid_range,
            lower_price=current_price - grid_range,
            num_levels=self.preset.grid_levels,
            total_capital=self.risk_manager.available_capital,
        )

        old_range = (self.grid.config.lower_price, self.grid.config.upper_price)
        self.grid = GridEngine(new_config, self.symbol_info)
        self.risk_manager.record_grid_reset()

        await self.notifier.notify_grid_reset(
            new_config.symbol,
            old_range,
            (new_config.lower_price, new_config.upper_price),
            new_config.total_capital,
        )

        # Place initial orders for new grid
        await self._place_initial_orders(current_price)

    async def _handle_stop_loss(self):
        """Stop-loss triggered — cancel everything and halt."""
        logger.error("STOP-LOSS TRIGGERED")
        await self.notifier.notify_stop_loss(self.risk_manager.cumulative_loss)

        # Cancel all open orders
        pending_ids = self.grid.cancel_all_pending()
        for oid in pending_ids:
            try:
                await self.exchange.cancel_order(self.grid.config.symbol, oid)
            except Exception:
                logger.exception("Failed to cancel order %s during stop-loss", oid)

        self._running = False

    async def stop(self):
        logger.info("Stopping TradeBot")
        self._running = False

        # Cancel open orders
        if self.grid:
            summary = self.grid.get_status_summary()
            risk_status = self.risk_manager.get_status() if self.risk_manager else {}
            logger.info("Final grid status: %s", summary)
            logger.info("Final risk status: %s", risk_status)
            await self.notifier.notify_bot_stopped(
                total_profit=summary["total_profit"],
                net_pnl=risk_status.get("net_pnl", 0),
                cycles=summary["completed_cycles"],
            )
            pending_ids = self.grid.cancel_all_pending()
            for oid in pending_ids:
                try:
                    await self.exchange.cancel_order(self.grid.config.symbol, oid)
                except Exception:
                    logger.exception("Failed to cancel order %s on shutdown", oid)

        await self.notifier.close()
        await self.exchange.disconnect()

    @property
    def is_running(self) -> bool:
        return self._running

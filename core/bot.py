"""Main bot orchestrator — coordinates grid engine, screener, risk manager."""

import asyncio
import logging
import time

from config.presets import RiskPreset
from core.approval import ApprovalQueue
from core.coin_screener import CoinScreener
from core.grid_engine import GridEngine, GridConfig, LevelState
from core.risk_manager import RiskManager
from core.scalp_engine import ScalpEngine, ScalpMode, ScalpTrade
from core.scalp_screener import ScalpScreener
from db.database import async_session
from db.repository import TradeRepository
from core.runtime_settings import RuntimeSettings
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
        self._last_rescreen: float = 0.0
        self._swap_in_progress = False
        self.approvals = ApprovalQueue()
        self.trade_repo = TradeRepository(async_session)
        self.runtime = RuntimeSettings(settings)

        # Scalping
        self.scalp_engine: ScalpEngine | None = None
        self.scalp_exchange: BinanceAdapter | None = None
        self._scalp_task: asyncio.Task | None = None

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

        # Determine per-strategy capital
        grid_enabled = self.settings.grid_capital > 0
        scalp_enabled = self.settings.scalp_capital > 0

        total_requested = self.settings.grid_capital + self.settings.scalp_capital
        if total_requested > usdt_balance:
            logger.warning("Requested capital ($%.5f) exceeds balance ($%.5f), scaling down",
                           total_requested, usdt_balance)
            ratio = usdt_balance / total_requested
            grid_capital = self.settings.grid_capital * ratio if grid_enabled else 0
            scalp_capital = self.settings.scalp_capital * ratio if scalp_enabled else 0
        else:
            grid_capital = self.settings.grid_capital if grid_enabled else 0
            scalp_capital = self.settings.scalp_capital if scalp_enabled else 0

        logger.info("Capital allocation — Grid: $%.5f | Scalp: $%.5f", grid_capital, scalp_capital)

        # --- Start Grid Strategy ---
        if grid_enabled and grid_capital > 0:
            await self._start_grid(symbol, grid_capital)
        else:
            logger.info("Grid trading disabled (GRID_CAPITAL=0)")

        # --- Start Scalp Strategy ---
        if scalp_enabled and scalp_capital > 0:
            self._scalp_task = asyncio.create_task(self._start_scalper(scalp_capital))
        else:
            logger.info("Scalping disabled (SCALP_CAPITAL=0)")

        if not grid_enabled and not scalp_enabled:
            logger.error("Both strategies disabled. Set GRID_CAPITAL or SCALP_CAPITAL > 0.")
            self._running = False
            return

        # Keep alive + periodic tasks
        self._last_daily_summary = time.time()
        self._last_rescreen = time.time()
        while self._running:
            await asyncio.sleep(10)
            await self._check_periodic_tasks()

    async def _start_grid(self, symbol: str | None, capital: float):
        """Initialize and start the grid trading strategy."""
        self.screener = CoinScreener(
            self.exchange, min_volume_usd=1_000_000, min_capital=capital,
        )

        trading_mode = self.settings.trading_mode.value

        # Try to restore saved grid state from DB
        restored = await self._try_restore_grid(trading_mode, capital)

        if not restored:
            # No saved state — create fresh grid
            if symbol is None:
                symbol = await self._select_symbol(capital)
                if symbol is None:
                    logger.error("No suitable grid trading pair found.")
                    await self.notifier.send("**No suitable grid pairs found.**")
                    return

            raw_info = await self.exchange.get_symbol_info(symbol)
            if not raw_info:
                logger.error("Symbol %s not found on exchange", symbol)
                return
            self.symbol_info = SymbolInfo(raw_info)
            logger.info("Grid symbol info: %s", self.symbol_info)

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
                mode=trading_mode,
                symbol=symbol,
                price=current_price,
                grid_range=(config.lower_price, config.upper_price),
                levels=self.preset.grid_levels,
                capital=config.total_capital,
                preset=self.preset.name,
            )

            await self.exchange.subscribe_user_data(self._on_order_update)
            await self._place_initial_orders(current_price)

            # Save initial grid state to DB
            await self._persist_grid_state(full_save=True)
        else:
            # Restored — just subscribe to streams
            await self.exchange.subscribe_user_data(self._on_order_update)

        await self.exchange.subscribe_price_stream(self.grid.config.symbol, self._on_price_update)
        logger.info("Grid active on %s", self.grid.config.symbol)

    async def _try_restore_grid(self, trading_mode: str, capital: float) -> bool:
        """Try to restore grid state from DB. Returns True if restored."""
        try:
            saved_grid, saved_levels = await self.trade_repo.get_active_grid(trading_mode)
        except Exception:
            logger.debug("Could not load grid state from DB")
            return False

        if not saved_grid or not saved_levels:
            return False

        symbol = saved_grid.symbol
        logger.info("Found saved grid state: %s (%d levels)", symbol, len(saved_levels))

        # Get symbol info
        raw_info = await self.exchange.get_symbol_info(symbol)
        if not raw_info:
            logger.warning("Saved grid symbol %s no longer available", symbol)
            return False
        self.symbol_info = SymbolInfo(raw_info)

        # Rebuild grid config from saved state
        config = GridConfig(
            symbol=symbol,
            upper_price=saved_grid.upper_price,
            lower_price=saved_grid.lower_price,
            num_levels=saved_grid.num_levels,
            total_capital=saved_grid.capital,
        )

        self.grid = GridEngine(config, self.symbol_info)
        self.grid.total_profit = saved_grid.total_profit
        self.grid.completed_cycles = saved_grid.completed_cycles

        # Restore level states
        level_dicts = [
            {
                "index": lv.level_index,
                "buy_price": lv.buy_price,
                "sell_price": lv.sell_price,
                "status": lv.status,
                "order_id": lv.order_id,
                "buy_fill_price": lv.buy_fill_price,
                "quantity": lv.quantity,
            }
            for lv in saved_levels
        ]
        self.grid.restore_levels(level_dicts)

        self.risk_manager = RiskManager(self.preset, config.total_capital)

        # Reconcile with Binance — check open orders are still valid
        await self._reconcile_grid(symbol)

        current_price = await self.exchange.get_symbol_price(symbol)
        logger.info("Grid restored: %s @ %.8f | profit: %.6f | cycles: %d",
                     symbol, current_price, self.grid.total_profit, self.grid.completed_cycles)

        await self.notifier.send(
            f"**Grid Restored** | {symbol} @ ${current_price:.5f}\n"
            f"Profit: ${self.grid.total_profit:.5f} | Cycles: {self.grid.completed_cycles}\n"
            f"{self.grid.get_status_summary()['level_states']}"
        )

        return True

    async def _reconcile_grid(self, symbol: str):
        """Reconcile saved grid state with actual Binance orders/balances."""
        try:
            # Check which saved order_ids are still open on Binance
            open_orders = await self.exchange._client.get_open_orders(symbol=symbol)
            open_ids = {str(o["orderId"]) for o in open_orders}

            for level in self.grid.levels:
                if level.order_id and str(level.order_id) not in open_ids:
                    # Order no longer exists on Binance
                    if level.state == LevelState.BUY_PENDING:
                        # Buy order gone — might have filled while we were down
                        # Check if we hold the coins
                        balance = await self.exchange.get_account_balance(self.symbol_info.base_asset)
                        if balance >= level.quantity and level.quantity > 0:
                            logger.info("Reconcile: level %d buy likely filled while offline, marking as holding", level.index)
                            level.state = LevelState.HOLDING
                            level.buy_fill_price = level.buy_price
                            level.order_id = None
                        else:
                            logger.info("Reconcile: level %d buy order gone, resetting to empty", level.index)
                            level.state = LevelState.EMPTY
                            level.order_id = None
                    elif level.state == LevelState.SELL_PENDING:
                        # Sell order gone — might have filled
                        logger.info("Reconcile: level %d sell order gone, marking as empty (likely filled)", level.index)
                        level.state = LevelState.EMPTY
                        level.order_id = None
                        level.buy_fill_price = None
                        level.quantity = 0.0

            # Re-place orders for levels that need them
            current_price = await self.exchange.get_symbol_price(symbol)
            for level in self.grid.levels:
                if level.state == LevelState.HOLDING and level.order_id is None:
                    # Need to place sell order
                    await self._try_place_sell(level)
                elif level.state == LevelState.EMPTY and level.buy_price <= current_price and level.order_id is None:
                    # Need to place buy order
                    await self._try_place_buy(level)

            logger.info("Grid reconciliation complete")

        except Exception:
            logger.exception("Grid reconciliation failed — continuing with saved state")

    async def _start_scalper(self, capital: float):
        """Initialize and start the scalp trading strategy."""
        try:
            # Create dedicated exchange adapter
            self.scalp_exchange = BinanceAdapter(
                api_key=self.settings.binance_api_key,
                api_secret=self.settings.binance_api_secret,
                testnet=self.settings.is_testnet,
            )
            await self.scalp_exchange.connect()

            # Select scalp symbol
            scalp_symbol = self.settings.scalp_symbol
            if not scalp_symbol:
                trade_capital = capital * (self.settings.scalp_trade_pct / 100)
                screener = ScalpScreener(
                    self.scalp_exchange,
                    min_volume_usd=self.settings.scalp_min_volume,
                    trade_capital=trade_capital,
                    trigger_pct=self.settings.scalp_trigger_pct,
                )
                candidates = await screener.screen(quote_asset="USDT", top_n=5)
                if candidates:
                    scalp_symbol = candidates[0]["symbol"]
                    logger.info("Scalp auto-selected: %s (score: %.1f)", scalp_symbol, candidates[0]["score"])
                else:
                    logger.warning("No suitable scalp pairs found")
                    return

            # Get symbol info
            raw_info = await self.scalp_exchange.get_symbol_info(scalp_symbol)
            if not raw_info:
                logger.error("Scalp symbol %s not found", scalp_symbol)
                return
            scalp_symbol_info = SymbolInfo(raw_info)

            # Create scalp engine
            mode = ScalpMode(self.settings.scalp_mode)
            self.scalp_engine = ScalpEngine(
                symbol=scalp_symbol,
                symbol_info=scalp_symbol_info,
                exchange=self.scalp_exchange,
                mode=mode,
                # Price-based (deprecated modes)
                trigger_pct=self.settings.scalp_trigger_pct,
                trigger_window=self.settings.scalp_trigger_window,
                time_limit=self.settings.scalp_time_limit,
                # Volume spike
                volume_multiplier=self.settings.scalp_volume_multiplier,
                volume_direction_pct=self.settings.scalp_volume_direction_pct,
                volume_timeout=self.settings.scalp_volume_timeout,
                false_signal_cooldown=self.settings.scalp_false_signal_cooldown,
                # Common
                tp_pct=self.settings.scalp_tp_pct,
                sl_pct=self.settings.scalp_sl_pct,
                capital=capital,
                trade_pct=self.settings.scalp_trade_pct,
                cooldown=self.settings.scalp_cooldown,
            )
            self.scalp_engine.on_trade_complete = self._on_scalp_trade_complete

            await self.notifier.send(
                f"**Scalper Started** | {scalp_symbol} | Mode: {mode.value} | Capital: ${capital:.5f}"
            )

            await self.scalp_engine.start()

            # Keep scalper alive
            while self._running:
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Scalper crashed")
        finally:
            if self.scalp_engine:
                await self.scalp_engine.stop()
            if self.scalp_exchange:
                await self.scalp_exchange.disconnect()

    async def _on_scalp_trade_complete(self, trade: ScalpTrade):
        """Handle completed scalp trade — persist and notify."""
        await self._save_trade(
            side="SELL" if trade.profit is not None else "BUY",
            price=trade.exit_price or trade.entry_price,
            qty=trade.quantity,
            order_id=trade.order_id or "",
            commission=0,
            commission_asset="",
            profit=trade.profit,
        )
        if trade.profit is not None:
            color = "profit" if trade.profit >= 0 else "loss"
            duration = (trade.exit_time - trade.entry_time) if trade.exit_time else 0
            await self.notifier.send(
                f"**Scalp {'Win' if trade.profit >= 0 else 'Loss'}** | {trade.symbol} | "
                f"Entry: ${trade.entry_price:,.4f} → Exit: ${trade.exit_price:,.4f} | "
                f"P&L: ${trade.profit:,.6f} | Reason: {trade.exit_reason} | {duration:.1f}s"
            )

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

        # Periodic re-screening for grid pair swap
        rescreen_interval = self.settings.grid_rescreen_interval * 3600
        if (self.grid and self.screener and not self._swap_in_progress
                and not self.settings.trading_symbol  # don't rescreen if symbol is manually set
                and now - self._last_rescreen >= rescreen_interval):
            self._last_rescreen = now
            await self._check_pair_swap()

    async def _check_pair_swap(self):
        """Re-screen and propose a pair swap if a better candidate is found."""
        if not self.grid or not self.screener:
            return

        current_symbol = self.grid.config.symbol
        logger.info("Re-screening grid pairs (current: %s)...", current_symbol)

        candidates = await self.screener.screen(
            quote_asset="USDT",
            top_n=5,
            num_grid_levels=self.preset.grid_levels,
        )

        if not candidates:
            return

        best = candidates[0]
        if best.symbol == current_symbol:
            logger.info("Current pair %s is still the best pick (score: %.1f)", current_symbol, best.score)
            return

        # Find current symbol's score in the new screening
        current_score = 0
        for c in candidates:
            if c.symbol == current_symbol:
                current_score = c.score
                break

        # Only propose swap if new pair scores significantly better (>10% higher)
        if best.score < current_score * 1.1:
            logger.info("New top pair %s (%.1f) not significantly better than %s (%.1f), skipping",
                         best.symbol, best.score, current_symbol, current_score)
            return

        logger.info("Better pair found: %s (%.1f) vs current %s (%.1f)",
                     best.symbol, best.score, current_symbol, current_score)

        await self._propose_swap(best.symbol, best.score, current_score)

    async def _propose_swap(self, new_symbol: str, new_score: float, current_score: float):
        """Propose a grid pair swap — notify, wait for approval or timeout."""
        self._swap_in_progress = True

        try:
            current_symbol = self.grid.config.symbol

            # Calculate cost of closing held positions
            held_levels = [l for l in self.grid.levels if l.state.value in ("holding", "sell_pending")]
            estimated_loss = 0.0
            for level in held_levels:
                if level.buy_fill_price and self._last_price > 0:
                    pnl = (self._last_price - level.buy_fill_price) * level.quantity
                    estimated_loss += pnl  # negative = loss

            grid_capital = self.grid.config.total_capital
            loss_pct = abs(estimated_loss) / grid_capital * 100 if grid_capital > 0 and estimated_loss < 0 else 0
            within_tolerance = loss_pct <= self.settings.grid_swap_loss_tolerance

            # Notify via Discord
            await self.notifier.notify_swap_proposal(
                current_symbol=current_symbol,
                new_symbol=new_symbol,
                new_score=new_score,
                current_score=current_score,
                held_positions=len(held_levels),
                estimated_loss=estimated_loss,
                loss_pct=loss_pct,
                within_tolerance=within_tolerance,
                timeout_seconds=self.settings.grid_swap_timeout,
            )

            # Create approval request
            req = self.approvals.create_request(
                "grid_swap",
                f"Swap {current_symbol} -> {new_symbol} (score {current_score:.1f} -> {new_score:.1f}). "
                f"Closing {len(held_levels)} positions, est. P&L: ${estimated_loss:,.5f} ({loss_pct:.2f}%)",
            )

            # Wait for response with timeout
            approved = await self.approvals.wait_for_decision(req, timeout=self.settings.grid_swap_timeout)

            if approved:
                logger.info("Swap approved by user")
                await self._execute_swap(new_symbol)
            elif req.approved is None:
                # Timeout — no response
                if within_tolerance:
                    logger.info("Swap timeout, auto-approving (loss %.2f%% within %.2f%% tolerance)",
                                loss_pct, self.settings.grid_swap_loss_tolerance)
                    await self._execute_swap(new_symbol)
                else:
                    logger.info("Swap timeout, NOT auto-approving (loss %.2f%% exceeds %.2f%% tolerance)",
                                loss_pct, self.settings.grid_swap_loss_tolerance)
                    await self.notifier.send(
                        f"**Swap NOT executed** — loss ({loss_pct:.2f}%) exceeds tolerance "
                        f"({self.settings.grid_swap_loss_tolerance}%). Will check again next cycle."
                    )
            else:
                logger.info("Swap rejected by user")
                await self.notifier.send(f"**Swap rejected** — staying on {current_symbol}")

        except Exception:
            logger.exception("Error during pair swap proposal")
        finally:
            self._swap_in_progress = False

    async def _execute_swap(self, new_symbol: str):
        """Execute the grid pair swap — close positions, cancel orders, rebuild grid."""
        old_symbol = self.grid.config.symbol
        sell_pnl = 0.0

        async with self._order_lock:
            # 1. Cancel all pending buy orders
            pending_ids = self.grid.cancel_all_pending()
            for oid in pending_ids:
                try:
                    await self.exchange.cancel_order(old_symbol, oid)
                except Exception:
                    logger.exception("Failed to cancel order %s during swap", oid)

            # 2. Market sell all held positions
            for level in self.grid.levels:
                if level.state.value == "holding" and level.quantity > 0:
                    try:
                        qty_str = self.symbol_info.format_quantity(level.quantity)
                        result = await self.exchange.place_market_order(
                            symbol=old_symbol, side="SELL", quantity=float(qty_str),
                        )
                        exit_price = float(result.get("fills", [{}])[0].get("price", self._last_price)) if result.get("fills") else self._last_price
                        pnl = (exit_price - level.buy_fill_price) * level.quantity if level.buy_fill_price else 0
                        sell_pnl += pnl
                        logger.info("Swap sell: level %d, qty %.8f @ %.8f, pnl: %.6f",
                                     level.index, level.quantity, exit_price, pnl)
                    except Exception:
                        logger.exception("Failed to market sell level %d during swap", level.index)

        # 3. Notify
        await self.notifier.notify_swap_executed(old_symbol, new_symbol, sell_pnl)

        # 4. Rebuild grid on new symbol
        grid_capital = self.risk_manager.available_capital if self.risk_manager else self.grid.config.total_capital
        await self._start_grid(new_symbol, grid_capital)

        logger.info("Grid swapped: %s -> %s (sell P&L: $%.5f)", old_symbol, new_symbol, sell_pnl)

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
                        f"Price {current_price:.5f} exited grid range.\n"
                        f"Approve reset around new price?",
                    )
                    req = self.approvals.create_request(
                        "grid_reset",
                        f"Price exited range. Reset grid around {current_price:.5f}?",
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
            await self._persist_grid_state()
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
            await self._persist_grid_state()
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
                        await self._persist_grid_state()

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
                        await self._persist_grid_state()

                        if self.risk_manager.is_stop_loss_triggered():
                            await self._handle_stop_loss()

        elif status == "CANCELED":
            async with self._order_lock:
                self.grid.on_order_cancelled(order_id)
                await self._persist_grid_state()

    async def _persist_grid_state(self, full_save: bool = False):
        """Save current grid state to DB. full_save=True creates new record."""
        if not self.grid:
            return
        try:
            if full_save:
                await self.trade_repo.save_grid_state(
                    symbol=self.grid.config.symbol,
                    upper_price=self.grid.config.upper_price,
                    lower_price=self.grid.config.lower_price,
                    num_levels=self.grid.config.num_levels,
                    capital=self.grid.config.total_capital,
                    trading_mode=self.settings.trading_mode.value,
                    levels=self.grid.serialize_levels(),
                    total_profit=self.grid.total_profit,
                    completed_cycles=self.grid.completed_cycles,
                )
            else:
                await self.trade_repo.update_grid_levels(
                    trading_mode=self.settings.trading_mode.value,
                    levels=self.grid.serialize_levels(),
                    total_profit=self.grid.total_profit,
                    completed_cycles=self.grid.completed_cycles,
                )
        except Exception:
            logger.exception("Failed to persist grid state to DB")

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

    async def apply_settings(self, changes: dict) -> list[str]:
        """Apply runtime settings changes. Returns list of result messages."""
        from config.presets import PRESETS
        from dataclasses import replace as dc_replace

        results = []
        needs_grid_reset = False
        needs_scalp_restart = False

        for key, value in changes.items():
            ok, msg = self.runtime.set(key, value)
            results.append(msg)
            if not ok:
                continue

            if key == "bot_mode":
                # Immediate — just update the reference
                from config.settings import BotMode
                self.settings.__dict__["bot_mode"] = BotMode(value)

            elif key == "risk_preset":
                # Update preset and flag grid reset
                self.preset = PRESETS[value]
                needs_grid_reset = True

            elif key == "grid_capital":
                needs_grid_reset = True

            elif key == "scalp_capital":
                needs_scalp_restart = True

        # Apply grid reset if needed
        if needs_grid_reset and self.grid:
            await self._partial_grid_reset()
            results.append("Grid partially reset with new parameters")

        # Apply scalp restart if needed
        if needs_scalp_restart:
            new_scalp_cap = self.runtime.get("scalp_capital")
            if new_scalp_cap > 0 and not self.scalp_engine:
                # Start scalper
                self._scalp_task = asyncio.create_task(self._start_scalper(new_scalp_cap))
                results.append("Scalper started")
            elif new_scalp_cap == 0 and self.scalp_engine:
                # Stop scalper
                if self._scalp_task and not self._scalp_task.done():
                    self._scalp_task.cancel()
                if self.scalp_engine:
                    await self.scalp_engine.stop()
                    self.scalp_engine = None
                results.append("Scalper stopped")
            elif new_scalp_cap > 0 and self.scalp_engine:
                # Restart with new capital
                if self._scalp_task and not self._scalp_task.done():
                    self._scalp_task.cancel()
                if self.scalp_engine:
                    await self.scalp_engine.stop()
                    self.scalp_engine = None
                self._scalp_task = asyncio.create_task(self._start_scalper(new_scalp_cap))
                results.append("Scalper restarted with new capital")

        # Notify via Discord
        change_summary = ", ".join(f"{k}={v}" for k, v in changes.items())
        await self.notifier.send(f"**Settings Updated** | {change_summary}")

        return results

    async def _partial_grid_reset(self):
        """Reset grid preserving sell-pending/holding levels."""
        if not self.grid:
            return

        symbol = self.grid.config.symbol
        current_price = self._last_price or await self.exchange.get_symbol_price(symbol)
        new_capital = self.runtime.get("grid_capital")

        async with self._order_lock:
            # Cancel only buy-pending orders
            for level in self.grid.levels:
                if level.state == LevelState.BUY_PENDING and level.order_id:
                    try:
                        await self.exchange.cancel_order(symbol, level.order_id)
                    except Exception:
                        logger.exception("Failed to cancel buy order %s", level.order_id)
                    level.state = LevelState.EMPTY
                    level.order_id = None

            # Count preserved levels
            preserved = [l for l in self.grid.levels if l.state in (LevelState.HOLDING, LevelState.SELL_PENDING)]
            preserved_capital = sum(l.buy_fill_price * l.quantity for l in preserved if l.buy_fill_price)

            # Rebuild grid with new params
            grid_range = current_price * self.preset.grid_range_pct
            available_capital = max(0, new_capital - preserved_capital)

            new_config = GridConfig(
                symbol=symbol,
                upper_price=current_price + grid_range,
                lower_price=current_price - grid_range,
                num_levels=self.preset.grid_levels,
                total_capital=new_capital,
            )

            # Preserve old profit/cycles
            old_profit = self.grid.total_profit
            old_cycles = self.grid.completed_cycles

            self.grid = GridEngine(new_config, self.symbol_info)
            self.grid.total_profit = old_profit
            self.grid.completed_cycles = old_cycles

            # Restore preserved levels into new grid (best-effort match by price proximity)
            for old_level in preserved:
                best_match = min(self.grid.levels, key=lambda l: abs(l.buy_price - old_level.buy_price))
                if best_match.state == LevelState.EMPTY:
                    best_match.state = old_level.state
                    best_match.order_id = old_level.order_id
                    best_match.buy_fill_price = old_level.buy_fill_price
                    best_match.quantity = old_level.quantity

            # Update risk manager
            self.risk_manager = RiskManager(self.preset, new_capital)

            # Place new buy orders for empty levels
            for level in self.grid.get_levels_to_buy(current_price):
                await self._try_place_buy(level)

            # Persist
            await self._persist_grid_state(full_save=True)

        logger.info("Partial grid reset: %d preserved levels, new capital $%.5f, %d levels",
                     len(preserved), new_capital, self.preset.grid_levels)

    async def force_sell_positions(self) -> dict:
        """Force sell all held/sell-pending positions using swap-style flow."""
        if not self.grid:
            return {"error": "No grid running"}

        held_levels = [l for l in self.grid.levels if l.state in (LevelState.HOLDING, LevelState.SELL_PENDING)]
        if not held_levels:
            return {"message": "No positions to sell"}

        # Calculate estimated P&L
        estimated_pnl = 0.0
        for level in held_levels:
            if level.buy_fill_price and self._last_price > 0:
                estimated_pnl += (self._last_price - level.buy_fill_price) * level.quantity

        grid_capital = self.runtime.get("grid_capital")
        loss_pct = abs(estimated_pnl) / grid_capital * 100 if grid_capital > 0 and estimated_pnl < 0 else 0
        tolerance = self.settings.grid_swap_loss_tolerance

        if loss_pct > tolerance:
            # Create approval request
            await self.notifier.send(
                f"**Force Sell Request** | {len(held_levels)} positions | "
                f"Est. P&L: ${estimated_pnl:,.5f} ({loss_pct:.2f}%) | "
                f"Exceeds tolerance ({tolerance}%). Approve via dashboard."
            )
            req = self.approvals.create_request(
                "force_sell",
                f"Sell {len(held_levels)} positions. Est. P&L: ${estimated_pnl:,.5f} ({loss_pct:.2f}%)",
            )
            approved = await self.approvals.wait_for_decision(req, timeout=self.settings.grid_swap_timeout)
            if not approved:
                return {"message": "Force sell rejected or timed out"}

        # Execute sells
        symbol = self.grid.config.symbol
        total_pnl = 0.0
        async with self._order_lock:
            for level in held_levels:
                # Cancel existing sell order if any
                if level.order_id:
                    try:
                        await self.exchange.cancel_order(symbol, level.order_id)
                    except Exception:
                        pass

                if level.quantity > 0:
                    try:
                        qty_str = self.symbol_info.format_quantity(level.quantity)
                        result = await self.exchange.place_market_order(
                            symbol=symbol, side="SELL", quantity=float(qty_str),
                        )
                        exit_price = float(result.get("fills", [{}])[0].get("price", self._last_price)) if result.get("fills") else self._last_price
                        pnl = (exit_price - level.buy_fill_price) * level.quantity if level.buy_fill_price else 0
                        total_pnl += pnl
                    except Exception:
                        logger.exception("Failed to force sell level %d", level.index)

                level.state = LevelState.EMPTY
                level.order_id = None
                level.buy_fill_price = None
                level.quantity = 0.0

            await self._persist_grid_state()

        await self.notifier.send(f"**Positions Force Sold** | P&L: ${total_pnl:,.5f}")
        return {"message": f"Sold {len(held_levels)} positions", "pnl": total_pnl}

    async def stop(self):
        logger.info("Stopping TradeBot")
        self._running = False

        # Stop scalper
        if self._scalp_task and not self._scalp_task.done():
            self._scalp_task.cancel()
            try:
                await self._scalp_task
            except (asyncio.CancelledError, Exception):
                pass

        if self.scalp_engine:
            scalp_status = self.scalp_engine.get_status()
            logger.info("Final scalp status: %s", scalp_status)

        # Cancel grid orders
        grid_profit = 0.0
        grid_cycles = 0
        if self.grid:
            summary = self.grid.get_status_summary()
            risk_status = self.risk_manager.get_status() if self.risk_manager else {}
            logger.info("Final grid status: %s", summary)
            logger.info("Final risk status: %s", risk_status)
            grid_profit = summary["total_profit"]
            grid_cycles = summary["completed_cycles"]
            pending_ids = self.grid.cancel_all_pending()
            for oid in pending_ids:
                try:
                    await self.exchange.cancel_order(self.grid.config.symbol, oid)
                except Exception:
                    logger.exception("Failed to cancel order %s on shutdown", oid)

        # Combined stop notification
        scalp_profit = self.scalp_engine.stats.total_profit if self.scalp_engine else 0
        scalp_trades = self.scalp_engine.stats.total_trades if self.scalp_engine else 0
        await self.notifier.notify_bot_stopped(
            total_profit=grid_profit + scalp_profit,
            net_pnl=(self.risk_manager.get_status().get("net_pnl", 0) if self.risk_manager else 0) + scalp_profit,
            cycles=grid_cycles,
        )

        await self.notifier.close()
        await self.exchange.disconnect()

    @property
    def is_running(self) -> bool:
        return self._running

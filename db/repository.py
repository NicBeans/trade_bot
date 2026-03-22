"""Trade and grid state repository — persists data to PostgreSQL."""

import logging
from datetime import datetime

from sqlalchemy import select, desc, delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Trade, GridState, GridLevelState, Base

logger = logging.getLogger(__name__)


class TradeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def init_tables(self):
        """Create tables if they don't exist."""
        from db.database import engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables initialized")

    # --- Trade methods ---

    async def save_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        order_id: str,
        trading_mode: str,
        fee: float = 0.0,
        fee_asset: str = "BNB",
        profit: float | None = None,
    ) -> Trade:
        async with self._session_factory() as session:
            trade = Trade(
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                order_id=str(order_id),
                trading_mode=trading_mode,
                fee=fee,
                fee_asset=fee_asset,
                profit=profit,
            )
            session.add(trade)
            await session.commit()
            await session.refresh(trade)
            logger.debug("Trade saved: %s %s %s @ %.4f", side, symbol, order_id, price)
            return trade

    async def get_trades(
        self,
        trading_mode: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Trade]:
        async with self._session_factory() as session:
            query = select(Trade).order_by(desc(Trade.created_at))
            if trading_mode:
                query = query.where(Trade.trading_mode == trading_mode)
            if symbol:
                query = query.where(Trade.symbol == symbol)
            query = query.limit(limit).offset(offset)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_trade_summary(self, trading_mode: str | None = None) -> dict:
        """Get aggregate trade statistics."""
        trades = await self.get_trades(trading_mode=trading_mode, limit=10000)
        total_trades = len(trades)
        total_profit = sum(t.profit or 0 for t in trades)
        total_fees = sum(t.fee for t in trades)
        buy_count = sum(1 for t in trades if t.side == "BUY")
        sell_count = sum(1 for t in trades if t.side == "SELL")
        completed_cycles = sum(1 for t in trades if t.side == "SELL" and t.profit is not None)

        return {
            "total_trades": total_trades,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "completed_cycles": completed_cycles,
            "total_profit": total_profit,
            "total_fees": total_fees,
            "net_profit": total_profit - total_fees,
        }

    # --- Grid state methods ---

    async def save_grid_state(
        self,
        symbol: str,
        upper_price: float,
        lower_price: float,
        num_levels: int,
        capital: float,
        trading_mode: str,
        levels: list[dict],
        total_profit: float = 0.0,
        completed_cycles: int = 0,
    ) -> int:
        """Save or update the active grid state. Returns grid_state_id."""
        async with self._session_factory() as session:
            # Deactivate any existing active grid for this trading mode
            await session.execute(
                update(GridState)
                .where(GridState.trading_mode == trading_mode, GridState.active == True)
                .values(active=False)
            )

            # Create new grid state
            gs = GridState(
                symbol=symbol,
                upper_price=upper_price,
                lower_price=lower_price,
                num_levels=num_levels,
                capital=capital,
                total_profit=total_profit,
                completed_cycles=completed_cycles,
                active=True,
                trading_mode=trading_mode,
            )
            session.add(gs)
            await session.flush()
            grid_id = gs.id

            # Delete old levels for this grid and save new ones
            await session.execute(
                delete(GridLevelState).where(GridLevelState.grid_state_id == grid_id)
            )

            for lv in levels:
                session.add(GridLevelState(
                    grid_state_id=grid_id,
                    level_index=lv["index"],
                    buy_price=lv["buy_price"],
                    sell_price=lv["sell_price"],
                    status=lv["status"],
                    order_id=lv.get("order_id"),
                    buy_fill_price=lv.get("buy_fill_price"),
                    quantity=lv.get("quantity", 0.0),
                ))

            await session.commit()
            logger.debug("Grid state saved: %s (%d levels)", symbol, len(levels))
            return grid_id

    async def update_grid_levels(self, trading_mode: str, levels: list[dict], total_profit: float, completed_cycles: int):
        """Update level states and profit for the active grid."""
        async with self._session_factory() as session:
            # Find active grid
            result = await session.execute(
                select(GridState).where(GridState.trading_mode == trading_mode, GridState.active == True)
            )
            gs = result.scalar_one_or_none()
            if not gs:
                return

            # Update grid totals
            gs.total_profit = total_profit
            gs.completed_cycles = completed_cycles

            # Update each level
            for lv in levels:
                await session.execute(
                    update(GridLevelState)
                    .where(
                        GridLevelState.grid_state_id == gs.id,
                        GridLevelState.level_index == lv["index"],
                    )
                    .values(
                        status=lv["status"],
                        order_id=lv.get("order_id"),
                        buy_fill_price=lv.get("buy_fill_price"),
                        quantity=lv.get("quantity", 0.0),
                    )
                )

            await session.commit()

    async def get_active_grid(self, trading_mode: str) -> tuple[GridState | None, list[GridLevelState]]:
        """Load the active grid state and its levels."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(GridState).where(GridState.trading_mode == trading_mode, GridState.active == True)
            )
            gs = result.scalar_one_or_none()
            if not gs:
                return None, []

            result = await session.execute(
                select(GridLevelState)
                .where(GridLevelState.grid_state_id == gs.id)
                .order_by(GridLevelState.level_index)
            )
            levels = list(result.scalars().all())
            return gs, levels

    async def deactivate_grid(self, trading_mode: str):
        """Mark the active grid as inactive."""
        async with self._session_factory() as session:
            await session.execute(
                update(GridState)
                .where(GridState.trading_mode == trading_mode, GridState.active == True)
                .values(active=False)
            )
            await session.commit()

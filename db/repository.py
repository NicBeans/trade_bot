"""Trade repository — persists trade data to PostgreSQL."""

import logging
from datetime import datetime

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Trade, Base

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

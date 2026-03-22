"""Database models for trade history, grid state, and P&L tracking."""

from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Boolean, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))  # BUY / SELL
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    fee_asset: Mapped[str] = mapped_column(String(10), default="BNB")
    order_id: Mapped[str] = mapped_column(String(50))
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    trading_mode: Mapped[str] = mapped_column(String(10))  # testnet / mainnet
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GridState(Base):
    __tablename__ = "grid_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    upper_price: Mapped[float] = mapped_column(Float)
    lower_price: Mapped[float] = mapped_column(Float)
    num_levels: Mapped[int] = mapped_column(Integer)
    capital: Mapped[float] = mapped_column(Float)
    total_profit: Mapped[float] = mapped_column(Float, default=0.0)
    completed_cycles: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_mode: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class GridLevelState(Base):
    __tablename__ = "grid_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    grid_state_id: Mapped[int] = mapped_column(Integer)
    level_index: Mapped[int] = mapped_column(Integer)
    buy_price: Mapped[float] = mapped_column(Float)
    sell_price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(15), default="empty")
    order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    buy_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)


class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    total_profit: Mapped[float] = mapped_column(Float, default=0.0)
    trading_mode: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

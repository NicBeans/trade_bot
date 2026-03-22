"""Exchange data models."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Ticker:
    symbol: str
    price: float
    bid: float
    ask: float
    volume_24h: float
    timestamp: datetime


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "LIMIT" or "MARKET"
    price: float
    quantity: float
    status: str  # "NEW", "FILLED", "CANCELED", etc.
    timestamp: datetime


@dataclass
class TradeResult:
    order_id: str
    symbol: str
    side: str
    price: float
    quantity: float
    fee: float
    fee_asset: str
    timestamp: datetime

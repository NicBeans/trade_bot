"""Binance API adapter — abstracts REST and WebSocket interactions."""

import asyncio
import logging
from binance import AsyncClient, BinanceSocketManager

logger = logging.getLogger(__name__)


class BinanceAdapter:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self._client: AsyncClient | None = None
        self._bsm: BinanceSocketManager | None = None
        self._price_ws_task: asyncio.Task | None = None
        self._user_ws_task: asyncio.Task | None = None
        self._shutting_down = False

    async def connect(self):
        """Initialize the Binance async client."""
        self._client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
        )
        self._bsm = BinanceSocketManager(self._client)
        mode = "testnet" if self.testnet else "mainnet"
        logger.info("Connected to Binance (%s)", mode)

    async def disconnect(self):
        """Close connections."""
        self._shutting_down = True
        # Don't cancel WS tasks — the library's __aexit__ does blocking
        # network calls that hang. The _shutting_down flag will cause
        # the loops to exit on their next iteration.
        logger.info("Disconnected from Binance")

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """Get available balance for an asset."""
        account = await self._client.get_account()
        for balance in account["balances"]:
            if balance["asset"] == asset:
                return float(balance["free"])
        return 0.0

    async def get_account_balances(self) -> dict[str, float]:
        """Get all non-zero balances."""
        account = await self._client.get_account()
        balances = {}
        for b in account["balances"]:
            free = float(b["free"])
            locked = float(b["locked"])
            if free > 0 or locked > 0:
                balances[b["asset"]] = free + locked
        return balances

    async def get_symbol_price(self, symbol: str) -> float:
        """Get current price for a symbol."""
        ticker = await self._client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    async def get_all_tickers(self) -> list[dict]:
        """Get 24h ticker data for all pairs."""
        return await self._client.get_ticker()

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 480) -> list[list]:
        """Get kline/candlestick data. Returns list of [open_time, open, high, low, close, ...]."""
        return await self._client.get_klines(symbol=symbol, interval=interval, limit=limit)

    async def get_symbol_info(self, symbol: str) -> dict | None:
        """Get trading rules for a symbol (lot size, min notional, etc.)."""
        info = await self._client.get_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return None

    async def get_all_symbol_info(self) -> dict[str, dict]:
        """Get trading rules for all symbols. Returns {symbol: info_dict}."""
        info = await self._client.get_exchange_info()
        return {s["symbol"]: s for s in info["symbols"]}

    async def place_limit_order(self, symbol: str, side: str, price: float, quantity: float) -> dict:
        """Place a limit order."""
        order = await self._client.create_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            price=str(price),
            quantity=str(quantity),
        )
        logger.info("Order placed: %s %s %s @ %s (qty: %s) -> %s",
                     side, symbol, order["orderId"], price, quantity, order["status"])
        return order

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Place a market order."""
        order = await self._client.create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=str(quantity),
        )
        logger.info("Market order: %s %s %s (qty: %s) -> %s",
                     side, symbol, order["orderId"], quantity, order["status"])
        return order

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an open order."""
        result = await self._client.cancel_order(symbol=symbol, orderId=order_id)
        logger.info("Order cancelled: %s %s", symbol, order_id)
        return result

    async def cancel_all_orders(self, symbol: str) -> list[dict]:
        """Cancel all open orders for a symbol."""
        orders = await self._client.get_open_orders(symbol=symbol)
        results = []
        for order in orders:
            result = await self.cancel_order(symbol, order["orderId"])
            results.append(result)
        return results

    async def subscribe_price_stream(self, symbol: str, callback):
        """Subscribe to real-time price updates via WebSocket.

        callback receives: {"symbol": str, "price": float, "timestamp": int}
        """
        socket_symbol = symbol.lower()

        async def _stream_loop():
            while not self._shutting_down:
                try:
                    ts = self._bsm.trade_socket(socket_symbol)
                    async with ts as stream:
                        while True:
                            msg = await stream.recv()
                            if msg is None:
                                continue
                            await callback({
                                "symbol": msg["s"],
                                "price": float(msg["p"]),
                                "quantity": float(msg["q"]),
                                "timestamp": msg["T"],
                                "side": "BUY" if msg["m"] else "SELL",
                            })
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if self._shutting_down:
                        return
                    logger.error("Price stream error: %s. Reconnecting in 5s...", e)
                    await asyncio.sleep(5)

        self._price_ws_task = asyncio.create_task(_stream_loop())
        logger.info("Subscribed to price stream: %s", symbol)

    async def subscribe_user_data(self, callback):
        """Subscribe to order fill updates via WebSocket.

        callback receives dicts with keys:
          - event: "ORDER_TRADE_UPDATE" or "executionReport"
          - symbol, side, order_id, order_status, price, quantity,
            cumulative_qty, execution_type
        """
        async def _user_stream_loop():
            while not self._shutting_down:
                try:
                    us = self._bsm.user_socket()
                    async with us as stream:
                        while True:
                            msg = await stream.recv()
                            if msg is None:
                                continue
                            event = msg.get("e")
                            if event == "executionReport":
                                await callback({
                                    "event": event,
                                    "symbol": msg["s"],
                                    "side": msg["S"],
                                    "order_id": msg["i"],
                                    "order_status": msg["X"],
                                    "execution_type": msg["x"],
                                    "price": float(msg["p"]),
                                    "last_filled_price": float(msg["L"]),
                                    "quantity": float(msg["q"]),
                                    "cumulative_qty": float(msg["z"]),
                                    "last_filled_qty": float(msg["l"]),
                                    "commission": float(msg["n"]),
                                    "commission_asset": msg.get("N", ""),
                                })
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if self._shutting_down:
                        return
                    logger.error("User data stream error: %s. Reconnecting in 5s...", e)
                    await asyncio.sleep(5)

        self._user_ws_task = asyncio.create_task(_user_stream_loop())
        logger.info("Subscribed to user data stream")

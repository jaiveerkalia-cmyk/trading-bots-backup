from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable
from common.models import Tick, Candle, OrderBook, Order, Position


class BaseExchangeAdapter(ABC):
    """
    Common interface for all exchange adapters.
    All exchange-specific code lives inside the adapter — callers
    (market_data_service, trading_engine) only ever use this interface.
    """

    # ── Capability flags — set per subclass ───────────────────────────────────
    # UI reads these to enable/disable controls that the exchange doesn't support
    supports_stop_limit: bool = True
    supports_futures: bool = False
    supports_leverage: bool = False
    supports_ws_ticker: bool = True
    supports_ws_orderbook: bool = True
    supports_ws_candles: bool = True

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.name: str = ""  # set by subclass e.g. 'binance', 'delta'

    # ── Connection lifecycle ──────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Initialise REST client and open WS connections."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean shutdown — close WS streams, release resources."""
        ...

    # ── Market data subscriptions ─────────────────────────────────────────────

    @abstractmethod
    async def subscribe_ticker(
        self,
        symbol: str,
        callback: Callable[[Tick], Awaitable[None]],
    ) -> None:
        """Stream live ticks for a canonical symbol."""
        ...

    @abstractmethod
    async def subscribe_orderbook(
        self,
        symbol: str,
        callback: Callable[[OrderBook], Awaitable[None]],
    ) -> None:
        """Stream L2 order book updates for a symbol."""
        ...

    @abstractmethod
    async def subscribe_candles(
        self,
        symbol: str,
        interval: str,
        callback: Callable[[Candle], Awaitable[None]],
    ) -> None:
        """Stream closed OHLCV candles for a symbol and interval."""
        ...

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe all streams for a symbol."""
        ...

    # ── REST: orders ──────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """
        Place a live order on the exchange.
        Returns order with exchange_order_id and updated status.
        Paper orders are never passed here — PaperEngine handles those.
        """
        ...

    @abstractmethod
    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        """Cancel a working order. Returns True on success."""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        """Fetch all working orders from the exchange."""
        ...

    @abstractmethod
    async def get_order(self, exchange_order_id: str, symbol: str) -> Order:
        """Fetch a single order by its exchange-assigned ID."""
        ...

    # ── REST: account ─────────────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch all open positions. Spot adapters return empty list."""
        ...

    @abstractmethod
    async def get_balance(self) -> dict[str, float]:
        """Returns {asset: free_balance} e.g. {'USDT': 1042.5, 'BTC': 0.12}"""
        ...

    # ── REST: futures controls ────────────────────────────────────────────────
    # Default no-ops — spot-only adapters don't need to override these

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a futures symbol."""
        return True

    async def set_margin_mode(self, symbol: str, mode: str) -> bool:
        """Set 'cross' or 'isolated' margin mode."""
        return True

    # ── Symbol normalisation ──────────────────────────────────────────────────

    @abstractmethod
    def normalize_symbol(self, raw_symbol: str) -> str:
        """
        Exchange-native -> canonical BASE/QUOTE.
        e.g. Binance 'BTCUSDT' -> 'BTC/USDT'
             Delta   'BTCUSD'  -> 'BTC/USD'
        """
        ...

    @abstractmethod
    def to_exchange_symbol(self, canonical: str) -> str:
        """
        Canonical BASE/QUOTE -> exchange-native.
        e.g. 'BTC/USDT' -> 'BTCUSDT' for Binance
        """
        ...

    # ── Instrument list ───────────────────────────────────────────────────────

    @abstractmethod
    async def get_tradable_symbols(self) -> list[str]:
        """
        Returns canonical symbols available for trading on this exchange.
        Called on startup and periodically by instrument_sync.
        """
        ...

    # ── Market data: historical OHLCV ────────────────────────────────────────

    async def fetch_ohlcv(
        self, symbol: str, interval: str, limit: int = 3
    ) -> list[list]:
        """
        Fetch historical OHLCV candles via REST.
        Returns a list of [timestamp_ms, open, high, low, close, volume] rows,
        sorted oldest-first.  The last row may be the current unclosed candle.
        Subclasses should override; default returns [] (triggers Redis fallback).
        """
        return []

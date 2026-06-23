from __future__ import annotations
import asyncio
from typing import Optional
from datetime import datetime
from common.models import OrderBook, OrderBookLevel


class L2OrderBook:
    """
    Maintains a live L2 order book for one exchange+symbol.
    Hard-capped at `depth` levels per side on every update —
    the internal dicts never grow beyond 2*depth entries.

    Used by:
      market_data_service  — builds book from WS feed, publishes to Redis
      paper_engine         — reads depth to simulate realistic fills
    """

    __slots__ = ('exchange', 'symbol', 'depth', '_bids', '_asks', '_timestamp', '_lock')

    def __init__(self, exchange: str, symbol: str, depth: int = 20):
        self.exchange   = exchange
        self.symbol     = symbol
        self.depth      = depth
        self._bids:      dict[float, float] = {}
        self._asks:      dict[float, float] = {}
        self._timestamp: Optional[datetime] = None
        self._lock       = asyncio.Lock()

    # ── Book updates ──────────────────────────────────────────────────────────

    async def apply_snapshot(
        self,
        bids: list[tuple],
        asks: list[tuple],
        timestamp: datetime,
    ) -> None:
        async with self._lock:
            self._bids = {float(p): float(q) for p, q in bids if float(q) > 0}
            self._asks = {float(p): float(q) for p, q in asks if float(q) > 0}
            self._trim()
            self._timestamp = timestamp

    async def apply_delta(
        self,
        bids: list[tuple],
        asks: list[tuple],
        timestamp: datetime,
    ) -> None:
        async with self._lock:
            for price, qty in bids:
                p, q = float(price), float(qty)
                if q == 0:
                    self._bids.pop(p, None)
                else:
                    self._bids[p] = q
            for price, qty in asks:
                p, q = float(price), float(qty)
                if q == 0:
                    self._asks.pop(p, None)
                else:
                    self._asks[p] = q
            self._trim()
            self._timestamp = timestamp

    def _trim(self) -> None:
        """
        Hard cap: keep only top N bid levels (desc price) and
        top N ask levels (asc price). Called inside lock — sync is fine.
        Avoids unbounded dict growth on high-frequency delta feeds.
        """
        if len(self._bids) > self.depth:
            self._bids = dict(
                sorted(self._bids.items(), key=lambda x: -x[0])[: self.depth]
            )
        if len(self._asks) > self.depth:
            self._asks = dict(
                sorted(self._asks.items(), key=lambda x: x[0])[: self.depth]
            )

    async def to_model(self) -> OrderBook:
        async with self._lock:
            bids = sorted(self._bids.items(), key=lambda x: -x[0])
            asks = sorted(self._asks.items(), key=lambda x:  x[0])
            return OrderBook(
                exchange=self.exchange,
                symbol=self.symbol,
                bids=[OrderBookLevel(price=p, qty=q) for p, q in bids],
                asks=[OrderBookLevel(price=p, qty=q) for p, q in asks],
                timestamp=self._timestamp or datetime.utcnow(),
            )

    # ── Read-only helpers ─────────────────────────────────────────────────────

    @property
    def best_bid(self) -> Optional[float]:
        return max(self._bids) if self._bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self._asks) if self._asks else None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb and ba else None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (ba - bb) if bb and ba else None

    def is_ready(self) -> bool:
        return self._timestamp is not None

    # ── Depth walking (used by paper_engine) ──────────────────────────────────

    def get_bids_up_to(self, qty: float) -> list[tuple[float, float]]:
        """Walk bid levels descending. For sell/short fills."""
        return self._walk(sorted(self._bids.items(), key=lambda x: -x[0]), qty)

    def get_asks_up_to(self, qty: float) -> list[tuple[float, float]]:
        """Walk ask levels ascending. For buy/long fills."""
        return self._walk(sorted(self._asks.items(), key=lambda x: x[0]), qty)

    def vwap_fill_price(self, side: str, qty: float) -> Optional[float]:
        """
        VWAP fill price for a market order of given qty.
        Returns None if depth is insufficient (< 99% fillable).
        side: 'buy' | 'sell'
        """
        levels = self.get_asks_up_to(qty) if side == 'buy' else self.get_bids_up_to(qty)
        if not levels:
            return None
        total = sum(q for _, q in levels)
        if total < qty * 0.99:
            return None
        return sum(p * q for p, q in levels) / total

    @staticmethod
    def _walk(levels: list[tuple[float, float]], target: float) -> list[tuple[float, float]]:
        result, remaining = [], target
        for price, qty in levels:
            if remaining <= 0:
                break
            fill = min(qty, remaining)
            result.append((price, fill))
            remaining -= fill
        return result

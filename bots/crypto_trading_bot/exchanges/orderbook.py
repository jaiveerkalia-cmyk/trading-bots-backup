from __future__ import annotations
import asyncio
from typing import Optional
from datetime import datetime
from common.models import OrderBook, OrderBookLevel


class L2OrderBook:
    """
    Maintains a live L2 order book for one exchange+symbol.
    Handles both full snapshot resets and incremental delta updates.

    Used by:
      market_data_service  — builds book from WS feed, publishes to Redis
      paper_engine         — reads depth to simulate realistic market fills
    """

    def __init__(self, exchange: str, symbol: str, depth: int = 20):
        self.exchange = exchange
        self.symbol = symbol
        self.depth = depth          # levels to keep per side
        self._bids: dict[float, float] = {}   # price -> qty
        self._asks: dict[float, float] = {}   # price -> qty
        self._timestamp: Optional[datetime] = None
        self._lock = asyncio.Lock()

    # ── Book updates ──────────────────────────────────────────────────────────

    async def apply_snapshot(
        self,
        bids: list[tuple],
        asks: list[tuple],
        timestamp: datetime,
    ) -> None:
        """Full reset. bids/asks: list of (price, qty). qty=0 removes level."""
        async with self._lock:
            self._bids = {float(p): float(q) for p, q in bids if float(q) > 0}
            self._asks = {float(p): float(q) for p, q in asks if float(q) > 0}
            self._timestamp = timestamp

    async def apply_delta(
        self,
        bids: list[tuple],
        asks: list[tuple],
        timestamp: datetime,
    ) -> None:
        """Incremental update. qty=0 removes that price level."""
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
            self._timestamp = timestamp

    async def to_model(self) -> OrderBook:
        """Return current book state as an OrderBook model (for Redis publish)."""
        async with self._lock:
            bids = sorted(self._bids.items(), key=lambda x: -x[0])[: self.depth]
            asks = sorted(self._asks.items(), key=lambda x: x[0])[: self.depth]
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
        return max(self._bids.keys()) if self._bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self._asks.keys()) if self._asks else None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb and ba else None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (ba - bb) if bb and ba else None

    def is_ready(self) -> bool:
        """True once at least one snapshot has been applied."""
        return self._timestamp is not None

    # ── Depth walking (used by paper_engine) ──────────────────────────────────

    def get_bids_up_to(self, qty: float) -> list[tuple[float, float]]:
        """Walk bid levels descending, collecting up to qty. For sell fills."""
        levels = sorted(self._bids.items(), key=lambda x: -x[0])
        return self._walk_levels(levels, qty)

    def get_asks_up_to(self, qty: float) -> list[tuple[float, float]]:
        """Walk ask levels ascending, collecting up to qty. For buy fills."""
        levels = sorted(self._asks.items(), key=lambda x: x[0])
        return self._walk_levels(levels, qty)

    def vwap_fill_price(self, side: str, qty: float) -> Optional[float]:
        """
        Compute VWAP fill price for a market order of given qty.
        Returns None if book is empty or depth is insufficient (< 99% fill).
        side: 'buy' (walks asks) | 'sell' (walks bids)
        """
        levels = self.get_asks_up_to(qty) if side == "buy" else self.get_bids_up_to(qty)
        if not levels:
            return None
        total_qty = sum(q for _, q in levels)
        if total_qty < qty * 0.99:
            return None  # insufficient depth
        return sum(p * q for p, q in levels) / total_qty

    @staticmethod
    def _walk_levels(
        levels: list[tuple[float, float]], target_qty: float
    ) -> list[tuple[float, float]]:
        result = []
        remaining = target_qty
        for price, qty in levels:
            if remaining <= 0:
                break
            fill = min(qty, remaining)
            result.append((price, fill))
            remaining -= fill
        return result

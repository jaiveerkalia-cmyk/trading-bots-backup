"""
Slippage-aware paper fill simulation.
Uses live order book depth from Redis for market orders.
Maintains paper positions in memory — bounded to active slots only.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from common.models import Order, Position, OrderBookLevel, OrderBook
from common import redis_keys, settings

logger = logging.getLogger('paper_engine')


class PaperEngine:
    __slots__ = ('_redis', '_positions', '_lock')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis     = redis_client
        self._positions: dict[str, Position] = {}   # slot_id -> Position
        self._lock      = asyncio.Lock()

    async def fill_order(self, order: Order) -> Order:
        """Simulate fill. Returns order with status='filled' and avg_fill_price set."""
        price = await self._fill_price(order)
        order.exchange_order_id = f"PAPER-{order.id[:8]}"
        order.status            = 'filled'
        order.filled_qty        = order.qty
        order.avg_fill_price    = price
        order.updated_at        = datetime.now(timezone.utc)
        logger.info(f"[PAPER] {order.side.upper()} {order.qty} {order.symbol} @ {price:.6f}")
        return order

    async def _fill_price(self, order: Order) -> float:
        # Limit / stop-limit: fill at the requested price
        if order.order_type in ('limit', 'stop_limit') and order.price:
            return order.price

        # Market: VWAP walk through live order book
        book = await self._fetch_book(order.exchange, order.symbol)
        if book:
            levels = (
                [(l.price, l.qty) for l in book.asks]
                if order.side == 'buy'
                else [(l.price, l.qty) for l in book.bids]
            )
            total_qty = total_val = 0.0
            for price, qty in levels:
                take       = min(qty, order.qty - total_qty)
                total_qty += take
                total_val += price * take
                if total_qty >= order.qty * 0.99:
                    break
            if total_qty > 0:
                return round(total_val / total_qty, 8)

        # Last resort: latest tick price
        return await self._last_tick(order.exchange, order.symbol)

    async def _fetch_book(self, exchange: str, symbol: str) -> Optional[OrderBook]:
        try:
            raw = await self._redis.get(redis_keys.latest_depth_key(exchange, symbol))
            if not raw:
                return None
            d = json.loads(raw)
            return OrderBook(
                exchange=exchange,
                symbol=symbol,
                bids=[OrderBookLevel(price=b[0], qty=b[1]) for b in d.get('bids', [])],
                asks=[OrderBookLevel(price=a[0], qty=a[1]) for a in d.get('asks', [])],
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning(f"Book fetch error [{exchange} {symbol}]: {e}")
            return None

    async def _last_tick(self, exchange: str, symbol: str) -> float:
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            if raw:
                return float(json.loads(raw).get('p', 0))
        except Exception:
            pass
        logger.warning(f"No price data for {exchange} {symbol} — fill price = 0")
        return 0.0

    async def open_position(self, slot_id: str, order: Order, side: str) -> Position:
        pos = Position(
            exchange=order.exchange,
            symbol=order.symbol,
            side=side,
            entry_price=order.avg_fill_price or 0.0,
            current_price=order.avg_fill_price or 0.0,
            qty=order.filled_qty,
            is_paper=True,
            slot_id=slot_id,
        )
        async with self._lock:
            self._positions[slot_id] = pos
        return pos

    async def close_position(self, slot_id: str) -> Optional[Position]:
        async with self._lock:
            return self._positions.pop(slot_id, None)

    def get_position(self, slot_id: str) -> Optional[Position]:
        return self._positions.get(slot_id)

    async def update_mark_prices(
        self, exchange: str, symbol: str, price: float
    ) -> None:
        """Update unrealised PnL for all matching paper positions."""
        async with self._lock:
            for pos in self._positions.values():
                if pos.exchange == exchange and pos.symbol == symbol:
                    pos.current_price = price
                    pos.unrealized_pnl = (
                        (price - pos.entry_price) * pos.qty
                        if pos.side == 'long'
                        else (pos.entry_price - price) * pos.qty
                    )

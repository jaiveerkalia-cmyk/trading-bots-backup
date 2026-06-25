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


def _fee_rate(exchange: str, order_type: str) -> float:
    fees = settings.EXCHANGE_FEES.get(exchange, {'maker': 0.001, 'taker': 0.001})
    return fees['maker'] if order_type == 'limit' else fees['taker']


def _is_futures(exchange: str) -> bool:
    return 'futures' in exchange


class PaperEngine:
    __slots__ = ('_redis', '_positions', '_pending_limits', '_lock')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis          = redis_client
        self._positions:      dict[str, Position] = {}
        self._pending_limits: list[dict]          = []
        self._lock           = asyncio.Lock()

    async def fill_order(self, order: Order) -> Order:
        if order.order_type == 'market':
            price = await self._market_fill_price(order)
            return self._do_fill(order, price)
        current = await self._reference_price(order.exchange, order.symbol)
        if self._is_fillable(order, current):
            return self._do_fill(order, order.price or current)
        order.exchange_order_id = f"PAPER-{order.id[:8]}"
        order.status            = 'working'
        order.updated_at        = datetime.now(timezone.utc)
        async with self._lock:
            self._pending_limits.append({
                'order':    order,
                'exchange': order.exchange,
                'symbol':   order.symbol,
            })
        logger.info(f"[PAPER] Limit working: {order.side} {order.qty} {order.symbol} @ {order.price}")
        return order

    def _is_fillable(self, order: Order, price: float) -> bool:
        if not price:
            return False
        if order.order_type == 'limit':
            return (
                (order.side == 'buy'  and price <= (order.price or 0)) or
                (order.side == 'sell' and price >= (order.price or 0))
            )
        if order.order_type == 'stop_limit':
            return (
                (order.side == 'buy'  and price >= (order.stop_price or 0)) or
                (order.side == 'sell' and price <= (order.stop_price or 0))
            )
        return False

    def _do_fill(self, order: Order, price: float) -> Order:
        order.exchange_order_id = f"PAPER-{order.id[:8]}"
        order.status            = 'filled'
        order.filled_qty        = order.qty
        order.avg_fill_price    = round(price, 8)
        order.updated_at        = datetime.now(timezone.utc)
        logger.info(f"[PAPER] FILLED {order.side.upper()} {order.qty} {order.symbol} @ {price:.6f}")
        return order

    async def _market_fill_price(self, order: Order) -> float:
        """
        Futures market orders fill at mark price.
        Spot market orders fill at VWAP through order book.
        """
        if _is_futures(order.exchange):
            try:
                raw = await self._redis.get(
                    redis_keys.mark_price_key(order.exchange, order.symbol)
                )
                if raw:
                    return float(json.loads(raw).get('p', 0))
            except Exception:
                pass
        # Spot: VWAP
        book = await self._fetch_book(order.exchange, order.symbol)
        if book:
            levels = (
                [(l.price, l.qty) for l in book.asks] if order.side == 'buy'
                else [(l.price, l.qty) for l in book.bids]
            )
            tq = tv = 0.0
            for p, q in levels:
                take = min(q, order.qty - tq)
                tq  += take
                tv  += p * take
                if tq >= order.qty * 0.99:
                    break
            if tq > 0:
                return round(tv / tq, 8)
        return await self._last_tick(order.exchange, order.symbol)

    async def _reference_price(self, exchange: str, symbol: str) -> float:
        """Price used to check if limit orders are immediately fillable."""
        return await self._last_tick(exchange, symbol)

    async def check_pending_fills(self, exchange: str, symbol: str, price: float) -> list[Order]:
        if not self._pending_limits:
            return []
        filled: list[Order] = []
        remaining: list[dict] = []
        async with self._lock:
            for item in self._pending_limits:
                if item['exchange'] != exchange or item['symbol'] != symbol:
                    remaining.append(item)
                    continue
                order = item['order']
                if self._is_fillable(order, price):
                    self._do_fill(order, order.price or price)
                    filled.append(order)
                else:
                    remaining.append(item)
            self._pending_limits = remaining
        return filled

    def cancel_pending(self, order_id: str) -> bool:
        before = len(self._pending_limits)
        self._pending_limits = [
            i for i in self._pending_limits if i['order'].id != order_id
        ]
        return len(self._pending_limits) < before

    async def modify_pending(self, order_id: str, new_price: float, new_qty: float) -> bool:
        """Modify a working paper limit order."""
        async with self._lock:
            for item in self._pending_limits:
                if item['order'].id == order_id:
                    o = item['order']
                    if new_price > 0: o.price = new_price
                    if new_qty   > 0: o.qty   = new_qty
                    o.updated_at = datetime.now(timezone.utc)
                    logger.info(f"[PAPER] Modified: {o.symbol} @ {o.price}")
                    return True
        return False

    async def _fetch_book(self, exchange: str, symbol: str) -> Optional[OrderBook]:
        try:
            raw = await self._redis.get(redis_keys.latest_depth_key(exchange, symbol))
            if not raw:
                return None
            d = json.loads(raw)
            return OrderBook(
                exchange=exchange, symbol=symbol,
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
        return 0.0

    async def open_position(self, slot_id: str, order: Order, side: str) -> Position:
        rate      = _fee_rate(order.exchange, order.order_type)
        entry_fee = (order.avg_fill_price or 0) * order.filled_qty * rate
        pos = Position(
            exchange=order.exchange, symbol=order.symbol, side=side,
            entry_price=order.avg_fill_price or 0.0,
            current_price=order.avg_fill_price or 0.0,
            qty=order.filled_qty,
            unrealized_pnl=-entry_fee,
            is_paper=True, slot_id=slot_id,
        )
        async with self._lock:
            self._positions[slot_id] = pos
        return pos

    async def close_position(self, slot_id: str) -> Optional[Position]:
        async with self._lock:
            return self._positions.pop(slot_id, None)

    def get_position(self, slot_id: str) -> Optional[Position]:
        return self._positions.get(slot_id)

    async def update_mark_prices(self, exchange: str, symbol: str, price: float) -> None:
        """
        For futures: price = mark price (from trigger_engine._price which uses mark_price_key).
        For spot:    price = last trade price.
        """
        taker_fee = settings.EXCHANGE_FEES.get(exchange, {'taker': 0.001})['taker']
        async with self._lock:
            for pos in self._positions.values():
                if pos.exchange == exchange and pos.symbol == symbol:
                    pos.current_price   = price
                    entry_fee_rate      = _fee_rate(exchange, 'limit')
                    entry_fee           = pos.entry_price * pos.qty * entry_fee_rate
                    exit_fee            = price           * pos.qty * taker_fee
                    gross               = (
                        (price - pos.entry_price) * pos.qty if pos.side == 'long'
                        else (pos.entry_price - price) * pos.qty
                    )
                    pos.unrealized_pnl  = round(gross - entry_fee - exit_fee, 4)

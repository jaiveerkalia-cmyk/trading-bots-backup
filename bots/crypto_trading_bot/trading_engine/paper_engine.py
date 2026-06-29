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
    return 'futures' in exchange or exchange == 'delta'


class PaperEngine:
    __slots__ = ('_redis', '_positions', '_pending_limits', '_lock', '_last_funding_dt')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis          = redis_client
        self._positions:      dict[str, Position] = {}
        self._pending_limits: list[dict]          = []
        self._lock           = asyncio.Lock()
        self._last_funding_dt: dict[str, datetime] = {}

    async def fill_order(self, order: Order) -> Order:
        if order.order_type == 'market':
            price = await self._market_fill_price(order)
            return self._do_fill(order, price)
        current = await self._reference_price(order.exchange, order.symbol)
        # was: return self._do_fill(order, order.price or current)
        if self._is_fillable(order, current):
            fill_price = (
                current                   # stop-market: fill at current price
                if order.order_type == 'stop_limit'
                else (order.price or current)   # limit: fill at limit price
            )
            return self._do_fill(order, fill_price)
        # Working limit order
        order.exchange_order_id = f"PAPER-{order.id[:8]}"
        order.status            = 'working'
        order.updated_at        = datetime.now(timezone.utc)
        async with self._lock:
            self._pending_limits.append({
                'order':    order,
                'exchange': order.exchange,
                'symbol':   order.symbol,
            })
        logger.info(
            f"[PAPER] Limit working: {order.side} {order.qty} "
            f"{order.symbol} @ {order.price}"
        )
        return order

    async def _market_fill_price(self, order: Order) -> float:
        """
        Futures: mark price.
        Spot: VWAP through order book.
        Falls back to reference_price supplied by UI if no live data yet.
        """
        if _is_futures(order.exchange):
            try:
                raw = await self._redis.get(
                    redis_keys.mark_price_key(order.exchange, order.symbol)
                )
                if raw:
                    p = float(json.loads(raw).get('p', 0))
                    if p > 0:
                        return p
            except Exception:
                pass

        # Spot VWAP
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

        # Last trade price
        p = await self._last_tick(order.exchange, order.symbol)
        if p > 0:
            return p

        # Last resort: reference price sent from UI at time of order submission
        if order.reference_price and order.reference_price > 0:
            logger.warning(
                f"[PAPER] No live price for {order.symbol} — using UI reference "
                f"{order.reference_price}"
            )
            return order.reference_price

        return 0.0

    async def _reference_price(self, exchange: str, symbol: str) -> float:
        """Price used to check if limit is immediately fillable."""
        return await self._last_tick(exchange, symbol)

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
        logger.info(
            f"[PAPER] FILLED {order.side.upper()} {order.qty} "
            f"{order.symbol} @ {price:.6f}"
        )
        return order

    async def check_pending_fills(
        self, exchange: str, symbol: str, price: float
    ) -> list[Order]:
        if not self._pending_limits:
            return []
        filled: list[Order] = []
        remaining: list[dict] = []
        async with self._lock:
            for item in self._pending_limits:
                if item['exchange'] != exchange or item['symbol'] != symbol:
                    remaining.append(item)
                    continue
                o = item['order']
                # was: self._do_fill(o, o.price or price)
                if self._is_fillable(o, price):
                    fill_price = price if o.order_type == 'stop_limit' else (o.price or price)
                    self._do_fill(o, fill_price)
                    filled.append(o)
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

    async def modify_pending(
        self, order_id: str, new_price: float, new_qty: float
    ) -> bool:
        async with self._lock:
            for item in self._pending_limits:
                if item['order'].id == order_id:
                    o = item['order']
                    if new_price > 0: o.price = new_price
                    if new_qty   > 0: o.qty   = new_qty
                    o.updated_at = datetime.now(timezone.utc)
                    return True
        return False

    async def open_position(
        self, slot_id: str, order: Order, side: str
    ) -> Position:
        rate      = _fee_rate(order.exchange, order.order_type)
        entry_fee = (order.avg_fill_price or 0) * order.filled_qty * rate
        pos = Position(
            exchange=order.exchange,
            symbol=order.symbol,
            side=side,
            entry_price=order.avg_fill_price or 0.0,
            current_price=order.avg_fill_price or 0.0,
            qty=order.filled_qty,
            unrealized_pnl=-entry_fee,
            entry_fee_paid=entry_fee,
            is_paper=True,
            slot_id=slot_id,
        )
        async with self._lock:
            self._positions[slot_id] = pos
        return pos

    async def add_to_position(self, slot_id: str, order: Order) -> Position:
        """
        Scale into an existing paper position.
        Recalculates weighted-average entry price and accumulates fees.
        """
        async with self._lock:
            pos = self._positions.get(slot_id)
            if not pos:
                # No existing position — create fresh
                return await self.open_position(slot_id, order, order.side)

            rate          = _fee_rate(order.exchange, order.order_type)
            new_entry_fee = (order.avg_fill_price or 0) * order.filled_qty * rate
            new_total_qty = pos.qty + order.filled_qty

            # Weighted-average entry
            new_avg_entry = (
                pos.entry_price * pos.qty
                + (order.avg_fill_price or 0) * order.filled_qty
            ) / new_total_qty

            pos.entry_price    = round(new_avg_entry, 8)
            pos.qty            = new_total_qty
            pos.entry_fee_paid += new_entry_fee

            # Recalculate unrealised PnL
            taker_fee = settings.EXCHANGE_FEES.get(
                pos.exchange, {'taker': 0.001}
            )['taker']
            exit_fee = pos.current_price * pos.qty * taker_fee
            gross = (
                (pos.current_price - pos.entry_price) * pos.qty
                if pos.side == 'long'
                else (pos.entry_price - pos.current_price) * pos.qty
            )
            pos.unrealized_pnl = round(gross - pos.entry_fee_paid - exit_fee, 6)
            return pos
        
    async def close_position(self, slot_id: str) -> Optional[Position]:
        async with self._lock:
            return self._positions.pop(slot_id, None)

    async def partial_close_position(
        self,
        slot_id:   str,
        close_qty: float,
    ) -> tuple['Position | None', float]:
        """
        Partially close a paper position.

        Returns (updated_position, realized_pnl).
        Returns (None, realized_pnl) when the position is fully exhausted.
        """
        async with self._lock:
            pos = self._positions.get(slot_id)
            if not pos:
                return None, 0.0

            close_qty = min(close_qty, pos.qty)
            frac      = close_qty / pos.qty if pos.qty > 0 else 1.0

            taker_fee         = settings.EXCHANGE_FEES.get(
                pos.exchange, {'taker': 0.001}
            )['taker']
            entry_fee_partial = pos.entry_fee_paid * frac
            exit_fee          = pos.current_price * close_qty * taker_fee

            gross = (
                (pos.current_price - pos.entry_price) * close_qty
                if pos.side == 'long'
                else (pos.entry_price - pos.current_price) * close_qty
            )
            realized = round(gross - entry_fee_partial - exit_fee, 6)

            remaining = round(pos.qty - close_qty, 8)
            if remaining <= 1e-10:
                self._positions.pop(slot_id, None)
                return None, realized

            # Reduce position
            pos.qty            -= close_qty
            pos.entry_fee_paid -= entry_fee_partial

            # Recalculate unrealized PnL for remaining qty
            rem_exit_fee = pos.current_price * pos.qty * taker_fee
            gross_rem = (
                (pos.current_price - pos.entry_price) * pos.qty
                if pos.side == 'long'
                else (pos.entry_price - pos.current_price) * pos.qty
            )
            pos.unrealized_pnl = round(
                gross_rem - pos.entry_fee_paid - rem_exit_fee, 6
            )
        return pos, realized
    
    def get_position(self, slot_id: str) -> Optional[Position]:
        return self._positions.get(slot_id)

    async def update_mark_prices(
        self,
        exchange:     str,
        symbol:       str,
        price:        float,
        funding_rate: float = 0.0,
    ) -> None:
        """
        Called every tick. Updates current_price, applies 8-hourly funding charges
        (Binance Futures rules: longs pay positive funding, shorts receive it),
        and recalculates unrealized PnL.
        """
        async with self._lock:
            now          = datetime.now(timezone.utc)
            # Current 8-hour funding window: 00:00, 08:00, or 16:00 UTC
            funding_hour = (now.hour // 8) * 8
            cur_window   = now.replace(
                hour=funding_hour, minute=0, second=0, microsecond=0
            )
            for slot_id, pos in self._positions.items():
                if pos.exchange != exchange or pos.symbol != symbol:
                    continue
                pos.current_price = price
                # ── Apply funding when entering a new 8-hour window ───────
                fund_key  = f"{exchange}:{symbol}:{slot_id}"
                last_fund = self._last_funding_dt.get(fund_key)
                if (funding_rate != 0 and price > 0 and pos.qty > 0 and
                        (last_fund is None or cur_window > last_fund)):
                    notional = pos.qty * price
                    fee      = notional * abs(funding_rate)
                    # Binance rule:
                    # +ve funding_rate → longs pay, shorts receive
                    # -ve funding_rate → longs receive, shorts pay
                    if (pos.side == 'long'  and funding_rate > 0) or \
                       (pos.side == 'short' and funding_rate < 0):
                        pos.funding_pnl -= round(fee, 8)
                    else:
                        pos.funding_pnl += round(fee, 8)
                    self._last_funding_dt[fund_key] = cur_window
                    logger.debug(
                        "Funding [%s %s] side=%s rate=%.4f%% fee=%.4f",
                        symbol[:8], slot_id[:6], pos.side,
                        funding_rate * 100, fee,
                    )
                # ── Recalculate unrealised PnL ────────────────────────────
                taker_fee = settings.EXCHANGE_FEES.get(
                    exchange, {'taker': 0.001}
                )['taker']
                exit_fee = price * pos.qty * taker_fee
                gross = (
                    (price - pos.entry_price) * pos.qty if pos.side == 'long'
                    else (pos.entry_price - price) * pos.qty
                )
                pos.unrealized_pnl = round(
                    gross - pos.entry_fee_paid - exit_fee + pos.funding_pnl, 6
                )

    async def _fetch_book(
        self, exchange: str, symbol: str
    ) -> Optional[OrderBook]:
        try:
            raw = await self._redis.get(
                redis_keys.latest_depth_key(exchange, symbol)
            )
            if not raw:
                return None
            d = json.loads(raw)
            return OrderBook(
                exchange=exchange, symbol=symbol,
                bids=[OrderBookLevel(price=b[0], qty=b[1])
                      for b in d.get('bids', [])],
                asks=[OrderBookLevel(price=a[0], qty=a[1])
                      for a in d.get('asks', [])],
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning(f"Book fetch error [{exchange} {symbol}]: {e}")
            return None

    async def _last_tick(self, exchange: str, symbol: str) -> float:
        try:
            raw = await self._redis.get(
                redis_keys.latest_tick_key(exchange, symbol)
            )
            if raw:
                return float(json.loads(raw).get('p', 0))
        except Exception:
            pass
        return 0.0

"""
Routes orders to live exchange adapter or paper engine.
Handles native SL/TP placement where exchange supports it.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from exchanges.base import BaseExchangeAdapter
from common.models import Order, TradeSlot

if TYPE_CHECKING:
    from trading_engine.paper_engine import PaperEngine
    from trading_engine.csv_writer import CSVWriter
    from trading_engine.state_publisher import StatePublisher

logger = logging.getLogger('order_manager')


class OrderManager:
    __slots__ = ('_adapters', '_paper', '_csv', '_state', '_live_mode')

    def __init__(
        self,
        adapters:        dict[str, BaseExchangeAdapter],
        paper_engine:    'PaperEngine',
        csv_writer:      'CSVWriter',
        state_publisher: 'StatePublisher',
        live_mode:       bool = False,
    ):
        self._adapters  = adapters
        self._paper     = paper_engine
        self._csv       = csv_writer
        self._state     = state_publisher
        self._live_mode = live_mode

    def set_live_mode(self, live: bool) -> None:
        self._live_mode = live
        self._state.set_live_mode(live)

    @property
    def paper(self) -> 'PaperEngine':
        return self._paper

    @property
    def adapters(self) -> dict[str, BaseExchangeAdapter]:
        return self._adapters

    async def place_order(self, order: Order, slot: TradeSlot) -> Order:
        order.is_paper = not self._live_mode
        order.slot_id  = slot.id

        if not self._live_mode:
            order = await self._paper.fill_order(order)
        else:
            adapter = self._adapters.get(order.exchange)
            if not adapter:
                logger.error(f"No adapter: {order.exchange}")
                order.status = 'rejected'
                return order
            order = await adapter.place_order(order)

        if order.status == 'filled':
            await self._record_fill(order, slot)

        mode = 'LIVE' if self._live_mode else 'PAPER'
        lvl  = 'info' if order.status in ('filled', 'working') else 'error'
        self._state.log(
            f"[{mode}] {order.side.upper()} {order.qty} {order.symbol} "
            f"@ {order.avg_fill_price or order.price or 'MKT'} — {order.status}",
            level=lvl, exchange=order.exchange, symbol=order.symbol,
        )
        return order

    async def cancel_order(self, order: Order) -> bool:
        if order.is_paper:
            order.status     = 'cancelled'
            order.updated_at = datetime.now(timezone.utc)
            return True
        adapter = self._adapters.get(order.exchange)
        if not adapter or not order.exchange_order_id:
            return False
        success = await adapter.cancel_order(order.exchange_order_id, order.symbol)
        if success:
            order.status     = 'cancelled'
            order.updated_at = datetime.now(timezone.utc)
        return success

    async def place_native_sl_tp(self, slot: TradeSlot) -> None:
        """
        Place native exchange stop/target orders after entry fill.
        TriggerEngine provides software fallback if exchange doesn't support them.
        """
        if not slot.position or not self._live_mode:
            return
        adapter = self._adapters.get(slot.exchange)
        if not adapter:
            return

        close_side = 'sell' if slot.side == 'long' else 'buy'

        if slot.stop_price and adapter.supports_stop_limit:
            sl = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side=close_side, order_type='stop_limit',
                stop_price=slot.stop_price, price=slot.stop_price,
                qty=slot.position.qty, slot_id=slot.id,
            )
            sl = await adapter.place_order(sl)
            if sl.status != 'rejected':
                slot.sl_order_id = sl.exchange_order_id
                slot.orders.append(sl)
                self._state.log(f"Native SL @ {slot.stop_price}",
                                exchange=slot.exchange, symbol=slot.symbol)

        if slot.target_price:
            tp = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side=close_side, order_type='limit',
                price=slot.target_price,
                qty=slot.position.qty, slot_id=slot.id,
            )
            tp = await adapter.place_order(tp)
            if tp.status != 'rejected':
                slot.target_order_id = tp.exchange_order_id
                slot.orders.append(tp)
                self._state.log(f"Native TP @ {slot.target_price}",
                                exchange=slot.exchange, symbol=slot.symbol)

    async def _record_fill(self, order: Order, slot: TradeSlot) -> None:
        await self._csv.enqueue_trade({
            'timestamp':   datetime.now(timezone.utc).isoformat(),
            'exchange':    order.exchange,
            'symbol':      order.symbol,
            'side':        order.side,
            'order_type':  order.order_type,
            'qty':         order.filled_qty,
            'entry_price': order.avg_fill_price,
            'exit_price':  '',
            'pnl':         '',
            'is_paper':    order.is_paper,
            'slot_id':     slot.id,
        })

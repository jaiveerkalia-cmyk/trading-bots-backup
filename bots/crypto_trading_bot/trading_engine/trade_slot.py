from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from common.models import TradeSlot, Order, Position, Alert

logger = logging.getLogger('slot_manager')

_PERSIST_KEY = 'engine:slots_persist'


class SlotManager:
    __slots__ = ('_redis', '_slots', '_alerts', '_history', '_lock')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis:   aioredis.Redis       = redis_client
        self._slots:   dict[str, TradeSlot] = {}
        self._alerts:  list[Alert]          = []
        self._history: list[Order]          = []
        self._lock     = asyncio.Lock()

    async def load(self) -> None:
        try:
            raw = await self._redis.get(_PERSIST_KEY)
            if raw:
                for d in json.loads(raw):
                    try:
                        slot = TradeSlot.model_validate(d)
                        self._slots[slot.id] = slot
                    except Exception as e:
                        logger.warning(f"Slot restore failed: {e}")
                logger.info(f"Restored {len(self._slots)} slot(s)")
        except Exception as e:
            logger.error(f"Slot load error: {e}")

    async def _persist(self) -> None:
        try:
            await self._redis.set(
                _PERSIST_KEY,
                json.dumps(
                    [s.model_dump(mode='json') for s in self._slots.values()],
                    separators=(',', ':'), default=str,
                ),
            )
        except Exception as e:
            logger.error(f"Slot persist error: {e}")

    async def create_slot(self, slot: TradeSlot) -> TradeSlot:
        async with self._lock:
            self._slots[slot.id] = slot
        await self._persist()
        await self._pub_market_data(slot, subscribe=True)
        logger.info(f"Slot created: {slot.id[:8]} {slot.exchange} {slot.symbol} {slot.side}")
        return slot

    async def update_slot(self, slot: TradeSlot) -> None:
        async with self._lock:
            self._slots[slot.id] = slot
        await self._persist()

    async def close_slot(self, slot_id: str, realized_pnl: float = 0.0) -> Optional[TradeSlot]:
        async with self._lock:
            slot = self._slots.get(slot_id)
            if not slot:
                return None
            slot.status       = 'closed'
            slot.closed_at    = datetime.now(timezone.utc)
            slot.realized_pnl = realized_pnl
            slot.position     = None
            seen = {o.id for o in self._history}
            for o in slot.orders:
                if o.status in ('filled', 'cancelled', 'rejected') and o.id not in seen:
                    self._history.append(o)
            if len(self._history) > 1000:
                self._history = self._history[-1000:]
        await self._persist()
        logger.info(f"Slot closed: {slot_id[:8]} pnl={realized_pnl:.4f}")
        return slot

    def get_slot(self, slot_id: str) -> Optional[TradeSlot]:
        return self._slots.get(slot_id)

    def get_all_slots(self)     -> list[TradeSlot]: return list(self._slots.values())
    def get_active_slots(self)  -> list[TradeSlot]: return [s for s in self._slots.values() if s.status == 'active']
    def get_all_positions(self) -> list[Position]:  return [s.position for s in self._slots.values() if s.position]

    def get_order_history(self) -> list[Order]:
        """Returns all filled/cancelled orders, newest first."""
        history = list(self._history)
        seen    = {o.id for o in history}
        # Include filled orders from ALL slots (active + closed)
        for slot in self._slots.values():
            for o in slot.orders:
                if o.status in ('filled', 'cancelled', 'rejected') and o.id not in seen:
                    history.append(o)
                    seen.add(o.id)
        return sorted(history, key=lambda o: o.created_at, reverse=True)

    def get_open_orders(self) -> list[Order]:
        """Returns working orders + virtual stop/target orders for active paper slots."""
        orders = [
            o for s in self._slots.values()
            for o in s.orders
            if o.status in ('pending', 'working')
        ]
        # Add virtual trigger orders for active paper slots so they're visible in UI
        for slot in self.get_active_slots():
            if not slot.position or not slot.is_paper:
                continue
            close_side = 'sell' if slot.side == 'long' else 'buy'
            if slot.stop_price and not slot.sl_order_id:
                orders.append(Order(
                    id=f"VSTOP-{slot.id[:8]}",
                    exchange=slot.exchange, symbol=slot.symbol,
                    side=close_side, order_type='stop_limit',
                    stop_price=slot.stop_price, price=slot.stop_price,
                    qty=slot.position.qty,
                    status='working', is_paper=True, slot_id=slot.id,
                ))
            if slot.target_price and not slot.target_order_id:
                orders.append(Order(
                    id=f"VTGT-{slot.id[:8]}",
                    exchange=slot.exchange, symbol=slot.symbol,
                    side=close_side, order_type='limit',
                    price=slot.target_price,
                    qty=slot.position.qty,
                    status='working', is_paper=True, slot_id=slot.id,
                ))
        return orders

    def add_alert(self, alert: Alert)     -> None: self._alerts.append(alert)
    def delete_alert(self, aid: str)      -> None: self._alerts = [a for a in self._alerts if a.id != aid]
    def get_alerts(self)                  -> list[Alert]: return self._alerts
    def clear_triggered_alerts(self)      -> None: self._alerts = [a for a in self._alerts if not a.triggered]

    async def _pub_market_data(self, slot: TradeSlot, subscribe: bool) -> None:
        cmd = {
            'cmd':      'subscribe' if subscribe else 'unsubscribe',
            'exchange': slot.exchange,
            'symbol':   slot.symbol,
            'streams':  ['ticker'],
        }
        try:
            await self._redis.publish('market_data:control', json.dumps(cmd))
        except Exception as e:
            logger.error(f"Market data notify error: {e}")

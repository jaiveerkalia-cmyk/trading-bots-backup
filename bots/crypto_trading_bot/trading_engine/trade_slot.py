from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

from common.models import TradeSlot, Order, Position, Alert
from common import settings

logger = logging.getLogger('slot_manager')

_PERSIST_KEY  = 'engine:slots_persist'
_ALERTS_KEY   = 'engine:alerts_persist'
_BACKUP_SLOTS = settings.STATE_DIR / 'slots.json'
_BACKUP_ALERTS = settings.STATE_DIR / 'alerts.json'


def _write_atomic(path: Path, data: str) -> None:
    """Atomic file write via rename — prevents corruption on crash."""
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(data, encoding='utf-8')
        os.replace(tmp, path)
    except Exception as e:
        logger.error("Atomic write failed %s: %s", path, e)
        tmp.unlink(missing_ok=True)


class SlotManager:
    __slots__ = ('_redis', '_slots', '_alerts', '_history', '_lock')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis:   aioredis.Redis       = redis_client
        self._slots:   dict[str, TradeSlot] = {}
        self._alerts:  list[Alert]          = []
        self._history: list[Order]          = []
        self._lock     = asyncio.Lock()

    # ── Persistence ───────────────────────────────────────────────────────────

    async def load(self) -> None:
        """Load slots from Redis; fall back to file backup if Redis is empty."""
        await self._load_slots()
        await self._load_alerts()

    async def _load_slots(self) -> None:
        raw = await self._redis.get(_PERSIST_KEY)
        if not raw:
            # Redis was wiped — restore from file backup
            if _BACKUP_SLOTS.exists():
                try:
                    raw = _BACKUP_SLOTS.read_text(encoding='utf-8')
                    if raw:
                        await self._redis.set(_PERSIST_KEY, raw)
                        logger.info("Slots restored from file backup")
                except Exception as e:
                    logger.error("Slot file restore error: %s", e)
        if raw:
            for d in json.loads(raw):
                try:
                    slot = TradeSlot.model_validate(d)
                    self._slots[slot.id] = slot
                except Exception as e:
                    logger.warning("Slot restore failed: %s", e)
            logger.info("Loaded %d slot(s)", len(self._slots))

    async def _load_alerts(self) -> None:
        raw = await self._redis.get(_ALERTS_KEY)
        if not raw and _BACKUP_ALERTS.exists():
            try:
                raw = _BACKUP_ALERTS.read_text(encoding='utf-8')
                if raw:
                    await self._redis.set(_ALERTS_KEY, raw)
            except Exception as e:
                logger.error("Alert file restore error: %s", e)
        if raw:
            try:
                for d in json.loads(raw):
                    self._alerts.append(Alert.model_validate(d))
                logger.info("Loaded %d alert(s)", len(self._alerts))
            except Exception as e:
                logger.error("Alert restore error: %s", e)

    async def _persist(self) -> None:
        data = json.dumps(
            [s.model_dump(mode='json') for s in self._slots.values()],
            separators=(',', ':'), default=str,
        )
        try:
            await self._redis.set(_PERSIST_KEY, data)
        except Exception as e:
            logger.error("Slot Redis persist error: %s", e)
        _write_atomic(_BACKUP_SLOTS, data)

    async def _persist_alerts(self) -> None:
        data = json.dumps(
            [a.model_dump(mode='json') for a in self._alerts],
            separators=(',', ':'), default=str,
        )
        try:
            await self._redis.set(_ALERTS_KEY, data)
        except Exception as e:
            logger.error("Alert Redis persist error: %s", e)
        _write_atomic(_BACKUP_ALERTS, data)

    # ── Slot CRUD ─────────────────────────────────────────────────────────────

    async def create_slot(self, slot: TradeSlot) -> TradeSlot:
        async with self._lock:
            self._slots[slot.id] = slot
        await self._persist()
        await self._pub_market_data(slot, subscribe=True)
        logger.info("Slot created: %s %s %s %s",
                    slot.id[:8], slot.exchange, slot.symbol, slot.side)
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
            if len(self._history) > 2000:
                self._history = self._history[-2000:]
        await self._persist()
        logger.info("Slot closed: %s pnl=%.4f", slot_id[:8], realized_pnl)
        return slot

    def get_slot(self, slot_id: str) -> Optional[TradeSlot]:
        return self._slots.get(slot_id)

    def get_all_slots(self)     -> list[TradeSlot]: return list(self._slots.values())
    def get_active_slots(self)  -> list[TradeSlot]: return [s for s in self._slots.values() if s.status == 'active']
    def get_all_positions(self) -> list[Position]:  return [s.position for s in self._slots.values() if s.position]

    def get_order_history(self) -> list[Order]:
        """All filled/cancelled orders, newest first."""
        history = list(self._history)
        seen    = {o.id for o in history}
        for slot in self._slots.values():
            for o in slot.orders:
                if o.status in ('filled', 'cancelled', 'rejected') and o.id not in seen:
                    history.append(o)
                    seen.add(o.id)
        return sorted(history, key=lambda o: (o.created_at or datetime.min), reverse=True)

    def get_open_orders(self) -> list[Order]:
        """Working orders + virtual stop/target orders + virtual conditional entry orders."""
        orders = [
            o for s in self._slots.values()
            for o in s.orders
            if o.status in ('pending', 'working')
        ]
        for slot in self._slots.values():
            if not slot.position or not slot.is_paper:
                continue
            if slot.status != 'active':
                continue
            close_side = 'sell' if slot.side == 'long' else 'buy'
            if slot.stop_price and not slot.sl_order_id:
                orders.append(Order(
                    id=f"VSTOP-{slot.id}",
                    exchange=slot.exchange, symbol=slot.symbol,
                    side=close_side, order_type='stop_limit',
                    stop_price=slot.stop_price, price=slot.stop_price,
                    qty=slot.position.qty,
                    status='working', is_paper=True, slot_id=slot.id,
                ))
            if slot.target_price and not slot.target_order_id:
                orders.append(Order(
                    id=f"VTGT-{slot.id}",
                    exchange=slot.exchange, symbol=slot.symbol,
                    side=close_side, order_type='limit',
                    price=slot.target_price,
                    qty=slot.position.qty,
                    status='working', is_paper=True, slot_id=slot.id,
                ))
        # Virtual conditional orders — slots waiting for a candle-close trigger
        for slot in self._slots.values():
            if slot.status != 'conditional':
                continue
            entry = slot.entries[0] if slot.entries else None
            if not entry:
                continue
            orders.append(Order(
                id=f"VCOND-{slot.id}",
                exchange=slot.exchange, symbol=slot.symbol,
                side='buy' if slot.side == 'long' else 'sell',
                order_type=entry.order_type,
                price=entry.price,
                qty=entry.qty,
                status='working',
                is_paper=slot.is_paper,
                slot_id=slot.id,
            ))
        return orders

    # ── Alert management ──────────────────────────────────────────────────────

    async def add_alert(self, alert: Alert) -> None:
        self._alerts.append(alert)
        await self._persist_alerts()

    async def delete_alert_async(self, aid: str) -> None:
        self._alerts = [a for a in self._alerts if a.id != aid]
        await self._persist_alerts()

    def delete_alert(self, aid: str) -> None:
        self._alerts = [a for a in self._alerts if a.id != aid]
        asyncio.ensure_future(self._persist_alerts())

    def get_alerts(self)             -> list[Alert]: return self._alerts
    def clear_triggered_alerts(self) -> None:
        self._alerts = [a for a in self._alerts if not a.triggered]
        asyncio.ensure_future(self._persist_alerts())

    def clear_all_alerts(self) -> None:
        self._alerts.clear()
        asyncio.ensure_future(self._persist_alerts())

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
            logger.error("Market data notify error: %s", e)

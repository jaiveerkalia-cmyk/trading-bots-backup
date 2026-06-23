"""
Software trigger/exit engine.
Polls Redis tick prices every ENGINE_STATE_PUBLISH_INTERVAL seconds.
Used as fallback for conditions the exchange doesn't handle natively.
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Callable, Awaitable, TYPE_CHECKING

import redis.asyncio as aioredis

from common.models import TradeSlot, Alert
from common import redis_keys, settings

if TYPE_CHECKING:
    from trading_engine.trade_slot import SlotManager

logger = logging.getLogger('trigger_engine')


class TriggerEngine:
    __slots__ = ('_redis', '_slots', '_on_exit', '_on_alert', '_task')

    def __init__(
        self,
        redis_client:  aioredis.Redis,
        slot_manager:  'SlotManager',
        on_exit:       Callable[[str, str], Awaitable[None]],
        on_alert:      Callable[[Alert],    Awaitable[None]],
    ):
        self._redis    = redis_client
        self._slots    = slot_manager
        self._on_exit  = on_exit
        self._on_alert = on_alert
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("TriggerEngine started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"TriggerEngine loop error: {e}")
            await asyncio.sleep(settings.ENGINE_STATE_PUBLISH_INTERVAL)

    async def _check_all(self) -> None:
        for slot in self._slots.get_active_slots():
            if not slot.position:
                continue
            price = await self._price(slot.exchange, slot.symbol)
            if price <= 0:
                continue
            # Stop
            if slot.stop_price:
                hit = (
                    (slot.side == 'long'  and price <= slot.stop_price) or
                    (slot.side == 'short' and price >= slot.stop_price)
                )
                if hit:
                    logger.info(f"Stop hit [{slot.symbol}] {price} <= {slot.stop_price}")
                    await self._on_exit(slot.id, 'stop_hit')
                    continue
            # Target
            if slot.target_price:
                hit = (
                    (slot.side == 'long'  and price >= slot.target_price) or
                    (slot.side == 'short' and price <= slot.target_price)
                )
                if hit:
                    logger.info(f"Target hit [{slot.symbol}] {price} >= {slot.target_price}")
                    await self._on_exit(slot.id, 'target_hit')

        for alert in self._slots.get_alerts():
            if alert.triggered:
                continue
            price = await self._price(alert.exchange, alert.symbol)
            if price <= 0:
                continue
            triggered = (
                (alert.upper is not None and price >= alert.upper) or
                (alert.lower is not None and price <= alert.lower)
            )
            if triggered:
                alert.triggered = True
                logger.info(f"Alert triggered [{alert.symbol}] price={price}")
                await self._on_alert(alert)

    async def _price(self, exchange: str, symbol: str) -> float:
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('p', 0)) if raw else 0.0
        except Exception:
            return 0.0

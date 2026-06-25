from __future__ import annotations
import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from common import redis_keys, settings

if TYPE_CHECKING:
    from trading_engine.trade_slot import SlotManager

logger = logging.getLogger('state_publisher')


class StatePublisher:
    __slots__ = ('_redis', '_slots', '_log', '_live_mode', '_task')

    def __init__(self, redis_client: aioredis.Redis, slot_manager: 'SlotManager'):
        self._redis     = redis_client
        self._slots     = slot_manager
        self._log: deque[dict] = deque(maxlen=settings.LOG_MAX_ENTRIES)
        self._live_mode = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("StatePublisher started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def set_live_mode(self, live: bool) -> None:
        self._live_mode = live

    def log(self, message: str, level: str = 'info',
            exchange: str | None = None, symbol: str | None = None) -> None:
        self._log.append({
            'ts':  datetime.now(timezone.utc).isoformat(),
            'lvl': level,
            'msg': message,
            'exc': exchange,
            'sym': symbol,
        })

    async def _loop(self) -> None:
        while True:
            try:
                await self._publish()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"StatePublisher error: {e}")
            await asyncio.sleep(settings.ENGINE_STATE_PUBLISH_INTERVAL)

    async def _publish(self) -> None:
        slots     = self._slots.get_all_slots()
        positions = self._slots.get_all_positions()
        open_ord  = self._slots.get_open_orders()
        # get_order_history() returns newest-first — take first 200 (newest)
        history   = self._slots.get_order_history()
        alerts    = self._slots.get_alerts()

        unrealized = sum(p.unrealized_pnl for p in positions)
        realized   = sum(s.realized_pnl   for s in slots)

        sep = (',', ':')

        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(redis_keys.SLOTS_KEY,
                     json.dumps([s.model_dump(mode='json') for s in slots], separators=sep))
            pipe.set(redis_keys.POSITIONS_KEY,
                     json.dumps([p.model_dump(mode='json') for p in positions], separators=sep))
            pipe.set(redis_keys.OPEN_ORDERS_KEY,
                     json.dumps([o.model_dump(mode='json') for o in open_ord[:100]], separators=sep))
            # [:200] — first 200 of newest-first list = 200 most recent orders
            pipe.set(redis_keys.ORDER_HISTORY_KEY,
                     json.dumps([o.model_dump(mode='json') for o in history[:200]], separators=sep))
            pipe.set(redis_keys.ALERTS_KEY,
                     json.dumps([a.model_dump(mode='json') for a in alerts], separators=sep))
            pipe.set(redis_keys.PNL_KEY,
                     json.dumps({'unrealized': unrealized, 'realized': realized}, separators=sep))
            pipe.set(redis_keys.LIVE_MODE_KEY, '1' if self._live_mode else '0')

            if self._log:
                entries = list(self._log)
                self._log.clear()
                for entry in entries:
                    pipe.lpush(redis_keys.LOG_KEY, json.dumps(entry, separators=sep))
                pipe.ltrim(redis_keys.LOG_KEY, 0, settings.LOG_MAX_ENTRIES - 1)

            await pipe.execute()

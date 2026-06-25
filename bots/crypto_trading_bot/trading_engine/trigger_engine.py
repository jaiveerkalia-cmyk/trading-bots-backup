from __future__ import annotations
import asyncio
import json
import logging
from typing import Callable, Awaitable, TYPE_CHECKING

import redis.asyncio as aioredis

from common.models import Alert
from common import redis_keys, settings

if TYPE_CHECKING:
    from trading_engine.trade_slot import SlotManager
    from trading_engine.paper_engine import PaperEngine

logger = logging.getLogger('trigger_engine')


class TriggerEngine:
    __slots__ = ('_redis', '_slots', '_paper', '_on_exit', '_on_alert', '_task')

    def __init__(
        self,
        redis_client:  aioredis.Redis,
        slot_manager:  'SlotManager',
        on_exit:       Callable[[str, str], Awaitable[None]],
        on_alert:      Callable[[Alert],    Awaitable[None]],
        paper_engine:  'PaperEngine',
    ):
        self._redis    = redis_client
        self._slots    = slot_manager
        self._paper    = paper_engine
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
        updated: set[str] = set()

        for slot in self._slots.get_active_slots():
            if not slot.position:
                continue

            # Use mark price for futures, last tick price for spot
            price = await self._price(slot.exchange, slot.symbol)
            if price <= 0:
                continue

            sym_key = f"{slot.exchange}:{slot.symbol}"
            if slot.position.is_paper and sym_key not in updated:
                await self._paper.update_mark_prices(slot.exchange, slot.symbol, price)
                updated.add(sym_key)

            if slot.stop_price:
                hit = (
                    (slot.side == 'long'  and price <= slot.stop_price) or
                    (slot.side == 'short' and price >= slot.stop_price)
                )
                if hit:
                    logger.info(f"Stop hit [{slot.symbol}] price={price} stop={slot.stop_price}")
                    await self._on_exit(slot.id, 'stop_hit')
                    continue

            if slot.target_price:
                hit = (
                    (slot.side == 'long'  and price >= slot.target_price) or
                    (slot.side == 'short' and price <= slot.target_price)
                )
                if hit:
                    logger.info(f"Target hit [{slot.symbol}] price={price} target={slot.target_price}")
                    await self._on_exit(slot.id, 'target_hit')

        for alert in self._slots.get_alerts():
            if alert.triggered:
                continue
            # Alerts checked against LTP (tick price), not mark price
            price = await self._ltp(alert.exchange, alert.symbol)
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
        """
        For futures exchanges: returns mark price (used for stop/target checks and PnL).
        For spot exchanges: returns last trade price.
        """
        try:
            if 'futures' in exchange:
                raw = await self._redis.get(redis_keys.mark_price_key(exchange, symbol))
                if raw:
                    return float(json.loads(raw).get('p', 0))
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('p', 0)) if raw else 0.0
        except Exception:
            return 0.0

    async def _ltp(self, exchange: str, symbol: str) -> float:
        """Last trade price — used for alerts."""
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('p', 0)) if raw else 0.0
        except Exception:
            return 0.0

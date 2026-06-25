from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Union

import redis.asyncio as aioredis

from common.models import Tick, Candle, OrderBook
from common import settings, redis_keys

logger = logging.getLogger(__name__)

MarketData = Union[Tick, Candle, OrderBook]


class RedisPublisher:
    __slots__ = ('_redis', '_queue', '_task')

    def __init__(self, redis_client: aioredis.Redis):
        self._redis  = redis_client
        self._queue: asyncio.Queue[MarketData] = asyncio.Queue(
            maxsize=settings.MARKET_DATA_QUEUE_SIZE
        )
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._drain())
        logger.info("RedisPublisher started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def publish(self, data: MarketData) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _drain(self) -> None:
        while True:
            try:
                data = await self._queue.get()
                await self._push(data)
                self._queue.task_done()
                del data
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Publisher drain error: {e}")

    async def _push(self, data: MarketData) -> None:
        try:
            if isinstance(data, Tick):
                payload = _tick_json(data)
                channel = redis_keys.tick_channel(data.exchange, data.symbol)
                key     = redis_keys.latest_tick_key(data.exchange, data.symbol)
                ttl     = settings.TICK_TTL

                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.set(key, payload, ex=ttl)
                    # Publish mark price separately for futures — used for fills/PnL
                    if data.mark_price is not None:
                        mp = json.dumps(
                            {'p': data.mark_price, 'ts': _ts(data.timestamp)},
                            separators=(',', ':'),
                        )
                        pipe.set(
                            redis_keys.mark_price_key(data.exchange, data.symbol),
                            mp, ex=ttl,
                        )
                    await pipe.execute()
                del payload

            elif isinstance(data, Candle):
                payload = _candle_json(data)
                channel = redis_keys.candle_channel(data.exchange, data.symbol, data.interval)
                key     = redis_keys.latest_candle_key(data.exchange, data.symbol, data.interval)
                ttl     = settings.CANDLE_TTL
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.set(key, payload, ex=ttl)
                    await pipe.execute()
                del payload

            elif isinstance(data, OrderBook):
                payload = _book_json(data)
                channel = redis_keys.depth_channel(data.exchange, data.symbol)
                key     = redis_keys.latest_depth_key(data.exchange, data.symbol)
                ttl     = settings.DEPTH_TTL
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.set(key, payload, ex=ttl)
                    await pipe.execute()
                del payload

        except Exception as e:
            logger.error(f"Redis push error: {e}")


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _tick_json(t: Tick) -> str:
    d: dict = {
        'e': t.exchange, 's': t.symbol,
        'p': t.price,    'v': t.volume, 'ts': _ts(t.timestamp),
    }
    if t.mark_price   is not None: d['mp'] = t.mark_price
    if t.funding_rate is not None: d['fr'] = t.funding_rate
    return json.dumps(d, separators=(',', ':'))


def _candle_json(c: Candle) -> str:
    return json.dumps(
        {'e': c.exchange, 's': c.symbol, 'i': c.interval,
         'o': c.open, 'h': c.high, 'l': c.low, 'c': c.close,
         'v': c.volume, 'ts': _ts(c.timestamp)},
        separators=(',', ':'),
    )


def _book_json(b: OrderBook) -> str:
    return json.dumps(
        {'e': b.exchange, 's': b.symbol,
         'bids': [[lv.price, lv.qty] for lv in b.bids],
         'asks': [[lv.price, lv.qty] for lv in b.asks],
         'ts': _ts(b.timestamp)},
        separators=(',', ':'),
    )

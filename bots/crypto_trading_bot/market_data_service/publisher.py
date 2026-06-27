"""
RedisPublisher — deduplicates tick messages to avoid hammering Redis
when price is unchanged. Only publishes if price changed OR 1 s has elapsed.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Union

import redis.asyncio as aioredis

from common.models import Tick, Candle, OrderBook
from common import settings, redis_keys

logger = logging.getLogger(__name__)

MarketData = Union[Tick, Candle, OrderBook]


class RedisPublisher:
    __slots__ = (
        '_redis', '_queue', '_task',
        '_last_price', '_last_pub_ts',   # dedup state
    )

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis  = redis_client
        self._queue: asyncio.Queue[MarketData] = asyncio.Queue(
            maxsize=settings.MARKET_DATA_QUEUE_SIZE
        )
        self._task: asyncio.Task | None = None
        self._last_price:  dict[str, float] = {}   # key -> last published price
        self._last_pub_ts: dict[str, float] = {}   # key -> last publish monotonic time

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
                logger.error("Publisher drain error: %s", e)

    async def _push(self, data: MarketData) -> None:
        try:
            if isinstance(data, Tick):
                await self._push_tick(data)

            elif isinstance(data, Candle):
                payload = _candle_json(data)
                channel = redis_keys.candle_channel(data.exchange, data.symbol, data.interval)
                key     = redis_keys.latest_candle_key(data.exchange, data.symbol, data.interval)
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.set(key, payload, ex=settings.CANDLE_TTL)
                    await pipe.execute()
                del payload

            elif isinstance(data, OrderBook):
                payload = _book_json(data)
                channel = redis_keys.depth_channel(data.exchange, data.symbol)
                key     = redis_keys.latest_depth_key(data.exchange, data.symbol)
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.publish(channel, payload)
                    pipe.set(key, payload, ex=settings.DEPTH_TTL)
                    await pipe.execute()
                del payload

        except Exception as e:
            logger.error("Redis push error: %s", e)

    async def _push_tick(self, data: Tick) -> None:
        """Publish tick, skipping Redis write if price unchanged within 1 s."""
        key = f"{data.exchange}:{data.symbol}"
        now = time.monotonic()

        price_changed = (data.price != self._last_price.get(key))
        time_elapsed  = (now - self._last_pub_ts.get(key, 0)) >= 1.0

        if not price_changed and not time_elapsed:
            return   # deduplicate — skip unchanged tick within 1 s window

        self._last_price[key]  = data.price
        self._last_pub_ts[key] = now

        payload = _tick_json(data)
        channel = redis_keys.tick_channel(data.exchange, data.symbol)
        lkey    = redis_keys.latest_tick_key(data.exchange, data.symbol)

        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.publish(channel, payload)
            pipe.set(lkey, payload, ex=settings.TICK_TTL)
            if data.mark_price is not None:
                mp = json.dumps(
                    {'p': data.mark_price, 'ts': _ts(data.timestamp)},
                    separators=(',', ':'),
                )
                pipe.set(
                    redis_keys.mark_price_key(data.exchange, data.symbol),
                    mp, ex=settings.TICK_TTL,
                )
            await pipe.execute()
        del payload


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

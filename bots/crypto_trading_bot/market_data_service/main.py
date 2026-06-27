"""
Market data service — ticker-only by default to minimise RAM.
Orderbook / candle subscriptions only on explicit request via control channel.
"""
from __future__ import annotations
import asyncio
import gc
import json
import logging
import os
import signal

import redis.asyncio as aioredis

from exchanges.registry import get_adapter
from exchanges.base import BaseExchangeAdapter
from market_data_service.publisher import RedisPublisher
from common.key_manager import load_keys
from common import settings, redis_keys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('market_data')

CONTROL_CHANNEL = 'market_data:control'
ACTIVE_SUBS_KEY = 'market_data:active_subs'

# Ticker only — biggest RAM saving: no orderbook objects, no candle objects
DEFAULT_STREAMS = ['ticker']


async def main() -> None:
    redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True,
        max_connections=3,          # was 5
    )

    publisher = RedisPublisher(redis)
    await publisher.start()

    keys    = load_keys()
    testnet = os.getenv('TESTNET', '0') == '1'
    adapters: dict[str, BaseExchangeAdapter] = {}

    for exchange in settings.SUPPORTED_EXCHANGES:
        creds = keys.get(exchange, {})
        try:
            adapter = get_adapter(
                exchange   = exchange,
                api_key    = creds.get('api_key',    ''),
                api_secret = creds.get('api_secret', ''),
                testnet    = testnet,
            )
            await adapter.connect()
            adapters[exchange] = adapter
            logger.info("Connected: %s", exchange)
        except Exception as e:
            logger.error("Failed to connect %s: %s", exchange, e)

    await redis.set(
        redis_keys.CONNECTED_EXCHANGES_KEY,
        json.dumps(list(adapters.keys())),
        ex=60,
    )

    await _restore_subs(redis, adapters, publisher)

    try:
        await asyncio.gather(
            _control_loop(redis, adapters, publisher),
            _heartbeat(redis, adapters),
        )
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown(redis, adapters, publisher)


async def _restore_subs(
    redis: aioredis.Redis,
    adapters: dict,
    publisher: RedisPublisher,
) -> None:
    members = await redis.smembers(ACTIVE_SUBS_KEY)
    if not members:
        return
    count = 0
    for raw in members:
        try:
            sub = json.loads(raw)
            # Only restore ticker stream on restart to save RAM
            sub['streams'] = [s for s in sub.get('streams', DEFAULT_STREAMS)
                              if s == 'ticker']
            if not sub['streams']:
                sub['streams'] = DEFAULT_STREAMS
            await _do_subscribe(sub, adapters, publisher)
            count += 1
        except Exception as e:
            logger.warning("Failed to restore sub %s: %s", raw, e)
    logger.info("Restored %d subscription(s)", count)


async def _control_loop(
    redis: aioredis.Redis,
    adapters: dict,
    publisher: RedisPublisher,
) -> None:
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    logger.info("Listening on %s", CONTROL_CHANNEL)

    while True:
        try:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if msg and msg['type'] == 'message':
                try:
                    cmd      = json.loads(msg['data'])
                    action   = cmd.get('cmd')
                    exchange = cmd.get('exchange', '')
                    symbol   = cmd.get('symbol', '')

                    if action == 'subscribe':
                        sub = {
                            'exchange': exchange,
                            'symbol':   symbol,
                            'streams':  cmd.get('streams', DEFAULT_STREAMS),
                        }
                        await redis.sadd(ACTIVE_SUBS_KEY, json.dumps(sub))
                        await _do_subscribe(sub, adapters, publisher)

                    elif action == 'unsubscribe':
                        adapter = adapters.get(exchange)
                        if adapter:
                            await adapter.unsubscribe(symbol)
                        for raw in await redis.smembers(ACTIVE_SUBS_KEY):
                            s = json.loads(raw)
                            if s.get('exchange') == exchange and s.get('symbol') == symbol:
                                await redis.srem(ACTIVE_SUBS_KEY, raw)
                        logger.info("Unsubscribed: %s %s", exchange, symbol)

                except Exception as e:
                    logger.error("Control command error: %s", e)

            await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Control loop error: %s", e)
            await asyncio.sleep(1)

    await pubsub.unsubscribe(CONTROL_CHANNEL)
    await pubsub.aclose()


async def _do_subscribe(
    sub: dict,
    adapters: dict,
    publisher: RedisPublisher,
) -> None:
    exchange = sub.get('exchange', '')
    symbol   = sub.get('symbol', '')
    streams  = sub.get('streams', DEFAULT_STREAMS)
    adapter  = adapters.get(exchange)

    if not adapter:
        logger.warning("No adapter for: %s", exchange)
        return

    for stream in streams:
        try:
            if stream == 'ticker':
                await adapter.subscribe_ticker(symbol, publisher.publish)
            elif stream == 'orderbook':
                await adapter.subscribe_orderbook(symbol, publisher.publish)
            elif stream.startswith('candles:'):
                interval = stream.split(':', 1)[1]
                await adapter.subscribe_candles(symbol, interval, publisher.publish)
        except Exception as e:
            logger.error("Subscribe error [%s %s %s]: %s", exchange, symbol, stream, e)

    logger.info("Subscribed: %s %s %s", exchange, symbol, streams)


async def _heartbeat(
    redis: aioredis.Redis,
    adapters: dict,
) -> None:
    """Refresh connected-exchanges TTL every 30 s. Force GC every 60 s."""
    tick = 0
    while True:
        await asyncio.sleep(30)
        tick += 1
        try:
            await redis.set(
                redis_keys.CONNECTED_EXCHANGES_KEY,
                json.dumps(list(adapters.keys())),
                ex=60,
            )
        except Exception as e:
            logger.error("Heartbeat error: %s", e)

        # Force garbage collection every 60 s to release cyclic refs
        if tick % 2 == 0:
            collected = gc.collect()
            if collected:
                logger.debug("GC collected %d objects", collected)


async def _shutdown(
    redis: aioredis.Redis,
    adapters: dict,
    publisher: RedisPublisher,
) -> None:
    logger.info("Shutting down market data service...")
    for name, adapter in adapters.items():
        try:
            await adapter.disconnect()
        except Exception as e:
            logger.error("Disconnect error [%s]: %s", name, e)
    await publisher.stop()
    await redis.set(redis_keys.CONNECTED_EXCHANGES_KEY, json.dumps([]))
    await redis.aclose()
    logger.info("Market data service stopped")


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
        )
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()

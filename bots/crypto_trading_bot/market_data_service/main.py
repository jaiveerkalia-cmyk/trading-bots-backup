"""
Market data service — one process, one asyncio task per exchange WS connection.
Subscribes to symbols on demand via Redis control channel.
Persists active subscriptions in Redis so they survive service restarts.
"""
from __future__ import annotations
import asyncio
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

# ── Redis keys owned by this service ──────────────────────────────────────────
CONTROL_CHANNEL = 'market_data:control'
ACTIVE_SUBS_KEY = 'market_data:active_subs'   # Redis SET — survives restarts

# Default streams to subscribe when trading engine adds a new symbol
DEFAULT_STREAMS = ['ticker', 'orderbook', 'candles:1m', 'candles:5m']


async def main() -> None:
    redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True,
        max_connections=5,
    )

    publisher = RedisPublisher(redis)
    await publisher.start()

    keys     = load_keys()
    testnet  = os.getenv('TESTNET', '0') == '1'
    adapters: dict[str, BaseExchangeAdapter] = {}

    for exchange in settings.SUPPORTED_EXCHANGES:
        creds = keys.get(exchange, {})
        try:
            adapter = get_adapter(
                exchange=exchange,
                api_key=creds.get('api_key', ''),
                api_secret=creds.get('api_secret', ''),
                testnet=testnet,
            )
            await adapter.connect()
            adapters[exchange] = adapter
            logger.info(f"Connected: {exchange}")
        except Exception as e:
            logger.error(f"Failed to connect {exchange}: {e}")

    await redis.set(
        redis_keys.CONNECTED_EXCHANGES_KEY,
        json.dumps(list(adapters.keys())),
        ex=60,
    )

    # Re-subscribe to symbols active before last restart
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
            await _do_subscribe(sub, adapters, publisher)
            count += 1
        except Exception as e:
            logger.warning(f"Failed to restore sub {raw}: {e}")
    logger.info(f"Restored {count} subscriptions")


async def _control_loop(
    redis: aioredis.Redis,
    adapters: dict,
    publisher: RedisPublisher,
) -> None:
    """
    Listen for subscribe/unsubscribe commands from trading engine.
    Command format:
      subscribe:   {"cmd":"subscribe",   "exchange":"binance", "symbol":"BTC/USDT",
                    "streams":["ticker","orderbook","candles:1m"]}
      unsubscribe: {"cmd":"unsubscribe", "exchange":"binance", "symbol":"BTC/USDT"}
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    logger.info(f"Listening on {CONTROL_CHANNEL}")

    async for msg in pubsub.listen():
        if msg['type'] != 'message':
            continue
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
                # Persist so we survive restarts
                await redis.sadd(ACTIVE_SUBS_KEY, json.dumps(sub))
                await _do_subscribe(sub, adapters, publisher)

            elif action == 'unsubscribe':
                adapter = adapters.get(exchange)
                if adapter:
                    await adapter.unsubscribe(symbol)
                # Remove from persistence
                for raw in await redis.smembers(ACTIVE_SUBS_KEY):
                    s = json.loads(raw)
                    if s.get('exchange') == exchange and s.get('symbol') == symbol:
                        await redis.srem(ACTIVE_SUBS_KEY, raw)
                logger.info(f"Unsubscribed: {exchange} {symbol}")

        except Exception as e:
            logger.error(f"Control command error: {e}")


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
        logger.warning(f"No adapter for: {exchange}")
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
            logger.error(f"Subscribe error [{exchange} {symbol} {stream}]: {e}")

    logger.info(f"Subscribed: {exchange} {symbol} {streams}")


async def _heartbeat(
    redis: aioredis.Redis,
    adapters: dict,
) -> None:
    """Refresh connected-exchanges key every 30s. Expires in 60s if service dies."""
    while True:
        await asyncio.sleep(30)
        try:
            await redis.set(
                redis_keys.CONNECTED_EXCHANGES_KEY,
                json.dumps(list(adapters.keys())),
                ex=60,
            )
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")


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
            logger.error(f"Disconnect error [{name}]: {e}")
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

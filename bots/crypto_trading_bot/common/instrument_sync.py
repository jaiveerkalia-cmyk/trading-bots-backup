"""
Periodic instrument list refresh.
Runs as a background asyncio task in both market_data_service and trading_engine.
Populates the shared SymbolMap so the UI can offer a live symbol search.
"""
from __future__ import annotations
import asyncio
import json
import logging

import redis.asyncio as aioredis

from common.symbol_map import symbol_map
from common import settings

logger = logging.getLogger('instrument_sync')

_SYMBOLS_KEY_PREFIX = 'instruments:'   # e.g. instruments:binance


async def run(
    adapters: dict,
    redis_client: aioredis.Redis,
    interval: int = 3600,              # refresh every hour
) -> None:
    """Long-running task — call with asyncio.create_task()."""
    while True:
        await _sync(adapters, redis_client)
        await asyncio.sleep(interval)


async def _sync(adapters: dict, redis_client: aioredis.Redis) -> None:
    for exchange, adapter in adapters.items():
        try:
            symbols  = await adapter.get_tradable_symbols()
            pairs    = {s: adapter.to_exchange_symbol(s) for s in symbols}
            symbol_map.update(exchange, pairs)

            # Publish to Redis so UI container can search symbols without
            # importing exchange adapters
            await redis_client.set(
                f"{_SYMBOLS_KEY_PREFIX}{exchange}",
                json.dumps(symbols[:2000], separators=(',', ':')),  # cap at 2000
                ex=7200,    # 2h TTL — stale after two missed syncs
            )
            logger.info(f"Synced {len(symbols)} symbols for {exchange}")
        except Exception as e:
            logger.error(f"Instrument sync failed [{exchange}]: {e}")


async def get_symbols_from_redis(
    redis_client: aioredis.Redis, exchange: str
) -> list[str]:
    """UI uses this to search symbols without importing adapters."""
    try:
        raw = await redis_client.get(f"{_SYMBOLS_KEY_PREFIX}{exchange}")
        return json.loads(raw) if raw else []
    except Exception:
        return []

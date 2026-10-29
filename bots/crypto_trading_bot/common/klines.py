"""
Minimal Binance public-REST kline fetch for candle-close alerts / "Fire on".

Why this exists: the trading engine builds its exchange adapters but never
connect()s them in paper mode, so adapter.fetch_ohlcv() returns [] there.
This needs no ccxt and no API keys (public endpoints), so the engine stays small.

Returns rows as [open_time_ms, open, high, low, close, volume] (floats),
oldest first, the last row being the still-forming candle.
"""
from __future__ import annotations
import logging

import aiohttp

logger = logging.getLogger('klines')

_ENDPOINTS = {
    'binance_futures': 'https://fapi.binance.com/fapi/v1/klines',
    'binance':         'https://api.binance.com/api/v3/klines',
}


def supports(exchange: str) -> bool:
    return exchange in _ENDPOINTS


def native_symbol(symbol: str) -> str:
    """'BTC/USDT' or 'BTC/USDT:USDT' -> 'BTCUSDT'."""
    return symbol.split(':')[0].replace('/', '').upper()


async def fetch_klines(
    session: aiohttp.ClientSession, exchange: str, symbol: str,
    interval: str, limit: int = 3,
) -> list[list]:
    url = _ENDPOINTS.get(exchange)
    if not url:
        return []
    params = {'symbol': native_symbol(symbol), 'interval': interval, 'limit': limit}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                logger.warning("klines HTTP %s [%s %s %s]", resp.status,
                               exchange, symbol, interval)
                return []
            data = await resp.json()
    except Exception as exc:
        logger.warning("klines request failed [%s %s %s]: %s",
                       exchange, symbol, interval, exc)
        return []

    rows: list[list] = []
    for k in data if isinstance(data, list) else []:
        try:
            rows.append([int(k[0]), float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5])])
        except (TypeError, ValueError, IndexError):
            continue
    return rows

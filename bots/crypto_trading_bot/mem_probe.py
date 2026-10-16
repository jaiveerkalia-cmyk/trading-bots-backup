"""
Throwaway RAM probe for crypto-market-data. Read-only, standalone.
Does not import or modify any app logic beyond the registry/adapter it measures.
Safe to delete after use.

Usage (from project dir):
  docker compose run --rm --no-deps -T crypto-market-data python mem_probe.py
"""
import asyncio
import collections
import ctypes
import gc
import os
import re
import subprocess
import sys


def st(key):
    for line in open('/proc/self/status'):
        if line.startswith(key):
            return int(line.split()[1]) // 1024


def p(tag):
    print(f"{tag:<36} RSS={st('VmRSS'):>4} MB  HWM={st('VmHWM'):>4} MB", flush=True)


def run_mem(keyed):
    p('python only')
    import pydantic;            p('+pydantic')            # noqa
    import redis.asyncio;       p('+redis')               # noqa
    import aiohttp;             p('+aiohttp')             # noqa
    import cryptography.fernet; p('+cryptography')        # noqa
    import ccxt.pro;            p('+ccxt.pro')            # noqa
    from exchanges.registry import get_adapter
    p('+app registry (all adapters)')

    async def go():
        creds = {}
        if keyed:
            from common.key_manager import load_keys
            creds = load_keys().get('binance_futures', {})
            print('keys found:', bool(creds), flush=True)
        ad = get_adapter('binance_futures',
                         creds.get('api_key', ''), creds.get('api_secret', ''), False)
        rest = collections.Counter()
        try:
            await asyncio.wait_for(ad.connect(), 90)
            ex = ad._ex
            p(f"after connect ({'keyed' if keyed else 'keyless'})")
            print('markets:', len(ex.markets or {}),
                  '| currencies:', len(ex.currencies or {}), flush=True)
            gc.collect();                              p('after gc.collect')
            ctypes.CDLL('libc.so.6').malloc_trim(0);   p('after malloc_trim')

            orig = ex.fetch

            async def counted(url, *a, **kw):
                rest[str(url).split('?')[0].split('//')[-1]] += 1
                return await orig(url, *a, **kw)
            ex.fetch = counted

            counts = {'tick': 0, 'book': 0}

            async def ctick(x): counts['tick'] += 1
            async def cbook(x): counts['book'] += 1

            await ad.subscribe_ticker('BTC/USDT', ctick)
            await ad.subscribe_orderbook('BTC/USDT', cbook)
            print('streaming BTC/USDT ticker+depth for 60s...', flush=True)
            await asyncio.sleep(60)
            p('after 60s streaming')
            print('callbacks:', counts, flush=True)
            print('REST calls during stream:', dict(rest), flush=True)
        finally:
            try:
                await ad.disconnect()
            except Exception as e:
                print('disconnect error:', e)

    asyncio.run(asyncio.wait_for(go(), 180))


def run_src():
    import ccxt
    d = os.path.dirname(ccxt.__file__)
    print('ccxt', ccxt.__version__, 'at', d, flush=True)

    def grep(rel, pat, maxn=40):
        rx = re.compile(pat)
        n = 0
        for i, line in enumerate(open(os.path.join(d, rel), encoding='utf-8'), 1):
            if rx.search(line):
                print(f"{rel}:{i}: {line.rstrip()[:170]}")
                n += 1
                if n >= maxn:
                    break

    print('\n-- binance markets/currencies options --')
    grep('async_support/binance.py', r"fetchMarkets|fetchCurrencies")
    print('\n-- base load_markets --')
    grep('async_support/base/exchange.py', r"fetchCurrencies|def load_markets")
    print('\n-- pro binance orderbook --')
    grep('pro/binance.py',
         r"self\.orderbooks|def watch_order_book|def fetch_order_book_snapshot|def handle_order_book", 60)
    print('\n-- pro binance ticker / markPrice --')
    grep('pro/binance.py', r"def watch_ticker|markPrice|def parse_ws_ticker", 30)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if mode == 'all':
        me = os.path.abspath(__file__)
        for m in ('keyless', 'keyed', 'src'):
            print(f"\n===== {m} =====", flush=True)
            try:
                subprocess.run([sys.executable, '-u', me, m], timeout=240)
            except subprocess.TimeoutExpired:
                print(f"{m}: timed out after 240s", flush=True)
        print('\n===== done =====', flush=True)
    elif mode == 'src':
        run_src()
    else:
        run_mem(mode == 'keyed')

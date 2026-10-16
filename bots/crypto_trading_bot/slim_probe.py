"""
Throwaway slim-adapter prototype + parity probe. Standalone, read-only.
  - Writes NOTHING to Redis. Does not modify or patch any app code.
  - Modes: slim | ccxt | parity | all   (default: all, ~5-6 min)
Usage (from project dir):
  docker compose run --rm --no-deps -T crypto-market-data python slim_probe.py
Safe to delete after use.
"""
import asyncio
import collections
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

WS_BASE = 'wss://fstream.binance.com/stream?streams='
REST    = 'https://fapi.binance.com'
SYMS    = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XAU/USDT']
KLINES  = [('BTC/USDT', '1m'), ('BTC/USDT', '5m')]
DEPTH   = 20
DUR     = 60          # seconds of streaming per mode

Tick = Candle = OrderBook = OrderBookLevel = None


def st(key):
    for line in open('/proc/self/status'):
        if line.startswith(key):
            return int(line.split()[1]) // 1024


def p(tag):
    print(f"{tag:<46} RSS={st('VmRSS'):>4} MB  HWM={st('VmHWM'):>4} MB", flush=True)


def sid(sym):
    return sym.replace('/', '').lower()


def utc(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def load_models():
    global Tick, Candle, OrderBook, OrderBookLevel
    from common.models import Tick as T, Candle as C, OrderBook as O, OrderBookLevel as L
    Tick, Candle, OrderBook, OrderBookLevel = T, C, O, L


# ── Prototype of the slim feed (aiohttp only, no ccxt) ─────────────────────────

class SlimFeed:
    def __init__(self, syms, klines, on_tick, on_book, on_candle):
        self.syms    = syms
        self.klines  = klines
        self.by_id   = {sid(s): s for s in syms}
        self.on_tick, self.on_book, self.on_candle = on_tick, on_book, on_candle
        self.raw     = {}      # first raw payload seen per stream kind
        self.msgs    = 0

    def streams(self):
        out = []
        for s in self.syms:
            out.append(f"{sid(s)}@ticker")
            out.append(f"{sid(s)}@depth{DEPTH}@100ms")
        for s, iv in self.klines:
            out.append(f"{sid(s)}@kline_{iv}")
        return out

    async def run(self, stop):
        import aiohttp
        url = WS_BASE + '/'.join(self.streams())
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, heartbeat=20, max_msg_size=1 << 20) as ws:
                async for msg in ws:
                    if stop.is_set():
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self.msgs += 1
                        await self.handle(json.loads(msg.data))
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    async def handle(self, m):
        stream = m.get('stream', '')
        d = m.get('data', m)
        if stream.endswith('@ticker'):
            if 'ticker' not in self.raw:
                self.raw['ticker'] = json.dumps(d)[:420]
            sym = self.by_id.get(str(d.get('s', '')).lower())
            if not sym:
                return
            price = float(d.get('c') or 0)
            ms = d.get('C') or d.get('E')
            # parity with current adapter: mark_price falls back to last price,
            # funding_rate absent (24hr ticker carries neither)
            await self.on_tick(Tick(
                exchange='binance_futures', symbol=sym, price=price,
                mark_price=price, funding_rate=None,
                volume=float(d.get('v') or 0),
                timestamp=utc(ms) if ms else datetime.now(timezone.utc),
            ))
        elif '@depth' in stream:
            if 'depth' not in self.raw:
                self.raw['depth'] = json.dumps(d)[:420]
            sym = self.by_id.get(str(d.get('s', '')).lower())
            if not sym:
                return
            bids = d.get('b') or d.get('bids') or []
            asks = d.get('a') or d.get('asks') or []
            ms = d.get('E') or d.get('T')
            await self.on_book(OrderBook(
                exchange='binance_futures', symbol=sym,
                bids=[OrderBookLevel(price=float(a), qty=float(b)) for a, b in bids[:DEPTH]],
                asks=[OrderBookLevel(price=float(a), qty=float(b)) for a, b in asks[:DEPTH]],
                timestamp=utc(ms) if ms else datetime.now(timezone.utc),
            ))
        elif '@kline_' in stream:
            if 'kline' not in self.raw:
                self.raw['kline'] = json.dumps(d)[:420]
            k = d['k']
            sym = self.by_id.get(str(k.get('s', '')).lower())
            if not sym:
                return
            await self.on_candle(Candle(
                exchange='binance_futures', symbol=sym, interval=k['i'],
                open=float(k['o']), high=float(k['h']),
                low=float(k['l']), close=float(k['c']),
                volume=float(k['v']), timestamp=utc(k['t']),
            ))


# ── Shared measuring helpers ───────────────────────────────────────────────────

def make_counters():
    from market_data_service.publisher import _tick_json, _book_json, _candle_json
    cnt, size, sample = collections.Counter(), collections.Counter(), {}

    async def on_tick(t):
        s = _tick_json(t); cnt['tick'] += 1; size['tick'] += len(s)
        sample.setdefault('tick', s)

    async def on_book(b):
        s = _book_json(b); cnt['book'] += 1; size['book'] += len(s)
        sample.setdefault('book', s)

    async def on_candle(c):
        s = _candle_json(c); cnt['candle'] += 1; size['candle'] += len(s)
        sample.setdefault('candle', s)

    return cnt, size, sample, on_tick, on_book, on_candle


async def stream_and_report(label, cnt, size, sample, raw=None):
    cpu0, t0 = time.process_time(), time.monotonic()
    while time.monotonic() - t0 < DUR:
        await asyncio.sleep(15)
        p(f"  [{label}] t={int(time.monotonic() - t0)}s {dict(cnt)}")
    wall, cpu = time.monotonic() - t0, time.process_time() - cpu0
    print(f"\n[{label}] msgs/s  tick={cnt['tick']/wall:.1f}  book={cnt['book']/wall:.1f}"
          f"  candle={cnt['candle']/wall:.1f}   CPU={cpu/wall*100:.1f}% of one core", flush=True)
    for k in ('tick', 'book', 'candle'):
        if cnt[k]:
            print(f"[{label}] avg json bytes {k}: {size[k] // cnt[k]}", flush=True)
    for k in ('tick', 'book', 'candle'):
        if k in sample:
            print(f"[{label}] sample {k} json: {sample[k][:260]}", flush=True)
    if raw:
        for k, v in raw.items():
            print(f"[{label}] RAW binance {k}: {v}", flush=True)


# ── Mode: slim ─────────────────────────────────────────────────────────────────

async def mode_slim():
    p('python only')
    import pydantic, redis.asyncio, aiohttp   # noqa
    load_models()
    cnt, size, sample, ot, ob, oc = make_counters()
    p('slim imports (pydantic+redis+aiohttp+app)')
    assert 'ccxt' not in sys.modules, 'ccxt must not be imported in slim mode'
    feed = SlimFeed(SYMS, KLINES, ot, ob, oc)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(3)
    p('slim connected')
    await stream_and_report('slim', cnt, size, sample, feed.raw)
    stop.set()
    task.cancel()
    p('slim FINAL')


# ── Mode: ccxt (current adapter, same subscription set, dry run) ───────────────

async def mode_ccxt():
    p('python only')
    load_models()
    import ccxt.pro   # noqa
    from exchanges.registry import get_adapter
    cnt, size, sample, ot, ob, oc = make_counters()
    p('+ccxt + app registry')
    ad = get_adapter('binance_futures', '', '', False)
    await asyncio.wait_for(ad.connect(), 90)
    p('ccxt after connect')
    for s in SYMS:
        await ad.subscribe_ticker(s, ot)
        await ad.subscribe_orderbook(s, ob)
    for s, iv in KLINES:
        await ad.subscribe_candles(s, iv, oc)
    await asyncio.sleep(3)
    await stream_and_report('ccxt', cnt, size, sample)
    ex = ad._ex
    try:
        m = ex.market('BTC/USDT')
        print(f"[ccxt] market('BTC/USDT') -> symbol={m['symbol']} type={m['type']} id={m['id']}", flush=True)
    except Exception as e:
        print('[ccxt] market lookup error:', e)
    print('[ccxt] orderbooks keys now:', list(ex.orderbooks.keys()), flush=True)
    print('[ccxt] tickers keys now   :', list(ex.tickers.keys()), flush=True)
    p('ccxt FINAL')
    await ad.disconnect()


# ── Mode: parity (both feeds live, same instant sampling) ──────────────────────

async def mode_parity():
    load_models()
    import ccxt.pro   # noqa
    import aiohttp
    from exchanges.registry import get_adapter
    sym = 'BTC/USDT'
    last = {'c': {}, 's': {}}

    def mk(src):
        async def ot(t): last[src]['tick'] = t
        async def ob(b): last[src]['book'] = b
        async def oc(c): last[src]['candle'] = c
        return ot, ob, oc

    ad = get_adapter('binance_futures', '', '', False)
    await asyncio.wait_for(ad.connect(), 90)
    ct, cb, cc = mk('c')
    st_, sb, sc = mk('s')
    await ad.subscribe_ticker(sym, ct)
    await ad.subscribe_orderbook(sym, cb)
    await ad.subscribe_candles(sym, '1m', cc)
    feed = SlimFeed([sym], [(sym, '1m')], st_, sb, sc)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    print('warming up 10s...', flush=True)
    await asyncio.sleep(10)

    S = collections.Counter()
    price_d, vol_d, bid_d, ask_d = [], [], [], []
    t0 = time.monotonic()
    while time.monotonic() - t0 < DUR:
        await asyncio.sleep(1)
        a, b = last['c'].get('tick'), last['s'].get('tick')
        if a and b:
            S['tick_n'] += 1
            S['tick_price_eq'] += a.price == b.price
            S['tick_mark_eq_price_ccxt'] += a.mark_price == a.price
            price_d.append(abs(a.price - b.price))
            vol_d.append(abs(a.volume - b.volume))
        a, b = last['c'].get('book'), last['s'].get('book')
        if a and b and a.bids and b.bids and a.asks and b.asks:
            S['book_n'] += 1
            S['best_bid_eq'] += a.bids[0].price == b.bids[0].price
            S['best_ask_eq'] += a.asks[0].price == b.asks[0].price
            S['bid_prices_eq'] += [x.price for x in a.bids] == [x.price for x in b.bids]
            S['ask_prices_eq'] += [x.price for x in a.asks] == [x.price for x in b.asks]
            S['levels_20_both'] += (len(a.bids) == DEPTH and len(b.bids) == DEPTH
                                    and len(a.asks) == DEPTH and len(b.asks) == DEPTH)
            bid_d.append(abs(a.bids[0].price - b.bids[0].price))
            ask_d.append(abs(a.asks[0].price - b.asks[0].price))
        a, b = last['c'].get('candle'), last['s'].get('candle')
        if a and b:
            S['candle_n'] += 1
            S['candle_ts_eq'] += a.timestamp == b.timestamp
            if a.timestamp == b.timestamp:
                S['candle_ts_same_n'] += 1
                S['candle_close_eq'] += a.close == b.close
                S['candle_open_eq'] += a.open == b.open

    def pct(k, n):
        return f"{S[k]}/{S[n]} ({100.0 * S[k] / S[n]:.0f}%)" if S[n] else 'n/a'

    def mx(l):
        return f"max={max(l):.4f}" if l else 'n/a'

    print('\n===== PARITY (ccxt adapter vs slim feed, BTC/USDT, sampled each 1s) =====')
    print('tick price equal      :', pct('tick_price_eq', 'tick_n'), ' |Δprice|', mx(price_d))
    print('tick |Δvolume| (24h)  :', mx(vol_d))
    print('ccxt mark==last price :', pct('tick_mark_eq_price_ccxt', 'tick_n'))
    print('best bid equal        :', pct('best_bid_eq', 'book_n'), ' |Δ|', mx(bid_d))
    print('best ask equal        :', pct('best_ask_eq', 'book_n'), ' |Δ|', mx(ask_d))
    print('all 20 bid prices eq  :', pct('bid_prices_eq', 'book_n'))
    print('all 20 ask prices eq  :', pct('ask_prices_eq', 'book_n'))
    print('both have 20 levels   :', pct('levels_20_both', 'book_n'))
    print('candle same open-ts   :', pct('candle_ts_eq', 'candle_n'))
    print('candle close equal    :', pct('candle_close_eq', 'candle_ts_same_n'),
          '| open equal:', pct('candle_open_eq', 'candle_ts_same_n'))
    for k, v in feed.raw.items():
        print(f"RAW binance {k}: {v}")

    print('\n===== FUNDING INFO (ccxt adapter vs raw REST) =====')
    try:
        print('ccxt adapter fetch_funding_info:', await ad.fetch_funding_info(sym), flush=True)
    except Exception as e:
        print('ccxt funding error:', e)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{REST}/fapi/v1/premiumIndex",
                                params={'symbol': sid(sym).upper()}) as r:
                j = await r.json()
            print('REST premiumIndex   : lastFundingRate=%s nextFundingTime=%s -> %s'
                  % (j.get('lastFundingRate'), j.get('nextFundingTime'),
                     utc(j['nextFundingTime']).isoformat() if j.get('nextFundingTime') else None))
            async with sess.get(f"{REST}/fapi/v1/fundingInfo") as r:
                fi = await r.json()
            row = [x for x in fi if x.get('symbol') == sid(sym).upper()]
            print('REST fundingInfo row:', row[0] if row else 'none (default interval)')
    except Exception as e:
        print('REST funding error:', e)

    stop.set()
    task.cancel()
    await ad.disconnect()


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if mode == 'all':
        me = os.path.abspath(__file__)
        for m in ('slim', 'ccxt', 'parity'):
            print(f"\n===== {m} =====", flush=True)
            try:
                subprocess.run([sys.executable, '-u', me, m], timeout=300)
            except subprocess.TimeoutExpired:
                print(f"{m}: timed out after 300s", flush=True)
        print('\n===== done =====', flush=True)
    else:
        fn = {'slim': mode_slim, 'ccxt': mode_ccxt, 'parity': mode_parity}[mode]
        asyncio.run(asyncio.wait_for(fn(), 240))

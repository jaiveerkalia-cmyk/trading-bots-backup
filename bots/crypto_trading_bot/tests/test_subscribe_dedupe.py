"""
Tests for market-data subscribe de-duplication (no Redis, no exchange).

Run (inside the market-data image):
  docker compose run --rm --no-deps -T crypto-market-data python tests/test_subscribe_dedupe.py
"""
import asyncio

from market_data_service import main as md


class FakeAdapter:
    def __init__(self, fail_ticker_times: int = 0):
        self.calls: list[tuple] = []
        self._fail = fail_ticker_times

    async def subscribe_ticker(self, symbol, cb):
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError('boom')
        self.calls.append(('ticker', symbol))

    async def subscribe_orderbook(self, symbol, cb):
        self.calls.append(('orderbook', symbol))

    async def subscribe_candles(self, symbol, interval, cb):
        self.calls.append(('candles', symbol, interval))


class FakePublisher:
    async def publish(self, data):
        pass


def _sub(adapter, symbol='BTC/USDT', streams=('ticker',), exchange='fx'):
    sub = {'exchange': exchange, 'symbol': symbol, 'streams': list(streams)}
    asyncio.run(md._do_subscribe(sub, {exchange: adapter}, set(), FakePublisher(), {}, False))


def _fresh():
    md._live_streams.clear()
    return FakeAdapter()


def test_repeat_ticker_subscribe_starts_one_set_of_loops():
    a = _fresh()
    for _ in range(50):
        _sub(a)
    assert a.calls == [('ticker', 'BTC/USDT'), ('orderbook', 'BTC/USDT')], a.calls


def test_new_candle_stream_added_once():
    a = _fresh()
    _sub(a)
    _sub(a, streams=('ticker', 'candles:1m'))
    _sub(a, streams=('ticker', 'candles:1m'))
    _sub(a, streams=('ticker', 'candles:5m'))
    assert a.calls == [
        ('ticker', 'BTC/USDT'), ('orderbook', 'BTC/USDT'),
        ('candles', 'BTC/USDT', '1m'),
        ('candles', 'BTC/USDT', '5m'),
    ], a.calls


def test_explicit_orderbook_after_ticker_is_skipped():
    a = _fresh()
    _sub(a)
    _sub(a, streams=('orderbook',))
    assert a.calls.count(('orderbook', 'BTC/USDT')) == 1


def test_other_symbols_and_exchanges_are_independent():
    a = _fresh()
    _sub(a, 'BTC/USDT')
    _sub(a, 'ETH/USDT')
    _sub(a, 'BTC/USDT', exchange='other')
    assert a.calls.count(('ticker', 'BTC/USDT')) == 2     # fx + other
    assert a.calls.count(('ticker', 'ETH/USDT')) == 1


def test_unsubscribe_clears_so_resubscribe_works():
    a = _fresh()
    _sub(a)
    md._forget_streams('fx', 'BTC/USDT')
    _sub(a)
    assert a.calls.count(('ticker', 'BTC/USDT')) == 2
    assert a.calls.count(('orderbook', 'BTC/USDT')) == 2


def test_forget_only_touches_that_symbol():
    a = _fresh()
    _sub(a, 'BTC/USDT')
    _sub(a, 'ETH/USDT')
    md._forget_streams('fx', 'BTC/USDT')
    _sub(a, 'ETH/USDT')                                   # still running -> skipped
    assert a.calls.count(('ticker', 'ETH/USDT')) == 1


def test_failed_subscribe_is_not_recorded_and_retries():
    md._live_streams.clear()
    a = FakeAdapter(fail_ticker_times=1)
    _sub(a)                                               # ticker raises -> nothing recorded
    assert ('fx', 'BTC/USDT', 'ticker') not in md._live_streams
    _sub(a)                                               # retry succeeds
    assert ('ticker', 'BTC/USDT') in a.calls
    assert ('fx', 'BTC/USDT', 'ticker') in md._live_streams


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS  {name}')
        except Exception as e:   # noqa: BLE001
            failed += 1
            print(f'FAIL  {name}: {e!r}')
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    raise SystemExit(1 if failed else 0)

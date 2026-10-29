"""
Tests for candle-close periods (15m / 30m / 1h alerts and conditional fire-on).
Pure logic — no Redis, no exchange.

Run (inside the trading-engine image):
  docker compose run --rm --no-deps -T crypto-trading-engine python tests/test_candle_periods.py
or with pytest if installed:
  docker compose run --rm --no-deps -T crypto-trading-engine python -m pytest tests/test_candle_periods.py
"""
import typing
from datetime import datetime, timezone

from common.models import Alert, TradeSlot
from common.periods import (
    CANDLE_PERIODS, LEGACY_PERIODS, closed_candle_open_ms,
    is_boundary, periods_for_exchange, select_closed_candle,
)


def _utc(h: int, m: int, day: int = 1) -> datetime:
    return datetime(2026, 1, day, h, m, 0, tzinfo=timezone.utc)


def _due(dt: datetime) -> list[str]:
    return [iv for iv in CANDLE_PERIODS if is_boundary(dt, iv)]


# ── Boundaries ────────────────────────────────────────────────────────────────

def test_boundaries_table():
    assert _due(_utc(10, 1))  == ['1m']
    assert _due(_utc(10, 5))  == ['1m', '5m']
    assert _due(_utc(10, 15)) == ['1m', '5m', '15m']
    assert _due(_utc(10, 30)) == ['1m', '5m', '15m', '30m']
    assert _due(_utc(10, 45)) == ['1m', '5m', '15m']
    assert _due(_utc(11, 0))  == ['1m', '5m', '15m', '30m', '1h']
    assert _due(_utc(0, 0))   == ['1m', '5m', '15m', '30m', '1h']          # midnight UTC
    assert _due(_utc(0, 0, day=2)) == ['1m', '5m', '15m', '30m', '1h']     # day wrap


def test_hour_bars_are_utc_aligned():
    # 04:00 UTC == 09:30 IST -> a real 1h close; 05:30 UTC == 11:00 IST is NOT
    assert is_boundary(_utc(4, 0), '1h')
    assert not is_boundary(_utc(5, 30), '1h')


# ── Closed-candle selection (by open time) ────────────────────────────────────

def _ms(dt: datetime) -> int:
    return int(dt.timestamp()) * 1000


def test_closed_candle_open_ms():
    assert closed_candle_open_ms(_utc(11, 0),  '1h')  == _ms(_utc(10, 0))
    assert closed_candle_open_ms(_utc(10, 30), '30m') == _ms(_utc(10, 0))
    assert closed_candle_open_ms(_utc(10, 15), '15m') == _ms(_utc(10, 0))
    assert closed_candle_open_ms(_utc(10, 5),  '5m')  == _ms(_utc(10, 0))
    assert closed_candle_open_ms(_utc(10, 1),  '1m')  == _ms(_utc(10, 0))


def test_select_normal_case():
    rows = [[_ms(_utc(9, 45)), 1, 1, 1, 10.0, 1],
            [_ms(_utc(10, 0)), 1, 1, 1, 11.0, 1],
            [_ms(_utc(10, 15)), 1, 1, 1, 12.0, 1]]   # last row = forming candle
    row = select_closed_candle(rows, _utc(10, 15), '15m')
    assert row is not None and row[4] == 11.0


def test_select_gap_returns_none_not_wrong_candle():
    # no trades 10:00-10:15 -> exchange has no row for it; [-2] would be 9:45's candle
    rows = [[_ms(_utc(9, 30)), 1, 1, 1, 9.0, 1],
            [_ms(_utc(9, 45)), 1, 1, 1, 10.0, 1],
            [_ms(_utc(10, 15)), 1, 1, 1, 12.0, 1]]
    assert select_closed_candle(rows, _utc(10, 15), '15m') is None


def test_select_lagging_rest_returns_none():
    # REST has not produced the 10:15 row yet; newest row is the previous candle
    rows = [[_ms(_utc(9, 30)), 1, 1, 1, 9.0, 1],
            [_ms(_utc(9, 45)), 1, 1, 1, 10.0, 1]]
    assert select_closed_candle(rows, _utc(10, 15), '15m') is None


def test_select_when_closed_candle_is_last_row():
    # illiquid symbol: new candle not created yet, closed candle is the LAST row
    rows = [[_ms(_utc(9, 45)), 1, 1, 1, 10.0, 1],
            [_ms(_utc(10, 0)), 1, 1, 1, 11.0, 1]]
    row = select_closed_candle(rows, _utc(10, 15), '15m')
    assert row is not None and row[4] == 11.0


def test_select_empty_and_malformed():
    assert select_closed_candle([], _utc(10, 15), '15m') is None
    assert select_closed_candle(None, _utc(10, 15), '15m') is None
    assert select_closed_candle([[], ['x'], None], _utc(10, 15), '15m') is None


def test_select_float_timestamps_from_ccxt():
    rows = [[float(_ms(_utc(10, 0))), 1, 1, 1, 11.0, 1]]
    row = select_closed_candle(rows, _utc(10, 15), '15m')
    assert row is not None and row[4] == 11.0


# ── Exchange support ──────────────────────────────────────────────────────────

def test_periods_for_exchange():
    assert periods_for_exchange('binance_futures') == ['1m', '5m', '15m', '30m', '1h']
    assert periods_for_exchange('binance')         == ['1m', '5m', '15m', '30m', '1h']
    assert periods_for_exchange('delta')           == list(LEGACY_PERIODS) == ['1m', '5m']


# ── Models stay in sync with the table ────────────────────────────────────────

def _literal_values(annotation) -> set:
    if typing.get_origin(annotation) is typing.Literal:
        return set(typing.get_args(annotation))
    for a in typing.get_args(annotation):
        if typing.get_origin(a) is typing.Literal:
            return set(typing.get_args(a))
    raise AssertionError(f'no Literal in {annotation!r}')


def test_models_match_periods_table():
    assert _literal_values(Alert.model_fields['period'].annotation) == {'current', *CANDLE_PERIODS}
    assert _literal_values(TradeSlot.model_fields['fire_on'].annotation) == set(CANDLE_PERIODS)


def test_models_accept_new_and_old_values():
    for p in ('current', '1m', '5m', '15m', '30m', '1h'):
        assert Alert.model_validate({'exchange': 'binance_futures', 'symbol': 'BTC/USDT', 'period': p}).period == p
    for p in (None, '1m', '5m', '15m', '30m', '1h'):
        s = TradeSlot.model_validate({'exchange': 'binance_futures', 'symbol': 'BTC/USDT',
                                      'side': 'long', 'fire_on': p})
        assert s.fire_on == p


def test_models_reject_unknown_period():
    for bad in ('2h', '4h', '3m', 'live'):
        try:
            Alert.model_validate({'exchange': 'x', 'symbol': 'A/B', 'period': bad})
        except Exception:
            pass
        else:
            raise AssertionError(f'Alert accepted bad period {bad}')


# ── Public kline REST (engine adapters are not connected in paper mode) ───────────────

class _FakeResp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, status, payload):
        self.status, self.payload, self.calls = status, payload, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return _FakeResp(self.status, self.payload)


def test_native_symbol():
    from common import klines
    assert klines.native_symbol('BTC/USDT') == 'BTCUSDT'
    assert klines.native_symbol('BTC/USDT:USDT') == 'BTCUSDT'
    assert klines.native_symbol('xau/usdt') == 'XAUUSDT'


def test_fetch_klines_parses_rows_and_builds_request():
    import asyncio
    from common import klines
    payload = [
        [1000, '1.0', '2.0', '0.5', '1.5', '10', 1999, '0', 5, '1', '1', '0'],
        [2000, '1.5', '3.0', '1.0', '2.5', '20', 2999, '0', 6, '1', '1', '0'],
        ['bad'],
    ]
    s = _FakeSession(200, payload)
    rows = asyncio.run(klines.fetch_klines(s, 'binance_futures', 'BTC/USDT', '15m', limit=3))
    assert rows == [[1000, 1.0, 2.0, 0.5, 1.5, 10.0], [2000, 1.5, 3.0, 1.0, 2.5, 20.0]]
    url, params = s.calls[0]
    assert url == 'https://fapi.binance.com/fapi/v1/klines'
    assert params == {'symbol': 'BTCUSDT', 'interval': '15m', 'limit': 3}
    s2 = _FakeSession(200, payload)
    asyncio.run(klines.fetch_klines(s2, 'binance', 'ETH/USDT', '1h'))
    assert s2.calls[0][0] == 'https://api.binance.com/api/v3/klines'


def test_fetch_klines_failures_return_empty():
    import asyncio
    from common import klines
    assert asyncio.run(klines.fetch_klines(_FakeSession(429, []), 'binance_futures', 'BTC/USDT', '15m')) == []
    assert asyncio.run(klines.fetch_klines(_FakeSession(200, {'code': -1121}), 'binance_futures', 'X/USDT', '15m')) == []
    assert asyncio.run(klines.fetch_klines(_FakeSession(200, []), 'delta', 'BTC/USDT', '15m')) == []
    assert klines.supports('binance_futures') and klines.supports('binance') and not klines.supports('delta')


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

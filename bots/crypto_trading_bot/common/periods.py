"""
Candle-close periods shared by alerts and conditional ("Fire on") orders.

Single source of truth — the trading engine and the UI selects both read this
table, so adding an interval later is a one-line change here (plus the two
Literal types in common/models.py, which tests/test_candle_periods.py keeps in
sync with this table).

Candle boundaries are UTC-epoch aligned, matching Binance klines for these
intervals (so 1h bars close at :30 IST).
"""
from __future__ import annotations
from datetime import datetime

# interval -> length in minutes
CANDLE_PERIODS: dict[str, int] = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60}

# Periods that work on every exchange (they have a Redis-candle fallback).
LEGACY_PERIODS: tuple[str, ...] = ('1m', '5m')

# Exchanges whose adapter has no REST fetch_ohlcv: only LEGACY_PERIODS are offered.
_LEGACY_ONLY_EXCHANGES = frozenset({'delta'})


def periods_for_exchange(exchange: str) -> list[str]:
    """Candle-close periods that can be evaluated reliably on `exchange`."""
    if exchange in _LEGACY_ONLY_EXCHANGES:
        return list(LEGACY_PERIODS)
    return list(CANDLE_PERIODS)


def is_boundary(boundary: datetime, interval: str) -> bool:
    """True if `boundary` (tz-aware, minute-aligned) is a candle-close time for `interval`."""
    return (int(boundary.timestamp()) // 60) % CANDLE_PERIODS[interval] == 0


def closed_candle_open_ms(boundary: datetime, interval: str) -> int:
    """Open time (epoch ms) of the candle that closed at `boundary`."""
    return int(boundary.timestamp()) * 1000 - CANDLE_PERIODS[interval] * 60_000


def select_closed_candle(rows: list, boundary: datetime, interval: str):
    """
    The OHLCV row ([ts_ms, o, h, l, c, v]) for the candle that closed at
    `boundary`, matched by OPEN TIME — not by list position — or None.

    Position-based picking ([-2]) returns the wrong candle when the symbol had
    no trades in the previous interval (exchange skips the empty candle) or
    when the REST response lags the boundary.
    """
    want = closed_candle_open_ms(boundary, interval)
    for row in reversed(rows or []):
        try:
            if int(row[0]) == want:
                return row
        except (TypeError, ValueError, IndexError):
            continue
    return None

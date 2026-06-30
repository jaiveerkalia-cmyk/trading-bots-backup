"""
Two single append-only CSV files — never date-partitioned:
  data/portfolio/portfolio.csv  — position lifecycle with full details
  data/trades/trades.csv        — every individual order fill
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from common import settings

logger = logging.getLogger('csv_writer')

# ── Column definitions ────────────────────────────────────────────────────────

PORTFOLIO_COLS = [
    'timestamp', 'event_type',            # open | close | partial_close
    'exchange', 'symbol', 'side',
    'qty', 'leverage',
    'entry_price', 'exit_price',
    'trade_pnl', 'funding_pnl', 'cumulative_pnl',
    'close_reason',                        # manual | stop_hit | target_hit | pnl_target_hit
    'is_paper', 'slot_id',
]

TRADE_COLS = [
    'timestamp',
    'exchange', 'symbol', 'side', 'order_type',
    'filled_qty', 'avg_fill_price',
    'is_paper', 'slot_id', 'order_id',
]


def _portfolio_path() -> Path:
    return settings.PORTFOLIO_DIR / 'portfolio.csv'


def _trades_path() -> Path:
    return settings.TRADES_DIR / 'trades.csv'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure(path: Path, cols: list[str]) -> None:
    if not path.exists() or path.stat().st_size == 0:
        with path.open('w', newline='') as f:
            csv.DictWriter(f, fieldnames=cols).writeheader()


def _append(path: Path, cols: list[str], row: dict) -> None:
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        if new_file:
            w.writeheader()
        w.writerow({c: row.get(c, '') for c in cols})


class CSVWriter:
    __slots__ = ('_queue', '_task')

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=500)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        _ensure(_portfolio_path(), PORTFOLIO_COLS)
        _ensure(_trades_path(),    TRADE_COLS)
        self._task = asyncio.create_task(self._drain())
        logger.info("CSVWriter started — portfolio=%s trades=%s",
                    _portfolio_path(), _trades_path())

    async def stop(self) -> None:
        if self._task:
            await self._queue.join()
            self._task.cancel()

    # ── Public API ────────────────────────────────────────────────────────────

    async def enqueue_event(self, row: dict) -> None:
        """Position lifecycle event (open / close / partial_close)."""
        row.setdefault('timestamp', _now())
        await self._put(('portfolio', row))

    async def enqueue_position(self, row: dict) -> None:
        """Alias for enqueue_event."""
        await self.enqueue_event(row)

    async def enqueue_order_fill(self, row: dict) -> None:
        """Individual order fill record."""
        row.setdefault('timestamp', _now())
        await self._put(('trade', row))

    # Legacy shims
    async def enqueue_trade(self, row: dict) -> None:
        mapped = {
            'timestamp':       row.get('timestamp', _now()),
            'event_type':      'close' if row.get('exit_price') else 'open',
            'exchange':        row.get('exchange', ''),
            'symbol':          row.get('symbol', ''),
            'side':            row.get('side', ''),
            'qty':             row.get('qty', ''),
            'entry_price':     row.get('entry_price', ''),
            'exit_price':      row.get('exit_price', ''),
            'trade_pnl':       row.get('pnl', row.get('trade_pnl', '')),
            'cumulative_pnl':  row.get('realized_pnl', ''),
            'is_paper':        row.get('is_paper', ''),
            'slot_id':         row.get('slot_id', ''),
        }
        await self._put(('portfolio', mapped))

    async def enqueue_pnl(self, _row: dict) -> None:
        pass   # daily PnL tracking removed

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _put(self, item: tuple[str, dict]) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("CSVWriter queue full — row dropped")

    async def _drain(self) -> None:
        while True:
            try:
                kind, row = await self._queue.get()
                if kind == 'portfolio':
                    _append(_portfolio_path(), PORTFOLIO_COLS, row)
                elif kind == 'trade':
                    _append(_trades_path(), TRADE_COLS, row)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("CSV write error: %s", e)

"""
Single-writer async CSV queue.
All components call enqueue_trade() / enqueue_pnl().
One drain task serialises all writes — no concurrent file access.
"""
from __future__ import annotations
import asyncio
import csv
import logging
from datetime import datetime
from typing import Literal

from common import settings

logger = logging.getLogger('csv_writer')

TRADE_COLS = [
    'timestamp', 'exchange', 'symbol', 'side', 'order_type',
    'qty', 'entry_price', 'exit_price', 'pnl', 'is_paper', 'slot_id',
]
PNL_COLS = ['date', 'realized_pnl', 'trade_count']

_Kind = Literal['trade', 'pnl']


class CSVWriter:
    __slots__ = ('_queue', '_task')

    def __init__(self):
        self._queue: asyncio.Queue[tuple[_Kind, dict]] = asyncio.Queue(maxsize=500)
        self._task:  asyncio.Task | None = None

    async def start(self) -> None:
        self._ensure_headers()
        self._task = asyncio.create_task(self._drain())
        logger.info("CSVWriter started")

    async def stop(self) -> None:
        if self._task:
            await self._queue.join()   # flush remaining before exit
            self._task.cancel()

    async def enqueue_trade(self, row: dict) -> None:
        await self._put('trade', row)

    async def enqueue_pnl(self, row: dict) -> None:
        await self._put('pnl', row)

    async def _put(self, kind: _Kind, row: dict) -> None:
        try:
            self._queue.put_nowait((kind, row))
        except asyncio.QueueFull:
            logger.warning(f"CSVWriter queue full — dropping {kind} row")

    async def _drain(self) -> None:
        while True:
            try:
                kind, row = await self._queue.get()
                self._write(kind, row)
                self._queue.task_done()
                del row
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CSV write error: {e}")

    def _write(self, kind: _Kind, row: dict) -> None:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        if kind == 'trade':
            path = settings.TRADES_DIR   / f"trades_{today}.csv"
            cols = TRADE_COLS
        else:
            path = settings.DAILY_PNL_DIR / f"pnl_{today}.csv"
            cols = PNL_COLS
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open('a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            if new_file:
                w.writeheader()
            w.writerow(row)

    def _ensure_headers(self) -> None:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        for name, cols in (
            (f"trades_{today}.csv", TRADE_COLS),
            (f"pnl_{today}.csv",    PNL_COLS),
        ):
            path = settings.TRADES_DIR / name if 'trade' in name else settings.DAILY_PNL_DIR / name
            if not path.exists():
                with path.open('w', newline='') as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()

"""
Unified portfolio CSV writer.

One file per day:  <PORTFOLIO_DIR>/portfolio_YYYY-MM-DD.csv

All trade opens, closes, partial closes and PnL snapshots go here.
enqueue_trade() and enqueue_pnl() are kept for backward compatibility.
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from typing import Literal

from common import settings

logger = logging.getLogger('csv_writer')

PORTFOLIO_COLS: list[str] = [
    'timestamp',
    'event_type',       # open | close | partial_close | snapshot
    'exchange',
    'symbol',
    'side',
    'order_type',
    'qty',
    'entry_price',
    'exit_price',
    'trade_pnl',
    'realized_pnl',     # cumulative session realized PnL
    'portfolio_value',  # starting_balance + realized  (caller fills if known)
    'is_paper',
    'slot_id',
    'notes',
]

_Kind = Literal['open', 'close', 'partial_close', 'snapshot']


class CSVWriter:
    __slots__ = ('_queue', '_task')

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        self._task:  asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._ensure_header()
        self._task = asyncio.create_task(self._drain())
        logger.info("CSVWriter started → %s", self._today_path())

    async def stop(self) -> None:
        if self._task:
            await self._queue.join()
            self._task.cancel()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def enqueue_event(self, row: dict) -> None:
        """Enqueue a fully-formed portfolio row (keys = PORTFOLIO_COLS)."""
        await self._put(row)

    async def enqueue_trade(self, row: dict) -> None:
        """Backward-compatible shim — maps old trade dict to unified schema."""
        mapped: dict = {
            'timestamp':       row.get('timestamp', self._now()),
            'event_type':      'close' if row.get('exit_price') else 'open',
            'exchange':        row.get('exchange',    ''),
            'symbol':          row.get('symbol',      ''),
            'side':            row.get('side',        ''),
            'order_type':      row.get('order_type',  ''),
            'qty':             row.get('qty',         ''),
            'entry_price':     row.get('entry_price', ''),
            'exit_price':      row.get('exit_price',  ''),
            'trade_pnl':       row.get('pnl', row.get('trade_pnl', '')),
            'realized_pnl':    row.get('realized_pnl',    ''),
            'portfolio_value': row.get('portfolio_value', ''),
            'is_paper':        row.get('is_paper',        ''),
            'slot_id':         row.get('slot_id',         ''),
            'notes':           row.get('notes',           ''),
        }
        await self._put(mapped)

    async def enqueue_pnl(self, row: dict) -> None:
        """Backward-compatible shim — maps old PnL summary to snapshot row."""
        mapped: dict = {
            'timestamp':       row.get('date', self._now()),
            'event_type':      'snapshot',
            'exchange':        '',
            'symbol':          '',
            'side':            '',
            'order_type':      '',
            'qty':             '',
            'entry_price':     '',
            'exit_price':      '',
            'trade_pnl':       '',
            'realized_pnl':    row.get('realized_pnl',    ''),
            'portfolio_value': row.get('portfolio_value', ''),
            'is_paper':        '',
            'slot_id':         '',
            'notes':           f"trade_count={row.get('trade_count', '')}",
        }
        await self._put(mapped)

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _put(self, row: dict) -> None:
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("CSVWriter queue full — row dropped")

    async def _drain(self) -> None:
        while True:
            try:
                row = await self._queue.get()
                self._write(row)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("CSV write error: %s", exc)

    def _write(self, row: dict) -> None:
        path     = self._today_path()
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open('a', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=PORTFOLIO_COLS, extrasaction='ignore')
            if new_file:
                w.writeheader()
            w.writerow({col: row.get(col, '') for col in PORTFOLIO_COLS})

    def _ensure_header(self) -> None:
        path = self._today_path()
        if not path.exists():
            with path.open('w', newline='') as fh:
                csv.DictWriter(fh, fieldnames=PORTFOLIO_COLS).writeheader()

    @staticmethod
    def _today_path():
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        return settings.PORTFOLIO_DIR / f"portfolio_{today}.csv"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

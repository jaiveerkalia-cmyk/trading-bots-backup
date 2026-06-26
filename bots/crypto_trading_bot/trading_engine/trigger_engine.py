"""
TriggerEngine — background monitor for stop/target exits and alert triggers.

Price-source rules (Binance Futures / Delta):
  • Stop-loss   → checked against MARK PRICE  (prevents wick manipulation)
  • Take-profit → checked against LTP          (last trade price)
  • Alerts      → always LTP
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable, TYPE_CHECKING

import redis.asyncio as aioredis

from common.models import Alert
from common import redis_keys, settings

if TYPE_CHECKING:
    from trading_engine.trade_slot  import SlotManager
    from trading_engine.paper_engine import PaperEngine

logger = logging.getLogger('trigger_engine')


class TriggerEngine:
    __slots__ = ('_redis', '_slots', '_paper', '_on_exit', '_on_alert', '_task')

    def __init__(
        self,
        redis_client: aioredis.Redis,
        slot_manager: 'SlotManager',
        on_exit:      Callable[[str, str], Awaitable[None]],
        on_alert:     Callable[[Alert],    Awaitable[None]],
        paper_engine: 'PaperEngine',
    ) -> None:
        self._redis    = redis_client
        self._slots    = slot_manager
        self._paper    = paper_engine
        self._on_exit  = on_exit
        self._on_alert = on_alert
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("TriggerEngine started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("TriggerEngine error: %s", exc)
            await asyncio.sleep(settings.ENGINE_STATE_PUBLISH_INTERVAL)

    async def _check_all(self) -> None:
        updated: set[str] = set()

        for slot in self._slots.get_active_slots():
            if not slot.position:
                continue

            # Mark price: drives stop checks + paper PnL mark-to-market
            mark = await self._price(slot.exchange, slot.symbol)
            if mark <= 0:
                continue

            sym_key = f"{slot.exchange}:{slot.symbol}"
            if slot.position.is_paper and sym_key not in updated:
                await self._paper.update_mark_prices(slot.exchange, slot.symbol, mark)
                updated.add(sym_key)

            # ── Stop: MARK PRICE only ──────────────────────────────────────
            if slot.stop_price:
                hit = (
                    (slot.side == 'long'  and mark <= slot.stop_price) or
                    (slot.side == 'short' and mark >= slot.stop_price)
                )
                if hit:
                    logger.info(
                        "Stop hit [%s] mark=%.6g stop=%.6g",
                        slot.symbol, mark, slot.stop_price,
                    )
                    await self._on_exit(slot.id, 'stop_hit')
                    continue

            # ── Target: LTP (last trade price) ────────────────────────────
            if slot.target_price:
                ltp = await self._ltp(slot.exchange, slot.symbol)
                chk = ltp if ltp > 0 else mark     # fall back to mark if LTP absent
                hit = (
                    (slot.side == 'long'  and chk >= slot.target_price) or
                    (slot.side == 'short' and chk <= slot.target_price)
                )
                if hit:
                    logger.info(
                        "Target hit [%s] ltp=%.6g target=%.6g",
                        slot.symbol, chk, slot.target_price,
                    )
                    await self._on_exit(slot.id, 'target_hit')

        # ── Alerts: always LTP ────────────────────────────────────────────────
        for alert in self._slots.get_alerts():
            if alert.triggered:
                continue
            ltp = await self._ltp(alert.exchange, alert.symbol)
            if ltp <= 0:
                continue
            triggered = (
                (alert.upper is not None and ltp >= alert.upper) or
                (alert.lower is not None and ltp <= alert.lower)
            )
            if triggered:
                alert.triggered = True
                logger.info("Alert triggered [%s] ltp=%.6g", alert.symbol, ltp)
                await self._on_alert(alert)

    # ── Price helpers ──────────────────────────────────────────────────────────

    async def _price(self, exchange: str, symbol: str) -> float:
        """
        Mark price for futures/delta, LTP for spot.
        Used for stop checks and paper PnL mark-to-market.
        """
        try:
            if 'futures' in exchange or exchange == 'delta':
                raw = await self._redis.get(
                    redis_keys.mark_price_key(exchange, symbol)
                )
                if raw:
                    p = float(json.loads(raw).get('p', 0))
                    if p > 0:
                        return p
            # Spot or mark unavailable → fall through to LTP
            return await self._ltp(exchange, symbol)
        except Exception:
            return 0.0

    async def _ltp(self, exchange: str, symbol: str) -> float:
        """Last trade price from the tick stream."""
        try:
            raw = await self._redis.get(
                redis_keys.latest_tick_key(exchange, symbol)
            )
            return float(json.loads(raw).get('p', 0)) if raw else 0.0
        except Exception:
            return 0.0

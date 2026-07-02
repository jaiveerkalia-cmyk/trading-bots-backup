"""
TriggerEngine — stop/target/pnl_target exits + price and candle-close alerts.

Alert periods:
  current — checked every tick against LTP
  1m / 5m — checked against the CLOSE price of completed 1m/5m candles
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, TYPE_CHECKING

import redis.asyncio as aioredis

from common.models import Alert
from common import redis_keys, settings

if TYPE_CHECKING:
    from trading_engine.trade_slot   import SlotManager
    from trading_engine.paper_engine import PaperEngine

logger = logging.getLogger('trigger_engine')


class TriggerEngine:
    __slots__ = ('_redis', '_slots', '_paper', '_on_exit', '_on_alert',
                 '_task', '_clock_task', '_adapters')

    def __init__(
        self,
        redis_client: aioredis.Redis,
        slot_manager: 'SlotManager',
        on_exit:      Callable[[str, str], Awaitable[None]],
        on_alert:     Callable[[Alert],    Awaitable[None]],
        paper_engine: 'PaperEngine',
        adapters:     dict | None = None,
    ) -> None:
        self._redis       = redis_client
        self._slots       = slot_manager
        self._paper       = paper_engine
        self._on_exit     = on_exit
        self._on_alert    = on_alert
        self._adapters    = adapters or {}
        self._task:        asyncio.Task | None = None
        self._clock_task:  asyncio.Task | None = None

    async def start(self) -> None:
        self._task       = asyncio.create_task(self._loop())
        self._clock_task = asyncio.create_task(self._candle_clock_task())
        logger.info("TriggerEngine started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._clock_task:
            self._clock_task.cancel()

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
            mark = await self._price(slot.exchange, slot.symbol)
            if mark <= 0:
                continue
            sym_key = f"{slot.exchange}:{slot.symbol}"
            if slot.position.is_paper and sym_key not in updated:
                funding = await self._funding_rate(slot.exchange, slot.symbol)
                await self._paper.update_mark_prices(
                    slot.exchange, slot.symbol, mark, funding
                )
                updated.add(sym_key)

            # ── Stop: mark price ──────────────────────────────────────────
            if slot.stop_price:
                if ((slot.side == 'long'  and mark <= slot.stop_price) or
                        (slot.side == 'short' and mark >= slot.stop_price)):
                    logger.info("Stop hit [%s] mark=%.6g stop=%.6g",
                                slot.symbol, mark, slot.stop_price)
                    await self._on_exit(slot.id, 'stop_hit')
                    continue

            # ── Target: LTP ───────────────────────────────────────────────
            if slot.target_price:
                ltp = await self._ltp(slot.exchange, slot.symbol)
                chk = ltp if ltp > 0 else mark
                if ((slot.side == 'long'  and chk >= slot.target_price) or
                        (slot.side == 'short' and chk <= slot.target_price)):
                    logger.info("Target hit [%s] ltp=%.6g target=%.6g",
                                slot.symbol, chk, slot.target_price)
                    await self._on_exit(slot.id, 'target_hit')
                    continue

            # ── PnL target ────────────────────────────────────────────────
            if slot.pnl_target is not None and slot.position:
                curr_pnl = slot.position.unrealized_pnl
                if ((slot.pnl_target >= 0 and curr_pnl >= slot.pnl_target) or
                        (slot.pnl_target < 0  and curr_pnl <= slot.pnl_target)):
                    logger.info("PnL target hit [%s] pnl=%.4f target=%.4f",
                                slot.symbol, curr_pnl, slot.pnl_target)
                    await self._on_exit(slot.id, 'pnl_target_hit')

        # ── Alerts (LTP/current only — candle-close alerts run in _candle_clock_task) ──
        for alert in self._slots.get_alerts():
            if alert.triggered:
                continue
            period = getattr(alert, 'period', 'current')
            if period in ('1m', '5m'):
                continue   # handled by _candle_clock_task
            ltp = await self._ltp(alert.exchange, alert.symbol)
            if ltp <= 0:
                continue
            triggered = (
                (alert.upper is not None and ltp >= alert.upper) or
                (alert.lower is not None and ltp <= alert.lower)
            )
            if triggered:
                alert.triggered = True
                logger.info("Alert triggered [%s] period=%s", alert.symbol, period)
                await self._on_alert(alert)

    # ── Candle-close alert (clock-based) ─────────────────────────────────────

    async def _candle_clock_task(self) -> None:
        """
        Runs independently of the main trigger loop.
        Sleeps precisely until each 1-minute wall-clock boundary + 1 s
        (the extra second lets the exchange REST endpoint reflect the
        just-closed candle before we query it).

        At each 1 m boundary  → evaluates all 1 m candle-close alerts.
        At each 5 m boundary  → also evaluates all 5 m candle-close alerts.

        Fetches official OHLC via the exchange REST API (fetch_ohlcv) so the
        close price is the exchange-confirmed final value — not a live tick.
        Falls back to the latest_candle Redis key if the adapter is unavailable.
        """
        while True:
            try:
                # Sleep until next 1 m boundary + 1 s
                now      = datetime.now(timezone.utc)
                next_min = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                sleep_s  = (next_min - now).total_seconds() + 1.0
                await asyncio.sleep(max(sleep_s, 0.1))

                now   = datetime.now(timezone.utc)
                is_5m = (now.minute % 5 == 0)

                # Build {(exchange, symbol, interval): [alerts]} mapping
                needed: dict[tuple, list] = {}
                for alert in self._slots.get_alerts():
                    if alert.triggered:
                        continue
                    period = getattr(alert, 'period', 'current')
                    if period == '1m':
                        k = (alert.exchange, alert.symbol, '1m')
                        needed.setdefault(k, []).append(alert)
                    elif period == '5m' and is_5m:
                        k = (alert.exchange, alert.symbol, '5m')
                        needed.setdefault(k, []).append(alert)

                for (exchange, symbol, interval), alerts in needed.items():
                    close = await self._fetch_closed_candle_close(
                        exchange, symbol, interval
                    )
                    if not close or close <= 0:
                        logger.debug(
                            "No closed candle data for %s %s %s",
                            exchange, symbol, interval,
                        )
                        continue

                    for alert in alerts:
                        if alert.triggered:
                            continue
                        triggered = (
                            (alert.upper is not None and close >= alert.upper) or
                            (alert.lower is not None and close <= alert.lower)
                        )
                        if triggered:
                            alert.triggered = True
                            logger.info(
                                "Candle-close alert [%s] %s close=%.6g",
                                alert.symbol, interval, close,
                            )
                            await self._on_alert(alert)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Candle clock task error: %s", exc)

    async def _fetch_closed_candle_close(
        self, exchange: str, symbol: str, interval: str
    ) -> float:
        """
        Returns the close price of the most recently COMPLETED candle.

        1. Tries the exchange REST API (fetch_ohlcv limit=3).
           ccxt returns rows sorted oldest-first; the last row may be the
           current open (unclosed) candle, so we take [-2].
        2. Falls back to the latest_candle Redis key if the adapter is
           unavailable or the call fails.
        """
        adapter = self._adapters.get(exchange)
        if adapter:
            try:
                ohlcvs = await adapter.fetch_ohlcv(symbol, interval, limit=3)
                if len(ohlcvs) >= 2:
                    _ts, _o, _h, _l, close, _v = ohlcvs[-2]
                    return float(close)
            except Exception as exc:
                logger.warning(
                    "fetch_ohlcv REST [%s %s %s]: %s",
                    exchange, symbol, interval, exc,
                )

        # Fallback — read from Redis (populated by market-data service)
        try:
            raw = await self._redis.get(
                redis_keys.latest_candle_key(exchange, symbol, interval)
            )
            if raw:
                return float(json.loads(raw).get('c', 0))
        except Exception:
            pass
        return 0.0

    # ── Price helpers ─────────────────────────────────────────────────────────

    async def _price(self, exchange: str, symbol: str) -> float:
        """Mark price for futures, LTP for spot."""
        try:
            if 'futures' in exchange or exchange == 'delta':
                raw = await self._redis.get(redis_keys.mark_price_key(exchange, symbol))
                if raw:
                    p = float(json.loads(raw).get('p', 0))
                    if p > 0:
                        return p
            return await self._ltp(exchange, symbol)
        except Exception:
            return 0.0

    async def _ltp(self, exchange: str, symbol: str) -> float:
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('p', 0)) if raw else 0.0
        except Exception:
            return 0.0

    async def _funding_rate(self, exchange: str, symbol: str) -> float:
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('fr', 0) or 0) if raw else 0.0
        except Exception:
            return 0.0

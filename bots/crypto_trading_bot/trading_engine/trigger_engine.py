"""
TriggerEngine — stop/target/pnl_target exits + price and candle-close alerts.

Alert periods:
  current — checked every tick against LTP
  1m / 5m / 15m / 30m / 1h — checked against the CLOSE price of the just-completed candle
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional, TYPE_CHECKING

import redis.asyncio as aioredis

from common.models import Alert
from common import redis_keys, settings
from common.periods import (
    CANDLE_PERIODS, LEGACY_PERIODS, closed_candle_open_ms,
    is_boundary, select_closed_candle,
)

if TYPE_CHECKING:
    from trading_engine.trade_slot   import SlotManager
    from trading_engine.paper_engine import PaperEngine

logger = logging.getLogger('trigger_engine')


class TriggerEngine:
    __slots__ = ('_redis', '_slots', '_paper', '_on_exit', '_on_alert',
                 '_task', '_clock_task', '_adapters', '_on_conditional')

    def __init__(
        self,
        redis_client: aioredis.Redis,
        slot_manager: 'SlotManager',
        on_exit:       Callable[[str, str], Awaitable[None]],
        on_alert:      Callable[[Alert],    Awaitable[None]],
        paper_engine:  'PaperEngine',
        adapters:      dict | None = None,
        on_conditional: Callable | None = None,
    ) -> None:
        self._redis          = redis_client
        self._slots          = slot_manager
        self._paper          = paper_engine
        self._on_exit        = on_exit
        self._on_alert       = on_alert
        self._adapters       = adapters or {}
        self._on_conditional = on_conditional
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
                rate, next_funding_ts = await self._funding_info(slot.exchange, slot.symbol)
                await self._paper.update_mark_prices(
                    slot.exchange, slot.symbol, mark, rate, next_funding_ts
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
            if period != 'current':
                continue   # candle-close periods are handled by _candle_clock_task
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

        At each 1 m boundary  → evaluates all 1 m candle-close alerts / fire-on slots.
        At each 5/15/30/60 m boundary (UTC-epoch aligned) → also evaluates the
        alerts and fire-on slots set for that period (common/periods.py).

        Fetches official OHLC via the exchange REST API (fetch_ohlcv) so the
        close price is the exchange-confirmed final value — not a live tick.
        Falls back to the latest_candle Redis key if the adapter is unavailable.
        """
        while True:
            try:
                # Sleep until the next 1 m boundary + 1 s (the extra second lets
                # the exchange REST endpoint reflect the just-closed candle)
                now      = datetime.now(timezone.utc)
                boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                await asyncio.sleep(max((boundary - now).total_seconds() + 1.0, 0.1))

                # Periods whose candle closed at this boundary: 1m always, the
                # rest by UTC-epoch alignment (see common/periods.py)
                due = [iv for iv in CANDLE_PERIODS if is_boundary(boundary, iv)]
                cache: dict[tuple, float] = {}   # one REST fetch per (exch, symbol, period) per wake

                # Build {(exchange, symbol, interval): [alerts]} mapping
                needed: dict[tuple, list] = {}
                for alert in self._slots.get_alerts():
                    if alert.triggered:
                        continue
                    period = getattr(alert, 'period', 'current')
                    if period in due:
                        k = (alert.exchange, alert.symbol, period)
                        needed.setdefault(k, []).append(alert)

                for (exchange, symbol, interval), alerts in needed.items():
                    close = await self._closed_close(
                        cache, exchange, symbol, interval, boundary
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

                # ── Conditional entry slots: placed from trade ticket with fire_on set ─
                for slot in self._slots.get_all_slots():
                    if slot.status != 'conditional':
                        continue
                    fire_on = getattr(slot, 'fire_on', None)
                    if not fire_on:
                        continue
                    if fire_on not in due:
                        continue   # slot only fires when its own candle closes

                    entry = slot.entries[0] if slot.entries else None
                    if not entry:
                        continue
                    ep         = entry.price or 0.0
                    order_type = entry.order_type or 'limit'

                    close_c = await self._closed_close(
                        cache, slot.exchange, slot.symbol, fire_on, boundary
                    )
                    if close_c <= 0:
                        continue

                    # Trigger logic (based on OHLC close, not live tick):
                    # limit  long : close <= entry  (price pulled back to buy level)
                    # limit  short: close >= entry  (price rallied to sell level)
                    # stop   long : close >= entry  (candle closed above breakout level)
                    # stop   short: close <= entry  (candle closed below breakdown level)
                    if slot.side == 'long':
                        cond_met = (close_c <= ep) if order_type == 'limit' else (close_c >= ep)
                    else:
                        cond_met = (close_c >= ep) if order_type == 'limit' else (close_c <= ep)

                    if cond_met and self._on_conditional:
                        logger.info(
                            "Conditional entry triggered [%s %s %s] close=%.6g ep=%.6g",
                            slot.symbol, slot.side, order_type, close_c, ep,
                        )
                        asyncio.create_task(self._on_conditional(slot))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Candle clock task error: %s", exc)

    async def _closed_close(
        self, cache: dict, exchange: str, symbol: str, interval: str,
        boundary: datetime,
    ) -> float:
        """One fetch per (exchange, symbol, interval) per clock wake — alerts and
        conditional slots waiting on the same candle share the result."""
        key = (exchange, symbol, interval)
        if key not in cache:
            cache[key] = await self._fetch_closed_candle_close(
                exchange, symbol, interval, boundary
            )
        return cache[key]

    async def _fetch_closed_candle_close(
        self, exchange: str, symbol: str, interval: str, boundary: datetime
    ) -> float:
        """
        Returns the close of the candle that closed at `boundary`, or 0.0 if it
        cannot be determined (the caller then skips that check).

        1. Exchange REST (fetch_ohlcv limit=3). The row is picked by OPEN TIME
           == boundary - interval, never by list position, so an empty interval
           (no trades) or a REST response that lags the boundary cannot yield
           the wrong candle. One retry after 2 s.
        2. Legacy 1m/5m only, when REST returned nothing at all (adapter without
           fetch_ohlcv, or a failed call): the latest_candle Redis key.
        """
        adapter = self._adapters.get(exchange)
        if adapter:
            got_rows = False
            for attempt in (0, 1):
                ohlcvs: list = []
                try:
                    ohlcvs = await adapter.fetch_ohlcv(symbol, interval, limit=3) or []
                except Exception as exc:
                    logger.warning(
                        "fetch_ohlcv REST [%s %s %s]: %s",
                        exchange, symbol, interval, exc,
                    )
                if ohlcvs:
                    got_rows = True
                    row = select_closed_candle(ohlcvs, boundary, interval)
                    if row is not None:
                        return float(row[4])
                elif interval in LEGACY_PERIODS:
                    break   # no REST data (unsupported/failed) -> Redis fallback below
                if attempt == 0:
                    await asyncio.sleep(2.0)
            if got_rows:
                logger.warning(
                    "Closed %s candle (open ts %s) not in REST response for %s %s - check skipped",
                    interval, closed_candle_open_ms(boundary, interval), exchange, symbol,
                )
                return 0.0

        # Longer periods have no safe fallback: the Redis candle is the still-forming
        # candle, so using it would evaluate the wrong close. Skip instead.
        if interval not in LEGACY_PERIODS:
            return 0.0

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

    async def _funding_info(
        self, exchange: str, symbol: str
    ) -> tuple[float, Optional[datetime]]:
        """
        (rate, next_funding_time) from the REST-polled funding cache
        (market_data_service._funding_poller). This is the authoritative
        source — next_funding_time drives WHEN funding is applied in
        PaperEngine, replacing the old fixed-8h-UTC-grid assumption.

        If the cache hasn't been populated yet (service just started, or
        poll hasn't landed), still surface a rate for display purposes via
        the ticker's lastFundingRate, but return next_funding_time=None so
        PaperEngine does NOT apply a guessed/unsettled charge.
        """
        try:
            raw = await self._redis.get(redis_keys.funding_info_key(exchange, symbol))
            if raw:
                d       = json.loads(raw)
                rate    = float(d.get('rate', 0) or 0)
                next_ts = d.get('next_ts')
                next_dt = datetime.fromisoformat(next_ts) if next_ts else None
                return rate, next_dt
        except Exception:
            pass
        return await self._funding_rate(exchange, symbol), None

    async def _funding_rate(self, exchange: str, symbol: str) -> float:
        """Live (unsettled) funding rate off the last tick — display fallback only."""
        try:
            raw = await self._redis.get(redis_keys.latest_tick_key(exchange, symbol))
            return float(json.loads(raw).get('fr', 0) or 0) if raw else 0.0
        except Exception:
            return 0.0

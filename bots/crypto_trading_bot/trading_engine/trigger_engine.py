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
                 '_task', '_candle_last')

    def __init__(
        self,
        redis_client: aioredis.Redis,
        slot_manager: 'SlotManager',
        on_exit:      Callable[[str, str], Awaitable[None]],
        on_alert:     Callable[[Alert],    Awaitable[None]],
        paper_engine: 'PaperEngine',
    ) -> None:
        self._redis       = redis_client
        self._slots       = slot_manager
        self._paper       = paper_engine
        self._on_exit     = on_exit
        self._on_alert    = on_alert
        self._task: asyncio.Task | None = None
        # Tracks last seen candle per "exchange:symbol:interval"
        # so we detect when a candle CLOSES (new candle timestamp appears)
        self._candle_last: dict[str, dict] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("TriggerEngine started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

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

        # ── Alerts ────────────────────────────────────────────────────────────
        for alert in self._slots.get_alerts():
            if alert.triggered:
                continue
            period = getattr(alert, 'period', 'current')
            if period in ('1m', '5m'):
                triggered = await self._check_candle_alert(alert, period)
            else:
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

    # ── Candle-close alert ────────────────────────────────────────────────────

    async def _check_candle_alert(self, alert: Alert, interval: str) -> bool:
        """
        Returns True when a new candle has started (= previous candle closed)
        and the CLOSE price of that previous candle crosses the alert level.
        Fires AT MOST ONCE per completed candle.
        """
        try:
            raw = await self._redis.get(
                redis_keys.latest_candle_key(alert.exchange, alert.symbol, interval)
            )
            if not raw:
                return False
            candle    = json.loads(raw)
            cur_ts    = candle.get('ts', '')
            cur_close = float(candle.get('c', 0))

            key = f"{alert.exchange}:{alert.symbol}:{interval}:{alert.id}"
            last = self._candle_last.get(key)

            if last is None:
                # First time — just record, don't fire
                self._candle_last[key] = {'ts': cur_ts, 'close': cur_close}
                return False

            if cur_ts == last['ts']:
                return False   # same candle still in progress

            # New candle started → previous candle is closed
            prev_close = last['close']
            self._candle_last[key] = {'ts': cur_ts, 'close': cur_close}
            return (
                (alert.upper is not None and prev_close >= alert.upper) or
                (alert.lower is not None and prev_close <= alert.lower)
            )
        except Exception:
            return False

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

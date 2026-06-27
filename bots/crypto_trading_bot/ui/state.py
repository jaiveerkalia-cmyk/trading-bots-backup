from __future__ import annotations
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from common import redis_keys, settings
from common.settings import IST

logger = logging.getLogger('ui.state')

_PORTFOLIO_KEY = 'ui:portfolio'


class UIState:
    __slots__ = (
        'slots', 'positions', 'open_orders', 'order_history',
        'alerts', 'pnl', 'live_mode', 'connected_exchanges',
        'log_entries', 'pnl_history', 'last_prices', 'mark_prices',
        'watch_exchange', 'watch_symbol', 'watch_interval',
        'starting_balance', 'pnl_offset',
        '_last_pnl_ts', '_redis', '_prev_pos_count',
        'ui_prefs', '_ui_prefs_loaded',
    )

    def __init__(self, redis_client: aioredis.Redis):
        self._redis               = redis_client
        self.slots:               list[dict] = []
        self.positions:           list[dict] = []
        self.open_orders:         list[dict] = []
        self.order_history:       list[dict] = []
        self.alerts:              list[dict] = []
        self.pnl:                 dict = {'unrealized': 0.0, 'realized': 0.0}
        self.live_mode:           bool = False
        self.connected_exchanges: list[str] = []
        self.log_entries:         list[dict] = []
        self.pnl_history: deque[tuple[str, float]] = deque(
            maxlen=settings.PNL_CHART_POINTS
        )
        self.last_prices:     dict[str, float] = {}
        self.mark_prices:     dict[str, float] = {}
        self.watch_exchange   = settings.SUPPORTED_EXCHANGES[0]
        self.watch_symbol     = 'BTC/USDT'
        self.watch_interval   = '1m'
        self.starting_balance = 10000.0
        self.pnl_offset:      float = 0.0
        self._last_pnl_ts:    datetime | None = None
        self._prev_pos_count: int  = 0
        self.ui_prefs:        dict = {}
        self._ui_prefs_loaded:bool = False

    # ── Portfolio persistence ─────────────────────────────────────────────────

    async def load_portfolio(self) -> None:
        try:
            raw = await self._redis.get(_PORTFOLIO_KEY)
            if raw:
                d = json.loads(raw)
                self.starting_balance = float(d.get('starting_balance', 10000.0))
                self.pnl_offset       = float(d.get('pnl_offset',       0.0))
                logger.info(
                    "Portfolio loaded: balance=%s offset=%s",
                    self.starting_balance, self.pnl_offset,
                )
        except Exception as e:
            logger.error("Portfolio load error: %s", e)

    async def save_portfolio(self) -> None:
        try:
            await self._redis.set(_PORTFOLIO_KEY, json.dumps({
                'starting_balance': self.starting_balance,
                'pnl_offset':       self.pnl_offset,
            }))
        except Exception as e:
            logger.error("Portfolio save error: %s", e)

    # ── UI preferences persistence ────────────────────────────────────────────

    async def load_ui_prefs(self) -> None:
        """Load chart height, interval, alert sound prefs from Redis."""
        try:
            raw = await self._redis.get(redis_keys.UI_PREFS_KEY)
            if raw:
                self.ui_prefs = json.loads(raw)
                # Sync watch_interval from saved pref
                if 'chart_interval' in self.ui_prefs:
                    self.watch_interval = self.ui_prefs['chart_interval']
                logger.info("UI prefs loaded: %s", self.ui_prefs)
        except Exception as e:
            logger.error("UI prefs load error: %s", e)
        self._ui_prefs_loaded = True

    async def save_ui_prefs(self, prefs: dict) -> None:
        """Merge new keys into UI prefs and persist to Redis."""
        self.ui_prefs.update(prefs)
        try:
            await self._redis.set(
                redis_keys.UI_PREFS_KEY,
                json.dumps(self.ui_prefs),
            )
        except Exception as e:
            logger.error("UI prefs save error: %s", e)

    # ── Refresh ───────────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        # Load UI prefs on the very first refresh (avoids changing startup code)
        if not self._ui_prefs_loaded:
            await self.load_ui_prefs()

        try:
            sym_key = f"{self.watch_exchange}:{self.watch_symbol}"
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.get(redis_keys.SLOTS_KEY)
                pipe.get(redis_keys.POSITIONS_KEY)
                pipe.get(redis_keys.OPEN_ORDERS_KEY)
                pipe.get(redis_keys.ORDER_HISTORY_KEY)
                pipe.get(redis_keys.ALERTS_KEY)
                pipe.get(redis_keys.PNL_KEY)
                pipe.get(redis_keys.LIVE_MODE_KEY)
                pipe.get(redis_keys.CONNECTED_EXCHANGES_KEY)
                pipe.lrange(redis_keys.LOG_KEY, 0, settings.LOG_MAX_ENTRIES - 1)
                pipe.get(redis_keys.latest_tick_key(
                    self.watch_exchange, self.watch_symbol
                ))
                pipe.get(redis_keys.mark_price_key(
                    self.watch_exchange, self.watch_symbol
                ))
                results = await pipe.execute()

            self.slots               = json.loads(results[0]) if results[0] else []
            self.positions           = json.loads(results[1]) if results[1] else []
            self.open_orders         = json.loads(results[2]) if results[2] else []
            self.order_history       = json.loads(results[3]) if results[3] else []
            self.alerts              = json.loads(results[4]) if results[4] else []
            self.pnl                 = json.loads(results[5]) if results[5] else {'unrealized': 0.0, 'realized': 0.0}
            self.live_mode           = results[6] == '1' if results[6] else False
            self.connected_exchanges = json.loads(results[7]) if results[7] else []
            self.log_entries         = [json.loads(e) for e in (results[8] or [])]

            if results[9]:
                tick = json.loads(results[9])
                self.last_prices[sym_key] = float(tick.get('p', 0))
                if 'mp' in tick:
                    self.mark_prices[sym_key] = float(tick['mp'])

            if results[10]:
                self.mark_prices[sym_key] = float(
                    json.loads(results[10]).get('p', 0)
                )

            if sym_key not in self.mark_prices:
                self.mark_prices[sym_key] = self.last_prices.get(sym_key, 0.0)

            # PnL history — only while positions are open; reset on new position
            now = datetime.now(IST)
            new_pos_count = len(self.positions)
            if new_pos_count > 0 and self._prev_pos_count == 0:
                self.pnl_history.clear()
                self._last_pnl_ts = None
            if new_pos_count > 0:
                if (self._last_pnl_ts is None or
                        (now - self._last_pnl_ts).total_seconds() >= settings.PNL_CHART_INTERVAL):
                    u = sum(float(p.get('unrealized_pnl', 0)) for p in self.positions)
                    self.pnl_history.append((now.strftime('%H:%M'), round(u, 4)))
                    self._last_pnl_ts = now
            self._prev_pos_count = new_pos_count

        except Exception as e:
            logger.error("UIState refresh error: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def get_last_price(self) -> float:
        return self.last_prices.get(
            f"{self.watch_exchange}:{self.watch_symbol}", 0.0
        )

    def get_mark_price(self) -> float:
        return self.mark_prices.get(
            f"{self.watch_exchange}:{self.watch_symbol}",
            self.get_last_price(),
        )

    def is_futures(self) -> bool:
        return 'futures' in self.watch_exchange or self.watch_exchange == 'delta'

    def get_display_pnl(self) -> tuple[float, float]:
        u = float(self.pnl.get('unrealized', 0))
        r = float(self.pnl.get('realized',   0)) - self.pnl_offset
        return u, r

    def get_current_balance(self) -> float:
        u, r = self.get_display_pnl()
        return self.starting_balance + r + u

    def reset_portfolio(self) -> None:
        self.pnl_offset   = float(self.pnl.get('realized', 0))
        self._last_pnl_ts = None
        self.pnl_history.clear()

    def get_slot(self, slot_id: str) -> Optional[dict]:
        for s in self.slots:
            if s.get('id') == slot_id:
                return s
        return None

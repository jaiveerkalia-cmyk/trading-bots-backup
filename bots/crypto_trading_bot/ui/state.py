from __future__ import annotations
import json
import logging
from collections import deque
from datetime import datetime, timezone

import redis.asyncio as aioredis

from common import redis_keys, settings
from common.settings import IST

logger = logging.getLogger('ui.state')


class UIState:
    __slots__ = (
        'slots', 'positions', 'open_orders', 'order_history',
        'alerts', 'pnl', 'live_mode', 'connected_exchanges',
        'log_entries', 'pnl_history', 'last_prices',
        'watch_exchange', 'watch_symbol', 'watch_interval',
        'starting_balance', '_last_pnl_ts', '_redis',
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
        self.watch_exchange   = 'binance'
        self.watch_symbol     = 'BTC/USDT'
        self.watch_interval   = '1m'
        self.starting_balance = 10000.0
        self._last_pnl_ts: datetime | None = None

    async def refresh(self) -> None:
        try:
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

            # Last tick price for watched symbol
            if results[9]:
                tick = json.loads(results[9])
                key  = f"{self.watch_exchange}:{self.watch_symbol}"
                self.last_prices[key] = float(tick.get('p', 0))

            # PnL history — sample every PNL_CHART_INTERVAL seconds
            now = datetime.now(IST)
            if (self._last_pnl_ts is None or
                    (now - self._last_pnl_ts).total_seconds() >= settings.PNL_CHART_INTERVAL):
                ts  = now.strftime('%H:%M')
                pnl = (float(self.pnl.get('unrealized', 0)) +
                       float(self.pnl.get('realized', 0)))
                self.pnl_history.append((ts, pnl))
                self._last_pnl_ts = now

        except Exception as e:
            logger.error(f"UIState refresh error: {e}")

    def get_last_price(self) -> float:
        return self.last_prices.get(
            f"{self.watch_exchange}:{self.watch_symbol}", 0.0
        )

    def get_current_balance(self) -> float:
        return (self.starting_balance +
                float(self.pnl.get('realized', 0)) +
                float(self.pnl.get('unrealized', 0)))

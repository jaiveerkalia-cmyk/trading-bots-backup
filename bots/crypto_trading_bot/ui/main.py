from __future__ import annotations
import logging
import os

import redis.asyncio as aioredis
from nicegui import app, ui

from ui.state import UIState
from ui.components import top_bar, trade_ticket, alerts_panel
from ui.components import positions_table, orders_table, history_table
from ui.components import pnl_chart, tv_widget, activity_log
from common import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

_redis: aioredis.Redis | None = None
_state: UIState | None        = None


@app.on_startup
async def startup() -> None:
    global _redis, _state
    _redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True, max_connections=5,
    )
    _state = UIState(_redis)


@app.on_shutdown
async def shutdown() -> None:
    if _redis:
        await _redis.aclose()


@ui.page('/')
async def index() -> None:
    ui.add_head_html(
        '<script type="text/javascript" '
        'src="https://s3.tradingview.com/tv.js"></script>'
    )
    ui.add_head_html("""
        <style>
          body { background: #111827; margin: 0; }
          .q-table__container { background: transparent !important; }
          .q-table thead tr th { background:#1f2937;color:#9ca3af;font-size:11px; }
          .q-table tbody tr td { font-size:11px;color:#d1d5db; }
          .q-table tbody tr:hover td { background:#1f2937 !important; }
        </style>
    """)

    ui.dark_mode(True)

    shared: dict = {
        'exchange': settings.SUPPORTED_EXCHANGES[0],
        'symbol':   'BTC/USDT',
        'risk_pct': settings.DEFAULT_RISK_PCT,
        'balance':  10000.0,
    }

    _state.watch_exchange   = shared['exchange']
    _state.watch_symbol     = shared['symbol']
    _state.starting_balance = shared['balance']

    # ── Top bar ───────────────────────────────────────────────────────────────
    tb = top_bar.build(_state, _redis, shared)

    with ui.element('div').classes('w-full px-3 py-2'):

        # ── Row 1: TradingView chart — full width ─────────────────────────────
        tv = tv_widget.build(_state, shared)

        # ── Row 2: PnL curve — full width, short ──────────────────────────────
        pnl = pnl_chart.build(_state)

        # ── Row 3: Short | Alerts+CloseAll | Long ─────────────────────────────
        with ui.row().classes('w-full gap-3 mt-3 mb-3 items-start flex-nowrap'):
            with ui.element('div').classes('flex-1 min-w-0'):
                short_updater = trade_ticket.build('short', _state, _redis, shared)
            with ui.element('div').style('width:300px; flex-shrink:0;'):
                ap = alerts_panel.build(_state, _redis, shared)
            with ui.element('div').classes('flex-1 min-w-0'):
                long_updater = trade_ticket.build('long', _state, _redis, shared)

        # ── Row 4: Activity log ───────────────────────────────────────────────
        al = activity_log.build(_state)

        # ── Rows 5-7: Tables ──────────────────────────────────────────────────
        with ui.element('div').classes('w-full mt-2 flex flex-col gap-3'):
            pos = positions_table.build(_state, _redis)
            oo  = orders_table.build(_state, _redis)
            oh  = history_table.build(_state)

    # ── Timer ─────────────────────────────────────────────────────────────────
    updaters = [
        tb['update'],
        ap['update'],
        tv['update'],
        pnl['update'],
        al['update'],
        pos['update'],
        oo['update'],
        oh['update'],
        short_updater['update'],
        long_updater['update'],
    ]

    async def refresh() -> None:
        await _state.refresh()
        for fn in updaters:
            try:
                fn()
            except Exception as e:
                logging.getLogger('ui').debug(f"Updater error: {e}")

    ui.timer(settings.UI_REFRESH_INTERVAL, refresh)


if __name__ in ('__main__', '__mp_main__'):
    ui.run(
        host=settings.UI_HOST,
        port=settings.UI_PORT,
        title='Crypto Trading Engine',
        favicon='⚡',
        dark=True,
        reload=False,
        show=False,
        storage_secret=os.getenv('MASTER_KEY', 'crypto-bot-secret'),
    )

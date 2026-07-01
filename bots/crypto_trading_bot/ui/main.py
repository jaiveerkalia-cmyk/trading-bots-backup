from __future__ import annotations
import json
import logging
import os
import time
import uuid as _uuid

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from nicegui import app, ui

from ui.state import UIState
from ui.components import (
    top_bar, trade_ticket, alerts_panel,
    positions_table, orders_table, history_table,
    pnl_chart, tv_widget, activity_log,
)
from common import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

_redis: aioredis.Redis | None = None
_state: UIState | None        = None

_TV_STORAGE_PREFIX = 'tv:storage'
_CORS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age':       '86400',
}

_TV_LAYOUT_PREFIX = 'tv:layout'
_TV_LAYOUT_DIR     = settings.STATE_DIR / 'tv_layouts'
_TV_LAYOUT_DIR.mkdir(parents=True, exist_ok=True)

def _layout_file(exchange: str, symbol: str):
    safe = f"{exchange}__{symbol.replace('/', '_')}.json"
    return _TV_LAYOUT_DIR / safe


# ── App lifecycle ─────────────────────────────────────────────────────────────

@app.on_startup
async def startup() -> None:
    global _redis, _state
    _redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True, max_connections=5,
    )
    _state = UIState(_redis)
    await _state.load_portfolio()
    await _state.load_ui_prefs()   # ← load eagerly so index() can use saved symbol


@app.on_shutdown
async def shutdown() -> None:
    if _redis:
        await _redis.aclose()


# ── TradingView chart storage API ─────────────────────────────────────────────
# Allows the TradingView widget to save/load drawings, indicators, and layouts.
# Requests come from TradingView's iframe (origin: tradingview.com).

@app.options('/tv_storage/{rest:path}')
async def tv_storage_options(rest: str = '') -> Response:
    return Response(status_code=204, headers=_CORS)


@app.get('/tv_storage/{version}/{resource}')
async def tv_storage_get(
    version: str, resource: str,
    client: str = '', user: str = '',
    chart: str = '', template: str = '',
) -> JSONResponse:
    item_id    = chart or template
    key_prefix = f"{_TV_STORAGE_PREFIX}:{resource}:{client}:{user}"

    if item_id:
        raw  = await _redis.get(f"{key_prefix}:{item_id}")
        data = json.loads(raw) if raw else {}
        return JSONResponse({'status': 'ok', 'data': data}, headers=_CORS)

    # List all saved items for this client/user/resource
    all_keys = await _redis.keys(f"{key_prefix}:*")
    items    = []
    for k in all_keys:
        raw = await _redis.get(k)
        if raw:
            items.append(json.loads(raw))
    return JSONResponse({'status': 'ok', 'data': items}, headers=_CORS)


@app.post('/tv_storage/{version}/{resource}')
async def tv_storage_post(
    request: Request,
    version: str, resource: str,
    client: str = '', user: str = '',
    chart: str = '', template: str = '',
) -> JSONResponse:
    body    = await request.json()
    item_id = chart or template or str(_uuid.uuid4())
    key     = f"{_TV_STORAGE_PREFIX}:{resource}:{client}:{user}:{item_id}"
    await _redis.set(key, json.dumps({
        'id':        item_id,
        'name':      body.get('name', 'Chart'),
        'timestamp': int(time.time()),
        'content':   body.get('content', ''),
        'symbol':    body.get('symbol', ''),
    }))
    return JSONResponse({'status': 'ok', 'id': item_id}, headers=_CORS)


@app.delete('/tv_storage/{version}/{resource}')
async def tv_storage_delete(
    version: str, resource: str,
    client: str = '', user: str = '',
    chart: str = '', template: str = '',
) -> JSONResponse:
    item_id = chart or template
    if item_id:
        key = f"{_TV_STORAGE_PREFIX}:{resource}:{client}:{user}:{item_id}"
        await _redis.delete(key)
    return JSONResponse({'status': 'ok'}, headers=_CORS)


@app.get('/tv_layout')
async def tv_layout_get(exchange: str = '', symbol: str = '') -> JSONResponse:
    """Returns the saved chart layout (drawings/indicators/settings) for exchange+symbol."""
    if not exchange or not symbol:
        return JSONResponse({'content': None}, status_code=400)
        
    key = f"{_TV_LAYOUT_PREFIX}:{exchange}:{symbol}"
    raw = await _redis.get(key)
    
    if not raw:
        path = _layout_file(exchange, symbol)
        if path.exists():
            try:
                raw = path.read_text(encoding='utf-8')
                if raw:
                    await _redis.set(key, raw)
            except Exception as e:
                logging.getLogger('ui').error("TV layout file restore: %s", e)
                
    if not raw:
        return JSONResponse({'content': None})
        
    return JSONResponse({'content': json.loads(raw)})


@app.post('/tv_layout')
async def tv_layout_post(request: Request, exchange: str = '', symbol: str = '') -> JSONResponse:
    """Saves the chart layout state (full widget.save() output) for exchange+symbol."""
    if not exchange or not symbol:
        return JSONResponse({'status': 'error'}, status_code=400)
        
    body = await request.json()
    key  = f"{_TV_LAYOUT_PREFIX}:{exchange}:{symbol}"
    data = json.dumps(body)
    
    try:
        await _redis.set(key, data)
    except Exception as e:
        logging.getLogger('ui').error("TV layout Redis save: %s", e)
        
    try:
        _layout_file(exchange, symbol).write_text(data, encoding='utf-8')
    except Exception as e:
        logging.getLogger('ui').error("TV layout file save: %s", e)
        
    return JSONResponse({'status': 'ok'})


# ── Market data helper ────────────────────────────────────────────────────────

async def _subscribe_md(exchange: str, symbol: str) -> None:
    """Tell the market data service to start streaming ticks for exchange+symbol."""
    if _redis:
        try:
            await _redis.publish('market_data:control', json.dumps({
                'cmd': 'subscribe', 'exchange': exchange,
                'symbol': symbol, 'streams': ['ticker'],
            }))
        except Exception as e:
            logging.getLogger('ui').warning("MD subscribe error: %s", e)


# ── Page ──────────────────────────────────────────────────────────────────────

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

    # ── Restore saved exchange/symbol from ui_prefs ───────────────────────────
    saved_exchange = _state.ui_prefs.get('watch_exchange', settings.SUPPORTED_EXCHANGES[0])
    saved_symbol   = _state.ui_prefs.get('watch_symbol',   'BTC/USDT')

    _state.watch_exchange = saved_exchange
    _state.watch_symbol   = saved_symbol

    shared: dict = {
        'exchange': saved_exchange,
        'symbol':   saved_symbol,
        'risk_pct': settings.DEFAULT_RISK_PCT,
        'balance':  _state.starting_balance,
    }

    # Ensure ticks flow immediately for the current watch symbol
    await _subscribe_md(saved_exchange, saved_symbol)

    tb   = top_bar.build(_state, _redis, shared)

    with ui.element('div').classes('w-full px-3 py-2'):
        tv   = tv_widget.build(_state, shared)
        pnl  = pnl_chart.build(_state)

        with ui.row().classes('w-full gap-3 mt-3 mb-3 items-start flex-nowrap'):
            with ui.element('div').classes('flex-1 min-w-0'):
                short_upd = trade_ticket.build('short', _state, _redis, shared)
            with ui.element('div').style('width:300px; flex-shrink:0;'):
                ap = alerts_panel.build(_state, _redis, shared)
            with ui.element('div').classes('flex-1 min-w-0'):
                long_upd = trade_ticket.build('long', _state, _redis, shared)

        al = activity_log.build(_state)

        with ui.element('div').classes('w-full mt-2 flex flex-col gap-3'):
            pos = positions_table.build(_state, _redis)
            oo  = orders_table.build(_state, _redis, shared)
            oh  = history_table.build(_state)

    updaters = [
        tb['update'], ap['update'], tv['update'], pnl['update'],
        al['update'], pos['update'], oo['update'], oh['update'],
        short_upd['update'], long_upd['update'],
    ]

    async def refresh() -> None:
        await _state.refresh()
        for fn in updaters:
            try:
                fn()
            except Exception as e:
                logging.getLogger('ui').debug("Updater error: %s", e)

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

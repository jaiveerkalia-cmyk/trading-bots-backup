from __future__ import annotations
import json
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

_TV_SYMBOL_MAP: dict[str, str] = {
    'binance:BTC/USDT':  'BINANCE:BTCUSDT',
    'binance:ETH/USDT':  'BINANCE:ETHUSDT',
    'binance:SOL/USDT':  'BINANCE:SOLUSDT',
    'binance:BNB/USDT':  'BINANCE:BNBUSDT',
    'binance:XRP/USDT':  'BINANCE:XRPUSDT',
    'binance:DOGE/USDT': 'BINANCE:DOGEUSDT',
    'binance:ADA/USDT':  'BINANCE:ADAUSDT',
    'delta:BTC/USD':     'BITMEX:XBTUSD',
    'delta:ETH/USD':     'BITMEX:ETHUSD',
    'delta:BTC/USDT':    'BINANCE:BTCUSDT',
    'delta:ETH/USDT':    'BINANCE:ETHUSDT',
}

_INTERVAL_MAP = {
    '1m': '1', '5m': '5', '15m': '15',
    '1h': '60', '4h': '240', '1d': 'D',
}

_CONTAINER_ID = 'tv_widget_container'


def _tv_symbol(exchange: str, symbol: str) -> str:
    key = f"{exchange.lower()}:{symbol}"
    return _TV_SYMBOL_MAP.get(key, f"BINANCE:{symbol.replace('/', '')}")


def _init_js(tv_symbol: str, interval: str) -> str:
    iv  = _INTERVAL_MAP.get(interval, '1')
    cfg = json.dumps({
        'autosize':          True,
        'symbol':            tv_symbol,
        'interval':          iv,
        'timezone':          'Asia/Kolkata',
        'theme':             'dark',
        'style':             '1',
        'locale':            'en',
        'toolbar_bg':        '#1f2937',
        'backgroundColor':   '#111827',
        'enable_publishing': False,
        'save_image':        True,
        'hide_top_toolbar':  False,
        'container_id':      _CONTAINER_ID,
    })
    # Self-retrying init — waits until tv.js has loaded before creating widget
    return f"""
(function tryInit() {{
  if (typeof TradingView === 'undefined') {{
    setTimeout(tryInit, 400);
    return;
  }}
  var el = document.getElementById('{_CONTAINER_ID}');
  if (!el) {{ setTimeout(tryInit, 400); return; }}
  el.innerHTML = '';
  window._tvWidget = new TradingView.widget({cfg});
}})();
"""


def build(state: 'UIState', shared: dict) -> dict:
    last: dict = {'symbol': None, 'interval': None}

    with ui.card().classes(
        'w-full bg-gray-900 p-0 rounded-lg overflow-hidden'
    ):
        with ui.row().classes(
            'w-full items-center px-3 py-2 bg-gray-800 gap-3'
        ):
            ui.label('TradingView Chart').classes(
                'text-gray-300 text-sm font-medium'
            )
            ui.label('(reference — TradingView data)').classes(
                'text-gray-600 text-xs'
            )
            ui.select(
                options=['1m', '5m', '15m', '1h', '4h', '1d'],
                value='1m',
                label='Interval',
                on_change=lambda e: (
                    setattr(state, 'watch_interval', e.value),
                    last.update({'interval': None}),
                ),
            ).props('dense dark outlined').classes('w-20 ml-auto')

        with ui.element('div').style(
            'width:100%; height:420px; position:relative;'
        ):
            el = ui.element('div').style('width:100%; height:100%;')
            el._props['id'] = _CONTAINER_ID

    def update() -> None:
        tv_sym = _tv_symbol(
            shared.get('exchange', 'binance'),
            shared.get('symbol',   'BTC/USDT'),
        )
        iv = state.watch_interval

        if tv_sym == last['symbol'] and iv == last['interval']:
            return

        last['symbol']   = tv_sym
        last['interval'] = iv
        ui.run_javascript(_init_js(tv_sym, iv))

    return {'update': update}

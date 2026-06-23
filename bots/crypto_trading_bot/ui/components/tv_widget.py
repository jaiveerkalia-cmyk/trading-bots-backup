"""
TradingView embeddable widget — reference chart using TradingView's data feed.
Sits alongside Lightweight Charts (which shows your live exchange data).
Symbol updates dynamically when top bar exchange/symbol changes.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

# Map canonical BASE/QUOTE + exchange -> TradingView symbol string
# Add more as needed
_TV_SYMBOL_MAP: dict[str, str] = {
    'binance:BTC/USDT':  'BINANCE:BTCUSDT',
    'binance:ETH/USDT':  'BINANCE:ETHUSDT',
    'binance:SOL/USDT':  'BINANCE:SOLUSDT',
    'binance:BNB/USDT':  'BINANCE:BNBUSDT',
    'binance:XRP/USDT':  'BINANCE:XRPUSDT',
    'binance:BTC/BUSD':  'BINANCE:BTCBUSD',
    'delta:BTC/USD':     'BITMEX:XBTUSD',
    'delta:ETH/USD':     'BITMEX:ETHUSD',
    'delta:BTC/USDT':    'BINANCE:BTCUSDT',
    'delta:ETH/USDT':    'BINANCE:ETHUSDT',
}

_DEFAULT_TV_SYMBOL = 'BINANCE:BTCUSDT'

# TradingView interval mapping
_INTERVAL_MAP = {
    '1m':  '1',
    '5m':  '5',
    '15m': '15',
    '1h':  '60',
    '4h':  '240',
    '1d':  'D',
}

_CONTAINER_ID = 'tv_widget_container'


def _tv_symbol(exchange: str, symbol: str) -> str:
    key = f"{exchange.lower()}:{symbol}"
    if key in _TV_SYMBOL_MAP:
        return _TV_SYMBOL_MAP[key]
    # Fallback: try to build Binance symbol
    native = symbol.replace('/', '')
    return f"BINANCE:{native}"


def _init_js(tv_symbol: str, interval: str) -> str:
    iv = _INTERVAL_MAP.get(interval, '1')
    cfg = json.dumps({
        'autosize':          True,
        'symbol':            tv_symbol,
        'interval':          iv,
        'timezone':          'Asia/Kolkata',
        'theme':             'dark',
        'style':             '1',
        'locale':            'en',
        'toolbar_bg':        '#111827',
        'backgroundColor':   '#111827',
        'gridColor':         '#1f2937',
        'enable_publishing': False,
        'hide_top_toolbar':  False,
        'hide_legend':       False,
        'save_image':        True,
        'container_id':      _CONTAINER_ID,
    })
    return f"""
    (function() {{
      var el = document.getElementById('{_CONTAINER_ID}');
      if (!el) return;
      el.innerHTML = '';
      if (typeof TradingView !== 'undefined') {{
        new TradingView.widget({cfg});
      }}
    }})();
    """


def build(state: 'UIState', shared: dict) -> dict:
    last: dict = {'symbol': None, 'interval': None}

    with ui.card().classes('w-full bg-gray-900 p-0 rounded-lg overflow-hidden'):
        with ui.row().classes('w-full items-center px-3 py-2 bg-gray-800 gap-2'):
            ui.label('TradingView').classes('text-gray-300 text-sm font-medium')
            ui.label('(reference chart — TradingView data)').classes('text-gray-600 text-xs')

        # Widget container — TradingView renders inside this div
        with ui.element('div').style('width:100%; height:400px; position:relative;'):
            el = ui.element('div').style('width:100%; height:100%;')
            el._props['id'] = _CONTAINER_ID

    def update() -> None:
        tv_sym  = _tv_symbol(shared.get('exchange', 'binance'), shared.get('symbol', 'BTC/USDT'))
        iv      = state.watch_interval

        # Only re-render if symbol or interval changed
        if tv_sym == last['symbol'] and iv == last['interval']:
            return

        last['symbol']   = tv_sym
        last['interval'] = iv
        ui.run_javascript(_init_js(tv_sym, iv))

    return {'update': update}

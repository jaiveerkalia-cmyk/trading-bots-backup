"""
TradingView chart widget.
Height and interval are persisted to Redis via state.ui_prefs so they survive
container restarts and browser refreshes.
"""
from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

_CONTAINER_ID = 'tv_chart_container'

_INTERVAL_MAP: dict[str, str] = {
    '1m': '1', '3m': '3', '5m': '5', '15m': '15', '30m': '30',
    '1h': '60', '2h': '120', '4h': '240', '6h': '360', '12h': '720',
    '1d': '1D', '1w': '1W',
}


def _tv_symbol(exchange: str, symbol: str) -> str:
    sym = symbol.replace('/', '').replace('-', '').upper()
    if 'futures' in exchange or exchange == 'delta':
        return f'BINANCE:{sym}.P'
    if exchange in ('binance', 'binance_spot'):
        return f'BINANCE:{sym}'
    return sym


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
        'gridColor':         'rgba(55,65,81,0.5)',
        'enable_publishing': False,
        'save_image':        True,
        'hide_top_toolbar':  False,
        'hide_side_toolbar': False,
        'withdateranges':    True,
        'container_id':      _CONTAINER_ID,
    })
    return f"""
(function tryInit() {{
    if (typeof TradingView === 'undefined') {{ setTimeout(tryInit, 400); return; }}
    var el = document.getElementById('{_CONTAINER_ID}');
    if (!el) {{ setTimeout(tryInit, 400); return; }}
    if (window._tvWidget && window._tvWidgetReady) {{
        try {{
            window._tvWidget.setSymbol('{tv_symbol}', '{iv}', function() {{}});
            return;
        }} catch (e) {{
            console.warn('[TV] setSymbol failed, reinitialising:', e);
        }}
    }}
    el.innerHTML = '';
    window._tvWidgetReady = false;
    window._tvWidget = new TradingView.widget({cfg});
    if (window._tvWidget && window._tvWidget.onChartReady) {{
        window._tvWidget.onChartReady(function() {{
            window._tvWidgetReady = true;
        }});
    }}
}})();
"""


def _set_resolution_js(iv_tv: str) -> str:
    return f"""
(function() {{
    if (!window._tvWidget || !window._tvWidgetReady) return;
    try {{ window._tvWidget.chart().setResolution('{iv_tv}', function() {{}}); }}
    catch (e) {{ console.warn('[TV] setResolution:', e); }}
}})();
"""


def build(state: 'UIState', shared: dict) -> dict:
    # Load persisted prefs (available after first state.refresh())
    saved_height   = state.ui_prefs.get('chart_height',   440)
    saved_interval = state.ui_prefs.get('chart_interval', '15m')

    # Sync state interval with saved pref
    if saved_interval and saved_interval != state.watch_interval:
        state.watch_interval = saved_interval

    last = {
        'symbol':      None,
        'interval':    None,
        'initialized': False,
        'height':      saved_height,
    }

    with ui.card().classes('w-full bg-gray-900 p-0 rounded-xl overflow-hidden shadow-lg'):
        with ui.row().classes('w-full items-center px-3 py-1.5 bg-gray-800/80 gap-3 flex-wrap'):
            ui.label('Chart').classes(
                'text-gray-300 text-xs font-semibold uppercase tracking-wider'
            )

            iv_select = (
                ui.select(
                    options=list(_INTERVAL_MAP.keys()),
                    value=saved_interval,
                    label=None,
                )
                .props('dense dark outlined borderless')
                .classes('w-16 text-xs')
            )

            ui.element('div').classes('flex-1')

            ui.label('H').classes('text-gray-500 text-xs')
            h_lbl = ui.label(f'{saved_height}px').classes('text-gray-500 text-xs w-12')

        chart_wrap = ui.element('div').style(
            f'width:100%; height:{saved_height}px;'
        )
        with chart_wrap:
            ui.element('div').style('width:100%; height:100%;') \
                ._props.__setitem__('id', _CONTAINER_ID)

        levels_bar = ui.row().classes(
            'w-full px-3 py-1.5 bg-gray-800/60 border-t border-gray-700/60 '
            'gap-5 items-center flex-wrap'
        )
        with levels_bar:
            entry_lbl = ui.label('').classes('text-xs font-mono')
            stop_lbl  = ui.label('').classes('text-xs font-mono')
            tgt_lbl   = ui.label('').classes('text-xs font-mono')
        levels_bar.set_visibility(False)

    # ── Interval handler ──────────────────────────────────────────────────────
    def _on_interval(e) -> None:
        iv = e.value
        state.watch_interval = iv
        if last['interval'] == iv:
            return
        last['interval'] = iv
        ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))
        asyncio.ensure_future(state.save_ui_prefs({'chart_interval': iv}))

    iv_select.on('update:model-value', _on_interval)

    # ── Height handler ────────────────────────────────────────────────────────
    def _on_height(h: int) -> None:
        last['height'] = h
        h_lbl.set_text(f'{h}px')
        chart_wrap.style(f'width:100%; height:{h}px;')
        ui.run_javascript(
            'try{if(window._tvWidget&&window._tvWidget.resize)'
            'window._tvWidget.resize();}catch(e){}'
        )
        asyncio.ensure_future(state.save_ui_prefs({'chart_height': h}))

    ui.slider(
        min=280, max=820, step=40, value=saved_height,
        on_change=lambda e: _on_height(int(e.value)),
    ).classes('w-28').props('dense color=grey-7')

    # ── Tick update ───────────────────────────────────────────────────────────
    def update() -> None:
        # Sync saved height/interval on first tick if prefs loaded after build
        if not last['initialized']:
            new_h = state.ui_prefs.get('chart_height', 440)
            if new_h != last['height']:
                _on_height(new_h)

        tv_sym = _tv_symbol(
            shared.get('exchange', 'binance'),
            shared.get('symbol', 'BTC/USDT'),
        )
        iv = getattr(state, 'watch_interval', '15m')

        if not last['initialized'] or tv_sym != last['symbol']:
            last.update({'symbol': tv_sym, 'interval': iv, 'initialized': True})
            ui.run_javascript(_init_js(tv_sym, iv))
        elif iv != last['interval']:
            last['interval'] = iv
            ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))

        # Position levels bar
        pos = None
        for p in state.positions:
            if (p.get('exchange') == shared.get('exchange') and
                    p.get('symbol') == shared.get('symbol')):
                pos = p
                break

        if pos:
            sid   = pos.get('slot_id', '')
            slot  = state.get_slot(sid)
            entry = float(pos.get('entry_price', 0) or 0)
            side  = pos.get('side', '')
            stop  = float((slot or {}).get('stop_price')   or 0)
            tgt   = float((slot or {}).get('target_price') or 0)
            sc    = 'text-green-400' if side == 'long' else 'text-red-400'
            entry_lbl.set_text(f'● Entry  {entry:g}' if entry else '● Entry  —')
            entry_lbl.classes(
                remove='text-green-400 text-red-400 text-gray-500', add=sc
            )
            stop_lbl.set_text(f'▼ Stop  {stop:g}' if stop else '▼ Stop  —')
            stop_lbl.classes(
                remove='text-orange-400 text-gray-600',
                add='text-orange-400' if stop else 'text-gray-600',
            )
            tgt_lbl.set_text(f'▲ Target  {tgt:g}' if tgt else '▲ Target  —')
            tgt_lbl.classes(
                remove='text-blue-400 text-gray-600',
                add='text-blue-400' if tgt else 'text-gray-600',
            )
            levels_bar.set_visibility(True)
        else:
            levels_bar.set_visibility(False)

    return {'update': update}

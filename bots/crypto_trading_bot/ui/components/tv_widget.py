"""
TradingView chart widget.

Key behaviours
──────────────
- Widget is created ONCE per session; symbol changes use setSymbol() JS API
  so drawings and layout customisations survive exchange / symbol switches.
- Interval changes from our dropdown call chart().setResolution() — no reinit.
- A height slider lets the user resize the chart inline.
- A position-level bar below the chart shows Entry / Stop / Target for the
  symbol currently being watched (updated every tick, zero DOM cost).
- Drawing toolbar is always visible (hide_side_toolbar: false).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

_CONTAINER_ID = 'tv_chart_container'

_INTERVAL_MAP: dict[str, str] = {
    '1m':  '1',
    '3m':  '3',
    '5m':  '5',
    '15m': '15',
    '30m': '30',
    '1h':  '60',
    '2h':  '120',
    '4h':  '240',
    '6h':  '360',
    '12h': '720',
    '1d':  '1D',
    '1w':  '1W',
}


# ── Symbol formatter ──────────────────────────────────────────────────────────

def _tv_symbol(exchange: str, symbol: str) -> str:
    sym = symbol.replace('/', '').replace('-', '').upper()
    if 'futures' in exchange or exchange == 'delta':
        return f'BINANCE:{sym}.P'   # perpetual futures
    if exchange in ('binance', 'binance_spot'):
        return f'BINANCE:{sym}'
    return sym


# ── Widget init JS ────────────────────────────────────────────────────────────

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
        'hide_side_toolbar': False,   # keep drawing tools visible
        'withdateranges':    True,
        'container_id':      _CONTAINER_ID,
    })
    # Try a soft symbol-update first (preserves drawings).
    # Fall back to full re-init only when necessary.
    return f"""
(function tryInit() {{
    if (typeof TradingView === 'undefined') {{ setTimeout(tryInit, 400); return; }}
    var el = document.getElementById('{_CONTAINER_ID}');
    if (!el) {{ setTimeout(tryInit, 400); return; }}

    // Soft update — avoids destroying the widget and losing drawings
    if (window._tvWidget && window._tvWidgetReady) {{
        try {{
            window._tvWidget.setSymbol('{tv_symbol}', '{iv}', function() {{}});
            return;
        }} catch (e) {{
            console.warn('[TV] setSymbol failed, reinitialising:', e);
        }}
    }}

    // Full init
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
    try {{
        window._tvWidget.chart().setResolution('{iv_tv}', function() {{}});
    }} catch (e) {{
        console.warn('[TV] setResolution not available:', e);
    }}
}})();
"""


# ── Component builder ─────────────────────────────────────────────────────────

def build(state: 'UIState', shared: dict) -> dict:
    last = {
        'symbol':      None,
        'interval':    None,
        'initialized': False,
        'height':      440,
    }

    # ── Outer card ────────────────────────────────────────────────────────────
    with ui.card().classes(
        'w-full bg-gray-900 p-0 rounded-xl overflow-hidden shadow-lg'
    ):
        # ── Header bar ────────────────────────────────────────────────────────
        with ui.row().classes(
            'w-full items-center px-3 py-1.5 bg-gray-800/80 gap-3 flex-wrap'
        ):
            ui.label('Chart').classes(
                'text-gray-300 text-xs font-semibold uppercase tracking-wider'
            )

            # Interval selector
            iv_select = (
                ui.select(
                    options=list(_INTERVAL_MAP.keys()),
                    value=getattr(state, 'watch_interval', '15m'),
                    label=None,
                )
                .props('dense dark outlined borderless')
                .classes('w-16 text-xs')
            )

            ui.element('div').classes('flex-1')

            # Height slider
            ui.label('H').classes('text-gray-500 text-xs')
            h_slider = (
                ui.slider(min=280, max=820, step=40, value=last['height'])
                .classes('w-28')
                .props('dense color=grey-7')
            )
            h_lbl = ui.label(f"{last['height']}px").classes(
                'text-gray-500 text-xs w-12'
            )

        # ── Chart container ───────────────────────────────────────────────────
        chart_wrap = ui.element('div').style(
            f'width:100%; height:{last["height"]}px;'
        )
        with chart_wrap:
            ui.element('div').style('width:100%; height:100%;') \
                ._props.__setitem__('id', _CONTAINER_ID)

        # ── Position-level bar ────────────────────────────────────────────────
        levels_bar = ui.row().classes(
            'w-full px-3 py-1.5 bg-gray-800/60 '
            'border-t border-gray-700/60 gap-5 items-center flex-wrap'
        )
        with levels_bar:
            entry_lbl = ui.label('').classes('text-xs font-mono')
            stop_lbl  = ui.label('').classes('text-xs font-mono')
            tgt_lbl   = ui.label('').classes('text-xs font-mono')
        levels_bar.set_visibility(False)

    # ── Interval selector handler ─────────────────────────────────────────────
    def _on_interval(e) -> None:
        iv = e.value
        state.watch_interval = iv
        if last['interval'] == iv:
            return
        last['interval'] = iv
        ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))

    iv_select.on('update:model-value', _on_interval)

    # ── Height slider handler ─────────────────────────────────────────────────
    def _on_height(e) -> None:
        h = int(e.value)
        last['height'] = h
        h_lbl.set_text(f'{h}px')
        chart_wrap.style(f'width:100%; height:{h}px;')

    h_slider.on('update:model-value', _on_height)

    # ── Tick update ───────────────────────────────────────────────────────────
    def update() -> None:
        tv_sym = _tv_symbol(
            shared.get('exchange', 'binance'),
            shared.get('symbol', 'BTC/USDT'),
        )
        iv = getattr(state, 'watch_interval', '15m')

        sym_changed = tv_sym != last['symbol']
        iv_changed  = iv != last['interval']

        if not last['initialized'] or sym_changed:
            last.update({'symbol': tv_sym, 'interval': iv, 'initialized': True})
            ui.run_javascript(_init_js(tv_sym, iv))
        elif iv_changed:
            last['interval'] = iv
            ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))

        # ── Position levels overlay ───────────────────────────────────────────
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

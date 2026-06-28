"""
TradingView chart widget.

Drawings & indicator persistence:
  - The free TradingView embed widget saves drawings to the user's TradingView
    account when logged in, or to browser localStorage when not logged in.
  - We never destroy the widget once created — setSymbol() is used for symbol
    switches, so drawings always survive in-session.
  - A 3-second creation guard prevents double-init (NiceGUI reconnect races).
  - Height and interval are persisted to Redis via state.ui_prefs.

Favorites:
  - Stored in state.ui_prefs['favorites'] → Redis.
  - Quick-switch chips shown below the header.
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
        'autosize':                  True,
        'symbol':                    tv_symbol,
        'interval':                  iv,
        'timezone':                  'Asia/Kolkata',
        'theme':                     'dark',
        'style':                     '1',
        'locale':                    'en',
        'toolbar_bg':                '#1f2937',
        'backgroundColor':           '#111827',
        'gridColor':                 'rgba(55,65,81,0.5)',
        'enable_publishing':         False,
        'save_image':                True,
        'hide_top_toolbar':          False,
        'hide_side_toolbar':         False,
        'withdateranges':            True,
        'chartsStorageApiVersion':   '1.1',
        'client_id':                 'crypto_bot',
        'user_id':                   'default',
        'load_last_chart':           True,
        'container_id':              _CONTAINER_ID,
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

    var now = Date.now();
    if (window._tvWidgetCreatedAt && (now - window._tvWidgetCreatedAt) < 3000) {{ return; }}
    window._tvWidgetCreatedAt = now;

    el.innerHTML = '';
    window._tvWidgetReady = false;

    // Build config and inject dynamic storageUrl (must use window.location at runtime)
    var cfg = {cfg};
    cfg.chartsStorageUrl = window.location.protocol + '//' + window.location.host + '/tv_storage';

    window._tvWidget = new TradingView.widget(cfg);
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
    saved_height   = state.ui_prefs.get('chart_height',   440)
    saved_interval = state.ui_prefs.get('chart_interval', '15m')
    if saved_interval and saved_interval != state.watch_interval:
        state.watch_interval = saved_interval

    last = {
        'symbol':      None,
        'interval':    None,
        'initialized': False,
        'height':      saved_height,
    }

    prev_favs_hash = {'v': ''}

    with ui.card().classes(
        'w-full bg-gray-900 p-0 rounded-xl overflow-hidden shadow-lg'
    ):
        # ── Header ────────────────────────────────────────────────────────────
        with ui.row().classes(
            'w-full items-center px-3 py-1.5 bg-gray-800/80 gap-2 flex-wrap'
        ):
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

            # Add-to-favorites button
            async def add_fav() -> None:
                favs  = list(state.ui_prefs.get('favorites', []))
                entry = {'exchange': shared.get('exchange', ''),
                         'symbol':   shared.get('symbol', '')}
                if entry not in favs:
                    favs.append(entry)
                    await state.save_ui_prefs({'favorites': favs})
                    _render_favs()
                    ui.notify(f"★ {entry['symbol']} added", type='positive',
                              position='bottom-right')
                else:
                    ui.notify('Already in favorites', type='info',
                              position='bottom-right')

            ui.button(icon='star_outline', on_click=add_fav).props(
                'flat dense size=xs'
            ).classes('text-yellow-400').tooltip('Add to favorites')

            ui.label('H').classes('text-gray-500 text-xs')
            h_lbl = ui.label(f'{saved_height}px').classes('text-gray-500 text-xs w-12')

        # ── Favorites bar ─────────────────────────────────────────────────────
        favs_row = ui.row().classes(
            'w-full px-3 py-1 bg-gray-800/40 gap-1.5 items-center flex-wrap '
            'border-b border-gray-700/40'
        )
        fav_inner = ui.row().classes('gap-1.5 flex-wrap flex-1')
        with favs_row:
            ui.label('★').classes('text-yellow-600 text-xs')

        def _render_favs() -> None:
            fav_inner.clear()
            favs = state.ui_prefs.get('favorites', [])
            with fav_inner:
                if not favs:
                    ui.label('No favorites — click ★ to add').classes(
                        'text-gray-600 text-xs'
                    )
                    return
                for fav in favs:
                    sym   = fav.get('symbol', '')
                    exch  = (fav.get('exchange', '').upper()
                             .replace('_FUTURES', '-F').replace('BINANCE', 'BNF'))
                    label = f"{exch}:{sym.replace('/','')}"

                    with ui.row().classes('items-center gap-0 bg-gray-700/50 rounded px-0.5'):
                        async def _switch(f=fav) -> None:
                            shared['exchange']   = f['exchange']
                            shared['symbol']     = f['symbol']
                            state.watch_exchange = f['exchange']
                            state.watch_symbol   = f['symbol']

                        ui.button(label, on_click=_switch).props(
                            'dense flat size=xs'
                        ).classes('text-yellow-300 text-xs px-1.5')

                        async def _rm(f=fav) -> None:
                            new_favs = [x for x in state.ui_prefs.get('favorites', [])
                                        if x != f]
                            await state.save_ui_prefs({'favorites': new_favs})
                            _render_favs()

                        ui.button(icon='close', on_click=_rm).props(
                            'flat round dense size=xs'
                        ).classes('text-gray-600 w-4 h-4')

        favs_row.add_slot('default', '')   # will be populated by _render_favs
        with favs_row:
            fav_inner   # attach the inner row

        # ── Chart container ───────────────────────────────────────────────────
        chart_wrap = ui.element('div').style(
            f'width:100%; height:{saved_height}px;'
        )
        with chart_wrap:
            ui.element('div').style('width:100%; height:100%;') \
                ._props.__setitem__('id', _CONTAINER_ID)

        # ── Position levels bar ───────────────────────────────────────────────
        levels_bar = ui.row().classes(
            'w-full px-3 py-1.5 bg-gray-800/60 border-t border-gray-700/60 '
            'gap-5 items-center flex-wrap'
        )
        with levels_bar:
            entry_lbl = ui.label('').classes('text-xs font-mono')
            stop_lbl  = ui.label('').classes('text-xs font-mono')
            tgt_lbl   = ui.label('').classes('text-xs font-mono')
        levels_bar.set_visibility(False)

    # ── Height slider — placed OUTSIDE card so chart_wrap is already defined ─
    ui.slider(
        min=280, max=820, step=40, value=saved_height,
        on_change=lambda e: _on_height(int(e.value)),
    ).classes('w-28').props('dense color=grey-7')

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _on_interval(e) -> None:
        iv = e.value
        state.watch_interval = iv
        if last['interval'] == iv:
            return
        last['interval'] = iv
        ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))
        asyncio.ensure_future(state.save_ui_prefs({'chart_interval': iv}))

    iv_select.on('update:model-value', _on_interval)

    def _on_height(h: int) -> None:
        last['height'] = h
        h_lbl.set_text(f'{h}px')
        chart_wrap.style(f'width:100%; height:{h}px;')
        ui.run_javascript(
            'try{if(window._tvWidget&&window._tvWidget.resize)'
            'window._tvWidget.resize();}catch(e){}'
        )
        asyncio.ensure_future(state.save_ui_prefs({'chart_height': h}))

    # Initial favorites render
    _render_favs()

    # ── Tick update ───────────────────────────────────────────────────────────
    def update() -> None:
        # Sync height from prefs if they loaded after build
        if not last['initialized']:
            nh = state.ui_prefs.get('chart_height', 440)
            if nh != last['height']:
                _on_height(nh)

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

        # Favorites re-render if prefs changed
        cur_fav_hash = str(state.ui_prefs.get('favorites', []))
        if cur_fav_hash != prev_favs_hash['v']:
            prev_favs_hash['v'] = cur_fav_hash
            _render_favs()

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

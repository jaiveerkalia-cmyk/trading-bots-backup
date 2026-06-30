"""
TradingView chart widget.

Layout persistence (no login required):
  The free embeddable widget exposes widget.save(callback)/widget.load(state)
  as plain JS instance methods — these work without any TradingView account
  or chartsStorageUrl backend. We capture the state object on save() and POST
  it to our own /tv_layout endpoint (Redis + file backup), keyed by
  exchange+symbol. On chart ready and on every symbol switch we fetch and
  load the matching saved layout, and we save the OLD symbol's layout right
  before switching away from it.

Height/interval/favorites/watchlist persist via state.ui_prefs (separate
mechanism, unrelated to chart drawings).
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


# ── JS: save/load helpers (installed once on window) ────────────────────────

_JS_HELPERS = """
window._tvSaveLayout = function(exchange, symbol, silent) {
    if (!window._tvWidget || !window._tvWidgetReady) return;
    try {
        window._tvWidget.save(function(state) {
            fetch('/tv_layout?exchange=' + encodeURIComponent(exchange) +
                  '&symbol=' + encodeURIComponent(symbol), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(state)
            }).catch(function(e){ console.warn('[TV] save POST failed', e); });
        });
    } catch(e) { console.warn('[TV] save() failed', e); }
};

window._tvLoadLayout = function(exchange, symbol) {
    if (!window._tvWidget || !window._tvWidgetReady) return;
    fetch('/tv_layout?exchange=' + encodeURIComponent(exchange) +
          '&symbol=' + encodeURIComponent(symbol))
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(data){
            if (data && data.content) {
                try { window._tvWidget.load(data.content); }
                catch(e){ console.warn('[TV] load() failed', e); }
            }
        })
        .catch(function(e){ console.warn('[TV] load GET failed', e); });
};

// Periodic autosave — runs once per page session
if (!window._tvAutosaveTimer) {
    window._tvAutosaveEnabled = window._tvAutosaveEnabled || false;
    window._tvAutosaveTimer = setInterval(function() {
        if (window._tvAutosaveEnabled && window._tvCurrentKey) {
            window._tvSaveLayout(
                window._tvCurrentKey.exchange,
                window._tvCurrentKey.symbol,
                true
            );
        }
    }, 20000);
}
"""


def _init_js(tv_symbol: str, interval: str, exchange: str, symbol: str) -> str:
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
    exch_esc = exchange.replace("'", "")
    sym_esc  = symbol.replace("'", "")
    return f"""
{_JS_HELPERS}

(function tryInit() {{
    if (typeof TradingView === 'undefined') {{ setTimeout(tryInit, 400); return; }}
    var el = document.getElementById('{_CONTAINER_ID}');
    if (!el) {{ setTimeout(tryInit, 400); return; }}

    // ── Soft update: symbol switch on an already-created widget ────────────
    if (window._tvWidget && window._tvWidgetReady) {{
        try {{
            // Save the OLD symbol's layout before switching away from it
            if (window._tvCurrentKey) {{
                window._tvSaveLayout(
                    window._tvCurrentKey.exchange,
                    window._tvCurrentKey.symbol,
                    true
                );
            }}
            window._tvWidget.setSymbol('{tv_symbol}', '{iv}', function() {{
                window._tvCurrentKey = {{exchange: '{exch_esc}', symbol: '{sym_esc}'}};
                window._tvLoadLayout('{exch_esc}', '{sym_esc}');
            }});
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
    window._tvWidget = new TradingView.widget({cfg});
    if (window._tvWidget && window._tvWidget.onChartReady) {{
        window._tvWidget.onChartReady(function() {{
            window._tvWidgetReady = true;
            window._tvCurrentKey  = {{exchange: '{exch_esc}', symbol: '{sym_esc}'}};
            window._tvLoadLayout('{exch_esc}', '{sym_esc}');
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
    saved_autosave = state.ui_prefs.get('tv_autosave', True)
    if saved_interval and saved_interval != state.watch_interval:
        state.watch_interval = saved_interval

    last = {
        'symbol':      None,
        'interval':    None,
        'initialized': False,
        'height':      saved_height,
        'exchange':    None,
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

            # ── Save Layout button ───────────────────────────────────────────
            def save_layout_now() -> None:
                exch = shared.get('exchange', '')
                sym  = shared.get('symbol', '')
                ui.run_javascript(
                    f"window._tvSaveLayout('{exch}', '{sym}', false);"
                )
                ui.notify('Chart layout saved', type='positive',
                          position='bottom-right')

            ui.button('Save Layout', on_click=save_layout_now).props(
                'flat dense size=xs'
            ).classes('text-green-400 text-xs').tooltip(
                'Save drawings, indicators and settings for this symbol'
            )

            # ── Autosave toggle ───────────────────────────────────────────────
            def _on_autosave(e) -> None:
                ui.run_javascript(
                    f"window._tvAutosaveEnabled = {str(bool(e.value)).lower()};"
                )
                asyncio.ensure_future(state.save_ui_prefs({'tv_autosave': e.value}))

            ui.switch('Autosave', value=saved_autosave, on_change=_on_autosave).props(
                'dense dark size=xs'
            ).classes('text-xs text-gray-400')

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
        favs_row  = ui.row().classes(
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

        with favs_row:
            fav_inner

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

    # ── Height slider ─────────────────────────────────────────────────────────
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

    _render_favs()

    # ── Tick update ───────────────────────────────────────────────────────────
    def update() -> None:
        if not last['initialized']:
            nh = state.ui_prefs.get('chart_height', 440)
            if nh != last['height']:
                _on_height(nh)
            # Sync autosave flag into JS once chart has had a chance to init
            ui.run_javascript(
                f"window._tvAutosaveEnabled = "
                f"{str(bool(state.ui_prefs.get('tv_autosave', True))).lower()};"
            )

        exch   = shared.get('exchange', 'binance')
        sym    = shared.get('symbol',   'BTC/USDT')
        tv_sym = _tv_symbol(exch, sym)
        iv     = getattr(state, 'watch_interval', '15m')

        symbol_changed = (tv_sym != last['symbol'])

        if not last['initialized'] or symbol_changed:
            last.update({
                'symbol': tv_sym, 'interval': iv,
                'initialized': True, 'exchange': exch,
            })
            ui.run_javascript(_init_js(tv_sym, iv, exch, sym))
        elif iv != last['interval']:
            last['interval'] = iv
            ui.run_javascript(_set_resolution_js(_INTERVAL_MAP.get(iv, '1')))

        cur_fav_hash = str(state.ui_prefs.get('favorites', []))
        if cur_fav_hash != prev_favs_hash['v']:
            prev_favs_hash['v'] = cur_fav_hash
            _render_favs()

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

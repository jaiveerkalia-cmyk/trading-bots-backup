from __future__ import annotations
import asyncio
import json
import logging
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands
from common import settings

if TYPE_CHECKING:
    from ui.state import UIState


async def _subscribe_md(redis: aioredis.Redis, exchange: str, symbol: str) -> None:
    try:
        await redis.publish('market_data:control', json.dumps({
            'cmd': 'subscribe', 'exchange': exchange,
            'symbol': symbol, 'streams': ['ticker'],
        }))
    except Exception as e:
        logging.getLogger('top_bar').warning("MD subscribe: %s", e)


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    prev_title = {'v': ''}
    prev_wl    = {'v': ''}

    with ui.column().classes('w-full gap-0'):
        # ── Main header row ───────────────────────────────────────────────────
        with ui.row().classes(
            'w-full items-center justify-between px-4 py-2 '
            'bg-gray-900 border-b border-gray-700/60 flex-wrap gap-3'
        ):
            with ui.row().classes('items-center gap-3 flex-wrap'):
                ui.label('⚡ Crypto Engine').classes(
                    'text-white font-bold text-sm mr-1'
                )

                def _on_exchange(e) -> None:
                    exch = e.value
                    shared.update({'exchange': exch})
                    state.watch_exchange = exch
                    asyncio.ensure_future(
                        state.save_ui_prefs({'watch_exchange': exch})
                    )
                    asyncio.ensure_future(
                        _subscribe_md(redis, exch, shared['symbol'])
                    )

                ui.select(
                    options=settings.SUPPORTED_EXCHANGES,
                    value=shared['exchange'],
                    on_change=_on_exchange,
                ).props('dense dark outlined label=Exchange').classes('w-36')

                sym_inp = ui.input(
                    value=shared['symbol']
                ).props('dense dark outlined label=Symbol').classes('w-32')

                def _apply_symbol() -> None:
                    s = sym_inp.value.strip().upper()
                    if not s:
                        return
                    shared['symbol']   = s
                    state.watch_symbol = s
                    asyncio.ensure_future(
                        state.save_ui_prefs({'watch_symbol': s})
                    )
                    asyncio.ensure_future(
                        _subscribe_md(redis, shared['exchange'], s)
                    )

                sym_inp.on('blur',          lambda _: _apply_symbol())
                sym_inp.on('keydown.enter', lambda _: _apply_symbol())

                ui.number(
                    value=shared['risk_pct'], min=0.01, max=10,
                    step=0.1, format='%g',
                    on_change=lambda e: shared.update(
                        {'risk_pct': float(e.value or 0.5)}
                    ),
                ).props('dense dark outlined label="Risk %"').classes('w-20')

                ui.number(
                    value=shared['balance'], min=0, step=100, format='%g',
                    on_change=lambda e: _on_balance(e, state, shared),
                ).props('dense dark outlined label="Balance $"').classes('w-28')

                async def reset() -> None:
                    state.reset_portfolio()
                    await state.save_portfolio()
                    ui.notify('Portfolio reset ✓', type='positive')

                ui.button('Reset PnL', on_click=reset).props(
                    'dense unelevated'
                ).classes('bg-gray-700 text-gray-300 text-xs px-3')

            with ui.row().classes('items-center gap-4'):
                with ui.column().classes('items-end leading-none gap-0'):
                    ui.label('Starting').classes('text-gray-500 text-xs')
                    start_lbl = ui.label('$0').classes(
                        'text-sm font-mono text-gray-300'
                    )
                with ui.column().classes('items-end leading-none gap-0'):
                    ui.label('Current').classes('text-gray-500 text-xs')
                    curr_lbl = ui.label('$0').classes(
                        'text-sm font-mono font-bold text-white'
                    )
                ui.separator().classes('bg-gray-700').style('width:1px;height:36px')
                with ui.column().classes('items-end leading-none gap-0'):
                    ui.label('Unrealized').classes('text-gray-500 text-xs')
                    unr = ui.label('$0.00').classes(
                        'text-base font-mono font-bold text-white'
                    )
                with ui.column().classes('items-end leading-none gap-0'):
                    ui.label('Realized').classes('text-gray-500 text-xs')
                    rel = ui.label('$0.00').classes(
                        'text-base font-mono font-bold text-white'
                    )

                mode_btn = ui.button('PAPER').props('unelevated').classes(
                    'bg-green-800 text-white text-xs px-4 font-bold'
                )
                mode_btn.on('click', lambda: asyncio.ensure_future(
                    commands.set_live_mode(redis, not state.live_mode)
                ))

        # ── Watchlist bar ─────────────────────────────────────────────────────
        with ui.row().classes(
            'w-full px-4 py-2 bg-gray-800/80 border-b border-gray-700/40 '
            'items-center gap-2 flex-wrap'
        ):
            ui.label('⭐').classes('text-yellow-400 text-sm')
            wl_inner = ui.row().classes('flex-1 gap-2 flex-wrap items-center')

            async def add_to_watchlist() -> None:
                wl    = list(state.ui_prefs.get('watchlist', []))
                entry = {'exchange': shared['exchange'], 'symbol': shared['symbol']}
                if entry not in wl:
                    wl.append(entry)
                    await state.save_ui_prefs({'watchlist': wl})
                    ui.notify(f"★ {entry['symbol']} added",
                              type='positive', position='bottom-right')
                else:
                    ui.notify('Already in watchlist', type='info',
                              position='bottom-right')

            ui.button(icon='add_circle_outline', on_click=add_to_watchlist).props(
                'flat dense size=sm'
            ).classes('text-yellow-400').tooltip('Add current symbol')

    def _render_watchlist() -> None:
        wl_inner.clear()
        with wl_inner:
            wl = state.ui_prefs.get('watchlist', [])
            if not wl:
                ui.label('Add symbols with ⭐').classes('text-gray-600 text-xs italic')
                return
            
            for item in wl:
                exch = item.get('exchange', '')
                sym  = item.get('symbol', '')
                exch_s = (exch.upper()
                          .replace('_FUTURES', '-F').replace('BINANCE', 'BNF'))
                label  = f"{exch_s}  {sym.replace('/','')}"
                
                is_active = (exch == shared.get('exchange') and
                             sym  == shared.get('symbol'))
                
                # Show price if available
                pk   = f"{exch}:{sym}"
                price = state.mark_prices.get(pk) or state.last_prices.get(pk, 0)
                price_str = f"${price:,.2f}" if price else ''
                
                async def _switch(e=exch, s=sym) -> None:
                    shared['exchange']   = e
                    shared['symbol']     = s
                    state.watch_exchange = e
                    state.watch_symbol   = s
                    asyncio.ensure_future(
                        state.save_ui_prefs({'watch_exchange': e, 'watch_symbol': s})
                    )
                    asyncio.ensure_future(_subscribe_md(redis, e, s))
                    
                async def _remove(e=exch, s=sym) -> None:
                    new_wl = [x for x in state.ui_prefs.get('watchlist', [])
                              if not (x.get('exchange') == e and x.get('symbol') == s)]
                    await state.save_ui_prefs({'watchlist': new_wl})

                # Card-style chip
                if is_active:
                    card_cls = ('bg-yellow-400 shadow-md '
                                'border-2 border-yellow-300')
                    label_cls = 'text-gray-900 font-bold text-sm'
                    price_cls = 'text-gray-700 text-xs font-mono'
                    close_cls = 'text-gray-600'
                else:
                    card_cls  = ('bg-white/10 border border-gray-500 '
                                 'hover:bg-white/20 cursor-pointer')
                    label_cls = 'text-white font-semibold text-sm'
                    price_cls = 'text-gray-300 text-xs font-mono'
                    close_cls = 'text-gray-500'

                with ui.row().classes(
                    f'items-center gap-1 rounded-lg px-3 py-1.5 '
                    f'{card_cls} transition-all'
                ):
                    with ui.column().classes('gap-0 leading-none cursor-pointer',
                                            ).on('click', _switch):
                        ui.label(label).classes(label_cls)
                        if price_str:
                            ui.label(price_str).classes(price_cls)
                            
                    ui.button(icon='close', on_click=_remove).props(
                        'flat round dense size=xs'
                    ).classes(f'{close_cls} w-5 h-5 ml-1')


    def update() -> None:
        u, r = state.get_display_pnl()
        s    = state.starting_balance
        c    = state.get_current_balance()

        unr.set_text(f"{'+'if u>=0 else ''}${abs(u):.2f}")
        rel.set_text(f"{'+'if r>=0 else ''}${abs(r):.2f}")
        unr.classes(remove='text-green-400 text-red-400 text-white',
                    add='text-green-400' if u >= 0 else 'text-red-400')
        rel.classes(remove='text-green-400 text-red-400 text-white',
                    add='text-green-400' if r >= 0 else 'text-red-400')
        start_lbl.set_text(f'${s:,.0f}')
        curr_lbl.set_text(f'${c:,.2f}')
        curr_lbl.classes(remove='text-white text-green-400 text-red-400',
                         add='text-green-400' if c >= s else 'text-red-400')

        if state.live_mode:
            mode_btn.set_text('LIVE')
            mode_btn.classes(remove='bg-green-800', add='bg-red-700')
        else:
            mode_btn.set_text('PAPER')
            mode_btn.classes(remove='bg-red-700', add='bg-green-800')

        # Browser title
        sym    = shared.get('symbol', 'BTC/USDT')
        exch   = (shared.get('exchange', '')
                  .upper().replace('_FUTURES', '-F').replace('BINANCE', 'BNF'))
        title  = f'Crypto Engine · {exch}:{sym}'
        if title != prev_title['v']:
            prev_title['v'] = title
            ui.run_javascript(f"document.title = '{title}';")

        # Watchlist — re-render when the list changes OR the active pair changes
        wl_hash = (
            str(state.ui_prefs.get('watchlist', []))
            + f"|{shared.get('exchange','')}:{shared.get('symbol','')}"
        )
        if wl_hash != prev_wl['v']:
            prev_wl['v'] = wl_hash
            _render_watchlist()

    return {'update': update}


def _on_balance(e, state, shared) -> None:
    v = float(e.value or 0)
    shared['balance']      = v
    state.starting_balance = v
    asyncio.ensure_future(state.save_portfolio())

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
    with ui.row().classes(
        'w-full items-center justify-between px-4 py-2 '
        'bg-gray-900 border-b border-gray-700 flex-wrap gap-3'
    ):
        with ui.row().classes('items-center gap-3 flex-wrap'):
            ui.label('⚡ Crypto Engine').classes('text-white font-bold text-sm mr-1')

            # Exchange selector
            def _on_exchange(e) -> None:
                exch = e.value
                shared.update({'exchange': exch})
                state.watch_exchange = exch
                asyncio.ensure_future(state.save_ui_prefs({'watch_exchange': exch}))
                asyncio.ensure_future(_subscribe_md(redis, exch, shared['symbol']))

            ui.select(
                options=settings.SUPPORTED_EXCHANGES,
                value=shared['exchange'],
                on_change=_on_exchange,
            ).props('dense dark outlined label=Exchange').classes('w-36')

            # Symbol input
            sym_inp = ui.input(value=shared['symbol']).props(
                'dense dark outlined label=Symbol'
            ).classes('w-32')

            def _apply_symbol() -> None:
                s = sym_inp.value.strip().upper()
                if not s:
                    return
                shared['symbol']   = s
                state.watch_symbol = s
                asyncio.ensure_future(state.save_ui_prefs({'watch_symbol': s}))
                asyncio.ensure_future(_subscribe_md(redis, shared['exchange'], s))

            sym_inp.on('blur',        lambda _: _apply_symbol())
            sym_inp.on('keydown.enter', lambda _: _apply_symbol())

            ui.number(
                value=shared['risk_pct'],
                min=0.01, max=10, step=0.1, format='%g',
                on_change=lambda e: shared.update(
                    {'risk_pct': float(e.value or 0.5)}
                ),
            ).props('dense dark outlined label="Risk %"').classes('w-20')

            ui.number(
                value=shared['balance'],
                min=0, step=100, format='%g',
                on_change=lambda e: _on_balance_change(e, state, shared),
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
                start_lbl = ui.label('$0').classes('text-sm font-mono text-gray-300')

            with ui.column().classes('items-end leading-none gap-0'):
                ui.label('Current').classes('text-gray-500 text-xs')
                curr_lbl = ui.label('$0').classes(
                    'text-sm font-mono font-bold text-white'
                )

            ui.separator().classes('bg-gray-700').style('width:1px; height:36px;')

            with ui.column().classes('items-end leading-none gap-0'):
                ui.label('Unrealized').classes('text-gray-500 text-xs')
                unr = ui.label('$0.00').classes('text-base font-mono font-bold text-white')

            with ui.column().classes('items-end leading-none gap-0'):
                ui.label('Realized').classes('text-gray-500 text-xs')
                rel = ui.label('$0.00').classes('text-base font-mono font-bold text-white')

            mode_btn = ui.button('PAPER').props('unelevated').classes(
                'bg-green-800 text-white text-xs px-4 font-bold'
            )

            async def toggle_mode() -> None:
                await commands.set_live_mode(redis, not state.live_mode)

            mode_btn.on('click', toggle_mode)

    def update() -> None:
        u, r = state.get_display_pnl()
        s    = state.starting_balance
        c    = state.get_current_balance()

        unr.set_text(f"{'+'if u>=0 else ''}${abs(u):.2f}")
        rel.set_text(f"{'+'if r>=0 else ''}${abs(r):.2f}")
        unr.classes(
            remove='text-green-400 text-red-400 text-white',
            add='text-green-400' if u >= 0 else 'text-red-400',
        )
        rel.classes(
            remove='text-green-400 text-red-400 text-white',
            add='text-green-400' if r >= 0 else 'text-red-400',
        )
        start_lbl.set_text(f"${s:,.0f}")
        curr_lbl.set_text(f"${c:,.2f}")
        curr_lbl.classes(
            remove='text-white text-green-400 text-red-400',
            add='text-green-400' if c >= s else 'text-red-400',
        )
        if state.live_mode:
            mode_btn.set_text('LIVE')
            mode_btn.classes(remove='bg-green-800', add='bg-red-700')
        else:
            mode_btn.set_text('PAPER')
            mode_btn.classes(remove='bg-red-700', add='bg-green-800')

    return {'update': update}


def _on_balance_change(e, state, shared) -> None:
    v = float(e.value or 0)
    shared.update({'balance': v})
    state.starting_balance = v
    asyncio.ensure_future(state.save_portfolio())

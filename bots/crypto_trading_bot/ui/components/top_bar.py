from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands
from common import settings

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    with ui.row().classes(
        'w-full items-center justify-between px-4 py-2 '
        'bg-gray-900 border-b border-gray-700 flex-wrap gap-4'
    ):
        # ── Left: controls ────────────────────────────────────────────────────
        with ui.row().classes('items-center gap-3 flex-wrap'):
            ui.label('⚡ Crypto Trading Engine').classes(
                'text-white font-bold text-base mr-2'
            )

            ex = ui.select(
                options=settings.SUPPORTED_EXCHANGES, value=shared['exchange'],
            ).props('dense dark outlined label=Exchange').classes('w-28')

            def on_exchange(e):
                shared['exchange']     = e.args
                state.watch_exchange   = e.args
            ex.on('update:model-value', on_exchange)

            sym = ui.input(value=shared['symbol']).props(
                'dense dark outlined label=Symbol'
            ).classes('w-32')

            def on_symbol():
                shared['symbol']   = sym.value
                state.watch_symbol = sym.value
                state.clear_candles()
            sym.on('blur', lambda _: on_symbol())

            ui.number(
                value=shared['risk_pct'], min=0.01, max=10,
                step=0.1, format='%.2f',
            ).props('dense dark outlined label="Risk %"').classes('w-20').on(
                'update:model-value',
                lambda e: shared.update({'risk_pct': float(e.args or 0.5)}),
            )

            ui.number(
                value=shared['balance'], min=0, step=100, format='%.2f',
            ).props('dense dark outlined label=Balance').classes('w-28').on(
                'update:model-value',
                lambda e: shared.update({'balance': float(e.args or 0)}),
            )

        # ── Right: mode + PnL ─────────────────────────────────────────────────
        with ui.row().classes('items-center gap-6'):
            mode_btn = ui.button('PAPER').props('unelevated').classes(
                'bg-green-800 text-white text-xs px-4 py-1 font-bold'
            )

            async def toggle_mode():
                new = not state.live_mode
                await commands.set_live_mode(redis, new)

            mode_btn.on('click', toggle_mode)

            with ui.column().classes('items-end leading-none gap-0'):
                ui.label('Unrealized').classes('text-gray-500 text-xs')
                unr = ui.label('$0.00').classes('text-base font-mono font-bold text-white')

            with ui.column().classes('items-end leading-none gap-0'):
                ui.label('Realized').classes('text-gray-500 text-xs')
                rel = ui.label('$0.00').classes('text-base font-mono font-bold text-white')

    def update():
        u = float(state.pnl.get('unrealized', 0))
        r = float(state.pnl.get('realized',   0))

        unr.set_text(f"{'+'if u>=0 else ''}${abs(u):.2f}")
        rel.set_text(f"{'+'if r>=0 else ''}${abs(r):.2f}")

        unr.classes(remove='text-green-400 text-red-400 text-white',
                    add='text-green-400' if u >= 0 else 'text-red-400')
        rel.classes(remove='text-green-400 text-red-400 text-white',
                    add='text-green-400' if r >= 0 else 'text-red-400')

        if state.live_mode:
            mode_btn.set_text('LIVE')
            mode_btn.classes(remove='bg-green-800', add='bg-red-700')
        else:
            mode_btn.set_text('PAPER')
            mode_btn.classes(remove='bg-red-700', add='bg-green-800')

    return {'update': update}

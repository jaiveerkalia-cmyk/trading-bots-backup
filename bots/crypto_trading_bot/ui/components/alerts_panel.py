from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    form = {'upper': None, 'lower': None, 'period': 'current'}

    with ui.card().classes('w-full p-3 bg-gray-900 border border-yellow-900 rounded-lg'):
        ui.label('Alerts').classes('text-yellow-400 font-bold text-sm mb-2')

        with ui.row().classes('w-full gap-2'):
            ui.number(label='Upper', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: form.update({'upper': float(e.args) if e.args else None}),
            )
            ui.number(label='Lower', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: form.update({'lower': float(e.args) if e.args else None}),
            )

        ui.select(
            ['current', '1m', '5m'], value='current', label='Period',
        ).props('dense dark outlined').classes('w-full my-2').on(
            'update:model-value', lambda e: form.update({'period': e.args})
        )

        async def set_alert():
            if not form['upper'] and not form['lower']:
                ui.notify('Set at least one price level', type='warning')
                return
            alert = {
                'exchange': shared.get('exchange', 'binance'),
                'symbol':   shared.get('symbol',   'BTC/USDT'),
                'upper':    form['upper'],
                'lower':    form['lower'],
                'period':   form['period'],
            }
            await commands.set_alert(redis, alert)
            ui.notify('Alert set ✓', type='positive')

        ui.button('Set Alert', on_click=set_alert,
        ).props('unelevated').classes('w-full bg-yellow-700 text-white mb-3')

        # Active alerts list
        ui.separator().classes('bg-gray-700 mb-2')
        ui.label('Active Alerts').classes('text-gray-500 text-xs mb-1')
        alerts_container = ui.column().classes('w-full gap-1')

        # ── Separator + Close All ─────────────────────────────────────────────
        ui.separator().classes('bg-gray-700 my-3')

        async def close_all():
            await commands.close_all(redis)
            ui.notify('Closing all positions...', type='warning')

        ui.button('CLOSE ALL POSITIONS', on_click=close_all,
        ).props('unelevated').classes('w-full bg-red-700 text-white font-bold')

    def update():
        alerts_container.clear()
        with alerts_container:
            if not state.alerts:
                ui.label('No active alerts').classes('text-gray-600 text-xs')
                return
            for alert in state.alerts:
                if alert.get('triggered'):
                    continue
                with ui.row().classes('w-full items-center justify-between'):
                    sym = alert.get('symbol', '')
                    u   = alert.get('upper')
                    l   = alert.get('lower')
                    lbl = f"{sym}"
                    if u: lbl += f" ▲{u}"
                    if l: lbl += f" ▼{l}"
                    ui.label(lbl).classes('text-xs text-yellow-300')

                    async def del_alert(aid=alert.get('id', '')):
                        await commands.delete_alert(redis, aid)
                    ui.button(icon='close', on_click=del_alert,
                    ).props('flat round dense size=xs').classes('text-gray-500')

    return {'update': update}

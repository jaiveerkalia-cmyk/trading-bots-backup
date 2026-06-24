from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState

_SOUND_JS = """
(function(){{
  try {{
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    var osc  = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(660, ctx.currentTime + 0.2);
    osc.frequency.setValueAtTime(880, ctx.currentTime + 0.4);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + {dur});
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + {dur});
  }} catch(e) {{ console.warn('Alert sound:', e); }}
}})();
"""


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    form          = {'upper': None, 'lower': None, 'period': 'current'}
    sound_cfg     = {'enabled': True, 'duration': 5}
    prev_triggered: set[str] = set()

    with ui.card().classes(
        'w-full p-3 bg-gray-900 border border-yellow-900 rounded-lg'
    ):
        ui.label('Alerts').classes('text-yellow-400 font-bold text-sm mb-2')

        with ui.row().classes('w-full gap-2'):
            ui.number(
                label='Upper', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: form.update(
                    {'upper': float(e.args) if e.args else None}
                ),
            )
            ui.number(
                label='Lower', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: form.update(
                    {'lower': float(e.args) if e.args else None}
                ),
            )

        ui.select(
            ['current', '1m', '5m'], value='current', label='Period',
            on_change=lambda e: form.update({'period': e.value}),
        ).props('dense dark outlined').classes('w-full my-2')

        async def set_alert():
            if not form['upper'] and not form['lower']:
                ui.notify('Set at least one price level', type='warning')
                return
            await commands.set_alert(redis, {
                'exchange': shared.get('exchange', 'binance'),
                'symbol':   shared.get('symbol',   'BTC/USDT'),
                'upper':    form['upper'],
                'lower':    form['lower'],
                'period':   form['period'],
            })
            ui.notify('Alert set ✓', type='positive')

        ui.button('Set Alert', on_click=set_alert,
        ).props('unelevated').classes('w-full bg-yellow-700 text-white mb-2')

        # Sound settings
        with ui.row().classes('w-full items-center gap-3 mb-2'):
            ui.switch(
                'Sound', value=True,
                on_change=lambda e: sound_cfg.update({'enabled': e.value}),
            ).props('dense dark').classes('text-xs text-gray-400')
            ui.number(
                label='Duration (s)', value=5, min=1, max=30, step=1,
                on_change=lambda e: sound_cfg.update(
                    {'duration': int(e.value or 5)}
                ),
            ).props('dense dark outlined').classes('w-28 text-xs')

        ui.separator().classes('bg-gray-700 mb-2')
        ui.label('Active').classes('text-gray-500 text-xs mb-1')
        alerts_container = ui.column().classes('w-full gap-1')

        ui.separator().classes('bg-gray-700 my-3')

        async def close_all():
            await commands.close_all(redis)
            ui.notify('Closing all positions...', type='warning')

        ui.button(
            'CLOSE ALL POSITIONS', on_click=close_all,
        ).props('unelevated').classes('w-full bg-red-700 text-white font-bold')

    def update():
        # Play sound for newly triggered alerts
        if sound_cfg['enabled']:
            for alert in state.alerts:
                aid = alert.get('id', '')
                if alert.get('triggered') and aid and aid not in prev_triggered:
                    prev_triggered.add(aid)
                    dur = sound_cfg['duration']
                    ui.run_javascript(_SOUND_JS.format(dur=dur))

        # Render active alert list
        alerts_container.clear()
        with alerts_container:
            active = [a for a in state.alerts if not a.get('triggered')]
            if not active:
                ui.label('No active alerts').classes('text-gray-600 text-xs')
                return
            for alert in active:
                with ui.row().classes('w-full items-center justify-between'):
                    sym = alert.get('symbol', '')
                    u, l = alert.get('upper'), alert.get('lower')
                    lbl  = sym
                    if u: lbl += f" ▲{u}"
                    if l: lbl += f" ▼{l}"
                    ui.label(lbl).classes('text-xs text-yellow-300')

                    async def del_alert(aid=alert.get('id', '')):
                        await commands.delete_alert(redis, aid)

                    ui.button(icon='close', on_click=del_alert,
                    ).props('flat round dense size=xs').classes('text-gray-500')

    return {'update': update}

"""
Alerts panel.
- Alert sounds repeat for the full configured duration (multi-oscillator pattern).
- Sound choice, duration and enabled state persisted to Redis via state.ui_prefs.
- Reset button clears triggered alerts.
- Triggered alerts appear in the UI activity log (fixed in main.py _on_alert_fired).
"""
from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState

# ── Repeating sound patterns ──────────────────────────────────────────────────
# Each body uses `dur` (seconds) to calculate how many times to repeat.
# Multiple oscillators are pre-scheduled via Web Audio API.

_SOUND_BODIES: dict[str, str] = {
    'Beep': """
        var period=0.45, bDur=0.28;
        var reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){
            var s=t+i*period;
            var o=ctx.createOscillator(),g=ctx.createGain();
            o.connect(g);g.connect(ctx.destination);
            o.type='sine';o.frequency.value=880;
            g.gain.setValueAtTime(0,s);
            g.gain.linearRampToValueAtTime(0.35,s+0.02);
            g.gain.exponentialRampToValueAtTime(0.001,s+bDur);
            o.start(s);o.stop(s+bDur+0.01);
        }
    """,
    'Chime': """
        var seq=[1047,1319,1568], period=0.85, noteDur=0.38;
        var reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){
            var base=t+i*period;
            seq.forEach(function(f,j){
                var s=base+j*0.13;
                var o=ctx.createOscillator(),g=ctx.createGain();
                o.connect(g);g.connect(ctx.destination);
                o.type='sine';o.frequency.value=f;
                g.gain.setValueAtTime(0.22,s);
                g.gain.exponentialRampToValueAtTime(0.001,s+noteDur);
                o.start(s);o.stop(s+noteDur+0.02);
            });
        }
    """,
    'Alarm': """
        var period=0.22, reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){
            var s=t+i*period;
            var o=ctx.createOscillator(),g=ctx.createGain();
            o.connect(g);g.connect(ctx.destination);
            o.type='square';
            o.frequency.value=(i%2===0)?880:660;
            g.gain.setValueAtTime(0.18,s);
            g.gain.exponentialRampToValueAtTime(0.001,s+period*0.88);
            o.start(s);o.stop(s+period*0.9);
        }
    """,
    'Buzz': """
        var period=0.38, bDur=0.26, reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){
            var s=t+i*period;
            var o=ctx.createOscillator(),g=ctx.createGain();
            o.connect(g);g.connect(ctx.destination);
            o.type='sawtooth';o.frequency.value=180;
            g.gain.setValueAtTime(0.15,s);
            g.gain.exponentialRampToValueAtTime(0.001,s+bDur);
            o.start(s);o.stop(s+bDur+0.01);
        }
    """,
    'Ping': """
        var period=0.65, bDur=0.45, reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){
            var s=t+i*period;
            var o=ctx.createOscillator(),g=ctx.createGain();
            o.connect(g);g.connect(ctx.destination);
            o.type='sine';o.frequency.value=1760;
            g.gain.setValueAtTime(0.4,s);
            g.gain.exponentialRampToValueAtTime(0.001,s+bDur);
            o.start(s);o.stop(s+bDur+0.01);
        }
    """,
}

_SOUND_WRAPPER = """
(function(){{
  try {{
    var ctx=new(window.AudioContext||window.webkitAudioContext)();
    if(ctx.state==='suspended'){{ ctx.resume(); }}
    var t=ctx.currentTime+0.05, dur={dur};
    {body}
  }} catch(e){{ console.warn('Alert sound:',e); }}
}})();
"""


def _play(sound_name: str, duration: float) -> None:
    body = _SOUND_BODIES.get(sound_name, _SOUND_BODIES['Beep'])
    ui.run_javascript(_SOUND_WRAPPER.format(body=body, dur=max(0.1, duration)))


# ── Component ─────────────────────────────────────────────────────────────────

def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    # Load persisted prefs
    sound_cfg = {
        'enabled':  state.ui_prefs.get('alert_enabled', True),
        'name':     state.ui_prefs.get('alert_sound',   'Beep'),
        'duration': float(state.ui_prefs.get('alert_dur', 3.0)),
    }
    form: dict = {'upper': None, 'lower': None, 'period': 'current'}
    prev_triggered: set[str] = set()

    def _save_sound_prefs() -> None:
        asyncio.ensure_future(state.save_ui_prefs({
            'alert_enabled': sound_cfg['enabled'],
            'alert_sound':   sound_cfg['name'],
            'alert_dur':     sound_cfg['duration'],
        }))

    with ui.card().classes(
        'w-full p-3 bg-gray-900 border border-yellow-900/50 rounded-lg'
    ):
        ui.label('Alerts').classes('text-yellow-400 font-bold text-sm mb-2')

        # Price levels
        with ui.row().classes('w-full gap-2'):
            ui.number(
                label='Upper ▲', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: form.update(
                    {'upper': float(e.args) if e.args else None}
                ),
            )
            ui.number(
                label='Lower ▼', value=0, min=0, format='%.4f',
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

        async def set_alert() -> None:
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
        ).props('unelevated').classes('w-full bg-yellow-700 text-white mb-3')

        # ── Sound settings ────────────────────────────────────────────────────
        with ui.row().classes('w-full items-center gap-2 mb-1 flex-wrap'):
            def _on_enabled(e) -> None:
                sound_cfg['enabled'] = e.value
                _save_sound_prefs()

            ui.switch(
                'Sound', value=sound_cfg['enabled'],
                on_change=_on_enabled,
            ).props('dense dark').classes('text-xs text-gray-400')

            def _on_sound(e) -> None:
                sound_cfg['name'] = e.value
                _save_sound_prefs()

            ui.select(
                list(_SOUND_BODIES.keys()),
                value=sound_cfg['name'],
                label='Sound',
                on_change=_on_sound,
            ).props('dense dark outlined').classes('w-24 text-xs')

            def _on_dur(e) -> None:
                sound_cfg['duration'] = float(e.value or 3)
                _save_sound_prefs()

            ui.number(
                label='Dur(s)', value=sound_cfg['duration'],
                min=0.5, max=30, step=0.5,
                on_change=_on_dur,
            ).props('dense dark outlined').classes('w-20 text-xs')

            async def preview_sound() -> None:
                _play(sound_cfg['name'], sound_cfg['duration'])

            ui.button(icon='volume_up', on_click=preview_sound).props(
                'flat round dense size=xs'
            ).classes('text-yellow-400').tooltip('Preview sound')

        ui.separator().classes('bg-gray-700 mb-2')

        # ── Active alerts list ────────────────────────────────────────────────
        with ui.row().classes('w-full items-center justify-between mb-1'):
            ui.label('Active').classes(
                'text-gray-500 text-xs uppercase tracking-wider'
            )

            async def reset_all() -> None:
                await commands.reset_alerts(redis)
                prev_triggered.clear()
                ui.notify('Alerts reset', type='info')

            ui.button('Reset All', on_click=reset_all).props(
                'flat dense size=xs unelevated'
            ).classes('text-gray-500 text-xs')

        alerts_container = ui.column().classes('w-full gap-1')

        ui.separator().classes('bg-gray-700 my-3')

        async def close_all() -> None:
            await commands.close_all(redis)
            ui.notify('Closing all positions…', type='warning')

        ui.button(
            'CLOSE ALL POSITIONS', on_click=close_all,
        ).props('unelevated').classes('w-full bg-red-700 text-white font-bold')

    # ── Update ────────────────────────────────────────────────────────────────
    def update() -> None:
        # Sync prefs if they loaded after build
        if state.ui_prefs.get('alert_sound') and \
                state.ui_prefs['alert_sound'] != sound_cfg.get('_synced'):
            sound_cfg['_synced'] = state.ui_prefs['alert_sound']

        if sound_cfg['enabled']:
            for alert in state.alerts:
                aid = alert.get('id', '')
                if alert.get('triggered') and aid and aid not in prev_triggered:
                    prev_triggered.add(aid)
                    _play(sound_cfg['name'], sound_cfg['duration'])

        alerts_container.clear()
        with alerts_container:
            active = [a for a in state.alerts if not a.get('triggered')]
            if not active:
                ui.label('No active alerts').classes('text-gray-600 text-xs')
                return
            for alert in active:
                with ui.row().classes(
                    'w-full items-center justify-between py-0.5'
                ):
                    sym = alert.get('symbol', '')
                    u, l = alert.get('upper'), alert.get('lower')
                    parts = [sym]
                    if u: parts.append(f'▲{u}')
                    if l: parts.append(f'▼{l}')
                    ui.label('  '.join(parts)).classes(
                        'text-xs text-yellow-300 font-mono'
                    )

                    async def del_alert(aid=alert.get('id', '')) -> None:
                        await commands.delete_alert(redis, aid)

                    ui.button(icon='close', on_click=del_alert).props(
                        'flat round dense size=xs'
                    ).classes('text-gray-500')

    return {'update': update}

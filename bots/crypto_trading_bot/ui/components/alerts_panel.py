"""
Alerts panel.
- Upper and lower alerts are independent (separate buttons, separate inputs).
- Reset clears ALL alerts and the input fields.
- No 'Close All Positions' button.
"""
from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState

_SOUND_BODIES: dict[str, str] = {
    'Beep': """
        var period=0.45,bDur=0.28,reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){var s=t+i*period;
          var o=ctx.createOscillator(),g=ctx.createGain();
          o.connect(g);g.connect(ctx.destination);o.type='sine';o.frequency.value=880;
          g.gain.setValueAtTime(0,s);g.gain.linearRampToValueAtTime(0.35,s+0.02);
          g.gain.exponentialRampToValueAtTime(0.001,s+bDur);o.start(s);o.stop(s+bDur+0.01);}
    """,
    'Chime': """
        var seq=[1047,1319,1568],period=0.85,noteDur=0.38,reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){var base=t+i*period;
          seq.forEach(function(f,j){var s=base+j*0.13;
            var o=ctx.createOscillator(),g=ctx.createGain();
            o.connect(g);g.connect(ctx.destination);o.type='sine';o.frequency.value=f;
            g.gain.setValueAtTime(0.22,s);g.gain.exponentialRampToValueAtTime(0.001,s+noteDur);
            o.start(s);o.stop(s+noteDur+0.02);});}
    """,
    'Alarm': """
        var period=0.22,reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){var s=t+i*period;
          var o=ctx.createOscillator(),g=ctx.createGain();
          o.connect(g);g.connect(ctx.destination);o.type='square';
          o.frequency.value=(i%2===0)?880:660;
          g.gain.setValueAtTime(0.18,s);g.gain.exponentialRampToValueAtTime(0.001,s+period*0.88);
          o.start(s);o.stop(s+period*0.9);}
    """,
    'Buzz': """
        var period=0.38,bDur=0.26,reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){var s=t+i*period;
          var o=ctx.createOscillator(),g=ctx.createGain();
          o.connect(g);g.connect(ctx.destination);o.type='sawtooth';o.frequency.value=180;
          g.gain.setValueAtTime(0.15,s);g.gain.exponentialRampToValueAtTime(0.001,s+bDur);
          o.start(s);o.stop(s+bDur+0.01);}
    """,
    'Ping': """
        var period=0.65,bDur=0.45,reps=Math.max(1,Math.floor(dur/period));
        for(var i=0;i<reps;i++){var s=t+i*period;
          var o=ctx.createOscillator(),g=ctx.createGain();
          o.connect(g);g.connect(ctx.destination);o.type='sine';o.frequency.value=1760;
          g.gain.setValueAtTime(0.4,s);g.gain.exponentialRampToValueAtTime(0.001,s+bDur);
          o.start(s);o.stop(s+bDur+0.01);}
    """,
}
_WRAPPER = """
(function(){{try{{
  var ctx=new(window.AudioContext||window.webkitAudioContext)();
  if(ctx.state==='suspended')ctx.resume();
  var t=ctx.currentTime+0.05,dur={dur};{body}
}}catch(e){{console.warn('alert sound',e);}}}}());
"""


def _play(name: str, dur: float) -> None:
    body = _SOUND_BODIES.get(name, _SOUND_BODIES['Beep'])
    ui.run_javascript(_WRAPPER.format(body=body, dur=max(0.1, dur)))


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    sound_cfg = {
        'enabled':  state.ui_prefs.get('alert_enabled', True),
        'name':     state.ui_prefs.get('alert_sound',   'Beep'),
        'duration': float(state.ui_prefs.get('alert_dur', 3.0)),
    }
    prev_triggered: set[str] = set()

    def _save_sound() -> None:
        asyncio.ensure_future(state.save_ui_prefs({
            'alert_enabled': sound_cfg['enabled'],
            'alert_sound':   sound_cfg['name'],
            'alert_dur':     sound_cfg['duration'],
        }))

    with ui.card().classes(
        'w-full p-3 bg-gray-900 border border-yellow-900/50 rounded-lg'
    ):
        ui.label('Alerts').classes('text-yellow-400 font-bold text-sm mb-2')

        # ── Upper alert (independent) ─────────────────────────────────────────
        with ui.row().classes('w-full gap-2 items-end mb-2'):
            upper_inp = (
                ui.number(label='Upper ▲', value=None, min=0, format='%.4f')
                .props('dense dark outlined').classes('flex-1')
            )

            async def set_upper() -> None:
                v = float(upper_inp.value) if upper_inp.value else None
                if not v:
                    ui.notify('Enter upper price', type='warning',
                              position='bottom-right')
                    return
                await commands.set_alert(redis, {
                    'exchange': shared.get('exchange', 'binance_futures'),
                    'symbol':   shared.get('symbol',   'BTC/USDT'),
                    'upper':    v,
                    'lower':    None,
                    'period':   'current',
                })
                upper_inp.set_value(None)
                ui.notify(f'Upper ▲{v:g} set', type='positive',
                          position='bottom-right')

            ui.button('Set ▲', on_click=set_upper).props(
                'dense unelevated'
            ).classes('bg-green-800 text-white text-xs px-3')

        # ── Lower alert (independent) ─────────────────────────────────────────
        with ui.row().classes('w-full gap-2 items-end mb-3'):
            lower_inp = (
                ui.number(label='Lower ▼', value=None, min=0, format='%.4f')
                .props('dense dark outlined').classes('flex-1')
            )

            async def set_lower() -> None:
                v = float(lower_inp.value) if lower_inp.value else None
                if not v:
                    ui.notify('Enter lower price', type='warning',
                              position='bottom-right')
                    return
                await commands.set_alert(redis, {
                    'exchange': shared.get('exchange', 'binance_futures'),
                    'symbol':   shared.get('symbol',   'BTC/USDT'),
                    'upper':    None,
                    'lower':    v,
                    'period':   'current',
                })
                lower_inp.set_value(None)
                ui.notify(f'Lower ▼{v:g} set', type='positive',
                          position='bottom-right')

            ui.button('Set ▼', on_click=set_lower).props(
                'dense unelevated'
            ).classes('bg-red-800 text-white text-xs px-3')

        # ── Sound settings ────────────────────────────────────────────────────
        with ui.row().classes('w-full items-center gap-2 mb-1 flex-wrap'):
            ui.switch('Sound', value=sound_cfg['enabled'],
                      on_change=lambda e: (
                          sound_cfg.update({'enabled': e.value}), _save_sound()
                      )).props('dense dark').classes('text-xs text-gray-400')

            ui.select(list(_SOUND_BODIES.keys()), value=sound_cfg['name'],
                      label='Sound',
                      on_change=lambda e: (
                          sound_cfg.update({'name': e.value}), _save_sound()
                      )).props('dense dark outlined').classes('w-24 text-xs')

            ui.number(label='Dur(s)', value=sound_cfg['duration'],
                      min=0.5, max=30, step=0.5,
                      on_change=lambda e: (
                          sound_cfg.update({'duration': float(e.value or 3)}),
                          _save_sound()
                      )).props('dense dark outlined').classes('w-20 text-xs')

            ui.button(icon='volume_up',
                      on_click=lambda: _play(sound_cfg['name'],
                                             sound_cfg['duration'])
                      ).props('flat round dense size=xs').classes('text-yellow-400')

        ui.separator().classes('bg-gray-700 mb-2')

        # ── Active alerts list ────────────────────────────────────────────────
        with ui.row().classes('w-full items-center justify-between mb-1'):
            ui.label('Active').classes(
                'text-gray-500 text-xs uppercase tracking-wider'
            )

            async def reset_all() -> None:
                # Clear ALL alerts and reset input fields
                await commands.clear_all_alerts(redis)
                prev_triggered.clear()
                upper_inp.set_value(None)
                lower_inp.set_value(None)
                ui.notify('All alerts cleared', type='info',
                          position='bottom-right')

            ui.button('Clear All', on_click=reset_all).props(
                'flat dense size=xs unelevated'
            ).classes('text-gray-500 text-xs')

        alerts_container = ui.column().classes('w-full gap-1')

    def update() -> None:
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
                    exch = alert.get('exchange', '').upper() \
                               .replace('_FUTURES', '-F').replace('BINANCE', 'BNF')
                    u, l = alert.get('upper'), alert.get('lower')
                    parts = [f"{exch}:{sym}"]
                    if u: parts.append(f'▲{u:g}')
                    if l: parts.append(f'▼{l:g}')
                    ui.label('  '.join(parts)).classes(
                        'text-xs text-yellow-300 font-mono'
                    )

                    async def del_alert(aid=alert.get('id', '')) -> None:
                        await commands.delete_alert(redis, aid)

                    ui.button(icon='close', on_click=del_alert).props(
                        'flat round dense size=xs'
                    ).classes('text-gray-500')

    return {'update': update}

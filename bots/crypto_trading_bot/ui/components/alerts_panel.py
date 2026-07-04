"""
Alerts panel.
- Upper and lower alerts are independent (separate buttons, separate inputs).
- Edit button on each active alert allows in-place modification.
- Conditional order section: set an alert that also places a limit/stop-market
  entry order when the candle closes or LTP crosses a level.
- Reset clears ALL alerts and the input fields.
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
            o.start(s);o.stop(s+noteDur+0.02);}); }
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
    period_ref  = {'v': 'current'}   # shared between upper and lower set functions
    co_period   = {'v': '1m'}        # conditional order period
    editing: dict[str, dict] = {}    # alert_id -> {upper, lower, period} being edited

    def _save_sound() -> None:
        asyncio.ensure_future(state.save_ui_prefs({
            'alert_enabled': sound_cfg['enabled'],
            'alert_sound':   sound_cfg['name'],
            'alert_dur':     sound_cfg['duration'],
        }))

    with ui.card().classes('w-full p-3 bg-gray-900 border border-yellow-900/50 rounded-lg'):
        ui.label('Alerts').classes('text-yellow-400 font-bold text-sm mb-2')

        # Period selector (shared for both upper and lower)
        with ui.row().classes('w-full mb-2'):
            ui.select(
                {'current': 'Live Price', '1m': '1m Candle Close', '5m': '5m Candle Close'},
                value='current', label='Fire on',
                on_change=lambda e: period_ref.update({'v': e.value}),
            ).props('dense dark outlined').classes('w-full')

        # Upper alert
        with ui.row().classes('w-full gap-2 items-end mb-2'):
            upper_inp = (ui.number(label='Upper', value=None, min=0, format='%.4f')
                         .props('dense dark outlined').classes('flex-1'))

            async def set_upper() -> None:
                v = float(upper_inp.value) if upper_inp.value else None
                if not v:
                    ui.notify('Enter upper price', type='warning', position='bottom-right')
                    return
                await commands.set_alert(redis, {
                    'exchange': shared.get('exchange', 'binance_futures'),
                    'symbol':   shared.get('symbol',   'BTC/USDT'),
                    'upper': v, 'lower': None,
                    'period': period_ref['v'],
                })
                upper_inp.set_value(None)
                ui.notify(f"Upper {v:g} ({period_ref['v']}) set",
                          type='positive', position='bottom-right')

            ui.button('Set', on_click=set_upper).props('dense unelevated').classes(
                'bg-green-800 text-white text-xs px-3'
            )

        # Lower alert
        with ui.row().classes('w-full gap-2 items-end mb-2'):
            lower_inp = (ui.number(label='Lower', value=None, min=0, format='%.4f')
                         .props('dense dark outlined').classes('flex-1'))

            async def set_lower() -> None:
                v = float(lower_inp.value) if lower_inp.value else None
                if not v:
                    ui.notify('Enter lower price', type='warning', position='bottom-right')
                    return
                await commands.set_alert(redis, {
                    'exchange': shared.get('exchange', 'binance_futures'),
                    'symbol':   shared.get('symbol',   'BTC/USDT'),
                    'upper': None, 'lower': v,
                    'period': period_ref['v'],
                })
                lower_inp.set_value(None)
                ui.notify(f"Lower {v:g} ({period_ref['v']}) set",
                          type='positive', position='bottom-right')

            ui.button('Set', on_click=set_lower).props('dense unelevated').classes(
                'bg-red-800 text-white text-xs px-3'
            )

        # Sound settings
        with ui.row().classes('w-full items-center gap-2 mb-1 flex-wrap'):
            ui.switch('Sound', value=sound_cfg['enabled'],
                      on_change=lambda e: (sound_cfg.update({'enabled': e.value}), _save_sound())
                      ).props('dense dark').classes('text-xs text-gray-400')
            ui.select(list(_SOUND_BODIES.keys()), value=sound_cfg['name'], label='Sound',
                      on_change=lambda e: (sound_cfg.update({'name': e.value}), _save_sound())
                      ).props('dense dark outlined').classes('w-24 text-xs')
            ui.number(label='Dur(s)', value=sound_cfg['duration'], min=0.5, max=30, step=0.5,
                      on_change=lambda e: (sound_cfg.update({'duration': float(e.value or 3)}), _save_sound())
                      ).props('dense dark outlined').classes('w-20 text-xs')
            ui.button(icon='volume_up',
                      on_click=lambda: _play(sound_cfg['name'], sound_cfg['duration'])
                      ).props('flat round dense size=xs').classes('text-yellow-400')

        ui.separator().classes('bg-gray-700 mb-2')

        with ui.row().classes('w-full items-center justify-between mb-1'):
            ui.label('Active').classes('text-gray-500 text-xs uppercase tracking-wider')

            async def reset_all() -> None:
                for alert in state.alerts:
                    aid = alert.get('id', '')
                    if aid:
                        prev_triggered.add(aid)
                await commands.clear_all_alerts(redis)
                upper_inp.set_value(None)
                lower_inp.set_value(None)
                ui.notify('All alerts cleared', type='info', position='bottom-right')

            ui.button('Clear All', on_click=reset_all).props(
                'flat dense size=xs unelevated'
            ).classes('text-gray-500 text-xs')

        alerts_container = ui.column().classes('w-full gap-1')

    # ── Active alert list renderer ──────────────────────────────────────────────────────

    def _redraw() -> None:
        alerts_container.clear()
        with alerts_container:
            active = [a for a in state.alerts if not a.get('triggered')]
            if not active:
                ui.label('No active alerts').classes('text-gray-600 text-xs')
                return

            for alert in active:
                aid  = alert.get('id', '')
                sym  = alert.get('symbol', '')
                exch = (alert.get('exchange', '').upper()
                        .replace('_FUTURES', '-F').replace('BINANCE', 'BNF'))
                per  = alert.get('period', 'current')
                u, l = alert.get('upper'), alert.get('lower')
                has_order = bool(alert.get('order_side'))

                # Label
                parts = [f"{exch}:{sym}"]
                if u: parts.append(f'Above {u:g}')
                if l: parts.append(f'Below {l:g}')
                if per != 'current': parts.append(f'[{per}]')
                if has_order:
                    ep = alert.get('order_entry_price')
                    parts.append(
                        f"-> {alert.get('order_side','').upper()} "
                        f"{alert.get('order_type','')}"
                        f"{f' @{ep:g}' if ep else ''}"
                    )

                with ui.row().classes('w-full items-start gap-1 py-0.5 flex-wrap'):
                    ui.label('  '.join(parts)).classes(
                        'text-xs font-mono flex-1 '
                        + ('text-blue-300' if has_order else 'text-yellow-300')
                    )

                    # ── Edit inline form ───────────────────────────────────
                    if editing.get(aid):
                        ef = editing[aid]

                        def _close_edit(a=aid) -> None:
                            editing.pop(a, None)
                            _redraw()

                        with ui.column().classes('w-full gap-1 mt-1 pl-2 border-l border-gray-600'):
                            with ui.row().classes('gap-2 items-end flex-wrap'):
                                if ef.get('upper') is not None:
                                    eu = ui.number(
                                        label='Upper', value=ef['upper'],
                                        min=0, format='%g',
                                        on_change=lambda e, ef=ef:
                                            ef.update({'upper': float(e.value or 0) or None}),
                                    ).props('dense dark outlined').classes('w-28')
                                if ef.get('lower') is not None:
                                    el = ui.number(
                                        label='Lower', value=ef['lower'],
                                        min=0, format='%g',
                                        on_change=lambda e, ef=ef:
                                            ef.update({'lower': float(e.value or 0) or None}),
                                    ).props('dense dark outlined').classes('w-28')
                                ui.select(
                                    {'current': 'Live', '1m': '1m', '5m': '5m'},
                                    value=ef.get('period', 'current'),
                                    on_change=lambda e, ef=ef:
                                        ef.update({'period': e.value}),
                                ).props('dense dark outlined').classes('w-24')

                            with ui.row().classes('gap-1 mt-0.5'):
                                async def confirm_edit(a=aid, ef=ef) -> None:
                                    # Delete old + create updated alert
                                    await commands.delete_alert(redis, a)
                                    await commands.set_alert(redis, {
                                        'exchange': alert.get('exchange', ''),
                                        'symbol':   alert.get('symbol',   ''),
                                        'upper':    ef.get('upper'),
                                        'lower':    ef.get('lower'),
                                        'period':   ef.get('period', 'current'),
                                        'order_side':        alert.get('order_side'),
                                        'order_type':        alert.get('order_type'),
                                        'order_entry_price': alert.get('order_entry_price'),
                                        'order_stop':        alert.get('order_stop'),
                                        'order_target':      alert.get('order_target'),
                                        'order_risk_pct':    alert.get('order_risk_pct'),
                                    })
                                    editing.pop(a, None)
                                    ui.notify('Alert updated', type='positive',
                                              position='bottom-right')

                                ui.button('Save', on_click=confirm_edit).props(
                                    'dense unelevated size=xs'
                                ).classes('bg-blue-700 text-white')
                                ui.button('Cancel', on_click=_close_edit).props(
                                    'flat dense size=xs'
                                ).classes('text-gray-500')
                    else:
                        # Edit / Delete buttons
                        def open_edit(a=aid, al=alert) -> None:
                            editing[a] = {
                                'upper':  al.get('upper'),
                                'lower':  al.get('lower'),
                                'period': al.get('period', 'current'),
                            }
                            _redraw()

                        ui.button(icon='edit', on_click=open_edit).props(
                            'flat round dense size=xs'
                        ).classes('text-blue-400')

                        async def del_alert(a=aid) -> None:
                            editing.pop(a, None)
                            prev_triggered.discard(a)
                            await commands.delete_alert(redis, a)

                        ui.button(icon='close', on_click=del_alert).props(
                            'flat round dense size=xs'
                        ).classes('text-gray-500')

    def update() -> None:
        # Keep prev_triggered in sync
        current_ids = {a.get('id', '') for a in state.alerts}
        prev_triggered.intersection_update(current_ids)

        # Sound on new trigger
        if sound_cfg['enabled']:
            for alert in state.alerts:
                aid = alert.get('id', '')
                if alert.get('triggered') and aid and aid not in prev_triggered:
                    prev_triggered.add(aid)
                    _play(sound_cfg['name'], sound_cfg['duration'])

        _redraw()

    return {'update': update}

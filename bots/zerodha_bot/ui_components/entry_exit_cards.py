"""
Per-side entry and exit cards: the unified Open Short/Long entry card, Auto Close
(Profit/Loss), Premium Exit, Index Exit, and the generic Global control card. These are the
largest, most logic-heavy cards in the app (draft-and-commit fields, instant-fire warnings,
mode-aware coloring).
"""
from nicegui import ui
from config import params, UI_OPTS, shared_state, get_eval_price
from datetime import datetime

from ._shared import _side_colors, _bind_card_colors, _bind_btn_color, _sync_draft_from_params, _side_is_red, _log_alert_action, _num_stepper
from ._warnings import (
    _confirm_warning, _unified_instant_fire_warning, _premium_instant_fire_warning,
    _index_instant_fire_warning, _pct_away_warning, _no_position_warning,
)
from . import _cancel_engine_job_if_any

def entry_card(side, label, mode_key, input_key, on_open=None, on_close=None):
    color_class = 'bg-red-50 border-red-200' if side == 'Call' else 'bg-green-50 border-green-200'
    btn_color = 'red' if side == 'Call' else 'green'
    with ui.card().classes(f'w-full p-3 gap-2 {color_class} border shadow-sm rounded-xl'):
        ui.label(label).classes('font-bold text-gray-700 text-sm')
        with ui.row().classes('items-center'):
            ui.radio(UI_OPTS['entry_modes'], value=params[mode_key]).bind_value(params, mode_key).props('inline dense')
        ui.input('Strike').bind_value(params, input_key).props('outlined dense bg-color=white').classes('w-full')
        with ui.row().classes('w-full gap-2'):
            ui.button(f'Open', color=btn_color, on_click=on_open).classes('grow rounded-lg shadow-sm')
            ui.button(f'Close', on_click=on_close).classes('grow rounded-lg shadow-sm bg-gray-200 text-gray-800 hover:bg-gray-300')


def _reset_unified_card_defaults(prefix):
    """Fully resets a unified Open Short/Long card (Cancel button) back to defaults:
    disarms, clears the trigger price, and restores order type/strike/qty/fire-on/stop/target.

    Also cancels any still-pending Enter via Stop job for this side (see
    _cancel_engine_job_if_any): a job only lives for the few seconds between deferral and
    hand-off, but without this a job in that window could later hand off and silently
    re-arm an order the person just cancelled."""
    side = 'Call' if prefix == 'call' else 'Put'
    _cancel_engine_job_if_any('entry', side)
    params[f'{prefix}_armed'] = False
    params[f'{prefix}_armed_at'] = None
    params[f'{prefix}_order_type'] = 'Market'
    params[f'{prefix}_trigger_price'] = 0
    params[f'{prefix}_strike_offset'] = 1
    params[f'{prefix}_qty'] = 4
    params[f'{prefix}_fire_on'] = 'Live'
    params[f'{prefix}_new_stop'] = ''
    params[f'{prefix}_new_target'] = ''


def _unified_card_title(side, buy_mode):
    """Card title reflects the CURRENT mode's real semantics, not the sell-mode-only 'Short'/
    'Long' framing. In Sell Mode, Call=sell CE (short-biased card), Put=sell PE (long-biased
    card) -- unchanged wording. In Buy Mode, Call=buy CE (bullish) and Put=buy PE (bearish),
    which is the opposite plain-English framing from Sell Mode's Put card, so the label must
    say so explicitly or a Buy Mode Put position looks like a mislabeled 'short'."""
    if not buy_mode:
        return 'Open Short' if side == 'Call' else 'Open Long'
    return 'Buy Call (Bullish)' if side == 'Call' else 'Buy Put (Bearish)'


def _unified_card_colors(side, buy_mode):
    """Backwards-compatible wrapper around _side_colors (100-weight), kept so any external
    reference to this exact name still works."""
    cls, btn = _side_colors(side, buy_mode, weight='100')
    return cls, btn


def unified_entry_card(side, prefix, on_fire_market=None, on_close=None):
    """Unified Open Short/Long card: index-based entry with order type (Market/Limit/
    Stop-Market), trigger price, strike offset (0=ATM, 1=ITM, -1=OTM, with -/+ steppers),
    optional stop/target, a fire-on timeframe, and its own qty (also with -/+ steppers).
    Market fires immediately via on_fire_market. Limit/Stop-Market arm the trade; index
    conditions are then checked and fired by LogicEngine._check_unified_open in
    logic_engine.py.

    Title, trigger-direction hint, AND card colors are all reactive to options_buy_mode (see
    _unified_card_title / _side_colors) so the card never uses Sell Mode's wording or colors
    while Buy Mode is active: in Buy Mode, Buy Call is green (bullish) and Buy Put is red
    (bearish) -- the opposite of Sell Mode's fixed Call=red/Put=green scheme.

    Trigger Price is bound to a local staged 'draft' dict, NOT directly to params: while the
    card is armed, LogicEngine._check_unified_open polls params[trig_key] every tick (every
    tick if fire_on='Live', the default) to decide whether to fire an entry. A direct live
    bind would let an in-progress edit fire an entry at an unintended intermediate price.
    Editing the trigger price now has zero live effect; the new value is only committed into
    params[trig_key] when Arm is clicked (which also re-arms with the fresh value if the card
    was already armed). _sync_draft_from_params keeps the field in sync with EXTERNAL resets
    (e.g. Close -> auto_run.clear_leg_fields() zeroing params[trig_key]) so the field visibly
    clears after closing, without reintroducing the mid-typing bug -- this ALSO picks up the
    stop_via_candle_engine's hand-off (it writes a fresh trigger_price directly into params
    once armed), so the card automatically shows the NEW live order the moment hand-off
    happens, with no separate 'deferred order' display needed. Strike offset and Qty are NOT
    staged this way since they're only read once, at the moment an order actually fires --
    never polled live against a threshold.

    idx_ltp used for the instant-fire sanity check below is Futures-Mode-aware
    (config.get_eval_price): the near-month future's LTP when params['futures_mode'] is on
    and resolved for this index, otherwise identical to spot -- matching exactly what
    LogicEngine._check_unified_open itself evaluates against, so this warning is never wrong
    about whether an order will actually fire immediately."""
    order_key = f'{prefix}_order_type'; trig_key = f'{prefix}_trigger_price'
    strike_key = f'{prefix}_strike_offset'; qty_key = f'{prefix}_qty'
    fire_key = f'{prefix}_fire_on'; armed_key = f'{prefix}_armed'
    stop_key = f'{prefix}_new_stop'; target_key = f'{prefix}_new_target'

    init_color_class, init_btn_color = _side_colors(side, params.get('options_buy_mode', False), weight='100')
    draft = {'trigger': params.get(trig_key, 0)}
    _sync_draft_from_params(draft, 'trigger', trig_key)

    with ui.card().classes(f'w-full p-4 gap-2 {init_color_class} border shadow-md rounded-xl') as card:
        _bind_card_colors(card, side, lambda is_red: f"w-full p-4 gap-2 {'bg-red-100 border-red-300' if is_red else 'bg-green-100 border-green-300'} border shadow-md rounded-xl")

        title_lbl = ui.label(_unified_card_title(side, params.get('options_buy_mode', False))).classes('font-bold text-sm uppercase text-gray-800')
        title_lbl.bind_text_from(params, 'options_buy_mode', backward=lambda v, s=side: _unified_card_title(s, v))

        hint_lbl = ui.label().classes('text-[10px] text-gray-500 mb-1')
        def _hint(buy_mode, s=side):
            if not buy_mode:
                return 'Stop-Market fires on breakout confirmation; Limit fires on a better price.'
            direction = 'rises above (breakout)' if s == 'Call' else 'falls below (breakdown)'
            return f'Buy Mode: Stop-Market fires when index {direction} trigger.'
        hint_lbl.set_text(_hint(params.get('options_buy_mode', False)))
        hint_lbl.bind_text_from(params, 'options_buy_mode', backward=_hint)

        with ui.row().classes('w-full justify-start'):
            ui.radio(UI_OPTS['order_types'], value=params[order_key]).bind_value(params, order_key).props('inline dense')

        with ui.row().classes('w-full gap-2'):
            # NOTE: intentionally NOT disabled for Market (previously used bind_enabled_from,
            # which could leave a typed value uncommitted after a disable->enable toggle on some
            # NiceGUI/Quasar versions). Always-editable avoids that class of binding bug; the
            # value is simply ignored by the firing logic when order type is Market. Bound to
            # 'draft', not params directly -- see docstring above.
            ui.input('Trigger Price').bind_value(draft, 'trigger').props('outlined dense bg-color=white').classes('grow')
            _num_stepper(params, strike_key, step=1, label='Strike (0=ATM,1=ITM,-1=OTM)')

        with ui.row().classes('w-full gap-2'):
            _num_stepper(params, qty_key, step=1, label='Qty (Lots)')
            ui.select(UI_OPTS['fire_on_opts'], value=params[fire_key]).bind_value(params, fire_key).props('outlined dense bg-color=white').classes('grow')

        with ui.row().classes('w-full gap-2'):
            ui.input('Stop (optional)').bind_value(params, stop_key).props('outlined dense bg-color=white').classes('grow')
            ui.input('Target (optional)').bind_value(params, target_key).props('outlined dense bg-color=white').classes('grow')

        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-green-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, armed_key)

        def fire_or_arm():
            if params[order_key] == 'Market':
                if on_fire_market: on_fire_market()
                return

            order_type = params[order_key]
            try: trigger_price = float(draft['trigger'])
            except (ValueError, TypeError):
                ui.notify(f"Invalid {side} Trigger Price", type='negative'); return
            idx_ltp = get_eval_price(params['trading_index'])
            buy_mode = params.get('options_buy_mode', False)

            def _do_arm():
                # Commit the staged trigger price into params NOW (atomically, not live while
                # typing), then stamp the arm time so the timing-boundary check in
                # LogicEngine._check_unified_open requires the NEXT candle close after this
                # moment, not any boundary crossing (fixes: arming during a boundary-minute
                # firing on the very next tick instead of waiting a full period).
                params[trig_key] = trigger_price
                params[f'{prefix}_armed_at'] = datetime.now()
                params[armed_key] = True
                status.set_text(f"ARMED: {order_type} @ {trigger_price} ({params[fire_key]})")
                ui.notify(f"{_unified_card_title(side, params.get('options_buy_mode', False))} ARMED", type='positive')
                _log_alert_action(f"⚙️ {side} Entry ARMED: {order_type} @ {trigger_price} ({params[fire_key]})")

            warning = _unified_instant_fire_warning(side, order_type, buy_mode, idx_ltp, trigger_price)
            if warning:
                _confirm_warning(warning, _do_arm)
            else:
                _do_arm()

        def cancel():
            # Full reset: trigger price + every other field back to default (not just disarm).
            _reset_unified_card_defaults(prefix)
            draft['trigger'] = 0
            ui.notify(f"{_unified_card_title(side, params.get('options_buy_mode', False))} Cancelled & Reset", type='info')

        with ui.row().classes('w-full gap-2'):
            fire_btn = ui.button('Open Now', color=init_btn_color, on_click=fire_or_arm).classes('grow h-8 text-xs rounded-lg shadow-sm')
            fire_btn.bind_text_from(params, order_key, backward=lambda v: 'Open Now' if v == 'Market' else 'Arm')
            _bind_btn_color(fire_btn, side)
            ui.button('Cancel', on_click=cancel).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')
            ui.button('Close', on_click=on_close).classes('grow h-8 text-xs rounded-lg bg-gray-300 text-gray-800 hover:bg-gray-400')


def auto_close_card(side, target_val_key, target_active_key, stop_val_key, stop_active_key):
    """Card background is mode-aware via _side_colors/_bind_card_colors: Sell Mode unchanged
    (Call=red, Put=green); Buy Mode flips (Buy Call=green, Buy Put=red), matching every other
    per-side card in the app.

    Profit/Loss inputs are bound to a local staged 'draft' dict, NOT directly to params.
    Typing is free-form and has zero live effect; the new value is only written into
    params[target_val_key]/params[stop_val_key] -- and the active flag set -- atomically when
    SET is clicked. logic_engine._check_exits polls these exact keys every tick with NO
    period gating at all, so a direct live bind previously let an in-progress edit (e.g.
    typing "10000" over "12000") pass through an intermediate value like "1000" that the live
    PnL already exceeded, closing the position mid-edit. _sync_draft_from_params keeps the
    field in sync with EXTERNAL resets (e.g. Close -> auto_run.clear_leg_fields() zeroing
    these params) so the field visibly clears after closing, without reintroducing that bug.

    SET also warns (via _no_position_warning, same 'Proceed Anyway' dialog used elsewhere) if
    this side has no open position right now -- likely means the person meant the other
    panel. Both SET and RESET explicitly log the exact value into the shared Trade Event Log
    (via _log_alert_action), so a Profit/Loss threshold that was armed can always be confirmed
    after the fact."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    draft = {'target': params.get(target_val_key, 0), 'stop': params.get(stop_val_key, 0)}
    _sync_draft_from_params(draft, 'target', target_val_key)
    _sync_draft_from_params(draft, 'stop', stop_val_key)
    with ui.card().classes(f'w-full p-3 gap-2 {init_color_class} border shadow-sm rounded-xl') as card:
        _bind_card_colors(card, side, lambda is_red: f"w-full p-3 gap-2 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border shadow-sm rounded-xl")

        ui.label(f'Auto Close {side}').classes('font-bold text-xs uppercase text-gray-500 mb-1')

        with ui.row().classes('w-full items-center gap-1'):
            ui.label('Profit').classes('text-[10px] w-8 font-bold text-green-700')
            ui.input().bind_value(draft, 'target').props('outlined dense prefix="₹" bg-color=white').classes('grow')
            st_tgt = ui.label('ON').classes('text-[9px] text-white bg-green-600 rounded px-1 hidden')
            st_tgt.bind_visibility_from(params, target_active_key)
            def _do_set_tgt(value):
                params[target_val_key] = value; params[target_active_key] = True
                ui.notify(f"{side} Profit Set", type='positive')
                _log_alert_action(f"⚙️ {side} Profit SET: ₹{value:.0f}")
            def set_tgt():
                try:
                    value = float(draft['target'])
                except (ValueError, TypeError):
                    ui.notify(f"Invalid {side} Profit Value", type='negative'); return
                warning = _no_position_warning(side, params.get('options_buy_mode', False))
                if warning:
                    _confirm_warning(warning, lambda v=value: _do_set_tgt(v))
                else:
                    _do_set_tgt(value)
            def rst_tgt():
                params[target_active_key] = False; params[target_val_key] = 0; draft['target'] = 0
                ui.notify(f"{side} Profit Reset", type='info')
                _log_alert_action(f"⚙️ {side} Profit RESET")
            ui.button('SET', on_click=set_tgt, color='green-8').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')
            ui.button('RESET', on_click=rst_tgt, color='grey').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')

        with ui.row().classes('w-full items-center gap-1'):
            ui.label('Loss').classes('text-[10px] w-8 font-bold text-red-700')
            ui.input().bind_value(draft, 'stop').props('outlined dense prefix="₹" bg-color=white').classes('grow')
            st_stp = ui.label('ON').classes('text-[9px] text-white bg-red-600 rounded px-1 hidden')
            st_stp.bind_visibility_from(params, stop_active_key)
            def _do_set_stp(value):
                params[stop_val_key] = value; params[stop_active_key] = True
                ui.notify(f"{side} Loss Set", type='positive')
                _log_alert_action(f"⚙️ {side} Loss SET: ₹{value:.0f}")
            def set_stp():
                try:
                    value = float(draft['stop'])
                except (ValueError, TypeError):
                    ui.notify(f"Invalid {side} Loss Value", type='negative'); return
                warning = _no_position_warning(side, params.get('options_buy_mode', False))
                if warning:
                    _confirm_warning(warning, lambda v=value: _do_set_stp(v))
                else:
                    _do_set_stp(value)
            def rst_stp():
                params[stop_active_key] = False; params[stop_val_key] = 0; draft['stop'] = 0
                ui.notify(f"{side} Loss Reset", type='info')
                _log_alert_action(f"⚙️ {side} Loss RESET")
            ui.button('SET', on_click=set_stp, color='red-8').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')
            ui.button('RESET', on_click=rst_stp, color='grey').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')


def open_logic_card(title, side, mode_key, amt_key, strike_key, active_key):
    color_class = 'bg-red-100 border-red-300' if side == 'Call' else 'bg-green-100 border-green-300'
    btn_color = 'red' if side == 'Call' else 'green'
    with ui.card().classes(f'w-full p-3 gap-2 {color_class} border shadow-md rounded-xl'):
        ui.label(title).classes('font-bold text-sm uppercase text-gray-800')
        with ui.row().classes('w-full justify-start'):
            ui.radio(UI_OPTS['open_modes'], value=params[mode_key]).bind_value(params, mode_key).props('inline dense')
        with ui.row().classes('w-full gap-2'):
            ui.input('Amount').bind_value(params, amt_key).props('outlined dense bg-color=white').classes('grow')
            ui.input('Strike').bind_value(params, strike_key).props('outlined dense bg-color=white').classes('grow')
        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-green-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, active_key)
        def activate():
            params[active_key] = True; msg = f"ACTIVE: {params[mode_key]} < {params[amt_key]}" if side=='Call' else f"ACTIVE: {params[mode_key]} > {params[amt_key]}"
            status.set_text(msg); ui.notify(f"{title} ACTIVATED", type='positive')
            _log_alert_action(f"⚙️ {title} ACTIVATED: {params[mode_key]} {'<' if side == 'Call' else '>'} {params[amt_key]}")
        def reset():
            params[active_key] = False; params[amt_key] = 0; params[strike_key] = 0
            ui.notify(f"{title} RESET", type='info')
            _log_alert_action(f"⚙️ {title} RESET")
        with ui.row().classes('w-full gap-2'):
            ui.button('Activate', color=btn_color, on_click=activate).classes('grow h-8 text-xs rounded-lg shadow-sm')
            ui.button('Reset', on_click=reset).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')


def global_control_card(label, value_key, active_key):
    """Value input is bound to a local staged 'draft' dict, NOT directly to params -- see
    auto_close_card's docstring for why (logic_engine._check_global_limits polls this key
    every tick with no period gating). _sync_draft_from_params keeps the field in sync with
    EXTERNAL resets without reintroducing the mid-typing bug -- see that helper's docstring.

    No _no_position_warning check here (unlike the per-side cards): this is global, applying
    to combined Call+Put PnL rather than one specific side's position.

    Set/Reset explicitly log the actual numeric value into the shared Trade Event Log (via
    _log_alert_action, the same generic activity-log writer alerts use) -- previously the
    only trace of a Set/Reset action was the generic 'MANUAL ACTION: {label} SET' line
    auto_run.py's global ui.notify interceptor produces, which does not include the value
    typed in, making it impossible to later confirm exactly what threshold was armed at any
    given time (see: diagnosing why a Global Stop fired earlier than expected)."""
    draft = {'value': params.get(value_key, 0)}
    _sync_draft_from_params(draft, 'value', value_key)
    with ui.card().classes('w-full p-3 gap-2 bg-gray-50 border border-gray-200 shadow-sm rounded-xl'):
        ui.label(label).classes('font-bold text-sm text-gray-700')
        ui.input().bind_value(draft, 'value').props('outlined dense bg-color=white prefix="₹"').classes('w-full')
        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-blue-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, active_key)
        def activate():
            try:
                value = float(draft['value'])
            except (ValueError, TypeError):
                ui.notify(f"Invalid {label} Value", type='negative'); return
            params[value_key] = value; params[active_key] = True
            status.set_text(f"ACTIVE: {value}")
            ui.notify(f"{label} SET", type='positive')
            _log_alert_action(f"⚙️ {label} SET: ₹{value:.0f}")
        def reset():
            params[active_key] = False; params[value_key] = 0; draft['value'] = 0
            ui.notify(f"{label} RESET", type='info')
            _log_alert_action(f"⚙️ {label} RESET")
        with ui.row().classes('w-full gap-2'):
            ui.button('Set', color='blue-7', on_click=activate).classes('grow h-8 text-xs rounded-lg')
            ui.button('Reset', on_click=reset).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')


def index_exit_component(side, label, time_key, value_key, active_key):
    """Card background is mode-aware via _side_colors/_bind_card_colors: Sell Mode unchanged
    (Call=red, Put=green); Buy Mode flips (Buy Call=green, Buy Put=red), matching every other
    per-side card in the app.

    Value input is bound to a local staged 'draft' dict, NOT directly to params -- typing has
    zero live effect; the new value is only written into params[value_key] (and the active
    flag set) atomically when Set is clicked/confirmed. logic_engine._check_exits polls this
    exact key every tick whenever the period is 'Current' (the default), so a direct live
    bind previously let an in-progress edit trigger a close on an intermediate keystroke
    value. _sync_draft_from_params keeps the field in sync with EXTERNAL resets (e.g. Close
    -> auto_run.clear_leg_fields(), or the Open Positions quick control sharing this same
    params_key) so the field visibly clears/updates without reintroducing that bug -- this
    ALSO picks up the stop_via_candle_engine's hand-off for the Stop card (it writes a fresh
    value/'Current' period directly into params once armed), so this card automatically shows
    the NEW live stop the moment hand-off happens. The period radio itself is unaffected (a
    click is already atomic, no typing risk) and stays bound directly to params.

    Set also warns (via _no_position_warning) if this side has no open position right now --
    checked alongside the existing instant-fire/pct-away warnings (index price is always
    available regardless of whether a position is open, so those remain independently
    useful). Reset ALSO cancels any still-pending Enter via Stop job for the Stop card only
    (see _cancel_engine_job_if_any) -- Target is never deferred, so nothing to cancel there.
    Both Set and Reset explicitly log the exact value into the shared Trade Event Log.

    idx_ltp used for the instant-fire/pct-away sanity checks below is Futures-Mode-aware
    (config.get_eval_price): the near-month future's LTP when params['futures_mode'] is on
    and resolved for this index, otherwise identical to spot -- matching exactly what
    LogicEngine._check_exits's INDEX EXITS branch itself evaluates against."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    draft = {'value': params.get(value_key, 0)}
    _sync_draft_from_params(draft, 'value', value_key)
    with ui.card().classes(f'w-full p-3 gap-1 {init_color_class} border rounded-lg') as card:
        _bind_card_colors(card, side, lambda is_red: f"w-full p-3 gap-1 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border rounded-lg")

        ui.label(label).classes('font-bold text-xs text-gray-600')
        with ui.row().classes('items-center justify-between w-full'):
            ui.input().bind_value(draft, 'value').props('outlined dense bg-color=white').classes('w-24')
            ui.radio(UI_OPTS['index_times'], value=params[time_key]).bind_value(params, time_key).props('inline dense scale=0.8')
        status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
        status.bind_visibility_from(params, active_key)
        def _do_activate(value):
            if label == 'Stop':
                _cancel_engine_job_if_any('index_stop', side)
            params[value_key] = value; params[active_key] = True
            status.set_text(f"ON: {value}")
            ui.notify(f"{side} Index {label} SET", type='positive')
            _log_alert_action(f"⚙️ {side} Index {label} SET: {value}")
        def activate():
            try:
                value = float(draft['value'])
            except (ValueError, TypeError):
                ui.notify(f"Invalid {side} Index {label} Value", type='negative'); return
            idx_ltp = get_eval_price(params['trading_index'])
            buy_mode = params.get('options_buy_mode', False)
            is_stop = (label == 'Stop')
            warnings = []
            w0 = _no_position_warning(side, buy_mode)
            if w0: warnings.append(w0)
            w1 = _index_instant_fire_warning(is_stop, side, buy_mode, value, idx_ltp)
            if w1: warnings.append(w1)
            w2 = _pct_away_warning(value, idx_ltp, label)
            if w2: warnings.append(w2)
            if warnings:
                _confirm_warning('\n\n'.join(warnings), lambda v=value: _do_activate(v))
            else:
                _do_activate(value)
        def reset():
            if label == 'Stop':
                _cancel_engine_job_if_any('index_stop', side)
            params[active_key] = False; params[value_key] = 0; draft['value'] = 0
            ui.notify(f"{side} Index {label} RESET", type='info')
            _log_alert_action(f"⚙️ {side} Index {label} RESET")
        with ui.row().classes('w-full gap-1 mt-1'):
            ui.button('Set', color='black', on_click=activate).props('outline').classes('grow h-6 text-[10px] rounded')
            ui.button('Reset', on_click=reset).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')


def premium_exit_card(side):
    """Exit based on the live LTP of the main option leg. Stop/Target sub-labels and helper
    text auto-flip based on options_buy_mode: in Sell Mode (unchanged) Stop = premium rises
    (loss on short), Target = premium falls (profit on short); in Buy Mode these invert since
    a long position profits as premium rises. Card + both sub-card backgrounds and the header
    label color are also mode-aware via _side_colors/_bind_card_colors (Sell Mode unchanged:
    Call=red, Put=green; Buy Mode flips: Buy Call=green, Buy Put=red).

    Stop/Target value inputs are bound to a local staged 'draft' dict, NOT directly to
    params -- same fix as index_exit_component/auto_close_card, for the same reason
    (logic_engine._check_exits' PREMIUM EXITS branch polls these keys every tick whenever the
    period is 'Current'). _sync_draft_from_params keeps both fields in sync with EXTERNAL
    resets (e.g. Close -> auto_run.clear_leg_fields()) without reintroducing that bug -- this
    ALSO picks up the stop_via_candle_engine's hand-off for the Stop side.

    The instant-fire/pct-away warnings need a live trade to read its current premium from, so
    they're only computed when one exists; when this side has NO open trade, Set instead
    warns via _no_position_warning (same 'Proceed Anyway' dialog) -- previously that case
    silently set the value with no warning at all. The Stop sub-card's Reset ALSO cancels any
    still-pending Enter via Stop job (see _cancel_engine_job_if_any) -- Target is never
    deferred, so nothing to cancel on that side. Both Set and Reset (both sub-cards)
    explicitly log the exact value into the shared Trade Event Log.

    NOTE: this card's warnings read the option's own premium (trade['main']['current_price']),
    never the index, so they are already correct and unaffected by Futures Mode -- no change
    needed here."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    s = side.lower()
    draft = {'stop': params.get(f'{s}_prem_stop_val', 0), 'target': params.get(f'{s}_prem_target_val', 0)}
    _sync_draft_from_params(draft, 'stop', f'{s}_prem_stop_val')
    _sync_draft_from_params(draft, 'target', f'{s}_prem_target_val')

    def _outer_cls(is_red):
        return f"w-full p-3 gap-2 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border shadow-sm rounded-xl"
    def _sub_cls(is_red):
        return f"w-full p-2 gap-1 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border rounded-lg"
    def _label_cls(is_red):
        return f"font-bold text-xs uppercase {'text-red-800' if is_red else 'text-green-800'}"

    init_label_color = 'text-red-800' if _side_is_red(side, params.get('options_buy_mode', False)) else 'text-green-800'

    with ui.card().classes(f'w-full p-3 gap-2 {init_color_class} border shadow-sm rounded-xl') as card:
        _bind_card_colors(card, side, _outer_cls)

        header_lbl = ui.label(f'{side} Exit based on Option Premium').classes(f'font-bold text-xs uppercase {init_label_color}')
        def _apply_label_color(buy_mode, lbl=header_lbl, sd=side):
            is_red = _side_is_red(sd, buy_mode)
            lbl.classes(replace=_label_cls(is_red))
            return ''
        _label_hook = ui.label('').classes('hidden')
        _label_hook.bind_text_from(params, 'options_buy_mode', backward=_apply_label_color)

        hint = ui.label().classes('text-[9px] text-gray-500 -mt-1')
        def _prem_hint(buy_mode):
            if not buy_mode:
                return 'Stop: premium rises above value. Target: premium falls below value.'
            return 'Buy Mode: Stop: premium falls below value. Target: premium rises above value.'
        hint.set_text(_prem_hint(params.get('options_buy_mode', False)))
        hint.bind_text_from(params, 'options_buy_mode', backward=_prem_hint)

        with ui.row().classes('w-full gap-2'):
            # Stop sub-card
            with ui.card().classes(f'w-full p-2 gap-1 {init_color_class} border rounded-lg') as stop_card:
                _bind_card_colors(stop_card, side, _sub_cls)
                ui.label('Stop').classes('font-bold text-xs text-gray-600')
                with ui.row().classes('items-center justify-between w-full'):
                    ui.input().bind_value(draft, 'stop').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_stop_time']).bind_value(params, f'{s}_prem_stop_time').props('inline dense scale=0.8')
                stop_status = ui.label().classes('w-full text-center text-[10px] font-bold text-red-800 bg-red-100 rounded')
                stop_status.bind_visibility_from(params, f'{s}_prem_stop_active')
                def make_stop_handlers(sd, ss):
                    def _do_activate(value):
                        _cancel_engine_job_if_any('premium_stop', side)
                        params[f'{sd}_prem_stop_val'] = value
                        params[f'{sd}_prem_stop_active'] = True
                        ss.set_text(f"ON: {value}")
                        ui.notify(f"{sd} Prem Stop SET", type='positive')
                        _log_alert_action(f"⚙️ {sd} Prem Stop SET: {value}")
                    def activate():
                        try:
                            value = float(draft['stop'])
                        except (ValueError, TypeError):
                            ui.notify(f"Invalid {sd} Prem Stop Value", type='negative'); return
                        trade = shared_state['active_trades'].get(side)
                        warnings = []
                        if trade is not None:
                            is_buy_trade = trade.get('direction', 'SELL') == 'BUY'
                            current = trade['main']['current_price']
                            w1 = _premium_instant_fire_warning(True, is_buy_trade, value, current)
                            if w1: warnings.append(w1)
                            w2 = _pct_away_warning(value, current, 'Stop')
                            if w2: warnings.append(w2)
                        else:
                            w0 = _no_position_warning(side, params.get('options_buy_mode', False))
                            if w0: warnings.append(w0)
                        if warnings:
                            _confirm_warning('\n\n'.join(warnings), lambda v=value: _do_activate(v))
                        else:
                            _do_activate(value)
                    def reset():
                        _cancel_engine_job_if_any('premium_stop', side)
                        params[f'{sd}_prem_stop_active'] = False
                        params[f'{sd}_prem_stop_val'] = 0
                        draft['stop'] = 0
                        ui.notify(f"{sd} Prem Stop RESET", type='info')
                        _log_alert_action(f"⚙️ {sd} Prem Stop RESET")
                    return activate, reset
                act_s, rst_s = make_stop_handlers(s, stop_status)
                with ui.row().classes('w-full gap-1 mt-1'):
                    ui.button('Set', color='black', on_click=act_s).props('outline').classes('grow h-6 text-[10px] rounded')
                    ui.button('Reset', on_click=rst_s).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

            # Target sub-card
            with ui.card().classes(f'w-full p-2 gap-1 {init_color_class} border rounded-lg') as tgt_card:
                _bind_card_colors(tgt_card, side, _sub_cls)
                ui.label('Tgt').classes('font-bold text-xs text-gray-600')
                with ui.row().classes('items-center justify-between w-full'):
                    ui.input().bind_value(draft, 'target').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_target_time']).bind_value(params, f'{s}_prem_target_time').props('inline dense scale=0.8')
                tgt_status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
                tgt_status.bind_visibility_from(params, f'{s}_prem_tgt_active')
                def make_tgt_handlers(sd, ts):
                    def _do_activate(value):
                        params[f'{sd}_prem_target_val'] = value
                        params[f'{sd}_prem_tgt_active'] = True
                        ts.set_text(f"ON: {value}")
                        ui.notify(f"{sd} Prem Target SET", type='positive')
                        _log_alert_action(f"⚙️ {sd} Prem Target SET: {value}")
                    def activate():
                        try:
                            value = float(draft['target'])
                        except (ValueError, TypeError):
                            ui.notify(f"Invalid {sd} Prem Target Value", type='negative'); return
                        trade = shared_state['active_trades'].get(side)
                        warnings = []
                        if trade is not None:
                            is_buy_trade = trade.get('direction', 'SELL') == 'BUY'
                            current = trade['main']['current_price']
                            w1 = _premium_instant_fire_warning(False, is_buy_trade, value, current)
                            if w1: warnings.append(w1)
                            w2 = _pct_away_warning(value, current, 'Target')
                            if w2: warnings.append(w2)
                        else:
                            w0 = _no_position_warning(side, params.get('options_buy_mode', False))
                            if w0: warnings.append(w0)
                        if warnings:
                            _confirm_warning('\n\n'.join(warnings), lambda v=value: _do_activate(v))
                        else:
                            _do_activate(value)
                    def reset():
                        params[f'{sd}_prem_tgt_active'] = False
                        params[f'{sd}_prem_target_val'] = 0
                        draft['target'] = 0
                        ui.notify(f"{sd} Prem Target RESET", type='info')
                        _log_alert_action(f"⚙️ {sd} Prem Target RESET")
                    return activate, reset
                act_t, rst_t = make_tgt_handlers(s, tgt_status)
                with ui.row().classes('w-full gap-1 mt-1'):
                    ui.button('Set', color='black', on_click=act_t).props('outline').classes('grow h-6 text-[10px] rounded')
                    ui.button('Reset', on_click=rst_t).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

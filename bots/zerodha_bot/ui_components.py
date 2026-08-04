from nicegui import ui
from config import params, UI_OPTS, ui_refs, TRADEBOOK_FILE, INDICES, shared_state, ALERT_SOUND_URLS, save_alert_profile
from datetime import datetime
import pandas as pd
import uuid

# --- SHARED HELPERS ---

def _num_stepper(target, key, step=1, label=''):
    """A small -/+ stepper wrapping a text input bound to target[key]. 'target' is any
    dict-like object (params, or a local per-row 'pending' dict for staged edits), so this
    works both for live-bound fields and for the Order Book's confirm-before-apply panel."""
    def _current():
        try: return float(target[key])
        except (ValueError, TypeError): return 0.0
    def _apply(v):
        target[key] = int(v) if float(v).is_integer() else round(v, 4)
    def dec(): _apply(_current() - step)
    def inc(): _apply(_current() + step)
    with ui.row().classes('grow items-center gap-1 no-wrap'):
        ui.button(icon='remove', on_click=dec).props('flat dense round size=sm').classes('text-gray-600 bg-gray-100')
        ui.input(label).bind_value(target, key).props('outlined dense bg-color=white').classes('grow text-center')
        ui.button(icon='add', on_click=inc).props('flat dense round size=sm').classes('text-gray-600 bg-gray-100')

def _side_is_red(side, buy_mode):
    """Single source of truth for Call/Put color direction across EVERY card in the app.
    Sell Mode (unchanged): Call=red (short-side), Put=green (long-side).
    Buy Mode (flipped, per request): Buy Call=green (bullish), Buy Put=red (bearish) --
    i.e. colors always track the plain-English market direction of the position, not the
    fixed Call/Put identity. All per-side cards (unified entry, auto close, premium exit,
    index exit, open positions) call this so they stay visually consistent with each other
    in both modes."""
    if not buy_mode:
        return side == 'Call'
    return side == 'Put'

def _trade_is_red(side, buy_mode, trade):
    """Same red/green direction logic as _side_is_red, but for an OPEN TRADE specifically:
    reads the trade's own recorded 'direction' (BUY/SELL) when a trade exists, so a
    position's color never flips just because the global options_buy_mode toggle changes
    later (e.g. after the trade closes and the mode is switched back) -- consistent with how
    _position_side_badge already handles the SHORT/BUY text label. Falls back to the live
    buy_mode flag only when no trade is present yet."""
    if trade is not None:
        direction = trade.get('direction', 'SELL')
        is_buy_trade = (direction == 'BUY')
    else:
        is_buy_trade = buy_mode
    # A BUY trade profits like a "Call-in-Buy-Mode" (bullish/green); a SELL trade behaves
    # like a "Call-in-Sell-Mode" (red) for Call, or the Put equivalent. Reuse _side_is_red's
    # exact mapping so this can never drift out of sync with every other card in the app.
    effective_buy_mode = is_buy_trade
    return _side_is_red(side, effective_buy_mode)

def _side_colors(side, buy_mode, weight='100'):
    """Returns (bg/border class string, plain color name) for a given side+mode+shade.
    weight matches the existing per-card shade conventions already in this file (some cards
    use bg-*-50/border-*-200, others bg-*-100/border-*-300) so visuals don't change except
    for the actual hue swap in Buy Mode."""
    is_red = _side_is_red(side, buy_mode)
    if weight == '50':
        return (('bg-red-50 border-red-200', 'red') if is_red else ('bg-green-50 border-green-200', 'green'))
    return (('bg-red-100 border-red-300', 'red') if is_red else ('bg-green-100 border-green-300', 'green'))

def _bind_card_colors(card, side, cls_template):
    """Re-applies a card's background/border classes every time options_buy_mode changes, so
    the color scheme flips live rather than only at initial render. NiceGUI/Quasar elements
    don't expose a generic reactive 'bind classes' helper, so this drives the re-application
    via a hidden zero-width label whose bind_text_from callback is used purely for its side
    effect (calling card.classes(replace=...)) -- the label's own text is always empty and
    it renders with 'hidden' so it's invisible. cls_template(is_red) -> full class string."""
    def _apply(buy_mode, c=card, s=side):
        is_red = _side_is_red(s, buy_mode)
        c.classes(replace=cls_template(is_red))
        return ''
    hook = ui.label('').classes('hidden')
    hook.bind_text_from(params, 'options_buy_mode', backward=_apply)

def _bind_btn_color(button, side, red_color='red', green_color='green'):
    """Same hidden-hook pattern as _bind_card_colors, but for a button's color prop (NiceGUI
    buttons don't expose a reactive 'color' bind either)."""
    def _apply(buy_mode, b=button, s=side):
        is_red = _side_is_red(s, buy_mode)
        b.props(f'color={red_color if is_red else green_color}')
        return ''
    hook = ui.label('').classes('hidden')
    hook.bind_text_from(params, 'options_buy_mode', backward=_apply)

# --- ORDER-SANITY WARNING SYSTEM ---
# Shared by the unified Open Short/Long entry cards, Premium Exit cards, and Index Exit cards.
# None of these BLOCK the action -- they show a confirmation dialog ('Proceed Anyway' /
# 'Cancel') so a legitimate edge case is never locked out, but an obvious mistake (a trigger
# that will fire the instant it's armed, or a stop/target value that's wildly far from the
# current price) gets a chance to be caught first.

def _confirm_warning(message, on_proceed):
    """Shows a warning dialog with the given message (one or more lines); calls on_proceed()
    ONLY if the person clicks 'Proceed Anyway'. Clicking 'Cancel' (or closing the dialog)
    leaves everything untouched -- nothing is armed/set until confirmed."""
    with ui.dialog() as dialog, ui.card().classes('p-4 gap-3 max-w-md'):
        ui.label('⚠️ Check Your Order').classes('font-bold text-orange-700 text-sm')
        for line in message.split('\n\n'):
            ui.label(line).classes('text-xs text-gray-700')
        with ui.row().classes('w-full gap-2 justify-end mt-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat').classes('text-gray-600')
            def proceed():
                dialog.close()
                on_proceed()
            ui.button('Proceed Anyway', color='orange', on_click=proceed)
    dialog.open()

def _unified_instant_fire_warning(side, order_type, buy_mode, idx_ltp, trigger_price):
    """For the unified Open Short/Long entry card. Returns a warning message if arming this
    Limit/Stop-Market trigger RIGHT NOW would fire immediately as a market order on the very
    next tick -- i.e. the exact condition LogicEngine._check_unified_open checks is already
    true. Mirrors that function's direction rules exactly (Sell Mode vs Buy Mode, Call vs
    Put) so this warning is never wrong about what will actually happen. In that case the
    person most likely meant the OTHER order type, since Limit and Stop-Market are
    opposite-direction triggers for the same side/mode."""
    if trigger_price <= 0 or idx_ltp <= 0: return None
    fires_now = False
    if not buy_mode:
        if order_type == 'Stop-Market':
            fires_now = (idx_ltp <= trigger_price) if side == 'Call' else (idx_ltp >= trigger_price)
        elif order_type == 'Limit':
            fires_now = (idx_ltp >= trigger_price) if side == 'Call' else (idx_ltp <= trigger_price)
    else:
        if order_type == 'Stop-Market':
            fires_now = (idx_ltp >= trigger_price) if side == 'Call' else (idx_ltp <= trigger_price)
        elif order_type == 'Limit':
            fires_now = (idx_ltp <= trigger_price) if side == 'Call' else (idx_ltp >= trigger_price)
    if not fires_now: return None
    other = 'Stop-Market' if order_type == 'Limit' else 'Limit'
    return (f"This {order_type} trigger ({trigger_price}) will fire IMMEDIATELY as a market order, "
            f"since the index is already at {idx_ltp:.2f}. Did you mean to place a {other} order instead?")

def _premium_instant_fire_warning(is_stop, is_buy_trade, value, current):
    """For Premium Exit Stop/Target. Returns a warning if this value has ALREADY been crossed
    by the live option premium, so it would close the position the instant it's set. Premium
    exit direction depends only on the trade's own direction (BUY vs SELL) -- NOT on Call vs
    Put -- exactly mirroring LogicEngine._check_exits's PREMIUM EXITS branch."""
    if value <= 0 or current <= 0: return None
    if is_stop:
        crossed = (current <= value) if is_buy_trade else (current >= value)
    else:
        crossed = (current >= value) if is_buy_trade else (current <= value)
    if not crossed: return None
    kind = 'Stop' if is_stop else 'Target'
    return f"{kind} value ({value}) has already been reached by the current premium ({current:.2f}) -- this will fire IMMEDIATELY."

def _index_instant_fire_warning(is_stop, side, buy_mode, value, current):
    """For Index Exit Stop/Target. Returns a warning if this value has ALREADY been crossed by
    the live index price, so it would close the position the instant it's set. Index exit
    direction depends on BOTH side (Call/Put) and the global options_buy_mode toggle, exactly
    mirroring LogicEngine._check_exits's INDEX EXITS branch."""
    if value <= 0 or current <= 0: return None
    if not buy_mode:
        if side == 'Call': crossed = (current >= value) if is_stop else (current <= value)
        else: crossed = (current <= value) if is_stop else (current >= value)
    else:
        if side == 'Call': crossed = (current <= value) if is_stop else (current >= value)
        else: crossed = (current >= value) if is_stop else (current <= value)
    if not crossed: return None
    kind = 'Stop' if is_stop else 'Target'
    return f"{kind} value ({value}) has already been reached by the current index price ({current:.2f}) -- this will fire IMMEDIATELY."

def _pct_away_warning(value, current, kind_label):
    """Generic 'sanity check' warning shared by Premium and Index Exit: flags a Stop/Target
    value that's more than 10% away from the current price, regardless of direction -- a
    likely typo (e.g. an extra/missing digit) rather than an intentional wide stop."""
    if value <= 0 or current <= 0: return None
    pct = abs(value - current) / current
    if pct <= 0.10: return None
    return f"{kind_label} value ({value}) is {pct*100:.0f}% away from the current price ({current:.2f})."

# --- CONTROL CARDS ---

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
    disarms, clears the trigger price, and restores order type/strike/qty/fire-on/stop/target."""
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
    (bearish) -- the opposite of Sell Mode's fixed Call=red/Put=green scheme."""
    order_key = f'{prefix}_order_type'; trig_key = f'{prefix}_trigger_price'
    strike_key = f'{prefix}_strike_offset'; qty_key = f'{prefix}_qty'
    fire_key = f'{prefix}_fire_on'; armed_key = f'{prefix}_armed'
    stop_key = f'{prefix}_new_stop'; target_key = f'{prefix}_new_target'

    init_color_class, init_btn_color = _side_colors(side, params.get('options_buy_mode', False), weight='100')

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
            # value is simply ignored by the firing logic when order type is Market.
            ui.input('Trigger Price').bind_value(params, trig_key).props('outlined dense bg-color=white').classes('grow')
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
            try: trigger_price = float(params[trig_key])
            except (ValueError, TypeError): trigger_price = 0
            idx_ltp = shared_state.get(params['trading_index'], {}).get('ltp', 0)
            buy_mode = params.get('options_buy_mode', False)

            def _do_arm():
                # Stamp the arm time so the timing-boundary check in
                # LogicEngine._check_unified_open requires the NEXT candle close after this
                # moment, not any boundary crossing (fixes: arming during a boundary-minute
                # firing on the very next tick instead of waiting a full period).
                params[f'{prefix}_armed_at'] = datetime.now()
                params[armed_key] = True
                status.set_text(f"ARMED: {order_type} @ {trigger_price} ({params[fire_key]})")
                ui.notify(f"{_unified_card_title(side, params.get('options_buy_mode', False))} ARMED", type='positive')

            warning = _unified_instant_fire_warning(side, order_type, buy_mode, idx_ltp, trigger_price)
            if warning:
                _confirm_warning(warning, _do_arm)
            else:
                _do_arm()

        def cancel():
            # Full reset: trigger price + every other field back to default (not just disarm).
            _reset_unified_card_defaults(prefix)
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
    per-side card in the app."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    with ui.card().classes(f'w-full p-3 gap-2 {init_color_class} border shadow-sm rounded-xl') as card:
        _bind_card_colors(card, side, lambda is_red: f"w-full p-3 gap-2 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border shadow-sm rounded-xl")

        ui.label(f'Auto Close {side}').classes('font-bold text-xs uppercase text-gray-500 mb-1')

        with ui.row().classes('w-full items-center gap-1'):
            ui.label('Profit').classes('text-[10px] w-8 font-bold text-green-700')
            ui.input().bind_value(params, target_val_key).props('outlined dense prefix="₹" bg-color=white').classes('grow')
            st_tgt = ui.label('ON').classes('text-[9px] text-white bg-green-600 rounded px-1 hidden')
            st_tgt.bind_visibility_from(params, target_active_key)
            def set_tgt(): params[target_active_key] = True; ui.notify(f"{side} Profit Set", type='positive')
            def rst_tgt(): params[target_active_key] = False; params[target_val_key] = 0; ui.notify(f"{side} Profit Reset", type='info')
            ui.button('SET', on_click=set_tgt, color='green-8').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')
            ui.button('RESET', on_click=rst_tgt, color='grey').props('dense flat').classes('w-auto px-2 h-6 text-[10px] rounded')

        with ui.row().classes('w-full items-center gap-1'):
            ui.label('Loss').classes('text-[10px] w-8 font-bold text-red-700')
            ui.input().bind_value(params, stop_val_key).props('outlined dense prefix="₹" bg-color=white').classes('grow')
            st_stp = ui.label('ON').classes('text-[9px] text-white bg-red-600 rounded px-1 hidden')
            st_stp.bind_visibility_from(params, stop_active_key)
            def set_stp(): params[stop_active_key] = True; ui.notify(f"{side} Loss Set", type='positive')
            def rst_stp(): params[stop_active_key] = False; params[stop_val_key] = 0; ui.notify(f"{side} Loss Reset", type='info')
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
        def reset():
            params[active_key] = False; params[amt_key] = 0; params[strike_key] = 0
            ui.notify(f"{title} RESET", type='info')
        with ui.row().classes('w-full gap-2'):
            ui.button('Activate', color=btn_color, on_click=activate).classes('grow h-8 text-xs rounded-lg shadow-sm')
            ui.button('Reset', on_click=reset).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')

def global_control_card(label, value_key, active_key):
    with ui.card().classes('w-full p-3 gap-2 bg-gray-50 border border-gray-200 shadow-sm rounded-xl'):
        ui.label(label).classes('font-bold text-sm text-gray-700')
        ui.input().bind_value(params, value_key).props('outlined dense bg-color=white prefix="₹"').classes('w-full')
        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-blue-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, active_key)
        def activate():
            params[active_key] = True; status.set_text(f"ACTIVE: {params[value_key]}")
            ui.notify(f"{label} SET", type='positive')
        def reset():
            params[active_key] = False; params[value_key] = 0
            ui.notify(f"{label} RESET", type='info')
        with ui.row().classes('w-full gap-2'):
            ui.button('Set', color='blue-7', on_click=activate).classes('grow h-8 text-xs rounded-lg')
            ui.button('Reset', on_click=reset).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')

def index_exit_component(side, label, time_key, value_key, active_key):
    """Card background is mode-aware via _side_colors/_bind_card_colors: Sell Mode unchanged
    (Call=red, Put=green); Buy Mode flips (Buy Call=green, Buy Put=red), matching every other
    per-side card in the app."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    with ui.card().classes(f'w-full p-3 gap-1 {init_color_class} border rounded-lg') as card:
        _bind_card_colors(card, side, lambda is_red: f"w-full p-3 gap-1 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border rounded-lg")

        ui.label(label).classes('font-bold text-xs text-gray-600')
        with ui.row().classes('items-center justify-between w-full'):
            ui.input().bind_value(params, value_key).props('outlined dense bg-color=white').classes('w-24')
            ui.radio(UI_OPTS['index_times'], value=params[time_key]).bind_value(params, time_key).props('inline dense scale=0.8')
        status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
        status.bind_visibility_from(params, active_key)
        def _do_activate():
            params[active_key] = True; status.set_text(f"ON: {params[value_key]}")
            ui.notify(f"{side} Index {label} SET", type='positive')
        def activate():
            try: value = float(params[value_key])
            except (ValueError, TypeError): value = 0
            idx_ltp = shared_state.get(params['trading_index'], {}).get('ltp', 0)
            buy_mode = params.get('options_buy_mode', False)
            is_stop = (label == 'Stop')
            warnings = []
            w1 = _index_instant_fire_warning(is_stop, side, buy_mode, value, idx_ltp)
            if w1: warnings.append(w1)
            w2 = _pct_away_warning(value, idx_ltp, label)
            if w2: warnings.append(w2)
            if warnings:
                _confirm_warning('\n\n'.join(warnings), _do_activate)
            else:
                _do_activate()
        def reset():
            params[active_key] = False; params[value_key] = 0
            ui.notify(f"{side} Index {label} RESET", type='info')
        with ui.row().classes('w-full gap-1 mt-1'):
            ui.button('Set', color='black', on_click=activate).props('outline').classes('grow h-6 text-[10px] rounded')
            ui.button('Reset', on_click=reset).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

def premium_exit_card(side):
    """Exit based on the live LTP of the main option leg. Stop/Target sub-labels and helper
    text auto-flip based on options_buy_mode: in Sell Mode (unchanged) Stop = premium rises
    (loss on short), Target = premium falls (profit on short); in Buy Mode these invert since
    a long position profits as premium rises. Card + both sub-card backgrounds and the header
    label color are also mode-aware via _side_colors/_bind_card_colors (Sell Mode unchanged:
    Call=red, Put=green; Buy Mode flips: Buy Call=green, Buy Put=red)."""
    init_color_class, _ = _side_colors(side, params.get('options_buy_mode', False), weight='50')
    s = side.lower()

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
                    ui.input().bind_value(params, f'{s}_prem_stop_val').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_stop_time']).bind_value(params, f'{s}_prem_stop_time').props('inline dense scale=0.8')
                stop_status = ui.label().classes('w-full text-center text-[10px] font-bold text-red-800 bg-red-100 rounded')
                stop_status.bind_visibility_from(params, f'{s}_prem_stop_active')
                def make_stop_handlers(sd, ss):
                    def _do_activate():
                        params[f'{sd}_prem_stop_active'] = True
                        ss.set_text(f"ON: {params[f'{sd}_prem_stop_val']}")
                        ui.notify(f"{sd} Prem Stop SET", type='positive')
                    def activate():
                        try: value = float(params[f'{sd}_prem_stop_val'])
                        except (ValueError, TypeError): value = 0
                        trade = shared_state['active_trades'].get(side)
                        warnings = []
                        if trade is not None:
                            is_buy_trade = trade.get('direction', 'SELL') == 'BUY'
                            current = trade['main']['current_price']
                            w1 = _premium_instant_fire_warning(True, is_buy_trade, value, current)
                            if w1: warnings.append(w1)
                            w2 = _pct_away_warning(value, current, 'Stop')
                            if w2: warnings.append(w2)
                        if warnings:
                            _confirm_warning('\n\n'.join(warnings), _do_activate)
                        else:
                            _do_activate()
                    def reset():
                        params[f'{sd}_prem_stop_active'] = False
                        params[f'{sd}_prem_stop_val'] = 0
                        ui.notify(f"{sd} Prem Stop RESET", type='info')
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
                    ui.input().bind_value(params, f'{s}_prem_target_val').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_target_time']).bind_value(params, f'{s}_prem_target_time').props('inline dense scale=0.8')
                tgt_status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
                tgt_status.bind_visibility_from(params, f'{s}_prem_tgt_active')
                def make_tgt_handlers(sd, ts):
                    def _do_activate():
                        params[f'{sd}_prem_tgt_active'] = True
                        ts.set_text(f"ON: {params[f'{sd}_prem_target_val']}")
                        ui.notify(f"{sd} Prem Target SET", type='positive')
                    def activate():
                        try: value = float(params[f'{sd}_prem_target_val'])
                        except (ValueError, TypeError): value = 0
                        trade = shared_state['active_trades'].get(side)
                        warnings = []
                        if trade is not None:
                            is_buy_trade = trade.get('direction', 'SELL') == 'BUY'
                            current = trade['main']['current_price']
                            w1 = _premium_instant_fire_warning(False, is_buy_trade, value, current)
                            if w1: warnings.append(w1)
                            w2 = _pct_away_warning(value, current, 'Target')
                            if w2: warnings.append(w2)
                        if warnings:
                            _confirm_warning('\n\n'.join(warnings), _do_activate)
                        else:
                            _do_activate()
                    def reset():
                        params[f'{sd}_prem_tgt_active'] = False
                        params[f'{sd}_prem_target_val'] = 0
                        ui.notify(f"{sd} Prem Target RESET", type='info')
                    return activate, reset
                act_t, rst_t = make_tgt_handlers(s, tgt_status)
                with ui.row().classes('w-full gap-1 mt-1'):
                    ui.button('Set', color='black', on_click=act_t).props('outline').classes('grow h-6 text-[10px] rounded')
                    ui.button('Reset', on_click=rst_t).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

def _log_alert_action(message):
    """Writes an entry into the shared Trade Event Log (shared_state['activity_log']) -- the
    exact same store LogicEngine.log_action() writes to in logic_engine.py, using the same
    '[HH:MM:SS] message' format and 100-entry cap -- so alert add/modify/cancel show up
    alongside trade opens/closes/fires in one unified log. Defined here rather than calling
    into LogicEngine since these UI handlers have no LogicEngine instance available; writing
    directly to shared_state keeps a single source of truth for the log's storage."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    shared_state['activity_log'].insert(0, f"[{timestamp}] {message}")
    shared_state['activity_log'] = shared_state['activity_log'][:100]

def _add_alert_card(direction, side_label, input_key, period_key, notify_fn):
    """'Add Alert' form for one direction (Upper or Lower). Unlike the old single-slot
    version, this does NOT hold the alert's live state -- clicking 'Add' appends a brand new,
    independent entry to shared_state['alerts'] and clears the input for the next one, so any
    number of alerts can be created in the same direction. Sound/duration are captured at
    creation time from the current shared Alert Sound Profile (render_alert_sound_panel),
    and can be changed per-alert afterward via MODIFY in the Active Alerts section."""
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label(f'Add {side_label} Price Alert').classes('font-bold text-gray-800')

        with ui.row().classes('items-center w-full justify-between'):
            ui.label('Period:').classes('text-xs text-gray-500')
            ui.radio(UI_OPTS['alert_periods'], value=params[period_key]).bind_value(params, period_key).props('inline dense')

        ui.input(side_label).bind_value(params, input_key).props('outlined dense bg-color=white').classes('w-full')

        def add_alert():
            try:
                value = float(params[input_key])
                if value <= 0: raise ValueError
            except (ValueError, TypeError):
                notify_fn("Invalid Alert Value", type='negative')
                return
            new_alert = {
                'id': str(uuid.uuid4())[:8],
                'direction': direction,
                'value': value,
                'period': params[period_key],
                'sound': params.get('alert_upper_sound', 'Wood Plank'),
                'duration': params.get('alert_upper_duration', 5),
                'created_at': datetime.now().strftime('%H:%M:%S'),
            }
            shared_state['alerts'].append(new_alert)
            params[input_key] = 0  # clear the form so the next alert starts fresh
            notify_fn(f"{side_label} Alert ADDED: {value}", type='positive')
            _log_alert_action(f"🔔 {side_label} Alert Set: {value} ({new_alert['period']})")

        ui.button('Add Alert', color='orange', on_click=add_alert).classes('w-full h-8 rounded-lg')

def alerts_card_upper():
    _add_alert_card('upper', 'Upper', 'alert_upper_input', 'alert_upper_period', ui.notify)

def alerts_card_lower():
    _add_alert_card('lower', 'Lower', 'alert_lower_input', 'alert_lower_period', ui.notify)

def _preview_sound(sound_name, duration):
    url = ALERT_SOUND_URLS.get(sound_name, ALERT_SOUND_URLS['Wood Plank'])
    try: dur_ms = int(float(duration) * 1000)
    except (ValueError, TypeError): dur_ms = 3000
    if dur_ms <= 0: dur_ms = 3000
    dur_ms = min(dur_ms, 8000)  # cap preview length so trying several sounds isn't tedious
    ui.run_javascript(
        f'const a = new Audio("{url}"); a.loop = true; a.play().catch(()=>{{}});'
        f'setTimeout(() => {{ a.pause(); a.currentTime = 0; }}, {dur_ms});'
    )

def render_alert_sound_panel():
    """Shared sound + duration profile used by BOTH Upper and Lower price alerts, separated
    from the alert cards themselves. Preview plays the selected sound without committing it;
    Set applies it to both alerts AND persists it to disk (alert_sound_profile.json) via
    config.save_alert_profile, so it survives page reloads and worker/script restarts."""
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label('Alert Sound Profile').classes('font-bold text-gray-800')
        ui.label('Applies to both Upper and Lower price alerts.').classes('text-[10px] text-gray-500 -mt-1 mb-1')

        draft = {'sound': params.get('alert_upper_sound', 'Wood Plank'), 'duration': params.get('alert_upper_duration', 5)}

        with ui.row().classes('w-full gap-2'):
            ui.select(UI_OPTS['alert_sounds'], value=draft['sound'], label='Sound').bind_value(draft, 'sound').props('outlined dense bg-color=white').classes('grow')
            ui.input('Duration (s)').bind_value(draft, 'duration').props('outlined dense bg-color=white').classes('w-28')

        def preview():
            _preview_sound(draft['sound'], draft['duration'])

        def set_profile():
            params['alert_upper_sound'] = draft['sound']; params['alert_upper_duration'] = draft['duration']
            params['alert_lower_sound'] = draft['sound']; params['alert_lower_duration'] = draft['duration']
            save_alert_profile(draft['sound'], draft['duration'])
            ui.notify(f"Alert sound set: {draft['sound']} ({draft['duration']}s)", type='positive')

        with ui.row().classes('w-full gap-2'):
            ui.button('▶ Preview', on_click=preview).classes('grow h-8 rounded-lg')
            ui.button('Set', color='orange', on_click=set_profile).classes('grow h-8 rounded-lg')

# --- ACTIVE ALERTS (multiple, independent price alerts per direction) ---

def _alert_row(alert):
    """One expandable row for a single pending price alert in shared_state['alerts'],
    matching the same header/expansion pattern as the Order Book rows (_orderbook_table_row/
    _exit_order_row) for visual consistency across the app. MODIFY opens a staged draft
    (value/period/sound/duration) that only commits on CONFIRM, by looking the alert back up
    via its id (so it keeps editing the right entry even if other alerts are added/removed/
    reordered in the list while this row's expansion is open). CANCEL removes it immediately,
    no confirm needed (matching REMOVE elsewhere in the app)."""
    alert_id = alert['id']
    direction = alert['direction']
    label = 'Upper' if direction == 'upper' else 'Lower'
    label_cls = 'w-14 text-orange-600 font-bold' if direction == 'upper' else 'w-14 text-blue-600 font-bold'
    draft = {'value': alert['value'], 'period': alert['period'], 'sound': alert['sound'], 'duration': alert['duration']}

    def _find():
        # Re-fetch the live dict by id every time, since shared_state['alerts'] entries are
        # never mutated in place from outside _check_alerts (which replaces the whole list).
        for a in shared_state['alerts']:
            if a.get('id') == alert_id: return a
        return None

    with ui.expansion('', icon='notifications').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
        with exp.add_slot('header'):
            with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                ui.label(alert['created_at']).classes('w-16 text-gray-400 font-mono')
                ui.label(label).classes(label_cls)
                # These four are bound directly to the SAME dict object stored in
                # shared_state['alerts'] (not a static f-string snapshot) so that CONFIRM's
                # in-place mutation of that dict (see confirm_changes below) is reflected here
                # immediately. Without this, the header kept showing the pre-MODIFY value: rows
                # are only rebuilt when the alert id SET changes (see render_active_alerts),
                # not on every field edit, so a static label set once at build time would never
                # picked up a later in-place change to the same alert.
                ui.label().bind_text_from(alert, 'value', backward=lambda v, d=direction: f"idx {'>=' if d == 'upper' else '<='} {v}").classes('w-32 font-mono text-gray-800')
                ui.label().bind_text_from(alert, 'period').classes('w-16 text-gray-500')
                ui.label().bind_text_from(alert, 'sound').classes('w-32 text-purple-700')
                ui.label().bind_text_from(alert, 'duration', backward=lambda v: f"{v}s").classes('w-12 text-gray-500')
                ui.label('PENDING').classes('bg-orange-500 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                ui.space()

                def cancel_alert():
                    shared_state['alerts'] = [a for a in shared_state['alerts'] if a.get('id') != alert_id]
                    ui.notify(f"{label} Alert Cancelled", type='info')
                    _log_alert_action(f"🔔 {label} Alert Cancelled: {alert['value']}")

                def open_modify():
                    live = _find()
                    if live:
                        draft['value'] = live['value']; draft['period'] = live['period']
                        draft['sound'] = live['sound']; draft['duration'] = live['duration']
                    exp.value = True

                ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                ui.button('CANCEL').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', cancel_alert)

        with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
            with ui.row().classes('w-full gap-2'):
                ui.input(label).bind_value(draft, 'value').props('outlined dense bg-color=white').classes('grow')
                ui.radio(UI_OPTS['alert_periods'], value=draft['period']).bind_value(draft, 'period').props('inline dense')
            with ui.row().classes('w-full gap-2'):
                ui.select(UI_OPTS['alert_sounds'], value=draft['sound'], label='Sound').bind_value(draft, 'sound').props('outlined dense bg-color=white').classes('grow')
                ui.input('Duration (s)').bind_value(draft, 'duration').props('outlined dense bg-color=white').classes('w-28')

            def preview():
                _preview_sound(draft['sound'], draft['duration'])

            def confirm_changes():
                try:
                    value = float(draft['value'])
                    if value <= 0: raise ValueError
                except (ValueError, TypeError):
                    ui.notify("Invalid Alert Value", type='negative')
                    return
                live = _find()
                if live is None:
                    ui.notify("Alert no longer exists", type='negative')
                    exp.value = False
                    return
                live['value'] = value; live['period'] = draft['period']
                live['sound'] = draft['sound']; live['duration'] = draft['duration']
                ui.notify(f"{label} Alert Updated", type='positive')
                _log_alert_action(f"🔔 {label} Alert Modified: {value} ({draft['period']})")
                exp.value = False

            with ui.row().classes('w-full gap-2'):
                ui.button('▶ Preview', on_click=preview).classes('grow h-8 text-xs rounded-lg')
                ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                ui.button('Cancel', on_click=lambda: setattr(exp, 'value', False)).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800')

def render_active_alerts():
    """'ACTIVE ALERTS' section: lists every pending price alert in shared_state['alerts'],
    any number per direction, each independently editable (MODIFY) or cancellable (CANCEL).

    Rows are only cleared and rebuilt when the SET of alert ids actually changes (an alert
    added or removed/fired) -- NOT on every 1s timer tick regardless. The real cause of
    'MODIFY keeps collapsing' was that an earlier version unconditionally called
    rows_container.clear() + rebuilt every row every second; any expansion the user had just
    opened via MODIFY was destroyed and recreated (collapsed by default) within ~1 second of
    opening it, which looked exactly like the click itself failing. Only touching the DOM
    when the id set changes leaves an open expansion alone indefinitely while nothing is
    added/removed elsewhere."""
    with ui.card().classes('w-full bg-white p-3 gap-2 rounded-xl shadow-sm mb-4 border border-gray-200'):
        with ui.row().classes('w-full justify-between items-center mb-1'):
            ui.label('ACTIVE ALERTS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')
            count_lbl = ui.label('0 pending').classes('text-[10px] text-gray-400')

        rows_container = ui.column().classes('w-full gap-1')
        empty_lbl = ui.label('No active alerts.').classes('w-full text-center text-xs text-gray-400 italic')

        last_ids = {'ids': None}

        def refresh_view():
            alerts = shared_state.get('alerts', [])
            count_lbl.set_text(f"{len(alerts)} pending")
            empty_lbl.set_visibility(len(alerts) == 0)

            current_ids = tuple(a.get('id') for a in alerts)
            if current_ids == last_ids['ids']:
                return  # nothing added/removed -- leave existing rows (and any open MODIFY
                        # expansion) completely untouched
            last_ids['ids'] = current_ids

            rows_container.clear()
            with rows_container:
                for alert in alerts:
                    _alert_row(alert)

        refresh_view()
        ui.timer(1.0, refresh_view)

# --- OPEN POSITIONS (kept alongside the existing banner CALL/PUT POSITION cards) ---

def _position_side_badge(side, buy_mode, trade):
    """SHORT/LONG (Sell Mode) or BUY (Buy Mode) badge text+color for the Open Positions row.
    Reads the trade's OWN recorded direction when a trade exists (so history/labels never
    flip just because the global toggle changes later); falls back to the live buy_mode flag
    only when no trade is present yet (e.g. right when a position is being opened, before the
    trade dict is fully populated in shared_state).

    Sell Mode (direction != 'BUY'): Call = SHORT (sell CE), Put = LONG (sell PE) -- these are
    genuinely different positions with different bias, so they must not share one label.
    Buy Mode (direction == 'BUY'): both Call and Put show BUY, since both are long options,
    just on opposite underlying bias (bullish CE vs bearish PE) -- color (not the text label)
    is what distinguishes them there, via _position_row_accent/_trade_is_red."""
    direction = None
    if trade is not None:
        direction = trade.get('direction')
    if direction is None:
        direction = 'BUY' if buy_mode else 'SELL'
    if direction == 'BUY':
        return 'BUY', 'bg-blue-100 text-blue-700'
    return ('SHORT', 'bg-red-100 text-red-700') if side == 'Call' else ('LONG', 'bg-green-100 text-green-700')

def _position_row_accent(side, buy_mode, trade):
    """Left accent border class for an Open Positions row. Mode-aware via _trade_is_red, so
    it matches every other card's color convention: Sell Mode unchanged (Call=red border,
    Put=green border); Buy Mode flips (Buy Call=green border since it's a bullish long, Buy
    Put=red border since it's a bearish long)."""
    is_red = _trade_is_red(side, buy_mode, trade)
    return 'border-red-500' if is_red else 'border-green-500'

def _set_position_row_style(row, side, buy_mode, trade):
    """SINGLE source of truth for an Open Positions row's accent color AND visibility.

    These two concerns are DELIBERATELY handled through two completely independent
    mechanisms so that updating one can never accidentally clobber the other:
      - Color: applied via row.style(...) (a MERGE into the element's inline style dict),
        never via row.classes(replace=...). classes(replace=...) overwrites the ENTIRE class
        list, which is what silently wiped out visibility state in earlier attempts at this
        fix. style() only touches the one CSS property named, leaving everything else --
        including any visibility-related class or style -- untouched.
      - Visibility: applied via a direct 'display' CSS style (the most fundamental, literal
        way to hide an element in a browser -- it cannot be undone by any classes() call
        elsewhere, since classes() and style() are separate attributes on the element). This
        does NOT rely on NiceGUI's set_visibility()/bind_visibility_from() abstractions at
        all, which is intentional: those are the mechanisms earlier fix attempts already went
        through, and the row was still staying visible, meaning something about how they
        interact with the Tailwind 'hidden' class and repeated classes(replace=...) calls in
        this codebase was not reliably taking effect. A raw 'display: none' style is the
        floor -- there's no lower-level way to hide a DOM element that a framework could
        still override out from under us.

    Every caller (build time in _position_row, and every tick in auto_run.py's update_ui())
    MUST go through this one function -- never call row.classes(replace=...) or
    row.set_visibility(...) on a position row directly."""
    is_red = _trade_is_red(side, buy_mode, trade)
    color = '#ef4444' if is_red else '#22c55e'  # Tailwind red-500 / green-500
    row.style(f'border-left-color: {color} !important')
    if trade is not None:
        row.style('display: block !important')
        row.set_visibility(True)
    else:
        row.style('display: none !important')
        row.set_visibility(False)

def _position_row(side, on_close=None):
    """One row of the Open Positions section. Only visible while that side has an active
    trade. Values (mark/size/pnl/entry/qty/symbol) are populated live each tick by
    auto_run.py's update_ui(), the same pattern already used for the banner cards.

    Classes AND visibility for the outer row are always set together via
    _set_position_row_style (see its docstring for why) -- both at build time here and every
    tick from auto_run.py's update_ui(), so they can never drift out of sync.

    Stop/Target here control the INDEX-PRICE-based exit (call_index_stop_val/
    call_index_stop_active etc, the same params the 'Exit based on Index' cards use) rather
    than the PnL-based Auto Close values, and always check on live price: toggling either
    switch forces the corresponding *_index_stop_time/*_index_target_time to 'Current'."""
    prefix = 'call' if side == 'Call' else 'put'
    stop_val_key = f'{prefix}_index_stop_val'; stop_active_key = f'{prefix}_index_stop_active'; stop_time_key = f'{prefix}_index_stop_time'
    tgt_val_key = f'{prefix}_index_target_val'; tgt_active_key = f'{prefix}_index_tgt_active'; tgt_time_key = f'{prefix}_index_target_time'

    def force_live(_e=None, key=None):
        params[key] = 'Current'

    with ui.card().classes('w-full bg-white border-l-4 border-gray-300 border border-gray-200 rounded-lg p-3 gap-2 shadow-sm') as row:
        ui_refs[f'{prefix}_pos_row'] = row
        _set_position_row_style(row, side, params.get('options_buy_mode', False), shared_state['active_trades'].get(side))

        # Re-applies style+visibility together whenever options_buy_mode changes (mode toggled
        # while this row happens to be visible or hidden).
        def _apply_row_style_on_mode(buy_mode, r=row, s=side):
            _set_position_row_style(r, s, buy_mode, shared_state['active_trades'].get(s))
            return ''
        _mode_hook = ui.label('').classes('hidden')
        _mode_hook.bind_text_from(params, 'options_buy_mode', backward=_apply_row_style_on_mode)

        with ui.row().classes('w-full justify-between items-center flex-wrap gap-2'):
            with ui.row().classes('items-center gap-2'):
                ui_refs[f'{prefix}_pos_symbol'] = ui.label('-').classes('text-gray-800 font-bold text-sm font-mono')
                side_label, side_cls = _position_side_badge(side, params.get('options_buy_mode', False), shared_state['active_trades'].get(side))
                side_lbl = ui.label(side_label).classes(f'{side_cls} text-[10px] font-bold px-2 py-0.5 rounded')
                ui_refs[f'{prefix}_pos_side_label'] = side_lbl
            with ui.row().classes('items-center gap-6'):
                with ui.column().classes('items-end gap-0'):
                    ui.label('MARK').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_mark'] = ui.label('0.0').classes('text-orange-600 font-mono font-bold text-sm')
                with ui.column().classes('items-end gap-0'):
                    ui.label('SIZE').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_size'] = ui.label('0').classes('text-gray-800 font-mono text-sm')
                with ui.column().classes('items-end gap-0'):
                    ui.label('uPnL').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_pnl'] = ui.label('0').classes('font-mono font-bold text-sm text-gray-800')

        with ui.row().classes('w-full gap-6 text-[11px] text-gray-500 flex-wrap'):
            with ui.row().classes('gap-1 items-baseline'):
                ui.label('Entry')
                ui_refs[f'{prefix}_pos_entry'] = ui.label('0.0').classes('text-gray-700 font-mono')
            with ui.row().classes('gap-1 items-baseline'):
                ui.label('Qty')
                ui_refs[f'{prefix}_pos_qty'] = ui.label('0').classes('text-gray-700 font-mono')

        with ui.row().classes('w-full gap-3 items-center pt-2 border-t border-gray-200 flex-wrap'):
            ui.label('Idx Stop').classes('text-[10px] text-gray-500')
            ui.switch(on_change=lambda e, k=stop_time_key: force_live(e, k)).bind_value(params, stop_active_key).props('dense color=red size=sm')
            ui.input().bind_value(params, stop_val_key).props('outlined dense bg-color=white').classes('w-24')
            ui.label('Idx Target').classes('text-[10px] text-gray-500')
            ui.switch(on_change=lambda e, k=tgt_time_key: force_live(e, k)).bind_value(params, tgt_active_key).props('dense color=green size=sm')
            ui.input().bind_value(params, tgt_val_key).props('outlined dense bg-color=white').classes('w-24')
            ui.space()
            ui.button('CLOSE', color='red', on_click=on_close).classes('h-7 text-xs px-4 rounded font-bold')

def render_open_positions(on_close_call=None, on_close_put=None):
    """'OPEN POSITIONS' section, kept alongside the existing banner CALL/PUT POSITION cards
    (not a replacement). White background, matching the rest of the app."""
    with ui.card().classes('w-full bg-white p-3 gap-3 rounded-xl shadow-sm mb-4 border border-gray-200'):
        with ui.row().classes('w-full justify-between items-center'):
            ui.label('OPEN POSITIONS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')
            ui_refs['open_positions_count'] = ui.label('0 positions').classes('text-[10px] text-gray-400')
        _position_row('Call', on_close=on_close_call)
        _position_row('Put', on_close=on_close_put)
        empty_lbl = ui.label('No open positions.').classes('w-full text-center text-xs text-gray-400 italic')
        empty_lbl.bind_visibility_from(shared_state['active_trades'], 'Call',
                                        backward=lambda v: v is None and shared_state['active_trades'].get('Put') is None)

# --- ORDER BOOK (pending unified entry triggers + active exit orders, table-styled) ---

def _toggle_expansion(exp):
    exp.value = not exp.value

def _orderbook_table_row(side, prefix):
    """One expandable row for a pending unified Open Short/Long entry trigger (Limit/Stop-
    Market only; Market fires immediately and never appears here). The header line stays
    live-bound to the underlying params (no manual refresh needed). MODIFY opens a staged
    edit panel (trigger price, strike/qty with -/+ steppers, fire-on, stop, target) bound to
    a local draft, not the live params directly -- edits only take effect after CONFIRM.
    CONFIRM re-stamps armed_at (see logic_engine._check_unified_open) so a modified order's
    timing window restarts from the moment it was confirmed, not the original arm time.
    REMOVE fully resets that side's card back to defaults immediately (no confirm needed,
    matching the existing Cancel behavior elsewhere).

    Side label (SELL/BUY) is mode-aware: Sell Mode entry orders open a SHORT position (so the
    order itself is a SELL), Buy Mode entry orders open a LONG position (so the order itself
    is a BUY) -- this reflects options_buy_mode live, since a pending (not yet filled) order
    has no trade dict yet to read a fixed direction from."""
    opt_type = 'CE' if side == 'Call' else 'PE'
    draft = {}

    def sync_draft():
        draft['order_type'] = params[f'{prefix}_order_type']
        draft['trigger_price'] = params[f'{prefix}_trigger_price']
        draft['strike_offset'] = params[f'{prefix}_strike_offset']
        draft['qty'] = params[f'{prefix}_qty']
        draft['fire_on'] = params[f'{prefix}_fire_on']
        draft['new_stop'] = params[f'{prefix}_new_stop']
        draft['new_target'] = params[f'{prefix}_new_target']

    sync_draft()

    def _txn_side_label(buy_mode):
        return 'BUY' if buy_mode else 'SELL'
    def _txn_side_cls(buy_mode):
        return 'w-14 text-blue-600 font-bold' if buy_mode else 'w-14 text-red-600 font-bold'

    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, f'{prefix}_armed')
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: INDICES.get(v, {}).get('segment', v)).classes('w-16 text-gray-400 font-mono')
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: f"{v} {opt_type}").classes('w-28 font-bold text-gray-800 font-mono')
                    txn_lbl = ui.label(_txn_side_label(params.get('options_buy_mode', False))).classes(_txn_side_cls(params.get('options_buy_mode', False)))
                    txn_lbl.bind_text_from(params, 'options_buy_mode', backward=_txn_side_label)
                    txn_lbl.bind_visibility_from(params, 'options_buy_mode', backward=lambda v: True)  # keep visible; classes set once at build, acceptable since mode can't change with orders armed
                    ui.label().bind_text_from(params, f'{prefix}_order_type').classes('w-24 text-purple-700')
                    ui.label().bind_text_from(params, f'{prefix}_trigger_price', backward=lambda v: f"{v}").classes('w-24 text-right font-mono text-gray-800')
                    ui.label().bind_text_from(params, f'{prefix}_fire_on').classes('w-16 text-gray-500')
                    ui.label().bind_text_from(params, f'{prefix}_new_stop', backward=lambda v: (str(v) if str(v).strip() != '' else '-')).classes('w-20 text-orange-600 text-right font-mono')
                    ui.label().bind_text_from(params, f'{prefix}_new_target', backward=lambda v: (str(v) if str(v).strip() != '' else '-')).classes('w-20 text-blue-600 text-right font-mono')
                    ui.label().bind_text_from(params, f'{prefix}_qty').classes('w-14 text-right font-mono text-gray-800')
                    ui.label('WORKING').classes('bg-blue-600 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                    ui.space()

                    def cancel_order():
                        _reset_unified_card_defaults(prefix)
                        ui.notify(f"{side} Order Removed", type='info')

                    def open_modify():
                        sync_draft()  # always start the edit from the current committed values
                        exp.value = True

                    # click.stop so these don't also trigger the header's own expand/collapse
                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', cancel_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2'):
                    ui.radio(UI_OPTS['order_types'], value=draft['order_type']).bind_value(draft, 'order_type').props('inline dense')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Trigger Price').bind_value(draft, 'trigger_price').props('outlined dense bg-color=white').classes('grow')
                    _num_stepper(draft, 'strike_offset', step=1, label='Strike (0=ATM,1=ITM,-1=OTM)')
                with ui.row().classes('w-full gap-2'):
                    _num_stepper(draft, 'qty', step=1, label='Qty (Lots)')
                    ui.select(UI_OPTS['fire_on_opts'], value=draft['fire_on']).bind_value(draft, 'fire_on').props('outlined dense bg-color=white').classes('grow')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Stop (optional)').bind_value(draft, 'new_stop').props('outlined dense bg-color=white').classes('grow')
                    ui.input('Target (optional)').bind_value(draft, 'new_target').props('outlined dense bg-color=white').classes('grow')

                def confirm_changes():
                    params[f'{prefix}_order_type'] = draft['order_type']
                    params[f'{prefix}_trigger_price'] = draft['trigger_price']
                    params[f'{prefix}_strike_offset'] = draft['strike_offset']
                    params[f'{prefix}_qty'] = draft['qty']
                    params[f'{prefix}_fire_on'] = draft['fire_on']
                    params[f'{prefix}_new_stop'] = draft['new_stop']
                    params[f'{prefix}_new_target'] = draft['new_target']
                    # Re-stamp arm time: a modified order's timing window should restart from
                    # now, matching fresh-arm behavior (see unified_entry_card.fire_or_arm).
                    params[f'{prefix}_armed_at'] = datetime.now()
                    ui.notify(f"{side} Order Updated", type='positive')
                    exp.value = False

                def discard_changes():
                    exp.value = False  # just close; draft is resynced next time MODIFY is clicked

                with ui.row().classes('w-full gap-2'):
                    ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                    ui.button('Cancel', on_click=discard_changes).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')

def _exit_order_row(side, order_label, value_key, active_key, time_key=None, is_target=False):
    """One expandable row for an active conditional EXIT order tied to an open position --
    from the Open Positions Idx Stop/Target quick controls, or the Premium/Index-based exit
    cards. Visible only while active. Columns match the SAME layout as the entry-order rows
    above (Exch/Symbol/Side/Type/Trigger Price/Fire On/Stop/Target/Qty/Status). Side reads
    the OPEN TRADE's own recorded direction when available (SELL trade -> exit order is BUY
    to cover; BUY trade -> exit order is SELL to close), falling back to the live buy_mode
    flag only if no trade is present. Symbol and Qty are read from the live open position
    itself (not guessed), so they always match the real trade. MODIFY edits the value/period
    inline; REMOVE deactivates and clears the value, mirroring the Reset behavior already in
    premium_exit_card/index_exit_component."""
    opt_type = 'CE' if side == 'Call' else 'PE'
    draft = {}

    def sync_draft():
        draft['value'] = params[value_key]
        if time_key: draft['time'] = params[time_key]

    sync_draft()

    def _symbol(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade: return f"{params['trading_index']} {int(trade['main']['strike'])} {opt_type}"
        return f"{params['trading_index']} {opt_type}"

    def _qty(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade: return str(trade['qty'])
        prefix = 'call' if side == 'Call' else 'put'
        return str(params.get(f'{prefix}_qty', '-'))

    def _exit_txn_label(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade is not None:
            return 'SELL' if trade.get('direction', 'SELL') == 'BUY' else 'BUY'
        return 'SELL' if params.get('options_buy_mode', False) else 'BUY'

    def _fire_on_label(v):
        return 'Live' if v == 'Current' else v  # cosmetic only: matches entry-card wording

    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, active_key)
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: INDICES.get(v, {}).get('segment', v)).classes('w-16 text-gray-400 font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_symbol).classes('w-28 font-bold text-gray-800 font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_exit_txn_label).classes('w-14 text-green-600 font-bold')
                    ui.label(order_label).classes('w-24 text-purple-700 font-semibold')
                    ui.label('-').classes('w-24 text-right font-mono text-gray-400')  # Trigger Price: n/a for exit orders
                    if time_key:
                        ui.label().bind_text_from(params, time_key, backward=_fire_on_label).classes('w-16 text-gray-500')
                    else:
                        ui.label('Live').classes('w-16 text-gray-400')
                    ui.label().bind_text_from(params, value_key, backward=lambda v: (str(v) if (not is_target and str(v).strip() not in ('', '0')) else '-')).classes('w-20 text-orange-600 text-right font-mono')
                    ui.label().bind_text_from(params, value_key, backward=lambda v: (str(v) if (is_target and str(v).strip() not in ('', '0')) else '-')).classes('w-20 text-blue-600 text-right font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_qty).classes('w-14 text-right font-mono text-gray-800')
                    ui.label('WORKING').classes('bg-blue-600 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                    ui.space()

                    def remove_order():
                        params[active_key] = False
                        params[value_key] = 0
                        ui.notify(f"{side} {order_label} Removed", type='info')

                    def open_modify():
                        sync_draft()
                        exp.value = True

                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', remove_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2 items-center'):
                    ui.input('Value').bind_value(draft, 'value').props('outlined dense bg-color=white').classes('grow')
                    if time_key:
                        ui.radio(UI_OPTS['index_times'], value=draft['time']).bind_value(draft, 'time').props('inline dense')

                def confirm_changes():
                    params[value_key] = draft['value']
                    if time_key: params[time_key] = draft['time']
                    ui.notify(f"{side} {order_label} Updated", type='positive')
                    exp.value = False

                with ui.row().classes('w-full gap-2'):
                    ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                    ui.button('Cancel', on_click=lambda: setattr(exp, 'value', False)).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800')

def render_orderbook():
    """Full-width Open Orders table (white background, matching the rest of the app): pending
    unified entry triggers (Limit/Stop-Market; Market fires immediately so never appears here)
    plus every active conditional exit order (Premium-based and Index-based stop/target,
    including the Open Positions quick Idx Stop/Target controls, since they share the same
    underlying params)."""
    with ui.card().classes('w-full bg-white p-3 gap-2 rounded-xl shadow-sm mb-4 border border-gray-200'):
        ui.label('OPEN ORDERS').classes('font-bold text-xs uppercase tracking-widest text-gray-500 mb-1')
        with ui.row().classes('w-full items-center gap-3 text-[10px] text-gray-400 uppercase px-2'):
            ui.label('Exch').classes('w-16'); ui.label('Symbol').classes('w-28'); ui.label('Side').classes('w-14')
            ui.label('Type').classes('w-24'); ui.label('Trigger Price').classes('w-24 text-right'); ui.label('Fire On').classes('w-16')
            ui.label('Stop').classes('w-20 text-right'); ui.label('Target').classes('w-20 text-right'); ui.label('Qty').classes('w-14 text-right'); ui.label('Status').classes('')

        _orderbook_table_row('Call', 'call')
        _orderbook_table_row('Put', 'put')

        # Exit orders: Premium-based, Index-based (this also covers the Open Positions'
        # quick Idx Stop/Target controls, since those write to the same call_index_*/
        # put_index_* params).
        _exit_order_row('Call', 'Prem Stop', 'call_prem_stop_val', 'call_prem_stop_active', 'call_prem_stop_time', is_target=False)
        _exit_order_row('Call', 'Prem Target', 'call_prem_target_val', 'call_prem_tgt_active', 'call_prem_target_time', is_target=True)
        _exit_order_row('Call', 'Idx Stop', 'call_index_stop_val', 'call_index_stop_active', 'call_index_stop_time', is_target=False)
        _exit_order_row('Call', 'Idx Target', 'call_index_target_val', 'call_index_tgt_active', 'call_index_target_time', is_target=True)
        _exit_order_row('Put', 'Prem Stop', 'put_prem_stop_val', 'put_prem_stop_active', 'put_prem_stop_time', is_target=False)
        _exit_order_row('Put', 'Prem Target', 'put_prem_target_val', 'put_prem_tgt_active', 'put_prem_target_time', is_target=True)
        _exit_order_row('Put', 'Idx Stop', 'put_index_stop_val', 'put_index_stop_active', 'put_index_stop_time', is_target=False)
        _exit_order_row('Put', 'Idx Target', 'put_index_target_val', 'put_index_tgt_active', 'put_index_target_time', is_target=True)

        empty_lbl = ui.label('No pending orders.').classes('w-full text-center text-xs text-gray-400 italic')

        def _nothing_active(_v=None):
            return not (
                params.get('call_armed') or params.get('put_armed') or
                params.get('call_prem_stop_active') or params.get('call_prem_tgt_active') or
                params.get('call_index_stop_active') or params.get('call_index_tgt_active') or
                params.get('put_prem_stop_active') or params.get('put_prem_tgt_active') or
                params.get('put_index_stop_active') or params.get('put_index_tgt_active')
            )
        empty_lbl.bind_visibility_from(params, 'call_armed', backward=_nothing_active)
        ui_refs['orderbook_empty'] = empty_lbl

# --- ORDER HISTORY (full options_tradebook.csv) ---

def _refresh_history_table():
    try:
        df = pd.read_csv(TRADEBOOK_FILE)
        df = df.iloc[::-1]  # most recent (last appended) first
        cols = [{'name': c, 'label': c.replace('_', ' '), 'field': c, 'sortable': True, 'align': 'left'} for c in df.columns]
        rows = df.to_dict('records')
        tbl = ui_refs.get('history_table')
        if tbl:
            tbl.columns = cols
            tbl.rows = rows
            tbl.update()
    except Exception:
        pass  # e.g. file not yet created; table just stays empty

def render_order_history():
    """Full-width Order History: the complete, all-time options_tradebook.csv log."""
    with ui.card().classes('w-full p-2 gap-2 border border-gray-300 rounded-xl shadow-sm mb-4'):
        with ui.row().classes('w-full justify-between items-center'):
            ui.label('ORDER HISTORY (All Trades)').classes('font-bold text-sm text-gray-700')
            ui.button('Refresh', on_click=_refresh_history_table).props('dense flat').classes('text-xs')
        with ui.scroll_area().classes('w-full h-64'):
            ui_refs['history_table'] = ui.table(columns=[], rows=[], row_key='Trade_ID').classes('w-full').props('dense flat bordered')
    _refresh_history_table()
    ui.timer(10.0, _refresh_history_table)

# --- HEADER / CHART / LOG ---

def render_master_banner(update_lots_callback):
    with ui.column().classes('w-full gap-0 mb-4'):
        with ui.card().classes('w-full p-3 bg-orange-200 text-orange-900 rounded-t-xl rounded-b-none border-b border-orange-300') as card:
            ui_refs['banner_card'] = card
            with ui.element('div').classes('w-full grid grid-cols-[1fr_auto] items-center gap-4'):
                with ui.row().classes('items-center gap-4 flex-nowrap'):
                    ui.label('Zerodha Trading Engine').classes('text-xl font-bold tracking-wide whitespace-nowrap')
                    ui_refs['monitor_status'] = ui.label('TRIGGERS OFF').classes('text-xs font-bold bg-gray-800 text-gray-400 px-2 py-1 rounded whitespace-nowrap')
                    ui.switch('Mute', value=params['mute_sound']).bind_value(params, 'mute_sound').props('color=red dense')
                    ui.button('🔊 Enable Sound', on_click=lambda: ui.run_javascript(
                        'try { const a = new Audio("https://actions.google.com/sounds/v1/cartoon/pop.ogg"); '
                        'a.volume = 0.4; a.play().catch(()=>{}); } catch(e) {}'
                    )).props('dense flat size=sm').classes('text-[10px] text-orange-900')
                with ui.row().classes('gap-6 items-center flex-nowrap justify-end'):
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Unrealized PnL').classes('text-orange-800 text-[10px] uppercase tracking-wider whitespace-nowrap')
                        ui_refs['pnl_unrealized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-gray-800 leading-none')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Realized PnL').classes('text-orange-800 text-[10px] uppercase tracking-wider whitespace-nowrap')
                        ui_refs['pnl_realized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-green-700 leading-none')

        with ui.card().classes('w-full p-2 bg-orange-50 flex-row items-center gap-6 rounded-none border-x border-orange-200'):
            with ui.row().classes('items-center gap-2'):
                ui.label('Index:').classes('font-bold text-orange-900 text-xs')
                ui.radio(UI_OPTS['indices'], value=params['trading_index'], on_change=update_lots_callback).bind_value(params, 'trading_index').props('inline dense')
            with ui.row().classes('items-center gap-2'):
                ui.label('Live Trading:').classes('font-bold text-orange-900 text-xs ml-4')
                ui.radio(UI_OPTS['on_off'], value=params['live_trading']).bind_value(params, 'live_trading').props('inline dense')

        with ui.card().classes('w-full p-1 px-3 bg-gray-100 border-t border-gray-300 rounded-none'):
            with ui.row().classes('items-center gap-2'):
                ui.label('LAST ACTION:').classes('text-[10px] font-bold text-gray-500')
                ui_refs['last_action'] = ui.label('System Ready').classes('font-mono text-xs font-bold text-orange-600')

        with ui.row().classes('w-full gap-0 border border-gray-300 rounded-none overflow-hidden shadow-sm'):
            with ui.card().classes('w-1/2 p-2 bg-red-50 border-r border-gray-300 rounded-none gap-1'):
                with ui.row().classes('justify-between items-center w-full border-b border-red-200 pb-1 mb-1'):
                    ui.label('CALL POSITION').classes('text-xs font-bold text-red-900')
                    ui_refs['call_status'] = ui.label('INACTIVE').classes('text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
                ui_refs['call_info'] = ui.label('Time: --').classes('text-[9px] font-mono text-gray-600')
                ui_refs['call_trigger'] = ui.label('Trig: --').classes('text-[9px] font-mono text-red-800')
                with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-1 items-center'):
                    ui.label('Inst').classes('text-[10px] font-bold text-gray-400'); ui.label('Open').classes('text-[10px] font-bold text-gray-400 text-right'); ui.label('Curr').classes('text-[10px] font-bold text-gray-400 text-right')
                    ui_refs['call_main_strike'] = ui.label('-').classes('text-xs font-bold text-red-900')
                    ui_refs['call_main_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['call_main_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui_refs['call_hedge_strike'] = ui.label('-').classes('text-xs font-bold text-red-700')
                    ui_refs['call_hedge_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['call_hedge_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui.label('INDEX').classes('text-xs font-bold text-gray-500')
                    ui_refs['call_idx_open'] = ui.label('0').classes('text-xs font-mono text-gray-500 text-right')
                    ui_refs['call_idx_curr'] = ui.label('0').classes('text-xs font-mono font-bold text-gray-700 text-right')
                with ui.row().classes('w-full justify-between items-center mt-2 pt-1 border-t border-red-200'):
                    ui.label('RUNNING PnL').classes('text-[10px] font-bold text-gray-400')
                    ui_refs['call_pnl'] = ui.label('₹ 0').classes('text-xl font-bold text-gray-400 font-mono')

            with ui.card().classes('w-1/2 p-2 bg-green-50 rounded-none gap-1'):
                with ui.row().classes('justify-between items-center w-full border-b border-green-200 pb-1 mb-1'):
                    ui.label('PUT POSITION').classes('text-xs font-bold text-green-900')
                    ui_refs['put_status'] = ui.label('INACTIVE').classes('text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
                ui_refs['put_info'] = ui.label('Time: --').classes('text-[9px] font-mono text-gray-600')
                ui_refs['put_trigger'] = ui.label('Trig: --').classes('text-[9px] font-mono text-green-800')
                with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-1 items-center'):
                    ui.label('Inst').classes('text-[10px] font-bold text-gray-400'); ui.label('Open').classes('text-[10px] font-bold text-gray-400 text-right'); ui.label('Curr').classes('text-[10px] font-bold text-gray-400 text-right')
                    ui_refs['put_main_strike'] = ui.label('-').classes('text-xs font-bold text-green-900')
                    ui_refs['put_main_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['put_main_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui_refs['put_hedge_strike'] = ui.label('-').classes('text-xs font-bold text-green-700')
                    ui_refs['put_hedge_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['put_hedge_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui.label('INDEX').classes('text-xs font-bold text-gray-500')
                    ui_refs['put_idx_open'] = ui.label('0').classes('text-xs font-mono text-gray-500 text-right')
                    ui_refs['put_idx_curr'] = ui.label('0').classes('text-xs font-mono font-bold text-gray-700 text-right')
                with ui.row().classes('w-full justify-between items-center mt-2 pt-1 border-t border-green-200'):
                    ui.label('RUNNING PnL').classes('text-[10px] font-bold text-gray-400')
                    ui_refs['put_pnl'] = ui.label('₹ 0').classes('text-xl font-bold text-gray-400 font-mono')

def render_chart_row():
    with ui.card().classes('w-full h-64 p-2 border-x border-gray-300 rounded-none shadow-sm'):
        ui.label('Real-Time PnL Curve').classes('text-xs font-bold text-gray-500 mb-2')
        ui_refs['pnl_chart'] = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'grid': {'top': 30, 'bottom': 20, 'left': 50, 'right': 20},
            'xAxis': {'type': 'category', 'data': [], 'axisLine': {'lineStyle': {'color': '#9ca3af'}}},
            'yAxis': {'type': 'value', 'scale': True, 'splitLine': {'lineStyle': {'color': '#e5e7eb'}}},
            'backgroundColor': '#f9fafb',
            'dataZoom': [{'type': 'inside', 'start': 0, 'end': 100}, {'type': 'slider'}],
            'series': [{
                'name': 'Total PnL', 'type': 'line', 'data': [], 'smooth': True, 'showSymbol': False,
                'lineStyle': {'color': '#f97316', 'width': 2}, 'areaStyle': {'color': '#ffedd5', 'opacity': 0.5},
                'markPoint': {'data': [], 'symbolSize': 25, 'label': {'fontSize': 8, 'color': 'white'}}
            }]
        })

def render_log_row():
    with ui.card().classes('w-full p-0 gap-0 border-x border-b border-gray-300 rounded-b-xl overflow-hidden shadow-sm mb-4'):
        ui.label('TRADE EVENT LOG').classes('text-xs font-bold text-gray-300 bg-gray-800 w-full p-2 border-b border-gray-700')
        with ui.scroll_area().classes('w-full h-32 bg-gray-900 p-2'):
            ui_refs['activity_log_container'] = ui.column().classes('gap-1')

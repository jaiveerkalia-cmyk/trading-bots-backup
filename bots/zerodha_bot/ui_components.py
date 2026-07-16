from nicegui import ui
from config import params, UI_OPTS, ui_refs, TRADEBOOK_FILE, INDICES, shared_state
import pandas as pd

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
    params[f'{prefix}_order_type'] = 'Market'
    params[f'{prefix}_trigger_price'] = 0
    params[f'{prefix}_strike_offset'] = 1
    params[f'{prefix}_qty'] = 4
    params[f'{prefix}_fire_on'] = 'Live'
    params[f'{prefix}_new_stop'] = ''
    params[f'{prefix}_new_target'] = ''

def unified_entry_card(side, prefix, on_fire_market=None, on_close=None):
    """Unified Open Short/Long card: index-based entry with order type (Market/Limit/
    Stop-Market), trigger price, strike offset (0=ATM, 1=ITM, -1=OTM), optional stop/target,
    a fire-on timeframe, and its own qty. Market fires immediately via on_fire_market.
    Limit/Stop-Market arm the trade; index conditions are then checked and fired by
    LogicEngine._check_unified_open in logic_engine.py."""
    color_class = 'bg-red-100 border-red-300' if side == 'Call' else 'bg-green-100 border-green-300'
    btn_color = 'red' if side == 'Call' else 'green'
    title = 'Open Short' if side == 'Call' else 'Open Long'
    order_key = f'{prefix}_order_type'; trig_key = f'{prefix}_trigger_price'
    strike_key = f'{prefix}_strike_offset'; qty_key = f'{prefix}_qty'
    fire_key = f'{prefix}_fire_on'; armed_key = f'{prefix}_armed'
    stop_key = f'{prefix}_new_stop'; target_key = f'{prefix}_new_target'

    with ui.card().classes(f'w-full p-3 gap-2 {color_class} border shadow-md rounded-xl'):
        ui.label(title).classes('font-bold text-sm uppercase text-gray-800')

        with ui.row().classes('w-full justify-start'):
            ui.radio(UI_OPTS['order_types'], value=params[order_key]).bind_value(params, order_key).props('inline dense')

        with ui.row().classes('w-full gap-2'):
            # NOTE: intentionally NOT disabled for Market (previously used bind_enabled_from,
            # which could leave a typed value uncommitted after a disable->enable toggle on some
            # NiceGUI/Quasar versions). Always-editable avoids that class of binding bug; the
            # value is simply ignored by the firing logic when order type is Market.
            ui.input('Trigger Price').bind_value(params, trig_key).props('outlined dense bg-color=white').classes('grow')
            ui.input('Strike (0=ATM,1=ITM,-1=OTM)').bind_value(params, strike_key).props('outlined dense bg-color=white').classes('grow')

        with ui.row().classes('w-full gap-2'):
            ui.input('Qty (Lots)').bind_value(params, qty_key).props('outlined dense bg-color=white').classes('grow')
            ui.select(UI_OPTS['fire_on_opts'], value=params[fire_key]).bind_value(params, fire_key).props('outlined dense bg-color=white').classes('grow')

        with ui.row().classes('w-full gap-2'):
            ui.input('Stop (optional)').bind_value(params, stop_key).props('outlined dense bg-color=white').classes('grow')
            ui.input('Target (optional)').bind_value(params, target_key).props('outlined dense bg-color=white').classes('grow')

        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-green-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, armed_key)

        def fire_or_arm():
            if params[order_key] == 'Market':
                if on_fire_market: on_fire_market()
            else:
                params[armed_key] = True
                status.set_text(f"ARMED: {params[order_key]} @ {params[trig_key]} ({params[fire_key]})")
                ui.notify(f"{title} ARMED", type='positive')

        def cancel():
            # Full reset: trigger price + every other field back to default (not just disarm).
            _reset_unified_card_defaults(prefix)
            ui.notify(f"{title} Cancelled & Reset", type='info')

        with ui.row().classes('w-full gap-2'):
            fire_btn = ui.button('Open Now', color=btn_color, on_click=fire_or_arm).classes('grow h-8 text-xs rounded-lg shadow-sm')
            fire_btn.bind_text_from(params, order_key, backward=lambda v: 'Open Now' if v == 'Market' else 'Arm')
            ui.button('Cancel', on_click=cancel).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')
            ui.button('Close', on_click=on_close).classes('grow h-8 text-xs rounded-lg bg-gray-300 text-gray-800 hover:bg-gray-400')

def auto_close_card(side, target_val_key, target_active_key, stop_val_key, stop_active_key):
    color_class = 'bg-red-50 border-red-200' if side == 'Call' else 'bg-green-50 border-green-200'
    with ui.card().classes(f'w-full p-2 gap-2 {color_class} border shadow-sm rounded-xl'):
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
    color_class = 'bg-red-50 border-red-200' if side == 'Call' else 'bg-green-50 border-green-200'
    with ui.card().classes(f'w-full p-2 gap-1 {color_class} border rounded-lg'):
        ui.label(label).classes('font-bold text-xs text-gray-600')
        with ui.row().classes('items-center justify-between w-full'):
            ui.input().bind_value(params, value_key).props('outlined dense bg-color=white').classes('w-24')
            ui.radio(UI_OPTS['index_times'], value=params[time_key]).bind_value(params, time_key).props('inline dense scale=0.8')
        status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
        status.bind_visibility_from(params, active_key)
        def activate():
            params[active_key] = True; status.set_text(f"ON: {params[value_key]}")
            ui.notify(f"{side} Index {label} SET", type='positive')
        def reset():
            params[active_key] = False; params[value_key] = 0
            ui.notify(f"{side} Index {label} RESET", type='info')
        with ui.row().classes('w-full gap-1 mt-1'):
            ui.button('Set', color='black', on_click=activate).props('outline').classes('grow h-6 text-[10px] rounded')
            ui.button('Reset', on_click=reset).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

def premium_exit_card(side):
    """Exit based on the live LTP of the main (short) option leg."""
    color_class = 'bg-red-50 border-red-200' if side == 'Call' else 'bg-green-50 border-green-200'
    label_color = 'text-red-800' if side == 'Call' else 'text-green-800'
    s = side.lower()
    with ui.card().classes(f'w-full p-2 gap-2 {color_class} border shadow-sm rounded-xl'):
        ui.label(f'{side} Exit based on Option Premium').classes(f'font-bold text-xs uppercase {label_color}')
        with ui.row().classes('w-full gap-2'):
            # Stop sub-card
            with ui.card().classes(f'w-full p-2 gap-1 {color_class} border rounded-lg'):
                ui.label('Stop').classes('font-bold text-xs text-gray-600')
                with ui.row().classes('items-center justify-between w-full'):
                    ui.input().bind_value(params, f'{s}_prem_stop_val').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_stop_time']).bind_value(params, f'{s}_prem_stop_time').props('inline dense scale=0.8')
                stop_status = ui.label().classes('w-full text-center text-[10px] font-bold text-red-800 bg-red-100 rounded')
                stop_status.bind_visibility_from(params, f'{s}_prem_stop_active')
                def make_stop_handlers(sd, ss):
                    def activate():
                        params[f'{sd}_prem_stop_active'] = True
                        ss.set_text(f"ON: {params[f'{sd}_prem_stop_val']}")
                        ui.notify(f"{sd} Prem Stop SET", type='positive')
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
            with ui.card().classes(f'w-full p-2 gap-1 {color_class} border rounded-lg'):
                ui.label('Tgt').classes('font-bold text-xs text-gray-600')
                with ui.row().classes('items-center justify-between w-full'):
                    ui.input().bind_value(params, f'{s}_prem_target_val').props('outlined dense bg-color=white').classes('w-24')
                    ui.radio(UI_OPTS['index_times'], value=params[f'{s}_prem_target_time']).bind_value(params, f'{s}_prem_target_time').props('inline dense scale=0.8')
                tgt_status = ui.label().classes('w-full text-center text-[10px] font-bold text-green-800 bg-green-100 rounded')
                tgt_status.bind_visibility_from(params, f'{s}_prem_tgt_active')
                def make_tgt_handlers(sd, ts):
                    def activate():
                        params[f'{sd}_prem_tgt_active'] = True
                        ts.set_text(f"ON: {params[f'{sd}_prem_target_val']}")
                        ui.notify(f"{sd} Prem Target SET", type='positive')
                    def reset():
                        params[f'{sd}_prem_tgt_active'] = False
                        params[f'{sd}_prem_target_val'] = 0
                        ui.notify(f"{sd} Prem Target RESET", type='info')
                    return activate, reset
                act_t, rst_t = make_tgt_handlers(s, tgt_status)
                with ui.row().classes('w-full gap-1 mt-1'):
                    ui.button('Set', color='black', on_click=act_t).props('outline').classes('grow h-6 text-[10px] rounded')
                    ui.button('Reset', on_click=rst_t).classes('grow h-6 text-[10px] rounded bg-gray-200 text-gray-800 hover:bg-gray-300')

def _alert_side_card(side_label, threshold_key, threshold_input_key, active_key, period_key, sound_key, duration_key, notify_fn):
    """Shared builder for a single-sided (Upper or Lower) price alert card."""
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label(f'{side_label} Price Alert').classes('font-bold text-gray-800')

        with ui.row().classes('items-center w-full justify-between'):
            ui.label('Period:').classes('text-xs text-gray-500')
            ui.radio(UI_OPTS['alert_periods'], value=params[period_key]).bind_value(params, period_key).props('inline dense')

        ui.input(side_label).bind_value(params, threshold_input_key).props('outlined dense bg-color=white').classes('w-full')

        with ui.row().classes('w-full gap-2'):
            ui.select(UI_OPTS['alert_sounds'], value=params[sound_key], label='Sound').bind_value(params, sound_key).props('outlined dense bg-color=white').classes('grow')
            ui.input('Duration (s)').bind_value(params, duration_key).props('outlined dense bg-color=white').classes('w-28')

        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-orange-500 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, active_key)

        def set_alert():
            try:
                params[threshold_key] = float(params[threshold_input_key])
                params[active_key] = True if params[threshold_key] > 0 else False
                status.set_text(f"ARMED: {params[threshold_key]}")
                notify_fn(f"{side_label} Alert ARMED: {params[threshold_key]}", type='positive')
            except: notify_fn("Invalid Alert Value", type='negative')

        def reset_alert():
            params[threshold_key] = 0; params[threshold_input_key] = 0; params[active_key] = False
            notify_fn(f"{side_label} Alert DISARMED", type='info')

        with ui.row().classes('w-full gap-2'):
            ui.button('Set', color='orange', on_click=set_alert).classes('grow h-8 rounded-lg')
            ui.button('Reset', color='grey', on_click=reset_alert).props('flat').classes('grow h-8 rounded-lg')

def alerts_card_upper():
    _alert_side_card('Upper', 'alert_upper', 'alert_upper_input', 'alert_upper_active',
                      'alert_upper_period', 'alert_upper_sound', 'alert_upper_duration', ui.notify)

def alerts_card_lower():
    _alert_side_card('Lower', 'alert_lower', 'alert_lower_input', 'alert_lower_active',
                      'alert_lower_period', 'alert_lower_sound', 'alert_lower_duration', ui.notify)

# --- OPEN POSITIONS (kept alongside the existing banner CALL/PUT POSITION cards) ---

def _position_row(side, on_close=None):
    """One row of the Open Positions section. Only visible while that side has an active
    trade. Values (mark/size/pnl/entry/qty/symbol) are populated live each tick by
    auto_run.py's update_ui(), the same pattern already used for the banner cards.

    Stop/Target here control the INDEX-PRICE-based exit (call_index_stop_val/
    call_index_stop_active etc, the same params the 'Exit based on Index' cards use) rather
    than the PnL-based Auto Close values, and always check on live price: toggling either
    switch forces the corresponding *_index_stop_time/*_index_target_time to 'Current'."""
    prefix = 'call' if side == 'Call' else 'put'
    accent = 'border-red-500' if side == 'Call' else 'border-green-500'
    stop_val_key = f'{prefix}_index_stop_val'; stop_active_key = f'{prefix}_index_stop_active'; stop_time_key = f'{prefix}_index_stop_time'
    tgt_val_key = f'{prefix}_index_target_val'; tgt_active_key = f'{prefix}_index_tgt_active'; tgt_time_key = f'{prefix}_index_target_time'

    def force_live(_e=None, key=None):
        params[key] = 'Current'

    with ui.card().classes(f'w-full bg-white border-l-4 {accent} border border-gray-200 rounded-lg p-3 gap-2 shadow-sm') as row:
        row.bind_visibility_from(shared_state['active_trades'], side, backward=lambda v: v is not None)
        ui_refs[f'{prefix}_pos_row'] = row

        with ui.row().classes('w-full justify-between items-center flex-wrap gap-2'):
            with ui.row().classes('items-center gap-2'):
                ui_refs[f'{prefix}_pos_symbol'] = ui.label('-').classes('text-gray-800 font-bold text-sm font-mono')
                ui.label('SHORT').classes('bg-red-100 text-red-700 text-[10px] font-bold px-2 py-0.5 rounded')
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
    live-bound to the underlying params (no manual refresh needed). MODIFY toggles the
    editable panel (trigger price, strike, qty, fire-on, stop, target); REMOVE fully resets
    that side's card back to defaults."""
    opt_type = 'CE' if side == 'Call' else 'PE'
    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, f'{prefix}_armed')
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: INDICES.get(v, {}).get('segment', v)).classes('w-16 text-gray-400 font-mono')
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: f"{v} {opt_type}").classes('w-28 font-bold text-gray-800 font-mono')
                    ui.label('SELL').classes('w-14 text-red-600 font-bold')
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

                    # click.stop so these don't also trigger the header's own expand/collapse
                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', lambda: _toggle_expansion(exp))
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', cancel_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2'):
                    ui.radio(UI_OPTS['order_types'], value=params[f'{prefix}_order_type']).bind_value(params, f'{prefix}_order_type').props('inline dense')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Trigger Price').bind_value(params, f'{prefix}_trigger_price').props('outlined dense bg-color=white').classes('grow')
                    ui.input('Strike (0=ATM,1=ITM,-1=OTM)').bind_value(params, f'{prefix}_strike_offset').props('outlined dense bg-color=white').classes('grow')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Qty (Lots)').bind_value(params, f'{prefix}_qty').props('outlined dense bg-color=white').classes('grow')
                    ui.select(UI_OPTS['fire_on_opts'], value=params[f'{prefix}_fire_on']).bind_value(params, f'{prefix}_fire_on').props('outlined dense bg-color=white').classes('grow')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Stop (optional)').bind_value(params, f'{prefix}_new_stop').props('outlined dense bg-color=white').classes('grow')
                    ui.input('Target (optional)').bind_value(params, f'{prefix}_new_target').props('outlined dense bg-color=white').classes('grow')

def _exit_order_row(side, order_label, value_key, active_key, time_key=None):
    """One expandable row for an active conditional EXIT order tied to an open position
    (from the Premium-based or Index-based exit cards). Visible only while active. MODIFY
    edits the value/period inline; REMOVE deactivates and clears the value, mirroring the
    Reset behavior already in premium_exit_card/index_exit_component."""
    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, active_key)
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label(side.upper()).classes('w-14 font-bold text-gray-700')
                    ui.label(order_label).classes('w-28 text-purple-700 font-semibold')
                    ui.label().bind_text_from(params, value_key, backward=lambda v: str(v) if str(v).strip() != '' else '-').classes('w-24 text-right font-mono text-gray-800')
                    if time_key:
                        ui.label().bind_text_from(params, time_key).classes('w-16 text-gray-500')
                    else:
                        ui.label('-').classes('w-16 text-gray-400')
                    ui.label('EXIT ORDER').classes('bg-orange-500 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                    ui.space()

                    def remove_order():
                        params[active_key] = False
                        params[value_key] = 0
                        ui.notify(f"{side} {order_label} Removed", type='info')

                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', lambda: _toggle_expansion(exp))
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', remove_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2 items-center'):
                    ui.input('Value').bind_value(params, value_key).props('outlined dense bg-color=white').classes('grow')
                    if time_key:
                        ui.radio(UI_OPTS['index_times'], value=params[time_key]).bind_value(params, time_key).props('inline dense')

def render_orderbook():
    """Full-width Open Orders table (white background, matching the rest of the app): pending
    unified entry triggers (Limit/Stop-Market; Market fires immediately so never appears here)
    plus every active conditional exit order (Premium-based and Index-based stop/target)."""
    with ui.card().classes('w-full bg-white p-3 gap-2 rounded-xl shadow-sm mb-4 border border-gray-200'):
        ui.label('OPEN ORDERS').classes('font-bold text-xs uppercase tracking-widest text-gray-500 mb-1')
        with ui.row().classes('w-full items-center gap-3 text-[10px] text-gray-400 uppercase px-2'):
            ui.label('Exch').classes('w-16'); ui.label('Symbol').classes('w-28'); ui.label('Side').classes('w-14')
            ui.label('Type').classes('w-24'); ui.label('Trigger Price').classes('w-24 text-right'); ui.label('Fire On').classes('w-16')
            ui.label('Stop').classes('w-20 text-right'); ui.label('Target').classes('w-20 text-right'); ui.label('Qty').classes('w-14 text-right'); ui.label('Status').classes('')

        _orderbook_table_row('Call', 'call')
        _orderbook_table_row('Put', 'put')

        # Exit orders from "Exit based on Option Premium" and "Exit based on Index" cards
        _exit_order_row('Call', 'Prem Stop', 'call_prem_stop_val', 'call_prem_stop_active', 'call_prem_stop_time')
        _exit_order_row('Call', 'Prem Target', 'call_prem_target_val', 'call_prem_tgt_active', 'call_prem_target_time')
        _exit_order_row('Call', 'Idx Stop', 'call_index_stop_val', 'call_index_stop_active', 'call_index_stop_time')
        _exit_order_row('Call', 'Idx Target', 'call_index_target_val', 'call_index_tgt_active', 'call_index_target_time')
        _exit_order_row('Put', 'Prem Stop', 'put_prem_stop_val', 'put_prem_stop_active', 'put_prem_stop_time')
        _exit_order_row('Put', 'Prem Target', 'put_prem_target_val', 'put_prem_tgt_active', 'put_prem_target_time')
        _exit_order_row('Put', 'Idx Stop', 'put_index_stop_val', 'put_index_stop_active', 'put_index_stop_time')
        _exit_order_row('Put', 'Idx Target', 'put_index_target_val', 'put_index_tgt_active', 'put_index_target_time')

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

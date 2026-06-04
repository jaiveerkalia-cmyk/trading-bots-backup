from nicegui import ui
from config import params, UI_OPTS, ui_refs

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

def alerts_card():
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label('Price Alerts').classes('font-bold text-gray-800')
        with ui.row().classes('items-center w-full justify-between'):
            ui.label('Period:').classes('text-xs text-gray-500')
            ui.radio(UI_OPTS['alert_periods'], value=params['alert_period']).bind_value(params, 'alert_period').props('inline dense')
        with ui.row().classes('w-full gap-2'):
            ui.input('Upper').bind_value(params, 'alert_upper_input').props('outlined dense bg-color=white').classes('grow')
            ui.input('Lower').bind_value(params, 'alert_lower_input').props('outlined dense bg-color=white').classes('grow')

        def set_alerts():
            try:
                params['alert_upper'] = float(params['alert_upper_input'])
                params['alert_lower'] = float(params['alert_lower_input'])
                params['alert_upper_active'] = True if params['alert_upper'] > 0 else False
                params['alert_lower_active'] = True if params['alert_lower'] > 0 else False
                ui.notify(f"Alerts ARMED: >{params['alert_upper']}, <{params['alert_lower']}", type='positive')
            except: ui.notify("Invalid Alert Values", type='negative')

        def reset_alerts():
            params['alert_upper'] = 0; params['alert_lower'] = 0
            params['alert_upper_input'] = 0; params['alert_lower_input'] = 0
            params['alert_upper_active'] = False; params['alert_lower_active'] = False
            ui.notify("Alerts DISARMED", type='info')

        with ui.row().classes('w-full gap-2'):
            ui.button('Set', color='orange', on_click=set_alerts).classes('grow h-8 rounded-lg')
            ui.button('Reset', color='grey', on_click=reset_alerts).props('flat').classes('grow h-8 rounded-lg')

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
                ui.label('Lots:').classes('font-bold text-orange-900 text-xs ml-4')
                ui.number(value=params['lots']).bind_value(params, 'lots').props('outlined dense bg-color=white').classes('w-20')
                ui_refs['calc_qty'] = ui.label('(Qty: --)').classes('text-xs font-mono text-gray-500')
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

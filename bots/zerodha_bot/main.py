from nicegui import ui
from auth_manager import get_kite_session
from ticker_engine import TickerClient
from instrument_manager import InstrumentManager
from logic_engine import LogicEngine
from config import shared_state, params, ui_refs, INDICES
import ui_components as comp
import sys
from datetime import datetime

class RedirectedStdout:
    def __init__(self): self.original_stdout = sys.stdout
    def write(self, message): self.original_stdout.write(message)
    def flush(self): self.original_stdout.flush()
    def isatty(self): return False
sys.stdout = RedirectedStdout()

print("--- STARTING BOT ---")
kite, api_key, access_token = get_kite_session()
if not kite: sys.exit(1)
ticker = TickerClient(api_key, access_token); inst_manager = InstrumentManager(kite)
logic = LogicEngine(ticker, inst_manager); ticker.start()

def run_daily_scan():
    global ticker; logic.log_action('Scanning...')
    if inst_manager.fetch_and_process_instruments():
        ticker.stop(); ticker = TickerClient(api_key, access_token); logic.ticker = ticker; ticker.start()
        logic.log_action('Instruments Ready.')
    else: logic.log_action('Scan Failed.')

def update_lots(e): 
    if e.value in INDICES: params['lots'] = 10 if e.value == 'NIFTY' else 12; ui.notify(f"Switched to {e.value}")

def handle_open(side):
    s, m = logic.open_position(side); ui.notify(m, type='positive' if s else 'negative')

def handle_close(side):
    s, m = logic.close_position(side); ui.notify(m, type='positive' if s else 'negative')

def handle_fire_market(side):
    """Fires the unified Open Short/Long card immediately when order type is Market."""
    prefix = 'call' if side == 'Call' else 'put'
    try: qty = int(float(params.get(f'{prefix}_qty', 4)))
    except: qty = 4
    try: strike_offset = float(params.get(f'{prefix}_strike_offset', 1))
    except: strike_offset = 1
    s, m = logic.open_position(side, reason="Market (Immediate)", qty_override=qty, strike_offset=strike_offset)
    if s:
        try:
            new_stop = float(params.get(f'{prefix}_new_stop', ''))
            if new_stop > 0:
                params[f'{prefix}_stop_val'] = new_stop
                params[f'{prefix}_stop_active'] = True
        except (ValueError, TypeError): pass
        try:
            new_target = float(params.get(f'{prefix}_new_target', ''))
            if new_target > 0:
                params[f'{prefix}_target_val'] = new_target
                params[f'{prefix}_target_active'] = True
        except (ValueError, TypeError): pass
    ui.notify(m, type='positive' if s else 'negative')

def reset_keys(keys, default=0):
    for k in keys: params[k] = default

def build_left_stack(): 
    comp.unified_entry_card('Call', 'call', on_fire_market=lambda: handle_fire_market('Call'), on_close=lambda: handle_close('Call'))
    # UPDATED: Passing independent keys
    comp.auto_close_card('Call', 'call_target_val', 'call_target_active', 'call_stop_val', 'call_stop_active')
    with ui.card().classes('w-full p-2 gap-2 bg-red-50 border border-red-200 shadow-sm rounded-xl'):
        ui.label('Call Exit based on Index').classes('font-bold text-xs uppercase text-red-800')
        with ui.row().classes('w-full gap-2'):
            comp.index_exit_component('Call', 'Stop', 'call_index_stop_time', 'call_index_stop_val', 'call_index_stop_active')
            comp.index_exit_component('Call', 'Tgt', 'call_index_target_time', 'call_index_target_val', 'call_index_tgt_active')

def build_center_stack():
    ui.button('Run 9 AM Daily Scan', on_click=run_daily_scan).classes('bg-orange-200 text-orange-900 w-full shadow-md rounded-xl h-12 font-bold')
    comp.global_control_card('Global Stop Loss', 'global_stop_value', 'global_stop_active')
    comp.global_control_card('Global Target', 'global_target_value', 'global_tgt_active')
    comp.alerts_card()
    ui.button('CLOSE ALL', on_click=lambda: logic.close_all_positions("Manual Close All", save_pnl=True), color='red').classes('w-full font-bold shadow-md rounded-xl mt-4')

def build_right_stack():
    comp.unified_entry_card('Put', 'put', on_fire_market=lambda: handle_fire_market('Put'), on_close=lambda: handle_close('Put'))
    # UPDATED: Passing independent keys
    comp.auto_close_card('Put', 'put_target_val', 'put_target_active', 'put_stop_val', 'put_stop_active')
    with ui.card().classes('w-full p-2 gap-2 bg-green-50 border border-green-200 shadow-sm rounded-xl'):
        ui.label('Put Exit based on Index').classes('font-bold text-xs uppercase text-green-800')
        with ui.row().classes('w-full gap-2'):
            comp.index_exit_component('Put', 'Stop', 'put_index_stop_time', 'put_index_stop_val', 'put_index_stop_active')
            comp.index_exit_component('Put', 'Tgt', 'put_index_target_time', 'put_index_target_val', 'put_index_tgt_active')

@ui.page('/', title='Zerodha Bot')
def index():
    comp.render_master_banner(update_lots)
    
    with ui.row().classes('w-full px-0 gap-0'): 
        comp.render_chart_row()
        comp.render_log_row()

    with ui.row().classes('w-full no-wrap items-start gap-4 p-2'):
        with ui.column().classes('grow gap-4'): build_left_stack()
        with ui.column().classes('grow gap-4'): build_center_stack()
        with ui.column().classes('grow gap-4'): build_right_stack()

    def update_dashboard():
        logic.check_triggers(); logic.update_pnl()
        
        now = datetime.now()
        if now.hour == 9 and now.minute == 0 and not shared_state['daily_scan_done']:
            run_daily_scan()
            shared_state['daily_scan_done'] = True
        if now.hour == 9 and now.minute == 1:
            shared_state['daily_scan_done'] = False
            shared_state['auto_sq_done'] = False

        while shared_state['reset_queue']:
            side = shared_state['reset_queue'].pop(0)
            prefix = 'call' if side == 'Call' else 'put'
            opp_prefix = 'short' if side == 'Call' else 'long'
            
            # Reset Flags
            params[f'{opp_prefix}_trigger_active'] = False
            params[f'{prefix}_target_active'] = False
            params[f'{prefix}_stop_active'] = False
            params[f'{prefix}_index_stop_active'] = False
            params[f'{prefix}_index_tgt_active'] = False
            
            # Reset Values (UPDATED with new keys)
            params[f'{opp_prefix}_open_amount'] = 0
            params[f'{opp_prefix}_open_strike'] = 0
            params[f'{prefix}_target_val'] = 0
            params[f'{prefix}_stop_val'] = 0
            params[f'{prefix}_index_stop_val'] = 0
            params[f'{prefix}_index_target_val'] = 0

            # Reset Unified Open Short/Long card fields
            params[f'{prefix}_armed'] = False
            params[f'{prefix}_trigger_price'] = 0
            params[f'{prefix}_new_stop'] = ''
            params[f'{prefix}_new_target'] = ''
            
            ui.notify(f"Reset {side} Controls", type='warning')

        if ui_refs['pnl_chart']:
            ui_refs['pnl_chart'].options['xAxis']['data'] = shared_state['chart_data']['times']
            ui_refs['pnl_chart'].options['series'][0]['data'] = shared_state['chart_data']['pnl']
            ui_refs['pnl_chart'].options['series'][0]['markPoint']['data'] = shared_state['chart_data']['markers']
            ui_refs['pnl_chart'].update()

        if ui_refs['activity_log_container']:
            ui_refs['activity_log_container'].clear()
            with ui_refs['activity_log_container']:
                for msg in shared_state['activity_log']: ui.label(msg).classes('text-[10px] font-mono text-green-400')

        if ui_refs['pnl_realized']:
            p = shared_state['pnl']['realized']; c = 'text-green-500' if p>=0 else 'text-red-500'
            ui_refs['pnl_realized'].set_text(f"₹ {p:.2f}"); ui_refs['pnl_realized'].classes(replace=f"text-2xl font-mono font-bold {c}")
        if ui_refs['pnl_unrealized']:
            p = shared_state['pnl']['unrealized']; c = 'text-green-600' if p>=0 else 'text-red-600'
            ui_refs['pnl_unrealized'].set_text(f"₹ {p:.2f}"); ui_refs['pnl_unrealized'].classes(replace=f"text-2xl font-bold font-mono {c}")

        if ui_refs['last_action']: ui_refs['last_action'].set_text(shared_state['last_action'])
        
        if ui_refs['monitor_status']:
            if params['short_trigger_active'] or params['long_trigger_active'] or params.get('call_armed') or params.get('put_armed'):
                ui_refs['monitor_status'].set_text('TRIGGERS ACTIVE'); ui_refs['monitor_status'].classes(replace='text-xs font-bold bg-green-500 text-white px-2 py-1 rounded animate-pulse')
            else:
                ui_refs['monitor_status'].set_text('TRIGGERS OFF'); ui_refs['monitor_status'].classes(replace='text-xs font-bold bg-gray-800 text-gray-400 px-2 py-1 rounded')

        if ui_refs['banner_card']:
            cls = 'w-full p-3 flex-row items-center justify-between rounded-t-xl rounded-b-none border-b '
            cls += 'bg-green-200 text-green-900 border-green-300' if params['live_trading'] == 'On' else 'bg-orange-200 text-orange-900 border-orange-300'
            ui_refs['banner_card'].classes(replace=cls)

        idx = params['trading_index']
        if idx in INDICES and ui_refs.get('calc_qty'): ui_refs['calc_qty'].set_text(f"(Qty: {int(params['lots']) * INDICES[idx]['lot_size']})")

        tc = shared_state['active_trades']['Call']
        if tc:
            ui_refs['call_status'].set_text('OPEN'); ui_refs['call_status'].classes(replace='text-[10px] font-bold text-white bg-red-600 px-2 rounded animate-pulse')
            ui_refs['call_info'].set_text(f"Time: {tc['entry_time']}"); ui_refs['call_trigger'].set_text(f"Trig: {tc['trigger']}")
            ui_refs['call_main_strike'].set_text(f"M ({tc['main']['strike']})"); ui_refs['call_main_open'].set_text(f"{tc['main']['entry_price']:.1f}"); ui_refs['call_main_curr'].set_text(f"{tc['main']['current_price']:.1f}")
            ui_refs['call_hedge_strike'].set_text(f"H ({tc['hedge']['strike']})"); ui_refs['call_hedge_open'].set_text(f"{tc['hedge']['entry_price']:.1f}"); ui_refs['call_hedge_curr'].set_text(f"{tc['hedge']['current_price']:.1f}")
            ui_refs['call_idx_open'].set_text(f"{tc['index_entry_price']:.0f}"); ui_refs['call_idx_curr'].set_text(f"{tc['index_current_price']:.0f}")
            c = 'text-green-600' if tc['pnl']>=0 else 'text-red-600'; ui_refs['call_pnl'].set_text(f"₹ {tc['pnl']:.0f}"); ui_refs['call_pnl'].classes(replace=f"text-xl font-bold {c} font-mono")
        else:
            ui_refs['call_status'].set_text('INACTIVE'); ui_refs['call_status'].classes(replace='text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
            ui_refs['call_info'].set_text('T: --'); ui_refs['call_trigger'].set_text('Trig: --')
            for k in ['call_main_strike','call_hedge_strike']: ui_refs[k].set_text('-')
            for k in ['call_main_open','call_main_curr','call_hedge_open','call_hedge_curr']: ui_refs[k].set_text('0.0')
            for k in ['call_idx_open','call_idx_curr']: ui_refs[k].set_text('0')
            ui_refs['call_pnl'].set_text('₹ 0')
        
        tp = shared_state['active_trades']['Put']
        if tp:
            ui_refs['put_status'].set_text('OPEN'); ui_refs['put_status'].classes(replace='text-[10px] font-bold text-white bg-green-600 px-2 rounded animate-pulse')
            ui_refs['put_info'].set_text(f"Time: {tp['entry_time']}"); ui_refs['put_trigger'].set_text(f"Trig: {tp['trigger']}")
            ui_refs['put_main_strike'].set_text(f"M ({tp['main']['strike']})"); ui_refs['put_main_open'].set_text(f"{tp['main']['entry_price']:.1f}"); ui_refs['put_main_curr'].set_text(f"{tp['main']['current_price']:.1f}")
            ui_refs['put_hedge_strike'].set_text(f"H ({tp['hedge']['strike']})"); ui_refs['put_hedge_open'].set_text(f"{tp['hedge']['entry_price']:.1f}"); ui_refs['put_hedge_curr'].set_text(f"{tp['hedge']['current_price']:.1f}")
            ui_refs['put_idx_open'].set_text(f"{tp['index_entry_price']:.0f}"); ui_refs['put_idx_curr'].set_text(f"{tp['index_current_price']:.0f}")
            c = 'text-green-600' if tp['pnl']>=0 else 'text-red-600'; ui_refs['put_pnl'].set_text(f"₹ {tp['pnl']:.0f}"); ui_refs['put_pnl'].classes(replace=f"text-xl font-bold {c} font-mono")
        else:
            ui_refs['put_status'].set_text('INACTIVE'); ui_refs['put_status'].classes(replace='text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
            ui_refs['put_info'].set_text('T: --'); ui_refs['put_trigger'].set_text('Trig: --')
            for k in ['put_main_strike','put_hedge_strike']: ui_refs[k].set_text('-')
            for k in ['put_main_open','put_main_curr','put_hedge_open','put_hedge_curr']: ui_refs[k].set_text('0.0')
            for k in ['put_idx_open','put_idx_curr']: ui_refs[k].set_text('0')
            ui_refs['put_pnl'].set_text('₹ 0')

    ui.timer(1.0, update_dashboard)
ui.run(title='Zerodha Bot', host='0.0.0.0', port=9000, reload=False, show=False)

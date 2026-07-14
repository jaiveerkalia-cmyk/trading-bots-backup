import os
from datetime import time as dtime

# --- PATHS ---
PROJECT_ROOT = '/app/bots/zerodha_bot'
AUTH_FILE_PATH = '/app/config/auth.txt'
MASTER_INSTRUMENTS_FILE = os.path.join(PROJECT_ROOT, 'instruments_master.csv')
NIFTY_OPT_FILE = os.path.join(PROJECT_ROOT, 'nifty_options.csv')
SENSEX_OPT_FILE = os.path.join(PROJECT_ROOT, 'sensex_options.csv')
TRADEBOOK_FILE = os.path.join(PROJECT_ROOT, 'options_tradebook.csv')
DAILY_PNL_FILE = os.path.join(PROJECT_ROOT, 'final_daily_pnl.csv')

# --- TRADING CONSTANTS ---
INDICES = {
    'NIFTY': {'token': 256265, 'exchange': 'NSE', 'name': 'NIFTY 50', 'segment': 'NFO', 'step': 50, 'lot_size': 65, 'opt_file': NIFTY_OPT_FILE},
    'SENSEX': {'token': 265, 'exchange': 'BSE', 'name': 'SENSEX', 'segment': 'BFO', 'step': 100, 'lot_size': 20, 'opt_file': SENSEX_OPT_FILE}
}

# --- SETTINGS ---
FORCE_EXIT_TIME = dtime(23, 59)
AUTO_SQUAREOFF_TIME = dtime(15, 19)

# --- ALERT SOUND LIBRARY ---
# Reuses the same known-good sound URLs already used elsewhere in the app (open/close/error),
# just exposed as user-selectable named options for the Upper/Lower price alert cards.
ALERT_SOUND_URLS = {
    'Wood Plank': 'https://actions.google.com/sounds/v1/cartoon/wood_plank_flicks.ogg',
    'Pop': 'https://actions.google.com/sounds/v1/cartoon/pop.ogg',
    'Boing': 'https://actions.google.com/sounds/v1/cartoon/cartoon_boing.ogg',
    'Crash': 'https://actions.google.com/sounds/v1/cartoon/clank_car_crash.ogg',
}

# --- SHARED STATE ---
shared_state = {
    'NIFTY': {'ltp': 0.0, 'open': 0.0, 'high': 0.0, 'low': 0.0},
    'SENSEX': {'ltp': 0.0, 'open': 0.0, 'high': 0.0, 'low': 0.0},
    'connection_status': 'Disconnected',
    'last_updated': 'Never',

    'daily_scan_done': False,
    'auto_sq_done': False,

    'last_action': 'System Ready',
    'activity_log': [],
    'reset_queue': [],
    'chart_data': {'times': [], 'pnl': [], 'markers': []},

    'instruments_loaded': False,
    'current_expiry': {'NIFTY': None, 'SENSEX': None},
    'active_trades': {'Call': None, 'Put': None},
    'option_chain': {},
    'pnl': {'realized': 0.0, 'unrealized': 0.0, 'trades_history': []},
    'sound_queue': [],
    'toast_queue': [],

    # Live diagnostic snapshot for armed unified triggers, overwritten every check_triggers()
    # tick (not appended/logged, so it can never grow or spam the activity log). Read by the
    # Order Book UI to show exactly what's being compared for a pending Limit/Stop-Market order.
    'unified_debug': {'Call': None, 'Put': None},
}

# --- UI REFERENCES ---
ui_refs = {
    'banner_card': None,
    'pnl_realized': None, 'pnl_unrealized': None, 'last_action': None,
    'activity_log_container': None, 'pnl_chart': None,

    'call_status': None, 'call_pnl': None,
    'call_main_strike': None, 'call_main_open': None, 'call_main_curr': None,
    'call_hedge_strike': None, 'call_hedge_open': None, 'call_hedge_curr': None,
    'call_idx_open': None, 'call_idx_curr': None,
    'call_info': None, 'call_trigger': None,

    'put_status': None, 'put_pnl': None,
    'put_main_strike': None, 'put_main_open': None, 'put_main_curr': None,
    'put_hedge_strike': None, 'put_hedge_open': None, 'put_hedge_curr': None,
    'put_idx_open': None, 'put_idx_curr': None,
    'put_info': None, 'put_trigger': None,

    'monitor_status': None, 'calc_qty': None, 'log_panel': None,
    'call_orderbook_debug': None, 'put_orderbook_debug': None,
}

# --- UI CONFIGURATION ---
UI_OPTS = {
    'indices': ['NIFTY', 'SENSEX'],
    'entry_modes': ['ATM', 'Other'],
    'alert_periods': ['Current', '5m', '1m'],
    'open_modes': ['Current', '5m', '1m', 'Loss'],
    'index_times': ['Current', '5m', '1m'],
    'toggles': ['Yes', 'No'],
    'on_off': ['On', 'Off'],
    # Unified Open Short/Long card options (order type + fire-on timeframe)
    'order_types': ['Market', 'Limit', 'Stop-Market'],
    'fire_on_opts': ['Live', '1m', '5m', '15m', '60m'],
    # Alert sound options (Upper/Lower alert cards)
    'alert_sounds': list(ALERT_SOUND_URLS.keys()),
}

# --- USER PARAMETERS ---
params = {
    'trading_index': 'NIFTY', 'lots': 4, 'live_trading': 'Off', 'mute_sound': False,
    'hedgeless_mode': True,

    # Independent Auto Close Flags
    'call_target_active': False, 'call_stop_active': False,
    'put_target_active': False, 'put_stop_active': False,

    # Independent Auto Close Values
    'call_target_val': 0, 'call_stop_val': 0,
    'put_target_val': 0, 'put_stop_val': 0,

    'call_entry_mode': 'ATM', 'call_manual_strike': '',
    'short_trigger_active': False, 'short_open_mode': 'Current', 'short_open_amount': 0, 'short_open_strike': 0,

    'put_entry_mode': 'ATM', 'put_manual_strike': '',
    'long_trigger_active': False, 'long_open_mode': 'Current', 'long_open_amount': 0, 'long_open_strike': 0,

    'alert_period': 'Current',
    'alert_upper_input': 0, 'alert_lower_input': 0,
    'alert_upper': 0, 'alert_lower': 0,
    'alert_upper_active': False, 'alert_lower_active': False,

    # Split Upper/Lower alert cards: independent period, sound choice, and sound duration (secs)
    'alert_upper_period': 'Current', 'alert_lower_period': 'Current',
    'alert_upper_sound': 'Wood Plank', 'alert_lower_sound': 'Wood Plank',
    'alert_upper_duration': 5, 'alert_lower_duration': 5,

    'global_stop_value': 0, 'global_target_value': 0, 'global_stop_active': False, 'global_tgt_active': False,

    'call_index_stop_val': 0, 'call_index_stop_time': 'Current', 'call_index_stop_active': False,
    'call_index_target_val': 0, 'call_index_target_time': 'Current', 'call_index_tgt_active': False,

    'put_index_stop_val': 0, 'put_index_stop_time': 'Current', 'put_index_stop_active': False,
    'put_index_target_val': 0, 'put_index_target_time': 'Current', 'put_index_tgt_active': False,

    # Premium exit params
    'call_prem_stop_val': 0, 'call_prem_stop_time': 'Current', 'call_prem_stop_active': False,
    'call_prem_target_val': 0, 'call_prem_target_time': 'Current', 'call_prem_tgt_active': False,

    'put_prem_stop_val': 0, 'put_prem_stop_time': 'Current', 'put_prem_stop_active': False,
    'put_prem_target_val': 0, 'put_prem_target_time': 'Current', 'put_prem_tgt_active': False,

    # --- Unified Open Short/Long cards (index-based, single-step order entry) ---
    # order_type: Market fires immediately. Limit/Stop-Market check trigger_price against
    # index price on the fire_on timeframe, then fire a market order for the option leg.
    'call_order_type': 'Market', 'call_trigger_price': 0, 'call_strike_offset': 1,
    'call_fire_on': 'Live', 'call_qty': 4, 'call_armed': False,
    'call_new_stop': '', 'call_new_target': '',

    'put_order_type': 'Market', 'put_trigger_price': 0, 'put_strike_offset': 1,
    'put_fire_on': 'Live', 'put_qty': 4, 'put_armed': False,
    'put_new_stop': '', 'put_new_target': '',
}

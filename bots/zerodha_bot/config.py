import os
import json
from datetime import time as dtime

# --- PATHS ---
PROJECT_ROOT = '/app/bots/zerodha_bot'
AUTH_FILE_PATH = '/app/config/auth.txt'
MASTER_INSTRUMENTS_FILE = os.path.join(PROJECT_ROOT, 'instruments_master.csv')
NIFTY_OPT_FILE = os.path.join(PROJECT_ROOT, 'nifty_options.csv')
SENSEX_OPT_FILE = os.path.join(PROJECT_ROOT, 'sensex_options.csv')
TRADEBOOK_FILE = os.path.join(PROJECT_ROOT, 'options_tradebook.csv')
DAILY_PNL_FILE = os.path.join(PROJECT_ROOT, 'final_daily_pnl.csv')
ALERT_PROFILE_FILE = os.path.join(PROJECT_ROOT, 'alert_sound_profile.json')

# --- TRADING CONSTANTS ---
INDICES = {
    'NIFTY': {'token': 256265, 'exchange': 'NSE', 'name': 'NIFTY 50', 'segment': 'NFO', 'step': 50, 'lot_size': 65, 'opt_file': NIFTY_OPT_FILE},
    'SENSEX': {'token': 265, 'exchange': 'BSE', 'name': 'SENSEX', 'segment': 'BFO', 'step': 100, 'lot_size': 20, 'opt_file': SENSEX_OPT_FILE}
}

# --- SETTINGS ---
FORCE_EXIT_TIME = dtime(23, 59)
AUTO_SQUAREOFF_TIME = dtime(15, 19)

# --- ALERT SOUND LIBRARY ---
ALERT_SOUND_URLS = {
    'Wood Plank': 'https://actions.google.com/sounds/v1/cartoon/wood_plank_flicks.ogg',
    'Pop': 'https://actions.google.com/sounds/v1/cartoon/pop.ogg',
    'Boing': 'https://actions.google.com/sounds/v1/cartoon/cartoon_boing.ogg',
    'Crash': 'https://actions.google.com/sounds/v1/cartoon/clank_car_crash.ogg',
    'Alarm Clock (Loud)': 'https://actions.google.com/sounds/v1/alarms/alarm_clock.ogg',
    'Digital Watch Alarm (Loud)': 'https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg',
    'Beep Alert (Loud)': 'https://actions.google.com/sounds/v1/alarms/beep_short.ogg',
}

# --- ALERT SOUND PROFILE PERSISTENCE ---
def load_alert_profile():
    try:
        with open(ALERT_PROFILE_FILE, 'r') as f:
            data = json.load(f)
        sound = data.get('sound', 'Wood Plank')
        duration = data.get('duration', 5)
        if sound not in ALERT_SOUND_URLS:
            sound = 'Wood Plank'
        return sound, duration
    except Exception:
        return 'Wood Plank', 5

def save_alert_profile(sound_name, duration):
    try:
        with open(ALERT_PROFILE_FILE, 'w') as f:
            json.dump({'sound': sound_name, 'duration': duration}, f)
    except Exception:
        pass

_saved_alert_sound, _saved_alert_duration = load_alert_profile()

# --- SHARED STATE ---
shared_state = {
    # fut_ltp/fut_symbol/fut_token: Futures Mode (params['futures_mode']) additions.
    # fut_token/fut_symbol are resolved once per day in auto_run.run_daily_scan() via
    # instrument_manager.get_near_month_future(); fut_ltp is populated by a parallel
    # tick subscription in ticker_engine.py alongside the existing spot 'ltp'. All three
    # default to empty/zero and are simply unused (spot 'ltp' continues to drive
    # everything) when futures_mode is off, so this is purely additive.
    'NIFTY': {'ltp': 0.0, 'open': 0.0, 'high': 0.0, 'low': 0.0, 'fut_ltp': 0.0, 'fut_symbol': '', 'fut_token': None},
    'SENSEX': {'ltp': 0.0, 'open': 0.0, 'high': 0.0, 'low': 0.0, 'fut_ltp': 0.0, 'fut_symbol': '', 'fut_token': None},
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
    # 'peak_total': Trailing Global PnL Stop (params['global_trailing_active']) support.
    # Tracks the highest (realized + unrealized) PnL seen so far THIS SESSION -- updated
    # every tick in LogicEngine._check_trailing_global_limit(). Reset to 0.0 at the 15:19
    # EOD routine in auto_run.AutoController.run_loop(), alongside daily_pnl_written/
    # active_trades, so it starts fresh each trading day. Unused (stays at whatever value
    # it last held, harmlessly) whenever global_trailing_active is off.
    'pnl': {'realized': 0.0, 'unrealized': 0.0, 'trades_history': [], 'peak_total': 0.0},
    'sound_queue': [],
    'toast_queue': [],

    'unified_debug': {'Call': None, 'Put': None},

    # --- Active Alerts (multi-alert system) ---
    # List of independent, user-created price alerts. Each entry:
    #   {'id': short-uuid str, 'direction': 'upper'|'lower', 'value': float,
    #    'period': 'Current'|'1m'|'5m', 'sound': str (key into ALERT_SOUND_URLS),
    #    'duration': float (seconds), 'created_at': 'HH:MM:SS'}
    # Multiple alerts in the same direction are allowed. Fired alerts are removed from
    # this list (one-shot, same semantics as the old single-slot alert_upper_active/
    # alert_lower_active flags). Lives in shared_state (not params) since these are
    # dynamic runtime instances, matching the existing active_trades/option_chain pattern.
    # ALSO fully cleared (regardless of fired/pending state) at the 15:19 EOD routine in
    # auto_run.AutoController.run_loop() -- see that method's docstring/comments -- since a
    # price alert set during today's session has no business firing against tomorrow's
    # price action.
    'alerts': [],

    # --- Candlestick Pattern Indicators (pattern_engine.py) ---
    # Per-interval diagnostic info: {'1m'|'5m'|'15m'|'30m'|'1h': {'boundary_time','status','updated'} or None}
    'pattern_debug': {},
    # Per-pattern last fired signal (for UI display): {'bullish_engulfing'|'bearish_engulfing': {...}}
    'pattern_last_signal': {},
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

    'open_positions_count': None,
    'call_pos_row': None, 'call_pos_symbol': None, 'call_pos_mark': None, 'call_pos_size': None,
    'call_pos_pnl': None, 'call_pos_entry': None, 'call_pos_qty': None,
    'call_pos_maxloss': None, 'call_pos_maxprofit': None, 'call_pos_side_label': None,
    'put_pos_row': None, 'put_pos_symbol': None, 'put_pos_mark': None, 'put_pos_size': None,
    'put_pos_pnl': None, 'put_pos_entry': None, 'put_pos_qty': None,
    'put_pos_maxloss': None, 'put_pos_maxprofit': None, 'put_pos_side_label': None,

    # Futures Mode banner switch: shows the resolved near-month future symbol for the
    # currently active trading_index while the mode is on (mirrors the ENTER VIA STOP
    # switch's small caption label pattern already in the banner).
    'futures_mode_symbol_label': None,
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
    'order_types': ['Market', 'Limit', 'Stop-Market'],
    'fire_on_opts': ['Live', '1m', '5m', '15m', '60m'],
    'alert_sounds': list(ALERT_SOUND_URLS.keys()),
    'pattern_intervals': ['1m', '5m', '15m', '30m', '1h'],
}

# --- USER PARAMETERS ---
params = {
    'trading_index': 'NIFTY', 'lots': 4, 'live_trading': 'Off', 'mute_sound': False,
    'hedgeless_mode': True,

    # Options Buy Mode: global toggle. True = whole bot buys (Call=buy CE, Put=buy PE),
    # always hedgeless. Guarded in auto_run.py: blocked while any position open, mutually
    # exclusive with Auto Pilot. Default False = identical to prior selling-only behavior.
    'options_buy_mode': False,

    # Enter via Stop: global toggle, default ON. When on, any Stop-Market unified entry, OR
    # any Index/Premium STOP (never Target) whose period is candle-close-based (1m/5m/15m/60m
    # for entries; 1m/5m for index/premium stops -- never 'Current'/'Live'), is intercepted at
    # the moment its condition is confirmed true: instead of acting immediately, the bot
    # fetches that just-closed candle and arms a real Stop-Market order one tick beyond its
    # high or low (mirroring the ORIGINAL condition's breakout direction -- downside cross ->
    # stop below the candle's low; upside cross -> stop above the candle's high), then
    # live-monitors that new level every tick until price actually trades through it. See
    # stop_via_candle_engine.py. When off, behavior is identical to before this feature
    # existed (immediate action at candle close).
    'enter_via_stop': True,

    # Futures Mode: global toggle (both NIFTY and SENSEX together), default OFF. When on,
    # all PRICE EVALUATION -- entry triggers, index stop/target checks, index-based alerts,
    # the index_entry_price/index_current_price fields shown in the UI/used for index-linked
    # PnL display, and the historical-candle fetches used by pattern_engine.py, hourly
    # trailing (AutoController.process_hourly_trailing), and stop_via_candle_engine.py --
    # reads the near-month index FUTURE's price/candles (shared_state[index]['fut_ltp'],
    # fut_token) instead of the spot index (shared_state[index]['ltp'], INDICES[..]['token']).
    # STRIKE SELECTION (find_balanced_strikes, recalc_reversal_strikes, ATM/manual strike
    # offset math, INDICES[..]['step'] rounding) is explicitly EXCLUDED and always stays
    # spot-based, since option strikes are spot-relative, not futures-relative. Premium-based
    # option PnL (trade['pnl']) is also unaffected -- it was already based on the option's own
    # LTP, never the index. Default False = identical to prior spot-only behavior; see
    # config.get_eval_price() / get_eval_token() for the single-source-of-truth switch used by
    # every consumer of this flag.
    'futures_mode': False,

    'call_target_active': False, 'call_stop_active': False,
    'put_target_active': False, 'put_stop_active': False,

    'call_target_val': 0, 'call_stop_val': 0,
    'put_target_val': 0, 'put_stop_val': 0,

    'call_entry_mode': 'ATM', 'call_manual_strike': '',
    'short_trigger_active': False, 'short_open_mode': 'Current', 'short_open_amount': 0, 'short_open_strike': 0,

    'put_entry_mode': 'ATM', 'put_manual_strike': '',
    'long_trigger_active': False, 'long_open_mode': 'Current', 'long_open_amount': 0, 'long_open_strike': 0,

    # --- Active Alerts: 'add new alert' form state (upper/lower), NOT the alerts
    # themselves (those live in shared_state['alerts'] as independent instances so
    # multiple can coexist per direction). These are just the staged input values for
    # the next alert about to be created, plus the shared default sound/duration profile
    # applied to new alerts at creation time (editable per-alert afterward via MODIFY).
    'alert_upper_input': 0, 'alert_lower_input': 0,
    'alert_upper_period': 'Current', 'alert_lower_period': 'Current',
    'alert_upper_sound': _saved_alert_sound, 'alert_lower_sound': _saved_alert_sound,
    'alert_upper_duration': _saved_alert_duration, 'alert_lower_duration': _saved_alert_duration,

    'global_stop_value': 0, 'global_target_value': 0, 'global_stop_active': False, 'global_tgt_active': False,

    # Trailing Global PnL Stop: independent from the absolute Global Stop/Target above.
    # 'global_trailing_drawdown' is an amount (in rupees), not a price level -- fires when
    # combined realized+unrealized PnL falls this much below its session peak
    # (shared_state['pnl']['peak_total']), regardless of whether that peak or the current
    # total is positive or negative. Default off/0 = no behavior change unless explicitly
    # enabled. See LogicEngine._check_trailing_global_limit().
    'global_trailing_value': 0, 'global_trailing_active': False,

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
    # armed_at: datetime.now() stamped the moment the card is armed (or MODIFY-CONFIRMed);
    # used by logic_engine._check_unified_open to require the fire_on boundary to be crossed
    # AFTER arming, not merely "any boundary crossing", so a card armed during a boundary
    # minute doesn't fire on the very next tick.
    'call_order_type': 'Market', 'call_trigger_price': 0, 'call_strike_offset': 2,
    'call_fire_on': 'Live', 'call_qty': 4, 'call_armed': False, 'call_armed_at': None,
    'call_new_stop': '', 'call_new_target': '',

    'put_order_type': 'Market', 'put_trigger_price': 0, 'put_strike_offset': 2,
    'put_fire_on': 'Live', 'put_qty': 4, 'put_armed': False, 'put_armed_at': None,
    'put_new_stop': '', 'put_new_target': '',

    # --- Candlestick Pattern Indicators (pattern_engine.py) ---
    # Seconds to wait AFTER a candle boundary closes before fetching it via historical API
    # (gives the broker's candle data time to finalize). Applies to all patterns/intervals.
    'pattern_fetch_delay_sec': 3,

    # Bullish Engulfing: prev (base) candle red, current/synthetic candle green, and the
    # synthetic candle's body fully engulfs the base candle's body.
    'bullish_engulfing_enabled': True,
    'bullish_engulfing_intervals': ['5m', '15m', '30m', '1h'],
    # Engulf candle count: 1 = standard single-candle engulfing. N = combine the N most
    # recently closed candles into one synthetic candle (open=first's open, close=last's
    # close) and check that against the base candle immediately preceding the window.
    'bullish_engulfing_count': 1,

    # Bearish Engulfing: prev (base) candle green, current/synthetic candle red, and the
    # synthetic candle's body fully engulfs the base candle's body.
    'bearish_engulfing_enabled': True,
    'bearish_engulfing_intervals': ['5m', '15m', '30m', '1h'],
    'bearish_engulfing_count': 1,
}


# --- FUTURES MODE: SINGLE-SOURCE-OF-TRUTH PRICE/TOKEN RESOLVERS ---
# Every consumer of Futures Mode (logic_engine, auto_run.AutoController, pattern_engine,
# stop_via_candle_engine) calls these two helpers instead of each re-implementing its own
# "if futures_mode: use fut_ltp else ltp" branch. Falls back safely to spot whenever the
# future's price/token hasn't been resolved yet (e.g. before today's daily scan has run),
# so a stale/zero fut_ltp can never silently produce a bad comparison -- it just behaves as
# if futures_mode were off for that index until the future is actually resolved.
def get_eval_price(index_name):
    """Returns the price to use for entry/exit/alert/index-PnL evaluation for index_name:
    the near-month future's LTP if params['futures_mode'] is on AND a future price has
    actually been resolved for this index yet, otherwise the spot index LTP (unchanged
    prior behavior)."""
    if params.get('futures_mode', False):
        fut_ltp = shared_state.get(index_name, {}).get('fut_ltp', 0.0)
        if fut_ltp:
            return fut_ltp
    return shared_state.get(index_name, {}).get('ltp', 0.0)


def get_eval_token(index_name):
    """Returns the instrument token to use for historical candle fetches (pattern_engine,
    hourly trailing, stop_via_candle_engine, check_inside_candle) for index_name: the
    near-month future's token if params['futures_mode'] is on AND it has been resolved,
    otherwise the spot index token from INDICES (unchanged prior behavior). Strike
    selection call sites (find_balanced_strikes, recalc_reversal_strikes, ATM/manual strike
    math) intentionally do NOT use this helper -- they always use INDICES[index_name]['token']
    directly, since strikes are spot-relative regardless of Futures Mode."""
    if params.get('futures_mode', False):
        fut_token = shared_state.get(index_name, {}).get('fut_token')
        if fut_token:
            return fut_token
    return INDICES[index_name]['token']

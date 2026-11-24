# --- LEAN MANAGER (default OFF; enable with env ZB_LEAN_MANAGER=1) -------------------------------
# The manager process (plain `python auto_run.py`) only supervises the --worker child, yet the heavy
# imports below (nicegui, ui_components, engines, ...) would load fully into it as well, leaving two
# complete copies of the app in RAM. With ZB_LEAN_MANAGER=1 the same supervisor loop runs HERE, with
# the standard library only, before any heavy import. The original MANAGER MODE block at the bottom of
# this file stays untouched and is simply never reached in this mode. Worker behaviour is unchanged.
import sys as _zb_sys, os as _zb_os
if __name__ == "__main__" and "--worker" not in _zb_sys.argv and _zb_os.environ.get("ZB_LEAN_MANAGER") == "1":
    import subprocess as _zb_sp, time as _zb_time
    from datetime import datetime as _zb_dt, timedelta as _zb_td
    print(f"[{_zb_dt.now().strftime('%Y-%m-%d %H:%M:%S')}] --- MANAGER STARTED (lean) ---")
    if _zb_os.path.exists(".automode_state"): _zb_os.remove(".automode_state")
    _zb_p = None
    while True:
        try:
            _zb_p = _zb_sp.Popen([_zb_sys.executable, __file__, "--worker"])
            _zb_p.wait()
            _zb_now = _zb_dt.now()
            _zb_ts = _zb_now.strftime('%Y-%m-%d %H:%M:%S')
            if (_zb_now.weekday() == 4 and _zb_now.hour >= 22) or _zb_now.weekday() in [5, 6]:
                print(f"[{_zb_ts}] 📅 WEEKEND DETECTED. Sleeping until Monday...")
                _zb_days = 7 - _zb_now.weekday()
                if _zb_now.weekday() == 4: _zb_days = 3
                if _zb_now.weekday() == 6: _zb_days = 1
                _zb_target = _zb_now.replace(hour=8, minute=55, second=0, microsecond=0) + _zb_td(days=_zb_days)
                _zb_time.sleep(max(1, (_zb_target - _zb_now).total_seconds()))
            elif _zb_now.hour >= 22 or _zb_now.hour < 8:
                print(f"[{_zb_ts}] 💤 NIGHT MODE: Sleeping until Morning...")
                _zb_target = _zb_now.replace(hour=8, minute=55, second=0, microsecond=0)
                if _zb_now.hour >= 22: _zb_target += _zb_td(days=1)
                _zb_time.sleep(max(1, (_zb_target - _zb_now).total_seconds()))
            else:
                print(f"[{_zb_ts}] ⚠️ WORKER RESTARTING IN 2s...")
                _zb_time.sleep(2)
        except KeyboardInterrupt:
            print("\n--- MANAGER STOPPING ---")
            if _zb_p is not None: _zb_p.terminate()
            break
    _zb_sys.exit(0)

# --- ZB_NO_KITE_TICKER (default OFF; enable with env ZB_NO_KITE_TICKER=1) -------------------------
# `import kiteconnect` eagerly imports kiteconnect.ticker (KiteTicker), which pulls in twisted,
# autobahn and OpenSSL (~15-25 MB RSS). This bot reads ticks from Redis (RedisTickClient) and never
# uses KiteTicker, so a stub module is pre-seeded and the real one is never imported. Instantiating
# the stub raises a clear error, so any unexpected use fails loudly instead of misbehaving silently.
if _zb_os.environ.get("ZB_NO_KITE_TICKER") == "1" and "kiteconnect" not in _zb_sys.modules:
    import types as _zb_types
    _zb_stub = _zb_types.ModuleType("kiteconnect.ticker")
    class _ZbKiteTickerDisabled:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("KiteTicker is disabled (ZB_NO_KITE_TICKER=1); ticks come from Redis")
    _zb_stub.KiteTicker = _ZbKiteTickerDisabled
    _zb_sys.modules["kiteconnect.ticker"] = _zb_stub

import sys
import time
import csv
import os
import threading
import subprocess  # <--- ADDED
from datetime import datetime, time as dtime, timedelta
from nicegui import ui, app  # <--- ADDED 'app'
import statistics

import nicegui
from contextlib import contextmanager
from datetime import datetime

# --- PERMANENT UI CONTEXT FIX ---
original_notify = nicegui.ui.notify

def global_custom_notify(message, **kwargs):
    """Globally intercepts all UI notifications from any thread."""
    msg_upper = str(message).upper()
    
    if 'ALERT' in msg_upper: 
        shared_state['sound_queue'].append('alert')
        
    # Route manual UI button clicks directly to Trade Event Log AND Terminal
    if any(keyword in msg_upper for keyword in ['SET', 'ACTIVATED', 'RESET', 'ARMED', 'CLEARED']):
        ts = datetime.now().strftime('%H:%M:%S')
        log_text = f"[{ts}] ⚙️ MANUAL ACTION: {message}"
        
        shared_state['activity_log'].insert(0, log_text)
        shared_state['activity_log'] = shared_state['activity_log'][:100]
        print(log_text)  # FORCES TERMINAL PRINT
        
    # Safely route the visual pop-up to the queue to avoid thread crashes
    shared_state['toast_queue'].append((message, kwargs.get('type', 'info')))

# Apply global notify patch
nicegui.ui.notify = global_custom_notify
ui.notify = global_custom_notify

@contextmanager
def safe_ui_context():
    """Dummy context manager to prevent breaking your existing indentations."""
    yield

# --- IMPORT EXISTING MODULES ---
import config
from config import shared_state, params, ui_refs, INDICES, UI_OPTS, ALERT_SOUND_URLS, EOD_TIME, get_eval_price, get_eval_token
import ui_components as comp
from auth_manager import get_kite_session
from ticker_engine import TickerClient
from instrument_manager import InstrumentManager
from logic_engine import LogicEngine
from pattern_engine import PatternEngine
from indicator_engine import IndicatorEngine, render_indicator_section as render_indicator_alerts_section
from stop_via_candle_engine import StopViaCandleEngine
from daily_logger import DailyLogger

# ==========================================
#      CONFIGURATION & CONSTANTS
# ==========================================

# ==========================================
#      DAY CONTROLLER (EOD routine + per-leg field wipe; Auto Pilot removed)
# ==========================================

class DayController:
    """Per-day housekeeping only. Auto Pilot (strategy, arming, trailing, reversal hunting)
    has been removed; what remains is the EOD routine (run_loop), clear_leg_fields, reset
    and the DONE flag that freezes the chart after EOD. mode is always 'OFF' and is still
    handed to daily_logger.log_snapshot, so the daily log CSV is unchanged."""
    def __init__(self, logic_engine, inst_manager):
        self.logic = logic_engine
        self.inst_mgr = inst_manager
        self.mode = 'OFF' 
        self.log_msg = "Auto Mode: Ready"
        self.last_trade_entry_price = 0.0
        
        # Log Flag
        self.waiting_log_sent = False
        
        # Memory for Change Logging
        self.last_params = {}

        # FIX: Ensure Queues exist
        if 'toast_queue' not in shared_state: shared_state['toast_queue'] = []
        if 'sound_queue' not in shared_state: shared_state['sound_queue'] = []
        if 'daily_pnl_written' not in shared_state: shared_state['daily_pnl_written'] = False
            
        self.reset()

    @property
    def is_active(self):
        return self.mode == 'ON'

    def reset(self):
        self.state = 'IDLE' 
        self.active_side = None
        self.ref_high = 0.0
        self.ref_low = 0.0
        self.today_index = None
        self.last_trade_entry_price = 0.0
        self.log("State Reset for New Day")

    def log(self, msg):
        self.log_msg = msg
        self.logic.log_action(f"🤖 AUTO: {msg}")

    def clear_leg_fields(self, side):
        """Wipes the physical text fields and disables toggles for a specific leg."""
        # Positions V2: the Buy card of the same option type is disarmed together with the Sell
        # card (EOD, Close All, Global limits all call this for Call and Put). No-op flag off.
        if getattr(self.logic, 'v2', None) is not None and side in ('Call', 'Put'):
            _bc = 'bcall' if side == 'Call' else 'bput'
            params[f'{_bc}_armed'] = False; params[f'{_bc}_armed_at'] = None; params[f'{_bc}_trigger_price'] = 0
            params[f'{_bc}_new_stop'] = ''; params[f'{_bc}_new_target'] = ''; params[f'{_bc}_net_ok'] = False
            params[f'{side.lower()}_net_ok'] = False
        if side == 'Call':
            params['call_target_val'] = 0; params['call_target_active'] = False
            params['call_stop_val'] = 0; params['call_stop_active'] = False
            params['call_index_stop_val'] = ""; params['call_index_stop_active'] = False
            params['call_index_target_val'] = ""; params['call_index_tgt_active'] = False
            params['short_open_amount'] = ""; params['short_trigger_active'] = False
            params['short_open_strike'] = ""
            params['call_manual_strike'] = ""
            params['call_prem_stop_val'] = 0; params['call_prem_stop_active'] = False
            params['call_prem_target_val'] = 0; params['call_prem_tgt_active'] = False
            # Unified Open Short card
            params['call_armed'] = False; params['call_trigger_price'] = 0
            params['call_armed_at'] = None
            params['call_new_stop'] = ''; params['call_new_target'] = ''
        elif side == 'Put':
            params['put_target_val'] = 0; params['put_target_active'] = False
            params['put_stop_val'] = 0; params['put_stop_active'] = False
            params['put_index_stop_val'] = ""; params['put_index_stop_active'] = False
            params['put_index_target_val'] = ""; params['put_index_tgt_active'] = False
            params['long_open_amount'] = ""; params['long_trigger_active'] = False
            params['long_open_strike'] = ""
            params['put_manual_strike'] = ""
            params['put_prem_stop_val'] = 0; params['put_prem_stop_active'] = False
            params['put_prem_target_val'] = 0; params['put_prem_tgt_active'] = False
            # Unified Open Long card
            params['put_armed'] = False; params['put_trigger_price'] = 0
            params['put_armed_at'] = None
            params['put_new_stop'] = ''; params['put_new_target'] = ''

    def run_loop(self):
        now = datetime.now()
        ts = now.strftime('%H:%M:%S')

        # 1. PRIORITY: GLOBAL EOD ROUTINE -- ALWAYS runs regardless of options_buy_mode. This
        # is the fix for "EOD final PnL write never happened in Buy Mode": this block used
        # to be skipped entirely because the options_buy_mode guard (which correctly blocks
        # Auto Pilot's sell-only strategy logic below) previously sat ABOVE this block and
        # returned before it could run. save_daily_report()/close_all_positions() themselves
        # are mode-agnostic (they only read trade PnL, which is already correct for both
        # modes via LogicEngine.update_pnl), so this routine is safe to run unconditionally.
        # EOD_TIME (config.py) is the single source of truth for when this window starts; the
        # window stays open until 15:40 purely to give the one-shot daily_pnl_written guard
        # below a wide enough margin before self.state flips to DONE and this block stops
        # running entirely for the day.
        if EOD_TIME <= now.time() < dtime(15, 40):
            # Wipe all fields
            self.clear_leg_fields('Call')
            self.clear_leg_fields('Put')
            
            if not shared_state.get('daily_pnl_written', False):
                self.log("⏰ EOD Day End Routine (Auto/Manual).")
                try:
                    self.logic.close_all_positions("Day End Auto-Square", save_pnl=True)
                    shared_state['daily_pnl_written'] = True
                    shared_state['sound_queue'].append('close')
                except: shared_state['daily_pnl_written'] = True

                # Clear all pending price alerts (Active Alerts section) -- they were set
                # against today's session; carrying them into tomorrow could fire against a
                # completely different day's price action. Reuses the same once-per-day
                # daily_pnl_written gate as the rest of this EOD routine, so it only runs
                # once, not every tick during the [EOD_TIME, 15:40) window.
                if shared_state.get('alerts'):
                    cleared_count = len(shared_state['alerts'])
                    shared_state['alerts'] = []
                    self.log(f"🔔 Cleared {cleared_count} pending price alert(s) at Day End.")
            
            if shared_state['active_trades']['Call'] is not None: shared_state['active_trades']['Call'] = None
            if shared_state['active_trades']['Put'] is not None: shared_state['active_trades']['Put'] = None
            self.state = 'DONE'
            return

# ==========================================
#      MAIN BOOTSTRAP & GLOBALS
# ==========================================

class RedirectedStdout:
    def __init__(self): self.original_stdout = sys.stdout
    def write(self, message): self.original_stdout.write(message)
    def flush(self): self.original_stdout.flush()
    def isatty(self): return False
sys.stdout = RedirectedStdout()

print("--- STARTING AUTO-RUN BOT ---")

kite, api_key, access_token = get_kite_session()
if not kite: sys.exit(1)

ticker = TickerClient(api_key, access_token)
inst_manager = InstrumentManager(kite)
logic = LogicEngine(ticker, inst_manager)
pattern_engine = PatternEngine(inst_manager)
indicator_engine = IndicatorEngine(inst_manager)
stop_via_candle_engine = StopViaCandleEngine(inst_manager, logic)
logic.stop_via_candle_engine = stop_via_candle_engine
comp._stop_via_candle_engine = stop_via_candle_engine   # <-- ADD THIS LINE
controller = DayController(logic, inst_manager)
# Global Stop/Target (LogicEngine._check_global_limits) needs to wipe every per-side field on
# BOTH sides when it fires, matching what Auto Pilot's own internal global stop already does
# -- wired in here (after controller exists) rather than importing AutoController into
# logic_engine.py, to avoid a circular import.
logic.clear_leg_fields_callback = controller.clear_leg_fields
# Positions V2 (default OFF; enable with env ZB_POSITIONS_V2=1 -- needs the Stage 3 UI first).
# With the flag off positions_engine is never imported and logic.v2 stays None, so every
# LogicEngine hook falls straight through to its legacy body.
if os.environ.get("ZB_POSITIONS_V2") == "1":
    from positions_engine import PositionsEngine
    logic.v2 = PositionsEngine(logic, stop_via_candle_engine)
    logic.v2.defer_entries = True      # Stop-Market entries and candle-based stops use Enter via Stop (same
    logic.v2.defer_exits = True        # params['enter_via_stop'] switch as the legacy page)
    print("--- POSITIONS V2 ENABLED ---")
daily_logger = DailyLogger() 
#ticker.start()

# ==========================================
#      UI HELPER FUNCTIONS (GLOBAL)
# ==========================================

def update_ui():
    """Updates all UI elements."""
    # Whole body wrapped in try/except RuntimeError: with multiple browser clients, each page
    # runs its own ui.timer(1.0, update_ui), but ui_refs / shared_state are process-global. If
    # a client disconnects, NiceGUI deletes that client's Client object, yet its (now-stale)
    # element references can still live in ui_refs and its timer callback can still fire one
    # more tick before NiceGUI tears it down -- any element access on a deleted client raises
    # RuntimeError('The client this element belongs to has been deleted.'). This is a UI-
    # refresh-only concern (this function only reads shared_state and writes label/chart
    # widgets); it never touches trading logic, order placement, or PnL calculation, so
    # swallowing it here is safe and mirrors the existing except-and-continue pattern already
    # used around controller.run_loop()/run_bot_logic() below.
    try:
        # 1. PROCESS TOAST QUEUE
        while shared_state.get('toast_queue'):
            msg, type_ = shared_state['toast_queue'].pop(0)
            original_notify(msg, type=type_)

        # 2. PROCESS SOUND QUEUE (Fixes 'No Sound')
        # NOTE: modern browsers block audio.play() unless a real user gesture happened on the
        # page first (autoplay policy). Each play() call below is wrapped in .catch(()=>{}) so a
        # blocked play() fails silently instead of throwing. Click the "Enable Sound" button in
        # the banner once per session to satisfy the browser's gesture requirement.
        while shared_state.get('sound_queue'):
            snd = shared_state['sound_queue'].pop(0)

            # NEW: user-selectable alert sound + duration, from the split Upper/Lower alert cards.
            # Distinct tuple shape, so it can't collide with the legacy string entries below.
            # Looped (a.loop = true) for the full requested duration then stopped -- otherwise a
            # short clip just plays once and goes silent well before the duration elapses, which
            # looked like "the duration isn't respected".
            if isinstance(snd, tuple) and len(snd) == 3 and snd[0] == 'alert_custom':
                _, sound_name, duration = snd
                url = ALERT_SOUND_URLS.get(sound_name, ALERT_SOUND_URLS['Wood Plank'])
                try: dur_ms = int(float(duration) * 1000)
                except (ValueError, TypeError): dur_ms = 5000
                if dur_ms <= 0: dur_ms = 5000
                ui.run_javascript(
                    f'const a = new Audio("{url}"); a.loop = true; a.play().catch(()=>{{}});'
                    f'setTimeout(() => {{ a.pause(); a.currentTime = 0; }}, {dur_ms});'
                )
                continue

            # We use a generic notification beep for all for now, or specific if desired
            # You can replace these URLs with any public mp3 link
            if snd == 'success':
                ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/cartoon_boing.ogg").play().catch(()=>{})')
            elif snd == 'error':
                ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/clank_car_crash.ogg").play().catch(()=>{})')
            elif snd == 'open':
                ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/pop.ogg").play().catch(()=>{})')
            else: # alert/general
                ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/wood_plank_flicks.ogg").play().catch(()=>{})')

        # 3. Standard UI Updates
        if controller.state != 'DONE':
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
            ui_refs['pnl_realized'].set_text(f"₹ {p:.2f}")
            ui_refs['pnl_realized'].classes(replace=f"text-2xl font-mono font-bold {c} leading-none")
        
        if ui_refs['pnl_unrealized']:
            p = shared_state['pnl']['unrealized']; c = 'text-green-600' if p>=0 else 'text-red-600'
            ui_refs['pnl_unrealized'].set_text(f"₹ {p:.2f}")
            ui_refs['pnl_unrealized'].classes(replace=f"text-2xl font-bold font-mono {c} leading-none")

        # Total PnL (Realized + Unrealized combined) -- displayed alongside the two existing
        # figures, so the person can see the number that actually drives Global Stop/Target/
        # PnL Floor without having to add the other two together mentally.
        if ui_refs.get('pnl_total'):
            p = shared_state['pnl']['realized'] + shared_state['pnl']['unrealized']
            c = 'text-green-500' if p >= 0 else 'text-red-500'
            ui_refs['pnl_total'].set_text(f"₹ {p:.2f}")
            ui_refs['pnl_total'].classes(replace=f"text-2xl font-mono font-bold {c} leading-none")

        if ui_refs['last_action']: ui_refs['last_action'].set_text(shared_state['last_action'])
        
        if ui_refs['monitor_status']:
            if params['short_trigger_active'] or params['long_trigger_active'] or params.get('call_armed') or params.get('put_armed'):
                ui_refs['monitor_status'].set_text('TRIGGERS ACTIVE')
                ui_refs['monitor_status'].classes(replace='text-xs font-bold bg-green-500 text-white px-2 py-1 rounded animate-pulse whitespace-nowrap')
            else:
                ui_refs['monitor_status'].set_text('TRIGGERS OFF')
                ui_refs['monitor_status'].classes(replace='text-xs font-bold bg-gray-800 text-gray-400 px-2 py-1 rounded whitespace-nowrap')

        # ... [Keep rest of update_ui as is, referencing banner_card, call_status, etc.] ...
        # (Just assume the rest of your UI update code is here unchanged)
        
        # [Rest of update_ui code for banner/call/put labels...]
        # ...
        if ui_refs['banner_card']:
            cls = 'w-full p-3 bg-orange-200 text-orange-900 rounded-t-xl rounded-b-none border-b border-orange-300'
            if params['live_trading'] == 'On':
                cls = 'w-full p-3 bg-green-200 text-green-900 rounded-t-xl rounded-b-none border-b border-green-300'
            ui_refs['banner_card'].classes(replace=cls)

        idx = params['trading_index']
        if idx in INDICES and ui_refs.get('calc_qty'): 
            ui_refs['calc_qty'].set_text(f"(Qty: {int(params['lots']) * INDICES[idx]['lot_size']})")

        # Futures Mode symbol caption: shows the resolved near-month future symbol for the
        # CURRENTLY ACTIVE trading_index while the mode is on, updating live as the index is
        # switched. Blank whenever the mode is off or the future hasn't been resolved yet
        # (e.g. before today's daily scan has run).
        if ui_refs.get('futures_mode_symbol_label'):
            if params.get('futures_mode', False):
                fut_sym = shared_state.get(idx, {}).get('fut_symbol', '')
                ui_refs['futures_mode_symbol_label'].set_text(fut_sym if fut_sym else 'resolving...')
            else:
                ui_refs['futures_mode_symbol_label'].set_text('spot price -> strike')

        # Global PnL Floor: refresh the live "Now" caption every tick (no peak tracking).
        if ui_refs.get('trailing_stop_caption'):
            total = shared_state['pnl']['realized'] + shared_state['pnl']['unrealized']
            ui_refs['trailing_stop_caption'].set_text(f"Now: ₹{total:.0f}")

        # Buy Mode big button: refresh its text/color every tick to reflect the current state
        # (also handles it being flipped back off automatically by a failed toggle attempt).
        # Solid green when ON, solid red when OFF -- no pulse/blink (per request: "no need to be
        # blinking").
        if ui_refs.get('buy_mode_button'):
            if params.get('options_buy_mode', False):
                ui_refs['buy_mode_button'].set_text('🟢 OPTIONS BUY MODE: ON  (tap to switch to SELLING)')
                ui_refs['buy_mode_button'].classes(replace='w-full h-14 text-base font-extrabold rounded-xl shadow-lg bg-green-600 text-white hover:bg-green-700 tracking-wide')
            else:
                ui_refs['buy_mode_button'].set_text('🔴 OPTIONS SELLING MODE  (tap to switch to BUY MODE)')
                ui_refs['buy_mode_button'].classes(replace='w-full h-14 text-base font-extrabold rounded-xl shadow-lg bg-red-600 text-white hover:bg-red-700 tracking-wide')

        # Buy Mode badge suffix: shows BUY on positions opened while Options Buy Mode is on,
        # driven by the trade's own recorded direction (not the live toggle), so history stays
        # correct even if the mode is switched back after the position closes.
        tc = shared_state['active_trades']['Call']
        if tc:
            is_buy_c = tc.get('direction', 'SELL') == 'BUY'
            status_txt = 'OPEN (BUY)' if is_buy_c else 'OPEN'
            status_cls = 'text-[10px] font-bold text-white bg-blue-600 px-2 rounded animate-pulse' if is_buy_c else 'text-[10px] font-bold text-white bg-red-600 px-2 rounded animate-pulse'
            ui_refs['call_status'].set_text(status_txt); ui_refs['call_status'].classes(replace=status_cls)
            ui_refs['call_info'].set_text(f"Time: {tc['entry_time']}"); ui_refs['call_trigger'].set_text(f"Trig: {tc['trigger']}")
            ui_refs['call_main_strike'].set_text(f"M ({tc['main']['strike']})")
            ui_refs['call_main_open'].set_text(f"{tc['main']['entry_price']:.1f}")
            ui_refs['call_main_curr'].set_text(f"{tc['main']['current_price']:.1f}")
            if tc['hedge']:
                ui_refs['call_hedge_strike'].set_text(f"H ({tc['hedge']['strike']})")
                ui_refs['call_hedge_open'].set_text(f"{tc['hedge']['entry_price']:.1f}")
                ui_refs['call_hedge_curr'].set_text(f"{tc['hedge']['current_price']:.1f}")
            else:
                ui_refs['call_hedge_strike'].set_text('-')
                ui_refs['call_hedge_open'].set_text('--')
                ui_refs['call_hedge_curr'].set_text('--')
            ui_refs['call_idx_open'].set_text(f"{tc['index_entry_price']:.0f}")
            ui_refs['call_idx_curr'].set_text(f"{tc['index_current_price']:.0f}")
            c = 'text-green-600' if tc['pnl']>=0 else 'text-red-600'
            ui_refs['call_pnl'].set_text(f"₹ {tc['pnl']:.0f}")
            ui_refs['call_pnl'].classes(replace=f"text-xl font-bold {c} font-mono")
        else:
            ui_refs['call_status'].set_text('INACTIVE'); ui_refs['call_status'].classes(replace='text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
            ui_refs['call_info'].set_text('T: --'); ui_refs['call_trigger'].set_text('Trig: --')
            for k in ['call_main_strike','call_hedge_strike']: ui_refs[k].set_text('-')
            for k in ['call_main_open','call_main_curr','call_hedge_open','call_hedge_curr']: ui_refs[k].set_text('0.0')
            for k in ['call_idx_open','call_idx_curr']: ui_refs[k].set_text('0')
            ui_refs['call_pnl'].set_text('₹ 0')
        
        tp = shared_state['active_trades']['Put']
        if tp:
            is_buy_p = tp.get('direction', 'SELL') == 'BUY'
            status_txt_p = 'OPEN (BUY)' if is_buy_p else 'OPEN'
            status_cls_p = 'text-[10px] font-bold text-white bg-blue-600 px-2 rounded animate-pulse' if is_buy_p else 'text-[10px] font-bold text-white bg-green-600 px-2 rounded animate-pulse'
            ui_refs['put_status'].set_text(status_txt_p); ui_refs['put_status'].classes(replace=status_cls_p)
            ui_refs['put_info'].set_text(f"Time: {tp['entry_time']}"); ui_refs['put_trigger'].set_text(f"Trig: {tp['trigger']}")
            ui_refs['put_main_strike'].set_text(f"M ({tp['main']['strike']})")
            ui_refs['put_main_open'].set_text(f"{tp['main']['entry_price']:.1f}")
            ui_refs['put_main_curr'].set_text(f"{tp['main']['current_price']:.1f}")
            if tp['hedge']:
                ui_refs['put_hedge_strike'].set_text(f"H ({tp['hedge']['strike']})")
                ui_refs['put_hedge_open'].set_text(f"{tp['hedge']['entry_price']:.1f}")
                ui_refs['put_hedge_curr'].set_text(f"{tp['hedge']['current_price']:.1f}")
            else:
                ui_refs['put_hedge_strike'].set_text('-')
                ui_refs['put_hedge_open'].set_text('--')
                ui_refs['put_hedge_curr'].set_text('--')
            ui_refs['put_idx_open'].set_text(f"{tp['index_entry_price']:.0f}")
            ui_refs['put_idx_curr'].set_text(f"{tp['index_current_price']:.0f}")
            c = 'text-green-600' if tp['pnl']>=0 else 'text-red-600'
            ui_refs['put_pnl'].set_text(f"₹ {tp['pnl']:.0f}")
            ui_refs['put_pnl'].classes(replace=f"text-xl font-bold {c} font-mono")
        else:
            ui_refs['put_status'].set_text('INACTIVE'); ui_refs['put_status'].classes(replace='text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
            ui_refs['put_info'].set_text('T: --'); ui_refs['put_trigger'].set_text('Trig: --')
            for k in ['put_main_strike','put_hedge_strike']: ui_refs[k].set_text('-')
            for k in ['put_main_open','put_main_curr','put_hedge_open','put_hedge_curr']: ui_refs[k].set_text('0.0')
            for k in ['put_idx_open','put_idx_curr']: ui_refs[k].set_text('0')
            ui_refs['put_pnl'].set_text('₹ 0')

        # --- OPEN POSITIONS section (kept alongside the banner cards above) ---
        open_count = 0

        if tc and ui_refs.get('call_pos_symbol'):
            open_count += 1
            strike = tc['main']['strike']; mark = tc['main']['current_price']; entry = tc['main']['entry_price']
            dir_suffix_c = ' (BUY)' if tc.get('direction', 'SELL') == 'BUY' else ''
            ui_refs['call_pos_symbol'].set_text(f"{params['trading_index']} {int(strike)} CE{dir_suffix_c}")
            ui_refs['call_pos_mark'].set_text(f"{mark:.2f}")
            ui_refs['call_pos_size'].set_text(f"{tc['qty']}")
            ui_refs['call_pos_entry'].set_text(f"{entry:.2f}")
            ui_refs['call_pos_qty'].set_text(f"{tc['qty']}")
            c = 'text-green-600' if tc['pnl'] >= 0 else 'text-red-600'
            ui_refs['call_pos_pnl'].set_text(f"₹ {tc['pnl']:.0f}")
            ui_refs['call_pos_pnl'].classes(replace=f"font-mono font-bold text-sm {c}")
            if ui_refs.get('call_pos_side_label'):
                if tc.get('direction', 'SELL') == 'BUY':
                    ui_refs['call_pos_side_label'].set_text('BUY')
                    ui_refs['call_pos_side_label'].classes(replace='bg-blue-100 text-blue-700 text-[10px] font-bold px-2 py-0.5 rounded')
                else:
                    ui_refs['call_pos_side_label'].set_text('SHORT')
                    ui_refs['call_pos_side_label'].classes(replace='bg-red-100 text-red-700 text-[10px] font-bold px-2 py-0.5 rounded')

        if tp and ui_refs.get('put_pos_symbol'):
            open_count += 1
            strike = tp['main']['strike']; mark = tp['main']['current_price']; entry = tp['main']['entry_price']
            dir_suffix_p = ' (BUY)' if tp.get('direction', 'SELL') == 'BUY' else ''
            ui_refs['put_pos_symbol'].set_text(f"{params['trading_index']} {int(strike)} PE{dir_suffix_p}")
            ui_refs['put_pos_mark'].set_text(f"{mark:.2f}")
            ui_refs['put_pos_size'].set_text(f"{tp['qty']}")
            ui_refs['put_pos_entry'].set_text(f"{entry:.2f}")
            ui_refs['put_pos_qty'].set_text(f"{tp['qty']}")
            c = 'text-green-600' if tp['pnl'] >= 0 else 'text-red-600'
            ui_refs['put_pos_pnl'].set_text(f"₹ {tp['pnl']:.0f}")
            ui_refs['put_pos_pnl'].classes(replace=f"font-mono font-bold text-sm {c}")
            if ui_refs.get('put_pos_side_label'):
                if tp.get('direction', 'SELL') == 'BUY':
                    ui_refs['put_pos_side_label'].set_text('BUY')
                    ui_refs['put_pos_side_label'].classes(replace='bg-blue-100 text-blue-700 text-[10px] font-bold px-2 py-0.5 rounded')
                else:
                    ui_refs['put_pos_side_label'].set_text('LONG')
                    ui_refs['put_pos_side_label'].classes(replace='bg-green-100 text-green-700 text-[10px] font-bold px-2 py-0.5 rounded')

        # Row classes AND visibility are ALWAYS set together via comp._set_position_row_style --
        # never call .classes(replace=...) or .set_visibility(...) on these rows separately (that
        # split caused the earlier bug: replacing classes silently wiped the 'hidden' class,
        # leaving the row visible with no trade). Called unconditionally every tick, for both
        # sides, regardless of whether tc/tp exist.
        if ui_refs.get('call_pos_row'):
            comp._set_position_row_style(ui_refs['call_pos_row'], 'Call', params.get('options_buy_mode', False), tc)
        if ui_refs.get('put_pos_row'):
            comp._set_position_row_style(ui_refs['put_pos_row'], 'Put', params.get('options_buy_mode', False), tp)

        if ui_refs.get('open_positions_count'):
            ui_refs['open_positions_count'].set_text(f"{open_count} position{'s' if open_count != 1 else ''}")
    except RuntimeError as e:
        # Stale-client tick from a browser tab that disconnected between the timer firing and
        # this function running -- not a logic error, nothing to recover, just skip this tick.
        if 'client' in str(e).lower() and 'deleted' in str(e).lower():
            return
        raise

def custom_render_master_banner(update_lots_callback):
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
                        ui.label('Unrealized').classes('text-orange-800 text-[9px] uppercase tracking-wider')
                        ui_refs['pnl_unrealized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-gray-800 leading-none')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Realized').classes('text-orange-800 text-[9px] uppercase tracking-wider')
                        ui_refs['pnl_realized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-green-700 leading-none')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Total').classes('text-orange-800 text-[9px] uppercase tracking-wider')
                        ui_refs['pnl_total'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-gray-800 leading-none')

        # --- BIG STANDALONE OPTIONS BUY MODE BUTTON ---
        # Made deliberately large, full-width, and high-contrast (not folded into the small
        # toggle row below) since a mode-switch this consequential (changes entry/exit
        # direction on both legs) needs to be unmissable rather than another row toggle among
        # several. Solid green = Buy Mode ON, solid red = Selling Mode (no pulse/blink). Text
        # and color are refreshed every tick in update_ui() so it always reflects the true
        # current state, including if a toggle attempt is auto-reverted (e.g. blocked because
        # a position is open). Initial colors here match the update_ui() default (OFF/red)
        # since options_buy_mode defaults to False.
        with ui.card().classes('w-full p-0 bg-transparent shadow-none rounded-none border-none'):
            buy_mode_btn = ui.button(
                '🔴 OPTIONS SELLING MODE  (tap to switch to BUY MODE)',
                on_click=lambda: on_buy_mode_button_click()
            ).classes('w-full h-14 text-base font-extrabold rounded-xl shadow-lg bg-red-600 text-white hover:bg-red-700 tracking-wide')
            ui_refs['buy_mode_button'] = buy_mode_btn

        with ui.card().classes('w-full p-2 bg-orange-50 flex-row items-center gap-6 rounded-none border-x border-orange-200'):
            with ui.row().classes('items-center gap-2'):
                ui.label('Index:').classes('font-bold text-orange-900 text-xs')
                ui.radio(UI_OPTS['indices'], value=params['trading_index'], on_change=update_lots_callback).bind_value(params, 'trading_index').props('inline dense')
            with ui.row().classes('items-center gap-2'):
                ui.label('Live Trading:').classes('font-bold text-orange-900 text-xs ml-4')
                ui.radio(UI_OPTS['on_off'], value=params['live_trading']).bind_value(params, 'live_trading').props('inline dense')
            with ui.row().classes('items-center gap-2 ml-4 border-l pl-4 border-orange-300'):
                with ui.column().classes('gap-0'):
                    ui.label('HEDGELESS').classes('font-bold text-purple-900 text-[10px] leading-none')
                    ui.label('No hedge buy').classes('text-[8px] font-mono text-purple-600 leading-none')
                # Disabled (greyed) whenever Options Buy Mode is on, since Buy Mode is always
                # hedgeless -- keeps the banner from looking contradictory. bind_enabled_from
                # reads a boolean, so it's inverted via a tiny backward transform.
                hedgeless_toggle = ui.toggle(['On', 'Off'], value='On' if params['hedgeless_mode'] else 'Off',
                    on_change=lambda e: params.update({'hedgeless_mode': e.value == 'On'})
                ).props('dense').classes('text-xs')
                hedgeless_toggle.bind_enabled_from(params, 'options_buy_mode', backward=lambda v: not v)
            with ui.row().classes('items-center gap-2 ml-4 border-l pl-4 border-orange-300'):
                with ui.column().classes('gap-0'):
                    ui.label('ENTER VIA STOP').classes('font-bold text-teal-900 text-[10px] leading-none')
                    ui.label('1m/5m/15m/60m -> real stop').classes('text-[8px] font-mono text-teal-600 leading-none')
                # Default ON (params['enter_via_stop'] defaults True in config.py). Live-bound
                # directly to params -- unlike the numeric draft-and-commit fields elsewhere in
                # this app, this is a plain boolean read at decision points (logic_engine.py /
                # stop_via_candle_engine.py), never polled against a numeric threshold, so a
                # direct bind carries none of the mid-edit risk that pattern exists to avoid.
                ui.switch(value=params.get('enter_via_stop', True)).bind_value(params, 'enter_via_stop').props('dense color=teal')
                # Tick N/S + Fetch Delay: draft-and-commit (typing has zero live effect until
                # Set is clicked), matching the pattern used throughout this app for any
                # numeric field whose change should be deliberate and recorded in the Trade
                # Event Log. Previously the two tick fields were live-bound directly to params
                # -- correctness-wise that carried no mid-edit risk (get_enter_via_stop_tick()/
                # the engine's own fetch-delay read only poll at the moment a job hands off,
                # never continuously against a live threshold), but switching to draft+Set
                # makes every change explicit and logged, matching the rest of the app. Fetch
                # Delay (enter_via_stop_fetch_delay_sec, read by stop_via_candle_engine.py's
                # _fetch_delay_sec()) had no UI control anywhere before this; it's exposed here
                # alongside the two tick buffers since all three are tuned together.
                ev_draft = {
                    'tick_nifty': params.get('enter_via_stop_tick_nifty', 1),
                    'tick_sensex': params.get('enter_via_stop_tick_sensex', 2.0),
                    'fetch_delay': params.get('enter_via_stop_fetch_delay_sec', 5),
                }
                with ui.column().classes('gap-0'):
                    ui.label('Tick N/S/Delay').classes('text-[8px] text-gray-400 leading-none')
                    with ui.row().classes('gap-1 items-center'):
                        ui.input().bind_value(ev_draft, 'tick_nifty') \
                            .props('outlined dense bg-color=white').classes('w-12 text-[10px]')
                        ui.input().bind_value(ev_draft, 'tick_sensex') \
                            .props('outlined dense bg-color=white').classes('w-12 text-[10px]')
                        ui.input().bind_value(ev_draft, 'fetch_delay') \
                            .props('outlined dense bg-color=white').classes('w-12 text-[10px]')
                        ui.button('Set', on_click=lambda: _set_enter_via_stop_values(ev_draft)) \
                            .props('dense size=sm color=teal').classes('text-[10px]')
            with ui.row().classes('items-center gap-2 ml-4 border-l pl-4 border-orange-300'):
                with ui.column().classes('gap-0'):
                    ui.label('FUTURES MODE').classes('font-bold text-indigo-900 text-[10px] leading-none')
                    # Shows the resolved near-month future symbol for the currently active
                    # trading_index while the mode is on (refreshed every tick in update_ui());
                    # shows a neutral caption while off. Default text matches the OFF state
                    # since futures_mode defaults to False.
                    ui_refs['futures_mode_symbol_label'] = ui.label('spot price -> strike').classes('text-[8px] font-mono text-indigo-600 leading-none')
                # Default OFF (params['futures_mode'] defaults False in config.py). Uses
                # on_change (not a direct bind) since toggling this needs a side effect --
                # cancelling any pending Enter via Stop jobs, since their candle-basis just
                # changed mid-flight (see on_futures_mode_change below).
                ui.switch(value=params.get('futures_mode', False), on_change=on_futures_mode_change).props('dense color=indigo')

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


comp.render_master_banner = custom_render_master_banner

def run_daily_scan():
    global kite, api_key, access_token
    
    # TIMESTAMP HELPER
    now_str = datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
    logic.log_action(f"[{now_str}] 🔄 Starting Daily Scan & Login...")

    try:
        new_kite, new_key, new_token = get_kite_session()
        if new_kite:
            kite, api_key, access_token = new_kite, new_key, new_token
            logic.log_action('✅ Session Refreshed Successfully.')
        else:
            logic.log_action('❌ Refresh Failed. Session Invalid.')
            return 
    except Exception as e:
        logic.log_action(f'❌ Auth Error: {str(e)}')
        return

    # 1. Update Managers with New Kite Object
    inst_manager.kite = kite
    
    # 2. Fetch Instruments
    logic.log_action('Scanning Instruments...')
    if inst_manager.fetch_and_process_instruments():
        
        # 3. CRITICAL: Start Ticker ONLY NOW with Valid Token
        logic.log_action('🔌 Connecting Ticker...')
        try:
            # If ticker was running (e.g. manual re-scan), stop it first
            if hasattr(ticker, 'stop'):
                try: ticker.stop()
                except: pass

            # Start Redis ticker (no token refresh needed, handled centrally)
            ticker.start()
            logic.log_action('✅ Ticker Connected & Ready.')

        except Exception as e:
            logic.log_action(f"⚠️ Ticker Start Failed: {e}")

    else:
        logic.log_action('❌ Scan Failed.')

    # 4. Futures Mode: resolve today's near-month future for BOTH indices (regardless of
    # whether the mode is currently on) and subscribe to their ticks, so switching the mode
    # on mid-session doesn't need to wait for tomorrow's scan. Failure to resolve either
    # index is logged but non-fatal -- get_eval_price/get_eval_token simply fall back to
    # spot for that index until it succeeds (e.g. on the next daily scan).
    for idx_name in INDICES:
        try:
            fut_token, fut_symbol_or_err = inst_manager.get_near_month_future(idx_name)
            if fut_token:
                shared_state[idx_name]['fut_token'] = fut_token
                shared_state[idx_name]['fut_symbol'] = fut_symbol_or_err
                ticker.subscribe_new([fut_token])
                logic.log_action(f"📈 Futures Mode: {idx_name} near-month future resolved", fut_symbol_or_err)
            else:
                logic.log_action(f"⚠️ Futures Mode: could not resolve {idx_name} future", str(fut_symbol_or_err))
        except Exception as e:
            logic.log_action(f"⚠️ Futures Mode: {idx_name} future resolution error", str(e))

def update_lots(e): 
    if e.value in INDICES: params['lots'] = 10 if e.value == 'NIFTY' else 10; ui.notify(f"Switched to {e.value}")

def on_futures_mode_change(e):
    """Futures Mode banner switch. Any pending Enter via Stop job (either direction of this
    flip) is cancelled outright via stop_via_candle_engine.cancel_all() -- its candle basis
    (spot vs future) just changed mid-flight, so a job already awaiting a candle fetch under
    the OLD basis should not hand off under the NEW one. This mirrors the existing
    LogicEngine._check_global_limits() usage of the same cancel_all() call. No other state is
    touched: existing open positions, already-armed live orders, and index stop/target values
    already on the params are left exactly as they are (they'll simply be evaluated against
    the new basis on the very next tick via get_eval_price(), same as any other params change
    takes effect immediately)."""
    params['futures_mode'] = e.value
    stop_via_candle_engine.cancel_all("Futures Mode toggled")
    if e.value:
        ui.notify("Futures Mode ON: entries/exits/alerts now use near-month future price. Strikes stay spot-based.", type='warning')
    else:
        ui.notify("Futures Mode OFF: back to spot price for entries/exits/alerts.", type='info')

def _set_enter_via_stop_values(draft):
    """Commits the Enter via Stop Tick N/Tick S/Fetch Delay draft fields (banner) to params
    in one step -- typing alone has no live effect until this Set button is clicked, matching
    the draft-and-commit pattern used throughout ui_components.py. Logs the three new values
    in one Trade Event Log line; toast text deliberately avoids SET/ACTIVATED/RESET/ARMED/
    CLEARED (auto_run's global ui.notify interceptor reacts to those) so this produces exactly
    one log line, not a duplicated 'MANUAL ACTION' entry on top of it."""
    try:
        tick_nifty = float(draft['tick_nifty'])
        tick_sensex = float(draft['tick_sensex'])
        fetch_delay = float(draft['fetch_delay'])
    except (ValueError, TypeError):
        ui.notify("Invalid Enter via Stop value(s)", type='negative')
        return
    if fetch_delay < 0:
        ui.notify("Fetch Delay cannot be negative", type='negative')
        return
    params['enter_via_stop_tick_nifty'] = tick_nifty
    params['enter_via_stop_tick_sensex'] = tick_sensex
    params['enter_via_stop_fetch_delay_sec'] = fetch_delay
    logic.log_action(f"⚙️ Enter via Stop values updated: Tick N={tick_nifty}, Tick S={tick_sensex}, Fetch Delay={fetch_delay:.0f}s")
    ui.notify("Enter via Stop values updated", type='positive')

def on_buy_mode_button_click():
    """Handles the big standalone Options Buy Mode button (toggles the opposite of current
    state). Reuses the exact same guard logic as the old small toggle: blocked while any
    position is open, and mutually exclusive with Auto Pilot."""
    if getattr(logic, 'v2', None) is not None:
        ui.notify("Positions V2 has separate Buy cards; the Buy Mode switch is not used.", type='warning')
        return
    turning_on = not params.get('options_buy_mode', False)

    if turning_on:
        if shared_state['active_trades']['Call'] is not None or shared_state['active_trades']['Put'] is not None:
            ui.notify("Close open positions before switching to Options Buy Mode.", type='negative')
            return
        params['options_buy_mode'] = True
        params['hedgeless_mode'] = True
        ui.notify("Options Buy Mode ON: Call=Buy CE, Put=Buy PE, always hedgeless.", type='warning')
    else:
        params['options_buy_mode'] = False
        ui.notify("Options Buy Mode OFF: back to option selling.", type='info')

def handle_open(side):
    s, m = logic.open_position(side); ui.notify(m, type='positive' if s else 'negative')

def handle_close(side):
    s, m = logic.close_position(side)
    if s:  # If the manual close was successful
        controller.clear_leg_fields(side)  # Wipe the fields
    ui.notify(m, type='positive' if s else 'negative')

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

def handle_close_all():
    """Wrapper to ensure CLOSE ALL button wipes both sides of the UI. save_pnl=False: the
    daily PnL CSV (final_daily_pnl.csv) is written exactly once per day, at the EOD_TIME
    routine in AutoController.run_loop(), regardless of what closed positions intraday --
    manual Close All, Global Stop/Target, or Auto Pilot's own global stop. Positions still
    close immediately here; only the aggregate daily report write is deferred."""
    try:
        logic.close_all_positions("Manual Close All", save_pnl=False)
        controller.clear_leg_fields('Call')
        controller.clear_leg_fields('Put')
        ui.notify("All Positions Closed manually", type='info')
    except Exception as e:
        ui.notify(f"Error closing all: {e}", type='negative')

def _index_exit_wrapper(side):
    """The outer 'Call Exit based on Index' / 'Put Exit based on Index' wrapper card. Uses
    the SAME mode-aware color helpers as every card in ui_components.py (comp._side_is_red /
    comp._bind_card_colors) so it never mismatches the index_exit_component cards nested
    inside it. Previously this wrapper's colors were hardcoded (Call=bg-red-50, Put=
    bg-green-50) independent of options_buy_mode, which caused it to visually clash with the
    correctly mode-aware sub-cards inside it in Buy Mode."""
    init_cls, _ = comp._side_colors(side, params.get('options_buy_mode', False), weight='50')

    def _cls(is_red):
        return f"w-full p-3 gap-2 {'bg-red-50 border-red-200' if is_red else 'bg-green-50 border-green-200'} border shadow-sm rounded-xl"
    def _label_cls(is_red):
        return f"font-bold text-xs uppercase {'text-red-800' if is_red else 'text-green-800'}"

    init_label_cls = 'text-red-800' if comp._side_is_red(side, params.get('options_buy_mode', False)) else 'text-green-800'

    with ui.card().classes(f'w-full p-3 gap-2 {init_cls} border shadow-sm rounded-xl') as card:
        comp._bind_card_colors(card, side, _cls)
        title = ui.label(f'{side} Exit based on Index').classes(f'font-bold text-xs uppercase {init_label_cls}')
        def _apply_label(buy_mode, lbl=title, s=side):
            is_red = comp._side_is_red(s, buy_mode)
            lbl.classes(replace=_label_cls(is_red))
            return ''
        hook = ui.label('').classes('hidden')
        hook.bind_text_from(params, 'options_buy_mode', backward=_apply_label)

        with ui.row().classes('w-full gap-2'):
            if side == 'Call':
                comp.index_exit_component('Call', 'Stop', 'call_index_stop_time', 'call_index_stop_val', 'call_index_stop_active')
                comp.index_exit_component('Call', 'Tgt', 'call_index_target_time', 'call_index_target_val', 'call_index_tgt_active')
            else:
                comp.index_exit_component('Put', 'Stop', 'put_index_stop_time', 'put_index_stop_val', 'put_index_stop_active')
                comp.index_exit_component('Put', 'Tgt', 'put_index_target_time', 'put_index_target_val', 'put_index_tgt_active')

def build_left_stack():
    comp.unified_entry_card('Call', 'call', on_fire_market=lambda: handle_fire_market('Call'), on_close=lambda: handle_close('Call'), strike_validator=logic.validate_strike)
    comp.auto_close_card('Call', 'call_target_val', 'call_target_active', 'call_stop_val', 'call_stop_active')
    comp.premium_exit_card('Call')
    _index_exit_wrapper('Call')

def _render_global_pnl_floor_card():
    """Global PnL Floor card: fires close-all the moment combined realized+unrealized PnL
    drops to OR BELOW an absolute value (params['global_trailing_value'] -- internal name
    kept as global_trailing_* to minimize code churn, though this is a plain absolute-floor
    check with no peak/drawdown tracking; see LogicEngine._check_global_pnl_floor()).
    Independent of the Global Stop/Target cards above it. Same draft-and-commit input
    pattern as global_control_card (typing has zero live effect until Set is clicked), with
    a live 'Now' caption underneath so the person can see the current combined PnL right next
    to the floor they're setting."""
    draft = {'value': params.get('global_trailing_value', 0)}
    with ui.card().classes('w-full p-3 gap-2 bg-gray-50 border border-gray-200 shadow-sm rounded-xl'):
        ui.label('Global PnL Floor (Lock-in Profit)').classes('font-bold text-sm text-gray-700')
        ui.label('Closes all positions the instant total PnL falls to this level.').classes('text-[10px] text-gray-500 -mt-1 mb-1')
        ui.input().bind_value(draft, 'value').props('outlined dense bg-color=white prefix="₹"').classes('w-full')
        ui_refs['trailing_stop_caption'] = ui.label('Now: ₹0').classes('text-[10px] text-gray-500')
        status = ui.label().classes('w-full text-center text-xs font-bold text-white bg-blue-600 rounded p-1 shadow-sm')
        status.bind_visibility_from(params, 'global_trailing_active')
        def activate():
            try:
                value = float(draft['value'])
            except (ValueError, TypeError):
                ui.notify("Invalid PnL Floor Value", type='negative'); return
            params['global_trailing_value'] = value; params['global_trailing_active'] = True
            status.set_text(f"ACTIVE: Floor ₹{value:.0f}")
            ui.notify("Global PnL Floor SET", type='positive')
            comp._log_alert_action(f"⚙️ Global PnL Floor SET: ₹{value:.0f}")
        def reset():
            params['global_trailing_active'] = False; params['global_trailing_value'] = 0; draft['value'] = 0
            ui.notify("Global PnL Floor RESET", type='info')
            comp._log_alert_action("⚙️ Global PnL Floor RESET")
        with ui.row().classes('w-full gap-2'):
            ui.button('Set', color='blue-7', on_click=activate).classes('grow h-8 text-xs rounded-lg')
            ui.button('Reset', on_click=reset).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')

def build_center_stack():
    ui.button('Run 9 AM Daily Scan', on_click=run_daily_scan).classes('bg-orange-200 text-orange-900 w-full shadow-md rounded-xl h-12 font-bold')
    comp.global_control_card('Global Stop Loss', 'global_stop_value', 'global_stop_active')
    comp.global_control_card('Global Target', 'global_target_value', 'global_tgt_active')
    _render_global_pnl_floor_card()
    comp.render_alert_sound_panel()
    comp.alerts_card_upper()
    comp.alerts_card_lower()
    
    # FIX: Point to our new wrapper that includes the UI wipe
    ui.button('CLOSE ALL', on_click=handle_close_all, color='red').classes('w-full font-bold shadow-md rounded-xl mt-4')

def build_right_stack():
    comp.unified_entry_card('Put', 'put', on_fire_market=lambda: handle_fire_market('Put'), on_close=lambda: handle_close('Put'), strike_validator=logic.validate_strike)
    comp.auto_close_card('Put', 'put_target_val', 'put_target_active', 'put_stop_val', 'put_stop_active')
    comp.premium_exit_card('Put')
    _index_exit_wrapper('Put')


# ==========================================
#      BACKGROUND LOGIC LOOP (PERSISTENT)
# ==========================================

async def run_bot_logic():
    """Runs the bot logic forever, independent of the UI."""
    last_trade_count = 0
    
    while True:
        try:
            if shared_state.get('shutdown_triggered', False): 
                break

            now = datetime.now()
            ts = now.strftime('%H:%M:%S')

            # 1. NIGHT MODE
            if now.hour == 22 and now.minute == 45:
                shared_state['shutdown_triggered'] = True 
                print(f"[{ts}] 💤 NIGHT MODE: Saving State & Shutting Down...")
                try:
                    if hasattr(ticker, 'close'): ticker.close()
                    if hasattr(ticker, 'stop'): ticker.stop() 
                except: pass
                app.shutdown()
                return

            # 2. DAILY INIT
            if now.time() >= dtime(9, 0) and not shared_state.get('daily_scan_done', False):
                print(f"[{ts}] 🚀 INITIALIZING BOT: Running Daily Scan...")
                run_daily_scan()
                controller.reset()
                shared_state['daily_scan_done'] = True
                shared_state['daily_pnl_written'] = False

            # --- CORE LOGIC (SAFE CONTEXT WRAPPER) ---
            # This PREVENTS 'Critical Logic Error' by redirecting ui.notify
            try:
                with safe_ui_context():
                    controller.run_loop()
                    
                    if datetime.today().weekday() not in [5, 6]:
                        if now.time() < EOD_TIME:
                            logic.check_triggers()
                            pattern_engine.check_patterns()
                            indicator_engine.check()
                        else:
                            if params['short_trigger_active'] or params['long_trigger_active']:
                                params['short_trigger_active'] = False
                                params['long_trigger_active'] = False

                    # Enter via Stop: live-monitors/executes any deferred candle-stop jobs
                    # every tick, regardless of weekday/EOD_TIME gating above -- a job
                    # already armed should keep being checked so it's never silently
                    # abandoned; if its position closes via the EOD routine or any other
                    # route in the meantime, the engine drops that job harmlessly on its own.
                    stop_via_candle_engine.check()

                    logic.update_pnl()
                    
                    # WIPE FIELDS FOR ANY CLOSES
                    if 'reset_queue' not in shared_state: shared_state['reset_queue'] = []
                    while shared_state['reset_queue']:
                        closed_side = shared_state['reset_queue'].pop(0)
                        controller.clear_leg_fields(closed_side)
                    
                    # SOUND MONITOR
                    current_count = (1 if shared_state['active_trades']['Call'] else 0) + \
                                    (1 if shared_state['active_trades']['Put'] else 0)
                    if current_count > last_trade_count:
                        if 'open' not in shared_state['sound_queue']: shared_state['sound_queue'].append('open')
                    elif current_count < last_trade_count:
                        if 'close' not in shared_state['sound_queue'] and 'success' not in shared_state['sound_queue'] and 'error' not in shared_state['sound_queue']:
                            shared_state['sound_queue'].append('close')
                    last_trade_count = current_count

            except Exception as e:
                print(f"⚠️ Critical Logic Error (Recovering...): {e}")

            if now.second == 0:
                daily_logger.log_snapshot(controller.mode)

        except Exception as e:
            print(f"⚠️ Main Loop Error: {e}")
        
        await asyncio.sleep(1)

# ==========================================
#      MAIN UI PAGE (SAFE MODE)
# ==========================================

def _build_v2_page():
    """Positions V2 page (only when logic.v2 is set): red cards left, green right, Open Positions
    directly under the chart / Trade Event Log row, Open Orders per position. Banner (incl. the
    CALL/PUT POSITION cards, fed by the active_trades mirror), alerts, patterns, indicators and
    order history are the same components as the legacy page."""
    from ui_components import v2_ui
    eng = logic.v2
    comp.render_master_banner(update_lots)
    buy_btn = ui_refs.get('buy_mode_button')
    if buy_btn is not None:      # Buy cards replace the global Buy Mode switch (display:none survives classes(replace=))
        buy_btn.style('display: none !important')

    with ui.row().classes('w-full px-0 gap-0'):
        comp.render_chart_row()
        comp.render_log_row()

    v2_ui.render_positions(eng)

    with ui.row().classes('w-full no-wrap items-start gap-4 p-2'):
        with ui.column().classes('grow gap-4'):
            v2_ui.entry_card(eng, 'call')
            v2_ui.entry_card(eng, 'bput')
        with ui.column().classes('grow gap-4'): build_center_stack()
        with ui.column().classes('grow gap-4'):
            v2_ui.entry_card(eng, 'put')
            v2_ui.entry_card(eng, 'bcall')

    comp.render_active_alerts()
    v2_ui.render_open_orders(eng)
    comp.render_pattern_indicators()
    render_indicator_alerts_section()
    comp.render_order_history()
    ui.timer(1.0, update_ui)


@ui.page('/', title='Zerodha Bot')
def index():
    if getattr(logic, 'v2', None) is not None:
        _build_v2_page()
        return
    _build_legacy_page()


def _build_legacy_page():
    # 1. Build UI Elements
    comp.render_master_banner(update_lots)
    
    with ui.row().classes('w-full px-0 gap-0'): 
        comp.render_chart_row()
        comp.render_log_row()

    with ui.row().classes('w-full no-wrap items-start gap-4 p-2'):
        with ui.column().classes('grow gap-4'): build_left_stack()
        with ui.column().classes('grow gap-4'): build_center_stack()
        with ui.column().classes('grow gap-4'): build_right_stack()

    # Active Alerts: full width, directly above Open Positions
    comp.render_active_alerts()

    # Open Positions (kept alongside the banner CALL/PUT POSITION cards)
    comp.render_open_positions(on_close_call=lambda: handle_close('Call'), on_close_put=lambda: handle_close('Put'))

    # Order Book (pending Limit/Stop-Market triggers, table-styled), Candlestick Pattern
    # Indicators (below Open Orders), Indicator Alerts (EMA/SMA, below Pattern Indicators),
    # and Order History (full tradebook)
    comp.render_orderbook()
    comp.render_pattern_indicators()
    render_indicator_alerts_section()
    comp.render_order_history()

    # 2. LOCAL TIMER
    # This ensures the UI updates ONLY when this page is open in a browser.
    # It reads the data from 'shared_state' populated by the background loop.
    ui.timer(1.0, update_ui)

# Start the Bot Logic in the background on startup
app.on_startup(lambda: asyncio.create_task(run_bot_logic()))

# ==========================================
#      MANAGER / WORKER BOOTSTRAP
# ==========================================

if __name__ in {"__main__", "__mp_main__"}:
    import asyncio # Ensure asyncio is available
    
    # --- WORKER MODE ---
    if "--worker" in sys.argv:
        shared_state['daily_scan_done'] = False

        print("--- WORKER STARTED ---")

        # Run the App (Blocking)
        ui.run(title='Zerodha Bot', host='0.0.0.0', port=9000, reload=False, show=False)
        
        # CLEAN EXIT (No more SystemExit crashes)
        sys.exit(0)

    # --- MANAGER MODE ---
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- MANAGER STARTED ---")
        
        # Clear old state file on fresh start
        if os.path.exists(".automode_state"): os.remove(".automode_state")
        
        while True:
            try:
                # Start Worker
                cmd = [sys.executable, __file__, "--worker"]
                p = subprocess.Popen(cmd)
                p.wait() 
                
                now = datetime.now()
                ts = now.strftime('%Y-%m-%d %H:%M:%S')
                
                # Sleep Logic
                if (now.weekday() == 4 and now.hour >= 22) or now.weekday() in [5, 6]:
                    print(f"[{ts}] 📅 WEEKEND DETECTED. Sleeping until Monday...")
                    days_ahead = 7 - now.weekday()
                    if now.weekday() == 4: days_ahead = 3
                    if now.weekday() == 6: days_ahead = 1
                    target = now.replace(hour=8, minute=55, second=0, microsecond=0) + timedelta(days=days_ahead)
                    sleep_seconds = (target - now).total_seconds()
                    time.sleep(max(1, sleep_seconds))

                elif now.hour >= 22 or now.hour < 8:
                    print(f"[{ts}] 💤 NIGHT MODE: Sleeping until Morning...")
                    target = now.replace(hour=8, minute=55, second=0, microsecond=0)
                    if now.hour >= 22: target += timedelta(days=1)
                    sleep_seconds = (target - now).total_seconds()
                    time.sleep(max(1, sleep_seconds))
                else:
                    print(f"[{ts}] ⚠️ WORKER RESTARTING IN 2s...")
                    time.sleep(2)

            except KeyboardInterrupt:
                print("\n--- MANAGER STOPPING ---")
                if 'p' in locals(): p.terminate()
                break

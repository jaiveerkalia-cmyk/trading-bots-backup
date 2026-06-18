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
from config import shared_state, params, ui_refs, INDICES, UI_OPTS
import ui_components as comp
from auth_manager import get_kite_session
from ticker_engine import TickerClient
from instrument_manager import InstrumentManager
from logic_engine import LogicEngine

# ==========================================
#      CONFIGURATION & CONSTANTS
# ==========================================

class AutoConfig:
    START_TIME = dtime(11, 15, 5)
    SQ_OFF_TIME = dtime(15, 19, 0)
    
    BUF_NIFTY = 1.0
    BUF_SENSEX = 3.5
    STRIKE_OFFSET = 2
    PRI_TARGET_PER_LOT = 3000
    PRI_STOP_PER_LOT = 2000
    GLB_TARGET_FLAT = 500
    GLB_STOP_PER_LOT = 3000
    # --- TRAILING SENSITIVITY CONTROL ---
    # Increase this to make trailing MORE RELAXED (e.g., 0.25 or 0.30)
    # Decrease this to make trailing MORE STRICT (e.g., 0.10 or 0.05)
    HOURLY_PROXIMITY_PCT = 0.25
    
    AVG_RANGE_PERIOD = 10   
    TINY_CANDLE_FACTOR = 0.30 

# ==========================================
#      DATA LOGGING ENGINE
# ==========================================

class DailyLogger:
    def __init__(self):
        self.log_dir = "daily_logs"
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        self.filename = f"{self.log_dir}/action_log_{datetime.now().strftime('%Y-%m-%d')}.csv"
        
        self.headers = [
            'Time', 'Mode', 
            'Total_Realized_PnL', 'Total_Unrealized_PnL',
            'Active_Call_PnL', 'Active_Put_PnL',
            'Call_LTP', 'Call_Entry', 'Call_Strike',
            'Put_LTP', 'Put_Entry', 'Put_Strike',
            'Index_LTP'
        ]
        self._ensure_header()

    def _ensure_header(self):
        # CHECK: If Saturday (5) or Sunday (6), do nothing
        if datetime.today().weekday() in [5, 6]: return 

        if not os.path.exists(self.filename):
            with open(self.filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)

    def log_snapshot(self, mode):
        # CHECK: If Saturday (5) or Sunday (6), do nothing
        if datetime.today().weekday() in [5, 6]: return

        now_str = datetime.now().strftime('%H:%M:%S')
        
        real_pnl = round(shared_state['pnl']['realized'], 2)
        unreal_pnl = round(shared_state['pnl']['unrealized'], 2)
        
        c_trade = shared_state['active_trades']['Call']
        p_trade = shared_state['active_trades']['Put']
        
        c_pnl = round(c_trade['pnl'], 2) if c_trade else 0.0
        c_ltp = c_trade['main']['current_price'] if c_trade else 0
        c_ent = c_trade['main']['entry_price'] if c_trade else 0
        c_str = c_trade['main']['strike'] if c_trade else 0
        
        p_pnl = round(p_trade['pnl'], 2) if p_trade else 0.0
        p_ltp = p_trade['main']['current_price'] if p_trade else 0
        p_ent = p_trade['main']['entry_price'] if p_trade else 0
        p_str = p_trade['main']['strike'] if p_trade else 0
        
        idx_name = params['trading_index']
        idx_ltp = 0
        if idx_name in shared_state:
            idx_ltp = shared_state[idx_name]['ltp']

        row = [
            now_str, mode,
            real_pnl, unreal_pnl,
            c_pnl, p_pnl,
            c_ltp, c_ent, c_str,
            p_ltp, p_ent, p_str,
            idx_ltp
        ]
        
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)

# ==========================================
#      AUTO CONTROLLER LOGIC
# ==========================================

class AutoController:
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

    def get_trading_index(self):
        day_idx = datetime.today().weekday()
        if day_idx in [0, 1]: return 'SENSEX'
        elif day_idx in [2, 3, 4]: return 'NIFTY'
        return None

    

    def fetch_with_retry(self, token, from_date, to_date, interval, attempts=3):
        for i in range(attempts):
            try:
                data = self.inst_mgr.kite.historical_data(token, from_date, to_date, interval)
                return data
            except Exception as e:
                if "TokenException" in str(e) or "403" in str(e):
                    self.log(f"⚠️ Token Expired. Waiting for refresh.")
                    return None
                time.sleep(2)
        return None

    def fetch_1015_candle(self, index_name):
        token = INDICES[index_name]['token']
        now = datetime.now()
        from_date = now.replace(hour=9, minute=0, second=0)
        to_date = now
        candles = self.fetch_with_retry(token, from_date, to_date, "60minute")
        
        if not candles: return False

        target_candle = None
        for c in candles:
            d = c['date']
            if hasattr(d, 'time'): c_time = d.time()
            else: c_time = datetime.strptime(d, '%Y-%m-%dT%H:%M:%S%z').time()
            if c_time.hour == 10 and c_time.minute == 15:
                target_candle = c; break
        
        if target_candle:
            self.ref_high = target_candle['high']
            self.ref_low = target_candle['low']
            self.log(f"Ref Candle Found. H: {self.ref_high}, L: {self.ref_low}")
            return True
        return False

    def find_balanced_strikes(self, index_name, call_trig, put_trig):
        """Finds ITM strikes. Goes deeper ITM only if one premium is 20% lower."""
        self.log("🔍 Starting Premium-Balanced Strike Selection (20% Rule)...")
        print("--- STRIKE SELECTION INITIATED ---")
        
        step = INDICES[index_name]['step']
        seg = INDICES[index_name]['segment']  # Get NFO or BFO
        base_call_strike = (round(call_trig / step) * step) - (AutoConfig.STRIKE_OFFSET * step)
        base_put_strike = (round(put_trig / step) * step) + (AutoConfig.STRIKE_OFFSET * step)
        
        try:
            # Extract both Token AND Trading Symbol
            c_token, c_sym = self.inst_mgr.get_atm_token(index_name, base_call_strike, 'CE')
            p_token, p_sym = self.inst_mgr.get_atm_token(index_name, base_put_strike, 'PE')
            
            self.logic.ticker.subscribe_new([c_token, p_token])
            
            # FIX 1: Format exactly as Kite requires (e.g. NFO:NIFTY24APR22500CE)
            c_quote_key = f"{seg}:{c_sym}"
            p_quote_key = f"{seg}:{p_sym}"
            
            quote_c = self.inst_mgr.kite.quote(c_quote_key)
            c_ltp = quote_c[c_quote_key]['last_price'] if c_quote_key in quote_c else 0
            
            quote_p = self.inst_mgr.kite.quote(p_quote_key)
            p_ltp = quote_p[p_quote_key]['last_price'] if p_quote_key in quote_p else 0
            
            self.log(f"📊 Base Call ({base_call_strike}) Premium: {c_ltp}")
            self.log(f"📊 Base Put ({base_put_strike}) Premium: {p_ltp}")
            print(f"Base Call ({base_call_strike}) Premium: {c_ltp} | Base Put ({base_put_strike}) Premium: {p_ltp}")
            
            if c_ltp > 0 and p_ltp > 0:
                if c_ltp < 0.8 * p_ltp:
                    base_call_strike -= step
                    msg = f"⚖️ Call premium too low (<80% of Put). Shifted Call deeper ITM to {base_call_strike}"
                    self.log(msg); print(msg)
                elif p_ltp < 0.8 * c_ltp:
                    base_put_strike += step
                    msg = f"⚖️ Put premium too low (<80% of Call). Shifted Put deeper ITM to {base_put_strike}"
                    self.log(msg); print(msg)
                else:
                    msg = "✅ Premiums are within 20% balance. Keeping base strikes."
                    self.log(msg); print(msg)
            else:
                msg = "⚠️ Could not fetch valid premiums. Defaulting to base strikes."
                self.log(msg); print(msg)
                
            return base_call_strike, base_put_strike
        except Exception as e:
            msg = f"❌ Strike balance check failed: {e}"
            self.log(msg); print(msg)
            return base_call_strike, base_put_strike

    def arm_triggers(self, index_name):
        buffer = AutoConfig.BUF_NIFTY if index_name == 'NIFTY' else AutoConfig.BUF_SENSEX
        call_trig_price = self.ref_low - buffer
        put_trig_price = self.ref_high + buffer
        
        # FIX 1: Find balanced ITM strikes based on Triggers
        call_strike, put_strike = self.find_balanced_strikes(index_name, call_trig_price, put_trig_price)
        
        params['short_open_mode'] = 'Current'
        params['short_open_amount'] = call_trig_price
        params['short_open_strike'] = call_strike
        
        params['long_open_mode'] = 'Current'
        params['long_open_amount'] = put_trig_price
        params['long_open_strike'] = put_strike
        
        params['call_index_stop_val'] = self.ref_high
        params['put_index_stop_val'] = self.ref_low
        params['call_index_stop_active'] = True
        params['put_index_stop_active'] = True
        
        params['call_entry_mode'] = 'Other'
        params['call_manual_strike'] = str(int(call_strike))
        params['put_entry_mode'] = 'Other'
        params['put_manual_strike'] = str(int(put_strike))

        params['short_trigger_active'] = True
        params['long_trigger_active'] = True
        self.log(f"ARMED: Call Sell <{call_trig_price:.2f} (Str: {call_strike}) | Put Sell >{put_trig_price:.2f} (Str: {put_strike})")
        
        shared_state['sound_queue'].append('alert')
        self.state = 'ARMED'

    def clear_leg_fields(self, side):
        """Wipes the physical text fields and disables toggles for a specific leg."""
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

    def sync_ui_parameters(self):
        if not self.is_active: return
        try: lots = int(float(params.get('lots', 1)))
        except: lots = 1
        
        tgt = AutoConfig.PRI_TARGET_PER_LOT * lots
        stop = AutoConfig.PRI_STOP_PER_LOT * lots
        
        # ONLY update if the value is different AND currently 0/empty
        if self.state == 'IN_POSITION':
            if self.active_side == 'Call' and not params.get('call_target_active'):
                if float(params.get('call_target_val', 0)) == 0: params['call_target_val'] = tgt
                if float(params.get('call_stop_val', 0)) == 0: params['call_stop_val'] = stop
                # FIX 2: Actually activate the UI switches so the engine enforces them
                params['call_target_active'] = True
                params['call_stop_active'] = True
                
            elif self.active_side == 'Put' and not params.get('put_target_active'):
                if float(params.get('put_target_val', 0)) == 0: params['put_target_val'] = tgt
                if float(params.get('put_stop_val', 0)) == 0: params['put_stop_val'] = stop
                # FIX 2: Actually activate the UI switches so the engine enforces them
                params['put_target_active'] = True
                params['put_stop_active'] = True

    def check_inside_candle(self):
        # 1. Define the specific target hour we are looking for (Current Hour - 1)
        # Example: At 12:15, we want the 11:15 candle.
        now = datetime.now()
        target_time = now.replace(minute=15, second=0, microsecond=0) - timedelta(hours=1)
        target_hour = target_time.hour
        
        token = INDICES[self.today_index]['token']
        # Fetch enough history to find the specific candle
        candles = self.fetch_with_retry(token, now - timedelta(hours=4), now, "60minute")
        
        if not candles: return

        # 2. Find the exact candle by timestamp
        target_candle = None
        for c in candles:
            # Handle different date formats (string vs datetime obj) if necessary
            c_date = c['date']
            if hasattr(c_date, 'hour'): 
                c_h, c_m = c_date.hour, c_date.minute
            else:
                dt_obj = datetime.strptime(c_date, '%Y-%m-%dT%H:%M:%S%z')
                c_h, c_m = dt_obj.hour, dt_obj.minute
            
            if c_h == target_hour and c_m == 15:
                target_candle = c
                break
        
        if not target_candle:
            self.log(f"⚠️ Could not find candle for {target_hour}:15 to check Inside Bar.")
            return

        # 3. Explicit Logging for Debugging
        curr_h = target_candle['high']
        curr_l = target_candle['low']
        self.log(f"🔍 Checking Inside Bar: {target_hour}:15 Candle (H:{curr_h}, L:{curr_l}) vs Ref (H:{self.ref_high}, L:{self.ref_low})")

        # 4. Check Condition (Strictly Inside)
        # We use a tiny epsilon or strict inequality to ensure we actually tighten
        if curr_h <= self.ref_high and curr_l >= self.ref_low:
            
            # check if it is actually tighter or just the same
            if curr_h == self.ref_high and curr_l == self.ref_low:
                self.log("ℹ️ Identical Candle. No change in triggers.")
                return

            self.log(f"🕯️ Inside Bar Confirmed! Tightening Triggers & Strikes.")
            
            # 5. Update References
            self.ref_high = curr_h
            self.ref_low = curr_l
            
            # 6. CRITICAL: Force Reset Strikes so arm_triggers calculates NEW strikes
            # If we don't clear these, arm_triggers will skip the math and keep old strikes.
            params['call_manual_strike'] = '0'
            params['put_manual_strike'] = '0'
            
            # 7. Re-Arm with new levels
            self.arm_triggers(self.today_index)
        else:
            self.log("ℹ️ Not an Inside Bar.")

    def check_second_leg_premium(self):
        if self.last_trade_entry_price == 0: return 
        side = 'Put' if shared_state['active_trades']['Call'] is None else 'Call' 
        strike_key = 'put_manual_strike' if side == 'Put' else 'call_manual_strike'
        strike = int(float(params[strike_key]))
        expiry = self.inst_mgr.get_current_expiry(self.today_index)
        
        token = None
        for inst in self.inst_mgr.instruments:
            if inst['name'] == self.today_index and inst['expiry'] == expiry and inst['strike'] == strike:
                if (side == 'Put' and inst['instrument_type'] == 'PE') or (side == 'Call' and inst['instrument_type'] == 'CE'):
                    token = inst['instrument_token']; break
        
        if token:
            try:
                quote = self.inst_mgr.kite.quote(f"NFO:{token}")
                lp = quote[str(token)]['last_price'] if str(token) in quote else 0
                if lp < self.last_trade_entry_price:
                    self.log(f"⚠️ Low Premium ({lp} < {self.last_trade_entry_price}). Shifting Strike ITM.")
                    step = INDICES[self.today_index]['step']
                    new_strike = strike + step if side == 'Put' else strike - step
                    params[strike_key] = str(new_strike)
                    self.log(f"🔄 New Reversal Strike: {new_strike}")
            except: pass
 
    def recalc_reversal_strikes(self, trigger_price):
        """Updates reversal strikes based on the TRIGGER PRICE (not current CMP)."""
        if not self.today_index: return
        
        step = INDICES[self.today_index]['step']
        # Round trigger to nearest step to get the 'ATM' level at the moment of trigger
        atm_at_trigger = round(trigger_price / step) * step
        
        # ITM Logic:
        # Put Reversal -> We want ITM Put (Strike > Trigger)
        itm_put_strike = atm_at_trigger + (AutoConfig.STRIKE_OFFSET * step)
        
        # Call Reversal -> We want ITM Call (Strike < Trigger)
        itm_call_strike = atm_at_trigger - (AutoConfig.STRIKE_OFFSET * step)
        
        params['put_manual_strike'] = str(int(itm_put_strike))
        params['call_manual_strike'] = str(int(itm_call_strike))
        
        self.log(f"♻️ Reversal Strikes Set (Base: {trigger_price}): Call {itm_call_strike} | Put {itm_put_strike}")

    def clear_leg_fields(self, side):
        """Wipes the physical text fields and disables toggles for a specific leg."""
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

    def run_loop(self):
        now = datetime.now()
        ts = now.strftime('%H:%M:%S')

        # 1. PRIORITY: GLOBAL EOD ROUTINE
        if AutoConfig.SQ_OFF_TIME <= now.time() < dtime(15, 40):
            # Wipe all fields
            self.clear_leg_fields('Call')
            self.clear_leg_fields('Put')
            
            if not shared_state.get('daily_pnl_written', False):
                self.log("⏰ 3:19 PM Day End Routine (Auto/Manual).")
                try:
                    self.logic.close_all_positions("Day End Auto-Square", save_pnl=True)
                    shared_state['daily_pnl_written'] = True
                    shared_state['sound_queue'].append('close')
                except: shared_state['daily_pnl_written'] = True
            
            if shared_state['active_trades']['Call'] is not None: shared_state['active_trades']['Call'] = None
            if shared_state['active_trades']['Put'] is not None: shared_state['active_trades']['Put'] = None
            self.state = 'DONE'
            return

        if self.state == 'DONE': return

        # 2. SYNC UI
        self.sync_ui_parameters()
        #self.log_parameter_changes() 
        
        c_trade = shared_state['active_trades']['Call']
        p_trade = shared_state['active_trades']['Put']

        has_active_trade = (c_trade is not None) or (p_trade is not None)

        # 4. AUTO MODE GUARD
        if not self.is_active and not has_active_trade:
            self.state = 'IDLE'
            self.waiting_log_sent = False
            if now.time() > dtime(15, 40):
                if now.minute % 30 == 0 and now.second == 0:
                    print(f"[{datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}] ℹ️ Market Closed. Waiting for Night Shutdown.")
            return
        
        # 5. STRATEGY EXECUTION
        trading_idx = self.get_trading_index()
        if not trading_idx:
            if self.state != 'SLEEPING': 
                self.log(f"Weekend ({now.strftime('%A')}) - Bot Sleeping")
                self.state = 'SLEEPING'
                
                # FIX: Force disarm triggers so UI doesn't light up on weekends
                params['short_trigger_active'] = False
                params['long_trigger_active'] = False
            return
        
        if self.state == 'IDLE':
            if has_active_trade:
                self.log("⚠️ Trade Detected (Manual/Trigger). Switching to Monitor Mode.")
                self.state = 'IN_POSITION'
                self.active_side = 'Call' if c_trade else 'Put'
            elif self.is_active:
                if now.time() < AutoConfig.START_TIME: return
                if dtime(11, 16) < now.time() < dtime(15, 30): return
                if now.time() >= dtime(15, 30): return
                
                self.today_index = trading_idx
                
                # FIX 1: Force the UI and Engine to switch to the correct daily index
                if params['trading_index'] != self.today_index:
                    params['trading_index'] = self.today_index
                    
                self.state = 'WAITING_FOR_DATA'

        if self.state == 'WAITING_FOR_DATA':
            if self.fetch_1015_candle(self.today_index): self.arm_triggers(self.today_index)

        if self.state == 'ARMED' and now.minute == 15 and now.second == 5:
            self.check_inside_candle()
        
        if self.state == 'ARMED':
            if c_trade: 
                self.log("Call Sell Active."); self.active_side = 'Call'; self.state = 'IN_POSITION'
                self.last_trade_entry_price = c_trade['main']['entry_price']
                params['long_trigger_active'] = False 
                shared_state['sound_queue'].append('open')
            elif p_trade: 
                self.log("Put Sell Active."); self.active_side = 'Put'; self.state = 'IN_POSITION'
                self.last_trade_entry_price = p_trade['main']['entry_price']
                params['short_trigger_active'] = False
                shared_state['sound_queue'].append('open')

        if self.state == 'IN_POSITION':
            trade = c_trade if self.active_side == 'Call' else p_trade
            if not trade: 
                self.state = 'STOPPED_OUT'; return

            # MANUAL MODE ESCAPE HATCH: If Auto Mode is OFF, skip auto-management completely
            if not self.is_active: return

            if (self.active_side == 'Call' and p_trade) or (self.active_side == 'Put' and c_trade):
                self.log("🔄 Reversal Detected. Global Limits."); self.state = 'SECOND_LEG'; return
            
            try: tgt = float(params.get(f'{self.active_side.lower()}_target_val', 0))
            except ValueError: tgt = 0.0
            try: stp = float(params.get(f'{self.active_side.lower()}_stop_val', 0))
            except ValueError: stp = 0.0
            
            # --- TARGET HIT ---
            if params.get(f'{self.active_side.lower()}_target_active') and tgt > 0 and trade['pnl'] >= tgt:
                self.log("🎯 Target Hit. All Triggers DISABLED.")
                try: self.logic.close_position(self.active_side, f"Auto Target {tgt}")
                except: pass
                
                self.clear_leg_fields('Call')
                self.clear_leg_fields('Put')
                self.state = 'DONE'
                shared_state['active_trades'][self.active_side] = None
                return
            
            # --- STOP HIT ---
            if params.get(f'{self.active_side.lower()}_stop_active') and stp > 0 and trade['pnl'] <= -stp:
                self.state = 'STOPPED_OUT'; self.log("🛑 PnL Stop Hit.")
                try: self.logic.close_position(self.active_side, f"Auto SL {stp}")
                except: pass
                
                closed_side = self.active_side
                shared_state['active_trades'][self.active_side] = None
                
                self.clear_leg_fields(closed_side)
                idx_ltp = shared_state[self.today_index]['ltp']
                self.recalc_reversal_strikes(idx_ltp) 
                
                if closed_side == 'Call': params['long_trigger_active'] = True
                else: params['short_trigger_active'] = True 
                return

            # --- INDEX STOP ---
            idx_ltp = shared_state[self.today_index]['ltp']
            idx_stop_hit = False
            
            try: call_stop_val = float(params.get('call_index_stop_val', 999999)) if str(params.get('call_index_stop_val', '')).strip() != '' else 999999.0
            except ValueError: call_stop_val = 999999.0
            try: put_stop_val = float(params.get('put_index_stop_val', 0)) if str(params.get('put_index_stop_val', '')).strip() != '' else 0.0
            except ValueError: put_stop_val = 0.0
            
            if self.active_side == 'Call' and params.get('call_index_stop_active', False) and idx_ltp >= call_stop_val: idx_stop_hit = True
            elif self.active_side == 'Put' and params.get('put_index_stop_active', False) and idx_ltp <= put_stop_val: idx_stop_hit = True
                
            if idx_stop_hit:
                try: self.logic.close_position(self.active_side, "Auto Index SL")
                except: pass
                
                closed_side = self.active_side
                shared_state['active_trades'][self.active_side] = None

                if trade['pnl'] > 0:
                    self.state = 'DONE'
                    self.clear_leg_fields('Call')
                    self.clear_leg_fields('Put')
                    self.log("🛑 Index Stop Hit (GREEN). DONE.")
                else:
                    self.state = 'STOPPED_OUT'
                    self.log("🛑 Index Stop Hit (RED). Reversal Hunt.")
                    self.clear_leg_fields(closed_side)
                    
                    trigger_base = call_stop_val if closed_side == 'Call' else put_stop_val
                    self.recalc_reversal_strikes(trigger_base)
                    
                    if closed_side == 'Call': params['long_trigger_active'] = True
                    else: params['short_trigger_active'] = True
                return

            if now.minute == 15 and now.second == 5:
                self.process_hourly_trailing(trade, self.active_side)

        if self.state == 'STOPPED_OUT':
            if now.second == 0: self.check_second_leg_premium()
            if shared_state['active_trades']['Call'] or shared_state['active_trades']['Put']:
                self.state = 'SECOND_LEG'; self.log("Reversal Entry Confirmed.")
                shared_state['sound_queue'].append('open')

        if self.state == 'SECOND_LEG':
            lots = int(params['lots'])
            total_pnl = shared_state['pnl']['realized'] + shared_state['pnl']['unrealized']

            if total_pnl <= -(AutoConfig.GLB_STOP_PER_LOT * lots):
                self.clear_leg_fields('Call')
                self.clear_leg_fields('Put')
                self.state = 'DONE'
                self.log("🛑 Global Stop Hit. Triggers DISARMED.")
                if not shared_state.get('daily_pnl_written', False):
                    try:
                        self.logic.close_all_positions("Global Stop", save_pnl=True)
                        shared_state['daily_pnl_written'] = True
                        shared_state['sound_queue'].append('error')
                    except: pass
                shared_state['active_trades']['Call'] = None
                shared_state['active_trades']['Put'] = None

    def process_hourly_trailing(self, trade, side):
        self.log(f"🔍 Checking Hourly Trailing Logic for {side}...")

        token = INDICES[self.today_index]['token']
        now = datetime.now()
        
        # Explicitly fetch the candle that closed 1 minute ago (the previous hour)
        to_date = now
        from_date = to_date - timedelta(days=5) 
        candles = self.fetch_with_retry(token, from_date, to_date, "60minute")
        
        if not candles: return

        # Target: Candle starting 1 hour ago
        target_time = now.replace(minute=15, second=0, microsecond=0) - timedelta(hours=1)
        target_candle = None
        target_idx = -1
        
        for i, c in enumerate(candles):
            c_dt = c['date']
            if (c_dt.day == target_time.day and 
                c_dt.hour == target_time.hour and 
                c_dt.minute == target_time.minute):
                target_candle = c
                target_idx = i
                break
        
        if not target_candle: return

        if target_idx < AutoConfig.AVG_RANGE_PERIOD: return

        last_candle = target_candle
        recent_candles = candles[target_idx - AutoConfig.AVG_RANGE_PERIOD : target_idx]
        ranges = [(c['high'] - c['low']) for c in recent_candles]
        
        avg_range = statistics.mean(ranges)
        curr_range = last_candle['high'] - last_candle['low']
        min_required = avg_range * AutoConfig.TINY_CANDLE_FACTOR
        
        self.log(f"📊 Analyzing {target_time.strftime('%H:%M')} Candle | Range: {curr_range:.1f} vs Min: {min_required:.1f}")

        if curr_range < min_required: return 

        threshold = curr_range * AutoConfig.HOURLY_PROXIMITY_PCT
        c_close = last_candle['close']
        c_open = last_candle['open']
        idx_entry = trade.get('index_entry_price', 0)
        buffer = AutoConfig.BUF_NIFTY if self.today_index == 'NIFTY' else AutoConfig.BUF_SENSEX
        
        # 1. CALL SIDE (Shorting Call)
        # We want the market to go DOWN. 
        # Trail ONLY if the hourly candle was RED (Close < Open) AND Close is near Low.
        if side == 'Call':
            # NEW: Color Check (Red Candle)
            if c_close >= c_open:
                self.log(f"ℹ️ Call: Candle is GREEN (Close {c_close} >= Open {c_open}). No Trail.")
                return

            dist = c_close - last_candle['low']
            if dist <= threshold:
                new_stop = last_candle['high']
                params['call_index_stop_val'] = new_stop
                params['call_index_stop_active'] = True # <--- ADD THIS LINE
                self.log(f"📉 Trailing Call Stop to {new_stop} (Red Candle Close within {threshold:.1f} of Low)")
                shared_state['sound_queue'].append('alert') # Sound
                
                if idx_entry > 0 and new_stop < idx_entry:
                    if params['long_trigger_active']:
                        params['long_trigger_active'] = False
                        self.log("✅ Stop in Profit. Reversal Trigger CANCELLED.")
                else:
                    params['long_open_amount'] = new_stop + buffer
                    self.log(f"🔗 Moved Reversal Trigger (Put) to {params['long_open_amount']}")
            else:
                self.log(f"ℹ️ Call: Red Candle but Close too high (Dist {dist:.1f})")

        # 2. PUT SIDE (Shorting Put)
        # We want the market to go UP.
        # Trail ONLY if the hourly candle was GREEN (Close > Open) AND Close is near High.
        else: 
            # NEW: Color Check (Green Candle)
            if c_close <= c_open:
                self.log(f"ℹ️ Put: Candle is RED (Close {c_close} <= Open {c_open}). No Trail.")
                return

            dist = last_candle['high'] - c_close
            if dist <= threshold:
                new_stop = last_candle['low']
                params['put_index_stop_val'] = new_stop
                params['put_index_stop_active'] = True # <--- ADD THIS LINE
                self.log(f"📈 Trailing Put Stop to {new_stop} (Green Candle Close within {threshold:.1f} of High)")
                shared_state['sound_queue'].append('alert') # Sound
                
                if idx_entry > 0 and new_stop > idx_entry:
                    if params['short_trigger_active']:
                        params['short_trigger_active'] = False
                        self.log("✅ Stop in Profit. Reversal Trigger CANCELLED.")
                else:
                    params['short_open_amount'] = new_stop - buffer
                    self.log(f"🔗 Moved Reversal Trigger (Call) to {params['short_open_amount']}")
            else:
                self.log(f"ℹ️ Put: Green Candle but Close too low (Dist {dist:.1f})")

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
controller = AutoController(logic, inst_manager)
daily_logger = DailyLogger() 
#ticker.start()

# ==========================================
#      UI HELPER FUNCTIONS (GLOBAL)
# ==========================================

def update_ui():
    """Updates all UI elements."""
    
    # 1. PROCESS TOAST QUEUE
    while shared_state.get('toast_queue'):
        msg, type_ = shared_state['toast_queue'].pop(0)
        original_notify(msg, type=type_)

    # 2. PROCESS SOUND QUEUE (Fixes 'No Sound')
    while shared_state.get('sound_queue'):
        snd = shared_state['sound_queue'].pop(0)
        # We use a generic notification beep for all for now, or specific if desired
        # You can replace these URLs with any public mp3 link
        if snd == 'success':
            ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/cartoon_boing.ogg").play()')
        elif snd == 'error':
            ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/clank_car_crash.ogg").play()')
        elif snd == 'open':
            ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/pop.ogg").play()')
        else: # alert/general
            ui.run_javascript('new Audio("https://actions.google.com/sounds/v1/cartoon/wood_plank_flicks.ogg").play()')

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

    if ui_refs['last_action']: ui_refs['last_action'].set_text(shared_state['last_action'])
    
    if ui_refs['monitor_status']:
        if params['short_trigger_active'] or params['long_trigger_active']:
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

    tc = shared_state['active_trades']['Call']
    if tc:
        ui_refs['call_status'].set_text('OPEN'); ui_refs['call_status'].classes(replace='text-[10px] font-bold text-white bg-red-600 px-2 rounded animate-pulse')
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
        ui_refs['put_status'].set_text('OPEN'); ui_refs['put_status'].classes(replace='text-[10px] font-bold text-white bg-green-600 px-2 rounded animate-pulse')
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

def custom_render_master_banner(update_lots_callback):
    with ui.column().classes('w-full gap-0 mb-4'):
        with ui.card().classes('w-full p-3 bg-orange-200 text-orange-900 rounded-t-xl rounded-b-none border-b border-orange-300') as card:
            ui_refs['banner_card'] = card
            with ui.element('div').classes('w-full grid grid-cols-[1fr_auto] items-center gap-4'):
                with ui.row().classes('items-center gap-4 flex-nowrap'):
                    ui.label('Zerodha Trading Engine').classes('text-xl font-bold tracking-wide whitespace-nowrap')
                    ui_refs['monitor_status'] = ui.label('TRIGGERS OFF').classes('text-xs font-bold bg-gray-800 text-gray-400 px-2 py-1 rounded whitespace-nowrap')
                    ui.switch('Mute', value=params['mute_sound']).bind_value(params, 'mute_sound').props('color=red dense')
                with ui.row().classes('absolute right-2 top-2 gap-4 text-right bg-orange-200 pl-4'):
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Unrealized').classes('text-orange-800 text-[9px] uppercase tracking-wider')
                        ui_refs['pnl_unrealized'] = ui.label('₹ 0.00').classes('text-xl font-mono font-bold text-gray-800 leading-none')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Realized').classes('text-orange-800 text-[9px] uppercase tracking-wider')
                        ui_refs['pnl_realized'] = ui.label('₹ 0.00').classes('text-xl font-mono font-bold text-green-700 leading-none')

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
            with ui.row().classes('items-center gap-2 ml-4 border-l pl-4 border-orange-300'):
                with ui.column().classes('gap-0'):
                    ui.label('AUTO PILOT').classes('font-bold text-blue-900 text-[10px] leading-none')
                    status_lbl = ui.label().bind_text_from(controller, 'log_msg')
                    status_lbl.classes('text-[8px] font-mono text-blue-600 leading-none')
                ui.radio(['ON', 'OFF'], value=controller.mode, on_change=on_auto_mode_change).bind_value(controller, 'mode').props('inline dense color=blue')
            with ui.row().classes('items-center gap-2 ml-4 border-l pl-4 border-orange-300'):
                with ui.column().classes('gap-0'):
                    ui.label('HEDGELESS').classes('font-bold text-purple-900 text-[10px] leading-none')
                    ui.label('No hedge buy').classes('text-[8px] font-mono text-purple-600 leading-none')
                ui.toggle(['On', 'Off'], value='On' if params['hedgeless_mode'] else 'Off',
                    on_change=lambda e: params.update({'hedgeless_mode': e.value == 'On'})
                ).props('dense').classes('text-xs')

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

def update_lots(e): 
    if e.value in INDICES: params['lots'] = 10 if e.value == 'NIFTY' else 10; ui.notify(f"Switched to {e.value}")

def on_auto_mode_change(e):
    ts = datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
    now = datetime.now()
    is_weekend = now.weekday() in [5, 6]
    
    if e.value == 'OFF':
        controller.clear_leg_fields('Call')
        controller.clear_leg_fields('Put')
        print(f"[{ts}] 🛑 AUTO MODE SWITCHED OFF: Stops & Triggers Cleared.")
        ui.notify("Auto Mode OFF: Stops & Triggers Cleared", type='warning')
        
    elif e.value == 'ON':
        print(f"[{ts}] 🟢 AUTO MODE SWITCHED ON: Bot Armed for Next Signal/Day.")
        
        # FIX: Only pick up existing UI values if they are greater than 0
        try:
            if float(params.get('call_index_stop_val', 0)) > 0: params['call_index_stop_active'] = True
        except ValueError: pass
        try:
            if float(params.get('put_index_stop_val', 0)) > 0: params['put_index_stop_active'] = True
        except ValueError: pass
        try:
            if float(params.get('short_open_amount', 0)) > 0: params['short_trigger_active'] = True
        except ValueError: pass
        try:
            if float(params.get('long_open_amount', 0)) > 0: params['long_trigger_active'] = True
        except ValueError: pass
        
        # Better Weekend / Evening notifications
        if is_weekend:
             print(f"[{ts}] ℹ️ Weekend Detected. Bot Sleeping until Monday morning.")
             ui.notify("Auto Mode ON (Sleeping until Monday)")
        elif now.time() > dtime(15, 30):
             print(f"[{ts}] ℹ️ Market Closed. Auto Mode will remain ON for tomorrow morning.")
             ui.notify("Auto Mode ON (Saved for Tomorrow)")
        else:
             ui.notify("Auto Mode ON")

def handle_open(side):
    s, m = logic.open_position(side); ui.notify(m, type='positive' if s else 'negative')

def handle_close(side):
    s, m = logic.close_position(side)
    if s:  # If the manual close was successful
        controller.clear_leg_fields(side)  # Wipe the fields
    ui.notify(m, type='positive' if s else 'negative')

def handle_close_all():
    """Wrapper to ensure CLOSE ALL button wipes both sides of the UI."""
    try:
        logic.close_all_positions("Manual Close All", save_pnl=True)
        controller.clear_leg_fields('Call')
        controller.clear_leg_fields('Put')
        ui.notify("All Positions Closed manually", type='info')
    except Exception as e:
        ui.notify(f"Error closing all: {e}", type='negative')

def build_left_stack():
    comp.open_logic_card('Open Short', 'Call', 'short_open_mode', 'short_open_amount', 'short_open_strike', 'short_trigger_active')
    comp.entry_card('Call', 'Entry', 'call_entry_mode', 'call_manual_strike', on_open=lambda: handle_open('Call'), on_close=lambda: handle_close('Call'))
    comp.auto_close_card('Call', 'call_target_val', 'call_target_active', 'call_stop_val', 'call_stop_active')
    comp.premium_exit_card('Call')
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
    
    # FIX: Point to our new wrapper that includes the UI wipe
    ui.button('CLOSE ALL', on_click=handle_close_all, color='red').classes('w-full font-bold shadow-md rounded-xl mt-4')

def build_right_stack():
    comp.open_logic_card('Open Long', 'Put', 'long_open_mode', 'long_open_amount', 'long_open_strike', 'long_trigger_active')
    comp.entry_card('Put', 'Entry', 'put_entry_mode', 'put_manual_strike', on_open=lambda: handle_open('Put'), on_close=lambda: handle_close('Put'))
    comp.auto_close_card('Put', 'put_target_val', 'put_target_active', 'put_stop_val', 'put_stop_active')
    comp.premium_exit_card('Put')
    with ui.card().classes('w-full p-2 gap-2 bg-green-50 border border-green-200 shadow-sm rounded-xl'):
        ui.label('Put Exit based on Index').classes('font-bold text-xs uppercase text-green-800')
        with ui.row().classes('w-full gap-2'):
            comp.index_exit_component('Put', 'Stop', 'put_index_stop_time', 'put_index_stop_val', 'put_index_stop_active')
            comp.index_exit_component('Put', 'Tgt', 'put_index_target_time', 'put_index_target_val', 'put_index_tgt_active')


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
                with open(".automode_state", "w") as f: f.write(controller.mode)
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
                        if now.time() < AutoConfig.SQ_OFF_TIME:
                            logic.check_triggers()
                        else:
                            if params['short_trigger_active'] or params['long_trigger_active']:
                                params['short_trigger_active'] = False
                                params['long_trigger_active'] = False
                    
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

@ui.page('/', title='Zerodha Bot')
def index():
    # 1. Build UI Elements
    comp.render_master_banner(update_lots)
    
    with ui.row().classes('w-full px-0 gap-0'): 
        comp.render_chart_row()
        comp.render_log_row()

    with ui.row().classes('w-full no-wrap items-start gap-4 p-2'):
        with ui.column().classes('grow gap-4'): build_left_stack()
        with ui.column().classes('grow gap-4'): build_center_stack()
        with ui.column().classes('grow gap-4'): build_right_stack()

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

        # Restore State from File or Arguments
        if os.path.exists(".automode_state"):
            with open(".automode_state", "r") as f:
                saved_mode = f.read().strip()
                if saved_mode in ['ON', 'OFF']:
                    controller.mode = saved_mode
                    print(f"--- WORKER STARTED: Auto Mode Restored to {saved_mode} ---")
        elif "--auto-on" in sys.argv:
            controller.mode = 'ON'
            print("--- WORKER STARTED: Auto Mode Restored to ON (Arg) ---")
        else:
            print("--- WORKER STARTED: Auto Mode Default (OFF) ---")

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
                
                # Check for Saved State File
                auto_mode_next = False
                if os.path.exists(".automode_state"):
                    with open(".automode_state", "r") as f:
                        if f.read().strip() == 'ON': auto_mode_next = True
                
                # Update Command for Next Run
                if auto_mode_next: 
                    # We write to the file again to ensure persistence if next run crashes
                    with open(".automode_state", "w") as f: f.write("ON")
                
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


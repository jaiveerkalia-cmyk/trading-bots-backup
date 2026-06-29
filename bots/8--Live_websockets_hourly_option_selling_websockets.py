import sys
import os
os.environ["PYTHONIOENCODING"] = "utf-8"  # safe for IDLE and all terminals
import subprocess
import threading
import time
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
from kiteconnect import KiteConnect, KiteTicker

# ==============================================================================
# 1. GLOBAL PATHS & CONFIGURATION
# ==============================================================================

GD_PATH = '/app/data/'

AUTH_FILE_PATH        = '/app/config/' + 'auth.txt'
TRADEBOOK_CSV_PATH    = GD_PATH + 'hourly_option_selling_results/Intraday_options_tradebook.csv'
DAILY_PNL_CSV_PATH    = GD_PATH + 'hourly_option_selling_results/Final_daily_pnl.csv'

TOKEN_SWAP_TIME  = "09:00"   # Supervisor restart time
IST              = pytz.timezone('Asia/Kolkata')

# ==============================================================================
# STRATEGY CONSTANTS  (edit here to change behaviour)
# ==============================================================================

# How many seconds to wait after xx:15:00 before fetching the completed hourly candle.
# Increase if Zerodha candle data appears incomplete or delayed.
HOURLY_CANDLE_WAIT_SECONDS = 15

# Strike offsets for position 1 and 2, calculated from live spot ATM at open time.
# +N = N strikes ITM, 0 = ATM, -N = N strikes OTM.
POS1_STRIKE_OFFSET = 2    # e.g. +2 = 2 strikes ITM
POS2_STRIKE_OFFSET = 3    # e.g. +3 = 3 strikes ITM

# ------------------------------------------------------------------------------
# HOURLY CANDLE SOURCE MODE
# Exactly one of the three should be True at a time.
# Priority if multiple are True: WS > MINUTES > API
# ------------------------------------------------------------------------------

# Build from 1-minute candles fetched from Zerodha historical API at xx:15:01.
# Close is captured from live websocket at xx:15:01.
# Retries if last 1min candle is missing.
BUILD_CANDLE_FROM_MINUTES    = True

# Build entirely from live websocket ticks (most precise, no API call for candle).
# O = first tick of hour, H/L = running max/min, C = last tick at xx:15:01.
BUILD_CANDLE_FROM_WS         = False

# When True, fetches India VIX daily candles at startup, computes Supertrend(10,3)
# on them, and overrides option_mode based on last completed day's supertrend:
#   ST trending UP   -> market is calm/bullish -> BUY mode
#   ST trending DOWN -> market is volatile     -> SELL mode
# Overrides the static option_mode in DAY_CONFIGURATION.
VIX_MODE                     = True
VIX_ST_PERIOD                = 10    # Supertrend ATR period
VIX_ST_MULTIPLIER            = 3.0

ATR_PERIOD                   = 10    # ATR period for global stop calculation
EMA_PERIOD                   = 200   # EMA period applied to ATR   # Supertrend multiplier

# Fetch completed hourly candle directly from Zerodha historical API.
# Waits HOURLY_CANDLE_WAIT_SECONDS after xx:15:00 for Zerodha to finalize.
FETCH_HOURLY_FROM_API        = False

# When True, appends fetched/built 1min candle data to a CSV for auditing.
SAVE_MINUTE_CANDLES_CSV      = True
MINUTE_CANDLES_CSV_PATH      = GD_PATH + 'hourly_option_selling_results/minute_candles_log.csv'

# ------------------------------------------------------------------------------
# INDEX CONSTANTS
# ------------------------------------------------------------------------------
INDEX_DETAILS = {
    'NIFTY': {
        'index_name'    : 'NIFTY 50',
        'spot_exchange' : 'NSE',
        'opt_exchange'  : 'NFO',
        'strike_step'   : 50,
        'std_lot_size'  : 65,
        'index_filter'  : 'NIFTY',
        'hist_symbol'   : 'NIFTY 50',
        'spot_exchange_hist': 'NSE',     # exchange to use for index token lookup
    },
    'BANKNIFTY': {
        'index_name'    : 'NIFTY BANK',
        'spot_exchange' : 'NSE',
        'opt_exchange'  : 'NFO',
        'strike_step'   : 100,
        'std_lot_size'  : 30,
        'index_filter'  : 'BANKNIFTY',
        'hist_symbol'   : 'NIFTY BANK',
        'spot_exchange_hist': 'NSE',
    },
    'SENSEX': {
        'index_name'    : 'SENSEX',
        'spot_exchange' : 'BSE',
        'opt_exchange'  : 'BFO',
        'strike_step'   : 100,
        'std_lot_size'  : 20,
        'index_filter'  : 'SENSEX',
        'hist_symbol'   : 'SENSEX',
        'spot_exchange_hist': 'BSE',
    },
}

# ------------------------------------------------------------------------------
# DAY CONFIGURATION  (0=Mon … 6=Sun)
# ------------------------------------------------------------------------------
DAY_CONFIGURATION = {
    # global_stop_atr_multiplier  : global_stop  = -(multiplier * avg_atr * LOTS)
    # global_target_atr_multiplier: global_target =  (multiplier * avg_atr * LOTS)
    #
    # option_mode: 'sell' -> sell options, 'buy' -> buy options
    # Strike offsets set globally via POS1_STRIKE_OFFSET / POS2_STRIKE_OFFSET.
    0: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 3, 'live_mode': 1,
        'pos1_stop'                   : 20000,  # Mon per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Mon total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Mon
        'global_target_atr_multiplier': 20.0,   # Mon
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    1: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 2, 'live_mode': 1,
        'pos1_stop'                   : 20000,  # Tue per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Tue total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Tue
        'global_target_atr_multiplier': 20.0,   # Tue
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    2: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 2, 'live_mode': 1,
        'pos1_stop'                   : 20000,  # Wed per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Wed total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Wed
        'global_target_atr_multiplier': 20.0,   # Wed
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    3: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 3, 'live_mode': 1,
        'pos1_stop'                   : 20000,  # Thu per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Thu total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Thu
        'global_target_atr_multiplier': 20.0,   # Thu
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    4: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 4, 'live_mode': 0,
        'pos1_stop'                   : 20000,  # Fri per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Fri total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Fri
        'global_target_atr_multiplier': 20.0,   # Fri
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    5: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 3, 'live_mode': 1,
        'pos1_stop'                   : 20000,  # Sat per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Sat total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Sat
        'global_target_atr_multiplier': 20.0,   # Sat
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
    6: {'target_index': 'SENSEX', 'start': '11:15', 'exit_hour': 15, 'exit_minute': 19,
        'lots': 5, 'live_mode': 0,
        'pos1_stop'                   : 20000,  # Sun per-leg SL for 1st position
        'pos2_close_target'           : 50000,  # Sun total PnL target to close 2nd position
        'global_stop_atr_multiplier'  : 20.0,   # Sun
        'global_target_atr_multiplier': 20.0,   # Sun
        'candle_close_threshold'      : 0.2,
        'HOURLY_LOGIC_ENABLED'       : True,
        'INSIDE_BAR_LOGIC_ENABLED'   : True,
        'TRAILING_STOP_LOGIC_ENABLED': True,
        'option_mode'                : 'buy',
        },
}

# ==============================================================================
# 2. SHARED STATE
# ==============================================================================

live_market_data = {}   # token -> last_price (updated by websocket)
tick_lock        = threading.Lock()

# Websocket candle accumulator (used when BUILD_CANDLE_FROM_WS = True)
# Tracks OHLC of the current hour for the index token in real time.
# Reset at the start of each new hour (xx:15:00).
ws_candle = {
    'open'        : None,   # first tick price of the hour
    'high'        : None,   # running max
    'low'         : None,   # running min
    'close'       : None,   # last tick price (updated every tick)
    'hour_start'  : None,   # datetime of when this candle started
    'tick_count'  : 0,      # number of ticks received
}
ws_candle_lock   = threading.Lock()
ws_index_token   = None     # set after index token is fetched, used by on_ticks

# ==============================================================================
# 3. HELPER FUNCTIONS
# ==============================================================================

def get_now_str():
    return datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')

def get_now():
    return datetime.now(IST)

def normalize_candle_time(ts):
    """
    Converts API/build candle timestamps to a comparable minute-level key.
    Kite API rows and locally built rows can carry different timezone objects,
    but the strategy only needs the local candle start time for ordering.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_localize(None)
    return t.replace(second=0, microsecond=0)

def prepare_hourly_df_for_logic(hourly_df, context='hourly'):
    """
    Sorts hourly candles by start time and removes duplicate candle slots.
    In BUILD/WS modes the freshly built candle is appended after API context,
    so keep='last' keeps the locally built candle when API returns the same
    hourly slot.
    """
    if hourly_df.empty or 'date' not in hourly_df.columns:
        return hourly_df

    df = hourly_df.copy()
    df['_candle_time'] = df['date'].apply(normalize_candle_time)
    before = len(df)
    df = (
        df.sort_values('_candle_time')
          .drop_duplicates(subset=['_candle_time'], keep='last')
          .drop(columns=['_candle_time'])
          .reset_index(drop=True)
    )
    removed = before - len(df)
    if removed:
        print(f"[{get_now_str()}] [{context}] Removed {removed} duplicate hourly candle(s); using built candle for duplicate slot", flush=True)
    return df

def commission(quantity, buy_price, sell_price):
    """
    Calculates total charges for BSE Equity Options (Sensex/Bankex)
    based on Zerodha's latest tariff and April 2026 STT rates.
    """
    buy_turnover    = quantity * buy_price
    sell_turnover   = quantity * sell_price
    total_turnover  = buy_turnover + sell_turnover

    # 1. Brokerage: Flat Rs. 20 per executed order (Buy + Sell = 40)
    zerodha_brokerage   = 40

    # 2. STT: 0.15% on SELL side premium (Budget 2026 Update)
    stt                 = 0.0015 * sell_turnover

    # 3. BSE Transaction Charges: 0.0325% on premium turnover
    exchange_txn_charge = 0.000325 * total_turnover

    # 4. SEBI Charges: Rs. 10 per crore (0.000001)
    sebi_charges        = 0.000001 * total_turnover

    # 5. GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    gst                 = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)

    # 6. Stamp Duty: 0.003% on BUY side premium only
    stamp_duty          = 0.00003 * buy_turnover

    # 7. BSE IPFT: 0.00001% on total turnover
    ipft                = 0.0000001 * total_turnover

    total_charges = (zerodha_brokerage + stt + exchange_txn_charge +
                     sebi_charges + gst + stamp_duty + ipft)

    return round(total_charges, 2)

def get_ltp_safe(kite, symbol_list):
    for _ in range(5):
        try:
            return kite.ltp(symbol_list)
        except:
            time.sleep(1)
    return {}

def get_quote_price(kite, symbol, side, exchange='NFO'):
    quote_key = f"{exchange}:{symbol}"
    for attempt in range(50):
        try:
            quote = kite.quote(quote_key)
            if quote_key in quote:
                depth = quote[quote_key]['depth']
                return float(depth['sell'][0]['price'] if side == 'BUY' else depth['buy'][0]['price'])
        except Exception as e:
            print(f"Quote fetch failed ({attempt+1}/50): {e}", flush=True)
            time.sleep(1)
    return 0.0

def place_order(kite, symbol, qty, side, live_mode, exchange='BFO'):
    if live_mode == 0:
        print(f"[PAPER] {side} {qty}x {symbol} on {exchange}", flush=True)
        return True

    ltp_sym    = f"{exchange}:{symbol}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    # --- Helper 1: Fetch LTP via kite.ltp ---
    def get_ltp():
        ltp_sleep = 0.5
        for attempt in range(10):
            try:
                resp = kite.ltp(ltp_sym)
                ltp  = resp[ltp_sym]['last_price']
                if ltp > 0:
                    return ltp
            except Exception as e:
                print(f"    [order] LTP fetch error attempt {attempt+1}: {e}", flush=True)
            time.sleep(ltp_sleep)
            if attempt >= 2:
                ltp_sleep += 1.0
        return None

    # --- Helper 2: Aggressive limit price (10% buffer, tick-rounded to 0.05) ---
    def get_limit_price(ltp):
        if side == 'BUY':
            price = ltp * 1.10 if ltp > 50 else ltp + 5.0
        else:
            price = ltp * 0.90 if ltp > 50 else ltp - 5.0
        price = max(price, 0.05)
        return round(round(price / 0.05) * 0.05, 2)

    # 1. Fetch initial LTP
    current_ltp = get_ltp()
    if current_ltp is None:
        print(f"    [order] order_placement_failed: could not fetch LTP for {symbol}", flush=True)
        return False

    # 2. Place initial limit order
    limit_price   = get_limit_price(current_ltp)
    order_id      = None
    place_retries = 0
    place_sleep   = 1.0

    while place_retries < 10:
        try:
            order_id = kite.place_order(
                tradingsymbol    = symbol,
                exchange         = exchange,
                transaction_type = order_side,
                quantity         = qty,
                order_type       = kite.ORDER_TYPE_LIMIT,
                price            = limit_price,
                variety          = kite.VARIETY_REGULAR,
                product          = kite.PRODUCT_MIS,
            )
            print(f"    [order] {side} {qty}x {symbol} | LTP: {current_ltp:.2f} | limit: {limit_price:.2f} | id: {order_id}", flush=True)
            break
        except Exception as e:
            place_retries += 1
            print(f"    [order] place error attempt {place_retries}/10: {e}", flush=True)
            if place_retries == 10:
                print(f"    [order] order_placement_failed for {symbol}", flush=True)
                return False
            time.sleep(place_sleep)
            if place_retries >= 3:
                place_sleep += 1.0

    if not order_id:
        return False

    # 3. Monitor fill and chase price if partial/unfilled
    max_modifications = 10
    mod_count         = 0
    mod_sleep         = 1.0

    while mod_count < max_modifications:
        time.sleep(mod_sleep)
        try:
            latest_state = kite.order_history(order_id)[-1]
            status       = latest_state['status']
            pending_qty  = latest_state.get('pending_quantity', 0)

            if status == 'COMPLETE':
                print(f"    [order] {side} {qty}x {symbol} FILLED | limit: {limit_price:.2f} | id: {order_id} [OK]", flush=True)
                return True

            elif status in ['REJECTED', 'CANCELLED']:
                print(f"    [order] Order {status} for {symbol} | reason: {latest_state.get('status_message', 'Unknown')}", flush=True)
                return False

            elif pending_qty > 0:
                print(f"    [order] Partial/no fill | pending: {pending_qty} | chasing price...", flush=True)
                new_ltp = get_ltp()
                if new_ltp:
                    new_limit = get_limit_price(new_ltp)
                    kite.modify_order(
                        variety    = kite.VARIETY_REGULAR,
                        order_id   = order_id,
                        order_type = kite.ORDER_TYPE_LIMIT,
                        price      = new_limit,
                    )
                    limit_price = new_limit
                    print(f"    [order] Modified to {new_limit:.2f}", flush=True)
                else:
                    print(f"    [order] LTP unavailable for modification — retrying status...", flush=True)

        except Exception as e:
            print(f"    [order] modification error: {e}", flush=True)

        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0

    print(f"    [order] WARNING: max modifications ({max_modifications}) reached for {symbol} — order may still be pending.", flush=True)
    return False


def get_token_and_symbol(df, strike, instrument_type):
    try:
        row = df[(df['strike'] == strike) & (df['instrument_type'] == instrument_type)].iloc[0]
        return int(row['instrument_token']), row['tradingsymbol']
    except:
        return None, None

# ------------------------------------------------------------------------------
# WebSocket callbacks
# ------------------------------------------------------------------------------
def on_ticks(ws, ticks):
    with tick_lock:
        for tick in ticks:
            # Only store if last_price is valid (>0) — BSE ticks can have last_price=0
            # on OI-only updates, which would overwrite a valid cached price with 0
            if tick.get('last_price', 0) > 0:
                live_market_data[tick['instrument_token']] = tick['last_price']

    # Accumulate ws_candle for the index token when WS candle mode is active
    if BUILD_CANDLE_FROM_WS and ws_index_token is not None:
        now = datetime.now(IST)
        with ws_candle_lock:
            for tick in ticks:
                if tick['instrument_token'] != ws_index_token:
                    continue
                price = tick['last_price']

                # Reset candle at the start of a new hour (xx:15:00)
                if ws_candle['hour_start'] is None or \
                   (now.minute == 15 and now.second == 0 and
                    (ws_candle['hour_start'] is None or now.hour != ws_candle['hour_start'].hour)):
                    ws_candle['open']       = price
                    ws_candle['high']       = price
                    ws_candle['low']        = price
                    ws_candle['close']      = price
                    ws_candle['hour_start'] = now.replace(second=0, microsecond=0)
                    ws_candle['tick_count'] = 1
                else:
                    if ws_candle['open'] is None:
                        ws_candle['open'] = price
                    ws_candle['high']       = max(ws_candle['high'], price) if ws_candle['high'] is not None else price
                    ws_candle['low']        = min(ws_candle['low'],  price) if ws_candle['low']  is not None else price
                    ws_candle['close']      = price
                    ws_candle['tick_count'] += 1

ws_connected   = threading.Event()   # set when websocket handshake completes
ws_last_error  = {'code': None, 'reason': None}   # populated by on_error / on_close

def on_connect(ws, response):
    print("WebSocket connected.", flush=True)
    ws_last_error['code']   = None
    ws_last_error['reason'] = None
    ws_connected.set()

def on_error(ws, code, reason):
    ws_last_error['code']   = code
    ws_last_error['reason'] = reason
    print(f"WebSocket error | code: {code} | reason: {reason}", flush=True)

def on_close(ws, code, reason):
    ws_last_error['code']   = code
    ws_last_error['reason'] = reason
    print(f"WebSocket closed | code: {code} | reason: {reason}", flush=True)

# ==============================================================================
# 4.  VIX SUPERTREND  (option mode override)
# ==============================================================================

def calculate_vix_supertrend(kite):
    """
    Fetches India VIX daily candles and computes Supertrend(VIX_ST_PERIOD, VIX_ST_MULTIPLIER).
    Returns 'buy' if last day's supertrend is UP (VIX rising/volatile -> buy options, premium expands),
            'sell' if last day's supertrend is DOWN (VIX falling/calm -> sell options, premium decays).

    VIX logic:
      ST UP   means VIX is in an uptrend (volatility rising) -> options are expensive and expanding -> BUY
      ST DOWN means VIX is in a downtrend (volatility falling) -> options are cheap and decaying  -> SELL

    India VIX instrument token on NSE: 264969 (fixed, does not change)
    """
    VIX_TOKEN = 264969
    try:
        now     = datetime.now(IST)
        from_dt = now - timedelta(days = 200)   # enough history for ST warmup

        print(f"[{get_now_str()}] [VIX] Fetching daily VIX candles...", flush=True)
        candles = kite.historical_data(
            instrument_token = VIX_TOKEN,
            from_date        = from_dt.strftime('%Y-%m-%d'),
            to_date          = now.strftime('%Y-%m-%d'),
            interval         = 'day',
            continuous       = False,
            oi               = False,
        )
        if not candles or len(candles) < VIX_ST_PERIOD + 2:
            print(f"[{get_now_str()}] [VIX] Not enough candles ({len(candles) if candles else 0}). Cannot compute supertrend.", flush=True)
            return None

        df = pd.DataFrame(candles)
        df['date'] = pd.to_datetime(df['date'])
        print(f"[{get_now_str()}] [VIX] {len(df)} daily candles fetched | last date: {df['date'].iloc[-1].date()}", flush=True)

        # -- Supertrend calculation --------------------------------------------
        # True Range
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'low', 'prev_close']].apply(
            lambda r: max(r['high'] - r['low'],
                          abs(r['high'] - r['prev_close']),
                          abs(r['low']  - r['prev_close'])), axis=1
        )
        # Wilder's ATR
        df['atr'] = df['tr'].ewm(alpha=1/VIX_ST_PERIOD, min_periods=VIX_ST_PERIOD, adjust=False).mean()

        # Drop rows where ATR is NaN (warmup period) before computing supertrend
        df = df.dropna(subset=['atr']).reset_index(drop=True)

        if len(df) < 3:
            print(f"[{get_now_str()}] [VIX] Not enough non-NaN rows after ATR warmup.", flush=True)
            return None

        hl_mid = (df['high'] + df['low']) / 2
        df['upper_band'] = hl_mid + VIX_ST_MULTIPLIER * df['atr']
        df['lower_band'] = hl_mid - VIX_ST_MULTIPLIER * df['atr']

        # Supertrend — iterative using numpy arrays to avoid pandas iloc setter issues
        n           = len(df)
        upper_arr   = df['upper_band'].to_numpy(dtype=float)
        lower_arr   = df['lower_band'].to_numpy(dtype=float)
        close_arr   = df['close'].to_numpy(dtype=float)
        final_upper = upper_arr.copy()
        final_lower = lower_arr.copy()
        supertrend  = np.ones(n, dtype=bool)   # True = uptrend

        for i in range(1, n):
            # Lower band: only move up, never down
            final_lower[i] = lower_arr[i] if (lower_arr[i] > final_lower[i-1] or close_arr[i-1] < final_lower[i-1]) else final_lower[i-1]
            # Upper band: only move down, never up
            final_upper[i] = upper_arr[i] if (upper_arr[i] < final_upper[i-1] or close_arr[i-1] > final_upper[i-1]) else final_upper[i-1]
            # Direction
            if supertrend[i-1]:
                supertrend[i] = close_arr[i] >= final_lower[i]
            else:
                supertrend[i] = close_arr[i] > final_upper[i]

        df['st_up']    = supertrend
        df['st_value'] = np.where(supertrend, final_lower, final_upper)

        # Last COMPLETED day (iloc[-1] is today's partial or last closed day)
        # We use iloc[-1] since candles are daily and we fetch up to today
        last        = df.iloc[-1]
        prev        = df.iloc[-2]
        st_up_today = last['st_up']
        st_up_prev  = prev['st_up']

        direction = 'up' if st_up_today else 'down'
        flipped   = st_up_today != st_up_prev

        print(f"[{get_now_str()}] [VIX] Last candle: {last['date'].date()} | "
              f"VIX close: {last['close']:.2f} | ST: {'UP' if st_up_today else 'DOWN'} | "
              f"ST value: {last['st_value']:.2f} | "
              f"{'*** FLIPPED FROM PREV ***' if flipped else 'same as prev'}", flush=True)
        print(f"[{get_now_str()}] [VIX] Prev candle: {prev['date'].date()} | "
              f"VIX close: {prev['close']:.2f} | ST: {'UP' if st_up_prev else 'DOWN'}", flush=True)

        # ST UP = VIX rising = volatile = BUY options (premium expands in your favour)
        # ST DOWN = VIX falling = calm  = SELL options (premium decays fast)
        option_mode = 'buy' if st_up_today else 'sell'
        print(f"[{get_now_str()}] [VIX] ST direction: {direction.upper()} -> option_mode: {option_mode.upper()}", flush=True)
        return option_mode

    except Exception as e:
        print(f"[{get_now_str()}] [VIX] Supertrend calculation failed: {e}", flush=True)
        import traceback; traceback.print_exc()
        return None


# ==============================================================================
# 5.  ATR -> 200 EMA  (global_stop calculation)
# ==============================================================================

def calculate_atr_ema(kite, index_token, interval='60minute'):
    """
    Fetches enough hourly candles to compute an EMA_PERIOD-EMA of ATR(ATR_PERIOD).
    Returns the most recent EMA value (avg_atr for the day).
    ATR_PERIOD and EMA_PERIOD are set as global constants at the top of the file.

    Fetches data up to now, computes ATR/EMA on full history, then explicitly
    slices to today's 09:15 candle as the latest row to use for global_stop.
    """
    try:
        now     = datetime.now(IST)
        today   = now.date()
        from_dt = now - timedelta(days=120)
        # Fetch up to now — gives us today's 09:15 candle in the results
        to_dt   = now

        candles = kite.historical_data(
            instrument_token = index_token,
            from_date        = from_dt.strftime('%Y-%m-%d %H:%M:%S'),
            to_date          = to_dt.strftime('%Y-%m-%d %H:%M:%S'),
            interval         = interval,
            continuous       = False,
            oi               = False,
        )
        df = pd.DataFrame(candles)
        if df.empty or len(df) < ATR_PERIOD + EMA_PERIOD:
            print(f"Not enough candles for ATR EMA: {len(df)}", flush=True)
            return None

        df['date'] = pd.to_datetime(df['date'])

        # Compute ATR and EMA on the full dataset first (EWM needs full history for accuracy)
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'low', 'prev_close']].apply(
            lambda r: max(r['high'] - r['low'],
                          abs(r['high'] - r['prev_close']),
                          abs(r['low']  - r['prev_close'])), axis=1)

        # Wilder's ATR
        df['atr'] = df['tr'].ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean()

        # EMA of ATR
        df['atr_ema'] = df['atr'].ewm(span=EMA_PERIOD, adjust=False).mean()

        # Explicitly slice to rows up to and including today's 09:15 candle
        today_0915 = pd.Timestamp(
            datetime.combine(today, datetime.strptime('09:15', '%H:%M').time())
        ).tz_localize(IST)

        df_sliced = df[df['date'] <= today_0915]

        if df_sliced.empty:
            print(f"[ATR EMA] No candle found on or before today's 09:15 ({today_0915}). Using full df.", flush=True)
            df_sliced = df

        # Confirm the last row used
        last_row = df_sliced.iloc[-1]
        print(f"[ATR EMA] Using candle: {last_row['date']} | close: {last_row['close']:.2f}", flush=True)

        # Print last 5 rows of sliced df for verification
        print(f"[ATR EMA] ATR_PERIOD:{ATR_PERIOD} | EMA_PERIOD:{EMA_PERIOD} | Last 5 rows (sliced to 09:15):", flush=True)
        display_cols = ['date', 'close', 'tr', 'atr', 'atr_ema']
        available    = [c for c in display_cols if c in df_sliced.columns]
        print(df_sliced[available].tail(5).to_string(index=False), flush=True)

        latest = df_sliced['atr_ema'].dropna().iloc[-1]
        print(f"[ATR EMA] Latest value: {latest:.2f}", flush=True)
        return latest

    except Exception as e:
        print(f"ATR EMA calculation failed: {e}", flush=True)
        return None

# ==============================================================================
# 6.  FETCH HOURLY CANDLES (for hourly logic  -  called at xx:15:05)
# ==============================================================================

def fetch_today_hourly(kite, index_token, silent=False):
    """
    Returns today's COMPLETED hourly candles as a DataFrame.
    Caps to_dt to the start of the current hour so the currently
    forming candle is never included in the results.
    e.g. called at 11:15:05 -> to_dt = 11:00:00 -> returns 09:15 and 10:15 candles only.
    silent=True suppresses the print (used when fetching prev context in build modes).
    """
    try:
        now       = datetime.now(IST)
        today     = now.date()
        from_dt   = datetime.combine(today, datetime.strptime('09:15', '%H:%M').time()).replace(tzinfo=IST)
        to_dt     = now.replace(minute=0, second=0, microsecond=0)
        candles   = kite.historical_data(
            instrument_token = index_token,
            from_date        = from_dt.strftime('%Y-%m-%d %H:%M:%S'),
            to_date          = to_dt.strftime('%Y-%m-%d %H:%M:%S'),
            interval         = '60minute',
            continuous       = False,
            oi               = False,
        )
        df = pd.DataFrame(candles)
        if not silent:
            print(f"[{get_now_str()}] [API] Fetched {len(df)} completed hourly candles (up to {to_dt.strftime('%H:%M')})", flush=True)
        return df
    except Exception as e:
        print(f"Hourly candle fetch failed: {e}", flush=True)
        return pd.DataFrame()


def fetch_hourly_from_minutes(kite, index_token, max_retries=5, retry_wait=3):
    """
    Used when BUILD_CANDLE_FROM_MINUTES = True.
    Waits HOURLY_CANDLE_WAIT_SECONDS after xx:15:00, then fetches 1min candles
    and slices them to the exact expected window (hour_start to hour_end = xx:14).
    O/H/L/C all come from the 1min candles — no websocket close needed.

    Strategy:
    - Fetch from hour_start to now (Zerodha returns only closed candles)
    - Slice to rows with date between hour_start and expected_end (xx:14)
    - If xx:14 candle is missing after slicing, retry
    - C = close of the xx:14 candle (last candle in sliced window)
    """
    try:
        now        = datetime.now(IST)
        hour_start = now.replace(minute=15, second=0, microsecond=0) - timedelta(hours=1)
        # Expected last candle: xx:14:00
        expected_last = now.replace(minute=14, second=0, microsecond=0)
        # Normalise to naive for comparison
        def norm(ts):
            return pd.Timestamp(ts).replace(tzinfo=None, second=0, microsecond=0)

        expected_last_norm  = norm(expected_last)
        expected_start_norm = norm(hour_start)

        print(f"[{get_now_str()}] [BUILD] Fetching 1min candles: {hour_start.strftime('%H:%M')} -> {expected_last.strftime('%H:%M')} | using now as to_date", flush=True)

        df_min = pd.DataFrame()

        for attempt in range(1, max_retries + 1):
            candles = kite.historical_data(
                instrument_token = index_token,
                from_date        = hour_start.strftime('%Y-%m-%d %H:%M:%S'),
                to_date          = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S'),
                interval         = 'minute',
                continuous       = False,
                oi               = False,
            )

            if not candles:
                print(f"[{get_now_str()}] [BUILD] Attempt {attempt}/{max_retries}: no candles returned, retrying in {retry_wait}s...", flush=True)
                time.sleep(retry_wait)
                continue

            df_raw = pd.DataFrame(candles)
            df_raw['date'] = pd.to_datetime(df_raw['date'])

            # Slice to expected window: keep only candles in [hour_start, expected_last]
            df_sliced = df_raw[
                df_raw['date'].apply(norm) >= expected_start_norm
            ][
                df_raw['date'].apply(norm) <= expected_last_norm
            ].reset_index(drop=True)

            n_raw    = len(df_raw)
            n_sliced = len(df_sliced)
            last_norm = norm(df_sliced['date'].iloc[-1]) if not df_sliced.empty else None

            print(f"[{get_now_str()}] [BUILD] Attempt {attempt}/{max_retries} | raw:{n_raw} | sliced:{n_sliced} | "
                  f"first:{df_sliced['date'].iloc[0] if not df_sliced.empty else 'N/A'} | "
                  f"last:{df_sliced['date'].iloc[-1] if not df_sliced.empty else 'N/A'}", flush=True)

            if not df_sliced.empty and last_norm == expected_last_norm:
                print(f"[{get_now_str()}] [BUILD] Last candle ({expected_last.strftime('%H:%M')}) verified", flush=True)
                df_min = df_sliced
                break
            else:
                missing = expected_last.strftime('%H:%M')
                print(f"[{get_now_str()}] [BUILD] WARNING: {missing} candle missing after slicing | retrying in {retry_wait}s...", flush=True)
                if attempt < max_retries:
                    time.sleep(retry_wait)

        if df_min.empty:
            print(f"[{get_now_str()}] [BUILD] Failed to get complete candles after {max_retries} attempts — using best available", flush=True)
            # Use whatever we got rather than failing entirely
            if not df_sliced.empty:
                df_min = df_sliced
                print(f"[{get_now_str()}] [BUILD] Using {len(df_min)} candles (last: {df_min['date'].iloc[-1]})", flush=True)
            else:
                return pd.DataFrame()

        # -- First candle check --------------------------------------------------
        if norm(df_min['date'].iloc[0]) != expected_start_norm:
            print(f"[{get_now_str()}] [BUILD] WARNING: first candle {norm(df_min['date'].iloc[0]).strftime('%H:%M')} != expected {expected_start_norm.strftime('%H:%M')}", flush=True)

        n_candles = len(df_min)
        if n_candles < 55:
            print(f"[{get_now_str()}] [BUILD] WARNING: only {n_candles} candles (expected ~59) — possible data gap", flush=True)

        # -- Export to CSV if enabled --------------------------------------------
        if SAVE_MINUTE_CANDLES_CSV:
            try:
                df_export = df_min.copy()
                df_export['fetched_at'] = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                write_header = not os.path.exists(MINUTE_CANDLES_CSV_PATH)
                df_export.to_csv(MINUTE_CANDLES_CSV_PATH, mode='a', header=write_header, index=False)
                print(f"[{get_now_str()}] [BUILD] {n_candles} candles appended to CSV", flush=True)
            except Exception as csv_err:
                print(f"[{get_now_str()}] [BUILD] WARNING: could not save CSV: {csv_err}", flush=True)

        # -- Build hourly candle from sliced 1min data ---------------------------
        o = df_min['open'].iloc[0]
        h = df_min['high'].max()
        l = df_min['low'].min()
        c = df_min['close'].iloc[-1]   # close of the xx:14 candle

        print(f"[{get_now_str()}] [BUILD] Hourly candle | O:{o} H:{h} L:{l} C:{c} (C from {norm(df_min['date'].iloc[-1]).strftime('%H:%M')} 1min close)", flush=True)

        return pd.DataFrame([{
            'date'  : hour_start,
            'open'  : o,
            'high'  : h,
            'low'   : l,
            'close' : c,
            'volume': df_min['volume'].sum() if 'volume' in df_min.columns else 0,
        }])

    except Exception as e:
        print(f"[{get_now_str()}] [BUILD] fetch_hourly_from_minutes failed: {e}", flush=True)
        import traceback; traceback.print_exc()
        return pd.DataFrame()

def fetch_hourly_from_ws():
    """
    Used when BUILD_CANDLE_FROM_WS = True.
    Snapshots the ws_candle accumulator at xx:15:01 and returns it as a
    single-row DataFrame compatible with fetch_today_hourly / fetch_hourly_from_minutes.

    O = first tick of the hour (from xx:15:00 onwards)
    H = highest tick seen during the hour
    L = lowest tick seen during the hour
    C = last tick received (captured at xx:15:01 by the caller)

    Also appends a row to MINUTE_CANDLES_CSV_PATH if SAVE_MINUTE_CANDLES_CSV is True,
    logging the ws_candle snapshot for auditing.
    """
    with ws_candle_lock:
        snap = dict(ws_candle)   # snapshot under lock

    o = snap['open']
    h = snap['high']
    l = snap['low']
    c = snap['close']
    hour_start  = snap['hour_start']
    tick_count  = snap['tick_count']

    print(f"[WS CANDLE] Snapshot | O:{o} H:{h} L:{l} C:{c} | ticks:{tick_count} | hour_start:{hour_start}", flush=True)

    if o is None or h is None or l is None or c is None:
        print(f"[WS CANDLE] WARNING: incomplete candle (open={o}, high={h}, low={l}, close={c})", flush=True)
        return pd.DataFrame()

    if tick_count < 10:
        print(f"[WS CANDLE] WARNING: only {tick_count} ticks received this hour — candle may be unreliable", flush=True)

    result = pd.DataFrame([{
        'date'  : hour_start if hour_start else datetime.now(IST).replace(minute=15, second=0, microsecond=0) - timedelta(hours=1),
        'open'  : o,
        'high'  : h,
        'low'   : l,
        'close' : c,
        'volume': 0,
    }])

    # -- Export ws candle snapshot to CSV if enabled ---------------------------
    if SAVE_MINUTE_CANDLES_CSV:
        try:
            df_export = result.copy()
            df_export['source']     = 'websocket'
            df_export['tick_count'] = tick_count
            df_export['fetched_at'] = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
            write_header = not os.path.exists(MINUTE_CANDLES_CSV_PATH)
            df_export.to_csv(MINUTE_CANDLES_CSV_PATH, mode='a', header=write_header, index=False)
            print(f"[WS CANDLE] Snapshot appended to {MINUTE_CANDLES_CSV_PATH}", flush=True)
        except Exception as csv_err:
            print(f"[WS CANDLE] WARNING: could not save candle CSV: {csv_err}", flush=True)

    return result


# ==============================================================================
# 7.  MAIN TRADING PROCESS
# ==============================================================================

def run_trading_process():
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"[{get_now_str()}] BREAKOUT OPTION SELLING  -  LIVE PROCESS", flush=True)
        print(f"{'='*60}", flush=True)

        # -- Config ------------------------------------------------------------
        day_idx    = datetime.now(IST).weekday()
        day_config = DAY_CONFIGURATION.get(day_idx, DAY_CONFIGURATION[0])
        idx_key    = day_config['target_index']
        idx_config = INDEX_DETAILS[idx_key]
        config     = {**day_config, **idx_config}

        LIVE_MODE         = config['live_mode']
        LOTS              = config['lots']
        LOT_SIZE          = config['std_lot_size']
        QUANTITY          = LOTS * LOT_SIZE
        STRIKE_STEP       = config['strike_step']
        OPT_EXCHANGE      = config['opt_exchange']
        SPOT_EXCHANGE     = config['spot_exchange']
        SYMBOL_NAME       = config['index_name']
        INDEX_FILTER      = config['index_filter']
        EXIT_HOUR         = config['exit_hour']
        EXIT_MINUTE       = config['exit_minute']

        HOURLY_LOGIC_ENABLED        = config['HOURLY_LOGIC_ENABLED']
        INSIDE_BAR_LOGIC_ENABLED    = config['INSIDE_BAR_LOGIC_ENABLED']
        TRAILING_STOP_LOGIC_ENABLED = config['TRAILING_STOP_LOGIC_ENABLED']
        CANDLE_CLOSE_THRESHOLD      = config.get('candle_close_threshold', 0.33)
        POS1_STOP                   = config.get('pos1_stop', 20000)
        POS2_CLOSE_TARGET           = config.get('pos2_close_target', 50000)
        OPTION_MODE                 = config.get('option_mode', 'sell')     # 'sell' or 'buy'

        print(f"Mode:      {'LIVE' if LIVE_MODE else 'PAPER'} | Option mode: {OPTION_MODE} | POS1 offset: +{POS1_STRIKE_OFFSET} | POS2 offset: +{POS2_STRIKE_OFFSET}", flush=True)
        print(f"Index:     {SYMBOL_NAME} | Lots: {LOTS} | Qty: {QUANTITY}", flush=True)
        print(f"Exit:      {EXIT_HOUR}:{EXIT_MINUTE:02d}", flush=True)

        # -- Authenticate ------------------------------------------------------
        try:
            with open(AUTH_FILE_PATH, 'r') as f:
                data = f.read().split(',')
                api_key, access_token = data[0].strip(), data[1].strip()
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)
            print(f"[{get_now_str()}] Authenticated.", flush=True)
        except Exception as e:
            print(f"Auth failed: {e}", flush=True)
            return

        # -- Instruments -------------------------------------------------------
        print(f"[{get_now_str()}] Fetching instruments...", flush=True)
        try:
            instruments_df = pd.DataFrame(kite.instruments(OPT_EXCHANGE))
            options_df     = instruments_df[
                (instruments_df['name']    == INDEX_FILTER) &
                (instruments_df['segment'] == f"{OPT_EXCHANGE}-OPT")
            ].copy()
            if options_df.empty:
                print("No options found. Exiting.", flush=True)
                return
            current_expiry = options_df['expiry'].min()
            options_df     = options_df[options_df['expiry'] == current_expiry]
            print(f"Expiry: {current_expiry} | Options rows: {len(options_df)}", flush=True)
        except Exception as e:
            print(f"Instrument fetch failed: {e}", flush=True)
            return

        # -- Get index instrument token (for historical API) -------------------
        SPOT_EXCHANGE_HIST = config.get('spot_exchange_hist', 'NSE')
        try:
            spot_instruments = pd.DataFrame(kite.instruments(SPOT_EXCHANGE_HIST))
            idx_row          = spot_instruments[spot_instruments['tradingsymbol'] == INDEX_FILTER.replace(' ', '')]
            if idx_row.empty:
                idx_row = spot_instruments[spot_instruments['name'] == SYMBOL_NAME]
            index_token = int(idx_row.iloc[0]['instrument_token'])
            print(f"Index token ({SPOT_EXCHANGE_HIST}): {index_token}", flush=True)
            if BUILD_CANDLE_FROM_WS:
                global ws_index_token
                ws_index_token = index_token
                print(f"[WS CANDLE] Accumulator armed for token {index_token}", flush=True)
        except Exception as e:
            print(f"Index token fetch failed: {e}", flush=True)
            return

        # -- WebSocket ---------------------------------------------------------
        # Retries indefinitely with increasing delay (5s, 10s, 15s ... capped at 60s)
        ws_attempt    = 0
        ws_retry_base = 5    # seconds for first retry
        ws_retry_cap  = 60   # maximum delay between retries

        while True:
            ws_attempt += 1
            ws_connected.clear()

            kws            = KiteTicker(api_key, access_token)
            kws.on_ticks   = on_ticks
            kws.on_connect = on_connect
            kws.on_error   = on_error
            kws.on_close   = on_close
            ws_thread      = threading.Thread(target=kws.connect, kwargs={'threaded': True})
            ws_thread.daemon = True
            ws_thread.start()

            print(f"[{get_now_str()}] WebSocket connect attempt {ws_attempt}...", flush=True)
            connected = ws_connected.wait(timeout=30)

            if connected:
                print(f"[{get_now_str()}] WebSocket ready (attempt {ws_attempt}).", flush=True)
                break

            # Connection failed — report reason and retry
            code   = ws_last_error.get('code')
            reason = ws_last_error.get('reason')
            delay  = min(ws_retry_base * ws_attempt, ws_retry_cap)
            print(f"[{get_now_str()}] WebSocket did not connect within 30s | "
                  f"code: {code} | reason: {reason} | "
                  f"retrying in {delay}s (attempt {ws_attempt})...", flush=True)
            try:
                kws.close()
            except Exception:
                pass
            time.sleep(delay)

        # Subscribe index token for spot LTP
        kws.subscribe([index_token])
        kws.set_mode(kws.MODE_LTP, [index_token])

        # -- Wait for start time -----------------------------------------------
        print(f"[{get_now_str()}] Waiting for start: {config['start']}...", flush=True)

        # -- VIX Supertrend mode override (runs before start time wait) --------
        if VIX_MODE:
            print(f"[{get_now_str()}] [VIX] VIX_MODE enabled — computing supertrend to determine option_mode...", flush=True)
            vix_mode = calculate_vix_supertrend(kite)
            if vix_mode is not None:
                OPTION_MODE = vix_mode
                print(f"[{get_now_str()}] [VIX] option_mode overridden to: {OPTION_MODE.upper()}", flush=True)
            else:
                print(f"[{get_now_str()}] [VIX] Supertrend failed — keeping static option_mode: {OPTION_MODE.upper()}", flush=True)
        else:
            print(f"[{get_now_str()}] VIX_MODE disabled — using static option_mode: {OPTION_MODE.upper()}", flush=True)

        while True:
            if get_now().strftime('%H:%M') >= config['start']:
                break
            time.sleep(5)

        # -- Calculate global_stop via ATR EMA200 ------------------------------
        print(f"[{get_now_str()}] Calculating ATR EMA200 for global stop...", flush=True)
        avg_atr = calculate_atr_ema(kite, index_token)
        if avg_atr is None:
            print("ATR calculation failed. Using fallback of 100 points.", flush=True)
            avg_atr = 100

        GLOBAL_STOP_MULT   = config.get('global_stop_atr_multiplier',   2.0)
        GLOBAL_TARGET_MULT = config.get('global_target_atr_multiplier',  2.0)

        global_stop   = -(GLOBAL_STOP_MULT   * avg_atr * LOTS)
        global_target =  (GLOBAL_TARGET_MULT * avg_atr * LOTS)

        print(f"avg_atr: {avg_atr:.2f} | stop_mult: {GLOBAL_STOP_MULT} | target_mult: {GLOBAL_TARGET_MULT}", flush=True)
        print(f"global_stop: {global_stop:.2f} | global_target: {global_target:.2f}", flush=True)

        # -- Wait for and fetch the 10:15 hourly candle for initial entry levels --
        if BUILD_CANDLE_FROM_WS:
            # WS mode: close captured at :01 from accumulated ws_candle, no API call needed
            print(f"[{get_now_str()}] [WS MODE] Waiting for 11:15:00 to snapshot ws_candle...", flush=True)
            while True:
                now = get_now()
                if (now.hour == 11 and now.minute == 15) or \
                   (now.hour == 11 and now.minute > 15) or now.hour > 11:
                    break
                time.sleep(0.05)
            print(f"[{get_now_str()}] [WS MODE] Snapshotting ws_candle...", flush=True)
            built_candle = fetch_hourly_from_ws()
            if built_candle.empty:
                print("WS candle snapshot empty. Exiting.", flush=True)
                return
            # Reset accumulator so it starts fresh for the next hour
            with ws_candle_lock:
                ws_candle['open']       = None
                ws_candle['high']       = None
                ws_candle['low']        = None
                ws_candle['close']      = None
                ws_candle['hour_start'] = None
                ws_candle['tick_count'] = 0
            print(f"[{get_now_str()}] [WS MODE] Accumulator reset for next hour", flush=True)
            hourly_df_api = fetch_today_hourly(kite, index_token)
            hourly_df = pd.concat([hourly_df_api, built_candle], ignore_index=True)

        elif BUILD_CANDLE_FROM_MINUTES:
            # Wait HOURLY_CANDLE_WAIT_SECONDS after 11:15:00 then fetch 1min candles
            print(f"[{get_now_str()}] [BUILD MODE] Waiting for 11:15:{HOURLY_CANDLE_WAIT_SECONDS:02d}...", flush=True)
            while True:
                now = get_now()
                if (now.hour == 11 and now.minute == 15 and now.second >= HOURLY_CANDLE_WAIT_SECONDS) or \
                   (now.hour == 11 and now.minute > 15) or now.hour > 11:
                    break
                time.sleep(0.1)
            print(f"[{get_now_str()}] Building 10:15 candle from 1min candles...", flush=True)
            built_candle = fetch_hourly_from_minutes(kite, index_token)
            if built_candle.empty:
                print("Could not build 10:15 candle. Exiting.", flush=True)
                return
            hourly_df_api = fetch_today_hourly(kite, index_token, silent=True)
            hourly_df = pd.concat([hourly_df_api, built_candle], ignore_index=True)
            print(f"[{get_now_str()}] [BUILD MODE] hourly_df: {len(hourly_df_api)} API prev + 1 built = {len(hourly_df_api)+1} candles", flush=True)
        else:
            # API mode: wait HOURLY_CANDLE_WAIT_SECONDS then fetch from Zerodha hourly API
            if not FETCH_HOURLY_FROM_API:
                print(f"[{get_now_str()}] WARNING: no candle mode is active (all False). Defaulting to API mode.", flush=True)
            print(f"[{get_now_str()}] [API MODE] Waiting for 11:15:{HOURLY_CANDLE_WAIT_SECONDS:02d} to fetch completed 10:15 hourly candle...", flush=True)
            while True:
                now = get_now()
                if (now.hour == 11 and now.minute == 15 and now.second >= HOURLY_CANDLE_WAIT_SECONDS) or \
                   (now.hour == 11 and now.minute > 15) or now.hour > 11:
                    break
                time.sleep(1)
            print(f"[{get_now_str()}] Fetching 10:15 hourly candle for entry levels...", flush=True)
            hourly_df = fetch_today_hourly(kite, index_token)

        hourly_df = prepare_hourly_df_for_logic(hourly_df, context='INIT')

        if hourly_df.empty:
            print("Could not fetch hourly candles. Exiting.", flush=True)
            return

        # The 10:15 candle is the second candle of the day (index 1)
        # hourly candles: 09:15, 10:15, 11:15 ...
        if len(hourly_df) < 2:
            print("Not enough hourly candles yet. Exiting.", flush=True)
            return

        # In build/ws modes the last candle is the one we just built
        # In API mode the last candle is the completed 10:15 hourly from Zerodha
        candle_1015       = hourly_df.iloc[-1]
        long_entry_price  = candle_1015['high'] + 1
        short_entry_price = candle_1015['low']  - 1

        print(f"[{get_now_str()}] Entry candle | O:{candle_1015['open']} H:{candle_1015['high']} L:{candle_1015['low']} C:{candle_1015['close']}", flush=True)
        print(f"Initial levels -> long_entry: {long_entry_price} | short_entry: {short_entry_price}", flush=True)

        # -- Initial strike calculation ----------------------------------------
        spot_sym  = f"{SPOT_EXCHANGE}:{SYMBOL_NAME}"
        spot_data = get_ltp_safe(kite, [spot_sym])
        spot_ltp  = spot_data.get(spot_sym, {}).get('last_price', 0)
        if spot_ltp == 0:
            print("Spot LTP fetch failed. Exiting.", flush=True)
            return

        # -- Initial strike placeholder ----------------------------------------
        # Strikes are calculated from live spot ATM at open time in try_open_call/put.
        # POS1_STRIKE_OFFSET / POS2_STRIKE_OFFSET control ITM depth (set at top of file).
        # +N = N strikes ITM, 0 = ATM, -N = N strikes OTM.
        entry_call_strike = 0
        entry_put_strike  = 0

        print(f"Spot: {spot_ltp} | long_entry: {long_entry_price} | short_entry: {short_entry_price}", flush=True)
        print(f"Mode: {OPTION_MODE} | POS1 offset: +{POS1_STRIKE_OFFSET} | POS2 offset: +{POS2_STRIKE_OFFSET}", flush=True)

        # -- Option token cache (populated at entry, cleared at close) ----------
        # Subscribe only when entry condition is met, unsubscribe at close
        token_to_sym = {}   # token -> (strike, opt_type, symbol)

        def subscribe_option(strike, opt_type):
            # Subscribe a single strike/type to websocket and cache its token
            tok, sym = get_token_and_symbol(options_df, strike, opt_type)
            if tok is None:
                print(f"subscribe_option: no token for {strike} {opt_type}", flush=True)
                return False
            token_to_sym[tok] = (strike, opt_type, sym)
            kws.subscribe([tok])
            kws.set_mode(kws.MODE_LTP, [tok])
            print(f"Subscribed {opt_type} {strike} | token: {tok} | sym: {sym}", flush=True)
            return True

        def unsubscribe_option(strike, opt_type):
            # Unsubscribe from websocket and remove from cache
            for tok, (s, t, sym) in list(token_to_sym.items()):
                if s == strike and t == opt_type:
                    kws.unsubscribe([tok])
                    del token_to_sym[tok]
                    print(f"Unsubscribed {opt_type} {strike} | sym: {sym}", flush=True)
                    return

        def get_option_ltp(strike, opt_type):
            # Get live LTP — websocket first, REST fallback if not yet ticked
            for tok, (s, t, sym) in token_to_sym.items():
                if s == strike and t == opt_type:
                    with tick_lock:
                        ltp = live_market_data.get(tok, 0.0)
                    if ltp > 0:
                        return ltp
                    # WS has no tick yet — fetch via REST and seed the cache
                    try:
                        resp = kite.ltp(f"{OPT_EXCHANGE}:{sym}")
                        rest_ltp = resp.get(f"{OPT_EXCHANGE}:{sym}", {}).get('last_price', 0.0)
                        if rest_ltp > 0:
                            with tick_lock:
                                live_market_data[tok] = rest_ltp
                            return rest_ltp
                    except Exception:
                        pass
                    return 0.0
            return 0.0

        def get_option_sym(strike, opt_type):
            # Get trading symbol from cache
            for tok, (s, t, sym) in token_to_sym.items():
                if s == strike and t == opt_type:
                    return sym
            return None

        # -- Tradebook ---------------------------------------------------------
        try:
            tradebook_df = pd.read_csv(TRADEBOOK_CSV_PATH)
        except:
            tradebook_df = pd.DataFrame(columns=[
                'Datetime', 'Expiry', 'Strike', 'Type',
                'Entry_Price', 'Exit_Price', 'Leg_PnL', 'Total_PnL',
                'Quantity', 'Position_Number', 'Close_Reason'
            ])

        # Track individual leg PnLs for the Trades column in daily PnL
        daily_trades = []

        # -- Strategy state ----------------------------------------------------
        call_position_open = False
        put_position_open  = False
        entry_call_price   = 0.0
        entry_put_price    = 0.0
        stop_call_index_price   = 0.0
        stop_put_index_price    = 0.0
        realized_profit         = 0.0
        pos_num                 = 0
        first_position_side     = None
        day_over                = False
        call_just_opened   = False
        put_just_opened    = False

        last_hourly_check_hour  = 11   # skip 11:15 hourly logic since entry levels are set at startup
        last_print_minute       = -1

        final_realized_pnl      = 0.0

        # -- Helpers: open/close positions -------------------------------------
        def try_open_call(label, current_spot):
            nonlocal call_position_open, entry_call_price, entry_call_strike
            nonlocal stop_call_index_price, first_position_side, call_just_opened

            order_side = 'BUY' if OPTION_MODE == 'buy' else 'SELL'
            action_str = 'BUY CALL' if OPTION_MODE == 'buy' else 'SELL CALL'

            # Calculate strike from live ATM at open time
            offset = POS1_STRIKE_OFFSET if label == '1st' else POS2_STRIKE_OFFSET
            atm = int(round(current_spot / STRIKE_STEP) * STRIKE_STEP)
            entry_call_strike = atm - offset * STRIKE_STEP
            print(f"[{get_now_str()}] {action_str} {label} | ATM: {atm} | offset: +{offset} | CE strike: {entry_call_strike}", flush=True)

            # Subscribe only if not already in cache for this exact strike
            if get_option_sym(entry_call_strike, 'CE') is None:
                if not subscribe_option(entry_call_strike, 'CE'):
                    print(f"[{get_now_str()}] {action_str} {label} skipped  -  subscription failed for {entry_call_strike}", flush=True)
                    return False
                time.sleep(3)  # allow websocket to deliver first tick

            sym = get_option_sym(entry_call_strike, 'CE')
            ltp = get_option_ltp(entry_call_strike, 'CE')   # WS first, REST fallback built-in
            if ltp == 0:
                print(f"[{get_now_str()}] {action_str} {label} skipped  -  LTP is 0 (WS + REST both failed)", flush=True)
                return False

            place_order(kite, sym, QUANTITY, order_side, LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym, order_side, exchange=OPT_EXCHANGE) if LIVE_MODE else ltp

            entry_call_price      = exec_price
            # Index stop: for sell -> opposite trigger (long_entry); for buy -> short_entry (if price reverses back)
            stop_call_index_price = long_entry_price if OPTION_MODE == 'sell' else short_entry_price
            call_position_open    = True
            call_just_opened      = True
            if label == '1st':
                first_position_side = 'call'

            trigger_ref = short_entry_price if OPTION_MODE == 'sell' else long_entry_price
            print(f"[{get_now_str()}] {action_str} {label} OPENED | Strike: {entry_call_strike} | Entry: {exec_price} | Index: {spot_now} | Trigger: {trigger_ref} | Stop: {stop_call_index_price}", flush=True)
            tradebook_df.loc[len(tradebook_df)] = [
                get_now_str(), str(current_expiry), entry_call_strike, 'CE',
                exec_price, None, None, None,
                QUANTITY, pos_num + 1, f'open_{label}'
            ]
            return True

        def try_open_put(label, current_spot):
            nonlocal put_position_open, entry_put_price, entry_put_strike
            nonlocal stop_put_index_price, first_position_side, put_just_opened

            order_side = 'BUY' if OPTION_MODE == 'buy' else 'SELL'
            action_str = 'BUY PUT' if OPTION_MODE == 'buy' else 'SELL PUT'

            # Calculate strike from live ATM at open time
            offset = POS1_STRIKE_OFFSET if label == '1st' else POS2_STRIKE_OFFSET
            atm = int(round(current_spot / STRIKE_STEP) * STRIKE_STEP)
            entry_put_strike = atm + offset * STRIKE_STEP
            print(f"[{get_now_str()}] {action_str} {label} | ATM: {atm} | offset: +{offset} | PE strike: {entry_put_strike}", flush=True)

            # Subscribe only if not already in cache for this exact strike
            if get_option_sym(entry_put_strike, 'PE') is None:
                if not subscribe_option(entry_put_strike, 'PE'):
                    print(f"[{get_now_str()}] {action_str} {label} skipped  -  subscription failed for {entry_put_strike}", flush=True)
                    return False
                time.sleep(3)  # allow websocket to deliver first tick

            sym = get_option_sym(entry_put_strike, 'PE')
            ltp = get_option_ltp(entry_put_strike, 'PE')   # WS first, REST fallback built-in
            if ltp == 0:
                print(f"[{get_now_str()}] {action_str} {label} skipped  -  LTP is 0 (WS + REST both failed)", flush=True)
                return False

            place_order(kite, sym, QUANTITY, order_side, LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym, order_side, exchange=OPT_EXCHANGE) if LIVE_MODE else ltp

            entry_put_price      = exec_price
            # Index stop: for sell -> opposite trigger (short_entry); for buy -> long_entry (if price reverses back)
            stop_put_index_price = short_entry_price if OPTION_MODE == 'sell' else long_entry_price
            put_position_open    = True
            put_just_opened      = True
            if label == '1st':
                first_position_side = 'put'

            trigger_ref = long_entry_price if OPTION_MODE == 'sell' else short_entry_price
            print(f"[{get_now_str()}] {action_str} {label} OPENED | Strike: {entry_put_strike} | Entry: {exec_price} | Index: {spot_now} | Trigger: {trigger_ref} | Stop: {stop_put_index_price}", flush=True)
            tradebook_df.loc[len(tradebook_df)] = [
                get_now_str(), str(current_expiry), entry_put_strike, 'PE',
                exec_price, None, None, None,
                QUANTITY, pos_num + 1, f'open_{label}'
            ]
            return True

        def close_call(reason, current_ltp, current_ltp_idx=0):
            nonlocal call_position_open, entry_call_price
            nonlocal realized_profit, pos_num, day_over, final_realized_pnl

            sym        = get_option_sym(entry_call_strike, 'CE')
            exit_side  = 'SELL' if OPTION_MODE == 'buy' else 'BUY'
            action_str = 'BUY CALL' if OPTION_MODE == 'buy' else 'SELL CALL'

            place_order(kite, sym, QUANTITY, exit_side, LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym, exit_side, exchange=OPT_EXCHANGE) if LIVE_MODE else current_ltp

            # buy mode: profit when exit > entry; sell mode: profit when entry > exit
            if OPTION_MODE == 'buy':
                leg_pnl = QUANTITY * (exec_price - entry_call_price) - commission(QUANTITY, entry_call_price, exec_price)
            else:
                leg_pnl = QUANTITY * (entry_call_price - exec_price) - commission(QUANTITY, exec_price, entry_call_price)
            total_pnl = realized_profit + leg_pnl

            trigger_ref = short_entry_price if OPTION_MODE == 'sell' else long_entry_price
            print(f"[{get_now_str()}] {action_str} CLOSED | Reason: {reason} | PnL: {leg_pnl:.2f} | Total: {total_pnl:.2f} | Index: {current_ltp_idx} | Trigger: {trigger_ref} | Stop: {stop_call_index_price}", flush=True)

            tradebook_df.loc[len(tradebook_df)] = [
                get_now_str(), str(current_expiry), entry_call_strike, 'CE',
                entry_call_price, exec_price,
                round(leg_pnl, 2), round(total_pnl, 2),
                QUANTITY, pos_num + 1, reason
            ]
            daily_trades.append(round(leg_pnl, 2))
            final_realized_pnl  += leg_pnl
            call_position_open   = False
            entry_call_price     = 0.0
            unsubscribe_option(entry_call_strike, 'CE')

            return leg_pnl, total_pnl

        def close_put(reason, current_ltp, current_ltp_idx=0):
            nonlocal put_position_open, entry_put_price
            nonlocal realized_profit, pos_num, day_over, final_realized_pnl

            sym        = get_option_sym(entry_put_strike, 'PE')
            exit_side  = 'SELL' if OPTION_MODE == 'buy' else 'BUY'
            action_str = 'BUY PUT' if OPTION_MODE == 'buy' else 'SELL PUT'

            place_order(kite, sym, QUANTITY, exit_side, LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym, exit_side, exchange=OPT_EXCHANGE) if LIVE_MODE else current_ltp

            # buy mode: profit when exit > entry; sell mode: profit when entry > exit
            if OPTION_MODE == 'buy':
                leg_pnl = QUANTITY * (exec_price - entry_put_price) - commission(QUANTITY, entry_put_price, exec_price)
            else:
                leg_pnl = QUANTITY * (entry_put_price - exec_price) - commission(QUANTITY, exec_price, entry_put_price)
            total_pnl = realized_profit + leg_pnl

            trigger_ref = long_entry_price if OPTION_MODE == 'sell' else short_entry_price
            print(f"[{get_now_str()}] {action_str} CLOSED | Reason: {reason} | PnL: {leg_pnl:.2f} | Total: {total_pnl:.2f} | Index: {current_ltp_idx} | Trigger: {trigger_ref} | Stop: {stop_put_index_price}", flush=True)

            tradebook_df.loc[len(tradebook_df)] = [
                get_now_str(), str(current_expiry), entry_put_strike, 'PE',
                entry_put_price, exec_price,
                round(leg_pnl, 2), round(total_pnl, 2),
                QUANTITY, pos_num + 1, reason
            ]
            daily_trades.append(round(leg_pnl, 2))
            final_realized_pnl += leg_pnl
            put_position_open   = False
            entry_put_price     = 0.0
            unsubscribe_option(entry_put_strike, 'PE')

            return leg_pnl, total_pnl

        # ======================================================================
        # MAIN LOOP
        # -- LOOP FREQUENCY CONTROL --------------------------------------------
        # Set LOOP_MODE to control how often entry/PnL/stop logic fires:
        #   'every_second'  - fires every second (default, best for live)
        #   'every_5sec'    - fires every 5 seconds
        #   'every_minute'  - fires once at the start of each new minute
        #   'every_5min'    - fires once at the start of each 5-minute bar
        # ======================================================================
        LOOP_MODE  = 'every_second'
        LOOP_SLEEP = 1   # seconds between each loop iteration

        def should_fire(now, last_ts):
            elapsed = (now - last_ts).total_seconds()
            if LOOP_MODE == 'every_second' : return elapsed >= 1
            if LOOP_MODE == 'every_5sec'   : return elapsed >= 5
            if LOOP_MODE == 'every_minute' : return now.minute != last_ts.minute
            if LOOP_MODE == 'every_5min'   : return (now.minute // 5) != (last_ts.minute // 5)
            return True

        print(f"[{get_now_str()}] Starting main loop | Mode: {LOOP_MODE}...", flush=True)
        last_fired_ts = get_now() - timedelta(seconds=60)  # ensure first tick fires immediately

        while True:
            time.sleep(LOOP_SLEEP)
            now = get_now()

            if day_over:
                break

            # Force exit at EOD (checked every loop iteration regardless of LOOP_MODE)
            if now.hour == EXIT_HOUR and now.minute >= EXIT_MINUTE:
                print(f"[{get_now_str()}] FORCE EXIT TIME REACHED", flush=True)
                if call_position_open:
                    ltp = get_option_ltp(entry_call_strike, 'CE')
                    close_call('force_exit', ltp, spot_ltp_now)
                    day_over = True
                if put_position_open:
                    ltp = get_option_ltp(entry_put_strike, 'PE')
                    close_put('force_exit', ltp, spot_ltp_now)
                    day_over = True
                if not call_position_open and not put_position_open:
                    day_over = True
                break

            # -- Fire strategy logic based on LOOP_MODE ------------------------
            if not should_fire(now, last_fired_ts):
                continue
            last_fired_ts = now

            # -- Get current index LTP from websocket --------------------------
            with tick_lock:
                spot_ltp_now = live_market_data.get(index_token, 0.0)
            if spot_ltp_now == 0:
                print(f"[{get_now_str()}] Spot LTP is 0, skipping.", flush=True)
                continue

            spot_now   = spot_ltp_now  # live websocket LTP used for all entry and stop checks

            force_exit = (now.hour == EXIT_HOUR and now.minute >= EXIT_MINUTE)

            # Reset just-opened flags on every fire
            call_just_opened = False
            put_just_opened  = False

            # -- HOURLY LOGIC at xx:15:01 (ws/build) or xx:15:xx (api) ----------
            # WS mode   : fires at second==0, snapshots ws_candle accumulator
            # BUILD mode: fires at second==0, waits HOURLY_CANDLE_WAIT_SECONDS internally before fetching
            # API mode  : fires at second>=HOURLY_CANDLE_WAIT_SECONDS
            hourly_trigger = False
            if HOURLY_LOGIC_ENABLED and now.minute == 15 and now.hour != last_hourly_check_hour and now.hour > 10:
                if BUILD_CANDLE_FROM_WS or BUILD_CANDLE_FROM_MINUTES:
                    hourly_trigger = (now.second == 0)
                else:  # FETCH_HOURLY_FROM_API or fallback
                    hourly_trigger = (now.second >= HOURLY_CANDLE_WAIT_SECONDS)

            if hourly_trigger:
                last_hourly_check_hour = now.hour
                mode_str = 'WS' if BUILD_CANDLE_FROM_WS else ('BUILD' if BUILD_CANDLE_FROM_MINUTES else 'API')
                print(f"[{get_now_str()}] Hourly logic firing for hour {now.hour} | mode: {mode_str}...", flush=True)

                if BUILD_CANDLE_FROM_WS:
                    # Snapshot the accumulator — close is the last tick at :01
                    print(f"[{get_now_str()}] [WS] Snapshotting ws_candle...", flush=True)
                    built = fetch_hourly_from_ws()
                    # Reset accumulator for the next hour
                    with ws_candle_lock:
                        ws_candle['open']       = None
                        ws_candle['high']       = None
                        ws_candle['low']        = None
                        ws_candle['close']      = None
                        ws_candle['hour_start'] = None
                        ws_candle['tick_count'] = 0
                    print(f"[{get_now_str()}] [WS] Accumulator reset for next hour", flush=True)
                    prev_df = fetch_today_hourly(kite, index_token, silent=True)
                    if built.empty or prev_df.empty:
                        print(f"[{get_now_str()}] [WS] Could not build candle, skipping logic.", flush=True)
                        hourly_df = pd.DataFrame()
                    else:
                        hourly_df = pd.concat([prev_df, built], ignore_index=True)

                elif BUILD_CANDLE_FROM_MINUTES:
                    # Wait HOURLY_CANDLE_WAIT_SECONDS then fetch and slice 1min candles
                    print(f"[{get_now_str()}] [BUILD] Waiting {HOURLY_CANDLE_WAIT_SECONDS}s for candles to settle...", flush=True)
                    time.sleep(HOURLY_CANDLE_WAIT_SECONDS)
                    built = fetch_hourly_from_minutes(kite, index_token)
                    prev_df = fetch_today_hourly(kite, index_token, silent=True)
                    if built.empty or prev_df.empty:
                        print(f"[{get_now_str()}] [BUILD] Could not build candle, skipping logic.", flush=True)
                        hourly_df = pd.DataFrame()
                    else:
                        hourly_df = pd.concat([prev_df, built], ignore_index=True)

                else:
                    # API mode: wait then fetch completed hourly candle from Zerodha
                    if not FETCH_HOURLY_FROM_API:
                        print(f"[{get_now_str()}] WARNING: no candle mode active, defaulting to API mode.", flush=True)
                    print(f"[{get_now_str()}] [API MODE] Fetching hourly candle from Zerodha...", flush=True)
                    time.sleep(HOURLY_CANDLE_WAIT_SECONDS)
                    hourly_df = fetch_today_hourly(kite, index_token)

                hourly_df = prepare_hourly_df_for_logic(hourly_df, context='HOURLY')

                if not hourly_df.empty and len(hourly_df) >= 2 and pos_num == 0:
                    latest_candle   = hourly_df.iloc[-1]
                    previous_candle = hourly_df.iloc[-2]

                    candle_range    = latest_candle['high'] - latest_candle['low']
                    is_inside_bar   = (latest_candle['high'] <= previous_candle['high'] and
                                       latest_candle['low']  >= previous_candle['low'])
                    is_red_candle   = latest_candle['close'] < latest_candle['open']
                    close_near_low  = (latest_candle['close'] - latest_candle['low'])  <= candle_range * CANDLE_CLOSE_THRESHOLD
                    close_near_high = (latest_candle['high']  - latest_candle['close']) <= candle_range * CANDLE_CLOSE_THRESHOLD

                    # --- Detailed candle print for every hourly logic check ---
                    print(f"[{get_now_str()}] PREV CANDLE   | D:{previous_candle.get('date', 'N/A')} O:{previous_candle['open']} H:{previous_candle['high']} L:{previous_candle['low']} C:{previous_candle['close']}", flush=True)
                    print(f"[{get_now_str()}] HOURLY CANDLE | D:{latest_candle.get('date', 'N/A')} O:{latest_candle['open']} H:{latest_candle['high']} L:{latest_candle['low']} C:{latest_candle['close']}", flush=True)
                    cnl_val = latest_candle['close'] - latest_candle['low']
                    cnh_val = latest_candle['high']  - latest_candle['close']
                    cnl_thr = candle_range * CANDLE_CLOSE_THRESHOLD
                    cnh_thr = candle_range * CANDLE_CLOSE_THRESHOLD
                    print(f"  range:{candle_range:.1f} | threshold:{CANDLE_CLOSE_THRESHOLD} | thr_val:{cnl_thr:.1f}", flush=True)
                    print(f"  close_near_low :{close_near_low}  | (C-L):{cnl_val:.1f} <= range*thr:{cnl_thr:.1f}", flush=True)
                    print(f"  close_near_high:{close_near_high} | (H-C):{cnh_val:.1f} <= range*thr:{cnh_thr:.1f}", flush=True)
                    print(f"  is_red:{is_red_candle}", flush=True)
                    print(f"  is_inside_bar:{is_inside_bar} | call_open:{call_position_open} | put_open:{put_position_open} | pos_num:{pos_num}", flush=True)

                    if not call_position_open and not put_position_open:
                        # No position -> update entry levels if inside bar
                        if INSIDE_BAR_LOGIC_ENABLED and is_inside_bar:
                            long_entry_price  = latest_candle['high'] + 1
                            short_entry_price = latest_candle['low']  - 1
                            print(f"[{get_now_str()}] INSIDE BAR TRIGGERED | long_entry: {long_entry_price} | short_entry: {short_entry_price}", flush=True)
                        else:
                            print(f"[{get_now_str()}] No position, no inside bar -> no action", flush=True)
                    else:
                        # Position open -> trail stops (first position only)
                        if TRAILING_STOP_LOGIC_ENABLED:
                            # -------------------------------------------------------
                            # CALL POSITION trail conditions:
                            #
                            # SELL CALL (short/bearish): profits when market falls.
                            #   Trail stop UP when we get a BEARISH candle (red + close near low).
                            #   This locks in a lower high as the new stop.
                            #   Stop moves to candle_high + 1.
                            #
                            # BUY CALL (long/bullish): profits when market rises.
                            #   Trail stop UP when we get a BULLISH candle (green + close near high).
                            #   This locks in a higher low as the new stop.
                            #   Stop moves to candle_low - 1.
                            # -------------------------------------------------------
                            if OPTION_MODE == 'sell':
                                call_trail_condition = is_red_candle and close_near_low
                            else:  # buy
                                call_trail_condition = (not is_red_candle) and close_near_high

                            # -------------------------------------------------------
                            # PUT POSITION trail conditions:
                            #
                            # SELL PUT (short/bullish): profits when market rises.
                            #   Trail stop DOWN when we get a BULLISH candle (green + close near high).
                            #   Stop moves to candle_low - 1.
                            #
                            # BUY PUT (long/bearish): profits when market falls.
                            #   Trail stop DOWN when we get a BEARISH candle (red + close near low).
                            #   Stop moves to candle_high + 1.
                            # -------------------------------------------------------
                            if OPTION_MODE == 'sell':
                                put_trail_condition = (not is_red_candle) and close_near_high
                            else:  # buy
                                put_trail_condition = is_red_candle and close_near_low

                            print(f"  call_trail_condition:{call_trail_condition} | put_trail_condition:{put_trail_condition} | mode:{OPTION_MODE}", flush=True)

                            if call_position_open:
                                if call_trail_condition:
                                    if OPTION_MODE == 'sell':
                                        # Bearish candle: stop moves to candle_high+1
                                        # 2nd pos = PUT, enters when spot >= long_entry -> tighten long_entry
                                        new_stop = latest_candle['high'] + 1
                                        stop_call_index_price = new_stop
                                        long_entry_price      = new_stop
                                        print(f"[{get_now_str()}] CALL STOP TRAILED | mode:sell | new stop:{stop_call_index_price} | long_entry updated:{long_entry_price} | short_entry unchanged:{short_entry_price}", flush=True)
                                    else:
                                        # Bullish candle: stop moves to candle_low-1
                                        # 2nd pos = PUT, enters when spot <= short_entry -> short_entry unchanged
                                        new_stop = latest_candle['low'] - 1
                                        stop_call_index_price = new_stop
                                        print(f"[{get_now_str()}] CALL STOP TRAILED | mode:buy  | new stop:{stop_call_index_price} | long_entry unchanged:{long_entry_price} | short_entry unchanged:{short_entry_price}", flush=True)
                                else:
                                    print(f"[{get_now_str()}] CALL position open but trail condition NOT met -> no trail", flush=True)

                            if put_position_open:
                                if put_trail_condition:
                                    if OPTION_MODE == 'sell':
                                        # Bullish candle: stop moves to candle_low-1
                                        # 2nd pos = CALL, enters when spot <= short_entry -> tighten short_entry
                                        new_stop = latest_candle['low'] - 1
                                        stop_put_index_price = new_stop
                                        short_entry_price    = new_stop
                                        print(f"[{get_now_str()}] PUT STOP TRAILED  | mode:sell | new stop:{stop_put_index_price} | short_entry updated:{short_entry_price} | long_entry unchanged:{long_entry_price}", flush=True)
                                    else:
                                        # Bearish candle: stop moves to candle_high+1
                                        # 2nd pos = CALL, enters when spot >= long_entry -> tighten long_entry
                                        new_stop = latest_candle['high'] + 1
                                        stop_put_index_price = new_stop
                                        long_entry_price     = new_stop
                                        print(f"[{get_now_str()}] PUT STOP TRAILED  | mode:buy  | new stop:{stop_put_index_price} | long_entry updated:{long_entry_price} | short_entry unchanged:{short_entry_price}", flush=True)
                                else:
                                    print(f"[{get_now_str()}] PUT position open but trail condition NOT met -> no trail", flush=True)

            # -- ENTRY LOGIC ---------------------------------------------------
            # sell mode: short_entry -> open_call (sell CE ITM), long_entry -> open_put (sell PE ITM)
            # buy  mode: long_entry  -> open_call (buy  CE),     short_entry -> open_put (buy  PE)
            if not call_position_open and not put_position_open:

                if pos_num == 0:
                    if OPTION_MODE == 'sell':
                        if   spot_now <= short_entry_price: try_open_call('1st', spot_now)
                        elif spot_now >= long_entry_price:  try_open_put('1st', spot_now)
                    else:  # buy
                        if   spot_now >= long_entry_price:  try_open_call('1st', spot_now)
                        elif spot_now <= short_entry_price: try_open_put('1st', spot_now)

                elif pos_num == 1:
                    if realized_profit >= 0:
                        print(f"[{get_now_str()}] 2nd position skipped  -  realized profit {realized_profit:.2f} >= 0 | day_over", flush=True)
                        day_over = True
                    else:
                        if OPTION_MODE == 'sell':
                            if first_position_side == 'call' and spot_now >= long_entry_price:
                                try_open_put('2nd', spot_now)
                            elif first_position_side == 'put' and spot_now <= short_entry_price:
                                try_open_call('2nd', spot_now)
                        else:  # buy
                            if first_position_side == 'call' and spot_now <= short_entry_price:
                                try_open_put('2nd', spot_now)
                            elif first_position_side == 'put' and spot_now >= long_entry_price:
                                try_open_call('2nd', spot_now)

            # -- MINUTE STATUS PRINT ------------------------------------------
            if now.minute != last_print_minute:
                print(f"\n[{get_now_str()}] -- STATUS --------------------------------------", flush=True)
                print(f"  Spot: {spot_ltp_now} | long_entry: {long_entry_price} | short_entry: {short_entry_price}", flush=True)
                print(f"  pos_num: {pos_num} | realized_pnl: {realized_profit:.2f} | global_stop: {global_stop:.2f} | global_target: {global_target:.2f}", flush=True)
                if call_position_open:
                    call_label = 'BUY CALL' if OPTION_MODE == 'buy' else 'SELL CALL'
                    print(f"  {call_label} OPEN | Strike: {entry_call_strike} | Entry: {entry_call_price} | IndexStop: {stop_call_index_price}", flush=True)
                elif put_position_open:
                    put_label = 'BUY PUT' if OPTION_MODE == 'buy' else 'SELL PUT'
                    print(f"  {put_label} OPEN  | Strike: {entry_put_strike}  | Entry: {entry_put_price} | IndexStop: {stop_put_index_price}", flush=True)
                else:
                    print(f"  No position open | waiting for entry signal", flush=True)
                print(f"  -----------------------------------------------------", flush=True)

            # -- P&L + EXIT LOGIC ---------------------------------------------
            if call_position_open and not call_just_opened:
                ltp_call = get_option_ltp(entry_call_strike, 'CE')
                if ltp_call == 0:
                    print(f"[{get_now_str()}] CALL LTP is 0, skipping exit check.", flush=True)
                else:
                    if OPTION_MODE == 'buy':
                        call_pnl = QUANTITY * (ltp_call - entry_call_price) - commission(QUANTITY, entry_call_price, ltp_call)
                    else:
                        call_pnl = QUANTITY * (entry_call_price - ltp_call) - commission(QUANTITY, ltp_call, entry_call_price)
                    total_pnl       = realized_profit + call_pnl

                    sl_hit          = call_pnl < -POS1_STOP * LOTS
                    target_hit      = call_pnl >  global_target
                    # index stop direction depends on mode
                    if OPTION_MODE == 'buy':
                        index_sl_hit = spot_now < stop_call_index_price  # buy call: stop if price falls back below trigger
                    else:
                        index_sl_hit = spot_now > stop_call_index_price  # sell call: stop if price rises to opposite trigger
                    global_stop_hit = total_pnl <= global_stop

                    close_reason = None
                    if pos_num == 1:
                        if   total_pnl >= POS2_CLOSE_TARGET: close_reason = 'total_profit_positive'
                        elif global_stop_hit:  close_reason = 'global_stop'
                        elif force_exit:       close_reason = 'force_exit'
                    else:
                        if   global_stop_hit:  close_reason = 'global_stop'
                        elif sl_hit:           close_reason = 'sl_hit'
                        elif target_hit:       close_reason = 'target_hit'
                        elif index_sl_hit:     close_reason = 'index_sl_hit'
                        elif force_exit:       close_reason = 'force_exit'

                    action_str = 'BUY CALL' if OPTION_MODE == 'buy' else 'SELL CALL'
                    if now.minute != last_print_minute:
                        print(f"[{get_now_str()}] {action_str} P&L | Entry: {entry_call_price} | LTP: {ltp_call} | Unrealized: {call_pnl:.2f} | Total: {total_pnl:.2f}", flush=True)

                    if close_reason:
                        leg_pnl, total_pnl = close_call(close_reason, ltp_call, spot_now)

                        if close_reason in ('target_hit', 'force_exit', 'global_stop') or pos_num == 1:
                            day_over = True
                            print('day_over', flush=True)
                        else:
                            realized_profit = leg_pnl
                            pos_num         = 1
                            print('Look_for_second_reverse_position', flush=True)
                            if realized_profit < 0 and first_position_side == 'call' and spot_now >= long_entry_price:
                                try_open_put('2nd', spot_now)
                            elif realized_profit >= 0:
                                print(f"[{get_now_str()}] Realized profit not negative | day_over", flush=True)
                                day_over = True

            if put_position_open and not put_just_opened:
                ltp_put = get_option_ltp(entry_put_strike, 'PE')
                if ltp_put == 0:
                    print(f"[{get_now_str()}] PUT LTP is 0, skipping exit check.", flush=True)
                else:
                    if OPTION_MODE == 'buy':
                        put_pnl = QUANTITY * (ltp_put - entry_put_price) - commission(QUANTITY, entry_put_price, ltp_put)
                    else:
                        put_pnl = QUANTITY * (entry_put_price - ltp_put) - commission(QUANTITY, ltp_put, entry_put_price)
                    total_pnl       = realized_profit + put_pnl

                    sl_hit          = put_pnl < -POS1_STOP * LOTS
                    target_hit      = put_pnl >  global_target
                    # index stop direction depends on mode
                    if OPTION_MODE == 'buy':
                        index_sl_hit = spot_now > stop_put_index_price  # buy put: stop if price rises back above trigger
                    else:
                        index_sl_hit = spot_now < stop_put_index_price  # sell put: stop if price falls to opposite trigger
                    global_stop_hit = total_pnl <= global_stop

                    close_reason = None
                    if pos_num == 1:
                        if   total_pnl >= POS2_CLOSE_TARGET: close_reason = 'total_profit_positive'
                        elif global_stop_hit:  close_reason = 'global_stop'
                        elif force_exit:       close_reason = 'force_exit'
                    else:
                        if   global_stop_hit:  close_reason = 'global_stop'
                        elif sl_hit:           close_reason = 'sl_hit'
                        elif target_hit:       close_reason = 'target_hit'
                        elif index_sl_hit:     close_reason = 'index_sl_hit'
                        elif force_exit:       close_reason = 'force_exit'

                    action_str = 'BUY PUT' if OPTION_MODE == 'buy' else 'SELL PUT'
                    if now.minute != last_print_minute:
                        print(f"[{get_now_str()}] {action_str} P&L  | Entry: {entry_put_price} | LTP: {ltp_put} | Unrealized: {put_pnl:.2f} | Total: {total_pnl:.2f}", flush=True)

                    if close_reason:
                        leg_pnl, total_pnl = close_put(close_reason, ltp_put, spot_now)

                        if close_reason in ('target_hit', 'force_exit', 'global_stop') or pos_num == 1:
                            day_over = True
                            print('day_over', flush=True)
                        else:
                            realized_profit = leg_pnl
                            pos_num         = 1
                            print('Look_for_second_reverse_position', flush=True)
                            if realized_profit < 0 and first_position_side == 'put' and spot_now <= short_entry_price:
                                try_open_call('2nd', spot_now)
                            elif realized_profit >= 0:
                                print(f"[{get_now_str()}] Realized profit not negative | day_over", flush=True)
                                day_over = True
            
            last_print_minute = now.minute

        # -- End of day --------------------------------------------------------
        if kws.is_connected():
            kws.close()

        print(f"\n{'='*40}", flush=True)
        print(f"FINAL DAY PNL: {final_realized_pnl:.2f}", flush=True)
        print(f"{'='*40}\n", flush=True)

        tradebook_df.to_csv(TRADEBOOK_CSV_PATH, index=False)
        print("Tradebook saved.", flush=True)

        try:
            daily_df = pd.read_csv(DAILY_PNL_CSV_PATH)
        except:
            daily_df = pd.DataFrame(columns=['Date', 'Final_PnL', 'Global_Stop', 'Trades'])
        daily_df.loc[len(daily_df)] = [
            datetime.now(IST).date(),
            round(final_realized_pnl, 2),
            round(global_stop, 2),
            str(daily_trades)
        ]
        daily_df.to_csv(DAILY_PNL_CSV_PATH, index=False)
        print("Daily PnL saved.", flush=True)

    except Exception as e:
        print(f"\nCRITICAL CRASH: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            input("Press Enter to exit...")
        except:
            pass

# ==============================================================================
# 8. SUPERVISOR  (identical pattern to original file)
# ==============================================================================
if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_process()

    else:
        worker_process = None
        print(f"Supervisor active. Waiting for {TOKEN_SWAP_TIME}...", flush=True)

        try:
            while True:
                if datetime.now().strftime("%H:%M") == TOKEN_SWAP_TIME:

                    if datetime.now().weekday() in [5, 6]:
                        print("Weekend  -  skipping.", flush=True)
                        time.sleep(70)
                        continue

                    print(f"\n{'*'*50}", flush=True)
                    print(f"LAUNCHING WORKER: {datetime.now().date()}", flush=True)
                    print(f"{'*'*50}\n", flush=True)

                    try:
                        worker_process = subprocess.Popen(
                            [sys.executable, __file__, "--worker"],
                            stdout = subprocess.PIPE,
                            stderr = subprocess.STDOUT,
                            text   = True,
                            bufsize = 1,
                        )
                        for line in worker_process.stdout:
                            print(line, end='', flush=True)
                        worker_process.wait()

                    except Exception as e:
                        print(f"Worker error: {e}", flush=True)

                    print("Worker finished. Supervisor sleeping.", flush=True)
                    worker_process = None
                    time.sleep(70)

                time.sleep(30)

        except KeyboardInterrupt:
            print("\nSupervisor stopped by user.", flush=True)

        finally:
            if worker_process and worker_process.poll() is None:
                print("[SAFETY] Killing orphan worker...", flush=True)
                try:
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
                except:
                    worker_process.kill()
                print("[SAFETY] Worker killed.", flush=True)

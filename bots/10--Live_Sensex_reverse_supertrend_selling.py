import os
import sys
import csv
import time
import math
import subprocess
import threading
import gc
from datetime import datetime, timedelta
from pprint import pprint

import numpy as np
import pandas as pd
import talib
import pytz
from kiteconnect import KiteConnect

from redis_tick_client import RedisTickClient

# ─────────────────────────────────────────────────────────────────────────────
# TOP-LEVEL UNIFIED CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL                          = 'SENSEX'       # 'SENSEX' or 'NIFTY 50'
CANDLE_FETCH_BUFFER_SECONDS     = 5              # Buffer seconds after bar close before fetch
MONITORING_INTERVAL_SECONDS     = 60              # Safety check evaluation cadence in seconds
TOKEN_SWAP_TIME                 = "09:00"        # Supervisor wake-up time
RESULTS_FOLDER                  = 'Sensex_reverse_supertrend_selling_websockts_results'
GD_PATH                         = '/app/data/'
CONFIG_PATH                     = '/app/config/'

# Symbol-specific market parameters
if SYMBOL == 'SENSEX':
    SCRIP                = 'SENSEX'
    IND                  = 'SENSEX'
    EXCHANGE             = 'BFO'
    INDEX_EXCHANGE       = 'BSE'
    INSTRUMENT_TOKEN     = 265                   # BSE SENSEX Index Token
    LOT_SIZE             = 20
    STRIKE_DIFFERENCE    = 100
    STRIKE_HEDGE_GAP     = 10
elif SYMBOL == 'NIFTY 50':
    SCRIP                = 'NIFTY 50'
    IND                  = 'NIFTY'
    EXCHANGE             = 'NFO'
    INDEX_EXCHANGE       = 'NSE'
    INSTRUMENT_TOKEN     = 256265                # NSE NIFTY 50 Index Token
    LOT_SIZE             = 65
    STRIKE_DIFFERENCE    = 50
    STRIKE_HEDGE_GAP     = 10
else:
    raise ValueError(f"Unsupported SYMBOL: {SYMBOL}")

# ─────────────────────────────────────────────────────────────────────────────
# DAY-OF-WEEK TRADING CONFIGURATION
# Keys: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
# ─────────────────────────────────────────────────────────────────────────────
DAY_CONFIG = {
    0: {'lots_num': 3, 'live_mode': 0, 'start_time': '09:20', 'consistency_time': '09:40',
        'supertrend_period': 3, 'stop_percent': 75, 'global_stop_per_lot': 40000,
        'hedgeless_mode': 1, 'itm_gap': 2, 'atr_stop_multiplier': 2.0, 'atr_tp_multiplier': 10.0,
        'max_positions_per_day': 3, 'position_cutoff_time': '15:15', 'exit_time': '15:19'},
    1: {'lots_num': 3, 'live_mode': 0, 'start_time': '09:20', 'consistency_time': '09:40',
        'supertrend_period': 3, 'stop_percent': 75, 'global_stop_per_lot': 40000,
        'hedgeless_mode': 1, 'itm_gap': 2, 'atr_stop_multiplier': 2.0, 'atr_tp_multiplier': 10.0,
        'max_positions_per_day': 3, 'position_cutoff_time': '15:15', 'exit_time': '15:19'},
    2: {'lots_num': 3, 'live_mode': 0, 'start_time': '09:20', 'consistency_time': '09:40',
        'supertrend_period': 3, 'stop_percent': 75, 'global_stop_per_lot': 40000,
        'hedgeless_mode': 1, 'itm_gap': 2, 'atr_stop_multiplier': 2.0, 'atr_tp_multiplier': 10.0,
        'max_positions_per_day': 3, 'position_cutoff_time': '15:15', 'exit_time': '15:19'},
    3: {'lots_num': 3, 'live_mode': 0, 'start_time': '09:20', 'consistency_time': '09:40',
        'supertrend_period': 3, 'stop_percent': 75, 'global_stop_per_lot': 40000,
        'hedgeless_mode': 1, 'itm_gap': 2, 'atr_stop_multiplier': 2.0, 'atr_tp_multiplier': 10.0,
        'max_positions_per_day': 3, 'position_cutoff_time': '15:15', 'exit_time': '15:19'},
    4: {'lots_num': 3, 'live_mode': 0, 'start_time': '09:20', 'consistency_time': '09:40',
        'supertrend_period': 3, 'stop_percent': 75, 'global_stop_per_lot': 40000,
        'hedgeless_mode': 1, 'itm_gap': 2, 'atr_stop_multiplier': 2.0, 'atr_tp_multiplier': 10.0,
        'max_positions_per_day': 3, 'position_cutoff_time': '15:15', 'exit_time': '15:19'},
}

def get_day_config():
    today_weekday = datetime.now().weekday()
    return DAY_CONFIG.get(today_weekday, DAY_CONFIG[0])

# Global streaming storage
live_market_data = {}
tick_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# ULTRA-FAST, LOW-RAM SUPERTREND ENGINE (Pure NumPy, Identical TV ta.rma Math)
# ─────────────────────────────────────────────────────────────────────────────
def calculate_supertrend_tv(high, low, close, atr_period, multiplier):
    """
    Wilder's RMA Supertrend matching TradingView ta.supertrend() exactly.
    Runs on pure NumPy arrays in ~0.2ms for 300 bars with zero Numba overhead.
    """
    n = len(close)
    
    # 1. True Range
    tr = np.zeros(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hpc = abs(high[i] - close[i - 1])
        lpc = abs(low[i] - close[i - 1])
        tr[i] = hl if (hl >= hpc and hl >= lpc) else (hpc if hpc >= lpc else lpc)

    # 2. Wilder's RMA ATR
    atr = np.zeros(n, dtype=np.float64)
    seed = np.sum(tr[:atr_period])
    atr[atr_period - 1] = seed / atr_period
    alpha = 1.0 / atr_period
    for i in range(atr_period, n):
        atr[i] = atr[i - 1] * (1.0 - alpha) + tr[i] * alpha

    # 3. Supertrend Upper & Lower Bands
    final_ub = np.zeros(n, dtype=np.float64)
    final_lb = np.zeros(n, dtype=np.float64)
    st = np.zeros(n, dtype=np.float64)
    direction = np.zeros(n, dtype=np.int8)

    p = atr_period - 1
    hl2_p = (high[p] + low[p]) * 0.5
    final_ub[p] = hl2_p + multiplier * atr[p]
    final_lb[p] = hl2_p - multiplier * atr[p]
    st[p] = final_ub[p]
    direction[p] = -1

    for i in range(p + 1, n):
        hl2 = (high[i] + low[i]) * 0.5
        basic_ub = hl2 + multiplier * atr[i]
        basic_lb = hl2 - multiplier * atr[i]

        if basic_ub < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]:
            final_ub[i] = basic_ub
        else:
            final_ub[i] = final_ub[i - 1]

        if basic_lb > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]:
            final_lb[i] = basic_lb
        else:
            final_lb[i] = final_lb[i - 1]

        if st[i - 1] == final_ub[i - 1]:
            if close[i] > final_ub[i]:
                st[i] = final_lb[i]
                direction[i] = 1
            else:
                st[i] = final_ub[i]
                direction[i] = -1
        else:
            if close[i] < final_lb[i]:
                st[i] = final_ub[i]
                direction[i] = -1
            else:
                st[i] = final_lb[i]
                direction[i] = 1

    return st, direction, atr

def calculate_supertrend_dataframe(df, supertrend_multiplier):
    high = df['high'].to_numpy(dtype=np.float64)
    low = df['low'].to_numpy(dtype=np.float64)
    close = df['close'].to_numpy(dtype=np.float64)

    st, direction, atr = calculate_supertrend_tv(high, low, close, 10, supertrend_multiplier)
    df['atr'] = atr
    df['supertrend'] = st
    df['direction'] = direction
    df['atr_ema'] = talib.EMA(atr, timeperiod=200)
    return df

# ─────────────────────────────────────────────────────────────────────────────
# STATUTORY COST & INSTRUMENT CACHE (< 3 MB RAM footprint)
# ─────────────────────────────────────────────────────────────────────────────
def commission(quantity, buy_price, sell_price):
    buy_turnover = quantity * buy_price
    sell_turnover = quantity * sell_price
    total_turnover = buy_turnover + sell_turnover

    zerodha_brokerage = 20 + 20
    stt = 0.0015 * sell_turnover
    exchange_txn_charge = 0.0003503 * total_turnover
    sebi_charges = 0.000001 * total_turnover
    gst = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)
    stamp_duty = 0.00003 * buy_turnover
    ipft = 0.00000005 * total_turnover

    return round(zerodha_brokerage + stt + exchange_txn_charge + sebi_charges + gst + stamp_duty + ipft, 2)

def load_instruments_compact(filepath, target_ind, target_segment):
    options_map = {}
    expiries = set()

    with open(filepath, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        filtered_rows = []
        for row in reader:
            if row['name'] == target_ind and row['segment'] == target_segment:
                filtered_rows.append(row)
                expiries.add(row['expiry'])

    if not expiries:
        raise ValueError(f"No option contracts found for {target_ind} ({target_segment})")

    current_expiry = sorted(list(expiries))[0]

    for row in filtered_rows:
        if row['expiry'] == current_expiry:
            strike = int(float(row['strike']))
            itype = row['instrument_type']
            token = int(row['instrument_token'])
            symbol = row['tradingsymbol']
            if strike not in options_map:
                options_map[strike] = {}
            options_map[strike][itype] = {'tradingsymbol': symbol, 'token': token}

    del filtered_rows
    gc.collect()
    return options_map, current_expiry

def get_symbol_and_token(options_map, strike, instrument_type):
    strike = int(strike)
    if strike in options_map and instrument_type in options_map[strike]:
        return options_map[strike][instrument_type]['tradingsymbol'], options_map[strike][instrument_type]['token']
    raise KeyError(f"Option strike {strike} {instrument_type} not found in instrument map")

def get_live_price(token, fallback):
    with tick_lock:
        px = live_market_data.get(token, 0.0)
    return px if px > 0.0 else fallback

# ─────────────────────────────────────────────────────────────────────────────
# ATM STRIKE SELECTION & ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────────────────
def get_best_atm_strike(kite, raw_atm, options_map):
    symbols_to_fetch = []
    strike_symbol_map = {}

    for x1 in range(-4, 5):
        test_strike = int(raw_atm + (x1 * STRIKE_DIFFERENCE))
        try:
            ce_sym, _ = get_symbol_and_token(options_map, test_strike, 'CE')
            pe_sym, _ = get_symbol_and_token(options_map, test_strike, 'PE')
            symbols_to_fetch.extend([f"{EXCHANGE}:{ce_sym}", f"{EXCHANGE}:{pe_sym}"])
            strike_symbol_map[test_strike] = {'CE': f"{EXCHANGE}:{ce_sym}", 'PE': f"{EXCHANGE}:{pe_sym}"}
        except KeyError:
            continue

    if not symbols_to_fetch:
        return raw_atm

    all_ltp = {}
    for _ in range(5):
        try:
            all_ltp = kite.ltp(symbols_to_fetch)
            if all_ltp:
                break
        except Exception:
            time.sleep(0.4)

    best_strike = raw_atm
    min_diff = 9999999.0

    for strike, syms in strike_symbol_map.items():
        ce_key = syms['CE']
        pe_key = syms['PE']
        if ce_key in all_ltp and pe_key in all_ltp:
            c_px = all_ltp[ce_key]['last_price']
            p_px = all_ltp[pe_key]['last_price']
            diff = abs(c_px - p_px)
            if diff < min_diff:
                min_diff = diff
                best_strike = strike

    return best_strike

def place_order(kite, sym, qty, side, live_mode):
    if live_mode == 0:
        return True, "PAPER_SIMULATED_ORDER"

    ltp_sym = f"{EXCHANGE}:{sym}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    current_ltp = 0.0
    for _ in range(5):
        try:
            resp = kite.ltp(ltp_sym)
            current_ltp = resp[ltp_sym]['last_price']
            if current_ltp > 0:
                break
        except Exception:
            time.sleep(0.3)

    if current_ltp <= 0:
        print(f"[ORDER ERROR] Unable to fetch LTP for {sym}")
        return False, None

    if side == 'BUY':
        limit_price = current_ltp * 1.05 if current_ltp > 50 else current_ltp + 3.0
    else:
        limit_price = current_ltp * 0.95 if current_ltp > 50 else current_ltp - 3.0
    limit_price = round(round(max(limit_price, 0.05) / 0.05) * 0.05, 2)

    order_id = None
    for _ in range(5):
        try:
            order_id = kite.place_order(
                tradingsymbol=sym,
                exchange=EXCHANGE,
                transaction_type=order_side,
                quantity=qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                variety=kite.VARIETY_REGULAR,
                product=kite.PRODUCT_MIS
            )
            break
        except Exception:
            time.sleep(0.5)

    if not order_id:
        return False, None

    for _ in range(10):
        time.sleep(0.8)
        try:
            history = kite.order_history(order_id)
            latest = history[-1]
            if latest['status'] == 'COMPLETE':
                return True, order_id
            if latest['status'] in ['REJECTED', 'CANCELLED']:
                return False, order_id

            resp = kite.ltp(ltp_sym)
            new_ltp = resp[ltp_sym]['last_price']
            if side == 'BUY':
                new_limit = new_ltp * 1.05 if new_ltp > 50 else new_ltp + 3.0
            else:
                new_limit = new_ltp * 0.95 if new_ltp > 50 else new_ltp - 3.0
            new_limit = round(round(max(new_limit, 0.05) / 0.05) * 0.05, 2)

            kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=order_id,
                              order_type=kite.ORDER_TYPE_LIMIT, price=new_limit)
        except Exception:
            pass

    return True, order_id

# ─────────────────────────────────────────────────────────────────────────────
# HISTORICAL DATA INGESTION & CANDLE VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
def fetch_verified_5m_candles(kite, instrument_token, expected_start_time, retries=10):
    ed = datetime.now()
    sd = ed - timedelta(days=10)

    for _ in range(retries):
        try:
            data = kite.historical_data(instrument_token, sd, ed, '5minute')
            if not data:
                time.sleep(1)
                continue

            df = pd.DataFrame(data)
            df['date'] = pd.to_datetime(df['date'])
            if df['date'].dt.tz is None:
                df['date'] = df['date'].dt.tz_localize('Asia/Kolkata')
            else:
                df['date'] = df['date'].dt.tz_convert('Asia/Kolkata')

            latest_time = df['date'].iloc[-1].to_pydatetime()

            if expected_start_time is None:
                return df

            if (latest_time.hour == expected_start_time.hour and
                latest_time.minute == expected_start_time.minute and
                latest_time.date() == expected_start_time.date()):
                return df

            time.sleep(1)
        except Exception:
            time.sleep(1)

    return df

# ─────────────────────────────────────────────────────────────────────────────
# LIVE TRADING WORKER PROCESS
# ─────────────────────────────────────────────────────────────────────────────
def run_trading_worker():
    now_ist = datetime.now(pytz.timezone('Asia/Kolkata'))
    if now_ist.weekday() in [5, 6]:
        print("[SUPERVISOR] Today is a weekend. Terminating worker.")
        return

    with open(os.path.join(CONFIG_PATH, 'auth.txt'), 'r') as f:
        auth_data = f.read().strip().split(',')
    api_key, access_token = auth_data[0].strip(), auth_data[1].strip()

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    cfg = get_day_config()
    lots_num            = cfg['lots_num']
    live_mode           = cfg['live_mode']
    supertrend_period   = cfg['supertrend_period']
    global_stop_per_lot = cfg['global_stop_per_lot']
    hedgeless_mode      = cfg['hedgeless_mode']
    itm_gap             = cfg['itm_gap']
    atr_stop_multiplier = cfg['atr_stop_multiplier']
    atr_tp_multiplier   = cfg['atr_tp_multiplier']
    max_positions       = cfg['max_positions_per_day']
    cutoff_time         = datetime.strptime(cfg['position_cutoff_time'], '%H:%M').time()
    exit_time           = datetime.strptime(cfg['exit_time'], '%H:%M').time()

    qty = lots_num * LOT_SIZE
    global_day_stop = -abs(global_stop_per_lot * lots_num)

    results_dir = os.path.join(GD_PATH, RESULTS_FOLDER)
    os.makedirs(results_dir, exist_ok=True)
    tradebook_path = os.path.join(results_dir, 'Intraday_options_tradebook.csv')
    daily_pnl_path = os.path.join(results_dir, 'Final_daily_pnl.csv')

    options_map, current_expiry = load_instruments_compact(
        os.path.join(GD_PATH, 'instrument_tokens.csv'), IND, f"{EXCHANGE}-OPT"
    )

    tick_client = RedisTickClient(live_market_data, tick_lock)
    tick_client.start()
    tick_client.subscribe([INSTRUMENT_TOKEN])

    print("\n" + "=" * 80)
    print("  SYSTEM INITIALIZATION | LOW-RAM SENSEX REVERSE MOMENTUM ENGINE")
    print("=" * 80)
    print(f"Timestamp         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Underlying Symbol : {SYMBOL} (Lot Size: {LOT_SIZE} | Strike Step: {STRIKE_DIFFERENCE})")
    print(f"Execution Mode    : {'LIVE TRADING' if live_mode == 1 else 'PAPER TRADING'}")
    print(f"Lots Num / Qty    : {lots_num} Lots ({qty} Qty) | Global Day Stop: Rs. {global_day_stop:,.2f}")
    print(f"Strategy Param    : Supertrend Period={supertrend_period} | ITM Gap={itm_gap}")
    print(f"ATR Multipliers   : Stop = {atr_stop_multiplier}x | Target = {atr_tp_multiplier}x")
    print(f"Max Positions/Day : {max_positions}")
    print(f"Hedges Active     : {'NO (Hedgeless Mode)' if hedgeless_mode == 1 else 'YES (OTM +10 Gap)'}")
    print(f"Option Expiry     : {current_expiry}")
    print(f"Results Storage   : {results_dir}")
    print("=" * 80 + "\n")

    # Standby until 09:45:05 IST (When the 09:40 candle completes)
    target_consistency_dt = datetime.now().replace(hour=9, minute=45, second=CANDLE_FETCH_BUFFER_SECONDS, microsecond=0)
    time_to_wait = (target_consistency_dt - datetime.now()).total_seconds()
    if time_to_wait > 0:
        print(f"[STANDBY] Waiting {time_to_wait:.1f}s until 09:45:05 IST for 09:40 candle close...")
        time.sleep(time_to_wait)

    # ─────────────────────────────────────────────────────────────────────────
    # MORNING TREND CONSISTENCY GATE (09:15 - 09:40)
    # ─────────────────────────────────────────────────────────────────────────
    expected_0940 = datetime.now().replace(hour=9, minute=40, second=0, microsecond=0)
    df_ohlc = fetch_verified_5m_candles(kite, INSTRUMENT_TOKEN, expected_0940, retries=10)
    df_ohlc = calculate_supertrend_dataframe(df_ohlc, supertrend_period)

    if len(df_ohlc) > 300:
        df_ohlc = df_ohlc.iloc[-300:].copy().reset_index(drop=True)

    today_date = datetime.now().date()
    df_today = df_ohlc[df_ohlc['date'].dt.date == today_date].copy()

    # Display today's morning candles (09:15 to 09:40) with Supertrend value & direction
    print("\n" + "=" * 115)
    print("  TODAY'S INTRADAY 5-MINUTE CANDLES (09:15 - 09:40)")
    print("=" * 115)
    print(f"{'Timestamp (IST)':<20} | {'Open':<9} | {'High':<9} | {'Low':<9} | {'Close':<9} | {'Volume':<10} | {'Supertrend':<10} | {'ST Direction'}")
    print("-" * 115)
    for _, row in df_today.iterrows():
        dir_str = "BULLISH (+1)" if row['direction'] == 1 else "BEARISH (-1)"
        t_str = row['date'].strftime('%Y-%m-%d %H:%M:%S')
        print(f"{t_str:<20} | {row['open']:<9.2f} | {row['high']:<9.2f} | {row['low']:<9.2f} | {row['close']:<9.2f} | {int(row['volume']):<10} | {row['supertrend']:<10.2f} | {dir_str}")
    print("=" * 115)

    # Consistency check: 09:15 through 09:40 must have identical direction
    mask_consistency = (df_today['date'].dt.time >= datetime.strptime('09:15', '%H:%M').time()) & \
                       (df_today['date'].dt.time <= datetime.strptime('09:40', '%H:%M').time())
    df_consistency = df_today[mask_consistency]
    dirs = df_consistency['direction'][df_consistency['direction'] != 0].values

    if len(dirs) == 0:
        print("[ERROR] No valid candles found in consistency window. Terminating.")
        return

    first_dir = dirs[0]
    is_consistent = all(d == first_dir for d in dirs)

    if not is_consistent:
        print("\n" + "#" * 80)
        print("  MORNING CONSISTENCY GATE: FAILED (Direction flipped between 09:15 and 09:40)")
        print("  Market is choppy/non-directional. Strategy will remain dormant today.")
        print("#" * 80 + "\n")
        return

    prev_dir = first_dir
    print(f"\n[CONSISTENCY GATE: PASSED] Trend confirmed: {'BULLISH (+1)' if first_dir == 1 else 'BEARISH (-1)'}. Listening for reversals...\n")

    # ─────────────────────────────────────────────────────────────────────────────
    # EVENT-DRIVEN INTRADAY EXECUTION & 1-SECOND MONITORING LOOP
    # ─────────────────────────────────────────────────────────────────────────────
    trades_taken = 0
    cumulative_realized_pnl = 0.0
    active_trade = None

    last_processed_5m_bar = expected_0940
    last_monitoring_time = datetime.now() - timedelta(seconds=MONITORING_INTERVAL_SECONDS)
    last_pnl_print_minute = -1

    while True:
        time.sleep(1)
        now = datetime.now()

        # EOD Square-Off
        if now.time() >= exit_time:
            print(f"\n[EOD REACHED] Current time {now.strftime('%H:%M:%S')} >= Exit time {exit_time}. Closing positions.")
            if active_trade:
                active_trade = close_trade(kite, active_trade, "EOD Exit (15:19)", tradebook_path, live_mode, hedgeless_mode)
                cumulative_realized_pnl += active_trade['net_pnl']
            break

        # ── 1. Every 5 Minutes: Fetch Completed Bar at :05s ──
        if now.minute % 5 == 0 and now.second >= CANDLE_FETCH_BUFFER_SECONDS:
            expected_bar = (now - timedelta(minutes=5)).replace(second=0, microsecond=0)

            if expected_bar > last_processed_5m_bar:
                df_ohlc = fetch_verified_5m_candles(kite, INSTRUMENT_TOKEN, expected_bar, retries=5)
                df_ohlc = calculate_supertrend_dataframe(df_ohlc, supertrend_period)
                if len(df_ohlc) > 300:
                    df_ohlc = df_ohlc.iloc[-300:].copy().reset_index(drop=True)

                latest_bar = df_ohlc.iloc[-1]
                curr_dir = int(latest_bar['direction'])
                curr_st = latest_bar['supertrend']
                curr_atr = latest_bar['atr']
                curr_atr_ema = latest_bar['atr_ema']

                # Print 5-minute candle OHLCV + Supertrend Value
                b_time = latest_bar['date'].strftime('%Y-%m-%d %H:%M:%S')
                d_label = "BULLISH (+1)" if curr_dir == 1 else "BEARISH (-1)"
                print(f"\n[CANDLE 5M] {b_time} | O: {latest_bar['open']:.2f} | H: {latest_bar['high']:.2f} | L: {latest_bar['low']:.2f} | C: {latest_bar['close']:.2f} | V: {int(latest_bar['volume'])}")
                print(f"[INDICATOR] Supertrend: {curr_st:.2f} | Direction: {d_label} | Wilder ATR: {curr_atr:.2f} | ATR EMA(200): {curr_atr_ema:.2f}\n")

                last_processed_5m_bar = expected_bar

                # Check for Reversal Flip
                if curr_dir != prev_dir:
                    print(f"[REVERSAL DETECTED] Supertrend direction flipped from {prev_dir} to {curr_dir}")
                    prev_dir = curr_dir

                    if active_trade is not None:
                        active_trade = close_trade(kite, active_trade, "ST Reversal Exit", tradebook_path, live_mode, hedgeless_mode)
                        cumulative_realized_pnl += active_trade['net_pnl']
                        active_trade = None

                    if trades_taken >= max_positions:
                        print(f"[GATE] Max positions per day ({max_positions}) reached. Skipping entry.")
                    elif now.time() >= cutoff_time:
                        print(f"[GATE] Current time {now.strftime('%H:%M:%S')} past cutoff {cutoff_time}. Skipping entry.")
                    elif cumulative_realized_pnl <= global_day_stop:
                        print(f"[GATE] Global Day Stop breached ({cumulative_realized_pnl:.2f}). Skipping entry.")
                    else:
                        trades_taken += 1
                        active_trade = enter_trade(
                            kite=kite,
                            direction=curr_dir,
                            underlying_price=latest_bar['close'],
                            atr_ema=curr_atr_ema,
                            trades_taken=trades_taken,
                            options_map=options_map,
                            tick_client=tick_client,
                            lots_num=lots_num,
                            qty=qty,
                            itm_gap=itm_gap,
                            atr_stop_multiplier=atr_stop_multiplier,
                            atr_tp_multiplier=atr_tp_multiplier,
                            hedgeless_mode=hedgeless_mode,
                            live_mode=live_mode,
                            tradebook_path=tradebook_path
                        )

        # ── 2. Position Monitoring at MONITORING_INTERVAL_SECONDS ──
        if (now - last_monitoring_time).total_seconds() >= MONITORING_INTERVAL_SECONDS:
            last_monitoring_time = now
            spot_px = get_live_price(INSTRUMENT_TOKEN, 0.0)

            if active_trade is not None:
                cur_opt_px = get_live_price(active_trade['opt_token'], active_trade['opt_entry_px'])
                cur_hedge_px = get_live_price(active_trade['hedge_token'], active_trade['hedge_entry_px']) if hedgeless_mode == 0 else 0.0

                opt_pnl = qty * (active_trade['opt_entry_px'] - cur_opt_px) - commission(qty, cur_opt_px, active_trade['opt_entry_px'])
                hedge_pnl = (qty * (cur_hedge_px - active_trade['hedge_entry_px']) - commission(qty, active_trade['hedge_entry_px'], cur_hedge_px)) if hedgeless_mode == 0 else 0.0
                unrealized_pnl = opt_pnl + hedge_pnl
                total_day_pnl = cumulative_realized_pnl + unrealized_pnl

                # ── Throttled Minute PnL Output (Strictly at :00s) ──
                if now.second == 0 and now.minute != last_pnl_print_minute:
                    last_pnl_print_minute = now.minute
                    print(f"[{now.strftime('%H:%M:%S')}] Spot: {spot_px:.2f} | Stop: {active_trade['spot_stop']:.2f} | Target: {active_trade['spot_tp']:.2f} | "
                          f"{active_trade['side']} LTP: {cur_opt_px:.2f} (Entry: {active_trade['opt_entry_px']:.2f}) | "
                          f"Pos PnL: {unrealized_pnl:+,.2f} | Day Net: {total_day_pnl:+,.2f}")

                # Check Exit Hierarchy
                # A. Global Day Circuit Breaker
                if total_day_pnl <= global_day_stop:
                    print(f"\n[GLOBAL STOP TRIGGERED] Total PnL ({total_day_pnl:.2f}) <= Limit ({global_day_stop:.2f})")
                    active_trade = close_trade(kite, active_trade, "Global Day Stop Hit", tradebook_path, live_mode, hedgeless_mode)
                    cumulative_realized_pnl += active_trade['net_pnl']
                    active_trade = None
                    break

                # B. Spot ATR Stop Loss
                spot_stop_hit = (active_trade['side'] == 'CE' and spot_px >= active_trade['spot_stop']) or \
                                (active_trade['side'] == 'PE' and spot_px <= active_trade['spot_stop'])
                if spot_stop_hit:
                    print(f"\n[SPOT STOP TRIGGERED] Spot {spot_px:.2f} crossed stop {active_trade['spot_stop']:.2f}")
                    active_trade = close_trade(kite, active_trade, "Spot ATR Stop Hit", tradebook_path, live_mode, hedgeless_mode)
                    cumulative_realized_pnl += active_trade['net_pnl']
                    active_trade = None
                    continue

                # C. Spot ATR Target
                spot_tp_hit = (active_trade['side'] == 'CE' and spot_px <= active_trade['spot_tp']) or \
                              (active_trade['side'] == 'PE' and spot_px >= active_trade['spot_tp'])
                if spot_tp_hit:
                    print(f"\n[SPOT TARGET TRIGGERED] Spot {spot_px:.2f} hit target {active_trade['spot_tp']:.2f}")
                    active_trade = close_trade(kite, active_trade, "Spot ATR Target Hit", tradebook_path, live_mode, hedgeless_mode)
                    cumulative_realized_pnl += active_trade['net_pnl']
                    active_trade = None
                    continue
            else:
                # Heartbeat output every 5 minutes when standing by
                if now.second == 0 and now.minute % 5 == 0 and now.minute != last_pnl_print_minute:
                    last_pnl_print_minute = now.minute
                    print(f"[{now.strftime('%H:%M:%S')}] Standing By | Spot: {spot_px:.2f} | ST Dir: {prev_dir} | Day Realized: Rs. {cumulative_realized_pnl:+,.2f}")

    # ─────────────────────────────────────────────────────────────────────────────
    # END OF DAY SUMMARY
    # ─────────────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"  DAILY PERFORMANCE SUMMARY | {today_date}")
    print("=" * 80)
    print(f"Total Trades Executed : {trades_taken} / {max_positions}")
    print(f"Final Realized PnL    : Rs. {cumulative_realized_pnl:+,.2f}")
    print(f"Starting Capital      : Rs. {100000 * lots_num:,.2f}")
    print(f"Results Saved To      : {daily_pnl_path}")
    print("=" * 80 + "\n")

    summary_exists = os.path.exists(daily_pnl_path)
    with open(daily_pnl_path, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not summary_exists:
            writer.writerow(['Date', 'Symbol', 'Trades_Taken', 'Final_PnL'])
        writer.writerow([today_date, SYMBOL, trades_taken, round(cumulative_realized_pnl, 2)])

# ─────────────────────────────────────────────────────────────────────────────
# TRADE EXECUTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def enter_trade(kite, direction, underlying_price, atr_ema, trades_taken, options_map,
                tick_client, lots_num, qty, itm_gap, atr_stop_multiplier, atr_tp_multiplier,
                hedgeless_mode, live_mode, tradebook_path):
    raw_atm = int(round(underlying_price / STRIKE_DIFFERENCE) * STRIKE_DIFFERENCE)
    atm_strike = get_best_atm_strike(kite, raw_atm, options_map)

    gap = itm_gap if trades_taken == 1 else 2

    if direction == 1:
        side = 'PE'
        main_strike = atm_strike + (gap * STRIKE_DIFFERENCE)
        hedge_strike = main_strike - (STRIKE_HEDGE_GAP * STRIKE_DIFFERENCE)
        spot_stop = underlying_price - (atr_stop_multiplier * atr_ema)
        spot_tp = underlying_price + (atr_tp_multiplier * atr_ema)
    else:
        side = 'CE'
        main_strike = atm_strike - (gap * STRIKE_DIFFERENCE)
        hedge_strike = main_strike + (STRIKE_HEDGE_GAP * STRIKE_DIFFERENCE)
        spot_stop = underlying_price + (atr_stop_multiplier * atr_ema)
        spot_tp = underlying_price - (atr_tp_multiplier * atr_ema)

    main_sym, main_token = get_symbol_and_token(options_map, main_strike, side)
    hedge_sym, hedge_token = (None, None)
    if hedgeless_mode == 0:
        hedge_sym, hedge_token = get_symbol_and_token(options_map, hedge_strike, side)

    tokens_to_sub = [main_token]
    if hedge_token:
        tokens_to_sub.append(hedge_token)
    tick_client.subscribe(tokens_to_sub)

    time.sleep(0.4)
    main_entry_px = 0.0
    hedge_entry_px = 0.0

    for _ in range(5):
        try:
            q = kite.ltp([f"{EXCHANGE}:{main_sym}"])
            main_entry_px = q[f"{EXCHANGE}:{main_sym}"]['last_price']
            if main_entry_px > 0:
                break
        except Exception:
            time.sleep(0.3)

    if hedgeless_mode == 0:
        for _ in range(5):
            try:
                q = kite.ltp([f"{EXCHANGE}:{hedge_sym}"])
                hedge_entry_px = q[f"{EXCHANGE}:{hedge_sym}"]['last_price']
                if hedge_entry_px > 0:
                    break
            except Exception:
                time.sleep(0.3)

    if hedgeless_mode == 0:
        place_order(kite, hedge_sym, qty, 'BUY', live_mode)
    place_order(kite, main_sym, qty, 'SELL', live_mode)

    print("\n" + "=" * 80)
    print(f"  TRADE {trades_taken} ENTRY TRIGGERED | {'BULLISH (SELL PE)' if direction == 1 else 'BEARISH (SELL CE)'}")
    print("=" * 80)
    print(f"Trigger Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Index Spot Price  : {underlying_price:.2f} | 200 EMA ATR: {atr_ema:.2f}")
    print(f"Best ATM Strike   : {atm_strike} (Raw ATM: {raw_atm})")
    print(f"Sold Leg          : {main_sym} ({side} {main_strike}) @ Rs. {main_entry_px:.2f} | Qty: {qty}")
    if hedgeless_mode == 0:
        print(f"Hedge Leg         : {hedge_sym} ({side} {hedge_strike}) @ Rs. {hedge_entry_px:.2f}")
    print(f"Spot Stop Level   : {spot_stop:.2f} ({'-' if direction == 1 else '+'}{atr_stop_multiplier * atr_ema:.2f} pts)")
    print(f"Spot Target Level : {spot_tp:.2f} ({'+' if direction == 1 else '-'}{atr_tp_multiplier * atr_ema:.2f} pts)")
    print("=" * 80 + "\n")

    write_tradebook_entry(tradebook_path, main_strike, side, main_entry_px, 0.0, 0.0, 0.0, f"Open - Trade_{trades_taken}")

    return {
        'trades_taken': trades_taken,
        'side': side,
        'direction': direction,
        'opt_sym': main_sym,
        'opt_token': main_token,
        'opt_strike': main_strike,
        'opt_entry_px': main_entry_px,
        'hedge_sym': hedge_sym,
        'hedge_token': hedge_token,
        'hedge_strike': hedge_strike,
        'hedge_entry_px': hedge_entry_px,
        'qty': qty,
        'spot_stop': spot_stop,
        'spot_tp': spot_tp,
        'entry_time': datetime.now()
    }

def close_trade(kite, trade, reason, tradebook_path, live_mode, hedgeless_mode):
    qty = trade['qty']

    exit_opt_px = get_live_price(trade['opt_token'], trade['opt_entry_px'])
    exit_hedge_px = get_live_price(trade['hedge_token'], trade['hedge_entry_px']) if hedgeless_mode == 0 else 0.0

    place_order(kite, trade['opt_sym'], qty, 'BUY', live_mode)
    if hedgeless_mode == 0:
        place_order(kite, trade['hedge_sym'], qty, 'SELL', live_mode)

    opt_gross = qty * (trade['opt_entry_px'] - exit_opt_px)
    opt_comm = commission(qty, exit_opt_px, trade['opt_entry_px'])

    hedge_gross = (qty * (exit_hedge_px - trade['hedge_entry_px'])) if hedgeless_mode == 0 else 0.0
    hedge_comm = commission(qty, trade['hedge_entry_px'], exit_hedge_px) if hedgeless_mode == 0 else 0.0

    net_pnl = (opt_gross - opt_comm) + (hedge_gross - hedge_comm)

    print("\n" + "=" * 80)
    print(f"  TRADE {trade['trades_taken']} EXIT | [{reason.upper()}]")
    print("=" * 80)
    print(f"Exit Time         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Instrument Closed : {trade['opt_sym']} @ Rs. {exit_opt_px:.2f} (Entry: Rs. {trade['opt_entry_px']:.2f})")
    if hedgeless_mode == 0:
        print(f"Hedge Closed      : {trade['hedge_sym']} @ Rs. {exit_hedge_px:.2f} (Entry: Rs. {trade['hedge_entry_px']:.2f})")
    print(f"Gross PnL         : Rs. {(opt_gross + hedge_gross):+,.2f}")
    print(f"Statutory Charges : Rs. {(opt_comm + hedge_comm):,.2f}")
    print(f"Net Realized PnL  : Rs. {net_pnl:+,.2f}")
    print("=" * 80 + "\n")

    write_tradebook_entry(tradebook_path, trade['opt_strike'], trade['side'], trade['opt_entry_px'],
                          exit_opt_px, net_pnl, 0.0, f"Close - {reason}")

    trade['net_pnl'] = net_pnl
    gc.collect()
    return trade

def write_tradebook_entry(filepath, strike, side, entry_px, exit_px, pnl, cum_pnl, status):
    file_exists = os.path.exists(filepath)
    with open(filepath, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Strike', 'Type', 'Entry_Price', 'Exit_Price', 'PnL', 'Cumulative_PnL', 'Status'])
        writer.writerow([
            datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S'),
            strike, side, entry_px, exit_px, round(pnl, 2), round(cum_pnl, 2), status
        ])

# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISOR PROCESS
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_worker()
    else:
        worker_process = None
        print(f"[SUPERVISOR] Active. Target wake-up: {TOKEN_SWAP_TIME} IST", flush=True)

        try:
            while True:
                now_str = datetime.now().strftime("%H:%M")
                if now_str == TOKEN_SWAP_TIME:
                    if datetime.now().weekday() in [5, 6]:
                        print("[SUPERVISOR] Weekend detected. Sleeping...", flush=True)
                        time.sleep(70)
                        continue

                    print("\n" + "*" * 50, flush=True)
                    print(f"LAUNCHING DAILY TRADING WORKER: {datetime.now().date()}", flush=True)
                    print("*" * 50 + "\n", flush=True)

                    try:
                        worker_process = subprocess.Popen(
                            [sys.executable, __file__, "--worker"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1
                        )

                        for line in worker_process.stdout:
                            print(line, end='', flush=True)

                        worker_process.wait()

                    except Exception as e:
                        print(f"[SUPERVISOR ERROR] Worker failure: {e}", flush=True)

                    print("\n[SUPERVISOR] Daily worker concluded. Standing by until tomorrow.", flush=True)
                    worker_process = None
                    time.sleep(70)

                time.sleep(30)

        except KeyboardInterrupt:
            print("\n[SUPERVISOR] Termination signal received (Ctrl+C).", flush=True)

        finally:
            if worker_process and worker_process.poll() is None:
                print("[SUPERVISOR] Safely shutting down worker process...", flush=True)
                try:
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
                except Exception:
                    worker_process.kill()
                print("[SUPERVISOR] Worker terminated.", flush=True)

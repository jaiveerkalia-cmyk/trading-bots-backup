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
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────────────────────────────
# OS-LEVEL MEMORY MANAGEMENT (< 30-40 MB RAM CEILING)
# ─────────────────────────────────────────────────────────────────────────────
try:
    import ctypes
    libc = ctypes.CDLL("libc.so.6")
    # M_ARENA_MAX = -8 in glibc; restricts multithreaded memory pool fragmentation
    libc.mallopt(-8, 2)
    def trim_memory():
        gc.collect()
        libc.malloc_trim(0)
except Exception:
    def trim_memory():
        gc.collect()

# Standard Timezone
IST = ZoneInfo('Asia/Kolkata')

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY & INSTRUMENT CONSTANTS (EXACT MATCH TO ORIGINAL)
# ─────────────────────────────────────────────────────────────────────────────
scrip                       = 'NIFTY 50'
ind                         = 'NIFTY'
EXCHANGE                    = 'NFO'
INDEX_EXCHANGE              = 'NSE'
INSTRUMENT_TOKEN            = 256265
min_delta                   = 0.15
strike_gap                  = 10
min_buy_price               = 15
entry_strike_gap            = 0
strike_difference           = 50
strike_hedge_gap            = 10
lot_size                    = 65
token_swap_time             = "09:00"

# Clock-aligned evaluation cadence: Set to 60 for round minute checks (:00),
# or set to 1, 5, 10, etc. Status telemetry remains locked to 1-minute round intervals.
MONITORING_INTERVAL_SECONDS = 60

gd_path                     = '/app/data/'
config_path                 = '/app/config/'
results_folder              = 'Nifty_sell_supertrend_results_websockets'

# ─────────────────────────────────────────────────────────────────────────────
# DAY-OF-WEEK TRADING CONFIG (EXACT ORIGINAL PARAMETERS)
# Keys: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
# live_mode: 1 = live trading (real orders placed), 0 = paper trading
# ─────────────────────────────────────────────────────────────────────────────
DAY_CONFIG = {
    0: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:20', 'supertrend_period': 4, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0, 'risk_mode_on': 1, 'max_risk_amount': 25000},  # Monday
    1: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:20', 'supertrend_period': 4, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.3, 'global_stop_per_lot': 40000, 'hedgeless_mode': 1, 'risk_mode_on': 1, 'max_risk_amount': 10000},  # Tuesday
    2: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:20', 'supertrend_period': 4, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0, 'risk_mode_on': 1, 'max_risk_amount': 25000},  # Wednesday
    3: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:20', 'supertrend_period': 4, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0, 'risk_mode_on': 1, 'max_risk_amount': 25000},  # Thursday
    4: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:20', 'supertrend_period': 4, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0, 'risk_mode_on': 1, 'max_risk_amount': 20000},  # Friday
}

def get_day_config():
    """Returns the DAY_CONFIG entry for today (weekday 0=Mon to 4=Fri)."""
    today_weekday = datetime.now(IST).weekday()
    return DAY_CONFIG.get(today_weekday, DAY_CONFIG[0])

def print_tomorrow_mode():
    """Prints all trading parameters for the next trading day."""
    day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday', 5: 'Saturday', 6: 'Sunday'}
    today_weekday = datetime.now(IST).weekday()
    next_trading_offset = 1
    next_weekday = (today_weekday + next_trading_offset) % 7
    while next_weekday in [5, 6]:
        next_trading_offset += 1
        next_weekday = (today_weekday + next_trading_offset) % 7
    next_cfg = DAY_CONFIG.get(next_weekday, DAY_CONFIG[0])
    mode_str = 'LIVE TRADING' if next_cfg['live_mode'] == 1 else 'PAPER TRADING'
    hedgeless_str = 'YES (no hedges)' if next_cfg['hedgeless_mode'] == 1 else 'NO (hedges active)'
    print(f"\n{'='*60}")
    print(f"  TOMORROW ({day_names[next_weekday]}) CONFIGURATION SUMMARY:")
    print(f"  Mode              : {mode_str}")
    print(f"  lots_num          : {next_cfg['lots_num']}")
    print(f"  start_time        : {next_cfg['start_time']}")
    print(f"  supertrend_period : {next_cfg['supertrend_period']}")
    print(f"  stop_percent      : {next_cfg['stop_percent']}")
    print(f"  reverse_threshold : {next_cfg['reverse_threshold_percentage']}")
    print(f"  global_stop/lot   : {next_cfg['global_stop_per_lot']}")
    print(f"  hedgeless_mode    : {hedgeless_str}")
    print(f"  risk_mode_on      : {next_cfg['risk_mode_on']}")
    print(f"  max_risk_amount   : {next_cfg['max_risk_amount']}")
    print(f"{'='*60}\n")

# Global streaming storage
live_market_data = {}
tick_lock = threading.Lock()

def get_live_price(instrument_token, fallback_price):
    with tick_lock:
        live_price = live_market_data.get(instrument_token, 0.0)
    return live_price if live_price > 0.0 else fallback_price

def subscribe_tokens(tick_client, tokens):
    tick_client.subscribe(tokens)

# ─────────────────────────────────────────────────────────────────────────────
# STATUTORY COMMISSION ENGINE (EXACT ORIGINAL FORMULA)
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

    total_charges = (zerodha_brokerage + stt + exchange_txn_charge + 
                     sebi_charges + gst + stamp_duty + ipft)

    return round(total_charges, 2)

# ─────────────────────────────────────────────────────────────────────────────
# STREAMING CSV INSTRUMENT CACHE (< 1.5 MB RAM)
# ─────────────────────────────────────────────────────────────────────────────
def load_instruments_compact(filepath, target_ind, target_segment):
    """
    Parses instrument_tokens.csv line-by-line using standard csv.DictReader.
    Discards all irrelevant rows without retaining them in memory.
    """
    options_map = {}
    expiries = set()
    filtered_rows = []

    with open(filepath, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
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
    trim_memory()
    return options_map, current_expiry

def get_symbol_and_token(options_map, strike, instrument_type):
    strike = int(strike)
    if strike in options_map and instrument_type in options_map[strike]:
        return options_map[strike][instrument_type]['tradingsymbol'], options_map[strike][instrument_type]['token']
    raise KeyError(f"Strike {strike} {instrument_type} not found in instrument map")

# ─────────────────────────────────────────────────────────────────────────────
# PURE-PYTHON SUPERTREND ENGINE (EXACT MATCH TO ORIGINAL NUMBA LOGIC)
# ─────────────────────────────────────────────────────────────────────────────
def calculate_supertrend_pure_python(candles, supertrend_period):
    """
    Calculates True Range, SMA ATR (rolling window = supertrend_period),
    and Supertrend matching the original Numba math exactly, with zero heap bloat.
    """
    n = len(candles)
    if n == 0:
        return candles, 0

    highs = [float(c['high']) for c in candles]
    lows = [float(c['low']) for c in candles]
    closes = [float(c['close']) for c in candles]

    # 1. True Range
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hpc, lpc)

    # 2. Simple Moving Average ATR (window = supertrend_period)
    atr = [0.0] * n
    for i in range(supertrend_period - 1, n):
        window_tr = tr[i - supertrend_period + 1 : i + 1]
        atr[i] = sum(window_tr) / supertrend_period

    # 3. Supertrend Bands (Initial index: 10, Multiplier: supertrend_period)
    period = 10
    multiplier = float(supertrend_period)

    final_ub = [0.0] * n
    final_lb = [0.0] * n
    st = [0.0] * n
    direction = [0] * n

    if n >= period:
        p = period - 1
        hl2_p = (highs[p] + lows[p]) / 2.0
        basic_ub_p = hl2_p + multiplier * atr[p]
        basic_lb_p = hl2_p - multiplier * atr[p]

        final_ub[p] = basic_ub_p
        final_lb[p] = basic_lb_p
        st[p] = basic_ub_p

        for i in range(period, n):
            hl2 = (highs[i] + lows[i]) / 2.0
            basic_ub = hl2 + multiplier * atr[i]
            basic_lb = hl2 - multiplier * atr[i]

            # Final Upper Band
            if basic_ub < final_ub[i - 1] or closes[i - 1] > final_ub[i - 1]:
                final_ub[i] = basic_ub
            else:
                final_ub[i] = final_ub[i - 1]

            # Final Lower Band
            if basic_lb > final_lb[i - 1] or closes[i - 1] < final_lb[i - 1]:
                final_lb[i] = basic_lb
            else:
                final_lb[i] = final_lb[i - 1]

            # Supertrend Value
            if st[i - 1] == final_ub[i - 1] and closes[i] <= final_ub[i]:
                st[i] = final_ub[i]
            elif st[i - 1] == final_ub[i - 1] and closes[i] > final_ub[i]:
                st[i] = final_lb[i]
            elif st[i - 1] == final_lb[i - 1] and closes[i] >= final_lb[i]:
                st[i] = final_lb[i]
            elif st[i - 1] == final_lb[i - 1] and closes[i] < final_lb[i]:
                st[i] = final_ub[i]
            else:
                st[i] = 0.0

        for i in range(period, n):
            if st[i] > 0.0:
                direction[i] = 1 if closes[i] > st[i] else -1

    return direction[-1]

# ─────────────────────────────────────────────────────────────────────────────
# ATM STRIKE SELECTION & ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────────────────
def get_best_atm_strike(kite, atm_strike, options_map):
    """
    Finds the strike with the lowest premium difference (Straddle Delta ~ 0).
    Runs batch queries against options_map in O(1) without DataFrame overhead.
    """
    start = time.time()
    symbols_to_fetch = []
    strike_symbol_map = {}

    for x1 in range(-3, 5):
        test_strike = int(atm_strike) + (x1 * 50)
        try:
            sym_put, _ = get_symbol_and_token(options_map, test_strike, 'PE')
            sym_call, _ = get_symbol_and_token(options_map, test_strike, 'CE')
            symbols_to_fetch.append(f"{EXCHANGE}:{sym_put}")
            symbols_to_fetch.append(f"{EXCHANGE}:{sym_call}")
            strike_symbol_map[test_strike] = {'PE': f"{EXCHANGE}:{sym_put}", 'CE': f"{EXCHANGE}:{sym_call}"}
        except KeyError:
            continue

    if not symbols_to_fetch:
        print("CRITICAL: No matching symbols found in instrument map. Returning input ATM.")
        return atm_strike

    all_ltp = {}
    success = False

    for attempt in range(20):
        try:
            all_ltp = kite.ltp(symbols_to_fetch)
            success = True
            break
        except Exception as e:
            print(f"API Attempt {attempt + 1}/20 failed: {e}")
            time.sleep(1)

    if not success:
        print("CRITICAL: Could not fetch quotes after 20 attempts. Returning input ATM.")
        return atm_strike

    org_price_difference = 9999999.0
    new_atm_strike = atm_strike

    print(f"\n{'Strike':<10} | {'Call LTP':<10} | {'Put LTP':<10} | {'Diff':<10}")
    print("-" * 50)

    for x1 in range(-3, 5):
        test_strike = int(atm_strike) + (x1 * 50)
        if test_strike in strike_symbol_map:
            sym_put = strike_symbol_map[test_strike]['PE']
            sym_call = strike_symbol_map[test_strike]['CE']

            if sym_put in all_ltp and sym_call in all_ltp:
                put_price = all_ltp[sym_put]['last_price']
                call_price = all_ltp[sym_call]['last_price']
                price_difference = abs(put_price - call_price)

                print(f"{test_strike:<10} | {call_price:<10} | {put_price:<10} | {price_difference:<10.2f}")

                if price_difference < org_price_difference:
                    org_price_difference = price_difference
                    new_atm_strike = test_strike

    print(f"It took {time.time() - start:0.4f} seconds")
    print(f"Selected Best ATM: {new_atm_strike} (Diff: {org_price_difference:.2f})\n")
    return new_atm_strike

def place_order(kite, sym, qty, side):
    ltp_sym = f"NFO:{sym}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    def get_ltp():
        ltp_sleep = 0.5
        for attempt in range(10):
            try:
                resp = kite.ltp(ltp_sym)
                ltp = resp[ltp_sym]['last_price']
                if ltp > 0:
                    return ltp
            except Exception as e:
                print(f"LTP fetch error attempt {attempt + 1}: {e}")
            time.sleep(ltp_sleep)
            if attempt >= 2:
                ltp_sleep += 1.0
        return None

    def get_limit_price(current_ltp):
        if side == 'BUY':
            price = current_ltp * 1.10 if current_ltp > 50 else current_ltp + 5.0
        else:
            price = current_ltp * 0.90 if current_ltp > 50 else current_ltp - 5.0
        price = max(price, 0.05)
        return round(round(price / 0.05) * 0.05, 2)

    current_ltp = get_ltp()
    if current_ltp is None:
        print('order_placement_failed: Could not fetch initial LTP')
        return

    limit_price = get_limit_price(current_ltp)
    order_id = None
    place_retries = 0
    place_sleep = 1.0

    while place_retries < 10:
        try:
            order_id = kite.place_order(
                tradingsymbol=sym,
                exchange='NFO',
                transaction_type=order_side,
                quantity=qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                variety=kite.VARIETY_REGULAR,
                product=kite.PRODUCT_MIS
            )
            break
        except Exception as e:
            place_retries += 1
            print(f'Entry_order_error attempt {place_retries}:', e)
            if place_retries == 10:
                print('order_placement_failed')
                return
            time.sleep(place_sleep)
            if place_retries >= 3:
                place_sleep += 1.0

    if not order_id:
        return

    max_modifications = 10
    mod_count = 0
    mod_sleep = 1.0

    while mod_count < max_modifications:
        time.sleep(mod_sleep)
        try:
            order_history = kite.order_history(order_id)
            latest_state = order_history[-1]
            status = latest_state['status']
            pending_qty = latest_state.get('pending_quantity', 0)

            if status == 'COMPLETE':
                break
            elif status in ['REJECTED', 'CANCELLED']:
                print(f"Order {status}. Reason: {latest_state.get('status_message', 'Unknown')}")
                break
            elif pending_qty > 0:
                print(f"Partial/No Fill. Pending Qty: {pending_qty}. Fetching latest LTP to modify...")
                new_ltp = get_ltp()
                if new_ltp:
                    new_limit_price = get_limit_price(new_ltp)
                    kite.modify_order(
                        variety=kite.VARIETY_REGULAR,
                        order_id=order_id,
                        order_type=kite.ORDER_TYPE_LIMIT,
                        price=new_limit_price
                    )
        except Exception as e:
            print('Order_modification_error:', e)

        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0

    if mod_count >= max_modifications:
        print(f"Warning: Max modifications ({max_modifications}) reached. Order may still be pending.")

# ─────────────────────────────────────────────────────────────────────────────
# APPEND-ONLY CSV LOGGING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def log_tradebook_entry(filepath, timestamp, strike, opt_type, entry_px, exit_px, pnl, cum_pnl, status):
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    with open(filepath, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Strike', 'Type', 'Entry_Price', 'Exit_Price', 'PnL', 'Cumulative_PnL', 'Status'])
        writer.writerow([timestamp, strike, opt_type, entry_px, exit_px, round(pnl, 2), round(cum_pnl, 2), status])

def log_daily_pnl_entry(filepath, timestamp, entry_call, entry_put, entry_call_px, entry_put_px, exit_call_px, exit_put_px, final_profit):
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    with open(filepath, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Entry_Call', 'Entry_Put', 'Entry_Call_Price', 'Entry_Put_Price', 'Exit_Call_Price', 'Exit_Put_Price', 'Final_Profit'])
        writer.writerow([timestamp, entry_call, entry_put, entry_call_px, entry_put_px, exit_call_px, exit_put_px, round(final_profit, 2)])

# ─────────────────────────────────────────────────────────────────────────────
# LIVE TRADING CORE STRATEGY EXECUTION
# ─────────────────────────────────────────────────────────────────────────────
def sell_fn(kite, options_map, expiry):
    # State tracking
    sell_put_flag, sell_call_flag = 0, 0
    final_profit = 0.0
    entry_call_price, entry_put_price = 0.0, 0.0
    entry_put, entry_call = 0, 0
    exit_sell_call_price, exit_sell_put_price = 0.0, 0.0
    entry_hedge_put_price, entry_hedge_call_price = 0.0, 0.0
    exit_hedge_put_price, exit_hedge_call_price = 0.0, 0.0
    current_entry_put_price, current_entry_call_price = 0.0, 0.0

    # Load today's config
    cfg = get_day_config()
    lots_num                     = cfg['lots_num']
    live_mode                    = cfg['live_mode']
    start_time                   = cfg['start_time']
    supertrend_period            = cfg['supertrend_period']
    stop_percent                 = cfg['stop_percent']
    reverse_threshold_percentage = cfg['reverse_threshold_percentage']
    global_stop_per_lot          = cfg['global_stop_per_lot']
    hedgeless_mode               = cfg['hedgeless_mode']
    risk_mode_on                 = cfg['risk_mode_on']
    max_risk_amount              = cfg['max_risk_amount']

    day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday'}
    today_name = day_names.get(datetime.now(IST).weekday(), 'Unknown')
    mode_str = 'LIVE TRADING' if live_mode == 1 else 'PAPER TRADING'
    hedgeless_str = 'YES (no hedges)' if hedgeless_mode == 1 else 'NO (hedges active)'
    
    print(f"\n{'='*60}")
    print(f"  TODAY ({today_name}) CONFIG LOADED:")
    print(f"  Mode={mode_str} | lots_num={lots_num} | start_time={start_time} | "
          f"supertrend_period={supertrend_period} | stop_percent={stop_percent} | "
          f"reverse_threshold_percentage={reverse_threshold_percentage} | "
          f"global_stop_per_lot={global_stop_per_lot} | hedgeless_mode={hedgeless_str} | "
          f"risk_mode_on={risk_mode_on} | max_risk_amount={max_risk_amount}")
    print(f"{'='*60}\n")

    results_dir = os.path.join(gd_path, results_folder)
    os.makedirs(results_dir, exist_ok=True)
    tradebook_csv = os.path.join(results_dir, 'Intraday_options_tradebook.csv')
    daily_pnl_csv = os.path.join(results_dir, 'Final_daily_pnl.csv')

    # Start WebSocket client with constrained thread stack
    threading.stack_size(128 * 1024)
    from redis_tick_client import RedisTickClient
    kws = RedisTickClient(live_market_data, tick_lock)
    kws.start()

    lots = math.floor(1 * lots_num)
    qty = lots * lot_size
    global_stop = -abs(global_stop_per_lot * lots)

    print('Qty (pre-entry, from lots_num):', qty)
    print(f'Global Stop: {global_stop}')

    # Standby until start_time
    now = datetime.now(IST)
    configured_start_time = datetime.strptime(start_time, '%H:%M').time()
    start_datetime = now.replace(hour=configured_start_time.hour, minute=configured_start_time.minute, second=0, microsecond=0)
    sleep_seconds = (start_datetime - now).total_seconds()
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

    time.sleep(7)
    print(datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S'))
    print('check_here')

    # Fetch 5m candles up to target candle time
    target_candle_config = datetime.strptime(start_time, '%H:%M') - timedelta(minutes=5)
    DAYS_TO_FETCH = 10
    ed = datetime.now(IST)
    sd = ed - timedelta(days=DAYS_TO_FETCH)

    candles = []
    while True:
        try:
            raw_candles = kite.historical_data(INSTRUMENT_TOKEN, sd, ed, '5minute')
            if not raw_candles:
                time.sleep(2)
                continue

            last_bar_dt = raw_candles[-1]['date']
            if last_bar_dt.tzinfo is None:
                last_bar_dt = last_bar_dt.replace(tzinfo=IST)
            else:
                last_bar_dt = last_bar_dt.astimezone(IST)

            target_candle_time = last_bar_dt.replace(
                hour=target_candle_config.hour,
                minute=target_candle_config.minute,
                second=0,
                microsecond=0
            )

            if last_bar_dt >= target_candle_time:
                # Filter bars up to target_candle_time and keep the last 300
                filtered = []
                for c in raw_candles:
                    c_dt = c['date']
                    if c_dt.tzinfo is None:
                        c_dt = c_dt.replace(tzinfo=IST)
                    else:
                        c_dt = c_dt.astimezone(IST)
                    if c_dt <= target_candle_time:
                        filtered.append(c)
                candles = filtered[-300:]
                del raw_candles, filtered
                break
            else:
                time.sleep(2)
        except Exception as e:
            print(f"Error fetching historical data: {e}")
            time.sleep(2)

    # Compute Supertrend direction using pure Python
    st_direction = calculate_supertrend_pure_python(candles, supertrend_period)
    del candles
    trim_memory()

    if st_direction == -1:
        sell_call_flag = 1
        sell_put_flag = 0
    elif st_direction == 1:
        sell_put_flag = 1
        sell_call_flag = 0

    print(f"Direction Flag: sell_call_flag={sell_call_flag}, sell_put_flag={sell_put_flag}")

    # ─────────────────────────────────────────────────────────────────────────
    # POSITIONS LOOP (UP TO 2 REVERSALS / 3 TOTAL ATTEMPTS)
    # ─────────────────────────────────────────────────────────────────────────
    for pos_num in range(0, 3):

        if pos_num > 1 or (pos_num > 0 and final_profit > 0):
            ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
            log_daily_pnl_entry(daily_pnl_csv, ts, entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit)
            print('day_over')
            print_tomorrow_mode()
            return

        # Reverse signal for subsequent position
        if pos_num >= 1:
            sell_call_flag, sell_put_flag = sell_put_flag, sell_call_flag
            entry_strike_gap = 2
        else:
            entry_strike_gap = 0

        # Fetch underlying NIFTY spot
        while True:
            try:
                current_price = kite.ltp(f"{INDEX_EXCHANGE}:{scrip}")[f"{INDEX_EXCHANGE}:{scrip}"]['last_price']
                break
            except Exception:
                time.sleep(1.5)

        pprint(current_price)
        atm_strike = int(round(current_price / strike_difference) * strike_difference)
        print('ATM Strike', atm_strike, 'Underlying:', current_price)

        # Advanced ATM Finder
        atm_strike = get_best_atm_strike(kite, atm_strike, options_map)
        print(sell_call_flag, sell_put_flag)

        # ── ENTRY: SELL PUT ──
        if sell_put_flag == 1:
            entry_put = atm_strike + (entry_strike_gap * strike_difference)
            hedge_put = entry_put - strike_hedge_gap * strike_difference

            sym_put, put_token = get_symbol_and_token(options_map, entry_put, 'PE')
            sym_hedge_put, hedge_put_token = get_symbol_and_token(options_map, hedge_put, 'PE')
            subscribe_tokens(kws, [put_token, hedge_put_token])
            print(sym_put, sym_hedge_put)

            for _ in range(10):
                try:
                    a1 = kite.ltp(f"{EXCHANGE}:{sym_put}")
                    entry_put_price = a1[f"{EXCHANGE}:{sym_put}"]['last_price']
                    break
                except Exception as e:
                    print(e)
                    time.sleep(1)

            if risk_mode_on == 1:
                stop_sell_put_price = (1 + stop_percent) * entry_put_price
                risk_qty = math.floor((max_risk_amount / (stop_sell_put_price - entry_put_price)) / lot_size) * lot_size
                qty = max(risk_qty, lot_size)
                print(f"Risk Mode ON: entry_put_price={entry_put_price:.2f}, stop={stop_sell_put_price:.2f}, calculated qty={qty}")

            if live_mode == 1:
                if hedgeless_mode == 0:
                    place_order(kite, sym_hedge_put, qty, 'BUY')
                place_order(kite, sym_put, qty, 'SELL')

            if hedgeless_mode == 0:
                for _ in range(10):
                    try:
                        a1 = kite.ltp(f"{EXCHANGE}:{sym_hedge_put}")
                        entry_hedge_put_price = a1[f"{EXCHANGE}:{sym_hedge_put}"]['last_price']
                        break
                    except Exception as e:
                        print(e)

            ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
            log_tradebook_entry(tradebook_csv, ts, entry_put, 'PE', entry_put_price, 0.0, 0.0, final_profit, 'Open - Sell_Put_Opened')
            current_entry_put_price = entry_put_price
            exit_hedge_put_price = entry_hedge_put_price

            print(f"{datetime.now(IST)} - Put Entry: {entry_put}, Hedge: {hedge_put} | Entry Price: {entry_put_price}, Hedge Price: {entry_hedge_put_price}, Qty: {qty}")

        # ── ENTRY: SELL CALL ──
        if sell_call_flag == 1:
            entry_call = atm_strike - (entry_strike_gap * strike_difference)
            hedge_call = entry_call + strike_hedge_gap * strike_difference

            sym_call, call_token = get_symbol_and_token(options_map, entry_call, 'CE')
            sym_hedge_call, hedge_call_token = get_symbol_and_token(options_map, hedge_call, 'CE')
            subscribe_tokens(kws, [call_token, hedge_call_token])
            print(sym_call, sym_hedge_call)

            for _ in range(10):
                try:
                    a1 = kite.ltp(f"{EXCHANGE}:{sym_call}")
                    entry_call_price = a1[f"{EXCHANGE}:{sym_call}"]['last_price']
                    break
                except Exception as e:
                    time.sleep(1)

            if risk_mode_on == 1:
                stop_sell_call_price = (1 + stop_percent) * entry_call_price
                risk_qty = math.floor((max_risk_amount / (stop_sell_call_price - entry_call_price)) / lot_size) * lot_size
                qty = max(risk_qty, lot_size)
                print(f"Risk Mode ON: entry_call_price={entry_call_price:.2f}, stop={stop_sell_call_price:.2f}, calculated qty={qty}")

            if live_mode == 1:
                if hedgeless_mode == 0:
                    place_order(kite, sym_hedge_call, qty, 'BUY')
                place_order(kite, sym_call, qty, 'SELL')

            if hedgeless_mode == 0:
                for _ in range(10):
                    try:
                        a1 = kite.ltp(f"{EXCHANGE}:{sym_hedge_call}")
                        entry_hedge_call_price = a1[f"{EXCHANGE}:{sym_hedge_call}"]['last_price']
                        break
                    except Exception as e:
                        print(e)

            ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
            log_tradebook_entry(tradebook_csv, ts, entry_call, 'CE', entry_call_price, 0.0, 0.0, final_profit, 'Open - Sell_Call_Opened')
            current_entry_call_price = entry_call_price
            exit_hedge_call_price = entry_hedge_call_price

            print(f"{datetime.now(IST)} - Call Entry: {entry_call}, Hedge: {hedge_call} | Entry Price: {entry_call_price}, Hedge Price: {entry_hedge_call_price}, Qty: {qty}")

        put_stop_price = (1 + stop_percent) * entry_put_price if sell_put_flag == 1 else 0.0
        call_stop_price = (1 + stop_percent) * entry_call_price if sell_call_flag == 1 else 0.0
        print(f"Stop Prices — put_stop_price={put_stop_price:.2f}, call_stop_price={call_stop_price:.2f}")

        # ─────────────────────────────────────────────────────────────────────
        # TWO-TIER CLOCK-ALIGNED MONITORING LOOP
        # ─────────────────────────────────────────────────────────────────────
        sell_put_profit, sell_call_profit = 0.0, 0.0
        last_monitored_marker = (-1, -1)
        last_status_print_minute = -1

        while True:
            time.sleep(0.5)
            now = datetime.now(IST)

            current_second_marker = (now.minute, now.second)
            # Tier 1: Risk Monitoring Tick aligned strictly to clock intervals
            is_monitoring_tick = (now.second % MONITORING_INTERVAL_SECONDS == 0) and (current_second_marker != last_monitored_marker)
            # Tier 2: Telemetry Logging Tick strictly once per minute on round boundary (:00s)
            is_minute_tick = (now.second == 0) and (now.minute != last_status_print_minute)

            if not (is_monitoring_tick or is_minute_tick):
                continue

            if is_monitoring_tick:
                last_monitored_marker = current_second_marker

            # 1. Update Live Streaming Prices
            if sell_put_flag == 1:
                current_entry_put_price = get_live_price(put_token, current_entry_put_price)
                if hedgeless_mode == 0:
                    exit_hedge_put_price = get_live_price(hedge_put_token, exit_hedge_put_price)

            if sell_call_flag == 1:
                current_entry_call_price = get_live_price(call_token, current_entry_call_price)
                if hedgeless_mode == 0:
                    exit_hedge_call_price = get_live_price(hedge_call_token, exit_hedge_call_price)

            # 2. Mark-to-Market PnL Calculation
            if sell_put_flag == 1:
                sell_put_profit = qty * (entry_put_price - current_entry_put_price) - commission(qty, current_entry_put_price, entry_put_price)
                if hedgeless_mode == 0:
                    sell_put_profit += qty * (exit_hedge_put_price - entry_hedge_put_price) - commission(qty, entry_hedge_put_price, exit_hedge_put_price)

            if sell_call_flag == 1:
                sell_call_profit = qty * (entry_call_price - current_entry_call_price) - commission(qty, current_entry_call_price, entry_call_price)
                if hedgeless_mode == 0:
                    sell_call_profit += qty * (exit_hedge_call_price - entry_hedge_call_price) - commission(qty, entry_hedge_call_price, exit_hedge_call_price)

            # 3. Synchronized 1-Minute Telemetry Print
            if is_minute_tick:
                last_status_print_minute = now.minute
                print(
                    f"{now.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Status | Realized P&L={final_profit:.2f} | "
                    f"PE strike={entry_put}, Entry={entry_put_price:.2f}, LTP={current_entry_put_price:.2f}, Stop={put_stop_price:.2f}, P&L={sell_put_profit:.2f} | "
                    f"CE strike={entry_call}, Entry={entry_call_price:.2f}, LTP={current_entry_call_price:.2f}, Stop={call_stop_price:.2f}, P&L={sell_call_profit:.2f}"
                )

            # 4. Exit Conditions
            # A. Global Stop Loss Check
            total_unrealized_pnl = sell_put_profit + sell_call_profit
            if (final_profit + total_unrealized_pnl) <= global_stop:
                print(f"{now.strftime('%Y-%m-%d %H:%M:%S')} GLOBAL STOP HIT: realized={final_profit:.2f}, unrealized={total_unrealized_pnl:.2f}, total={final_profit + total_unrealized_pnl:.2f}, limit={global_stop}")

                if sell_put_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_put, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_put, qty, 'SELL')
                    exit_sell_put_price = current_entry_put_price
                    final_profit += sell_put_profit
                    ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                    log_tradebook_entry(tradebook_csv, ts, entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Global_Stop_Hit')
                    print(datetime.now(IST), 'Sell_Put_closed_GlobalStop', sell_put_profit, exit_sell_put_price)

                if sell_call_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_call, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_call, qty, 'SELL')
                    exit_sell_call_price = current_entry_call_price
                    final_profit += sell_call_profit
                    ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                    log_tradebook_entry(tradebook_csv, ts, entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Global_Stop_Hit')
                    print(datetime.now(IST), 'Sell_Call_closed_GlobalStop', sell_call_profit, exit_sell_call_price)

                print('Global_Stop_reached', 'Final_profit:', final_profit)
                print('x' * 68)
                ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                log_daily_pnl_entry(daily_pnl_csv, ts, entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit)
                trim_memory()
                print_tomorrow_mode()
                return

            # B. End of Day Exit (15:19 IST)
            if now.hour == 15 and now.minute == 19:
                if sell_put_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_put, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_put, qty, 'SELL')
                    exit_sell_put_price = current_entry_put_price
                    final_profit += sell_put_profit
                    ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                    log_tradebook_entry(tradebook_csv, ts, entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Sell_Put_Closed')
                    print(datetime.now(IST), 'Sell_Put_closed', sell_put_profit, exit_sell_put_price, exit_hedge_put_price)

                if sell_call_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_call, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_call, qty, 'SELL')
                    exit_sell_call_price = current_entry_call_price
                    final_profit += sell_call_profit
                    ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                    log_tradebook_entry(tradebook_csv, ts, entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Sell_Call_Closed')
                    print(datetime.now(IST), 'Sell_Call_closed', sell_call_profit, exit_sell_call_price, exit_hedge_call_price)

                print('All_positions_closed', 'Final_profit:', final_profit)
                print('x' * 68)
                ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                log_daily_pnl_entry(daily_pnl_csv, ts, entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit)
                trim_memory()
                print_tomorrow_mode()
                return

            # C. Leg Stop Loss / Reversal Profit Target Hit
            if (sell_put_flag == 1 and current_entry_put_price >= put_stop_price) or \
               (sell_put_flag == 1 and pos_num >= 1 and sell_put_profit >= (1 + reverse_threshold_percentage) * abs(final_profit)):
                if live_mode == 1:
                    place_order(kite, sym_put, qty, 'BUY')
                    if hedgeless_mode == 0:
                        place_order(kite, sym_hedge_put, qty, 'SELL')

                exit_sell_put_price = current_entry_put_price
                final_profit += sell_put_profit
                ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                log_tradebook_entry(tradebook_csv, ts, entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Sell_Put_Stopped')
                print(datetime.now(IST), 'Sell_Put_closed', final_profit, exit_sell_put_price, exit_hedge_put_price)
                trim_memory()
                break

            if (sell_call_flag == 1 and current_entry_call_price >= call_stop_price) or \
               (sell_call_flag == 1 and pos_num >= 1 and sell_call_profit >= (1 + reverse_threshold_percentage) * abs(final_profit)):
                if live_mode == 1:
                    place_order(kite, sym_call, qty, 'BUY')
                    if hedgeless_mode == 0:
                        place_order(kite, sym_hedge_call, qty, 'SELL')

                exit_sell_call_price = current_entry_call_price
                final_profit += sell_call_profit
                ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                log_tradebook_entry(tradebook_csv, ts, entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Sell_Call_Stopped')
                print(datetime.now(IST), 'Sell_Call_closed', final_profit, exit_sell_call_price, exit_hedge_call_price)
                trim_memory()
                break

# ─────────────────────────────────────────────────────────────────────────────
# WORKER PROCESS ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────
def run_trading_process():
    print('Supertrend STart', flush=True)
    now = datetime.now(IST)
    if now.weekday() in [5, 6]:
        print("Today is Weekend. Skipping.", flush=True)
        return

    print(now, flush=True)
    with open(os.path.join(config_path, 'auth.txt'), 'r') as f:
        api_data = f.read().strip()
    api_key = api_data.split(',')[0].strip()
    access_token = api_data.split(',')[1].strip()

    # Lazy-load KiteConnect in worker
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Load compact instrument tokens
    options_map, current_expiry = load_instruments_compact(
        os.path.join(gd_path, 'instrument_tokens.csv'),
        ind,
        f"{EXCHANGE}-OPT"
    )

    sell_fn(kite, options_map, current_expiry)

# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISOR SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_process()
    else:
        worker_process = None
        print(f"Supervisor Active. Waiting for {token_swap_time} to start...", flush=True)
        print_tomorrow_mode()

        try:
            while True:
                now_str = datetime.now(IST).strftime("%H:%M")
                if now_str == token_swap_time:
                    if datetime.now(IST).weekday() in [5, 6]:
                        print("Today is Weekend. Skipping.", flush=True)
                        time.sleep(70)
                        continue

                    print(f"\n{'*'*40}", flush=True)
                    print(f"LAUNCHING DAILY TRADING WORKER: {datetime.now(IST).date()}", flush=True)
                    print(f"{'*'*40}\n", flush=True)

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
                        print(f"Worker Crash/Interruption: {e}", flush=True)

                    print("\nWorker Finished. Supervisor sleeping until tomorrow.", flush=True)
                    worker_process = None
                    time.sleep(70)

                time.sleep(30)

        except KeyboardInterrupt:
            print("\nSupervisor stopping by User Request (Ctrl+C).", flush=True)

        finally:
            if worker_process and worker_process.poll() is None:
                print("\n[SAFETY] Killing background Worker process...", flush=True)
                try:
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
                except Exception:
                    worker_process.kill()
                print("[SAFETY] Worker terminated.", flush=True)

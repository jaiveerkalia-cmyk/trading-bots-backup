import logging
import subprocess
import sys
from kiteconnect import KiteTicker
# logging.basicConfig(level=logging.DEBUG)
import threading
import time
import datetime
from collections import OrderedDict
# from datetime import time
import pandas as pd
from pprint import pprint
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from blackscholes import *
import talib
import requests
import pyotp
import pytz
from numba import jit


scrip = 'NIFTY 50'
ind = 'NIFTY'
min_delta = 0.15
sObject = slice(16)
strike_gap = 10
min_buy_price = 15
entry_strike_gap = 0
strike_difference = 50
strike_hedge_gap = 10
lot_size = 65
exit_check_interval_seconds = 1
token_swap_time = "09:00"

gd_path = '/app/data/'
results_folder = 'Nifty_sell_supertrend_results_websockets'

# ─────────────────────────────────────────────────────────────────────────────
# DAY-OF-WEEK TRADING CONFIG
# Keys: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday
# live_mode: 1 = live trading (real orders placed), 0 = paper trading
# ─────────────────────────────────────────────────────────────────────────────
DAY_CONFIG = {
    0: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:35', 'supertrend_period': 10, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0},  # Monday
    1: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:35', 'supertrend_period': 10, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0},  # Tuesday
    2: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:35', 'supertrend_period': 10, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0},  # Wednesday
    3: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:35', 'supertrend_period': 10, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0},  # Thursday
    4: {'lots_num': 10, 'live_mode': 0, 'start_time': '09:35', 'supertrend_period': 10, 'stop_percent': 0.5, 'reverse_threshold_percentage': 0.1, 'global_stop_per_lot': 40000, 'hedgeless_mode': 0},  # Friday
}

def get_day_config():
    """Returns the DAY_CONFIG entry for today (weekday 0=Mon to 4=Fri)."""
    today_weekday = datetime.now().weekday()
    return DAY_CONFIG.get(today_weekday, DAY_CONFIG[0])

def print_tomorrow_mode():
    """Prints all trading parameters for the next trading day."""
    day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday', 5: 'Saturday', 6: 'Sunday'}
    today_weekday = datetime.now().weekday()
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
    print(f"{'='*60}\n")
    
call_token, put_token,hedge_call_token,hedge_put_token,mid_loop,prev_call_token, prev_put_token,put_stop_price,call_stop_price = 0,0,0,0,0,0,0,0,0
entry_call_price,entry_put_price,entry_put,entry_call,hedge_put,hedge_call,hedge_put_price,hedge_call_price,profit,final_close = 0,0,0,0,0,0,0,0,0,0
current_call_buy,current_put_buy,total_premium,opening_underlying_price, call_open, put_open = 0,0,0,0,1,1
sell_put_flag, sell_call_flag, buy_put_flag, buy_call_flag = 0,0,0,0
tick_list = []
token_list = []
live_mode = 0
live_market_data = {}
tick_lock = threading.Lock()

def on_ticks(ws, ticks):
    with tick_lock:
        for tick in ticks:
            try:
                instrument_token = tick.get('instrument_token')
                last_price = tick.get('last_price', 0)

                if instrument_token is None:
                    continue

                if last_price is None or last_price <= 0:
                    if instrument_token not in live_market_data:
                        live_market_data[instrument_token] = 0
                    continue

                live_market_data[instrument_token] = last_price
            except Exception as e:
                print(f"WebSocket tick ignored due to processing error: {e}")

def on_connect(ws, response):
    print("WebSocket Connected Successfully.")

def get_symbol_and_token(zerodha_instruments_list, strike, instrument_type):
    option_rows = zerodha_instruments_list[zerodha_instruments_list['strike'] == int(strike)]
    row = option_rows[option_rows['tradingsymbol'].str.contains(instrument_type, na=False)].iloc[0]
    return row['tradingsymbol'], int(row['instrument_token'])

def subscribe_tokens(kws, tokens):
    tokens = [int(token) for token in tokens if token]
    if not tokens:
        return
    kws.subscribe(tokens)
    kws.set_mode(kws.MODE_LTP, tokens)
    time.sleep(0.25)

def get_live_price(instrument_token, fallback_price):
    with tick_lock:
        live_price = live_market_data.get(instrument_token, 0)
    return live_price if live_price > 0 else fallback_price

def commission(quantity, buy_price, sell_price):
    """
    Calculates total charges for NSE Equity Options based on 
    the April 1, 2026 STT hike and current NSE transaction fees.
    """
    buy_turnover = quantity * buy_price
    sell_turnover = quantity * sell_price
    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage: Standard Rs. 20 per executed order
    # Note: Zerodha may charge 40/order if your cash collateral is < 50%
    zerodha_brokerage = 20 + 20 

    # 2. STT: 0.15% on SELL side premium (Budget 2026 update)
    stt = 0.0015 * sell_turnover

    # 3. NSE Transaction Charges: 0.03503% on premium turnover
    exchange_txn_charge = 0.0003503 * total_turnover

    # 4. SEBI Charges: 10 per crore (0.000001)
    sebi_charges = 0.000001 * total_turnover

    # 5. GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    gst = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)

    # 6. Stamp Duty: 0.003% on BUY side premium only
    stamp_duty = 0.00003 * buy_turnover
    
    # 7. NSE IPFT: 0.05 per crore (0.0000005) on premium turnover
    ipft = 0.00000005 * total_turnover

    total_charges = (zerodha_brokerage + stt + exchange_txn_charge + 
                     sebi_charges + gst + stamp_duty + ipft)

    return round(total_charges, 2)

def SuperTrend(df, period, multiplier, ohlc=['open', 'high', 'low', 'close']):
    
    # Compute basic upper and lower bands
    df['basic_ub'] = (df[ohlc[1]] + df[ohlc[2]]) / 2 + multiplier * df['atr']
    df['basic_lb'] = (df[ohlc[1]] + df[ohlc[2]]) / 2 - multiplier * df['atr']

    # Compute final upper and lower bands
    df['final_ub'] = 0.00
    df['final_lb'] = 0.00
    for i in range(period, len(df)):
        df['final_ub'].iat[i] = df['basic_ub'].iat[i] if df['basic_ub'].iat[i] < df['final_ub'].iat[i - 1] or df[ohlc[3]].iat[i - 1] > df['final_ub'].iat[i - 1] else df['final_ub'].iat[i - 1]
        df['final_lb'].iat[i] = df['basic_lb'].iat[i] if df['basic_lb'].iat[i] > df['final_lb'].iat[i - 1] or df[ohlc[3]].iat[i - 1] < df['final_lb'].iat[i - 1] else df['final_lb'].iat[i - 1]
       
    # Set the Supertrend value
    df['st'] = 0.00
    for i in range(period, len(df)):
        df['st'].iat[i] = df['final_ub'].iat[i] if df['st'].iat[i - 1] == df['final_ub'].iat[i - 1] and df[ohlc[3]].iat[i] <= df['final_ub'].iat[i] else \
                        df['final_lb'].iat[i] if df['st'].iat[i - 1] == df['final_ub'].iat[i - 1] and df[ohlc[3]].iat[i] >  df['final_ub'].iat[i] else \
                        df['final_lb'].iat[i] if df['st'].iat[i - 1] == df['final_lb'].iat[i - 1] and df[ohlc[3]].iat[i] >= df['final_lb'].iat[i] else \
                        df['final_ub'].iat[i] if df['st'].iat[i - 1] == df['final_lb'].iat[i - 1] and df[ohlc[3]].iat[i] <  df['final_lb'].iat[i] else 0.00 
                 
    # Mark the trend direction up/down
    df['stx'] = np.where((df['st'] > 0.00), np.where((df[ohlc[3]] < df['st']), 'down',  'up'), np.NaN)

    # Remove basic and final bands from the columns
    df.drop(['basic_ub', 'basic_lb', 'final_ub', 'final_lb'], inplace=True, axis=1)
    
    df.fillna(0, inplace=True)

    return (df)

@jit(nopython=True)
def _calculate_supertrend_numba(high, low, close, atr, period, multiplier):
    n = len(close)
    hl2 = (high + low) / 2
    basic_ub = hl2 + multiplier * atr
    basic_lb = hl2 - multiplier * atr
    
    final_ub = np.zeros(n)
    final_lb = np.zeros(n)
    st = np.zeros(n)
    
    # Initialize
    final_ub[period-1] = basic_ub[period-1]
    final_lb[period-1] = basic_lb[period-1]
    st[period-1] = basic_ub[period-1]
    
    for i in range(period, n):
        # Final Upper Band
        if basic_ub[i] < final_ub[i-1] or close[i-1] > final_ub[i-1]:
            final_ub[i] = basic_ub[i]
        else:
            final_ub[i] = final_ub[i-1]
        
        # Final Lower Band
        if basic_lb[i] > final_lb[i-1] or close[i-1] < final_lb[i-1]:
            final_lb[i] = basic_lb[i]
        else:
            final_lb[i] = final_lb[i-1]
        
        # Supertrend Value
        if st[i-1] == final_ub[i-1] and close[i] <= final_ub[i]:
            st[i] = final_ub[i]
        elif st[i-1] == final_ub[i-1] and close[i] > final_ub[i]:
            st[i] = final_lb[i]
        elif st[i-1] == final_lb[i-1] and close[i] >= final_lb[i]:
            st[i] = final_lb[i]
        elif st[i-1] == final_lb[i-1] and close[i] < final_lb[i]:
            st[i] = final_ub[i]
        else:
            st[i] = 0
    
    # Direction
    direction = np.zeros(n, dtype=np.int8)
    for i in range(period, n):
        if st[i] > 0:
            direction[i] = 1 if close[i] > st[i] else -1
    
    return st, direction

def calculate_supertrend_numba(df, atr_period, multiplier):
    st, dir = _calculate_supertrend_numba(
        df['high'].values,
        df['low'].values,
        df['close'].values,
        df['atr'].values,
        atr_period,
        multiplier
    )
    df['supertrend'] = st
    df['direction'] = dir
    return df

def get_best_atm_strike(kite, atm_strike, zerodha_instruments_list):
    """
    Finds the strike with the lowest premium difference (Straddle Delta ~ 0).
    Optimized for speed (batch fetching) and reliability (retries).
    """
    start = time.time()
    
    symbols_to_fetch = []
    strike_symbol_map = {} 

    for x1 in range(-3, 5):
        test_strike = int(atm_strike) + (x1 * 50)
        
        try:
            sym_put = next(item for item in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(test_strike)
            ]['tradingsymbol'].values if 'PE' in item)
            
            sym_call = next(item for item in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(test_strike)
            ]['tradingsymbol'].values if 'CE' in item)
            
            symbols_to_fetch.append('NFO:' + sym_put)
            symbols_to_fetch.append('NFO:' + sym_call)
            
            strike_symbol_map[test_strike] = {'PE': 'NFO:' + sym_put, 'CE': 'NFO:' + sym_call}
            
        except StopIteration:
            continue
        except Exception as e:
            print(f"Skipping strike {test_strike} due to data error: {e}")
            continue

    if not symbols_to_fetch:
        print("CRITICAL: No matching symbols found in instrument list. Returning input ATM.")
        return atm_strike

    all_ltp = {}
    success = False

    for attempt in range(20):
        try:
            all_ltp = kite.ltp(symbols_to_fetch)
            success = True
            break
        except Exception as e:
            print(f"API Attempt {attempt+1}/20 failed: {e}")
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

    print('It took {0:0.4f} seconds'.format(time.time() - start))
    print(f'Selected Best ATM: {new_atm_strike} (Diff: {org_price_difference:.2f})\n')
    
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
            order_id = kite.place_order(tradingsymbol=sym,
                                        exchange='NFO',
                                        transaction_type=order_side,
                                        quantity=qty,
                                        order_type=kite.ORDER_TYPE_LIMIT,
                                        price=limit_price,
                                        variety=kite.VARIETY_REGULAR,
                                        product=kite.PRODUCT_MIS)
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
                    
                    kite.modify_order(variety=kite.VARIETY_REGULAR,
                                      order_id=order_id,
                                      order_type=kite.ORDER_TYPE_LIMIT,
                                      price=new_limit_price)
                else:
                    print("Could not fetch new LTP for modification. Retrying status check...")
                    
        except Exception as e:
            print('Order_modification_error:', e)
            
        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0
            
    if mod_count >= max_modifications:
         print(f"Warning: Max modifications ({max_modifications}) reached. Order may still be pending.")



def sell_fn(kite, zerodha_instruments_list, expiry, api_key, access_token):
    global total_premium,put_stop_price,call_stop_price,opening_underlying_price, call_open, put_open,option_df,call_token,token,put_token,hedge_call_token,hedge_put_token,current_call_buy,current_put_buy,entry_call_price,entry_put_price,entry_put,entry_call,hedge_put,hedge_call,hedge_put_price,hedge_call_price
    sell_put_flag, sell_call_flag, buy_put_flag, buy_call_flag = 0,0,0,0
    current_profit, final_profit = 0,0
    sell_put_profit, sell_call_profit, buy_put_profit, buy_call_profit = 0,0,0,0
    initial_entry_put_price, initial_entry_call_price, initial_buy_put_price, initial_buy_call_price, current_entry_put_price, current_entry_call_price, current_buy_put_price, current_buy_call_price, entry_put_price, entry_call_price, buy_put_price, buy_call_price = 0,0,0,0,0,0,0,0,0,0,0,0
    exit_sell_call_price, exit_sell_put_price, exit_buy_call_price, exit_buy_put_price = 0,0,0,0
    entry_hedge_put_price, entry_hedge_call_price = 0,0

    # ── Load today's config from DAY_CONFIG ──────────────────────────────────
    cfg = get_day_config()
    lots_num                     = cfg['lots_num']
    live_mode                    = cfg['live_mode']
    start_time                   = cfg['start_time']
    supertrend_period            = cfg['supertrend_period']
    stop_percent                 = cfg['stop_percent']
    reverse_threshold_percentage = cfg['reverse_threshold_percentage']
    global_stop_per_lot          = cfg['global_stop_per_lot']
    hedgeless_mode               = cfg['hedgeless_mode']

    day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday'}
    today_name = day_names.get(datetime.now().weekday(), 'Unknown')
    mode_str = 'LIVE TRADING' if live_mode == 1 else 'PAPER TRADING'
    hedgeless_str = 'YES (no hedges)' if hedgeless_mode == 1 else 'NO (hedges active)'
    print(f"\n{'='*60}")
    print(f"  TODAY ({today_name}) CONFIG LOADED:")
    print(f"  Mode={mode_str} | lots_num={lots_num} | start_time={start_time} | "
          f"supertrend_period={supertrend_period} | stop_percent={stop_percent} | "
          f"reverse_threshold_percentage={reverse_threshold_percentage} | "
          f"global_stop_per_lot={global_stop_per_lot} | hedgeless_mode={hedgeless_str}")
    print(f"{'='*60}\n")
    # ─────────────────────────────────────────────────────────────────────────

    intraday_positions = pd.read_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv')
    final_position_df = pd.read_csv(gd_path + results_folder + '/Final_daily_pnl.csv')

    kws = KiteTicker(api_key, access_token)
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    ws_thread = threading.Thread(target=kws.connect, kwargs={'threaded': True})
    ws_thread.daemon = True
    ws_thread.start()

    lots = math.floor(1*lots_num)
    qty = lots*lot_size
    global_stop = -abs(global_stop_per_lot * lots)

    print('Qty:', qty)
    print(f'Global Stop: {global_stop}')
    loss_target = 4500 * lots

    #####GET TOKEN
    now = datetime.now()
    configured_start_time = datetime.strptime(start_time, '%H:%M').time()
    start_datetime = now.replace(hour=configured_start_time.hour, minute=configured_start_time.minute, second=0, microsecond=0)
    sleep_seconds = (start_datetime - now).total_seconds()
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    print(datetime.now())

    ##### GET 5m DATAFRAME AT 09:35

    print('check_here')

    # --- CONFIGURATION VARIABLES ---
    INSTRUMENT_TOKEN = 256265
    DAYS_TO_FETCH = 10
    # -------------------------------
    target_candle_config = datetime.strptime(start_time, '%H:%M') - timedelta(minutes=5)

    ed = datetime.now()
    sd = ed - timedelta(days=DAYS_TO_FETCH)

    while True:
        try:
            a = kite.historical_data(INSTRUMENT_TOKEN, sd, ed, '5minute')

            if not a:
                print("Empty response, retrying...")
                time.sleep(2)
                continue

            latest_fetched_time = pd.to_datetime(a[-1]['date'])

            if latest_fetched_time.tzinfo is None:
                latest_fetched_time = latest_fetched_time.tz_localize('Asia/Kolkata')
            else:
                latest_fetched_time = latest_fetched_time.tz_convert('Asia/Kolkata')

            target_candle_time = latest_fetched_time.replace(
                hour=target_candle_config.hour,
                minute=target_candle_config.minute,
                second=0,
                microsecond=0
            )

            if latest_fetched_time >= target_candle_time:
                print(f"Target {target_candle_config.strftime('%H:%M')} candle verified! Latest fetched: {latest_fetched_time}")
                break
            else:
                print(f"Target candle missing. Latest is {latest_fetched_time}. Waiting...")
                time.sleep(2)

        except Exception as e:
            print(f"Error occurred: {str(e)}")
            time.sleep(2)

    df_ohlc = pd.DataFrame(a)
    df_ohlc['date'] = pd.to_datetime(df_ohlc['date'])

    if df_ohlc['date'].dt.tz is None:
        df_ohlc['date'] = df_ohlc['date'].dt.tz_localize('Asia/Kolkata')
    else:
        df_ohlc['date'] = df_ohlc['date'].dt.tz_convert('Asia/Kolkata')

    df_ohlc = df_ohlc[df_ohlc['date'] <= target_candle_time]
    df_ohlc.reset_index(drop=True, inplace=True)
    print(df_ohlc.tail())

##    df_ohlc['atr'] =  talib.ATR(df_ohlc.high, df_ohlc.low, df_ohlc.close, timeperiod= 10)

    df_ohlc['prev_close'] = df_ohlc['close'].shift(1)
    df_ohlc['tr1'] = df_ohlc['high'] - df_ohlc['low']
    df_ohlc['tr2'] = abs(df_ohlc['high'] - df_ohlc['prev_close'])
    df_ohlc['tr3'] = abs(df_ohlc['low'] - df_ohlc['prev_close'])
    df_ohlc['tr'] = df_ohlc[['tr1', 'tr2', 'tr3']].max(axis=1)

    atr_period = supertrend_period
    df_ohlc['atr'] = df_ohlc['tr'].rolling(atr_period).mean()
    df_ohlc.drop(['prev_close', 'tr1', 'tr2', 'tr3', 'tr'], axis=1, inplace=True)
    df_ohlc = calculate_supertrend_numba(df_ohlc, supertrend_period, 4)

    print(df_ohlc)
    try:
        df_ohlc.to_csv('NIFTYST.csv', index=False)
    except Exception as e:
        pass

    if df_ohlc.iloc[-1].direction == -1:
        sell_call_flag = 1
        sell_put_flag = 0
    if df_ohlc.iloc[-1].direction == 1:
        sell_put_flag = 1
        sell_call_flag = 0
    print(sell_call_flag, sell_put_flag)

    ###### GET ENTRIES AND START CHECKING THE POSITIONS ####
    ########################################################

    current_pos_profit = 0

    for pos_num in range(0, 4):

        if pos_num > 2 or (pos_num > 1 and final_profit > 0):
            final_position_df.loc[final_position_df.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit]
            final_position_df.to_csv(gd_path + results_folder + '/Final_daily_pnl.csv', index=False)
            print('day_over')
            print_tomorrow_mode()
            return()

        #### REVERSE THE SIGNAL FOR SECOND POSITION
        if pos_num >= 1:
            sell_call_flag, sell_put_flag = sell_put_flag, sell_call_flag  # Swap values
            entry_strike_gap = 2
        else: entry_strike_gap = 0

        ###GET NIFTY CURRENT PRICE
        while True:
            try:
                current_price = kite.ltp('NSE:'+ scrip)['NSE:'+ scrip]['last_price']
                break
            except Exception as e:
                time.sleep(1.5)
        pprint(current_price)
        atm_strike = int(round(current_price / strike_difference) * strike_difference)
        print('ATM Strike', atm_strike, 'Underlying:', current_price)

        ###### ADVANCED ATM FINDER #####
        atm_strike = get_best_atm_strike(kite, atm_strike, zerodha_instruments_list)
        ################################

        print(sell_call_flag, sell_put_flag)

        ###################### GET ENTRY SELL PUTS AND BUY PUTS ################################

        if sell_put_flag == 1:
        #####GET INITIAL ENTRY PUT PRICE
            entry_put = atm_strike + (entry_strike_gap * strike_difference)
            hedge_put = entry_put - strike_hedge_gap * strike_difference

            sym_put, put_token = get_symbol_and_token(zerodha_instruments_list, entry_put, 'PE')
            sym_hedge_put, hedge_put_token = get_symbol_and_token(zerodha_instruments_list, hedge_put, 'PE')
            subscribe_tokens(kws, [put_token, hedge_put_token])
            print(sym_put, sym_hedge_put)

            if live_mode == 1:
                ####PLACE BUY HEDGE ORDER
                if hedgeless_mode == 0:
                    place_order(kite, sym_hedge_put, qty, 'BUY')
                ####PLACE SELL ACTUAL ORDER
                place_order(kite, sym_put, qty, 'SELL')

            #####GET ENTRY PUT PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_put)
                    entry_put_price = (a1['NFO:'+sym_put]['last_price'])
                    break
                except Exception as e:
                    print(e)
                    time.sleep(1)

            #####GET ENTRY HEDGE PUT PRICE
            if hedgeless_mode == 0:
                for _ in range(10):
                    try:
                        a1 = kite.ltp('NFO:'+sym_hedge_put)
                        entry_hedge_put_price = (a1['NFO:'+sym_hedge_put]['last_price'])
                        break
                    except Exception as e:
                        print(e)

            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', entry_put_price, 0, 0, 0, 'Open - Sell_Put_Opened']
            intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
            current_entry_put_price = entry_put_price
            exit_hedge_put_price = entry_hedge_put_price

            print(f"{datetime.now()} - Put Entry: {entry_put}, Hedge: {hedge_put} | Entry Price: {entry_put_price}, Hedge Price: {entry_hedge_put_price}")

        if sell_call_flag == 1:
        #####GET INITIAL ENTRY CALL PRICE
            entry_call = atm_strike - (entry_strike_gap * strike_difference)
            hedge_call = entry_call + strike_hedge_gap * strike_difference

            sym_call, call_token = get_symbol_and_token(zerodha_instruments_list, entry_call, 'CE')
            sym_hedge_call, hedge_call_token = get_symbol_and_token(zerodha_instruments_list, hedge_call, 'CE')
            subscribe_tokens(kws, [call_token, hedge_call_token])
            print(sym_call, sym_hedge_call)

            if live_mode == 1:
                ####PLACE BUY HEDGE ORDER
                if hedgeless_mode == 0:
                    place_order(kite, sym_hedge_call, qty, 'BUY')
                ####PLACE SELL ACTUAL ORDER
                place_order(kite, sym_call, qty, 'SELL')

            #####GET ENTRY CALL PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_call)
                    entry_call_price = (a1['NFO:'+sym_call]['last_price'])
                    break
                except Exception as e:
                    time.sleep(1)

            #####GET ENTRY HEDGE CALL PRICE
            if hedgeless_mode == 0:
                for _ in range(10):
                    try:
                        a1 = kite.ltp('NFO:'+sym_hedge_call)
                        entry_hedge_call_price = (a1['NFO:'+sym_hedge_call]['last_price'])
                        break
                    except Exception as e:
                        print(e)

            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', entry_call_price, 0, 0, 0, 'Open - Sell_Call_Opened']
            intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
            current_entry_call_price = entry_call_price
            exit_hedge_call_price = entry_hedge_call_price

            print(f"{datetime.now()} - Call Entry: {entry_call}, Hedge: {hedge_call} | Entry Price: {entry_call_price}, Hedge Price: {entry_hedge_call_price}")

        ######### START CHECKING THE ENTRY CONDITIONS
        counter, sell_put_profit, sell_call_profit = 0, 0, 0
        last_status_print_minute = -1

        while True:
            time.sleep(exit_check_interval_seconds)
            counter += 1

            ####### GET CURRENT PRICES FOR ALL THE SYMBOLS
            if sell_put_flag == 1:
                current_entry_put_price = get_live_price(put_token, current_entry_put_price)
                if hedgeless_mode == 0:
                    exit_hedge_put_price = get_live_price(hedge_put_token, exit_hedge_put_price)

            if sell_call_flag == 1:
                current_entry_call_price = get_live_price(call_token, current_entry_call_price)
                if hedgeless_mode == 0:
                    exit_hedge_call_price = get_live_price(hedge_call_token, exit_hedge_call_price)

            ###################### CHECK PROFIT AND EXITS ################################

            ######### CALCULATE CURRENT PROFIT
            if sell_put_flag == 1:
                sell_put_profit = qty*(entry_put_price - current_entry_put_price) - commission(qty, current_entry_put_price, entry_put_price)
                if hedgeless_mode == 0:
                    sell_put_profit = sell_put_profit + qty*(exit_hedge_put_price-entry_hedge_put_price) - commission(qty, entry_hedge_put_price, exit_hedge_put_price)

            if sell_call_flag == 1:
                sell_call_profit = qty*(entry_call_price - current_entry_call_price) - commission(qty, current_entry_call_price, entry_call_price)
                if hedgeless_mode == 0:
                    sell_call_profit = sell_call_profit + qty*(exit_hedge_call_price-entry_hedge_call_price) - commission(qty, entry_hedge_call_price, exit_hedge_call_price)

            put_stop_price = (1 + stop_percent) * entry_put_price if sell_put_flag == 1 else 0
            call_stop_price = (1 + stop_percent) * entry_call_price if sell_call_flag == 1 else 0
            current_status_minute = datetime.now().minute
            if current_status_minute != last_status_print_minute:
                print(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Status | Realized P&L={final_profit:.2f} | "
                    f"PE strike={entry_put}, Entry={entry_put_price:.2f}, LTP={current_entry_put_price:.2f}, Stop={put_stop_price:.2f}, P&L={sell_put_profit:.2f} | "
                    f"CE strike={entry_call}, Entry={entry_call_price:.2f}, LTP={current_entry_call_price:.2f}, Stop={call_stop_price:.2f}, P&L={sell_call_profit:.2f}"
                )
                last_status_print_minute = current_status_minute

            ########################## GLOBAL STOP LOSS CHECK ############################
            total_unrealized_pnl = sell_put_profit + sell_call_profit
            if (final_profit + total_unrealized_pnl) <= global_stop:
                print(f"{datetime.now()} GLOBAL STOP HIT: realized={final_profit:.2f}, unrealized={total_unrealized_pnl:.2f}, total={final_profit+total_unrealized_pnl:.2f}, limit={global_stop}")

                if sell_put_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_put, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_put, qty, 'SELL')
                    exit_sell_put_price = current_entry_put_price
                    final_profit = final_profit + sell_put_profit
                    intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Global_Stop_Hit']
                    intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                    print(datetime.now(), 'Sell_Put_closed_GlobalStop', sell_put_profit, exit_sell_put_price)

                if sell_call_flag == 1:
                    if live_mode == 1:
                        place_order(kite, sym_call, qty, 'BUY')
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_call, qty, 'SELL')
                    exit_sell_call_price = current_entry_call_price
                    final_profit = final_profit + sell_call_profit
                    intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Global_Stop_Hit']
                    intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                    print(datetime.now(), 'Sell_Call_closed_GlobalStop', sell_call_profit, exit_sell_call_price)

                print('Global_Stop_reached', 'Final_profit:', final_profit)
                print('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
                final_position_df.loc[final_position_df.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit]
                final_position_df.to_csv(gd_path + results_folder + '/Final_daily_pnl.csv', index=False)
                print_tomorrow_mode()
                return()
            ###############################################################################

            ########################## EXIT AT THE END OF DAY #############################
            if (datetime.now().hour == 15 and datetime.now().minute == 19):

                if sell_put_flag == 1:
                    if live_mode == 1:
                        #####CLOSE THE SELL ORDER
                        place_order(kite, sym_put, qty, 'BUY')
                        ####CLOSE THE HEDGE ORDER
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_put, qty, 'SELL')

                    exit_sell_put_price = current_entry_put_price
                    final_profit = final_profit + sell_put_profit
                    intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Sell_Put_Closed']
                    intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                    print(datetime.now(), 'Sell_Put_closed', sell_put_profit, exit_sell_put_price, exit_hedge_put_price)

                if sell_call_flag == 1:
                    if live_mode == 1:
                        #####CLOSE THE SELL ORDER
                        place_order(kite, sym_call, qty, 'BUY')
                        ####CLOSE THE HEDGE ORDER
                        if hedgeless_mode == 0:
                            place_order(kite, sym_hedge_call, qty, 'SELL')

                    exit_sell_call_price = current_entry_call_price
                    final_profit = final_profit + sell_call_profit
                    intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Sell_Call_Closed']
                    intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                    print(datetime.now(), 'Sell_Call_closed', sell_call_profit, exit_sell_call_price, exit_hedge_call_price)

                print('All_positions_closed', 'Final_profit:', final_profit)
                print('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
                final_position_df.loc[final_position_df.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, entry_put, entry_call_price, entry_put_price, exit_sell_call_price, exit_sell_put_price, final_profit]
                final_position_df.to_csv(gd_path + results_folder + '/Final_daily_pnl.csv', index=False)
                print_tomorrow_mode()
                return()

            ################################### EXIT IF STOP LOSS / TARGET IS HIT ############################
            if (sell_put_flag == 1 and current_entry_put_price >= (1 + stop_percent)*entry_put_price) or (sell_put_flag == 1 and pos_num >= 1 and sell_put_profit >= (1 + reverse_threshold_percentage)*abs(final_profit)):

                if live_mode == 1:
                    #####CLOSE THE SELL ORDER
                    place_order(kite, sym_put, qty, 'BUY')
                    ####CLOSE THE HEDGE ORDER
                    if hedgeless_mode == 0:
                        place_order(kite, sym_hedge_put, qty, 'SELL')

                exit_sell_put_price = current_entry_put_price
                final_profit = final_profit + sell_put_profit
                intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', entry_put_price, exit_sell_put_price, sell_put_profit, final_profit, 'Close - Sell_Put_Stopped']
                intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                print(datetime.now(), 'Sell_Put_closed', final_profit, exit_sell_put_price, exit_hedge_put_price)
                break

            if (sell_call_flag == 1 and current_entry_call_price >= (1 + stop_percent)*entry_call_price) or (sell_call_flag == 1 and pos_num >= 1 and sell_call_profit >= (1 + reverse_threshold_percentage)*abs(final_profit)):

                if live_mode == 1:
                    #####CLOSE THE SELL ORDER
                    place_order(kite, sym_call, qty, 'BUY')
                    ####CLOSE THE HEDGE ORDER
                    if hedgeless_mode == 0:
                        place_order(kite, sym_hedge_call, qty, 'SELL')

                exit_sell_call_price = current_entry_call_price
                final_profit = final_profit + sell_call_profit
                intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', entry_call_price, exit_sell_call_price, sell_call_profit, final_profit, 'Close - Sell_Call_Stopped']
                intraday_positions.to_csv(gd_path + results_folder + '/Intraday_options_tradebook.csv', index=False)
                print(datetime.now(), 'Sell_Call_closed', final_profit, exit_sell_call_price, exit_hedge_call_price)
                break
            
def run_trading_process():
    print('Supertrend STart', flush=True)
    now = datetime.now()
    current_day = now.strftime('%w')

    if current_day == '0' or current_day == '6':
        print("Today is Weekend. Skipping.", flush=True)
        return

    print(now, flush=True)
    with open('/app/config/'+'auth.txt', 'r') as f:
        api_data = f.read()
    api_key = api_data.split(',')[0]
    access_token = api_data.split(',')[1]
    kite = KiteConnect(api_key = api_key)
    kite.set_access_token(access_token)
    
    ###########PICK UP THE ZERODHA INSTRUMENT LIST#########
    zerodha_instruments_list = pd.read_csv('/app/data/instrument_tokens.csv')
    zerodha_instruments_list = zerodha_instruments_list[(zerodha_instruments_list['name'] == ind) & (zerodha_instruments_list['segment'] == 'NFO-OPT')].reset_index(drop=True)
    zerodha_instruments_list = zerodha_instruments_list[zerodha_instruments_list['expiry'] == zerodha_instruments_list['expiry'].iloc[0]]
    expiry = zerodha_instruments_list['expiry'].iloc[0]
    sell_fn(kite, zerodha_instruments_list,expiry, api_key, access_token)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_process()
    else:
        worker_process = None
        print(f"Supervisor Active. Waiting for {token_swap_time} to start...", flush=True)
        print_tomorrow_mode()
        
        try:
            while True:
                if datetime.now().strftime("%H:%M") == token_swap_time:
                    if datetime.now().weekday() in [5, 6]:
                        print("Today is Weekend. Skipping.", flush=True)
                        time.sleep(70)
                        continue

                    print(f"\n{'*'*40}", flush=True)
                    print(f"LAUNCHING DAILY TRADING WORKER: {datetime.now().date()}", flush=True)
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

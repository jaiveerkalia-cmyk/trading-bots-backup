import sys
import subprocess
import time
import math
import pandas as pd
from datetime import datetime
import pytz
import talib
from kiteconnect import KiteConnect

# ==============================================================================
# 1. GLOBAL PATHS & CONFIGURATION
# ==============================================================================

# UPDATE THIS PATH TO YOUR EXACT FOLDER
if sys.platform.startswith('linux'):
    GD_PATH = '/app/data/'
else:
    GD_PATH = 'G:/My Drive/eclipse/eclipse-workspace/Back_testing_engine/Custom_Backtesting/Zerodha_Scripts/Option strategies/'

print(f"Using GD_PATH: {GD_PATH}", flush=True)

AUTH_FILE_PATH = '/app/config/' + 'auth.txt'
INSTRUMENT_FILE_PATH = GD_PATH + 'instrument_tokens.csv'
TRADEBOOK_CSV_PATH = GD_PATH + 'nifty_buy_sell_results/Intraday_options_tradebook.csv'
DAILY_PNL_CSV_PATH = GD_PATH + 'nifty_buy_sell_results/Final_daily_pnl.csv'

# Strategy Constants
SYMBOL_NAME = 'NIFTY 50'      # Name of the index in NSE
INDEX_FILTER = 'NIFTY'        # Name in Instrument file
STRIKE_STEP = 50              # Nifty strike difference
TOKEN_SWAP_TIME = "09:00"     # Time to restart process daily
STANDARD_LOT_SIZE = 65        # Nifty Lot Size (Jan 2026 Standard)
ATR_PERIOD = 10
INDEX_ENTRY_ATR_PERIOD = 14
VIX_SYMBOL = 'NSE:INDIA VIX'
TRADING_LOOP_CHECK_INTERVAL_MINUTES = 1

# TARGETS (Per Lot)
TARGET_PROFIT_PER_LOT = 45000 
MAX_LOSS_PER_LOT = 3000
# While skip_till_hour is active, close for the day if net PnL reaches this multiple of GLOBAL_MAX_LOSS. Set 0 to disable.
SKIP_CHECKING_TILL_HOUR_LOSS_BYPASS = 2
# Once one leg is closed, close the other leg when total profit crosses this value.
POST_LEG_CLOSE_PROFIT_TARGET = 100
# PREMIUM THRESHOLDS (For Total_premium_skip)
LOWER_THRESHOLD = 250  # Example value, adjust as needed
UPPER_THRESHOLD = 270  # Example value, adjust as needed

# ------------------------------------------------------------------------------
# INDEX CONSTANTS (Exchange & Instrument Details)
# ------------------------------------------------------------------------------
INDEX_DETAILS = {
    'NIFTY': {
        'index_name': 'NIFTY 50', 
        'spot_exchange': 'NSE', 
        'opt_exchange': 'NFO', 
        'strike_step': 50, 
        'std_lot_size': 65, 
        'index_filter': 'NIFTY'
    },
    'SENSEX': {
        'index_name': 'SENSEX', 
        'spot_exchange': 'BSE', 
        'opt_exchange': 'BFO', 
        'strike_step': 100, 
        'std_lot_size': 20, 
        'index_filter': 'SENSEX'
    }
}

# ------------------------------------------------------------------------------
# DAY CONFIGURATION (0=Monday, ... 6=Sunday)
# NOTE:
# - When `vix_stop_mode_on` is enabled, it overrides the normal per-day
#   `max_loss_per_lot`.
# - When `atr_mode_on` is enabled, it affects the entry-gap and max-loss calculations.
# - Set `hedgeless_mode` to True to sell entry strikes without buying hedges.
# ------------------------------------------------------------------------------
DAY_CONFIGURATION = {
    # Monday (NIFTY)
    0: {'target_index': 'NIFTY', 'start': '10:20', 'exit': '14:45', 'entry_gap': 5, 'strike_gap': 0, 'lots': 8, 'live_mode': 0, 'percent_mode': 1, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
        'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': False, 'hedgeless_mode': False,
        'index_based_entry': False, 'atr_entry_multiplier': 2},

    # Tuesday (NIFTY)
    1: {'target_index': 'NIFTY', 'start': '09:20', 'exit': '15:19', 'entry_gap': 5, 'strike_gap': 2, 'lots': 3, 'live_mode': 1, 'percent_mode': 1, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': True, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
        'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': True, 'hedgeless_mode': True,
        'index_based_entry': False, 'atr_entry_multiplier': 2},

    # Wednesday (NIFTY)
    2: {'target_index': 'NIFTY', 'start': '10:45', 'exit': '14:45', 'entry_gap': 8, 'strike_gap': 0, 'lots': 8, 'live_mode': 0, 'percent_mode': 0, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
        'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': False, 'hedgeless_mode': False,
        'index_based_entry': True, 'atr_entry_multiplier': 2},

    # Thursday (SENSEX)
    3: {'target_index': 'SENSEX', 'start': '09:16', 'exit': '15:19', 'entry_gap': 10, 'strike_gap': 2, 'lots': 3, 'live_mode': 1, 'percent_mode': 1, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': True, 'atr_ema_window': 20,
        'atr_stop_per_lot': 10, 'atr_entry_gap': 20, 'skip_till_hour': 10, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': False, 'hedgeless_mode': True,
        'index_based_entry': True, 'atr_entry_multiplier': 2},

   # Friday (NIFTY)
   4: {'target_index': 'NIFTY', 'start': '10:00', 'exit': '14:45', 'entry_gap': 5, 'strike_gap': 0, 'lots': 6, 'live_mode': 0, 'percent_mode': 0, 'find_atm': True,
       'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
       'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
       'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 30, 'vix_stop_mode_on': False, 'hedgeless_mode': False,
       'index_based_entry': True, 'atr_entry_multiplier': 2},

    # Saturday/Sunday (Defaults)
    5: {'target_index': 'NIFTY', 'start': '09:20', 'exit': '15:19', 'entry_gap': 10, 'strike_gap': 0, 'lots': 7, 'live_mode': 0, 'percent_mode': 1, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
        'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': False,
        'index_based_entry': False, 'atr_entry_multiplier': 1},
    6: {'target_index': 'NIFTY', 'start': '09:20', 'exit': '15:19', 'entry_gap': 10, 'strike_gap': 0, 'lots': 7, 'live_mode': 0, 'percent_mode': 1, 'find_atm': True,
        'total_premium_skip': False, 'buy_strikes_flag': False, 'total_profit_change': False, 'atr_mode_on': False, 'atr_ema_window': 20,
        'atr_stop_per_lot': 30, 'atr_entry_gap': 10, 'skip_till_hour': 8, 'target_profit_per_lot': TARGET_PROFIT_PER_LOT,
        'max_loss_per_lot': MAX_LOSS_PER_LOT, 'stop_percent': 50, 'vix_stop_mode_on': False,
        'index_based_entry': False, 'atr_entry_multiplier': 1}
}

DEFAULT_CONFIGURATION = DAY_CONFIGURATION[0]

# ==============================================================================
# 2. SHARED DATA & HELPER FUNCTIONS
# ==============================================================================

IST = pytz.timezone('Asia/Kolkata')


def get_current_day_config():
    """Returns the configuration dictionary for the current day of the week."""
    day_index = datetime.now().weekday()
    return DAY_CONFIGURATION.get(day_index, DEFAULT_CONFIGURATION)

def get_day_config_for_day_index(day_index):
    """Returns the configuration dictionary for a specific day index."""
    return DAY_CONFIGURATION.get(day_index, DEFAULT_CONFIGURATION)

def get_next_trading_day_info():
    """Returns the next trading date and its day configuration, skipping weekends."""
    next_dt = datetime.now() + pd.Timedelta(days=1)
    while next_dt.weekday() in [5, 6]:
        next_dt += pd.Timedelta(days=1)
    return next_dt, get_day_config_for_day_index(next_dt.weekday())

def get_now_str():
    """Returns clean timestamp string 'YYYY-MM-DD HH:MM:SS'"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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

def get_quote_price(kite, symbol, side, exchange='NFO'):
    """Fetches the Bid/Ask price from the market quote with retries."""
    quote_key = f"{exchange}:{symbol}"
    for attempt in range(50):
        try:
            quote = kite.quote(quote_key)
            if quote_key in quote:
                market_depth = quote[quote_key]['depth']
                if side == 'BUY':
                    return float(market_depth['sell'][0]['price']) # Ask
                else:
                    return float(market_depth['buy'][0]['price'])  # Bid
        except Exception as e:
            print(f"Quote Fetch Failed ({attempt+1}/50): {e}", flush=True)
            time.sleep(1)
    return 0.0

def place_order_with_retry(kite, symbol, qty, side, live_mode, exchange='NFO'):
    """Places and manages an order with retries and price chasing."""
    if live_mode == 0:
        print(f"[PAPER TRADE] Placed {side} order for {symbol} on {exchange} | Qty: {qty}", flush=True)
        return True

    transaction_type = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL
    ltp_sym = f"{exchange}:{symbol}"

    def get_ltp():
        ltp_sleep = 0.5
        for attempt in range(10):
            try:
                resp = kite.ltp(ltp_sym)
                ltp = resp.get(ltp_sym, {}).get('last_price', 0)
                if ltp > 0:
                    return ltp
            except Exception as e:
                print(f"LTP fetch error attempt {attempt + 1}: {e}", flush=True)

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
        print('order_placement_failed: Could not fetch initial LTP', flush=True)
        return False

    limit_price = get_limit_price(current_ltp)

    order_id = None
    place_retries = 0
    place_sleep = 1.0

    while place_retries < 10:
        try:
            order_id = kite.place_order(tradingsymbol=symbol,
                                        exchange=exchange,
                                        transaction_type=transaction_type,
                                        quantity=qty,
                                        order_type=kite.ORDER_TYPE_LIMIT,
                                        price=limit_price,
                                        variety=kite.VARIETY_REGULAR,
                                        product=kite.PRODUCT_MIS)
            print(f"[LIVE ORDER] Submitted {side} {symbol} @ {limit_price}", flush=True)
            break
        except Exception as e:
            place_retries += 1
            print(f"Entry_order_error attempt {place_retries}: {e}", flush=True)
            if place_retries == 10:
                print('order_placement_failed', flush=True)
                return False

            time.sleep(place_sleep)
            if place_retries >= 3:
                place_sleep += 1.0

    if not order_id:
        return False

    max_modifications = 10
    mod_count = 0
    mod_sleep = 1.0

    while mod_count < max_modifications:
        time.sleep(mod_sleep)

        try:
            order_history = kite.order_history(order_id)
            latest_state = order_history[-1]
            status = latest_state.get('status')
            pending_qty = latest_state.get('pending_quantity', 0)

            if status == 'COMPLETE':
                print(f"[LIVE ORDER] Complete: {side} {symbol}", flush=True)
                return True

            if status in ['REJECTED', 'CANCELLED']:
                print(f"Order {status}. Reason: {latest_state.get('status_message', 'Unknown')}", flush=True)
                return False

            if pending_qty > 0:
                print(f"Partial/No Fill. Pending Qty: {pending_qty}. Fetching latest LTP to modify...", flush=True)

                new_ltp = get_ltp()
                if new_ltp is not None:
                    new_limit_price = get_limit_price(new_ltp)
                    kite.modify_order(variety=kite.VARIETY_REGULAR,
                                      order_id=order_id,
                                      order_type=kite.ORDER_TYPE_LIMIT,
                                      price=new_limit_price)
                    print(f"Modified order {order_id} to {new_limit_price}", flush=True)
                else:
                    print("Could not fetch new LTP for modification. Retrying status check...", flush=True)

        except Exception as e:
            print(f"Order_modification_error: {e}", flush=True)

        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0

    print(f"Warning: Max modifications ({max_modifications}) reached. Order may still be pending.", flush=True)
    return False

def get_ltp_safe(kite, symbol_list):
    """Fetches LTP with retries."""
    for _ in range(5):
        try:
            return kite.ltp(symbol_list)
        except: time.sleep(1)
    return {}

def wait_for_next_check_interval(interval_minutes):
    """Sleeps until the next interval boundary, aligned to minute starts."""
    interval_minutes = max(1, int(interval_minutes))

    while True:
        now = datetime.now()
        secs_since_hour = (now.minute * 60) + now.second
        interval_seconds = interval_minutes * 60
        sleep_seconds = interval_seconds - (secs_since_hour % interval_seconds)

        if sleep_seconds <= 0:
            sleep_seconds = interval_seconds

        time.sleep(sleep_seconds)
        check_dt = datetime.now()
        if check_dt.second == 0 and (check_dt.minute % interval_minutes) == 0:
            return check_dt

def get_latest_atr_ema(kite, spot_symbol, current_dt, atr_ema_window=20, lookback_days=60):
    """Fetches 1h candles, calculates ATR and ATR EMA, and returns the latest completed ATR EMA."""
    try:
        quote = kite.quote(spot_symbol)
        instrument_token = quote.get(spot_symbol, {}).get('instrument_token')
        if not instrument_token:
            print(f"ATR fetch failed: instrument token missing for {spot_symbol}", flush=True)
            return None, None

        from_dt = current_dt - pd.Timedelta(days=lookback_days)
        candles = kite.historical_data(instrument_token, from_dt, current_dt, '60minute')
        if not candles:
            print(f"ATR fetch failed: no historical candles returned for {spot_symbol}", flush=True)
            return None, None

        df_m = pd.DataFrame(candles)
        if df_m.empty or 'date' not in df_m.columns:
            print(f"ATR fetch failed: invalid historical candle data for {spot_symbol}", flush=True)
            return None, None

        df_m['date'] = pd.to_datetime(df_m['date'])
        df_m = df_m.sort_values('date').reset_index(drop=True)
        high = df_m['high'].astype(float)
        low = df_m['low'].astype(float)
        close = df_m['close'].astype(float)
        atr_series = talib.ATR(high, low, close, timeperiod=ATR_PERIOD)
        df_m['atr'] = atr_series
        df_m['atr_ema'] = df_m['atr'].ewm(span=atr_ema_window, adjust=False, min_periods=atr_ema_window).mean()

        candle_tz = df_m['date'].dt.tz
        current_date = pd.Timestamp(current_dt.date())
        if candle_tz is not None:
            current_date = current_date.tz_localize(candle_tz)

        eligible_rows = df_m[(df_m['date'] < current_date) & df_m['atr_ema'].notna()]
        if eligible_rows.empty:
            print(f"ATR fetch failed: no completed ATR EMA value available for {spot_symbol}", flush=True)
            return None, df_m

        display_cols = ['date', 'open', 'high', 'low', 'close', 'atr', 'atr_ema']
        print("ATR EMA last 5 rows:", flush=True)
        print(df_m[display_cols].tail(5).to_string(index=False), flush=True)

        selected_row = eligible_rows.iloc[-1]
        print("ATR EMA row used for latest ATR:", flush=True)
        print(selected_row[display_cols].to_frame().T.to_string(index=False), flush=True)
        print(f"Using ATR candle: {selected_row['date']}", flush=True)

        latest_atr = float(selected_row['atr_ema'])
        return latest_atr, df_m

    except Exception as e:
        print(f"ATR fetch failed for {spot_symbol}: {e}", flush=True)
        return None, None

def get_vix_stop_per_lot(kite, current_dt, lookback_days=400):
    """Fetches 1D VIX candles and returns stop_per_lot based on prior-day 252-day percentile rank."""
    try:
        quote = kite.quote(VIX_SYMBOL)
        instrument_token = quote.get(VIX_SYMBOL, {}).get('instrument_token')
        if not instrument_token:
            print(f"VIX fetch failed: instrument token missing for {VIX_SYMBOL}", flush=True)
            return None, None

        from_dt = current_dt - pd.Timedelta(days=lookback_days)
        candles = kite.historical_data(instrument_token, from_dt, current_dt, 'day')
        if not candles:
            print(f"VIX fetch failed: no historical candles returned for {VIX_SYMBOL}", flush=True)
            return None, None

        df_vix = pd.DataFrame(candles)
        if df_vix.empty or 'date' not in df_vix.columns:
            print(f"VIX fetch failed: invalid historical candle data for {VIX_SYMBOL}", flush=True)
            return None, None

        df_vix['date'] = pd.to_datetime(df_vix['date'])
        df_vix = df_vix.sort_values('date').reset_index(drop=True)

        candle_tz = df_vix['date'].dt.tz
        current_date = pd.Timestamp(current_dt.date())
        if candle_tz is not None:
            current_date = current_date.tz_localize(candle_tz)

        eligible_rows = df_vix[df_vix['date'] < current_date].copy()
        if len(eligible_rows) < 252:
            print(f"VIX fetch failed: need at least 252 prior daily candles, found {len(eligible_rows)}", flush=True)
            return None, df_vix

        trailing_window = eligible_rows['close'].astype(float).tail(252)
        vix_rank_pct = trailing_window.rank(pct=True).iloc[-1]
        stop_per_lot = 4000 if vix_rank_pct > 0.5 else 3000

        display_cols = ['date', 'open', 'high', 'low', 'close']
        print("VIX last 5 prior-day eligible rows:", flush=True)
        print(eligible_rows[display_cols].tail(5).to_string(index=False), flush=True)
        selected_row = eligible_rows.iloc[-1]
        print("VIX row used for stop decision:", flush=True)
        print(selected_row[display_cols].to_frame().T.to_string(index=False), flush=True)
        print(f"Using VIX candle: {selected_row['date']} | Rank pct over trailing 252: {vix_rank_pct:.4f} | Stop/Lot: {stop_per_lot}", flush=True)

        return stop_per_lot, df_vix

    except Exception as e:
        print(f"VIX stop fetch failed for {VIX_SYMBOL}: {e}", flush=True)
        return None, None

def get_index_entry_atr(kite, spot_symbol, current_dt, lookback_days=10):
    """
    Fetches 1-minute OHLC candles for the spot index and returns the ATR(INDEX_ENTRY_ATR_PERIOD)
    value from the last completed 1-minute candle of the previous trading day.
    Used exclusively for index_based_entry mode.
    """
    try:
        print(f"[INDEX_ENTRY_ATR] Fetching instrument token for {spot_symbol}...", flush=True)
        quote = kite.quote(spot_symbol)
        instrument_token = quote.get(spot_symbol, {}).get('instrument_token')
        if not instrument_token:
            print(f"[INDEX_ENTRY_ATR] FAILED: instrument token missing for {spot_symbol}", flush=True)
            return None

        from_dt = current_dt - pd.Timedelta(days=lookback_days)
        print(f"[INDEX_ENTRY_ATR] Fetching 1m candles for {spot_symbol} from {from_dt} to {current_dt}...", flush=True)
        candles = kite.historical_data(instrument_token, from_dt, current_dt, 'minute')
        if not candles:
            print(f"[INDEX_ENTRY_ATR] FAILED: no 1m candles returned for {spot_symbol}", flush=True)
            return None

        df = pd.DataFrame(candles)
        if df.empty or 'date' not in df.columns:
            print(f"[INDEX_ENTRY_ATR] FAILED: invalid candle data for {spot_symbol}", flush=True)
            return None

        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        print(f"[INDEX_ENTRY_ATR] Total 1m candles fetched: {len(df)}", flush=True)

        # Determine today's date, timezone-aware if needed
        candle_tz = df['date'].dt.tz
        current_date = pd.Timestamp(current_dt.date())
        if candle_tz is not None:
            current_date = current_date.tz_localize(candle_tz)

        # Keep only candles strictly before today (previous trading day and earlier)
        prev_day_candles = df[df['date'] < current_date].copy()
        if prev_day_candles.empty:
            print(f"[INDEX_ENTRY_ATR] FAILED: no candles found before today ({current_date})", flush=True)
            return None

        # Identify the last trading day date
        last_trading_date = prev_day_candles['date'].dt.date.max()
        prev_day_only = prev_day_candles[prev_day_candles['date'].dt.date == last_trading_date].copy()
        print(f"[INDEX_ENTRY_ATR] Previous trading day identified: {last_trading_date} | Candles in that day: {len(prev_day_only)}", flush=True)

        # Calculate ATR(INDEX_ENTRY_ATR_PERIOD) on ALL available prior candles (need enough history)
        high = prev_day_candles['high'].astype(float)
        low  = prev_day_candles['low'].astype(float)
        close = prev_day_candles['close'].astype(float)

        if len(prev_day_candles) < INDEX_ENTRY_ATR_PERIOD + 1:
            print(f"[INDEX_ENTRY_ATR] FAILED: not enough candles ({len(prev_day_candles)}) to compute ATR({INDEX_ENTRY_ATR_PERIOD})", flush=True)
            return None

        atr_series = talib.ATR(high.values, low.values, close.values, timeperiod=INDEX_ENTRY_ATR_PERIOD)
        prev_day_candles = prev_day_candles.copy()
        prev_day_candles['atr'] = atr_series

        # Get candles from the previous trading day that have a valid ATR
        prev_day_with_atr = prev_day_candles[
            (prev_day_candles['date'].dt.date == last_trading_date) &
            prev_day_candles['atr'].notna()
        ]

        if prev_day_with_atr.empty:
            print(f"[INDEX_ENTRY_ATR] FAILED: no valid ATR values on previous trading day {last_trading_date}", flush=True)
            return None

        last_candle = prev_day_with_atr.iloc[-1]
        atr_value = float(last_candle['atr'])

        display_cols = ['date', 'open', 'high', 'low', 'close', 'atr']
        print(f"[INDEX_ENTRY_ATR] Last 5 candles of previous trading day ({last_trading_date}) with ATR:", flush=True)
        print(prev_day_with_atr[display_cols].tail(5).to_string(index=False), flush=True)
        print(f"[INDEX_ENTRY_ATR] Selected candle (last of previous day):", flush=True)
        print(last_candle[display_cols].to_frame().T.to_string(index=False), flush=True)
        print(f"[INDEX_ENTRY_ATR] ATR({INDEX_ENTRY_ATR_PERIOD}) value from previous day last candle: {atr_value:.4f}", flush=True)

        return atr_value

    except Exception as e:
        print(f"[INDEX_ENTRY_ATR] EXCEPTION during fetch for {spot_symbol}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None
    
# ==============================================================================
# 3. STRATEGY LOGIC (ATM Finder, Strike Selection)
# ==============================================================================

def find_best_atm_strike(kite, mathematical_atm, instruments_df, step, exchange='NFO'):
    """Finds the strike with the lowest difference between CE and PE premiums."""
    print(f"Scanning for Best ATM (Step: {step})...", flush=True)
    best_strike = mathematical_atm
    min_premium_diff = 999999.0
    
    # Use the explicit STEP passed from config
    candidates = [int(mathematical_atm) + (x * step) for x in range(-3, 4)]
    
    tokens_to_fetch = []
    strike_map = {}
    
    for strike in candidates:
        try:
            ce_row = instruments_df[(instruments_df['strike'] == strike) & (instruments_df['instrument_type'] == 'CE')]
            pe_row = instruments_df[(instruments_df['strike'] == strike) & (instruments_df['instrument_type'] == 'PE')]
            
            if ce_row.empty or pe_row.empty: continue
            
            ce_sym = ce_row.iloc[0]['tradingsymbol']
            pe_sym = pe_row.iloc[0]['tradingsymbol']
            
            tokens_to_fetch.extend([f"{exchange}:{ce_sym}", f"{exchange}:{pe_sym}"])
            strike_map[strike] = {'CE': f"{exchange}:{ce_sym}", 'PE': f"{exchange}:{pe_sym}"}
        except: continue
        
    if not tokens_to_fetch: return mathematical_atm

    ltp_data = get_ltp_safe(kite, tokens_to_fetch)
    
    print(f"{'Strike':<10} | {'Diff':<10}", flush=True)
    for strike, symbols in strike_map.items():
        try:
            ce_price = ltp_data.get(symbols['CE'], {}).get('last_price', 0)
            pe_price = ltp_data.get(symbols['PE'], {}).get('last_price', 0)
            
            # Skip if data is missing
            if ce_price == 0 or pe_price == 0: continue
            
            diff = abs(ce_price - pe_price)
            print(f"{strike:<10} | {diff:<10.2f}", flush=True)
            if diff < min_premium_diff:
                min_premium_diff = diff
                best_strike = strike
        except: continue
    
    print(f"Selected Best ATM: {best_strike}", flush=True)
    return best_strike

def find_otm_buy_strikes(kite, atm_strike, instruments_df, exchange='NFO', target_premium=15):
    """Finds deep OTM strikes close to target premium for hedging."""
    print(f"Finding OTM Buy Strikes (Target Premium: {target_premium})...", flush=True)
    try:
        upper_strikes = instruments_df[instruments_df["strike"] > atm_strike].sort_values("strike").head(40)
        lower_strikes = instruments_df[instruments_df["strike"] < atm_strike].sort_values("strike", ascending=False).head(40)
        all_candidates = pd.concat([upper_strikes, lower_strikes])
        
        candidate_symbols = [f"{exchange}:" + s for s in all_candidates["tradingsymbol"].unique()]
        ltp_data = get_ltp_safe(kite, candidate_symbols)
        
        ce_options = all_candidates[all_candidates['instrument_type'] == 'CE']
        best_ce_strike = min(ce_options.to_dict('records'), 
                             key=lambda x: abs(ltp_data.get(f"{exchange}:"+x["tradingsymbol"], {}).get('last_price', 999) - target_premium))['strike']
        
        pe_options = all_candidates[all_candidates['instrument_type'] == 'PE']
        best_pe_strike = min(pe_options.to_dict('records'), 
                             key=lambda x: abs(ltp_data.get(f"{exchange}:"+x["tradingsymbol"], {}).get('last_price', 999) - target_premium))['strike']
        
        return best_ce_strike, best_pe_strike
    except Exception as e:
        print(f"Error finding buy strikes: {e}. Using Fallback.", flush=True)
        return atm_strike + 500, atm_strike - 500
    
def get_token_and_symbol(df, strike, instrument_type):
    try:
        row = df[(df['strike'] == strike) & (df['instrument_type'] == instrument_type)].iloc[0]
        return int(row['instrument_token']), row['tradingsymbol']
    except: return None, None

# ==============================================================================
# 4. DAILY PROCESS LIFECYCLE (The Worker)
# ==============================================================================

def run_trading_process():
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"[{get_now_str()}] DAILY TRADING PROCESS INITIALIZED", flush=True)
        print(f"{'='*60}", flush=True)
        
        # 1. Configuration (Merged)
        day_config = get_current_day_config()
        idx_key = day_config['target_index']
        idx_config = INDEX_DETAILS[idx_key]
        
        # Merge dictionaries
        config = {**day_config, **idx_config}
        
        # Load Constants
        SYMBOL_NAME = config['index_name']       
        INDEX_FILTER = config['index_filter']    
        SPOT_EXCHANGE = config['spot_exchange']  
        OPT_EXCHANGE = config['opt_exchange']    
        STRIKE_STEP = config['strike_step']      
        STANDARD_LOT_SIZE = config['std_lot_size'] 
        
        LOTS = config['lots']
        LIVE_MODE = config['live_mode']
        PERCENT_MODE = config.get('percent_mode', 1)
        ATR_MODE_ON = config.get('atr_mode_on', False)
        VIX_STOP_MODE_ON = config.get('vix_stop_mode_on', False)
        HEDGELESS_MODE = config.get('hedgeless_mode', False)
        INDEX_BASED_ENTRY = config.get('index_based_entry', False)
        ATR_ENTRY_MULTIPLIER = config.get('atr_entry_multiplier', 1)
        QUANTITY = LOTS * STANDARD_LOT_SIZE
        
        effective_entry_gap = config['entry_gap']
        target_profit_per_lot = config.get('target_profit_per_lot', TARGET_PROFIT_PER_LOT)
        max_loss_per_lot = config.get('max_loss_per_lot', MAX_LOSS_PER_LOT)
        stop_percent = config.get('stop_percent', 50)
        skip_checking_till_hour_loss_bypass = config.get('skip_checking_till_hour_loss_bypass', SKIP_CHECKING_TILL_HOUR_LOSS_BYPASS)
        GLOBAL_PROFIT_TARGET = target_profit_per_lot * LOTS
        GLOBAL_MAX_LOSS = max_loss_per_lot * LOTS
        
        print(f"[{get_now_str()}] Configuration Loaded for {SYMBOL_NAME} ({OPT_EXCHANGE}):", flush=True)
        print(f"Mode:              {'LIVE TRADING (REAL MONEY)' if LIVE_MODE else 'PAPER TRADING (SIMULATION)'}", flush=True)
        print(f"Quantity:          {QUANTITY} ({LOTS} Lots x {STANDARD_LOT_SIZE})", flush=True)
        print(f"Gap Mode:          {'ATR ABSOLUTE' if ATR_MODE_ON else ('PERCENTAGE' if PERCENT_MODE else 'ABSOLUTE POINTS')}", flush=True)
        print(f"Entry Gap:         {'ATR-DERIVED' if ATR_MODE_ON else config['entry_gap']}", flush=True)
        print(f"Strike Gap:        {config['strike_gap']}", flush=True)
        print(f"Risk Checks:       Start after {config.get('skip_till_hour', 8)}:59", flush=True)
        print(f"Skip-Hour Bypass:  {skip_checking_till_hour_loss_bypass}x Global Max Loss ({'DISABLED' if skip_checking_till_hour_loss_bypass == 0 else 'ENABLED'})", flush=True)
        print(f"Stop %:            {stop_percent}", flush=True)
        print(f"ATM Finder:        {'ENABLED' if config.get('find_atm', True) else 'DISABLED'}", flush=True)
        print(f"ATR Mode:          {'ENABLED' if ATR_MODE_ON else 'DISABLED'} | EMA Window: {config.get('atr_ema_window', 20)} | Stop Mult: {config.get('atr_stop_per_lot', 30)} | Gap %: {config.get('atr_entry_gap', 10)}", flush=True)
        print(f"VIX Stop:          {'ENABLED' if VIX_STOP_MODE_ON else 'DISABLED'}", flush=True)
        print(f"Index Based Entry: {'ENABLED' if INDEX_BASED_ENTRY else 'DISABLED'} | ATR Entry Multiplier: {ATR_ENTRY_MULTIPLIER} | ATR Period: {INDEX_ENTRY_ATR_PERIOD}", flush=True)
        print(f"Flags:             Skip: {config['total_premium_skip']} | Buy: {config['buy_strikes_flag']} | PftChg: {config['total_profit_change']} | Hedgeless: {HEDGELESS_MODE}", flush=True)
        print(f"Target/Lot:        {target_profit_per_lot}", flush=True)
        print(f"Max Loss/Lot:      {max_loss_per_lot}", flush=True)
        print(f"Global Target:     {GLOBAL_PROFIT_TARGET}", flush=True)
        print(f"Max Loss:          {GLOBAL_MAX_LOSS}", flush=True)

        # 2. Authenticate
        try:
            with open(AUTH_FILE_PATH, 'r') as f:
                data = f.read().split(',')
                api_key, access_token = data[0], data[1]
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)
            print(f"[{get_now_str()}] Kite Session Authenticated.", flush=True)
        except Exception as e:
            print(f"[{get_now_str()}] CRITICAL ERROR: Auth failed. {e}", flush=True)
            return

        # 3. Instruments
        print(f"[{get_now_str()}] Fetching Instruments for {OPT_EXCHANGE} from Kite API...", flush=True)
        try:
            instruments_list = kite.instruments(OPT_EXCHANGE)
            instruments_df = pd.DataFrame(instruments_list)
            
            nifty_options = instruments_df[
                (instruments_df['name'] == INDEX_FILTER) & 
                (instruments_df['segment'] == f"{OPT_EXCHANGE}-OPT")
            ]
            
            if nifty_options.empty:
                print(f"[{get_now_str()}] CRITICAL: No instruments found for {INDEX_FILTER} in {OPT_EXCHANGE}. Check filters.", flush=True)
                return

            current_expiry = nifty_options['expiry'].unique()[0] 
            nifty_options = nifty_options[nifty_options['expiry'] == current_expiry]
            is_expiry_day = (datetime.now().date() == pd.to_datetime(current_expiry).date())
            print(f"[{get_now_str()}] Expiry Date: {current_expiry} (Is Today Expiry? {is_expiry_day})", flush=True)
            
        except Exception as e:
            print(f"[{get_now_str()}] Error fetching instruments: {e}", flush=True)
            return

        # 4. 9:00 AM Setup (ATR / VIX / Index Entry ATR)
        spot_sym_full = f"{SPOT_EXCHANGE}:{SYMBOL_NAME}"
        index_entry_atr = None  # Will be set below if INDEX_BASED_ENTRY is on

        if ATR_MODE_ON or VIX_STOP_MODE_ON or INDEX_BASED_ENTRY:
            calc_time = "09:00"
            print(f"[{get_now_str()}] Pre-start setup active. Waiting for calculation time: {calc_time}", flush=True)
            while datetime.now().strftime("%H:%M") < calc_time:
                time.sleep(1)

            reference_dt = datetime.now(IST)

            if ATR_MODE_ON:
                latest_atr, df_m = get_latest_atr_ema(
                    kite,
                    spot_sym_full,
                    reference_dt,
                    atr_ema_window=config.get('atr_ema_window', 20)
                )

                if latest_atr is not None:
                    atr_entry_gap_pct = config.get('atr_entry_gap', 10)
                    max_loss_per_lot = config.get('atr_stop_per_lot', 30) * latest_atr
                    GLOBAL_MAX_LOSS = max_loss_per_lot * LOTS
                    if atr_entry_gap_pct == 0:
                        print(f"[{get_now_str()}] ATR entry gap value is 0. Falling back to configured entry gap.", flush=True)
                    else:
                        effective_entry_gap = latest_atr / atr_entry_gap_pct
                    print(f"[{get_now_str()}] ATR Mode Active @ 09:00 | Latest ATR EMA: {latest_atr:.2f} | Effective Entry Gap %: {effective_entry_gap:.2f} | Max Loss/Lot: {max_loss_per_lot:.2f} | Global Max Loss: {GLOBAL_MAX_LOSS:.2f}", flush=True)
                else:
                    print(f"[{get_now_str()}] ATR Mode fallback: using configured entry gap {effective_entry_gap}.", flush=True)

            if VIX_STOP_MODE_ON:
                vix_stop_per_lot, df_vix = get_vix_stop_per_lot(kite, reference_dt)
                if vix_stop_per_lot is not None:
                    max_loss_per_lot = vix_stop_per_lot
                    GLOBAL_MAX_LOSS = max_loss_per_lot * LOTS
                    print(f"[{get_now_str()}] VIX Stop Mode Active @ 09:00 | Max Loss/Lot overridden to {max_loss_per_lot:.2f} | Global Max Loss: {GLOBAL_MAX_LOSS:.2f}", flush=True)
                else:
                    print(f"[{get_now_str()}] VIX Stop Mode fallback: using current max loss per lot {max_loss_per_lot}.", flush=True)

            if INDEX_BASED_ENTRY:
                print(f"[{get_now_str()}] Index Based Entry: Fetching 1m ATR({INDEX_ENTRY_ATR_PERIOD}) for {spot_sym_full}...", flush=True)
                index_entry_atr = get_index_entry_atr(kite, spot_sym_full, reference_dt)
                if index_entry_atr is not None:
                    print(f"[{get_now_str()}] Index Based Entry ATR fetched successfully: {index_entry_atr:.4f} | Multiplier: {ATR_ENTRY_MULTIPLIER} | Effective ATR Band: {index_entry_atr * ATR_ENTRY_MULTIPLIER:.4f}", flush=True)
                else:
                    print(f"[{get_now_str()}] WARNING: Index Based Entry ATR fetch FAILED. index_based_entry will be DISABLED for today. Falling back to normal option-price entry logic.", flush=True)
                    INDEX_BASED_ENTRY = False

        # 5. Wait for Start Time
        print(f"\n[{get_now_str()}] Waiting for Start Time: {config['start']}...", flush=True)
        while True:
            now_str = datetime.now().strftime("%H:%M")
            if now_str >= config['start']: break
            time.sleep(1)
            
        # 6. Levels
        print(f"\n[{get_now_str()}] --- CALCULATING LEVELS ---", flush=True)
        spot_ltp = get_ltp_safe(kite, [spot_sym_full]).get(spot_sym_full, {}).get('last_price', 0)
        
        if spot_ltp == 0:
            print(f"[{get_now_str()}] Error: Could not fetch Spot Price for {spot_sym_full}", flush=True)
            return

        mathematical_atm = int(round(spot_ltp / STRIKE_STEP) * STRIKE_STEP)
        print(f"[{get_now_str()}] Spot Price ({SYMBOL_NAME}): {spot_ltp} | Math ATM: {mathematical_atm}", flush=True)
        
        # --- ATM FINDER LOGIC ---
        if config.get('find_atm', True):
            atm_strike = find_best_atm_strike(kite, mathematical_atm, nifty_options, step=STRIKE_STEP, exchange=OPT_EXCHANGE)
        else:
            print(f"[{get_now_str()}] ATM Finder Disabled. Using Mathematical ATM: {mathematical_atm}", flush=True)
            atm_strike = mathematical_atm
        
        strike_gap = config['strike_gap']
        entry_put_strike = atm_strike + (strike_gap * STRIKE_STEP)
        entry_call_strike = atm_strike - (strike_gap * STRIKE_STEP)
        
        token_entry_put, sym_entry_put = get_token_and_symbol(nifty_options, entry_put_strike, 'PE')
        token_entry_call, sym_entry_call = get_token_and_symbol(nifty_options, entry_call_strike, 'CE')
        
        initial_prices = get_ltp_safe(kite, [f"{OPT_EXCHANGE}:{sym_entry_put}", f"{OPT_EXCHANGE}:{sym_entry_call}"])
        price_put = initial_prices.get(f"{OPT_EXCHANGE}:{sym_entry_put}", {}).get('last_price', 0)
        price_call = initial_prices.get(f"{OPT_EXCHANGE}:{sym_entry_call}", {}).get('last_price', 0)
        
        print(f"[{get_now_str()}] Skew Balance Check | Before | PE Strike: {entry_put_strike} ({sym_entry_put}) | PE Price: {price_put} | CE Strike: {entry_call_strike} ({sym_entry_call}) | CE Price: {price_call} | Threshold: 1.25x", flush=True)
        if price_put > 1.25 * price_call:
            old_entry_call_strike = entry_call_strike
            print(f"[{get_now_str()}] Skew Balance Decision: Adjustment needed. PE ({price_put}) > 1.25x CE ({price_call}). Shifting CE down by {STRIKE_STEP}.", flush=True)
            entry_call_strike -= STRIKE_STEP
            token_entry_call, sym_entry_call = get_token_and_symbol(nifty_options, entry_call_strike, 'CE')
            print(f"[{get_now_str()}] Skew Balance After | CE Strike: {old_entry_call_strike} -> {entry_call_strike} ({sym_entry_call}) | PE Strike unchanged: {entry_put_strike} ({sym_entry_put})", flush=True)
        elif 1.25 * price_put < price_call:
            old_entry_put_strike = entry_put_strike
            print(f"[{get_now_str()}] Skew Balance Decision: Adjustment needed. CE ({price_call}) > 1.25x PE ({price_put}). Shifting PE up by {STRIKE_STEP}.", flush=True)
            entry_put_strike += STRIKE_STEP
            token_entry_put, sym_entry_put = get_token_and_symbol(nifty_options, entry_put_strike, 'PE')
            print(f"[{get_now_str()}] Skew Balance After | PE Strike: {old_entry_put_strike} -> {entry_put_strike} ({sym_entry_put}) | CE Strike unchanged: {entry_call_strike} ({sym_entry_call})", flush=True)
        else:
            print(f"[{get_now_str()}] Skew Balance Decision: No adjustment needed. PE Strike remains {entry_put_strike} ({sym_entry_put}); CE Strike remains {entry_call_strike} ({sym_entry_call}).", flush=True)
            
        if HEDGELESS_MODE:
            hedge_put_strike, hedge_call_strike = 0, 0
            token_hedge_put, sym_hedge_put = None, None
            token_hedge_call, sym_hedge_call = None, None
        else:
            hedge_put_strike = entry_put_strike - (10 * STRIKE_STEP)
            hedge_call_strike = entry_call_strike + (10 * STRIKE_STEP)
            token_hedge_put, sym_hedge_put = get_token_and_symbol(nifty_options, hedge_put_strike, 'PE')
            token_hedge_call, sym_hedge_call = get_token_and_symbol(nifty_options, hedge_call_strike, 'CE')
        
        buy_strike_ce, buy_strike_pe = 0, 0
        token_buy_ce, sym_buy_ce, token_buy_pe, sym_buy_pe = None, None, None, None
        
        # --- BUY STRIKES FLAG LOGIC ---
        use_buy_legs = config['buy_strikes_flag']
        if use_buy_legs:
            buy_strike_ce, buy_strike_pe = find_otm_buy_strikes(kite, atm_strike, nifty_options, exchange=OPT_EXCHANGE)
            token_buy_ce, sym_buy_ce = get_token_and_symbol(nifty_options, buy_strike_ce, 'CE')
            token_buy_pe, sym_buy_pe = get_token_and_symbol(nifty_options, buy_strike_pe, 'PE')
            
        snap_symbols = [f"{OPT_EXCHANGE}:{s}" for s in [sym_entry_put, sym_entry_call, sym_hedge_put, sym_hedge_call, sym_buy_pe, sym_buy_ce] if s]
        snapshot = get_ltp_safe(kite, snap_symbols)
        
        initial_ep_price = snapshot.get(f"{OPT_EXCHANGE}:{sym_entry_put}", {}).get('last_price', 0)
        initial_ec_price = snapshot.get(f"{OPT_EXCHANGE}:{sym_entry_call}", {}).get('last_price', 0)
        
        gap_val_put = 0
        gap_val_call = 0
        if ATR_MODE_ON:
            gap_val_put = (initial_ep_price * effective_entry_gap * 0.01)
            gap_val_call = (initial_ec_price * effective_entry_gap * 0.01)
        elif PERCENT_MODE == 1:
            gap_val_put = (initial_ep_price * config['entry_gap'] * 0.01)
            gap_val_call = (initial_ec_price * config['entry_gap'] * 0.01)
        else:
            gap_val_put = effective_entry_gap
            gap_val_call = effective_entry_gap

        threshold_put = initial_ep_price - gap_val_put
        threshold_call = initial_ec_price - gap_val_call
        stop_put = threshold_put * (1 + stop_percent * 0.01)
        stop_call = threshold_call * (1 + stop_percent * 0.01)
        
        initial_bp_price = snapshot.get(f"{OPT_EXCHANGE}:{sym_buy_pe}", {}).get('last_price', 0) if sym_buy_pe else 0
        initial_bc_price = snapshot.get(f"{OPT_EXCHANGE}:{sym_buy_ce}", {}).get('last_price', 0) if sym_buy_ce else 0

        # --- INDEX BASED ENTRY: Compute index thresholds using spot_ltp fetched at start time (REST) ---
        index_entry_threshold_put = None
        index_entry_threshold_call = None

        if INDEX_BASED_ENTRY:
            if index_entry_atr is None:
                print(f"[{get_now_str()}] Index Based Entry | CRITICAL: ATR is None. Disabling INDEX_BASED_ENTRY.", flush=True)
                INDEX_BASED_ENTRY = False
            else:
                # Use spot_ltp already fetched via REST at start time (get_ltp_safe call above at config['start'])
                index_ltp_at_start = spot_ltp
                print(f"[{get_now_str()}] Index Based Entry | Using spot_ltp fetched at start time (REST): {index_ltp_at_start}", flush=True)
                if index_ltp_at_start == 0:
                    print(f"[{get_now_str()}] Index Based Entry | CRITICAL: spot_ltp is 0. Disabling INDEX_BASED_ENTRY.", flush=True)
                    INDEX_BASED_ENTRY = False
                else:
                    atr_band = index_entry_atr * ATR_ENTRY_MULTIPLIER
                    index_entry_threshold_put  = index_ltp_at_start + atr_band
                    index_entry_threshold_call = index_ltp_at_start - atr_band
                    print(f"[{get_now_str()}] Index Based Entry THRESHOLDS SET:", flush=True)
                    print(f"  Index LTP at Start ({config['start']}): {index_ltp_at_start}", flush=True)
                    print(f"  ATR({INDEX_ENTRY_ATR_PERIOD}) from prev day: {index_entry_atr:.4f}", flush=True)
                    print(f"  ATR Multiplier:                  {ATR_ENTRY_MULTIPLIER}", flush=True)
                    print(f"  ATR Band (ATR x Multiplier):     {atr_band:.4f}", flush=True)
                    print(f"  PUT  entry threshold (index > ): {index_entry_threshold_put:.2f}", flush=True)
                    print(f"  CALL entry threshold (index < ): {index_entry_threshold_call:.2f}", flush=True)
                    print(f"  NOTE: Stop prices will be computed from actual fill price at entry time.", flush=True)
        
        # --- TOTAL PREMIUM SKIP LOGIC ---
        total_premium = threshold_put + threshold_call
        print(f"[{get_now_str()}] Total Premium (Thresholds): {total_premium:.2f}", flush=True)
        
        if config['total_premium_skip'] and LIVE_MODE == 1:
            if LOWER_THRESHOLD <= total_premium <= UPPER_THRESHOLD:
                print(f"[{get_now_str()}] Premium Check Failed: In Skip Range ({LOWER_THRESHOLD} <= {total_premium:.2f} <= {UPPER_THRESHOLD}). SWITCHING TO PAPER TRADING.", flush=True)
                LIVE_MODE = 0
            else:
                print(f"[{get_now_str()}] Premium Check Passed: Outside Skip Range ({total_premium:.2f}). Staying Live.", flush=True)
        
        print(f"\n{'-'*60}", flush=True)
        print(f"[{get_now_str()}] STRATEGY SETUP COMPLETE", flush=True)
        print(f"{'-'*60}", flush=True)
        if ATR_MODE_ON:
            print(f"ATR INFO       | Entry Gap: {effective_entry_gap:.2f} | Max Loss/Lot: {max_loss_per_lot:.2f} | Global Max Loss: {GLOBAL_MAX_LOSS:.2f}", flush=True)
        print(f"PUT LEG        | Strike: {entry_put_strike} PE | Initial: {initial_ep_price} | Threshold: {threshold_put:.2f} | Stop: {stop_put:.2f}", flush=True)
        print(f"CALL LEG       | Strike: {entry_call_strike} CE | Initial: {initial_ec_price} | Threshold: {threshold_call:.2f} | Stop: {stop_call:.2f}", flush=True)
        if INDEX_BASED_ENTRY:
            print(f"INDEX ENTRY    | PUT threshold (index >): {index_entry_threshold_put:.2f} | CALL threshold (index <): {index_entry_threshold_call:.2f} | (option-price entry thresholds above are INACTIVE for sell legs)", flush=True)
            print(f"INDEX ENTRY    | Stop prices will be set from actual fill price at entry time using stop_percent: {stop_percent}%", flush=True)
        if HEDGELESS_MODE:
            print("HEDGES         | DISABLED (hedgeless mode)", flush=True)
        else:
            print(f"HEDGES         | PE: {hedge_put_strike} | CE: {hedge_call_strike}", flush=True)
        if use_buy_legs:
            print(f"BUY LEGS       | PE: {buy_strike_pe} (@ {initial_bp_price}) | CE: {buy_strike_ce} (@ {initial_bc_price})", flush=True)
        print(f"{'-'*60}\n", flush=True)
        
        # ----------------------------------------------------------------------
        # 7. TRADING LOOP
        # ----------------------------------------------------------------------
        tradebook_df = pd.read_csv(TRADEBOOK_CSV_PATH)
        
        flag_sell_put, flag_sell_call = 0, 0
        flag_buy_put, flag_buy_call = 0, 0
        
        entry_price_put_sold, entry_price_call_sold = 0, 0
        entry_price_hedge_put, entry_price_hedge_call = 0, 0 
        entry_price_put_bought, entry_price_call_bought = 0, 0

        exit_price_put_sold, exit_price_call_sold = 0, 0
        exit_price_hedge_put, exit_price_hedge_call = 0, 0
        exit_price_put_bought, exit_price_call_bought = 0, 0
        
        final_realized_pnl = 0
        final_realized_main_pnl = 0 
        realized_put_main_pnl, realized_put_hedge_pnl, realized_put_leg_pnl = 0, 0, 0
        realized_call_main_pnl, realized_call_hedge_pnl, realized_call_leg_pnl = 0, 0, 0
        last_print_minute = datetime.now().minute
        
        # --- CLOSING HELPER FUNCTIONS ---
        
        def close_put_side(reason, sl_price=None):
            nonlocal final_realized_pnl, final_realized_main_pnl, flag_sell_put, exit_price_put_sold, exit_price_hedge_put, realized_put_main_pnl, realized_put_hedge_pnl, realized_put_leg_pnl, GLOBAL_PROFIT_TARGET
            if flag_sell_put != 1: return 

            print(f"\n[{get_now_str()}] !!! CLOSING PUT SIDE | REASON: {reason} !!!", flush=True)
            if sl_price: print(f"    STOP PRICE: {sl_price:.2f}", flush=True)

            place_order_with_retry(kite, sym_entry_put, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym_entry_put, 'BUY', exchange=OPT_EXCHANGE) 
            exit_price_put_sold = exec_price 
            
            main_pnl = QUANTITY * (entry_price_put_sold - exec_price)
            comm_main = commission(QUANTITY, exec_price, entry_price_put_sold)

            if HEDGELESS_MODE:
                exec_hedge = 0
                exit_price_hedge_put = 0
                hedge_pnl = 0
                comm_hedge = 0
            else:
                place_order_with_retry(kite, sym_hedge_put, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                exec_hedge = get_quote_price(kite, sym_hedge_put, 'SELL', exchange=OPT_EXCHANGE) 
                exit_price_hedge_put = exec_hedge
                hedge_pnl = QUANTITY * (exec_hedge - entry_price_hedge_put)
                comm_hedge = commission(QUANTITY, entry_price_hedge_put, exec_hedge)
            
            total_leg_pnl = main_pnl + hedge_pnl - (comm_main + comm_hedge)
            final_realized_pnl += total_leg_pnl
            final_realized_main_pnl += (main_pnl - comm_main)
            realized_put_main_pnl = main_pnl - comm_main
            realized_put_hedge_pnl = hedge_pnl - comm_hedge
            realized_put_leg_pnl = total_leg_pnl
            
            print(f"    MAIN  | Entry: {entry_price_put_sold} | Exit: {exec_price}", flush=True)
            if HEDGELESS_MODE:
                print("    HEDGE | DISABLED", flush=True)
            else:
                print(f"    HEDGE | Entry: {entry_price_hedge_put} | Exit: {exec_hedge}", flush=True)
            print(f"    STATS | Net PnL: {total_leg_pnl:.2f} (Comm: {comm_main+comm_hedge:.2f})", flush=True)
            
            tradebook_df.loc[len(tradebook_df)] = [get_now_str(), entry_put_strike, 'PE', initial_ep_price, entry_price_put_sold, exec_price, total_leg_pnl, final_realized_pnl, f'Close - Put_Side_{reason}']
            flag_sell_put = 2 
            
            # --- TOTAL PROFIT CHANGE LOGIC ---
            if config['total_profit_change']:
                GLOBAL_PROFIT_TARGET = POST_LEG_CLOSE_PROFIT_TARGET
                print(f"[{get_now_str()}] Total Profit Change Active. New Global Target: {GLOBAL_PROFIT_TARGET}", flush=True)

        def close_call_side(reason, sl_price=None):
            nonlocal final_realized_pnl, final_realized_main_pnl, flag_sell_call, exit_price_call_sold, exit_price_hedge_call, realized_call_main_pnl, realized_call_hedge_pnl, realized_call_leg_pnl, GLOBAL_PROFIT_TARGET
            if flag_sell_call != 1: return

            print(f"\n[{get_now_str()}] !!! CLOSING CALL SIDE | REASON: {reason} !!!", flush=True)
            if sl_price: print(f"    STOP PRICE: {sl_price:.2f}", flush=True)

            place_order_with_retry(kite, sym_entry_call, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
            exec_price = get_quote_price(kite, sym_entry_call, 'BUY', exchange=OPT_EXCHANGE)
            exit_price_call_sold = exec_price 
            
            main_pnl = QUANTITY * (entry_price_call_sold - exec_price)
            comm_main = commission(QUANTITY, exec_price, entry_price_call_sold)

            if HEDGELESS_MODE:
                exec_hedge = 0
                exit_price_hedge_call = 0
                hedge_pnl = 0
                comm_hedge = 0
            else:
                place_order_with_retry(kite, sym_hedge_call, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                exec_hedge = get_quote_price(kite, sym_hedge_call, 'SELL', exchange=OPT_EXCHANGE)
                exit_price_hedge_call = exec_hedge
                hedge_pnl = QUANTITY * (exec_hedge - entry_price_hedge_call)
                comm_hedge = commission(QUANTITY, entry_price_hedge_call, exec_hedge)
            
            total_leg_pnl = main_pnl + hedge_pnl - (comm_main + comm_hedge)
            final_realized_pnl += total_leg_pnl
            final_realized_main_pnl += (main_pnl - comm_main)
            realized_call_main_pnl = main_pnl - comm_main
            realized_call_hedge_pnl = hedge_pnl - comm_hedge
            realized_call_leg_pnl = total_leg_pnl
            
            print(f"    MAIN  | Entry: {entry_price_call_sold} | Exit: {exec_price}", flush=True)
            if HEDGELESS_MODE:
                print("    HEDGE | DISABLED", flush=True)
            else:
                print(f"    HEDGE | Entry: {entry_price_hedge_call} | Exit: {exec_hedge}", flush=True)
            print(f"    STATS | Net PnL: {total_leg_pnl:.2f} (Comm: {comm_main+comm_hedge:.2f})", flush=True)

            tradebook_df.loc[len(tradebook_df)] = [get_now_str(), entry_call_strike, 'CE', initial_ec_price, entry_price_call_sold, exec_price, total_leg_pnl, final_realized_pnl, f'Close - Call_Side_{reason}']
            flag_sell_call = 2

            # --- TOTAL PROFIT CHANGE LOGIC ---
            if config['total_profit_change']:
                GLOBAL_PROFIT_TARGET = POST_LEG_CLOSE_PROFIT_TARGET
                print(f"[{get_now_str()}] Total Profit Change Active. New Global Target: {GLOBAL_PROFIT_TARGET}", flush=True)

        def close_buy_legs(reason):
            nonlocal final_realized_pnl, final_realized_main_pnl, flag_buy_put, flag_buy_call, exit_price_put_bought, exit_price_call_bought
            
            if flag_buy_put == 1:
                place_order_with_retry(kite, sym_buy_pe, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                exec_price = get_quote_price(kite, sym_buy_pe, 'SELL', exchange=OPT_EXCHANGE)
                exit_price_put_bought = exec_price 
                
                raw_pnl = QUANTITY * (exec_price - entry_price_put_bought)
                comm = commission(QUANTITY, entry_price_put_bought, exec_price)
                net = raw_pnl - comm
                final_realized_pnl += net
                final_realized_main_pnl += net 
                
                print(f"[{get_now_str()}] CLOSED BUY PUT | Entry: {entry_price_put_bought} | Exit: {exec_price} | PnL: {net:.2f}", flush=True)
                tradebook_df.loc[len(tradebook_df)] = [get_now_str(), buy_strike_pe, 'PE', initial_bp_price, entry_price_put_bought, exec_price, net, final_realized_pnl, f'Close - Put_Bought_{reason}']
                flag_buy_put = 2

            if flag_buy_call == 1:
                place_order_with_retry(kite, sym_buy_ce, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                exec_price = get_quote_price(kite, sym_buy_ce, 'SELL', exchange=OPT_EXCHANGE)
                exit_price_call_bought = exec_price 
                
                raw_pnl = QUANTITY * (exec_price - entry_price_call_bought)
                comm = commission(QUANTITY, entry_price_call_bought, exec_price)
                net = raw_pnl - comm
                final_realized_pnl += net
                final_realized_main_pnl += net
                
                print(f"[{get_now_str()}] CLOSED BUY CALL | Entry: {entry_price_call_bought} | Exit: {exec_price} | PnL: {net:.2f}", flush=True)
                tradebook_df.loc[len(tradebook_df)] = [get_now_str(), buy_strike_ce, 'CE', initial_bc_price, entry_price_call_bought, exec_price, net, final_realized_pnl, f'Close - Call_Bought_{reason}']
                flag_buy_call = 2

        def close_all_positions(reason):
            print(f"\n[{get_now_str()}] !!! GLOBAL EXIT TRIGGERED: {reason} !!!", flush=True)
            close_put_side(reason)
            close_call_side(reason)
            close_buy_legs(reason)

        print(f"[{get_now_str()}] Starting Main Loop...", flush=True)
        while True:
            check_dt = wait_for_next_check_interval(TRADING_LOOP_CHECK_INTERVAL_MINUTES)
            
            if check_dt.strftime("%H:%M") >= config['exit']:
                close_all_positions("Time_Exit")
                break

            current_snapshot = get_ltp_safe(kite, snap_symbols)
            ltp_put = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_entry_put}", {}).get('last_price', 0)
            ltp_call = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_entry_call}", {}).get('last_price', 0)
            ltp_hedge_put = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_hedge_put}", {}).get('last_price', 0)
            ltp_hedge_call = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_hedge_call}", {}).get('last_price', 0)
            ltp_buy_put = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_buy_pe}", {}).get('last_price', 0) if token_buy_pe else 0
            ltp_buy_call = current_snapshot.get(f"{OPT_EXCHANGE}:{sym_buy_ce}", {}).get('last_price', 0) if token_buy_ce else 0

            # Bot 9: Fetch index LTP via REST for index_based_entry
            if INDEX_BASED_ENTRY:
                ltp_index = get_ltp_safe(kite, [spot_sym_full]).get(spot_sym_full, {}).get('last_price', 0)
                print(f"[{get_now_str()}] [INDEX_ENTRY_CHECK] Index LTP (REST): {ltp_index:.2f} | PUT thresh (>): {index_entry_threshold_put:.2f} | CALL thresh (<): {index_entry_threshold_call:.2f}", flush=True)
            else:
                ltp_index = 0

            if ltp_put == 0 or ltp_call == 0: continue
            risk_checks_enabled = check_dt.hour > config.get('skip_till_hour', 8)
            
            # --- ENTRIES ---
            # Determine whether to use index-based or option-price-based entry for sell legs
            if INDEX_BASED_ENTRY:
                if ltp_index == 0:
                    print(f"[{get_now_str()}] Index Based Entry | WARNING: index LTP is 0. Skipping entry checks.", flush=True)
                    put_entry_condition  = False
                    call_entry_condition = False
                else:
                    put_entry_condition  = ltp_index > index_entry_threshold_put   # Index broke above PUT threshold
                    call_entry_condition = ltp_index < index_entry_threshold_call  # Index broke below CALL threshold
                    print(f"[{get_now_str()}] [INDEX_ENTRY_CHECK] PUT cond: {put_entry_condition} | CALL cond: {call_entry_condition}", flush=True)
            else:
                put_entry_condition  = ltp_put  < threshold_put
                call_entry_condition = ltp_call < threshold_call

            if put_entry_condition and flag_sell_put == 0:
                if INDEX_BASED_ENTRY:
                    print(f"\n[{get_now_str()}] >>> ENTRY PUT SIDE [INDEX-BASED] | Index LTP: {ltp_index:.2f} > Index Threshold: {index_entry_threshold_put:.2f}", flush=True)
                else:
                    print(f"\n[{get_now_str()}] >>> ENTRY PUT SIDE | LTP: {ltp_put} < Thresh: {threshold_put:.2f}", flush=True)
                if HEDGELESS_MODE:
                    entry_price_hedge_put = 0
                else:
                    place_order_with_retry(kite, sym_hedge_put, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
                    entry_price_hedge_put = get_quote_price(kite, sym_hedge_put, 'BUY', exchange=OPT_EXCHANGE)
                    time.sleep(1)
                place_order_with_retry(kite, sym_entry_put, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                entry_price_put_sold = get_quote_price(kite, sym_entry_put, 'SELL', exchange=OPT_EXCHANGE)
                flag_sell_put = 1
                # --- INDEX BASED ENTRY: Recompute stop_put from actual fill price ---
                if INDEX_BASED_ENTRY:
                    stop_put = entry_price_put_sold * (1 + stop_percent * 0.01)
                    print(f"[{get_now_str()}] [INDEX_BASED_ENTRY] PUT stop recomputed from fill price | Fill: {entry_price_put_sold} | Stop: {stop_put:.2f} ({stop_percent}%)", flush=True)
                print(f"[{get_now_str()}] OPENED PUT SIDE | Main: {entry_price_put_sold} | Hedge: {'DISABLED' if HEDGELESS_MODE else entry_price_hedge_put} | Stop: {stop_put:.2f}", flush=True)
                tradebook_df.loc[len(tradebook_df)] = [get_now_str(), entry_put_strike, 'PE', initial_ep_price, entry_price_put_sold, 0, 0, final_realized_pnl, 'Open - Put_Sold']
                
            if call_entry_condition and flag_sell_call == 0:
                if INDEX_BASED_ENTRY:
                    print(f"\n[{get_now_str()}] >>> ENTRY CALL SIDE [INDEX-BASED] | Index LTP: {ltp_index:.2f} < Index Threshold: {index_entry_threshold_call:.2f}", flush=True)
                else:
                    print(f"\n[{get_now_str()}] >>> ENTRY CALL SIDE | LTP: {ltp_call} < Thresh: {threshold_call:.2f}", flush=True)
                if HEDGELESS_MODE:
                    entry_price_hedge_call = 0
                else:
                    place_order_with_retry(kite, sym_hedge_call, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
                    entry_price_hedge_call = get_quote_price(kite, sym_hedge_call, 'BUY', exchange=OPT_EXCHANGE)
                    time.sleep(1)
                place_order_with_retry(kite, sym_entry_call, QUANTITY, 'SELL', LIVE_MODE, exchange=OPT_EXCHANGE)
                entry_price_call_sold = get_quote_price(kite, sym_entry_call, 'SELL', exchange=OPT_EXCHANGE)
                flag_sell_call = 1
                # --- INDEX BASED ENTRY: Recompute stop_call from actual fill price ---
                if INDEX_BASED_ENTRY:
                    stop_call = entry_price_call_sold * (1 + stop_percent * 0.01)
                    print(f"[{get_now_str()}] [INDEX_BASED_ENTRY] CALL stop recomputed from fill price | Fill: {entry_price_call_sold} | Stop: {stop_call:.2f} ({stop_percent}%)", flush=True)
                print(f"[{get_now_str()}] OPENED CALL SIDE | Main: {entry_price_call_sold} | Hedge: {'DISABLED' if HEDGELESS_MODE else entry_price_hedge_call} | Stop: {stop_call:.2f}", flush=True)
                tradebook_df.loc[len(tradebook_df)] = [get_now_str(), entry_call_strike, 'CE', initial_ec_price, entry_price_call_sold, 0, 0, final_realized_pnl, 'Open - Call_Sold']

            # --- BUYS (unchanged — always option-price based) ---
            if use_buy_legs:
                if flag_buy_put == 0 and ltp_buy_put >= (2 * initial_bp_price):
                    print(f"\n[{get_now_str()}] >>> ENTRY BUY PUT | LTP: {ltp_buy_put} >= Trigger: {2 * initial_bp_price:.2f}", flush=True)
                    place_order_with_retry(kite, sym_buy_pe, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
                    entry_price_put_bought = get_quote_price(kite, sym_buy_pe, 'BUY', exchange=OPT_EXCHANGE)
                    flag_buy_put = 1
                    print(f"[{get_now_str()}] OPENED BUY PUT | Entry: {entry_price_put_bought}", flush=True)
                    tradebook_df.loc[len(tradebook_df)] = [get_now_str(), buy_strike_pe, 'PE', initial_bp_price, entry_price_put_bought, 0, 0, final_realized_pnl, 'Open - Put_Bought']
                
                if flag_buy_call == 0 and ltp_buy_call >= (2 * initial_bc_price):
                    print(f"\n[{get_now_str()}] >>> ENTRY BUY CALL | LTP: {ltp_buy_call} >= Trigger: {2 * initial_bc_price:.2f}", flush=True)
                    place_order_with_retry(kite, sym_buy_ce, QUANTITY, 'BUY', LIVE_MODE, exchange=OPT_EXCHANGE)
                    entry_price_call_bought = get_quote_price(kite, sym_buy_ce, 'BUY', exchange=OPT_EXCHANGE)
                    flag_buy_call = 1
                    print(f"[{get_now_str()}] OPENED BUY CALL | Entry: {entry_price_call_bought}", flush=True)
                    tradebook_df.loc[len(tradebook_df)] = [get_now_str(), buy_strike_ce, 'CE', initial_bc_price, entry_price_call_bought, 0, 0, final_realized_pnl, 'Open - Call_Bought']

            # --- PNL CALCULATION ---
            pnl_sp = (QUANTITY * (entry_price_put_sold - ltp_put)) if flag_sell_put == 1 else 0
            pnl_sc = (QUANTITY * (entry_price_call_sold - ltp_call)) if flag_sell_call == 1 else 0
            pnl_hp = (QUANTITY * (ltp_hedge_put - entry_price_hedge_put)) if flag_sell_put == 1 and not HEDGELESS_MODE else 0
            pnl_hc = (QUANTITY * (ltp_hedge_call - entry_price_hedge_call)) if flag_sell_call == 1 and not HEDGELESS_MODE else 0
            pnl_bp = (QUANTITY * (ltp_buy_put - entry_price_put_bought)) if flag_buy_put == 1 else 0
            pnl_bc = (QUANTITY * (ltp_buy_call - entry_price_call_bought)) if flag_buy_call == 1 else 0
            
            comm_curr = 0
            if flag_sell_put == 1:
                comm_curr += commission(QUANTITY, ltp_put, entry_price_put_sold) 
                if not HEDGELESS_MODE:
                    comm_curr += commission(QUANTITY, entry_price_hedge_put, ltp_hedge_put) 
            if flag_sell_call == 1:
                comm_curr += commission(QUANTITY, ltp_call, entry_price_call_sold)
                if not HEDGELESS_MODE:
                    comm_curr += commission(QUANTITY, entry_price_hedge_call, ltp_hedge_call)
            if flag_buy_put == 1: comm_curr += commission(QUANTITY, entry_price_put_bought, ltp_buy_put)
            if flag_buy_call == 1: comm_curr += commission(QUANTITY, entry_price_call_bought, ltp_buy_call)
            
            gross_pnl = pnl_sp + pnl_sc + pnl_hp + pnl_hc + pnl_bp + pnl_bc
            current_net_pnl = gross_pnl - comm_curr
            total_net_pnl = current_net_pnl + final_realized_pnl

            if (
                skip_checking_till_hour_loss_bypass > 0
                and not risk_checks_enabled
                and total_net_pnl <= -(GLOBAL_MAX_LOSS * skip_checking_till_hour_loss_bypass)
            ):
                bypass_loss_limit = -(GLOBAL_MAX_LOSS * skip_checking_till_hour_loss_bypass)
                print(f"!!! SKIP-HOUR LOSS BYPASS TRIGGERED: Net PnL {total_net_pnl:.2f} <= {bypass_loss_limit:.2f} while normal risk checks are skipped !!!", flush=True)
                close_all_positions(f"Skip_Hour_Loss_Bypass_({total_net_pnl:.2f})")
                break
            
            # --- MINUTE LOG ---
            if check_dt.minute != last_print_minute:
                print(f"\n[STATUS {check_dt.strftime('%H:%M:00')}] Net PnL: {round(current_net_pnl + final_realized_pnl, 2)} (Gross: {round(gross_pnl + final_realized_pnl, 2)} - Comm: {round(comm_curr, 2)})", flush=True)
                print(f"STRIKES    | PUT: {entry_put_strike} PE ({sym_entry_put}) | CALL: {entry_call_strike} CE ({sym_entry_call})" + (f" | HEDGE PE: {hedge_put_strike} | HEDGE CE: {hedge_call_strike}" if not HEDGELESS_MODE else " | HEDGES: DISABLED") + (f" | BUY PE: {buy_strike_pe} | BUY CE: {buy_strike_ce}" if use_buy_legs else ""), flush=True)
                print(f"RISK INFO  | Put Thresh: {threshold_put:.2f} | Put Stop: {stop_put:.2f} | Call Thresh: {threshold_call:.2f} | Call Stop: {stop_call:.2f} | Global Stop: {-GLOBAL_MAX_LOSS:.2f} | Stops Active: {'YES' if risk_checks_enabled else 'NO (SKIPPED)'}", flush=True)
                if INDEX_BASED_ENTRY:
                    print(f"INDEX ENTRY | Index LTP: {ltp_index:.2f} | PUT thresh (>): {index_entry_threshold_put:.2f} | CALL thresh (<): {index_entry_threshold_call:.2f} | PUT cond: {put_entry_condition} | CALL cond: {call_entry_condition}", flush=True)
                
                if flag_sell_put == 1:
                    status_p = 'OPEN'
                    put_leg_pnl = pnl_sp + pnl_hp
                    put_main_pnl = pnl_sp
                    put_hedge_pnl = pnl_hp
                    put_main_price_label, put_main_price = 'LTP', ltp_put
                    put_hedge_price_label, put_hedge_price = 'LTP', ltp_hedge_put
                    put_main_exit_price, put_hedge_exit_price = 0, 0
                elif flag_sell_put == 2:
                    status_p = 'CLOSED'
                    put_leg_pnl = realized_put_leg_pnl
                    put_main_pnl = realized_put_main_pnl
                    put_hedge_pnl = realized_put_hedge_pnl
                    put_main_price_label, put_main_price = 'LTP', ltp_put
                    put_hedge_price_label, put_hedge_price = 'LTP', ltp_hedge_put
                    put_main_exit_price, put_hedge_exit_price = exit_price_put_sold, exit_price_hedge_put
                else:
                    status_p = 'WAIT'
                    put_leg_pnl = 0
                    put_main_pnl = 0
                    put_hedge_pnl = 0
                    put_main_price_label, put_main_price = 'LTP', ltp_put
                    put_hedge_price_label, put_hedge_price = 'LTP', ltp_hedge_put
                    put_main_exit_price, put_hedge_exit_price = 0, 0
                print(f"PUT SIDE  | Status: {status_p:<6} | Net Leg PnL: {round(put_leg_pnl, 2)}", flush=True)
                print(f"  > Main  | {put_main_price_label}: {put_main_price:<6} | Entry: {entry_price_put_sold:<6} | Exit: {put_main_exit_price:<6} | PnL: {round(put_main_pnl, 2)}", flush=True)
                print(f"  > Hedge | {put_hedge_price_label}: {put_hedge_price:<6} | Entry: {entry_price_hedge_put:<6} | Exit: {put_hedge_exit_price:<6} | PnL: {round(put_hedge_pnl, 2)}", flush=True)

                if flag_sell_call == 1:
                    status_c = 'OPEN'
                    call_leg_pnl = pnl_sc + pnl_hc
                    call_main_pnl = pnl_sc
                    call_hedge_pnl = pnl_hc
                    call_main_price_label, call_main_price = 'LTP', ltp_call
                    call_hedge_price_label, call_hedge_price = 'LTP', ltp_hedge_call
                    call_main_exit_price, call_hedge_exit_price = 0, 0
                elif flag_sell_call == 2:
                    status_c = 'CLOSED'
                    call_leg_pnl = realized_call_leg_pnl
                    call_main_pnl = realized_call_main_pnl
                    call_hedge_pnl = realized_call_hedge_pnl
                    call_main_price_label, call_main_price = 'LTP', ltp_call
                    call_hedge_price_label, call_hedge_price = 'LTP', ltp_hedge_call
                    call_main_exit_price, call_hedge_exit_price = exit_price_call_sold, exit_price_hedge_call
                else:
                    status_c = 'WAIT'
                    call_leg_pnl = 0
                    call_main_pnl = 0
                    call_hedge_pnl = 0
                    call_main_price_label, call_main_price = 'LTP', ltp_call
                    call_hedge_price_label, call_hedge_price = 'LTP', ltp_hedge_call
                    call_main_exit_price, call_hedge_exit_price = 0, 0
                print(f"CALL SIDE | Status: {status_c:<6} | Net Leg PnL: {round(call_leg_pnl, 2)}", flush=True)
                print(f"  > Main  | {call_main_price_label}: {call_main_price:<6} | Entry: {entry_price_call_sold:<6} | Exit: {call_main_exit_price:<6} | PnL: {round(call_main_pnl, 2)}", flush=True)
                print(f"  > Hedge | {call_hedge_price_label}: {call_hedge_price:<6} | Entry: {entry_price_hedge_call:<6} | Exit: {call_hedge_exit_price:<6} | PnL: {round(call_hedge_pnl, 2)}", flush=True)
                
                if use_buy_legs:
                    print(f"BUY PE    | LTP: {ltp_buy_put:<6} | Trigger: {(2 * initial_bp_price):<6.2f} | Entry: {entry_price_put_bought:<6} | PnL: {round(pnl_bp, 2)}", flush=True)
                    print(f"BUY CE    | LTP: {ltp_buy_call:<6} | Trigger: {(2 * initial_bc_price):<6.2f} | Entry: {entry_price_call_bought:<6} | PnL: {round(pnl_bc, 2)}", flush=True)
                last_print_minute = check_dt.minute

            # --- STOP LOSS CHECKS ---
            if risk_checks_enabled and flag_sell_put == 1 and ltp_put >= stop_put:
                print(f"!!! STOP LOSS TRIGGERED: PUT SIDE ({ltp_put} >= {stop_put:.2f}) !!!", flush=True)
                close_put_side("SL_Put", sl_price=stop_put) 
                
            if risk_checks_enabled and flag_sell_call == 1 and ltp_call >= stop_call:
                print(f"!!! STOP LOSS TRIGGERED: CALL SIDE ({ltp_call} >= {stop_call:.2f}) !!!", flush=True)
                close_call_side("SL_Call", sl_price=stop_call)

            # Re-calculate open pnl
            pnl_sp = (QUANTITY * (entry_price_put_sold - ltp_put)) if flag_sell_put == 1 else 0
            pnl_sc = (QUANTITY * (entry_price_call_sold - ltp_call)) if flag_sell_call == 1 else 0
            pnl_bp = (QUANTITY * (ltp_buy_put - entry_price_put_bought)) if flag_buy_put == 1 else 0
            pnl_bc = (QUANTITY * (ltp_buy_call - entry_price_call_bought)) if flag_buy_call == 1 else 0
            
            # --- GLOBAL TARGET CHECKS (MAIN STRIKES ONLY) ---
            open_main_gross = pnl_sp + pnl_sc + pnl_bp + pnl_bc
            
            open_main_comm = 0
            if flag_sell_put == 1: open_main_comm += commission(QUANTITY, ltp_put, entry_price_put_sold)
            if flag_sell_call == 1: open_main_comm += commission(QUANTITY, ltp_call, entry_price_call_sold)
            if flag_buy_put == 1: open_main_comm += commission(QUANTITY, entry_price_put_bought, ltp_buy_put)
            if flag_buy_call == 1: open_main_comm += commission(QUANTITY, entry_price_call_bought, ltp_buy_call)
            
            net_main_total = (open_main_gross - open_main_comm) + final_realized_main_pnl
            
            if risk_checks_enabled and (net_main_total > GLOBAL_PROFIT_TARGET or net_main_total < -GLOBAL_MAX_LOSS):
                close_all_positions(f"Global_Target_({net_main_total:.2f})")
                break

        # 8. End of Day Cleanup
        print(f"\n{'='*30}", flush=True)
        print(f"FINAL DAY PNL: {final_realized_pnl:.2f}", flush=True)
        print(f"{'='*30}\n", flush=True)
        
        tradebook_df.to_csv(TRADEBOOK_CSV_PATH, index=False)
        print("Tradebook Saved to CSV.", flush=True)
        
        final_pnl_df = pd.read_csv(DAILY_PNL_CSV_PATH)
        final_pnl_df.loc[len(final_pnl_df)] = [
            get_now_str(), entry_call_strike, entry_put_strike, buy_strike_ce, buy_strike_pe, 
            entry_price_call_sold, entry_price_put_sold, entry_price_call_bought, entry_price_put_bought, 
            exit_price_call_sold, exit_price_put_sold, exit_price_call_bought, exit_price_put_bought, 
            final_realized_pnl
        ]
        final_pnl_df.to_csv(DAILY_PNL_CSV_PATH, index=False)
        print("Daily Process Finished Successfully.", flush=True)

    except Exception as e:
        print(f"\nCRITICAL PROCESS CRASH: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            input("Press Enter to Exit Process...")
        except: pass
        
# ==============================================================================
# 5. PHOENIX SUPERVISOR (Pipe Proxy for IDLE Support + Safety Block)
# ==============================================================================
if __name__ == "__main__":
    
    # 1. AM I THE WORKER?
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_process()
        
    # 2. I AM THE SUPERVISOR
    else:
        worker_process = None
        next_trading_dt, upcoming_day_config = get_next_trading_day_info()
        upcoming_live_mode = 'LIVE TRADING (REAL MONEY)' if upcoming_day_config.get('live_mode', 0) else 'PAPER TRADING (SIMULATION)'
        print(f"Supervisor Active. Waiting for {TOKEN_SWAP_TIME} to start...", flush=True)
        print(f"Next Trading Day Mode ({next_trading_dt.strftime('%Y-%m-%d')}): {upcoming_live_mode}", flush=True)
        
        try:
            while True:
                # Check Start Time
                if datetime.now().strftime("%H:%M") == TOKEN_SWAP_TIME:
                    
                    # Weekend Check
                    if datetime.now().weekday() in [5, 6]:
                        print(f"Today is Weekend. Skipping.", flush=True)
                        time.sleep(70)
                        continue

                    print(f"\n{'*'*40}", flush=True)
                    print(f"LAUNCHING DAILY TRADING WORKER: {datetime.now().date()}", flush=True)
                    launch_day_config = get_day_config_for_day_index(datetime.now().weekday())
                    launch_live_mode = 'LIVE TRADING (REAL MONEY)' if launch_day_config.get('live_mode', 0) else 'PAPER TRADING (SIMULATION)'
                    print(f"DAY LIVE MODE: {launch_live_mode}", flush=True)
                    print(f"{'*'*40}\n", flush=True)

                    try:
                        # START WORKER WITH PIPE
                        worker_process = subprocess.Popen(
                            [sys.executable, __file__, "--worker"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1
                        )

                        # PROXY LOOP: READ FROM WORKER -> PRINT TO IDLE
                        for line in worker_process.stdout:
                            print(line, end='', flush=True)
                        
                        # Wait for clean exit
                        worker_process.wait()

                    except Exception as e:
                        print(f"Worker Crash/Interruption: {e}", flush=True)
                    
                    print(f"\nWorker Finished. Supervisor sleeping until tomorrow.", flush=True)
                    worker_process = None # Reset handle
                    time.sleep(70) 
                
                time.sleep(30)

        except KeyboardInterrupt:
            print("\nSupervisor stopping by User Request (Ctrl+C).", flush=True)
        
        finally:
            # SAFETY BLOCK: KILL ORPHAN WORKER IF IDLE RESTARTS
            if worker_process and worker_process.poll() is None:
                print("\n[SAFETY] Killing background Worker process...", flush=True)
                try:
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
                except:
                    worker_process.kill()
                print("[SAFETY] Worker terminated.", flush=True)

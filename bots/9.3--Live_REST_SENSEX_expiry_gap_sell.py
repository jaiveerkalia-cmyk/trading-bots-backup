import logging
from kiteconnect import KiteTicker
# logging.basicConfig(level=logging.DEBUG)
import threading
import time
import datetime
from collections import OrderedDict
import math
import pandas as pd
from pprint import pprint
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from blackscholes import *
import talib
import requests
import pyotp
import pytz

# =============================================================================
# ######+  ######+ ######+ ########+    #######+##+      #####+  ######+ #######+
# CONFIG FLAGS - EDIT HERE
# =============================================================================

# --- SESSION TIMING ---
# Script waits until this exact HH:MM before entering sell_fn each day.
# ATR stops (if enabled) are calculated BEFORE this wait so they are ready.
SESSION_START_TIME = '09:16'   # format: 'HH:MM'  -- when sell_fn begins trading
SESSION_PREP_TIME  = '09:00'   # format: 'HH:MM'  -- when ATR calc + auth runs (before start)
EOD_TIME           = '15:19'   # format: 'HH:MM'  -- end-of-day forced exit time

# --- ATM FINDER MODE ---
# False -> Simple rounding to nearest strike_difference  (fast, default)
# True  -> Batch-fetch CE/PE LTPs for nearby strikes and pick the one with
#         the minimum |CE - PE| premium difference (true delta-neutral ATM)
ADVANCED_ATM_FINDER = True

# How many strikes on each side of the simple ATM to scan (step = strike_difference)
# e.g. ATM_SCAN_RANGE = 3  ->  scans ATM-3*100 ... ATM+3*100  (7 strikes total)
ATM_SCAN_RANGE = 4   # increase for wider markets, decrease for speed

# --- STRIKE BALANCE MODE ---
# False -> Use entry_put / entry_call as-is (no imbalance adjustment at all)
# True  -> After strike selection, iteratively compare CE and PE premiums.
#         If either leg is > STRIKE_BALANCE_THRESHOLD more expensive than the
#         other, the cheaper leg's strike is moved one step further ITM and
#         re-fetched. Repeats until balanced or STRIKE_BALANCE_MAX_ITERATIONS hit.
STRIKE_BALANCE_MODE           = True
STRIKE_BALANCE_THRESHOLD      = 0.25   # 25% -- imbalanced if price > (1+this) x other
STRIKE_BALANCE_MAX_ITERATIONS = 5      # safety cap to prevent infinite loops

# --- ATR STOP MODE ---
# False -> Use the fixed entry_price_gap / stop_per_lot / profit_per_lot declared below
# True  -> Fetch hourly SENSEX OHLC from Kite historical API, compute ATR (ATR_PERIOD
#         candles), smooth with EMA (ATR_EMA_WINDOW), then OVERRIDE the fixed values:
#           stop_per_lot  = latest_atr_ema * ATR_STOP_MULTIPLIER
#           entry_gap     = latest_atr_ema / ATR_ENTRY_GAP_DIVISOR
#         Calculated BEFORE SESSION_START_TIME using the previous day's last candle.
#         Falls back to fixed values gracefully if the historical fetch fails.
ATR_STOP_MODE            = True
ATR_PERIOD               = 10    # ATR rolling window (candles)
ATR_EMA_WINDOW           = 20     # EMA span smoothed over the ATR series
ATR_STOP_MULTIPLIER      = 10   # stop_per_lot  = latest_atr_ema x this
ATR_ENTRY_GAP_DIVISOR    = 20   # entry_gap     = latest_atr_ema / this
ATR_LOOKBACK_DAYS        = 300    # calendar days of hourly history to pull

# --- SKIP CHECKING UNTIL HOUR ---
# False -> Stop-loss and target checks begin immediately after positions are taken
# True  -> All stop-loss / target / EOD exit checks are suppressed until the clock
#         reaches SKIP_CHECK_UNTIL_HOUR (wall-clock hour, 24h IST).
#         e.g. SKIP_CHECK_UNTIL_HOUR = 9  -> checks start from 10:00:00 onwards
#         Entry checks (sell trigger) are NOT affected -- they always run.
SKIP_CHECK_UNTIL_HOUR_ENABLED = True
SKIP_CHECK_UNTIL_HOUR         = 10      # checks begin once datetime.now().hour > this

# =============================================================================
# STRATEGY PARAMETERS
# =============================================================================
scrip          = 'SENSEX'
ind            = 'SENSEX'
min_delta      = 0.15
sObject        = slice(16)
day_off        = 0
strike_gap     = 10
min_buy_price  = 15
entry_strike_gap   = 2
strike_difference  = 100
strike_hedge_gap   = 10
lots_num       = 1
lot_size       = 20
lot_reducer    = 0.1

# --- FIXED STOP / PROFIT / ENTRY GAP ---
# Used directly when ATR_STOP_MODE=False.
# When ATR_STOP_MODE=True these are the fallback values if ATR fetch fails,
# and profit_per_lot is always used as-is (ATR mode only overrides stop/entry_gap).
#
#   entry_gap      -- % drop in option price below initial price that triggers entry
#   stop_per_lot   -- Rs loss per lot at which the stop-loss exit fires
#                     (loss_target = stop_per_lot * lots)
#   profit_per_lot -- Rs profit per lot at which the profit target fires
#                     (profit_target = profit_per_lot * lots)
#   stop_multiplier-- pre-fill safety default: stop_price = threshold_price * this
#                     (used only before a fill is recorded; overwritten at fill time)
entry_gap       = 10.0   # percent drop from initial option price to trigger entry
stop_per_lot    = 3000   # Rs per lot stop loss
profit_per_lot  = 45000  # Rs per lot profit target
stop_multiplier = 1.3    # pre-fill safety: stop_price = threshold_price * stop_multiplier

# live_mode: 0 = paper trading (no orders placed), 1 = live
# Set per session type below -- expiry day and normal day can differ
live_mode_expiry     = 0   # live_mode on expiry day
live_mode_normal     = 0   # live_mode on normal day

gd_path = '/app/data/'

# =============================================================================
# GLOBAL STATE
# =============================================================================
call_token, put_token, hedge_call_token, hedge_put_token = 0, 0, 0, 0
mid_loop, prev_call_token, prev_put_token = 0, 0, 0
put_stop_price, call_stop_price = 0, 0

entry_call_price, entry_put_price = 0, 0
entry_put, entry_call = 0, 0
hedge_put, hedge_call = 0, 0
hedge_put_price, hedge_call_price = 0, 0
profit, final_close = 0, 0

current_call_buy, current_put_buy  = 0, 0
total_premium, opening_underlying_price = 0, 0
call_open, put_open = 1, 1

sell_put_flag, sell_call_flag = 0, 0
buy_put_flag,  buy_call_flag  = 0, 0

tick_list  = []
token_list = []


# =============================================================================
# HELPER: COMMISSION CALCULATOR
# =============================================================================
def commission(quantity, buy_price, sell_price):
    """
    Calculates total charges for BSE Equity Options (Sensex/Bankex)
    based on Zerodha's latest tariff and April 2026 STT rates.
    """
    buy_turnover  = quantity * buy_price
    sell_turnover = quantity * sell_price
    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage: Flat Rs. 20 per executed order (Buy + Sell = 40)
    zerodha_brokerage = 40

    # 2. STT: 0.15% on SELL side premium (Budget 2026 Update)
    stt = 0.0015 * sell_turnover

    # 3. BSE Transaction Charges: 0.0325% on premium turnover
    # (BSE is generally 3250 per Crore, which is 0.0325%)
    exchange_txn_charge = 0.000325 * total_turnover

    # 4. SEBI Charges: Rs. 10 per crore (0.000001)
    sebi_charges = 0.000001 * total_turnover

    # 5. GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    # GST does NOT apply to STT or Stamp Duty
    gst = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)

    # 6. Stamp Duty: 0.003% on BUY side premium only
    stamp_duty = 0.00003 * buy_turnover

    # 7. BSE IPFT: Usually negligible but calculated at 0.00001%
    ipft = 0.0000001 * total_turnover

    total_charges = (zerodha_brokerage + stt + exchange_txn_charge +
                     sebi_charges + gst + stamp_duty + ipft)

    return round(total_charges, 2)


# =============================================================================
# HELPER: FETCH ZERODHA INSTRUMENTS (with retry)
# =============================================================================
def get_contracts():
    with open('/app/config/' +'auth.txt', 'r') as f:
        api_data = f.read()

    kite = KiteConnect(api_key=api_data.split(',')[0])
    kite.set_access_token(api_data.split(',')[1])

    while True:
        try:
            orderedDictList = kite.instruments(exchange='NFO')
            break
        except Exception as e:
            print(f"[get_contracts] API error: {e} -- retrying in 10s")
            time.sleep(10)

    return pd.DataFrame(orderedDictList)


# =============================================================================
# HELPER: PLACE ORDER (3 retries)
# =============================================================================
def place_order(kite, sym, qty, side):

    # Format symbol for BFO LTP fetching
    ltp_sym = f"BFO:{sym}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    # --- Nested Helper 1: Fetch LTP ---
    def get_ltp():
        ltp_sleep = 0.5
        for attempt in range(10):
            try:
                resp = kite.ltp(ltp_sym)
                ltp = resp[ltp_sym]['last_price']
                if ltp > 0:
                    return ltp
            except Exception as e:
                print(f"    [order] LTP fetch error attempt {attempt + 1}: {e}")
            time.sleep(ltp_sleep)
            if attempt >= 2:
                ltp_sleep += 1.0
        return None

    # --- Nested Helper 2: Calculate Aggressive Limit Price ---
    def get_limit_price(current_ltp):
        if side == 'BUY':
            price = current_ltp * 1.10 if current_ltp > 50 else current_ltp + 5.0
        else:
            price = current_ltp * 0.90 if current_ltp > 50 else current_ltp - 5.0
        price = max(price, 0.05)
        return round(round(price / 0.05) * 0.05, 2)

    # 1. Fetch Initial LTP
    current_ltp = get_ltp()
    if current_ltp is None:
        print(f"    [order] order_placement_failed: Could not fetch initial LTP for {sym}")
        return

    # 2. Calculate Initial Limit Price
    limit_price = get_limit_price(current_ltp)

    # 3. Place Initial Limit Order (10 retries, dynamic sleep)
    order_id = None
    place_retries = 0
    place_sleep = 1.0

    while place_retries < 10:
        try:
            order_id = kite.place_order(
                tradingsymbol    = sym,
                exchange         = 'BFO',
                transaction_type = order_side,
                quantity         = qty,
                order_type       = kite.ORDER_TYPE_LIMIT,
                price            = limit_price,
                variety          = kite.VARIETY_REGULAR,
                product          = kite.PRODUCT_MIS,
            )
            break
        except Exception as e:
            place_retries += 1
            print(f"    [order] Entry_order_error attempt {place_retries}: {e}")
            if place_retries == 10:
                print(f"    [order] order_placement_failed for {sym}")
                return
            time.sleep(place_sleep)
            if place_retries >= 3:
                place_sleep += 1.0

    if not order_id:
        return

    # 4. Check Fill Status and Chase Price if Partial/Unfilled
    max_modifications = 10
    mod_count = 0
    mod_sleep = 1.0

    while mod_count < max_modifications:
        time.sleep(mod_sleep)

        try:
            order_history = kite.order_history(order_id)
            latest_state  = order_history[-1]
            status        = latest_state['status']
            pending_qty   = latest_state.get('pending_quantity', 0)

            if status == 'COMPLETE':
                print(f"    [order] {side} {qty}x {sym} | LTP: {current_ltp:.2f}  limit: {limit_price:.2f}  id: {order_id}  [OK]")
                break

            elif status in ['REJECTED', 'CANCELLED']:
                print(f"    [order] Order {status} for {sym}. Reason: {latest_state.get('status_message', 'Unknown')}")
                break

            elif pending_qty > 0:
                print(f"    [order] Partial/No fill. Pending: {pending_qty}. Fetching LTP to modify...")
                new_ltp = get_ltp()
                if new_ltp:
                    new_limit_price = get_limit_price(new_ltp)
                    kite.modify_order(
                        variety    = kite.VARIETY_REGULAR,
                        order_id   = order_id,
                        order_type = kite.ORDER_TYPE_LIMIT,
                        price      = new_limit_price,
                    )
                    print(f"    [order] Modified to new limit: {new_limit_price:.2f}")
                else:
                    print(f"    [order] Could not fetch new LTP for modification. Retrying status check...")

        except Exception as e:
            print(f"    [order] Order_modification_error: {e}")

        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0

    if mod_count >= max_modifications:
        print(f"    [order] Warning: Max modifications ({max_modifications}) reached for {sym}. Order may still be pending.")


# =============================================================================
# HELPER: SAFE LTP FETCH (retries)
# =============================================================================
def safe_ltp(kite, symbol, exchange='BFO', retries=10, delay=1.0):
    """
    Fetch LTP for a single symbol with retries.
    Returns float price, or None on total failure.
    """
    full_sym = f"{exchange}:{symbol}"
    for attempt in range(1, retries + 1):
        try:
            result = kite.ltp(full_sym)
            return result[full_sym]['last_price']
        except Exception as e:
            print(f"    [ltp] attempt {attempt}/{retries} failed for {full_sym}: {e}")
            if attempt < retries:
                time.sleep(delay)
    print(f"    [ltp] CRITICAL: could not fetch LTP for {full_sym} after {retries} attempts")
    return None


def safe_batch_ltp(kite, symbols, retries=20, delay=1.0):
    """
    Batch fetch LTP for a list of 'EXCHANGE:SYMBOL' strings.
    Returns dict or empty dict on total failure.
    """
    for attempt in range(1, retries + 1):
        try:
            result = kite.ltp(symbols)
            return result
        except Exception as e:
            print(f"    [batch_ltp] attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(delay)
    print(f"    [batch_ltp] CRITICAL: batch fetch failed after {retries} attempts")
    return {}


# =============================================================================
# CORE: ATM STRIKE FINDER
# =============================================================================
def get_atm_strike(kite, current_price, zerodha_instruments_list):
    """
    Returns the ATM strike based on ADVANCED_ATM_FINDER flag.

    SIMPLE mode  (ADVANCED_ATM_FINDER=False):
        Rounds current_price to nearest strike_difference. O(1), no API call.

    ADVANCED mode (ADVANCED_ATM_FINDER=True):
        Scans +/-ATM_SCAN_RANGE strikes around the simple ATM.
        Batch-fetches CE + PE LTPs in ONE API call.
        Returns the strike where |CE_LTP - PE_LTP| is minimal (delta-neutral ATM).
    """
    simple_atm = int(round(current_price / strike_difference) * strike_difference)

    if not ADVANCED_ATM_FINDER:
        print(f"[ATM] Simple mode -> ATM = {simple_atm}  (underlying: {current_price})")
        return simple_atm

    # -- ADVANCED MODE ---------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"[ATM] Advanced finder | Underlying: {current_price:.2f} | Simple ATM: {simple_atm}")
    print(f"      Scanning {ATM_SCAN_RANGE} strikes each side (step={strike_difference})")
    print(f"{'-'*60}")

    start = time.time()

    # 1. Build symbol lists
    symbols_to_fetch = []
    strike_symbol_map = {}

    for offset in range(-ATM_SCAN_RANGE, ATM_SCAN_RANGE + 1):
        test_strike = simple_atm + offset * strike_difference
        try:
            filtered = zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(test_strike)
            ]['tradingsymbol'].values

            sym_put  = next(s for s in filtered if 'PE' in s)
            sym_call = next(s for s in filtered if 'CE' in s)

            symbols_to_fetch.extend([f'BFO:{sym_put}', f'BFO:{sym_call}'])
            strike_symbol_map[test_strike] = {
                'PE': f'BFO:{sym_put}',
                'CE': f'BFO:{sym_call}',
            }
        except StopIteration:
            print(f"      [ATM] Strike {test_strike} not found in instrument list -- skipping")
            continue
        except Exception as e:
            print(f"      [ATM] Error preparing strike {test_strike}: {e} -- skipping")
            continue

    if not symbols_to_fetch:
        print("[ATM] CRITICAL: No symbols found -- falling back to simple ATM")
        return simple_atm

    print(f"      Built symbol list: {len(symbols_to_fetch)//2} strikes x 2 options "
          f"= {len(symbols_to_fetch)} symbols")

    # 2. Batch fetch all LTPs in ONE call (20 retries)
    all_ltp = safe_batch_ltp(kite, symbols_to_fetch, retries=20, delay=1.0)

    if not all_ltp:
        print("[ATM] CRITICAL: Could not fetch quotes -- falling back to simple ATM")
        return simple_atm

    # 3. Find strike with minimum |CE - PE|
    print(f"\n  {'Strike':>8}  {'CE LTP':>10}  {'PE LTP':>10}  {'|CE-PE|':>10}  {'Flag'}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*4}")

    best_atm   = simple_atm
    best_diff  = float('inf')
    missing    = 0

    for offset in range(-ATM_SCAN_RANGE, ATM_SCAN_RANGE + 1):
        test_strike = simple_atm + offset * strike_difference
        if test_strike not in strike_symbol_map:
            continue

        sym_put  = strike_symbol_map[test_strike]['PE']
        sym_call = strike_symbol_map[test_strike]['CE']

        if sym_put not in all_ltp or sym_call not in all_ltp:
            missing += 1
            print(f"  {test_strike:>8}  {'N/A':>10}  {'N/A':>10}  {'N/A':>10}  <- missing")
            continue

        ce_ltp   = all_ltp[sym_call]['last_price']
        pe_ltp   = all_ltp[sym_put]['last_price']
        diff     = abs(ce_ltp - pe_ltp)
        marker   = " < BEST" if diff < best_diff else ""

        print(f"  {test_strike:>8}  {ce_ltp:>10.2f}  {pe_ltp:>10.2f}  {diff:>10.2f}{marker}")

        if diff < best_diff:
            best_diff = diff
            best_atm  = test_strike

    elapsed = time.time() - start
    print(f"\n  {'-'*55}")
    print(f"  Selected ATM : {best_atm}  (|CE-PE| = {best_diff:.2f})")
    print(f"  Time taken   : {elapsed:.3f}s  |  Strikes missing data: {missing}")
    print(f"{'='*60}\n")

    return best_atm


# =============================================================================
# HELPER: ATR-BASED STOP & ENTRY GAP CALCULATOR
# =============================================================================
def get_atr_stops(kite):
    """
    Fetches hourly SENSEX OHLC from the Kite historical API, computes ATR
    (period = ATR_PERIOD) and then smooths it with an EMA (window = ATR_EMA_WINDOW).
    Uses the last completed hourly candle BEFORE today's session open to avoid
    look-ahead bias (same logic as the caller's original snippet).

    Returns:
        (stop_per_lot, starting_gap, latest_atr_ema)  on success
        None                                           on any failure
    """
    print(f"\n{'='*60}")
    print(f"[ATR] ATR stop mode ON")
    print(f"      ATR period={ATR_PERIOD}  EMA window={ATR_EMA_WINDOW}  "
          f"Lookback={ATR_LOOKBACK_DAYS}d")
    print(f"      Multipliers -> stop_per_lot=ATRx{ATR_STOP_MULTIPLIER}  "
          f"entry_gap=ATR/{ATR_ENTRY_GAP_DIVISOR}")
    print(f"{'-'*60}")

    try:
        # -- 1. Resolve the BSE SENSEX instrument token ------------------------
        # The index instrument lives in the 'BSE' exchange list.
        for attempt in range(1, 6):
            try:
                all_instruments = kite.instruments(exchange='BSE')
                break
            except Exception as e:
                print(f"  [ATR] instrument fetch attempt {attempt}/5 failed: {e}")
                time.sleep(2)
        else:
            print("  [ATR] CRITICAL: could not fetch BSE instrument list")
            return None

        inst_df = pd.DataFrame(all_instruments)
        sensex_row = inst_df[inst_df['tradingsymbol'] == 'SENSEX']
        if sensex_row.empty:
            # Fallback: try 'BSE' tradingsymbol (exchange-level index token)
            sensex_row = inst_df[inst_df['tradingsymbol'] == 'BSE']
        if sensex_row.empty:
            print("  [ATR] CRITICAL: SENSEX instrument token not found in BSE list")
            return None

        instrument_token = int(sensex_row.iloc[0]['instrument_token'])
        print(f"  [ATR] Resolved instrument token: {instrument_token} "
              f"({sensex_row.iloc[0]['tradingsymbol']})")

        # -- 2. Fetch hourly historical OHLC -----------------------------------
        current_date = datetime.now(pytz.timezone('Asia/Kolkata'))
        to_date      = current_date
        from_date    = current_date - timedelta(days=ATR_LOOKBACK_DAYS)

        for attempt in range(1, 6):
            try:
                records = kite.historical_data(
                    instrument_token = instrument_token,
                    from_date        = from_date.strftime('%Y-%m-%d %H:%M:%S'),
                    to_date          = to_date.strftime('%Y-%m-%d %H:%M:%S'),
                    interval         = '60minute',
                    continuous       = False,
                    oi               = False,
                )
                break
            except Exception as e:
                print(f"  [ATR] historical_data attempt {attempt}/5 failed: {e}")
                time.sleep(3)
        else:
            print("  [ATR] CRITICAL: historical data fetch failed after 5 attempts")
            return None

        if not records:
            print("  [ATR] CRITICAL: historical API returned empty data")
            return None

        df_m = pd.DataFrame(records)
        # Kite returns 'date' as tz-aware datetime; normalise column name
        df_m.rename(columns={'date': 'date', 'open': 'open', 'high': 'high',
                              'low': 'low', 'close': 'close'}, inplace=True)
        df_m['date'] = pd.to_datetime(df_m['date'])

        # Ensure tz-aware for comparison
        if df_m['date'].dt.tz is None:
            df_m['date'] = df_m['date'].dt.tz_localize('Asia/Kolkata')

        print(f"  [ATR] Fetched {len(df_m)} hourly candles "
              f"({df_m['date'].iloc[0]} -> {df_m['date'].iloc[-1]})")

        # -- 3. Compute ATR with talib -----------------------------------------
        # talib.ATR needs numpy float64 arrays
        high  = df_m['high'].astype(float).values
        low   = df_m['low'].astype(float).values
        close = df_m['close'].astype(float).values

        atr_series = talib.ATR(high, low, close, timeperiod=ATR_PERIOD)
        df_m['atr'] = atr_series

        # -- 4. EMA on ATR -----------------------------------------------------
        df_m['atr_ema'] = df_m['atr'].ewm(
            span=ATR_EMA_WINDOW, adjust=False, min_periods=ATR_EMA_WINDOW
        ).mean()

        # -- 5. Pick the last completed candle strictly before today's date ----
        today_midnight = pd.to_datetime(current_date.date()).tz_localize(
            df_m['date'].dt.tz
        )
        prior_candles = df_m[df_m['date'] < today_midnight].dropna(subset=['atr_ema'])

        if prior_candles.empty:
            print("  [ATR] CRITICAL: no completed candles before today with valid atr_ema")
            return None

        latest_atr_ema = prior_candles.iloc[-1]['atr_ema']
        latest_candle_time = prior_candles.iloc[-1]['date']

        # -- 6. Derive strategy values -----------------------------------------
        # stop_per_lot : Rs loss per lot  (= atr_ema * ATR_STOP_MULTIPLIER)
        # entry_gap    : % drop from initial option price to trigger entry
        #                (= atr_ema / ATR_ENTRY_GAP_DIVISOR, already a percent)
        stop_per_lot_atr = latest_atr_ema * ATR_STOP_MULTIPLIER
        entry_gap_atr    = latest_atr_ema / ATR_ENTRY_GAP_DIVISOR

        # -- 7. Print diagnostic table -----------------------------------------
        recent = df_m[['date', 'high', 'low', 'close', 'atr', 'atr_ema']].tail(6)
        print(f"\n  Recent hourly ATR series (last {len(recent)} candles):")
        print(f"  {'Date':<25}  {'High':>8}  {'Low':>8}  {'Close':>8}  "
              f"{'ATR':>8}  {'ATR_EMA':>8}")
        print(f"  {'-'*25}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
        for _, row in recent.iterrows():
            marker = " < used" if row['date'] == latest_candle_time else ""
            atr_str     = f"{row['atr']:.2f}"     if not pd.isna(row['atr'])     else "  N/A  "
            atr_ema_str = f"{row['atr_ema']:.2f}" if not pd.isna(row['atr_ema']) else "  N/A  "
            print(f"  {str(row['date']):<25}  {row['high']:>8.2f}  {row['low']:>8.2f}  "
                  f"{row['close']:>8.2f}  {atr_str:>8}  {atr_ema_str:>8}{marker}")

        print(f"\n  {'-'*60}")
        print(f"  Reference candle : {latest_candle_time}")
        print(f"  Latest ATR EMA   : {latest_atr_ema:.4f}")
        print(f"  stop_per_lot     : {latest_atr_ema:.4f} x {ATR_STOP_MULTIPLIER} = {stop_per_lot_atr:.4f}")
        print(f"  entry_gap        : {latest_atr_ema:.4f} / {ATR_ENTRY_GAP_DIVISOR} = {entry_gap_atr:.4f}%")
        print(f"{'='*60}\n")

        return stop_per_lot_atr, entry_gap_atr, latest_atr_ema

    except Exception as e:
        print(f"  [ATR] Unexpected error in get_atr_stops: {e}")
        import traceback; traceback.print_exc()
        return None


# =============================================================================
# CORE: SELL FUNCTION
# =============================================================================
def sell_fn(kite, zerodha_instruments_list, expiry, atr_used=False,
            cur_stop_per_lot=None, cur_entry_gap=None):
    global total_premium, put_stop_price, call_stop_price
    global opening_underlying_price, call_open, put_open
    global call_token, put_token, hedge_call_token, hedge_put_token
    global current_call_buy, current_put_buy
    global entry_call_price, entry_put_price
    global entry_put, entry_call
    global hedge_put, hedge_call
    global hedge_put_price, hedge_call_price

    # Resolve effective stop/gap -- prefer values passed in from main(), else module-level
    eff_stop_per_lot = cur_stop_per_lot if cur_stop_per_lot is not None else stop_per_lot
    eff_entry_gap    = cur_entry_gap    if cur_entry_gap    is not None else entry_gap

    # Local state reset
    sell_put_flag = sell_call_flag = buy_put_flag = buy_call_flag = 0
    current_profit = final_profit = 0.0
    sell_put_profit = sell_call_profit = 0.0
    (initial_entry_put_price, initial_entry_call_price,
     entry_put_price, entry_call_price) = 0.0, 0.0, 0.0, 0.0
    (entry_hedge_put_price, entry_hedge_call_price) = 0.0, 0.0
    (exit_sell_call_price, exit_sell_put_price,
     exit_buy_call_price, exit_buy_put_price) = 0.0, 0.0, 0.0, 0.0
    buy_entry_call = buy_entry_put = 0.0
    buy_put_price = buy_call_price = 0.0
    current_entry_put_price = current_entry_call_price = 0.0
    threshold_call_price = threshold_put_price = 0.0
    stop_call_price = stop_put_price = 0.0

    intraday_positions = pd.read_csv(
        gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv')
    final_position_df = pd.read_csv(
        gd_path + 'SENSEX_buy_sell_results/Final_daily_pnl.csv')

    # -- Expiry day lot and live_mode adjustment -------------------------------
    is_expiry_day = datetime.now().date() == pd.to_datetime(expiry).date()
    if is_expiry_day:
        lots      = math.floor(1 * lots_num)
        live_mode = live_mode_expiry
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] EXPIRY DAY  -- lots={lots}  live_mode={live_mode}")
    else:
        lots      = lots_num
        live_mode = live_mode_normal
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Normal day  -- lots={lots}  live_mode={live_mode}")

    qty = lots * lot_size

    # -- stop_per_lot and entry_gap are set by the scheduler at SESSION_PREP_TIME
    # (via ATR calc when ATR_STOP_MODE=True, or fixed values otherwise).
    # atr_used flag is passed in so the summary print knows which mode ran.

    # -- Profit / loss targets -------------------------------------------------
    profit_target = profit_per_lot * lots
    loss_target   = eff_stop_per_lot * lots

    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Qty: {qty} | "
          f"Profit target: Rs{profit_target:,.0f} | Loss limit: Rs{loss_target:,.0f} | "
          f"Stop mode: {'ATR' if atr_used else 'Fixed'}")

    # -- Fetch underlying price ------------------------------------------------
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Fetching {scrip} spot price...")
    while True:
        price = safe_ltp(kite, scrip, exchange='BSE', retries=20, delay=1.0)
        if price is not None:
            current_price = price
            break
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Spot price fetch failed -- retrying...")
        time.sleep(2)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Underlying: {current_price:.2f}")

    # -- Determine ATM strike (simple or advanced) -----------------------------
    atm_strike = get_atm_strike(kite, current_price, zerodha_instruments_list)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Final ATM strike: {atm_strike}")

    entry_put  = atm_strike + strike_difference * entry_strike_gap
    entry_call = atm_strike - strike_difference * entry_strike_gap
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Initial entry -> Put strike: {entry_put}  Call strike: {entry_call}")

    # -- Fetch initial CE/PE prices (needed for STRIKE BALANCE MODE) ----------
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Fetching initial CE/PE prices...")
    sym_put  = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(entry_put)]['tradingsymbol'].values if 'PE' in s)
    sym_call = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(entry_call)]['tradingsymbol'].values if 'CE' in s)

    batch = safe_batch_ltp(kite, [f'BFO:{sym_put}', f'BFO:{sym_call}'], retries=10)
    put_price  = batch.get(f'BFO:{sym_put}',  {}).get('last_price', 0.0)
    call_price = batch.get(f'BFO:{sym_call}', {}).get('last_price', 0.0)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Initial prices -> Put@{entry_put}: {put_price:.2f}  Call@{entry_call}: {call_price:.2f}")

    # -- STRIKE BALANCE MODE ---------------------------------------------------
    # Iteratively moves the cheaper leg further ITM until premiums are within
    # STRIKE_BALANCE_THRESHOLD of each other (or max iterations reached).
    if STRIKE_BALANCE_MODE:
        print(f"\n[BALANCE] Strike balance mode ON "
              f"(threshold={STRIKE_BALANCE_THRESHOLD*100:.0f}%, max_iter={STRIKE_BALANCE_MAX_ITERATIONS})")
        print(f"  {'Iter':>4}  {'PE Strike':>10}  {'CE Strike':>10}  {'PE Price':>10}  {'CE Price':>10}  {'Status'}")
        print(f"  {'-'*4}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*20}")

        for bal_iter in range(1, STRIKE_BALANCE_MAX_ITERATIONS + 1):
            # Re-fetch both legs fresh each iteration
            _sym_put  = next(s for s in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(entry_put)]['tradingsymbol'].values if 'PE' in s)
            _sym_call = next(s for s in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(entry_call)]['tradingsymbol'].values if 'CE' in s)

            _batch     = safe_batch_ltp(kite, [f'BFO:{_sym_put}', f'BFO:{_sym_call}'], retries=10)
            put_price  = _batch.get(f'BFO:{_sym_put}',  {}).get('last_price', 0.0)
            call_price = _batch.get(f'BFO:{_sym_call}', {}).get('last_price', 0.0)

            if put_price == 0 or call_price == 0:
                print(f"  {bal_iter:>4}  {entry_put:>10}  {entry_call:>10}  "
                      f"{'ERR':>10}  {'ERR':>10}  <- price fetch failed, stopping balance")
                break

            upper = 1 + STRIKE_BALANCE_THRESHOLD

            if put_price > upper * call_price:
                # PE is the expensive leg -> move CE one step further ITM (lower strike)
                status = f"PE>{upper:.2f}xCE -> CE ITM (-{strike_difference})"
                print(f"  {bal_iter:>4}  {entry_put:>10}  {entry_call:>10}  "
                      f"{put_price:>10.2f}  {call_price:>10.2f}  {status}")
                entry_call -= strike_difference

            elif call_price > upper * put_price:
                # CE is the expensive leg -> move PE one step further ITM (higher strike)
                status = f"CE>{upper:.2f}xPE -> PE ITM (+{strike_difference})"
                print(f"  {bal_iter:>4}  {entry_put:>10}  {entry_call:>10}  "
                      f"{put_price:>10.2f}  {call_price:>10.2f}  {status}")
                entry_put += strike_difference

            else:
                # Balanced -- exit loop
                print(f"  {bal_iter:>4}  {entry_put:>10}  {entry_call:>10}  "
                      f"{put_price:>10.2f}  {call_price:>10.2f}  [OK] Balanced")
                break
        else:
            # Loop exhausted without reaching balance
            print(f"  [BALANCE] [!] Max iterations reached -- proceeding with current strikes")

        print(f"[BALANCE] Final strikes -> Entry Put: {entry_put}  Entry Call: {entry_call}\n")

    # -- Compute hedge strikes -------------------------------------------------
    hedge_put  = entry_put  - strike_hedge_gap * strike_difference
    hedge_call = entry_call + strike_hedge_gap * strike_difference
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Hedge strikes -> Put hedge: {hedge_put}  Call hedge: {hedge_call}")

    # -- Batch fetch all 4 leg prices -----------------------------------------
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Fetching all 4 leg prices (batch)...")
    sym_put        = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(entry_put)]['tradingsymbol'].values if 'PE' in s)
    sym_hedge_put  = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(hedge_put)]['tradingsymbol'].values if 'PE' in s)
    sym_call       = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(entry_call)]['tradingsymbol'].values if 'CE' in s)
    sym_hedge_call = next(s for s in zerodha_instruments_list[
        zerodha_instruments_list['strike'] == int(hedge_call)]['tradingsymbol'].values if 'CE' in s)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Symbols -> Sell Put: {sym_put}  Hedge Put: {sym_hedge_put}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Symbols -> Sell Call: {sym_call}  Hedge Call: {sym_hedge_call}")

    all_syms = [f'BFO:{sym_put}', f'BFO:{sym_hedge_put}',
                f'BFO:{sym_call}', f'BFO:{sym_hedge_call}']
    prices   = safe_batch_ltp(kite, all_syms, retries=10)

    initial_entry_put_price  = prices.get(f'BFO:{sym_put}',  {}).get('last_price', 0.0)
    initial_entry_call_price = prices.get(f'BFO:{sym_call}', {}).get('last_price', 0.0)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sell_fn] Initial prices -> Entry Put: {initial_entry_put_price:.2f}  "
          f"Entry Call: {initial_entry_call_price:.2f}")

    # -- Entry thresholds & stops ----------------------------------------------
    # eff_entry_gap is a percent value: threshold = initial_price * (1 - eff_entry_gap/100)
    threshold_put_price  = initial_entry_put_price  * (1 - eff_entry_gap / 100)
    threshold_call_price = initial_entry_call_price * (1 - eff_entry_gap / 100)
    # Pre-fill safety stop using stop_multiplier x threshold.
    # Overwritten at actual fill time in the entry trigger blocks below.
    stop_put_price  = threshold_put_price  * stop_multiplier
    stop_call_price = threshold_call_price * stop_multiplier

    print(f"\n{'-'*60}")
    print(f"  Entry Put  : {entry_put}  (hedge: {hedge_put})")
    print(f"  Entry Call : {entry_call}  (hedge: {hedge_call})")
    print(f"  Stop mode  : {'ATR-based' if atr_used else 'Fixed'}")
    print(f"  entry_gap  : {eff_entry_gap:.4f}%  |  stop_per_lot (loss_target only): Rs{eff_stop_per_lot:.2f}")
    print(f"  Initial PE : {initial_entry_put_price:.2f}  |  Threshold: {threshold_put_price:.2f}  |  Stop: {stop_put_price:.2f}")
    print(f"  Initial CE : {initial_entry_call_price:.2f}  |  Threshold: {threshold_call_price:.2f}  |  Stop: {stop_call_price:.2f}")
    print(f"  loss_target: Rs{loss_target:,.0f}  |  profit_target: Rs{profit_target:,.0f}")
    print(f"{'-'*60}\n")

    # =========================================================================
    # MAIN MONITORING LOOP
    # =========================================================================
    while True:
        now = datetime.now()
        sleep_sec = max(0, 62 - now.second)
        time.sleep(sleep_sec)
        now = datetime.now()

        # -- Batch fetch current prices ----------------------------------------
        prices = safe_batch_ltp(kite, [f'BFO:{sym_put}', f'BFO:{sym_call}'], retries=10)
        current_entry_put_price  = prices.get(f'BFO:{sym_put}',  {}).get('last_price', current_entry_put_price)
        current_entry_call_price = prices.get(f'BFO:{sym_call}', {}).get('last_price', current_entry_call_price)

        # -- Entry check: SELL PUT ---------------------------------------------
        if current_entry_put_price < threshold_put_price and sell_put_flag == 0:
            print(f"\n[ENTRY] PUT SELL TRIGGERED @ {now}")
            print(f"        Threshold: {threshold_put_price:.2f}  Current: {current_entry_put_price:.2f}")

            if live_mode == 1:
                place_order(kite, sym_hedge_put, qty, 'BUY')
                place_order(kite, sym_put, qty, 'SELL')

            hedge_prices = safe_batch_ltp(kite, [f'BFO:{sym_put}', f'BFO:{sym_hedge_put}'], retries=10)
            entry_put_price       = hedge_prices.get(f'BFO:{sym_put}',      {}).get('last_price', current_entry_put_price)
            entry_hedge_put_price = hedge_prices.get(f'BFO:{sym_hedge_put}',{}).get('last_price', 0.0)

            print(f"        Entry PE: {entry_put_price:.2f}  Hedge PE: {entry_hedge_put_price:.2f}")
            intraday_positions.loc[intraday_positions.shape[0]] = [
                str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                entry_put, 'PE', initial_entry_put_price,
                entry_put_price, 0, 0, final_profit, 'Open - Put_Sold'
            ]
            intraday_positions.to_csv(
                gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_put_flag = 1

        # -- Entry check: SELL CALL --------------------------------------------
        if current_entry_call_price < threshold_call_price and sell_call_flag == 0:
            print(f"\n[ENTRY] CALL SELL TRIGGERED @ {now}")
            print(f"        Threshold: {threshold_call_price:.2f}  Current: {current_entry_call_price:.2f}")

            if live_mode == 1:
                place_order(kite, sym_hedge_call, qty, 'BUY')
                place_order(kite, sym_call, qty, 'SELL')

            hedge_prices = safe_batch_ltp(kite, [f'BFO:{sym_call}', f'BFO:{sym_hedge_call}'], retries=10)
            entry_call_price       = hedge_prices.get(f'BFO:{sym_call}',       {}).get('last_price', current_entry_call_price)
            entry_hedge_call_price = hedge_prices.get(f'BFO:{sym_hedge_call}', {}).get('last_price', 0.0)

            print(f"        Entry CE: {entry_call_price:.2f}  Hedge CE: {entry_hedge_call_price:.2f}")
            intraday_positions.loc[intraday_positions.shape[0]] = [
                str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                entry_call, 'CE', initial_entry_call_price,
                entry_call_price, 0, 0, final_profit, 'Open - Call_Sold'
            ]
            intraday_positions.to_csv(
                gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_call_flag = 1

        # -- P&L calculation ---------------------------------------------------
        if sell_put_flag  == 1:
            sell_put_profit  = qty * (entry_put_price  - current_entry_put_price)  \
                               - commission(qty, current_entry_put_price, entry_put_price)
        if sell_call_flag == 1:
            sell_call_profit = qty * (entry_call_price - current_entry_call_price) \
                               - commission(qty, current_entry_call_price, entry_call_price)

        current_profit = sell_put_profit + sell_call_profit

        print(f"[{now.strftime('%H:%M:%S')}] "
              f"PE: {current_entry_put_price:.2f} (thr: {threshold_put_price:.2f}  stop: {stop_put_price:.2f})  "
              f"CE: {current_entry_call_price:.2f} (thr: {threshold_call_price:.2f}  stop: {stop_call_price:.2f}) | "
              f"PE_P&L: {sell_put_profit:+,.0f}  CE_P&L: {sell_call_profit:+,.0f} | "
              f"Total: {current_profit:+,.0f}  final: {final_profit:+,.0f}  loss_target: -{loss_target:,.0f}")

        # -- Stop/target checks: honour SKIP_CHECK_UNTIL_HOUR -----------------
        checks_suppressed = (
            SKIP_CHECK_UNTIL_HOUR_ENABLED and
            datetime.now().hour <= SKIP_CHECK_UNTIL_HOUR
        )
        if checks_suppressed:
            print(f"         +- [checks suppressed until {SKIP_CHECK_UNTIL_HOUR + 1:02d}:00:00]")
            continue

        # -- Stop-loss: PUT ----------------------------------------------------
        if sell_put_flag == 1 and current_entry_put_price > stop_put_price:
            exit_sell_put_price = current_entry_put_price
            print(f"\n[STOP] [X] PUT STOP HIT @ {now} | Price: {exit_sell_put_price:.2f} > Stop: {stop_put_price:.2f}")

            if live_mode == 1:
                place_order(kite, sym_put,       qty, 'BUY')
                time.sleep(1.5)
                place_order(kite, sym_hedge_put, qty, 'SELL')

            hp = safe_ltp(kite, sym_hedge_put, exchange='BFO', retries=10)
            exit_hedge_put_price = hp if hp is not None else entry_hedge_put_price
            hedge_profit = qty * (exit_hedge_put_price - entry_hedge_put_price) \
                           - commission(qty, entry_hedge_put_price, exit_hedge_put_price)
            final_profit += hedge_profit

            print(f"[STOP] Sell_Put_Stopped | Sell P&L: {sell_put_profit:+,.0f} | "
                  f"Hedge P&L: {hedge_profit:+,.0f} | Running Final: {final_profit:+,.0f}")

            intraday_positions.loc[intraday_positions.shape[0]] = [
                str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                entry_put, 'PE', initial_entry_put_price,
                entry_put_price, current_entry_put_price,
                sell_put_profit, final_profit, 'Close - Sell_Put_Stopped'
            ]
            intraday_positions.to_csv(
                gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_put_flag = 2
            continue

        # -- Stop-loss: CALL ---------------------------------------------------
        if sell_call_flag == 1 and current_entry_call_price > stop_call_price:
            exit_sell_call_price = current_entry_call_price
            print(f"\n[STOP] [X] CALL STOP HIT @ {now} | Price: {exit_sell_call_price:.2f} > Stop: {stop_call_price:.2f}")

            if live_mode == 1:
                place_order(kite, sym_call,       qty, 'BUY')
                time.sleep(1.5)
                place_order(kite, sym_hedge_call, qty, 'SELL')

            hp = safe_ltp(kite, sym_hedge_call, exchange='BFO', retries=10)
            exit_hedge_call_price = hp if hp is not None else entry_hedge_call_price
            hedge_profit = qty * (exit_hedge_call_price - entry_hedge_call_price) \
                           - commission(qty, entry_hedge_call_price, exit_hedge_call_price)
            final_profit += hedge_profit

            print(f"[STOP] Sell_Call_Stopped | Sell P&L: {sell_call_profit:+,.0f} | "
                  f"Hedge P&L: {hedge_profit:+,.0f} | Running Final: {final_profit:+,.0f}")

            intraday_positions.loc[intraday_positions.shape[0]] = [
                str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                entry_call, 'CE', initial_entry_call_price,
                entry_call_price, current_entry_call_price,
                sell_call_profit, final_profit, 'Close - Sell_Call_Stopped'
            ]
            intraday_positions.to_csv(
                gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_call_flag = 2
            continue

        # -- Target / Loss / EOD exit ------------------------------------------
        eod_h, eod_m = int(EOD_TIME.split(':')[0]), int(EOD_TIME.split(':')[1])
        eod = datetime.now().hour == eod_h and datetime.now().minute >= eod_m
        target_hit  = current_profit > profit_target
        loss_hit    = final_profit   < -loss_target

        if target_hit or loss_hit or eod:
            if eod:
                reason = "EOD"
            elif target_hit:
                reason = "TARGET"
            else:
                reason = "LOSS_TARGET"
            print(f"\n[EXIT] === ALL POSITIONS CLOSE | Reason: {reason} @ {now} ===")
            print(f"       current_profit: Rs{current_profit:+,.0f}  final_profit: Rs{final_profit:+,.0f}  "
                  f"(profit_target: Rs{profit_target:,}  loss_target: -Rs{loss_target:,})")

            final_profit += current_profit

            if sell_put_flag == 1:
                exit_sell_put_price = current_entry_put_price
                if live_mode == 1:
                    place_order(kite, sym_put,       qty, 'BUY')
                    time.sleep(1.5)
                    place_order(kite, sym_hedge_put, qty, 'SELL')

                hp = safe_ltp(kite, sym_hedge_put, exchange='BFO', retries=10)
                exit_hedge_put_price = hp if hp is not None else entry_hedge_put_price
                hedge_profit = qty * (exit_hedge_put_price - entry_hedge_put_price) \
                               - commission(qty, entry_hedge_put_price, exit_hedge_put_price)
                final_profit += hedge_profit

                print(f"  [PUT]  Sell PE closed @ {exit_sell_put_price:.2f} | "
                      f"Hedge exit: {exit_hedge_put_price:.2f} | Hedge P&L: {hedge_profit:+,.0f}")
                intraday_positions.loc[intraday_positions.shape[0]] = [
                    str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                    entry_put, 'PE', initial_entry_put_price,
                    entry_put_price, current_entry_put_price,
                    sell_put_profit, final_profit, 'Close - Sell_Put_Closed'
                ]
                intraday_positions.to_csv(
                    gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)

            if sell_call_flag == 1:
                exit_sell_call_price = current_entry_call_price
                if live_mode == 1:
                    place_order(kite, sym_call,       qty, 'BUY')
                    time.sleep(1.5)
                    place_order(kite, sym_hedge_call, qty, 'SELL')

                hp = safe_ltp(kite, sym_hedge_call, exchange='BFO', retries=10)
                exit_hedge_call_price = hp if hp is not None else entry_hedge_call_price
                hedge_profit = qty * (exit_hedge_call_price - entry_hedge_call_price) \
                               - commission(qty, entry_hedge_call_price, exit_hedge_call_price)
                final_profit += hedge_profit

                print(f"  [CALL] Sell CE closed @ {exit_sell_call_price:.2f} | "
                      f"Hedge exit: {exit_hedge_call_price:.2f} | Hedge P&L: {hedge_profit:+,.0f}")
                intraday_positions.loc[intraday_positions.shape[0]] = [
                    str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                    entry_call, 'CE', initial_entry_call_price,
                    entry_call_price, current_entry_call_price,
                    sell_call_profit, final_profit, 'Close - Sell_Call_Closed'
                ]
                intraday_positions.to_csv(
                    gd_path + 'SENSEX_buy_sell_results/Intraday_options_tradebook.csv', index=False)

            print(f"\n  ## FINAL P&L: Rs{final_profit:+,.2f}")
            print(f"  ======================================================")

            final_position_df.loc[final_position_df.shape[0]] = [
                str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0],
                entry_call, entry_put, buy_entry_call, buy_entry_put,
                entry_call_price, entry_put_price, buy_put_price, buy_call_price,
                exit_sell_call_price, exit_sell_put_price,
                exit_buy_call_price, exit_buy_put_price,
                final_profit
            ]
            final_position_df.to_csv(
                gd_path + 'SENSEX_buy_sell_results/Final_daily_pnl.csv', index=False)
            return


# =============================================================================
# MAIN SCHEDULER
# =============================================================================
def main():
    # Working copies of stop/gap -- updated by ATR calc each morning
    cur_stop_per_lot = stop_per_lot
    cur_entry_gap    = entry_gap

    _prep_done_today    = None
    _session_done_today = None

    _kite                     = None
    _zerodha_instruments_list = None
    _expiry                   = None
    _atr_used                 = False

    _prep_h  = int(SESSION_PREP_TIME.split(':')[0])
    _prep_m  = int(SESSION_PREP_TIME.split(':')[1])
    _start_h = int(SESSION_START_TIME.split(':')[0])
    _start_m = int(SESSION_START_TIME.split(':')[1])

    print('[MAIN] Script started')

    # If the script is launched after SESSION_START_TIME, today's session is
    # already over -- wait until tomorrow's SESSION_PREP_TIME before doing anything.
    now = datetime.now()
    already_past_start = (
        now.hour > _start_h or (now.hour == _start_h and now.minute >= _start_m)
    )
    if already_past_start and now.strftime('%w') not in ('0', '6'):
        tomorrow = now.date() + timedelta(days=1)
        # Skip to next weekday
        while tomorrow.strftime('%w') in ('0', '6'):
            tomorrow = tomorrow + timedelta(days=1)
        next_prep = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, _prep_h, _prep_m, 0
        )
        wait_secs = (next_prep - datetime.now()).total_seconds()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] Started after session time -- "
              f"waiting until {next_prep.strftime('%Y-%m-%d %H:%M')} ({wait_secs/3600:.1f}h)")
        time.sleep(max(0, wait_secs))

    while True:
        time.sleep(5)
        now = datetime.now()
        current_day = now.strftime('%w')

        if current_day in ('0', '6'):
            continue

        today = now.date()

        prep_time_reached = (
            now.hour > _prep_h or (now.hour == _prep_h and now.minute >= _prep_m)
        )
        session_time_reached = (
            now.hour > _start_h or (now.hour == _start_h and now.minute >= _start_m)
        )

        # -- PREP PHASE --------------------------------------------------------
        if prep_time_reached and _prep_done_today != today:
            _prep_done_today = today
            print(f"\n[{now.strftime('%H:%M:%S')}] [MAIN] PREP START")

            with open('/app/config/' + 'auth.txt', 'r') as f:
                api_data = f.read()

            _kite = KiteConnect(api_key=api_data.split(',')[0])
            _kite.set_access_token(api_data.split(',')[1])
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] KiteConnect authenticated")

            _zerodha_instruments_list = pd.DataFrame(_kite.instruments())
            _zerodha_instruments_list = _zerodha_instruments_list[
                (_zerodha_instruments_list['name']    == ind) &
                (_zerodha_instruments_list['segment'] == 'BFO-OPT')
            ].reset_index(drop=True)
            _zerodha_instruments_list = _zerodha_instruments_list[
                _zerodha_instruments_list['expiry'] == _zerodha_instruments_list['expiry'].iloc[0]
            ]
            _expiry = _zerodha_instruments_list['expiry'].iloc[0]

            print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] Instruments loaded: "
                  f"{len(_zerodha_instruments_list)} rows | Expiry: {_expiry} | "
                  f"ADVANCED_ATM_FINDER: {ADVANCED_ATM_FINDER} | ATR_STOP_MODE: {ATR_STOP_MODE}")

            # ATR calculation
            cur_stop_per_lot = stop_per_lot   # reset to fixed defaults first
            cur_entry_gap    = entry_gap
            _atr_used        = False
            if ATR_STOP_MODE:
                atr_result = get_atr_stops(_kite)
                if atr_result is not None:
                    cur_stop_per_lot, cur_entry_gap, _latest_atr_ema = atr_result
                    _atr_used = True
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [ATR] Active -- "
                          f"stop_per_lot={cur_stop_per_lot:.2f}  entry_gap={cur_entry_gap:.4f}%  "
                          f"(ATR EMA={_latest_atr_ema:.2f})")
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [ATR] Fetch failed -- "
                          f"using fixed: stop_per_lot={cur_stop_per_lot:.2f}  "
                          f"entry_gap={cur_entry_gap:.2f}%")

            print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] PREP DONE -- "
                  f"waiting for {SESSION_START_TIME} to start session")

        # -- SESSION PHASE -----------------------------------------------------
        if session_time_reached and _session_done_today != today and _prep_done_today == today:
            # Precise wait: sleep until exactly SESSION_START_TIME HH:MM:00
            now = datetime.now()
            target = now.replace(hour=_start_h, minute=_start_m, second=0, microsecond=0)
            wait = (target - now).total_seconds()
            if wait > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] Sleeping {wait:.1f}s for exact session start...")
                time.sleep(wait)
            _session_done_today = today
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [MAIN] SESSION START")
            sell_fn(_kite, _zerodha_instruments_list, _expiry,
                    atr_used=_atr_used,
                    cur_stop_per_lot=cur_stop_per_lot,
                    cur_entry_gap=cur_entry_gap)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [MAIN] SESSION END\n")


if __name__ == '__main__':
    main()

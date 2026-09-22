import os
import sys
import time
import math
import json
import csv
import gc
import uuid
import subprocess
import threading
from datetime import datetime, timedelta, date, time as dtime
import pytz
import numpy as np
import pandas as pd
from kiteconnect import KiteConnect
from redis_tick_client import RedisTickClient

# ============================================================================
# 1. STRATEGY CONFIGURATION & SYSTEM PARAMETERS
# ============================================================================
symbol = 'NIFTY'
scrip = 'NIFTY 50'
ind = 'NIFTY'
lot_size = 65
strike_step = 100
setup_cost = 150000
ratio_multiplier = 1
exchange = 'NSE'
live_mode = 0  # 0 = Paper Trading (Default), 1 = Live Trading

# Execution Schedule & Frequency Timers
start_time = '15:15'
rollover_time = '15:15'
pnl_check_start_time = '09:16'
pnl_check_end_time = '15:30'
check_interval_seconds = 60
token_swap_time = "09:00"

# Target Expiry & Delta Specifications
target_entry_dte = 45
min_entry_dte = 35
max_entry_dte = 50
exit_dte = 15

target_short_delta = 0.30
target_long_delta = 0.16
max_risk_reward_ratio = 2.0

# Intraday Opportunistic Scan Controls
enable_intraday_entry_scan = 1
intraday_scan_start_time = '09:16'
intraday_scan_end_time = '15:20'
max_entry_spread_pct = 0.12

# Defensive Adjustment Controls (Video 3 Dynamic Engine)
enable_adjustments = 1
adjustment_timing = 'eod'  # 'eod' or 'intraday'
roll_call_delta = 0.45

# Profit & Loss Risk Targets (% of Initial Gross Credit Collected)
call_profit_percent = 50.0
put_profit_percent = 50.0
call_loss_percent = 100.0
put_loss_percent = 100.0
risk_free_rate = 0.065

# Storage Directories & State Paths
gd_path = '/app/data/'
auth_path = '/app/config/auth.txt'
results_dir = 'Nifty_Monthly_Iron_Condor_Strategy_Results'
state_file = os.path.join(results_dir, 'active_trade_state.json')
tradebook_file = os.path.join(results_dir, 'options_tradebook.csv')
final_pnl_file = os.path.join(results_dir, 'final_pnl.csv')
daily_mtm_file = os.path.join(results_dir, 'daily_closing_mtm.csv')

os.makedirs(results_dir, exist_ok=True)
KOLKATA_TZ = pytz.timezone('Asia/Kolkata')

# Shared Tick Streaming State
live_market_data = {}
tick_lock = threading.Lock()

# ============================================================================
# 2. STATUTORY COMMISSION ENGINE (April 1, 2026 Mandate)
# ============================================================================
def commission_single_leg(quantity, buy_price, sell_price, exchange='NSE'):
    buy_turnover = quantity * buy_price
    sell_turnover = quantity * sell_price
    total_turnover = buy_turnover + sell_turnover

    zerodha_brokerage = 20.0 + 20.0
    stt = 0.0015 * sell_turnover
    txn_rate = 0.0005 if exchange.upper() == 'BSE' else 0.0003503
    exchange_txn_charge = txn_rate * total_turnover
    sebi_charges = 0.000001 * total_turnover
    gst = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)
    stamp_duty = 0.00003 * buy_turnover
    ipft = 0.00000005 * total_turnover

    total = zerodha_brokerage + stt + exchange_txn_charge + sebi_charges + gst + stamp_duty + ipft
    return round(total, 2)

def calc_single_execution_charges(quantity, price, side, exchange='NSE'):
    turnover = quantity * price
    brokerage = 20.0
    stt = (0.0015 * turnover) if side == 'SELL' else 0.0
    txn_rate = 0.0005 if exchange.upper() == 'BSE' else 0.0003503
    exchange_txn_charge = txn_rate * turnover
    sebi_charges = 0.000001 * turnover
    gst = 0.18 * (brokerage + exchange_txn_charge + sebi_charges)
    stamp_duty = (0.00003 * turnover) if side == 'BUY' else 0.0
    ipft = 0.00000005 * turnover

    total = brokerage + stt + exchange_txn_charge + sebi_charges + gst + stamp_duty + ipft
    return round(total, 2)

# ============================================================================
# 3. BLACK-SCHOLES IV & GREEKS ENGINE
# ============================================================================
_erf_vec = np.vectorize(math.erf)

def _norm_cdf(x):
    return 0.5 * (1.0 + _erf_vec(x / math.sqrt(2.0)))

def _norm_pdf(x):
    return np.exp(-0.5 * np.asarray(x, dtype=float) ** 2) / math.sqrt(2.0 * math.pi)

def bs_price_vectorized(S, K, T, r, sigma, option_type):
    K = np.asarray(K, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    T_safe = max(T, 1e-6)
    sigma_safe = np.where(sigma <= 1e-6, 1e-6, sigma)

    d1 = (np.log(S / K) + (r + 0.5 * sigma_safe ** 2) * T_safe) / (sigma_safe * math.sqrt(T_safe))
    d2 = d1 - sigma_safe * math.sqrt(T_safe)

    if option_type == 'call':
        return S * _norm_cdf(d1) - K * math.exp(-r * T_safe) * _norm_cdf(d2)
    return K * math.exp(-r * T_safe) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def solve_implied_vol_vectorized(target_prices, S, K, T, r, option_type, iterations=50):
    target_prices = np.asarray(target_prices, dtype=float)
    K = np.asarray(K, dtype=float)
    lo = np.full_like(target_prices, 0.001, dtype=float)
    hi = np.full_like(target_prices, 5.0, dtype=float)

    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        prices = bs_price_vectorized(S, K, T, r, mid, option_type)
        too_high = prices > target_prices
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)

    return (lo + hi) / 2.0

def compute_greeks_vectorized(S, K, T, r, sigma, option_type):
    K = np.asarray(K, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    T_safe = max(T, 1e-6)
    sigma_safe = np.where(sigma <= 1e-6, 1e-6, sigma)

    d1 = (np.log(S / K) + (r + 0.5 * sigma_safe ** 2) * T_safe) / (sigma_safe * math.sqrt(T_safe))
    d2 = d1 - sigma_safe * math.sqrt(T_safe)
    pdf_d1 = _norm_pdf(d1)

    if option_type == 'call':
        delta = _norm_cdf(d1)
        theta_annual = (-(S * pdf_d1 * sigma_safe) / (2.0 * math.sqrt(T_safe))
                        - r * K * math.exp(-r * T_safe) * _norm_cdf(d2))
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (-(S * pdf_d1 * sigma_safe) / (2.0 * math.sqrt(T_safe))
                        + r * K * math.exp(-r * T_safe) * _norm_cdf(-d2))

    gamma = pdf_d1 / (S * sigma_safe * math.sqrt(T_safe))
    vega = S * pdf_d1 * math.sqrt(T_safe) / 100.0
    theta_per_day = theta_annual / 365.0

    return {'delta': delta, 'gamma': gamma, 'theta': theta_per_day, 'vega': vega}

def compute_single_iv_delta(price, S, K, T, r, option_type):
    try:
        iv = solve_implied_vol_vectorized(np.array([price]), S, np.array([K]), T, r, option_type)[0]
        greeks = compute_greeks_vectorized(S, np.array([K]), T, r, np.array([iv]), option_type)
        return float(iv), float(greeks['delta'][0])
    except Exception:
        return 0.0, 0.0

# ============================================================================
# 4. DATA STREAMING & INSTRUMENT HELPERS
# ============================================================================
def subscribe_tokens(tick_client, tokens):
    valid_tokens = [int(t) for t in tokens if t and int(t) > 0]
    if valid_tokens:
        tick_client.subscribe(valid_tokens)

def get_live_price(instrument_token, fallback_price):
    with tick_lock:
        live_price = live_market_data.get(int(instrument_token), 0.0)
    return float(live_price) if live_price > 0.0 else float(fallback_price)

def get_kite_session():
    with open(auth_path, 'r') as f:
        api_data = f.read().strip()
    api_key = api_data.split(',')[0].strip()
    access_token = api_data.split(',')[1].strip()
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite, api_key, access_token

def load_filtered_instruments():
    usecols = ['tradingsymbol', 'instrument_token', 'name', 'segment', 'expiry', 'strike', 'instrument_type']
    dtypes = {
        'tradingsymbol': 'str', 'instrument_token': 'int32',
        'name': 'str', 'segment': 'str', 'expiry': 'str',
        'strike': 'float32', 'instrument_type': 'str'
    }
    df = pd.read_csv(os.path.join(gd_path, 'instrument_tokens.csv'), usecols=usecols, dtype=dtypes)
    df = df[(df['name'] == ind) & (df['segment'] == 'NFO-OPT')].copy()
    df.reset_index(drop=True, inplace=True)
    gc.collect()
    return df

def get_target_monthly_expiry(instruments_df, current_date, traded_expiries):
    unique_expiries = sorted(instruments_df['expiry'].unique())
    grouped = {}
    for exp in unique_expiries:
        dt = datetime.strptime(exp, '%Y-%m-%d').date()
        grouped[(dt.year, dt.month)] = exp

    monthly_expiries = sorted(list(grouped.values()))
    candidate_exp = None
    best_diff = 999

    for exp in monthly_expiries:
        if exp in traded_expiries:
            continue
        exp_dt = datetime.strptime(exp, '%Y-%m-%d').date()
        dte = (exp_dt - current_date).days
        if min_entry_dte <= dte <= max_entry_dte:
            diff = abs(dte - target_entry_dte)
            if diff < best_diff:
                best_diff = diff
                candidate_exp = exp

    return candidate_exp

# ============================================================================
# 5. ATOMIC PERSISTENCE & DIRECT-APPEND CSV LEDGERS
# ============================================================================
def load_trade_state():
    if not os.path.exists(state_file):
        return {'status': 'IDLE', 'trade_id': None, 'traded_expiries': []}
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
            if 'traded_expiries' not in state:
                state['traded_expiries'] = []
            return state
    except Exception as e:
        print(f"Error loading state: {e}. Reverting to IDLE.")
        return {'status': 'IDLE', 'trade_id': None, 'traded_expiries': []}

def save_trade_state(state):
    tmp_path = state_file + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, state_file)

def get_historical_traded_expiries():
    expiries = set()
    if os.path.exists(final_pnl_file):
        try:
            df = pd.read_csv(final_pnl_file, usecols=['Expiry'])
            expiries = set(df['Expiry'].dropna().unique())
        except Exception:
            pass
    return expiries

def append_to_tradebook(row_dict):
    headers = ['Trade_ID', 'Timestamp', 'Symbol', 'Expiry', 'Strike', 'Option_Type', 
               'Order_Side', 'Quantity', 'Order_Type', 'Fill_Price', 'Order_ID', 
               'Charges_STT_Brok', 'Action_Tag']
    file_exists = os.path.exists(tradebook_file)
    with open(tradebook_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)

def append_to_final_pnl(row_dict):
    headers = ['Trade_ID', 'Expiry', 'Entry_Time', 'Exit_Time', 'Days_Held', 'Entry_Spot', 
               'Exit_Spot', 'Initial_Credit_Pts', 'Gross_PnL', 'Total_Charges', 
               'Net_Realized_PnL', 'Setup_Capital', 'RoC_Pct', 'Exit_Reason', 'Max_Drawdown_Seen']
    file_exists = os.path.exists(final_pnl_file)
    with open(final_pnl_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)

def append_to_daily_mtm(row_dict):
    headers = ['Trade_ID', 'Date', 'Expiry', 'DTE', 'Spot', 'Unrealized_Gross', 
               'Realized_Gross', 'Est_Closing_Charges', 'Total_Net_PnL', 'Net_Delta']
    file_exists = os.path.exists(daily_mtm_file)
    with open(daily_mtm_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)

# ============================================================================
# 6. ORDER ROUTING & REALISTIC EXECUTION (NRML)
# ============================================================================
def place_order(kite, sym, qty, side, live_execution_flag):
    ltp_sym = f"NFO:{sym}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    def fetch_quote_data():
        for attempt in range(10):
            try:
                resp = kite.quote(ltp_sym)
                if ltp_sym in resp:
                    q = resp[ltp_sym]
                    ltp = float(q.get('last_price', 0.0))
                    depth = q.get('depth', {})
                    return ltp, depth
            except Exception as e:
                print(f"Quote fetch error attempt {attempt + 1} for {sym}: {e}")
            time.sleep(0.5 + (1.0 if attempt >= 2 else 0.0))
        return None, None

    def get_limit_price(current_ltp):
        if side == 'BUY':
            price = current_ltp * 1.10 if current_ltp > 50 else current_ltp + 5.0
        else:
            price = current_ltp * 0.90 if current_ltp > 50 else current_ltp - 5.0
        price = max(price, 0.05)
        return round(round(price / 0.05) * 0.05, 2)

    current_ltp, depth_data = fetch_quote_data()
    if current_ltp is None or current_ltp <= 0.0:
        print(f"Order placement failed: Could not fetch initial Quote/LTP for {sym}")
        return {'status': 'FAILED', 'fill_price': 0.0, 'order_id': None}

    limit_price = get_limit_price(current_ltp)

    # -------------------------------------------------------------
    # PAPER TRADING: ORDERBOOK DEPTH FILL & SLIPPAGE
    # -------------------------------------------------------------
    if live_execution_flag == 0:
        paper_fill_price = current_ltp
        depth_source = "LTP (Fallback)"
        
        if depth_data:
            if side == 'BUY':
                sell_depth = depth_data.get('sell', [])
                if sell_depth and len(sell_depth) > 0 and float(sell_depth[0].get('price', 0)) > 0:
                    paper_fill_price = float(sell_depth[0]['price'])
                    depth_source = f"Best Ask Depth (Qty: {sell_depth[0].get('quantity', 0)})"
            else:
                buy_depth = depth_data.get('buy', [])
                if buy_depth and len(buy_depth) > 0 and float(buy_depth[0].get('price', 0)) > 0:
                    paper_fill_price = float(buy_depth[0]['price'])
                    depth_source = f"Best Bid Depth (Qty: {buy_depth[0].get('quantity', 0)})"

        simulated_order_id = f"SIM_{uuid.uuid4().hex[:8].upper()}"
        slippage_pts = paper_fill_price - current_ltp if side == 'BUY' else current_ltp - paper_fill_price
        print(f"[PAPER TRADING] Fill {side} {qty}x {sym} @ Rs. {paper_fill_price:.2f} | Ref LTP: {current_ltp:.2f} (Slippage: {slippage_pts:+.2f} pts) via {depth_source}")
        return {'status': 'COMPLETE', 'fill_price': paper_fill_price, 'order_id': simulated_order_id}

    # -------------------------------------------------------------
    # LIVE TRADING: NRML AGGRESSIVE LIMIT ORDER CHASING
    # -------------------------------------------------------------
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
                product=kite.PRODUCT_NRML
            )
            break
        except Exception as e:
            place_retries += 1
            print(f"Entry_order_error attempt {place_retries} for {sym}: {e}")
            if place_retries == 10:
                print(f"Order placement rejected permanently for {sym}")
                return {'status': 'FAILED', 'fill_price': 0.0, 'order_id': None}
            time.sleep(place_sleep)
            if place_retries >= 3:
                place_sleep += 1.0

    if not order_id:
        return {'status': 'FAILED', 'fill_price': 0.0, 'order_id': None}

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
                fill_price = float(latest_state.get('average_price', limit_price))
                return {'status': 'COMPLETE', 'fill_price': fill_price, 'order_id': order_id}

            elif status in ['REJECTED', 'CANCELLED']:
                print(f"Order {status}. Reason: {latest_state.get('status_message', 'Unknown')}")
                return {'status': 'FAILED', 'fill_price': 0.0, 'order_id': order_id}

            elif pending_qty > 0:
                new_ltp, _ = fetch_quote_data()
                if new_ltp:
                    new_limit_price = get_limit_price(new_ltp)
                    kite.modify_order(
                        variety=kite.VARIETY_REGULAR,
                        order_id=order_id,
                        order_type=kite.ORDER_TYPE_LIMIT,
                        price=new_limit_price
                    )
        except Exception as e:
            print(f"Order modification error for {order_id}: {e}")

        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0

    print(f"Warning: Max modifications reached for {sym}. Attempting market cancel/inquiry.")
    try:
        order_history = kite.order_history(order_id)
        latest_state = order_history[-1]
        if latest_state['status'] == 'COMPLETE':
            return {'status': 'COMPLETE', 'fill_price': float(latest_state.get('average_price', limit_price)), 'order_id': order_id}
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    except Exception:
        pass
    return {'status': 'FAILED', 'fill_price': 0.0, 'order_id': order_id}

# ============================================================================
# 7. STRIKE SELECTION & RISK-TO-REWARD VALIDATION
# ============================================================================
def select_best_iron_condor_setup(kite, spot, exp_str, instruments_df, current_date):
    target_exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
    dte = (target_exp_date - current_date).days
    T_entry = max(dte / 365.0, 1e-5)

    exp_df = instruments_df[instruments_df['expiry'] == exp_str].copy()
    if exp_df.empty:
        return None

    # Dynamic wide search bandwidth (±15% of spot)
    min_k = spot * 0.85
    max_k = spot * 1.15
    candidate_strikes = exp_df[(exp_df['strike'] % strike_step == 0) & 
                               (exp_df['strike'] >= min_k) & 
                               (exp_df['strike'] <= max_k)]['strike'].unique()
    candidate_strikes = sorted(candidate_strikes)

    symbols_map = {}
    tokens_map = {}
    quote_symbols = []

    for k in candidate_strikes:
        ce_match = exp_df[(exp_df['strike'] == k) & (exp_df['instrument_type'] == 'CE')]
        pe_match = exp_df[(exp_df['strike'] == k) & (exp_df['instrument_type'] == 'PE')]
        if not ce_match.empty and not pe_match.empty:
            ce_sym = ce_match.iloc[0]['tradingsymbol']
            pe_sym = pe_match.iloc[0]['tradingsymbol']
            symbols_map[(k, 'CE')] = ce_sym
            symbols_map[(k, 'PE')] = pe_sym
            tokens_map[(k, 'CE')] = int(ce_match.iloc[0]['instrument_token'])
            tokens_map[(k, 'PE')] = int(pe_match.iloc[0]['instrument_token'])
            quote_symbols.extend([f"NFO:{ce_sym}", f"NFO:{pe_sym}"])

    if not quote_symbols:
        return None

    quotes = {}
    chunk_size = 200
    for i in range(0, len(quote_symbols), chunk_size):
        chunk = quote_symbols[i:i + chunk_size]
        try:
            res = kite.quote(chunk)
            quotes.update(res)
        except Exception as e:
            print(f"Error fetching quote batch: {e}")
            return None

    chain_data = []
    for k in candidate_strikes:
        ce_sym = f"NFO:{symbols_map.get((k, 'CE'), '')}"
        pe_sym = f"NFO:{symbols_map.get((k, 'PE'), '')}"

        if ce_sym in quotes and pe_sym in quotes:
            q_ce = quotes[ce_sym]
            q_pe = quotes[pe_sym]

            ce_bid = float(q_ce['depth']['buy'][0]['price']) if q_ce.get('depth', {}).get('buy') else 0.0
            ce_ask = float(q_ce['depth']['sell'][0]['price']) if q_ce.get('depth', {}).get('sell') else 0.0
            pe_bid = float(q_pe['depth']['buy'][0]['price']) if q_pe.get('depth', {}).get('buy') else 0.0
            pe_ask = float(q_pe['depth']['sell'][0]['price']) if q_pe.get('depth', {}).get('sell') else 0.0

            chain_data.append({
                'strike': k,
                'ce_bid': ce_bid, 'ce_ask': ce_ask, 'ce_ltp': q_ce.get('last_price', 0.0),
                'pe_bid': pe_bid, 'pe_ask': pe_ask, 'pe_ltp': q_pe.get('last_price', 0.0)
            })

    cdf = pd.DataFrame(chain_data)
    if cdf.empty:
        return None

    def pick_strike(opt_type, target_delta, is_short):
        sub = cdf.copy()
        if opt_type == 'call':
            sub = sub[sub['strike'] >= spot]
            bid_c, ask_c = 'ce_bid', 'ce_ask'
        else:
            sub = sub[sub['strike'] <= spot]
            bid_c, ask_c = 'pe_bid', 'pe_ask'

        sub = sub[(sub[bid_c] > 0) & (sub[ask_c] > 0)]
        if sub.empty:
            return None, 0.0, 0.0

        sub['mid'] = (sub[bid_c] + sub[ask_c]) / 2.0
        sub['spread'] = (sub[ask_c] - sub[bid_c]).abs() / sub['mid']
        sub = sub[sub['spread'] <= max_entry_spread_pct]
        if sub.empty:
            return None, 0.0, 0.0

        strikes = sub['strike'].values
        mids = sub['mid'].values
        ivs = solve_implied_vol_vectorized(mids, spot, strikes, T_entry, risk_free_rate, opt_type)
        greeks = compute_greeks_vectorized(spot, strikes, T_entry, risk_free_rate, ivs, opt_type)
        deltas = np.abs(greeks['delta'])

        best_idx = int(np.abs(deltas - target_delta).argmin())
        chosen_k = float(strikes[best_idx])
        row = sub[sub['strike'] == chosen_k].iloc[0]
        # Short sells at Bid; Long buys at Ask
        exec_price = float(row[bid_c if is_short else ask_c])
        return chosen_k, exec_price, float(deltas[best_idx])

    sc_k, sc_p, sc_d = pick_strike('call', target_short_delta, True)
    lc_k, lc_p, lc_d = pick_strike('call', target_long_delta, False)
    sp_k, sp_p, sp_d = pick_strike('put', target_short_delta, True)
    lp_k, lp_p, lp_d = pick_strike('put', target_long_delta, False)

    if None in [sc_k, lc_k, sp_k, lp_k] or lc_k <= sc_k or lp_k >= sp_k:
        return None

    if lc_p >= sc_p or lp_p >= sp_p:
        return None

    net_credit_pts = (sc_p + sp_p) - (lc_p + lp_p)
    if net_credit_pts <= 0:
        return None

    call_wing_w = lc_k - sc_k
    put_wing_w = sp_k - lp_k
    max_w = max(call_wing_w, put_wing_w)
    max_loss = max_w - net_credit_pts
    rr = max_loss / net_credit_pts if net_credit_pts > 0 else 999.0

    if rr > max_risk_reward_ratio:
        nudged = False
        test_lc_k, test_lp_k = lc_k, lp_k
        for _ in range(2):
            test_lc_k -= strike_step
            test_lp_k += strike_step
            if test_lc_k > sc_k and test_lp_k < sp_k:
                r_lc = cdf[cdf['strike'] == test_lc_k]
                r_lp = cdf[cdf['strike'] == test_lp_k]
                if not r_lc.empty and not r_lp.empty:
                    test_lc_p = float(r_lc.iloc[0]['ce_ask'])  # Buying call wing at Ask
                    test_lp_p = float(r_lp.iloc[0]['pe_ask'])  # Buying put wing at Ask
                    test_credit = (sc_p + sp_p) - (test_lc_p + test_lp_p)
                    test_w = max(test_lc_k - sc_k, sp_k - test_lp_k)
                    test_loss = test_w - test_credit
                    test_rr = test_loss / test_credit if test_credit > 0 else 999.0
                    
                    if test_rr <= max_risk_reward_ratio and test_lc_p < sc_p and test_lp_p < sp_p:
                        lc_k, lc_p = test_lc_k, test_lc_p
                        lp_k, lp_p = test_lp_k, test_lp_p
                        net_credit_pts = test_credit
                        call_wing_w = lc_k - sc_k
                        put_wing_w = sp_k - lp_k
                        max_w = test_w
                        max_loss = test_loss
                        rr = test_rr
                        
                        # Recalculate deltas after inward wing shift
                        lc_mid = (float(r_lc.iloc[0]['ce_bid']) + test_lc_p) / 2.0
                        lp_mid = (float(r_lp.iloc[0]['pe_bid']) + test_lp_p) / 2.0
                        _, new_lc_d = compute_single_iv_delta(lc_mid, spot, lc_k, T_entry, risk_free_rate, 'call')
                        _, new_lp_d = compute_single_iv_delta(lp_mid, spot, lp_k, T_entry, risk_free_rate, 'put')
                        lc_d, lp_d = abs(new_lc_d), abs(new_lp_d)
                        nudged = True
                        break

        if not nudged and rr > max_risk_reward_ratio:
            return None

    return {
        'expiry': exp_str,
        'dte': dte,
        'spot': spot,
        'sc': {'strike': sc_k, 'price': sc_p, 'delta': sc_d, 'sym': symbols_map[(sc_k, 'CE')], 'token': tokens_map[(sc_k, 'CE')]},
        'lc': {'strike': lc_k, 'price': lc_p, 'delta': lc_d, 'sym': symbols_map[(lc_k, 'CE')], 'token': tokens_map[(lc_k, 'CE')]},
        'sp': {'strike': sp_k, 'price': sp_p, 'delta': sp_d, 'sym': symbols_map[(sp_k, 'PE')], 'token': tokens_map[(sp_k, 'PE')]},
        'lp': {'strike': lp_k, 'price': lp_p, 'delta': lp_d, 'sym': symbols_map[(lp_k, 'PE')], 'token': tokens_map[(lp_k, 'PE')]},
        'call_wing_w': call_wing_w,
        'put_wing_w': put_wing_w,
        'net_credit_pts': net_credit_pts,
        'rr_ratio': rr
    }

# ============================================================================
# 8. LIVE TRADING WORKER ROUTINE
# ============================================================================
def run_trading_worker():
    now_init = datetime.now(KOLKATA_TZ)
    total_qty = lot_size * ratio_multiplier
    total_capital = setup_cost * ratio_multiplier
    mode_tag = "LIVE TRADING (Real Capital Deployed)" if live_mode == 1 else "PAPER TRADING (Orderbook Depth Fills)"

    print("=" * 88, flush=True)
    print("      IRON CONDOR QUANTITATIVE PRODUCTION ENGINE - TRADING WORKER BOOT", flush=True)
    print("=" * 88, flush=True)
    print(f"  Launch Time         : {now_init.strftime('%Y-%m-%d %H:%M:%S')} IST", flush=True)
    print(f"  Execution Mode      : {mode_tag}", flush=True)
    print(f"  Underlying Asset    : {symbol} (Scrip: {scrip}, Exchange: {exchange})", flush=True)
    print(f"  Position Sizing     : {ratio_multiplier} lots ({total_qty} units) | Lot Size: {lot_size}", flush=True)
    print(f"  Capital Allocation  : Rs. {total_capital:,.2f} (Rs. {setup_cost:,.2f}/lot)", flush=True)
    print(f"  Target Entry Window : {min_entry_dte} to {max_entry_dte} DTE (Target ~{target_entry_dte} DTE)", flush=True)
    print(f"  Delta Sizing Target : Short Wings ~{target_short_delta*100:.0f}D | Long Wings ~{target_long_delta*100:.0f}D", flush=True)
    print(f"  Risk Targets        : TP: +{call_profit_percent:.0f}% Credit | SL: -{call_loss_percent:.0f}% Credit | Sunset: {exit_dte} DTE @ {rollover_time}", flush=True)
    print(f"  Monitoring Timers   : Active PnL: {pnl_check_start_time} - {pnl_check_end_time} | Poll Interval: {check_interval_seconds}s", flush=True)
    print(f"  Intraday Entry Scan : {'ENABLED (' + intraday_scan_start_time + ' to ' + intraday_scan_end_time + ')' if enable_intraday_entry_scan else 'DISABLED (Check at ' + start_time + ')'}", flush=True)
    print(f"  Defensive Engine    : {'ENABLED (Video 3 Asymmetric Rolls)' if enable_adjustments else 'DISABLED'}", flush=True)
    print("-" * 88, flush=True)

    today_date = now_init.date()
    if today_date.weekday() in [5, 6]:
        print("Weekend detected. Terminating worker.", flush=True)
        return

    kite, api_key, access_token = get_kite_session()
    instruments_df = load_filtered_instruments()

    kws = RedisTickClient(live_market_data, tick_lock)
    kws.start()
    time.sleep(1.0)

    # Initial Spot Quote Retrieval
    spot_price = 0.0
    try:
        q_scrip = kite.ltp(f"NSE:{scrip}")
        spot_price = float(q_scrip[f"NSE:{scrip}"]['last_price'])
    except Exception as e:
        print(f"Initial spot fetch warning: {e}")

    state = load_trade_state()
    historical_expiries = get_historical_traded_expiries()
    traded_expiries = set(state.get('traded_expiries', [])).union(historical_expiries)
    state['traded_expiries'] = sorted(list(traded_expiries))
    save_trade_state(state)

    active_tokens = []
    if state.get('status') == 'ACTIVE':
        legs = state['legs']
        active_tokens = [legs['sc']['token'], legs['lc']['token'], legs['sp']['token'], legs['lp']['token']]
        subscribe_tokens(kws, active_tokens)
        exp_d = datetime.strptime(state['expiry'], '%Y-%m-%d').date()
        rem_dte = (exp_d - today_date).days
        print(f"  Active Trade State  : REHYDRATED [{state['trade_id']}]", flush=True)
        print(f"  Monthly Expiry      : {state['expiry']} ({rem_dte} DTE remaining)", flush=True)
        print(f"  Iron Condor Wings   : Put Spread [{legs['lp']['strike']}/{legs['sp']['strike']}] | Call Spread [{legs['sc']['strike']}/{legs['lc']['strike']}]", flush=True)
        print(f"  Initial Net Credit  : {state['initial_credit_pts']:.2f} pts | Target: Rs. {state['frozen_config']['target_pnl']:,.2f} | Stop: Rs. {state['frozen_config']['stop_pnl']:,.2f}", flush=True)
        print(f"  Subscribed Tokens   : {active_tokens}", flush=True)
    else:
        print(f"  Active Trade State  : IDLE (Hunting for ~{target_entry_dte} DTE Monthly Setup)", flush=True)
    print(f"  Locked Expiries     : {state['traded_expiries']}", flush=True)
    print("=" * 88 + "\n", flush=True)

    daily_trade_exited = False

    # Synchronize to next minute
    now = datetime.now(KOLKATA_TZ)
    time.sleep(max(0, 60 - now.second))

    while True:
        now = datetime.now(KOLKATA_TZ)
        curr_time = now.strftime('%H:%M')

        # -------------------------------------------------------------
        # PRE-MARKET IDLE GATE (09:00 - 09:16)
        # -------------------------------------------------------------
        if curr_time < pnl_check_start_time:
            print(f"[{now.strftime('%H:%M:%S')}] Standing by for market surveillance opening at {pnl_check_start_time} IST...", flush=True)
            time.sleep(check_interval_seconds)
            continue

        # -------------------------------------------------------------
        # POST-MARKET CLOSE GATE (15:30)
        # -------------------------------------------------------------
        if curr_time >= pnl_check_end_time:
            print(f"[{curr_time}] Market surveillance concluded. Recording daily closing ledger...", flush=True)
            if state.get('status') == 'ACTIVE':
                legs = state['legs']
                q_val = state['frozen_config']['quantity']
                
                # Snapshot closing prices using depth quotes
                try:
                    q_snap = kite.quote([f"NFO:{legs['sc']['sym']}", f"NFO:{legs['lc']['sym']}", 
                                         f"NFO:{legs['sp']['sym']}", f"NFO:{legs['lp']['sym']}"])
                    sc_ask = float(q_snap[f"NFO:{legs['sc']['sym']}"]['depth']['sell'][0]['price'])
                    lc_bid = float(q_snap[f"NFO:{legs['lc']['sym']}"]['depth']['buy'][0]['price'])
                    sp_ask = float(q_snap[f"NFO:{legs['sp']['sym']}"]['depth']['sell'][0]['price'])
                    lp_bid = float(q_snap[f"NFO:{legs['lp']['sym']}"]['depth']['buy'][0]['price'])
                except Exception:
                    sc_ask = get_live_price(legs['sc']['token'], legs['sc']['entry_price'])
                    lc_bid = get_live_price(legs['lc']['token'], legs['lc']['entry_price'])
                    sp_ask = get_live_price(legs['sp']['token'], legs['sp']['entry_price'])
                    lp_bid = get_live_price(legs['lp']['token'], legs['lp']['entry_price'])

                unrealized_gross = ((legs['sc']['entry_price'] - sc_ask) + (lc_bid - legs['lc']['entry_price']) +
                                    (legs['sp']['entry_price'] - sp_ask) + (lp_bid - legs['lp']['entry_price'])) * q_val

                est_close_charges = (
                    commission_single_leg(q_val, sc_ask, legs['sc']['entry_price'], exchange) +
                    commission_single_leg(q_val, legs['lc']['entry_price'], lc_bid, exchange) +
                    commission_single_leg(q_val, sp_ask, legs['sp']['entry_price'], exchange) +
                    commission_single_leg(q_val, legs['lp']['entry_price'], lp_bid, exchange)
                )

                total_comm = state.get('total_commission_paid', 0.0) + est_close_charges
                total_net = (unrealized_gross + state.get('realized_gross_pnl', 0.0)) - total_comm
                exp_d = datetime.strptime(state['expiry'], '%Y-%m-%d').date()
                cur_dte = (exp_d - now.date()).days

                append_to_daily_mtm({
                    'Trade_ID': state['trade_id'],
                    'Date': str(now.date()),
                    'Expiry': state['expiry'],
                    'DTE': cur_dte,
                    'Spot': spot_price,
                    'Unrealized_Gross': round(unrealized_gross, 2),
                    'Realized_Gross': round(state.get('realized_gross_pnl', 0.0), 2),
                    'Est_Closing_Charges': round(est_close_charges, 2),
                    'Total_Net_PnL': round(total_net, 2),
                    'Net_Delta': 0.0
                })
            break

        # Periodic Underlying Spot Refresh with Stale Guard
        try:
            q_scrip = kite.ltp(f"NSE:{scrip}")
            fresh_spot = float(q_scrip[f"NSE:{scrip}"]['last_price'])
            if fresh_spot > 0:
                spot_price = fresh_spot
        except Exception as e:
            print(f"Warning: Failed to fetch spot price: {e}")

        # Missing/Zero Spot Guard: Halt evaluation loop if spot is missing/corrupt
        if spot_price <= 0.0:
            print(f"[{now.strftime('%H:%M:%S')}] CRITICAL: Corrupted/Stale spot price ({spot_price:.2f}). Skipping cycle...", flush=True)
            time.sleep(check_interval_seconds)
            continue

        # -------------------------------------------------------------
        # STATE: IDLE - ENTRY SCANNING ENGINE
        # -------------------------------------------------------------
        if state.get('status') == 'IDLE' and not daily_trade_exited:
            within_scan_window = (
                (enable_intraday_entry_scan == 1 and intraday_scan_start_time <= curr_time <= intraday_scan_end_time) or
                (enable_intraday_entry_scan == 0 and curr_time == start_time)
            )

            if within_scan_window:
                cand_exp = get_target_monthly_expiry(instruments_df, now.date(), state['traded_expiries'])
                if cand_exp:
                    setup = select_best_iron_condor_setup(kite, spot_price, cand_exp, instruments_df, now.date())
                    if setup:
                        trade_id = f"IC_{cand_exp}_{uuid.uuid4().hex[:6].upper()}"
                        total_qty = lot_size * ratio_multiplier
                        total_cap = setup_cost * ratio_multiplier
                        total_credit_inr = setup['net_credit_pts'] * total_qty
                        target_pnl = total_credit_inr * (call_profit_percent / 100.0)
                        stop_pnl = -(total_credit_inr * (call_loss_percent / 100.0))

                        print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ENTRY CRITERIA SATISFIED -> {trade_id}", flush=True)
                        print(f"Expiry: {cand_exp} ({setup['dte']} DTE) | Spot: {spot_price:.2f} | Risk-Reward: 1:{setup['rr_ratio']:.2f}", flush=True)

                        state = {'status': 'ENTRY_PENDING', 'trade_id': trade_id, 'traded_expiries': state['traded_expiries']}
                        save_trade_state(state)

                        # Phased Entry Execution: Long Wings First (Hedge Protection)
                        fill_lc = place_order(kite, setup['lc']['sym'], total_qty, 'BUY', live_mode)
                        fill_lp = place_order(kite, setup['lp']['sym'], total_qty, 'BUY', live_mode)

                        # Break-Glass Safeguard (Option A: Immediate Flat Abort)
                        if fill_lc['status'] != 'COMPLETE' or fill_lp['status'] != 'COMPLETE':
                            print("CRITICAL: Long wing fills failed. Executing break-glass unwind...", flush=True)
                            if fill_lc['status'] == 'COMPLETE':
                                place_order(kite, setup['lc']['sym'], total_qty, 'SELL', live_mode)
                            if fill_lp['status'] == 'COMPLETE':
                                place_order(kite, setup['lp']['sym'], total_qty, 'SELL', live_mode)
                            state = {'status': 'IDLE', 'trade_id': None, 'traded_expiries': state['traded_expiries']}
                            save_trade_state(state)
                            time.sleep(check_interval_seconds)
                            continue

                        # Execute Short Legs Once Hedges are Active
                        fill_sc = place_order(kite, setup['sc']['sym'], total_qty, 'SELL', live_mode)
                        fill_sp = place_order(kite, setup['sp']['sym'], total_qty, 'SELL', live_mode)

                        if fill_sc['status'] != 'COMPLETE' or fill_sp['status'] != 'COMPLETE':
                            print("CRITICAL: Short leg fills failed. Liquidating entire structure immediately...", flush=True)
                            if fill_sc['status'] == 'COMPLETE':
                                place_order(kite, setup['sc']['sym'], total_qty, 'BUY', live_mode)
                            if fill_sp['status'] == 'COMPLETE':
                                place_order(kite, setup['sp']['sym'], total_qty, 'BUY', live_mode)
                            place_order(kite, setup['lc']['sym'], total_qty, 'SELL', live_mode)
                            place_order(kite, setup['lp']['sym'], total_qty, 'SELL', live_mode)
                            state = {'status': 'IDLE', 'trade_id': None, 'traded_expiries': state['traded_expiries']}
                            save_trade_state(state)
                            time.sleep(check_interval_seconds)
                            continue

                        # All 4 Legs Confirmed: Write Execution Ledger
                        legs_conf = {
                            'sc': {'strike': setup['sc']['strike'], 'sym': setup['sc']['sym'], 'token': setup['sc']['token'], 'entry_price': fill_sc['fill_price']},
                            'lc': {'strike': setup['lc']['strike'], 'sym': setup['lc']['sym'], 'token': setup['lc']['token'], 'entry_price': fill_lc['fill_price']},
                            'sp': {'strike': setup['sp']['strike'], 'sym': setup['sp']['sym'], 'token': setup['sp']['token'], 'entry_price': fill_sp['fill_price']},
                            'lp': {'strike': setup['lp']['strike'], 'sym': setup['lp']['sym'], 'token': setup['lp']['token'], 'entry_price': fill_lp['fill_price']}
                        }

                        ts_str = now.strftime('%Y-%m-%d %H:%M:%S')
                        entry_comm = 0.0
                        for k_leg, side_str, act_tag in [('lc', 'BUY', 'ENTRY_HEDGE'), ('lp', 'BUY', 'ENTRY_HEDGE'),
                                                         ('sc', 'SELL', 'ENTRY_SHORT'), ('sp', 'SELL', 'ENTRY_SHORT')]:
                            f_p = legs_conf[k_leg]['entry_price']
                            o_id = fill_lc['order_id'] if k_leg == 'lc' else (fill_lp['order_id'] if k_leg == 'lp' else (fill_sc['order_id'] if k_leg == 'sc' else fill_sp['order_id']))
                            chg = calc_single_execution_charges(total_qty, f_p, side_str, exchange)
                            entry_comm += chg
                            append_to_tradebook({
                                'Trade_ID': trade_id, 'Timestamp': ts_str, 'Symbol': symbol,
                                'Expiry': cand_exp, 'Strike': legs_conf[k_leg]['strike'],
                                'Option_Type': 'CE' if 'c' in k_leg else 'PE',
                                'Order_Side': side_str, 'Quantity': total_qty,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_p, 'Order_ID': o_id,
                                'Charges_STT_Brok': chg, 'Action_Tag': act_tag
                            })

                        # Atomic State Transition to ACTIVE
                        state = {
                            'status': 'ACTIVE',
                            'trade_id': trade_id,
                            'expiry': cand_exp,
                            'entry_time': ts_str,
                            'entry_spot': spot_price,
                            'initial_credit_pts': setup['net_credit_pts'],
                            'legs': legs_conf,
                            'call_wing_width': setup['call_wing_w'],
                            'put_wing_width': setup['put_wing_w'],
                            'realized_gross_pnl': 0.0,
                            'total_commission_paid': entry_comm,
                            'down_rolls_done': 0,
                            'up_rolls_done': 0,
                            'spot_at_roll_1': None,
                            'last_adj_date': str(now.date()),
                            'max_drawdown_seen': 0.0,
                            'traded_expiries': state['traded_expiries'],
                            'frozen_config': {
                                'quantity': total_qty,
                                'setup_cost': total_cap,
                                'target_pnl': target_pnl,
                                'stop_pnl': stop_pnl,
                                'call_profit_percent': call_profit_percent,
                                'call_loss_percent': call_loss_percent,
                                'exit_dte': exit_dte
                            }
                        }
                        save_trade_state(state)
                        active_tokens = [legs_conf['sc']['token'], legs_conf['lc']['token'], legs_conf['sp']['token'], legs_conf['lp']['token']]
                        subscribe_tokens(kws, active_tokens)
                        print(f"State Locked: ACTIVE | Subscribed Tokens: {active_tokens}\n", flush=True)

        # -------------------------------------------------------------
        # STATE: ACTIVE - EVERY-MINUTE MTM & DEFENSIVE SURVEILLANCE
        # -------------------------------------------------------------
        if state.get('status') == 'ACTIVE':
            legs = state['legs']
            q_val = state['frozen_config']['quantity']
            cfg = state['frozen_config']

            # Real Bid/Ask quotes for accurate Mark-to-Market
            try:
                active_symbols = [f"NFO:{legs['sc']['sym']}", f"NFO:{legs['lc']['sym']}", 
                                  f"NFO:{legs['sp']['sym']}", f"NFO:{legs['lp']['sym']}"]
                live_quotes = kite.quote(active_symbols)
                sc_ask = float(live_quotes[f"NFO:{legs['sc']['sym']}"]['depth']['sell'][0]['price']) if live_quotes[f"NFO:{legs['sc']['sym']}"]['depth']['sell'] else float(live_quotes[f"NFO:{legs['sc']['sym']}"]['last_price'])
                lc_bid = float(live_quotes[f"NFO:{legs['lc']['sym']}"]['depth']['buy'][0]['price']) if live_quotes[f"NFO:{legs['lc']['sym']}"]['depth']['buy'] else float(live_quotes[f"NFO:{legs['lc']['sym']}"]['last_price'])
                sp_ask = float(live_quotes[f"NFO:{legs['sp']['sym']}"]['depth']['sell'][0]['price']) if live_quotes[f"NFO:{legs['sp']['sym']}"]['depth']['sell'] else float(live_quotes[f"NFO:{legs['sp']['sym']}"]['last_price'])
                lp_bid = float(live_quotes[f"NFO:{legs['lp']['sym']}"]['depth']['buy'][0]['price']) if live_quotes[f"NFO:{legs['lp']['sym']}"]['depth']['buy'] else float(live_quotes[f"NFO:{legs['lp']['sym']}"]['last_price'])
            except Exception:
                # Resilient Fallback to Redis LTP
                sc_ask = get_live_price(legs['sc']['token'], legs['sc']['entry_price'])
                lc_bid = get_live_price(legs['lc']['token'], legs['lc']['entry_price'])
                sp_ask = get_live_price(legs['sp']['token'], legs['sp']['entry_price'])
                lp_bid = get_live_price(legs['lp']['token'], legs['lp']['entry_price'])

            unrealized_gross = ((legs['sc']['entry_price'] - sc_ask) + (lc_bid - legs['lc']['entry_price']) +
                                (legs['sp']['entry_price'] - sp_ask) + (lp_bid - legs['lp']['entry_price'])) * q_val

            # Closing charges if squared off now
            comm_open_legs = (
                commission_single_leg(q_val, sc_ask, legs['sc']['entry_price'], exchange) +
                commission_single_leg(q_val, legs['lc']['entry_price'], lc_bid, exchange) +
                commission_single_leg(q_val, sp_ask, legs['sp']['entry_price'], exchange) +
                commission_single_leg(q_val, legs['lp']['entry_price'], lp_bid, exchange)
            )

            # Total Net Realized & Unrealized PnL (No Double Commission Deduction)
            total_net_pnl = unrealized_gross + state.get('realized_gross_pnl', 0.0) - (state.get('total_commission_paid', 0.0) + comm_open_legs)
            
            if total_net_pnl < state['max_drawdown_seen']:
                state['max_drawdown_seen'] = round(total_net_pnl, 2)
                save_trade_state(state)

            exp_date_obj = datetime.strptime(state['expiry'], '%Y-%m-%d').date()
            cur_dte = (exp_date_obj - now.date()).days
            T_curr = max(cur_dte / 365.0, 1e-5)

            _, sc_d = compute_single_iv_delta(sc_ask, spot_price, legs['sc']['strike'], T_curr, risk_free_rate, 'call')
            _, sp_d = compute_single_iv_delta(sp_ask, spot_price, legs['sp']['strike'], T_curr, risk_free_rate, 'put')
            net_delta = ((-sc_d) + (-sp_d)) * q_val

            # Every-Minute Real-Time MTM Progress Display
            print(f"[{now.strftime('%H:%M:%S')}] MTM | DTE: {cur_dte:02d} | Spot: {spot_price:.1f} | "
                  f"SC({legs['sc']['strike']:.0f}): {sc_ask:.2f} | LC({legs['lc']['strike']:.0f}): {lc_bid:.2f} | "
                  f"SP({legs['sp']['strike']:.0f}): {sp_ask:.2f} | LP({legs['lp']['strike']:.0f}): {lp_bid:.2f} | "
                  f"Gross: Rs. {unrealized_gross:+,.2f} | Net: Rs. {total_net_pnl:+,.2f} | "
                  f"Target: Rs. {cfg['target_pnl']:,.2f} | Stop: Rs. {cfg['stop_pnl']:,.2f} | Delta: {net_delta:+.1f}", flush=True)

            # Exit Conditions Evaluation (Sunset strictly triggers immediately if cur_dte < 15)
            target_hit = total_net_pnl >= cfg['target_pnl']
            stop_hit = total_net_pnl <= cfg['stop_pnl']
            dte_cutoff_hit = (cur_dte < cfg['exit_dte']) or (cur_dte == cfg['exit_dte'] and curr_time >= rollover_time)
            friction_guard_hit = False

            if (state['down_rolls_done'] > 0 or state['up_rolls_done'] > 0) and total_net_pnl > 0:
                if (cfg['target_pnl'] - total_net_pnl) < (comm_open_legs * 1.2):
                    friction_guard_hit = True

            if target_hit or stop_hit or dte_cutoff_hit or friction_guard_hit:
                reason = "Target Hit (+50% Credit)" if target_hit else (
                         "Stop Loss Hit (-100% Credit)" if stop_hit else (
                         "Remaining Profit <= Friction Cutoff" if friction_guard_hit else
                         f"Time Cutoff Reached ({cfg['exit_dte']} DTE Exit)"))

                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] EXIT SIGNAL TRIGGERED: {reason}", flush=True)
                state['status'] = 'EXIT_PENDING'
                save_trade_state(state)

                # Exit Sequencing: Buy to Close Short Legs First
                f_sc_x = place_order(kite, legs['sc']['sym'], q_val, 'BUY', live_mode)
                f_sp_x = place_order(kite, legs['sp']['sym'], q_val, 'BUY', live_mode)

                # Sell to Close Long Wings
                f_lc_x = place_order(kite, legs['lc']['sym'], q_val, 'SELL', live_mode)
                f_lp_x = place_order(kite, legs['lp']['sym'], q_val, 'SELL', live_mode)

                exit_ts = now.strftime('%Y-%m-%d %H:%M:%S')
                exit_comm = 0.0
                for k_leg, s_side, f_res in [('sc', 'BUY', f_sc_x), ('sp', 'BUY', f_sp_x),
                                             ('lc', 'SELL', f_lc_x), ('lp', 'SELL', f_lp_x)]:
                    chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                    exit_comm += chg
                    append_to_tradebook({
                        'Trade_ID': state['trade_id'], 'Timestamp': exit_ts, 'Symbol': symbol,
                        'Expiry': state['expiry'], 'Strike': legs[k_leg]['strike'],
                        'Option_Type': 'CE' if 'c' in k_leg else 'PE',
                        'Order_Side': s_side, 'Quantity': q_val, 'Order_Type': 'LIMIT',
                        'Fill_Price': f_res['fill_price'], 'Order_ID': f_res['order_id'],
                        'Charges_STT_Brok': chg, 'Action_Tag': 'EXIT_' + reason.split()[0].upper()
                    })

                total_comm = state['total_commission_paid'] + exit_comm
                final_gross_pnl = unrealized_gross + state.get('realized_gross_pnl', 0.0)
                final_net = final_gross_pnl - total_comm
                roc = (final_net / cfg['setup_cost']) * 100.0
                days_held = (now.date() - datetime.strptime(state['entry_time'], '%Y-%m-%d %H:%M:%S').date()).days

                append_to_final_pnl({
                    'Trade_ID': state['trade_id'], 'Expiry': state['expiry'],
                    'Entry_Time': state['entry_time'], 'Exit_Time': exit_ts,
                    'Days_Held': days_held, 'Entry_Spot': state['entry_spot'],
                    'Exit_Spot': spot_price, 'Initial_Credit_Pts': state['initial_credit_pts'],
                    'Gross_PnL': round(final_gross_pnl, 2),
                    'Total_Charges': round(total_comm, 2),
                    'Net_Realized_PnL': round(final_net, 2),
                    'Setup_Capital': cfg['setup_cost'], 'RoC_Pct': round(roc, 2),
                    'Exit_Reason': reason, 'Max_Drawdown_Seen': state['max_drawdown_seen']
                })

                # Permanently lock this monthly expiry from being re-entered
                if state['expiry'] not in state['traded_expiries']:
                    state['traded_expiries'].append(state['expiry'])

                state = {'status': 'IDLE', 'trade_id': None, 'traded_expiries': state['traded_expiries']}
                save_trade_state(state)
                daily_trade_exited = True
                print(f"Cycle Settled. Final Realized Net PnL: Rs. {final_net:,.2f} | RoC: {roc:+.2f}%\n", flush=True)
                time.sleep(check_interval_seconds)
                continue

            # ---------------------------------------------------------
            # DEFENSIVE ADJUSTMENT ENGINE (Video 3 Dynamic Rolls)
            # ---------------------------------------------------------
            timing_ok = (curr_time >= rollover_time) if adjustment_timing == 'eod' else True
            cooldown_ok = str(now.date()) > state['last_adj_date']

            if enable_adjustments and cur_dte > cfg['exit_dte'] and cooldown_ok and timing_ok:
                T_snap = max(cur_dte / 365.0, 1e-5)
                call_wing_w = state['call_wing_width']
                put_wing_w = state['put_wing_width']
                exp_scope = instruments_df[instruments_df['expiry'] == state['expiry']]

                # Helper to pre-verify replacement strikes & quotes
                def verify_roll_spread(opt_type, new_short_k, new_long_k):
                    sc_match = exp_scope[(exp_scope['strike'] == new_short_k) & (exp_scope['instrument_type'] == opt_type)]
                    lc_match = exp_scope[(exp_scope['strike'] == new_long_k) & (exp_scope['instrument_type'] == opt_type)]
                    if sc_match.empty or lc_match.empty:
                        return False, None, None, 0.0, 0.0

                    row_s = sc_match.iloc[0]
                    row_l = lc_match.iloc[0]

                    try:
                        q_roll = kite.quote([f"NFO:{row_s['tradingsymbol']}", f"NFO:{row_l['tradingsymbol']}"])
                        # Selling new short at Bid; Buying new long at Ask
                        new_s_p = float(q_roll[f"NFO:{row_s['tradingsymbol']}"]['depth']['buy'][0]['price']) if q_roll[f"NFO:{row_s['tradingsymbol']}"]['depth']['buy'] else float(q_roll[f"NFO:{row_s['tradingsymbol']}"]['last_price'])
                        new_l_p = float(q_roll[f"NFO:{row_l['tradingsymbol']}"]['depth']['sell'][0]['price']) if q_roll[f"NFO:{row_l['tradingsymbol']}"]['depth']['sell'] else float(q_roll[f"NFO:{row_l['tradingsymbol']}"]['last_price'])
                        
                        if new_l_p < new_s_p:  # Strict net credit requirement
                            return True, row_s, row_l, new_s_p, new_l_p
                    except Exception as e:
                        print(f"Error fetching roll spread quotes: {e}")
                    return False, None, None, 0.0, 0.0

                # SCENARIO A1: Downside Stage 1 (Spot Breaches Short Put)
                if state['down_rolls_done'] == 0 and spot_price <= legs['sp']['strike']:
                    print(f"\n[ADJUSTMENT] Downside Breach 1: Spot {spot_price:.2f} <= Short Put {legs['sp']['strike']}", flush=True)

                    # 1. Pre-calculate candidate ~45 Delta Call Spread
                    cand_k = sorted([k for k in exp_scope['strike'].unique() if k >= spot_price and k % strike_step == 0])
                    quote_syms = [f"NFO:{exp_scope[(exp_scope['strike'] == k) & (exp_scope['instrument_type'] == 'CE')].iloc[0]['tradingsymbol']}" for k in cand_k if not exp_scope[(exp_scope['strike'] == k) & (exp_scope['instrument_type'] == 'CE')].empty]
                    
                    try:
                        batch_q = kite.quote(quote_syms)
                    except Exception:
                        batch_q = {}

                    new_sc_k = None
                    best_delta_diff = 999
                    for k in cand_k:
                        sym_match = exp_scope[(exp_scope['strike'] == k) & (exp_scope['instrument_type'] == 'CE')]
                        if sym_match.empty:
                            continue
                        tsym = f"NFO:{sym_match.iloc[0]['tradingsymbol']}"
                        if tsym in batch_q:
                            mid_p = (float(batch_q[tsym]['depth']['buy'][0]['price']) + float(batch_q[tsym]['depth']['sell'][0]['price'])) / 2.0 if batch_q[tsym]['depth']['buy'] and batch_q[tsym]['depth']['sell'] else float(batch_q[tsym]['last_price'])
                            _, d = compute_single_iv_delta(mid_p, spot_price, k, T_snap, risk_free_rate, 'call')
                            diff = abs(d - roll_call_delta)
                            if diff < best_delta_diff:
                                best_delta_diff = diff
                                new_sc_k = k

                    new_lc_k = new_sc_k + call_wing_w if new_sc_k else None

                    # 2. Pre-verify strikes and credit before routing any orders
                    valid_roll, row_sc, row_lc, new_sc_p, new_lc_p = verify_roll_spread('CE', new_sc_k, new_lc_k) if new_sc_k and new_lc_k else (False, None, None, 0, 0)

                    if valid_roll:
                        # 3. Safe Execution: Close Old Call Spread
                        f_sc_c = place_order(kite, legs['sc']['sym'], q_val, 'BUY', live_mode)
                        f_lc_c = place_order(kite, legs['lc']['sym'], q_val, 'SELL', live_mode)

                        comm_close_call = (
                            calc_single_execution_charges(q_val, f_sc_c['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_lc_c['fill_price'], 'SELL', exchange)
                        )
                        call_gross = (legs['sc']['entry_price'] - f_sc_c['fill_price'] + f_lc_c['fill_price'] - legs['lc']['entry_price']) * q_val

                        adj_ts = now.strftime('%Y-%m-%d %H:%M:%S')
                        for k_leg, s_side, f_res in [('sc', 'BUY', f_sc_c), ('lc', 'SELL', f_lc_c)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': legs[k_leg]['strike'],
                                'Option_Type': 'CE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_CLOSE_CALL'
                            })

                        # 4. Open New Call Spread (Hedge Long First)
                        f_new_lc = place_order(kite, row_lc['tradingsymbol'], q_val, 'BUY', live_mode)
                        f_new_sc = place_order(kite, row_sc['tradingsymbol'], q_val, 'SELL', live_mode)

                        comm_open_new = (
                            calc_single_execution_charges(q_val, f_new_lc['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_new_sc['fill_price'], 'SELL', exchange)
                        )

                        for k_k, s_side, f_res in [(new_lc_k, 'BUY', f_new_lc), (new_sc_k, 'SELL', f_new_sc)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': k_k,
                                'Option_Type': 'CE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_OPEN_CALL'
                            })

                        state['realized_gross_pnl'] = state.get('realized_gross_pnl', 0.0) + call_gross
                        state['total_commission_paid'] += (comm_close_call + comm_open_new)
                        state['down_rolls_done'] = 1
                        state['last_adj_date'] = str(now.date())
                        state['spot_at_roll_1'] = spot_price
                        state['legs']['sc'] = {'strike': new_sc_k, 'sym': row_sc['tradingsymbol'], 'token': int(row_sc['instrument_token']), 'entry_price': f_new_sc['fill_price']}
                        state['legs']['lc'] = {'strike': new_lc_k, 'sym': row_lc['tradingsymbol'], 'token': int(row_lc['instrument_token']), 'entry_price': f_new_lc['fill_price']}
                        save_trade_state(state)
                        subscribe_tokens(kws, [int(row_sc['instrument_token']), int(row_lc['instrument_token'])])
                        print(f"Call Spread Rolled to ~45D Cushion -> New Strikes: [{new_sc_k}/{new_lc_k}]\n", flush=True)

                # SCENARIO A2: Downside Stage 2 (Market Breaches Long Put Wing -> Iron Fly)
                elif state['down_rolls_done'] == 1 and spot_price <= legs['lp']['strike'] and spot_price < state['spot_at_roll_1']:
                    print(f"\n[ADJUSTMENT] Downside Breach 2: Spot {spot_price:.2f} <= Long Put {legs['lp']['strike']} (Converting to Iron Fly)", flush=True)
                    new_sc_k = legs['sp']['strike']
                    new_lc_k = new_sc_k + call_wing_w

                    valid_roll, row_sc, row_lc, new_sc_p, new_lc_p = verify_roll_spread('CE', new_sc_k, new_lc_k)

                    if valid_roll:
                        f_sc_c = place_order(kite, legs['sc']['sym'], q_val, 'BUY', live_mode)
                        f_lc_c = place_order(kite, legs['lc']['sym'], q_val, 'SELL', live_mode)

                        comm_close_call = (
                            calc_single_execution_charges(q_val, f_sc_c['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_lc_c['fill_price'], 'SELL', exchange)
                        )
                        call_gross = (legs['sc']['entry_price'] - f_sc_c['fill_price'] + f_lc_c['fill_price'] - legs['lc']['entry_price']) * q_val

                        adj_ts = now.strftime('%Y-%m-%d %H:%M:%S')
                        for k_leg, s_side, f_res in [('sc', 'BUY', f_sc_c), ('lc', 'SELL', f_lc_c)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': legs[k_leg]['strike'],
                                'Option_Type': 'CE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_CLOSE_CALL'
                            })

                        f_new_lc = place_order(kite, row_lc['tradingsymbol'], q_val, 'BUY', live_mode)
                        f_new_sc = place_order(kite, row_sc['tradingsymbol'], q_val, 'SELL', live_mode)

                        comm_open_new = (
                            calc_single_execution_charges(q_val, f_new_lc['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_new_sc['fill_price'], 'SELL', exchange)
                        )

                        for k_k, s_side, f_res in [(new_lc_k, 'BUY', f_new_lc), (new_sc_k, 'SELL', f_new_sc)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': k_k,
                                'Option_Type': 'CE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_OPEN_CALL'
                            })

                        state['realized_gross_pnl'] = state.get('realized_gross_pnl', 0.0) + call_gross
                        state['total_commission_paid'] += (comm_close_call + comm_open_new)
                        state['down_rolls_done'] = 2
                        state['last_adj_date'] = str(now.date())
                        state['legs']['sc'] = {'strike': new_sc_k, 'sym': row_sc['tradingsymbol'], 'token': int(row_sc['instrument_token']), 'entry_price': f_new_sc['fill_price']}
                        state['legs']['lc'] = {'strike': new_lc_k, 'sym': row_lc['tradingsymbol'], 'token': int(row_lc['instrument_token']), 'entry_price': f_new_lc['fill_price']}
                        save_trade_state(state)
                        subscribe_tokens(kws, [int(row_sc['instrument_token']), int(row_lc['instrument_token'])])
                        print(f"Position Converted to Centered Iron Fly on {new_sc_k} Strike", flush=True)

                        # Terminal Cutoff Check on Converted Iron Fly
                        comm_terminal_exit = (
                            commission_single_leg(q_val, new_sc_p, f_new_sc['fill_price'], exchange) +
                            commission_single_leg(q_val, f_new_lc['fill_price'], new_lc_p, exchange) +
                            commission_single_leg(q_val, sp_ask, legs['sp']['entry_price'], exchange) +
                            commission_single_leg(q_val, legs['lp']['entry_price'], lp_bid, exchange)
                        )
                        final_net_terminal_pnl = (unrealized_gross + state['realized_gross_pnl']) - (state['total_commission_paid'] + comm_terminal_exit)

                        if (cfg['target_pnl'] - final_net_terminal_pnl) <= (comm_terminal_exit * 1.5):
                            print(f"\n[TERMINAL CUTOFF] Position exhausted post-Iron Fly conversion. Liquidating immediately...", flush=True)
                            f_sc_t = place_order(kite, row_sc['tradingsymbol'], q_val, 'BUY', live_mode)
                            f_sp_t = place_order(kite, legs['sp']['sym'], q_val, 'BUY', live_mode)
                            f_lc_t = place_order(kite, row_lc['tradingsymbol'], q_val, 'SELL', live_mode)
                            f_lp_t = place_order(kite, legs['lp']['sym'], q_val, 'SELL', live_mode)

                            tot_terminal_comm = state['total_commission_paid'] + comm_terminal_exit
                            final_net_exit = (unrealized_gross + state['realized_gross_pnl']) - tot_terminal_comm
                            roc = (final_net_exit / cfg['setup_cost']) * 100.0
                            days_held = (now.date() - datetime.strptime(state['entry_time'], '%Y-%m-%d %H:%M:%S').date()).days

                            append_to_final_pnl({
                                'Trade_ID': state['trade_id'], 'Expiry': state['expiry'],
                                'Entry_Time': state['entry_time'], 'Exit_Time': now.strftime('%Y-%m-%d %H:%M:%S'),
                                'Days_Held': days_held, 'Entry_Spot': state['entry_spot'],
                                'Exit_Spot': spot_price, 'Initial_Credit_Pts': state['initial_credit_pts'],
                                'Gross_PnL': round(unrealized_gross + state['realized_gross_pnl'], 2),
                                'Total_Charges': round(tot_terminal_comm, 2),
                                'Net_Realized_PnL': round(final_net_exit, 2),
                                'Setup_Capital': cfg['setup_cost'], 'RoC_Pct': round(roc, 2),
                                'Exit_Reason': "Terminal Iron Fly Exit (Loss Controlled)", 'Max_Drawdown_Seen': state['max_drawdown_seen']
                            })

                            if state['expiry'] not in state['traded_expiries']:
                                state['traded_expiries'].append(state['expiry'])

                            state = {'status': 'IDLE', 'trade_id': None, 'traded_expiries': state['traded_expiries']}
                            save_trade_state(state)
                            daily_trade_exited = True
                            time.sleep(check_interval_seconds)
                            continue

                # SCENARIO B: Upside Breach (Spot Breaches Short Call -> Iron Fly)
                elif state['up_rolls_done'] == 0 and spot_price >= legs['sc']['strike']:
                    print(f"\n[ADJUSTMENT] Upside Breach: Spot {spot_price:.2f} >= Short Call {legs['sc']['strike']} (Converting to Iron Fly)", flush=True)
                    new_sp_k = legs['sc']['strike']
                    new_lp_k = new_sp_k - put_wing_w

                    valid_roll, row_sp, row_lp, new_sp_p, new_lp_p = verify_roll_spread('PE', new_sp_k, new_lp_k)

                    if valid_roll:
                        f_sp_c = place_order(kite, legs['sp']['sym'], q_val, 'BUY', live_mode)
                        f_lp_c = place_order(kite, legs['lp']['sym'], q_val, 'SELL', live_mode)

                        comm_close_put = (
                            calc_single_execution_charges(q_val, f_sp_c['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_lp_c['fill_price'], 'SELL', exchange)
                        )
                        put_gross = (legs['sp']['entry_price'] - f_sp_c['fill_price'] + f_lp_c['fill_price'] - legs['lp']['entry_price']) * q_val

                        adj_ts = now.strftime('%Y-%m-%d %H:%M:%S')
                        for k_leg, s_side, f_res in [('sp', 'BUY', f_sp_c), ('lp', 'SELL', f_lp_c)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': legs[k_leg]['strike'],
                                'Option_Type': 'PE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_CLOSE_PUT'
                            })

                        f_new_lp = place_order(kite, row_lp['tradingsymbol'], q_val, 'BUY', live_mode)
                        f_new_sp = place_order(kite, row_sp['tradingsymbol'], q_val, 'SELL', live_mode)

                        comm_open_new = (
                            calc_single_execution_charges(q_val, f_new_lp['fill_price'], 'BUY', exchange) +
                            calc_single_execution_charges(q_val, f_new_sp['fill_price'], 'SELL', exchange)
                        )

                        for k_k, s_side, f_res in [(new_lp_k, 'BUY', f_new_lp), (new_sp_k, 'SELL', f_new_sp)]:
                            chg = calc_single_execution_charges(q_val, f_res['fill_price'], s_side, exchange)
                            append_to_tradebook({
                                'Trade_ID': state['trade_id'], 'Timestamp': adj_ts, 'Symbol': symbol,
                                'Expiry': state['expiry'], 'Strike': k_k,
                                'Option_Type': 'PE', 'Order_Side': s_side, 'Quantity': q_val,
                                'Order_Type': 'LIMIT', 'Fill_Price': f_res['fill_price'],
                                'Order_ID': f_res['order_id'], 'Charges_STT_Brok': chg, 'Action_Tag': 'ROLL_OPEN_PUT'
                            })

                        state['realized_gross_pnl'] = state.get('realized_gross_pnl', 0.0) + put_gross
                        state['total_commission_paid'] += (comm_close_put + comm_open_new)
                        state['up_rolls_done'] = 1
                        state['last_adj_date'] = str(now.date())
                        state['legs']['sp'] = {'strike': new_sp_k, 'sym': row_sp['tradingsymbol'], 'token': int(row_sp['instrument_token']), 'entry_price': f_new_sp['fill_price']}
                        state['legs']['lp'] = {'strike': new_lp_k, 'sym': row_lp['tradingsymbol'], 'token': int(row_lp['instrument_token']), 'entry_price': f_new_lp['fill_price']}
                        save_trade_state(state)
                        subscribe_tokens(kws, [int(row_sp['instrument_token']), int(row_lp['instrument_token'])])
                        print(f"Put Spread Converted to Center on Short Call {new_sp_k} (Iron Fly)\n", flush=True)

        time.sleep(check_interval_seconds)

    print("Trading Worker Cycle Finished.", flush=True)

# ============================================================================
# 9. SUPERVISOR PROCESS MANAGER
# ============================================================================
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_trading_worker()
    else:
        worker_process = None
        last_completed_date = None
        print(f"Supervisor Active. Monitoring window {token_swap_time} to {pnl_check_end_time} IST...", flush=True)

        try:
            while True:
                now_kolkata = datetime.now(KOLKATA_TZ)
                curr_hm = now_kolkata.strftime("%H:%M")
                today_d = now_kolkata.date()

                # Launch worker during active market window (resilient to reboots)
                if token_swap_time <= curr_hm < pnl_check_end_time:
                    if today_d.weekday() not in [5, 6] and last_completed_date != today_d:
                        if worker_process is None or worker_process.poll() is not None:
                            print(f"\n{'*' * 60}", flush=True)
                            print(f"DISPATCHING DAILY TRADING WORKER: {today_d} at {curr_hm} IST", flush=True)
                            print(f"{'*' * 60}\n", flush=True)

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
                                if curr_hm >= "15:29":
                                    last_completed_date = today_d
                            except Exception as e:
                                print(f"Worker Interruption Exception: {e}", flush=True)

                            print("\nWorker Terminated. Memory Cleaned.", flush=True)
                            worker_process = None

                time.sleep(15)

        except KeyboardInterrupt:
            print("\nSupervisor terminating by User Request (Ctrl+C).", flush=True)
        finally:
            if worker_process and worker_process.poll() is None:
                print("[SAFETY] Terminating active background worker...", flush=True)
                try:
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
                except Exception:
                    worker_process.kill()
                print("[SAFETY] Worker successfully shut down.", flush=True)

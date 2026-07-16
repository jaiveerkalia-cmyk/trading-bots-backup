import dash
from dash import dcc, html
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import aiohttp
import asyncio
import datetime
import threading
import sys
import os
import logging
import time
import tempfile  # BUG 2 FIX: Added missing import for CSV compaction

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
# AUDITED FINAL VERSION - 6-Bar Rolling Accumulators & Live/Closed Split Engine - 2026-07-14

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# 1. CONSTANTS - PRODUCTION GRADE - FULL DESCRIPTIVE NAMES
# ==========================================
MAX_HISTORY_ROWS = 10080
UI_REFRESH_INTERVAL = 10000
USE_SPOT_CVD_FILTER_DEFAULT = False
PRESSURE_CLIP_MAX = 10.0
PRESSURE_CLIP_MIN = 0.1
WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN = 10
CVD_NOISE_THRESHOLD_MINIMUM_VALUE = 0.5
CVD_NOISE_THRESHOLD_FACTOR_MULTIPLIER = 0.2
VOLATILITY_SCALER_ENABLED = False
DIVERGENCE_LOOKBACK_BARS = 10
PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS = 30
OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS = 30
PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS = 1.5
BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS = 100
TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS = 100
TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS = 100
SPOT_DELTA_VOLATILITY_LOOKBACK_BARS = 20

# NEW: ROLLING ACCUMULATION WINDOWS
SHORT_ROLLING_WINDOW_BARS = 3
LONG_ROLLING_WINDOW_BARS = 6

# BUG 1 FIX: Updated CSV Schema to safely store explicit 15m and 30m API data
EXPECTED_CSV_COLUMN_ORDER = ['timestamp','price','basis','oi','taker_imbalance','whale_div','taker_imbalance_15m','whale_div_15m','taker_imbalance_30m','whale_div_30m','spot_delta']
SIGNAL_CSV_COLUMN_ORDER = ['epoch','timestamp','timeframe','signal','price','reason','price_change','price_thresh','oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure','sp_thresh','basis','premium_thresh','premium_thresh_lower']

# --- IST wall-clock helpers -----------------------------------------------
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(IST_TZ).replace(tzinfo=None)

def ist_from_epoch(epoch_seconds):
    return datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc).astimezone(IST_TZ).replace(tzinfo=None)

DATA_FILE = "data/lci_history.csv"
SIGNAL_LOG_FILE = "data/signal_events.csv"
os.makedirs("data", exist_ok=True)

if os.path.exists(DATA_FILE):
    global_df = pd.read_csv(DATA_FILE)
    global_df['timestamp'] = pd.to_datetime(global_df['timestamp'], format='ISO8601')
    
    # --- SEAMLESS MIGRATION FIX ---
    # Detects if old CSV doesn't have the new columns, adds them safely as NaN, and saves it
    missing_cols = [c for c in EXPECTED_CSV_COLUMN_ORDER if c not in global_df.columns]
    if missing_cols:
        print(f"Migrating historical CSV: adding missing columns {missing_cols}", flush=True)
        for c in missing_cols:
            global_df[c] = np.nan
        # Enforce exact column order
        global_df = global_df[EXPECTED_CSV_COLUMN_ORDER]
        # Overwrite the file with the upgraded schema to prevent append_row_atomic from discarding it
        global_df.to_csv(DATA_FILE, index=False, date_format='%Y-%m-%d %H:%M:%S')
    # ------------------------------

    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows")
else:
    global_df = pd.DataFrame(columns=EXPECTED_CSV_COLUMN_ORDER)

df_lock = threading.Lock()
file_lock = threading.Lock()
cvd_setting_lock = threading.Lock()
current_cvd_filter_setting = USE_SPOT_CVD_FILTER_DEFAULT

logged_signals = set()
if os.path.exists(SIGNAL_LOG_FILE):
    try:
        sdf = pd.read_csv(SIGNAL_LOG_FILE, on_bad_lines='skip')
        for _, r in sdf.iterrows():
            if 'epoch' in sdf.columns and pd.notna(r.get('epoch', None)):
                logged_signals.add(f"{int(r['epoch'])}_{r['timeframe']}_{r['signal']}")
            else:
                try:
                    epoch_from_ts = int(pd.to_datetime(r['timestamp']).timestamp())
                    logged_signals.add(f"{epoch_from_ts}_{r['timeframe']}_{r['signal']}")
                except:
                    logged_signals.add(f"{r['timestamp']}_{r['timeframe']}_{r['signal']}")
        print(f"Loaded {len(logged_signals)} signals")
    except: pass

# ==========================================
# 2. FETCH WITH RETRY + ABSOLUTE EPOCH DAEMON
# ==========================================
async def fetch_json(session, url, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                if attempt == retries - 1:
                    print(f"API FAIL {url}: HTTP {resp.status}", flush=True)
                    return None
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            if attempt == retries-1:
                return None
            await asyncio.sleep(2 ** attempt)
    return None

async def fetch_binance_data():
    async with aiohttp.ClientSession() as session:
        # BUG 1 FIX: Concurrently fetch native 15m and 30m API data from Binance
        urls = {
            "spot": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            "spot_kline": "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1",
            "premium": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            "oi": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            "taker": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=1",
            "acc_ratio": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
            "pos_ratio": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=1",
            "taker_15m": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=15m&limit=1",
            "acc_ratio_15m": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=15m&limit=1",
            "pos_ratio_15m": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=15m&limit=1",
            "taker_30m": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=30m&limit=1",
            "acc_ratio_30m": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=30m&limit=1",
            "pos_ratio_30m": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=30m&limit=1"
        }
        tasks = {k: fetch_json(session, u) for k,u in urls.items()}
        results = await asyncio.gather(*tasks.values())
        data = dict(zip(tasks.keys(), results))
        if not data.get("spot") or not data.get("premium") or not data.get("oi"): return None
        ts = now_ist().replace(second=0, microsecond=0)
        try:
            spot_price = float(data["spot"]["price"])
            mark = float(data["premium"]["markPrice"])
            index = float(data["premium"]["indexPrice"])
            oi = float(data["oi"]["openInterest"])
            basis = mark - index
            
            def parse_ratios(t_data, ar_data, pr_data):
                t_imb, w_div = 1.0, 1.0
                if t_data and len(t_data)>0:
                    sell = float(t_data[0]["sellVol"]); buy = float(t_data[0]["buyVol"])
                    t_imb = sell/buy if buy>0 else 1.0
                if ar_data and pr_data and len(ar_data)>0 and len(pr_data)>0:
                    ar = float(ar_data[0]["longShortRatio"]); pr = float(pr_data[0]["longShortRatio"])
                    w_div = ar/pr if pr>0 else 1.0
                return float(np.clip(t_imb, 0, PRESSURE_CLIP_MAX)), float(np.clip(w_div, 0, PRESSURE_CLIP_MAX))
            
            taker_imb, whale = parse_ratios(data.get("taker"), data.get("acc_ratio"), data.get("pos_ratio"))
            taker_imb_15m, whale_15m = parse_ratios(data.get("taker_15m"), data.get("acc_ratio_15m"), data.get("pos_ratio_15m"))
            taker_imb_30m, whale_30m = parse_ratios(data.get("taker_30m"), data.get("acc_ratio_30m"), data.get("pos_ratio_30m"))

            spot_delta = 0.0
            if data.get("spot_kline"):
                k = data["spot_kline"][0]
                tot = float(k[5]); tb = float(k[9]); spot_delta = tb - (tot - tb)
                spot_delta = float(np.clip(spot_delta, -100, 100))
            
            return {'timestamp':ts,'price':spot_price,'basis':basis,'oi':oi,
                    'taker_imbalance':taker_imb,'whale_div':whale,
                    'taker_imbalance_15m':taker_imb_15m,'whale_div_15m':whale_15m,
                    'taker_imbalance_30m':taker_imb_30m,'whale_div_30m':whale_30m,
                    'spot_delta':spot_delta}
        except: return None

def append_row_atomic(row):
    with file_lock:
        df_row = pd.DataFrame([row], columns=EXPECTED_CSV_COLUMN_ORDER)[EXPECTED_CSV_COLUMN_ORDER]
        if not os.path.exists(DATA_FILE):
            df_row.to_csv(DATA_FILE, index=False, date_format='%Y-%m-%d %H:%M:%S')
            return
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                hdr = f.readline().strip().split(',')
                hdr = [h.strip().strip('"') for h in hdr]
            if hdr != EXPECTED_CSV_COLUMN_ORDER:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                os.rename(DATA_FILE, f"{DATA_FILE}.mismatch_{ts}.bak")
                df_row.to_csv(DATA_FILE, index=False, date_format='%Y-%m-%d %H:%M:%S')
                return
        except: pass
        df_row.to_csv(DATA_FILE, mode='a', header=False, index=False, date_format='%Y-%m-%d %H:%M:%S')

def compact_csv_if_needed():
    with file_lock:
        try:
            with df_lock:
                if global_df.empty: return
                trimmed = global_df[EXPECTED_CSV_COLUMN_ORDER].iloc[-MAX_HISTORY_ROWS:].copy()
            with tempfile.NamedTemporaryFile(mode='w', delete=False, dir='data', suffix='.tmp') as tf:
                tmp = tf.name
                trimmed.to_csv(tmp, index=False, date_format='%Y-%m-%d %H:%M:%S')
            os.replace(tmp, DATA_FILE)
        except: pass

def log_signal_to_csv(epoch_int, ts_str, timeframe, sig_name, price, reason, row):
    sig_id = f"{epoch_int}_{timeframe}_{sig_name}"
    with file_lock:
        if sig_id not in logged_signals:
            logged_signals.add(sig_id)
            d = {'epoch': epoch_int, 'timestamp': ts_str, 'timeframe': timeframe, 'signal': sig_name, 'price': float(price), 'reason': str(reason),
                 'price_change':0.0,'price_thresh':0.0,'oi_change':0.0,'oi_thresh_upper':0.0,'oi_thresh_lower':0.0,'buy_pressure':0.0,'bp_thresh':0.0,'sell_pressure':0.0,'sp_thresh':0.0,'basis':0.0,'premium_thresh':0.0,'premium_thresh_lower':0.0}
            try:
                for k in ['price_change','price_thresh','oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure','sp_thresh','basis','premium_thresh','premium_thresh_lower']:
                    if k in row: d[k]=float(row[k])
            except: pass
            df_out = pd.DataFrame([d], columns=SIGNAL_CSV_COLUMN_ORDER)[SIGNAL_CSV_COLUMN_ORDER]
            if not os.path.exists(SIGNAL_LOG_FILE):
                df_out.to_csv(SIGNAL_LOG_FILE, index=False)
                return
            df_out.to_csv(SIGNAL_LOG_FILE, mode='a', header=False, index=False)

def build_resampled_view(base_df, timeframe):
    if base_df.empty: return pd.DataFrame()
    df = base_df.copy()
    
    # BUG 1 FIX: Gracefully substitute the correct API endpoints for higher timeframes
    # Protects existing 5m flow. Will safely fallback to 5m via fillna() on legacy CSV data.
    for col in ['taker_imbalance_15m', 'whale_div_15m', 'taker_imbalance_30m', 'whale_div_30m']:
        if col not in df.columns: df[col] = np.nan
            
    if timeframe == '15min':
        df['taker_imbalance'] = df['taker_imbalance_15m'].fillna(df['taker_imbalance'])
        df['whale_div'] = df['whale_div_15m'].fillna(df['whale_div'])
    elif timeframe == '30min':
        df['taker_imbalance'] = df['taker_imbalance_30m'].fillna(df['taker_imbalance'])
        df['whale_div'] = df['whale_div_30m'].fillna(df['whale_div'])
        
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
    df.set_index('timestamp', inplace=True)
    if timeframe != '1min':
        agg = {'price':'last','basis':'last','oi':'last','taker_imbalance':'first','whale_div':'first','spot_delta':'sum'}
        res = df.resample(timeframe, closed='left', label='left').agg(agg)
        res[['taker_imbalance','whale_div']] = res[['taker_imbalance','whale_div']].ffill()
        res = res.dropna(subset=['price','basis','oi'])
        df = res
    df = df.reset_index()
    try:
        ts_ist = df['timestamp'].dt.tz_localize('Asia/Kolkata', ambiguous='infer', nonexistent='shift_forward')
        ts_utc = ts_ist.dt.tz_convert('UTC')
        df['session_date'] = ts_utc.dt.floor('D').dt.tz_localize(None)
    except:
        df['session_date'] = (df['timestamp'] - pd.Timedelta(hours=5, minutes=30)).dt.normalize()
    df['spot_cvd'] = df.groupby('session_date')['spot_delta'].cumsum()
    df['cvd_trend'] = df['spot_delta']
    df['time_diff'] = df['timestamp'].diff()
    return df

def compute_signals_for_view(df_input, timeframe, use_cvd_filter=False):
    if len(df_input) < 2: return df_input, True
    df = df_input.copy()

    # --- ALIGNMENT FIX ---
    if timeframe != '1min':
        df['taker_imbalance'] = df['taker_imbalance'].shift(-1).ffill()
        df['whale_div'] = df['whale_div'].shift(-1).ffill()

    # --- INSTANT WHALE TRAP CALCULATION ---
    df['whale_div_smooth'] = df['whale_div'].ewm(span=WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN, adjust=False).mean()
    df['whale_delta'] = df['whale_div'] - df['whale_div_smooth']
    
    safe_ti = np.where(df['taker_imbalance'] <= 0, 1e-9, df['taker_imbalance'])
    df['buy_pressure'] = np.clip(1 / safe_ti, 0, PRESSURE_CLIP_MAX)
    df['sell_pressure'] = np.clip(df['taker_imbalance'], 0, PRESSURE_CLIP_MAX)
    
    df['price_change'] = df['price'].pct_change()*100
    df['oi_change'] = df['oi'].pct_change()*100
    
    tf_map = {'1min':1,'5min':5,'15min':15,'30min':30}
    tf_min = tf_map.get(timeframe,5)
    gap_thr = pd.Timedelta(minutes=tf_min*1.5)
    df.loc[df['time_diff'] > gap_thr, ['price_change','oi_change']] = np.nan

    # --- ROLLING ACCUMULATORS ---
    df['oi_accum_long'] = df['oi_change'].rolling(LONG_ROLLING_WINDOW_BARS, min_periods=1).sum().fillna(0)
    df['oi_accum_short'] = df['oi_change'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).sum().fillna(0)
    df['price_stretch'] = df['price_change'].rolling(LONG_ROLLING_WINDOW_BARS, min_periods=1).sum().fillna(0)

    # --- SHORT MEMORY DANGER ZONES ---
    df['price_max_short'] = df['price'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).max()
    df['price_min_short'] = df['price'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).min()
    df['bp_max_short'] = df['buy_pressure'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).max()
    df['sp_max_short'] = df['sell_pressure'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).max()
    df['premium_max_short'] = df['basis'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).max()
    df['premium_min_short'] = df['basis'].rolling(SHORT_ROLLING_WINDOW_BARS, min_periods=1).min()

    # --- VOLATILITY BASELINES ---
    abs_price = df['price_change'].abs()
    ewm_price_mean = abs_price.ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_price_std = abs_price.ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['price_thresh'] = ewm_price_mean + (ewm_price_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    
    ewm_price_stretch_mean = df['price_stretch'].abs().ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_price_stretch_std = df['price_stretch'].abs().ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['price_stretch_thresh'] = ewm_price_stretch_mean + (ewm_price_stretch_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)

    ewm_oi_long_mean = df['oi_accum_long'].abs().ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_oi_long_std = df['oi_accum_long'].abs().ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['oi_accum_long_thresh'] = ewm_oi_long_mean + (ewm_oi_long_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    
    ewm_oi_short_mean = df['oi_accum_short'].abs().ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_oi_short_std = df['oi_accum_short'].abs().ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['oi_accum_short_thresh'] = ewm_oi_short_mean + (ewm_oi_short_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)

    df['premium_thresh'] = df['basis'].rolling(BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS).quantile(0.90)
    df['premium_thresh_lower'] = df['basis'].rolling(BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS).quantile(0.10)
    
    ewm_bp_mean = df['buy_pressure'].ewm(span=TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_bp_std = df['buy_pressure'].ewm(span=TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['bp_thresh'] = ewm_bp_mean + (ewm_bp_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    
    ewm_sp_mean = df['sell_pressure'].ewm(span=TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_sp_std = df['sell_pressure'].ewm(span=TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['sp_thresh'] = ewm_sp_mean + (ewm_sp_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    
    for col in ['price_thresh','price_stretch_thresh','oi_accum_long_thresh','oi_accum_short_thresh','premium_thresh','premium_thresh_lower','bp_thresh','sp_thresh']:
        df[col] = df[col].fillna({'price_thresh':0.15,'price_stretch_thresh':0.4,'oi_accum_long_thresh':0.8,'oi_accum_short_thresh':0.4,'premium_thresh':8.0,'premium_thresh_lower':-8.0,'bp_thresh':1.3,'sp_thresh':1.3}.get(col,0))

    # --- LOGIC RULES ---
    # 1. Breakouts/Breakdowns (Spark + Fuel + Trap)
    raw_breakout = (df['price_change'] > df['price_thresh']) & (df['oi_accum_long'] > df['oi_accum_long_thresh']) & (df['whale_delta'] < 0)
    raw_breakdown = (df['price_change'] < -df['price_thresh']) & (df['oi_accum_long'] > df['oi_accum_long_thresh']) & (df['whale_delta'] > 0)
    
    # 2. Squeeze/Liquidation (Cascade + Force + Trap)
    raw_fakeout = (df['oi_accum_short'] < -df['oi_accum_short_thresh']) & (df['buy_pressure'] > df['bp_thresh']) & (df['whale_delta'] > 0)
    raw_liq = (df['oi_accum_short'] < -df['oi_accum_short_thresh']) & (df['sell_pressure'] > df['sp_thresh']) & (df['whale_delta'] < 0)
    
    # 3. Exhaustion (Stretch + Stall + Danger Zone + Overext + Trap)
    raw_exhaust = (df['price_stretch'] > df['price_stretch_thresh']) & (df['price'] <= df['price_max_short'].shift(1)) & (df['bp_max_short'] > df['bp_thresh']) & (df['premium_max_short'] > df['premium_thresh']) & (df['whale_delta'] > 0)
    raw_bottom = (df['price_stretch'] < -df['price_stretch_thresh']) & (df['price'] >= df['price_min_short'].shift(1)) & (df['sp_max_short'] > df['sp_thresh']) & (df['premium_min_short'] < df['premium_thresh_lower']) & (df['whale_delta'] < 0)

    df['cvd_noise_thresh'] = np.maximum(CVD_NOISE_THRESHOLD_MINIMUM_VALUE, CVD_NOISE_THRESHOLD_FACTOR_MULTIPLIER * df['spot_delta'].rolling(SPOT_DELTA_VOLATILITY_LOOKBACK_BARS).std().fillna(0))

    if use_cvd_filter:
        cvd_roll_mean = df['spot_cvd'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).mean()
        raw_exhaust = raw_exhaust & (df['price'] >= df['price'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).max()) & (df['spot_cvd'] < cvd_roll_mean)
        raw_bottom = raw_bottom & (df['price'] <= df['price'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).min()) & (df['spot_cvd'] > cvd_roll_mean)
        
        df['is_breakout'] = raw_breakout & (df['cvd_trend'] > df['cvd_noise_thresh'])
        df['is_fakeout'] = raw_fakeout & (df['cvd_trend'] <= df['cvd_noise_thresh'])
        df['is_exhaustion'] = raw_exhaust & (df['cvd_trend'] < -df['cvd_noise_thresh'])
        df['is_breakdown'] = raw_breakdown & (df['cvd_trend'] < -df['cvd_noise_thresh'])
        df['is_long_liq'] = raw_liq & (df['cvd_trend'] >= -df['cvd_noise_thresh'])
        df['is_bottom_exhaust'] = raw_bottom & (df['cvd_trend'] > df['cvd_noise_thresh'])
    else:
        df['is_breakout'], df['is_fakeout'], df['is_exhaustion'], df['is_breakdown'], df['is_long_liq'], df['is_bottom_exhaust'] = raw_breakout, raw_fakeout, raw_exhaust, raw_breakdown, raw_liq, raw_bottom
        
    df['is_fakeout'] = df['is_fakeout'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust']
    df['is_long_liq'] = df['is_long_liq'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust']
    df['is_breakout'] = df['is_breakout'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust'] & ~df['is_fakeout'] & ~df['is_long_liq']
    df['is_breakdown'] = df['is_breakdown'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust'] & ~df['is_fakeout'] & ~df['is_long_liq']
    return df, False

def generate_signal_reason_v2(row, tf, name, active_cvd_filter):
    cvd_trend = float(row.get('cvd_trend', 0))
    cvd_noise = float(row.get('cvd_noise_thresh', 0.5))
    whale_delta = float(row.get('whale_delta', 0))
    
    if name in ["TRUE_BREAKOUT", "BOTTOM_EXHAUSTION"]:
        cvd_str = f"CVD {cvd_trend:.2f} > noise {cvd_noise:.2f}" if cvd_trend > cvd_noise else f"CVD {cvd_trend:.2f} <= noise {cvd_noise:.2f} (Weak)"
    elif name in ["TRUE_BREAKDOWN", "TOP_EXHAUSTION"]:
        cvd_str = f"CVD {cvd_trend:.2f} < -noise {-cvd_noise:.2f}" if cvd_trend < -cvd_noise else f"CVD {cvd_trend:.2f} >= -noise {-cvd_noise:.2f} (Weak)"
    elif name == "SHORT_SQUEEZE":
        cvd_str = f"CVD {cvd_trend:.2f} <= noise {cvd_noise:.2f}" if cvd_trend <= cvd_noise else f"CVD {cvd_trend:.2f} > noise {cvd_noise:.2f} (Weak)"
    else: 
        cvd_str = f"CVD {cvd_trend:.2f} >= -noise {-cvd_noise:.2f}" if cvd_trend >= -cvd_noise else f"CVD {cvd_trend:.2f} < -noise {-cvd_noise:.2f} (Weak)"
    
    whale_str = f"Raw < EMA (Δ {whale_delta:.3f})" if whale_delta < 0 else f"Raw > EMA (Δ {whale_delta:.3f})"
    
    if name == "TRUE_BREAKOUT":
        return f"Daemon {tf} | Spark: Price {row.get('price_change',0):+.2f}% > {row.get('price_thresh',0):.2f}% | Fuel: OI {LONG_ROLLING_WINDOW_BARS}-Bar {row.get('oi_accum_long',0):+.2f}% > {row.get('oi_accum_long_thresh',0):.2f}% | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    elif name == "TRUE_BREAKDOWN":
        return f"Daemon {tf} | Spark: Price {row.get('price_change',0):+.2f}% < -{row.get('price_thresh',0):.2f}% | Fuel: OI {LONG_ROLLING_WINDOW_BARS}-Bar {row.get('oi_accum_long',0):+.2f}% > {row.get('oi_accum_long_thresh',0):.2f}% | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    elif name == "SHORT_SQUEEZE":
        return f"Daemon {tf} | Cascade: OI {SHORT_ROLLING_WINDOW_BARS}-Bar {row.get('oi_accum_short',0):+.2f}% < -{row.get('oi_accum_short_thresh',0):.2f}% | Force: BuyPress {row.get('buy_pressure',0):.2f}x > {row.get('bp_thresh',0):.2f}x | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    elif name == "LONG_LIQUIDATION":
        return f"Daemon {tf} | Cascade: OI {SHORT_ROLLING_WINDOW_BARS}-Bar {row.get('oi_accum_short',0):+.2f}% < -{row.get('oi_accum_short_thresh',0):.2f}% | Force: SellPress {row.get('sell_pressure',0):.2f}x > {row.get('sp_thresh',0):.2f}x | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    elif name == "TOP_EXHAUSTION":
        return f"Daemon {tf} | Stretch: Price {LONG_ROLLING_WINDOW_BARS}-Bar {row.get('price_stretch',0):+.2f}% | Stall: Failed to break {SHORT_ROLLING_WINDOW_BARS}-Bar High | Danger: BuyPress 3-Bar Max > {row.get('bp_thresh',0):.2f}x | Overext: Basis Max > ${row.get('premium_thresh',0):.2f} | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    elif name == "BOTTOM_EXHAUSTION":
        return f"Daemon {tf} | Stretch: Price {LONG_ROLLING_WINDOW_BARS}-Bar {row.get('price_stretch',0):+.2f}% | Stall: Failed to break {SHORT_ROLLING_WINDOW_BARS}-Bar Low | Danger: SellPress 3-Bar Max > {row.get('sp_thresh',0):.2f}x | Overext: Basis Min < ${row.get('premium_thresh_lower',0):.2f} | {cvd_str} | Whale Trap: {whale_str} | Filter {'ON' if active_cvd_filter else 'OFF'}"
    return ""

async def clock_sync_daemon():
    global global_df
    while True:
        now_ts = time.time()
        next_epoch = (int(now_ts)//60+1)*60
        sleep_sec = next_epoch - now_ts
        if sleep_sec<0: sleep_sec=0
        await asyncio.sleep(sleep_sec)
        new_data = await fetch_binance_data()
        if not new_data:
            with df_lock:
                if not global_df.empty:
                    last = global_df.iloc[-1]
                    synth_ts = ist_from_epoch(next_epoch)
                    new_data = {
                        'timestamp':synth_ts,'price':float(last['price']),'basis':float(last['basis']),'oi':float(last['oi']),
                        'taker_imbalance':1.0,'whale_div':float(last['whale_div']),
                        'taker_imbalance_15m':1.0,'whale_div_15m':float(last.get('whale_div_15m', last['whale_div'])),
                        'taker_imbalance_30m':1.0,'whale_div_30m':float(last.get('whale_div_30m', last['whale_div'])),
                        'spot_delta':0.0
                    }
                else: continue
        with df_lock:
            tmp = pd.DataFrame([new_data])
            global_df = tmp if global_df.empty else pd.concat([global_df, tmp], ignore_index=True)
            if len(global_df)>MAX_HISTORY_ROWS:
                global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
                need_compact=True
            else: need_compact=False
        append_row_atomic(new_data)
        if need_compact: compact_csv_if_needed()
        try:
            with df_lock: base = global_df.copy()
            dt = ist_from_epoch(next_epoch)
            minute = dt.minute
            tfs=['1min']
            if minute%5==0: tfs.append('5min')
            if minute%15==0: tfs.append('15min')
            if minute%30==0: tfs.append('30min')
            with cvd_setting_lock: active_cvd_filter = current_cvd_filter_setting
            for tf in tfs:
                view = build_resampled_view(base, tf)
                if len(view)<100: continue
                sig_df, warm = compute_signals_for_view(view, tf, use_cvd_filter=active_cvd_filter)
                if warm or sig_df.empty: continue
                
                tf_map={'1min':1,'5min':5,'15min':15,'30min':30}
                tf_min=tf_map.get(tf,5)
                tf_delta=pd.Timedelta(minutes=tf_min)
                last_ts = sig_df['timestamp'].iloc[-1]
                is_forming = (last_ts + tf_delta) > now_ist()
                
                if is_forming and len(sig_df) >= 2:
                    closed_last = sig_df.iloc[-2]
                else:
                    closed_last = sig_df.iloc[-1]
                
                mp_closed = {
                    'TRUE_BREAKOUT': closed_last['is_breakout'],
                    'SHORT_SQUEEZE': closed_last['is_fakeout'],
                    'TRUE_BREAKDOWN': closed_last['is_breakdown'],
                    'LONG_LIQUIDATION': closed_last['is_long_liq'],
                    'TOP_EXHAUSTION': closed_last['is_exhaustion'],
                    'BOTTOM_EXHAUSTION': closed_last['is_bottom_exhaust']
                }
                
                for name, active in mp_closed.items():
                    if active:
                        epoch_int = int(closed_last['timestamp'].timestamp())
                        ts_str = closed_last['timestamp'].strftime('%Y-%m-%d %H:%M')
                        reason = generate_signal_reason_v2(closed_last, tf, name, active_cvd_filter)
                        
                        proxy_row = closed_last.copy()
                        if name in ["TRUE_BREAKOUT", "TRUE_BREAKDOWN"]:
                            proxy_row['oi_change'] = closed_last['oi_accum_long']
                            proxy_row['oi_thresh_upper'] = closed_last['oi_accum_long_thresh']
                            proxy_row['oi_thresh_lower'] = -closed_last['oi_accum_long_thresh']
                        elif name in ["SHORT_SQUEEZE", "LONG_LIQUIDATION"]:
                            proxy_row['oi_change'] = closed_last['oi_accum_short']
                            proxy_row['oi_thresh_upper'] = closed_last['oi_accum_short_thresh']
                            proxy_row['oi_thresh_lower'] = -closed_last['oi_accum_short_thresh']
                        elif name in ["TOP_EXHAUSTION", "BOTTOM_EXHAUSTION"]:
                            proxy_row['price_change'] = closed_last['price_stretch']
                            proxy_row['price_thresh'] = closed_last['price_stretch_thresh'] if name == "TOP_EXHAUSTION" else -closed_last['price_stretch_thresh']
                            proxy_row['buy_pressure'] = closed_last['bp_max_short']
                            proxy_row['sell_pressure'] = closed_last['sp_max_short']
                            proxy_row['basis'] = closed_last['premium_max_short'] if name == "TOP_EXHAUSTION" else closed_last['premium_min_short']
                        
                        log_signal_to_csv(epoch_int, ts_str, tf, name, closed_last['price'], reason, proxy_row)
        except Exception as e: print(f"daemon eval err {e}", flush=True)

def start_background_thread():
    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(clock_sync_daemon())
    threading.Thread(target=runner, daemon=True).start()

def create_metric_card(title, value, desc, thr, color="white"):
    return html.Div(style={'backgroundColor':'#222','padding':'15px','borderRadius':'8px','border':f'1px solid {color}','flex':'1','minWidth':'200px','margin':'10px','boxSizing':'border-box'}, children=[html.H3(title, style={'margin':'0 0 5px 0','fontSize':'16px','color':'#aaa'}), html.H2(value, style={'margin':'0 0 5px 0','color':color,'fontSize':'28px'}), html.P(desc, style={'margin':'0 0 5px 0','fontSize':'12px','color':'#777'}), html.P(thr, style={'margin':'0','fontSize':'11px','color':'#555','fontStyle':'italic'})])

def create_signal_card(title, active, col, env, data, strat, regime, thr):
    bg = f'rgba({col[4:-1]}, 0.15)' if active else '#141414'
    bc = col if active else '#333'
    tc = col if active else '#555'
    stt = "🚨 ACTION REQUIRED" if active else "SCANNING..."
    return html.Div(style={'backgroundColor':bg,'padding':'15px','borderRadius':'8px','border':f'2px solid {bc}','flex':'1','minWidth':'280px','margin':'0','textAlign':'left','boxSizing':'border-box','transition':'all 0.3s ease'}, children=[html.Div(style={'textAlign':'center','marginBottom':'10px'}, children=[html.H2(title, style={'margin':'0 0 5px 0','color':tc,'fontSize':'18px','fontWeight':'bold'}), html.H3(stt, style={'margin':'0','color':tc,'fontSize':'14px','letterSpacing':'1px'})]), html.Div(style={'borderTop':'1px solid #333','paddingTop':'10px','marginTop':'10px'}, children=[html.P([html.Strong("Env: ", style={'color':'#aaa'}), env], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Target: ", style={'color':'#aaa'}), data], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Action: ", style={'color':'#aaa'}), strat], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Regime: ", style={'color':'#aaa'}), regime], style={'margin':'0 0 10px 0','fontSize':'12px','color':'#888'})]), html.P(thr, style={'margin':'0','fontSize':'11px','color':tc,'fontWeight':'bold','textAlign':'center'})])

app = dash.Dash(__name__)
app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>{%favicon%} {%css%}<style>.Select-control { background-color: #222 !important; border-color: #444 !important; color: white !important; } .Select-menu-outer { background-color: #222 !important; color: white !important; } .Select-value-label { color: white !important; }</style></head><body>{%app_entry%}<footer>{%config%} {%scripts%} {%renderer%}</footer></body></html>"""
app.title = "Order Flow Terminal v3 Bulletproof"
app.layout = html.Div(style={'backgroundColor':'#111','color':'white','fontFamily':'Arial, sans-serif','padding':'20px','minHeight':'100vh','boxSizing':'border-box'}, children=[
    dcc.Store(id='last-signal-time', data=None),
    dcc.Store(id='sound-trigger', data=None),
    dcc.Store(id='audio-enabled', data=False),
    dcc.Store(id='active-sound-url', data='https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg', storage_type='local'),
    html.Div(id='audio-dummy', style={'display':'none'}),
    html.Div(id='preview-dummy', style={'display':'none'}),
    html.Div(style={'display':'none','justifyContent':'center','marginBottom':'15px'}, children=[html.Button("🔇 Click to Enable Audio", id='audio-enable-btn', n_clicks=0, style={'backgroundColor':'#111','color':'#ffaa00','border':'2px solid #ffaa00','borderRadius':'20px','padding':'8px 18px','fontWeight':'bold','cursor':'pointer','boxShadow':'0 0 10px rgba(255,170,0,0.3)'})]),
    html.H1("QUANTITATIVE ORDER FLOW TERMINAL v3 - BULLETPROOF", style={'textAlign':'center','letterSpacing':'2px','marginBottom':'5px'}),
    html.Div(id='last-updated-label', style={'textAlign':'center','color':'#888','fontSize':'14px','marginBottom':'20px','fontStyle':'italic'}),
    html.Div(style={'display':'flex','flexWrap':'wrap','justifyContent':'center','alignItems':'center','marginBottom':'20px','gap':'20px','backgroundColor':'#1a1a1a','padding':'15px','borderRadius':'8px','border':'1px solid #333','boxSizing':'border-box'}, children=[
        html.Div([html.Label("Timeframe: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='timeframe-dropdown', options=[{'label':'1 Minute','value':'1min'},{'label':'5 Minutes','value':'5min'},{'label':'15 Minutes','value':'15min'},{'label':'30 Minutes','value':'30min'}], value='5min', clearable=False, style={'width':'120px','display':'inline-block','color':'black','textAlign':'left'})]),
        html.Div([html.Label("History Range: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='range-dropdown', options=[{'label':'Last 1 Hour','value':'1h'},{'label':'Last 6 Hours','value':'6h'},{'label':'Last 24 Hours','value':'24h'},{'label':'Last 1 Week','value':'7d'},{'label':'All Time','value':'all'}], value='24h', clearable=False, style={'width':'140px','display':'inline-block','color':'black','textAlign':'left'})]),
        html.Div([html.Label("EMA Window: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Input(id='ema-window', type='number', value=20, min=2, max=200, style={'width':'60px','borderRadius':'4px','border':'none','padding':'8px','backgroundColor':'#fff','color':'#000'})]),
        html.Div([html.Label("CVD Filter: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Checklist(id='cvd-toggle', options=[{'label':' ON (Adaptive Noise + Divergence)','value':'on'}], value=[], style={'display':'inline-block','color':'white'})], style={'display':'none'}),
        html.Div([html.Label("Alert Sound: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='sound-dropdown', options=[{'label':'Sonar Ping','value':'https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg'},{'label':'Beep','value':'https://actions.google.com/sounds/v1/alarms/beep_short.ogg'},{'label':'Digital Watch','value':'https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg'},{'label':'Fire Alarm (LOUD)','value':'https://actions.google.com/sounds/v1/alarms/fire_alarm_bell_and_siren.ogg'},{'label':'Nuclear Siren (LOUD)','value':'https://actions.google.com/sounds/v1/alarms/radiation_meter.ogg'},{'label':'Siren Noise (LOUD)','value':'https://actions.google.com/sounds/v1/alarms/siren_noise.ogg'},{'label':'Bugle Tune (LOUD)','value':'https://actions.google.com/sounds/v1/alarms/bugle_tune.ogg'},{'label':'Mechanical Clock (LOUD)','value':'https://actions.google.com/sounds/v1/alarms/mechanical_clock_ring.ogg'},{'label':'Mute','value':'none'}], value='https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg', clearable=False, style={'width':'150px','display':'inline-block','color':'black','textAlign':'left'}, persistence=True, persistence_type='local')]),
        html.Div([html.Label("Duration (s): ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Input(id='sound-duration', type='number', value=6, min=1, max=60, style={'width':'60px','borderRadius':'4px','border':'none','padding':'8px','backgroundColor':'#fff','color':'#000'}, persistence=True, persistence_type='local')]),
        html.Div([html.Button("🔊 Preview", id='preview-sound-btn', n_clicks=0, style={'backgroundColor':'#222','color':'#00ffcc','border':'1px solid #00ffcc','borderRadius':'4px','padding':'8px 12px','fontWeight':'bold','cursor':'pointer','marginLeft':'10px'})]),
        html.Div([html.Button("✅ Set Tone", id='set-sound-btn', n_clicks=0, style={'backgroundColor':'#222','color':'#00ff00','border':'1px solid #00ff00','borderRadius':'4px','padding':'8px 12px','fontWeight':'bold','cursor':'pointer','marginLeft':'10px'})])
    ]),
    html.Div(id='signal-row-up', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px','marginBottom':'15px','boxSizing':'border-box'}),
    html.Div(id='signal-row-down', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px','marginBottom':'15px','boxSizing':'border-box'}),
    html.Div(id='metrics-row', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(200px, 1fr))','gap':'10px','marginTop':'10px','boxSizing':'border-box'}),
    html.Div(style={'backgroundColor':'#1a1a1a','border':'1px solid #333','borderRadius':'8px','padding':'15px','height':'180px','overflowY':'auto','marginTop':'20px','fontFamily':'monospace'}, children=[html.H3("EVENT LOG - DAEMON EPOCH HASHED - PRIORITY HIERARCHY", style={'margin':'0 0 10px 0','fontSize':'14px','color':'#aaa'}), html.Div(id='event-log-row')]),
    html.Div(dcc.Graph(id='main-chart', config={'displayModeBar':False}), style={'marginTop':'20px','border':'1px solid #333','borderRadius':'8px'}),
    dcc.Interval(id='interval-component', interval=UI_REFRESH_INTERVAL, n_intervals=0)
])

app.clientside_callback(
    """function(n_clicks, sound_val) { return sound_val; }""",
    Output('active-sound-url', 'data'),
    Input('set-sound-btn', 'n_clicks'),
    State('sound-dropdown', 'value')
)

app.clientside_callback(
    """function(n_clicks) { if(n_clicks && n_clicks>0) { try { window.audioCtxBypass = new (window.AudioContext || window.webkitAudioContext)(); if(window.audioCtxBypass.state === 'suspended') { window.audioCtxBypass.resume(); } } catch(e) { } return [true, "🔊 Audio Active - Alerts Enabled"]; } return [false, "🔇 Click to Enable Audio"]; }""",
    [Output('audio-enabled','data'), Output('audio-enable-btn','children')], Input('audio-enable-btn','n_clicks')
)

app.clientside_callback(
    """function(trigger, sound_url, duration, audio_enabled) {
        if(trigger && sound_url && sound_url !== 'none') {
            try {
                if (!window.audioCtxBypass) { window.audioCtxBypass = new (window.AudioContext || window.webkitAudioContext)(); }
                if (window.audioCtxBypass.state === 'suspended') { window.audioCtxBypass.resume(); }
                fetch(sound_url).then(function(res) { return res.arrayBuffer(); }).then(function(buf) { return window.audioCtxBypass.decodeAudioData(buf); }).then(function(decoded) {
                    if (window.daemonSource) { try { window.daemonSource.stop(); } catch(e){} }
                    var source = window.audioCtxBypass.createBufferSource();
                    source.buffer = decoded;
                    source.loop = true;
                    var gainNode = window.audioCtxBypass.createGain();
                    gainNode.gain.value = 4.0; // Force maximum piercing volume (400%)
                    source.connect(gainNode);
                    gainNode.connect(window.audioCtxBypass.destination);
                    source.start(0);
                    window.daemonSource = source;
                    if (window.daemonTimeout) { clearTimeout(window.daemonTimeout); }
                    window.daemonTimeout = setTimeout(function(){
                        if (window.daemonSource) { try { window.daemonSource.stop(); } catch(e){} }
                    }, (duration || 6) * 1000);
                }).catch(function(e) { console.log('Firefox blocked autoplay or network failed. Please whitelist 127.0.0.1', e); });
            } catch(err) { console.log('Audio routing err', err); }
        }
        return window.dash_clientside.no_update;
    }""",
    Output('audio-dummy','children'), Input('sound-trigger','data'), State('active-sound-url','data'), State('sound-duration','value'), State('audio-enabled','data')
)

app.clientside_callback(
    """function(n_clicks, sound_url, duration) {
        if(n_clicks && n_clicks > 0 && sound_url && sound_url !== 'none') {
            try {
                if (!window.audioCtxBypass) { window.audioCtxBypass = new (window.AudioContext || window.webkitAudioContext)(); }
                if (window.audioCtxBypass.state === 'suspended') { window.audioCtxBypass.resume(); }
                fetch(sound_url).then(function(res) { return res.arrayBuffer(); }).then(function(buf) { return window.audioCtxBypass.decodeAudioData(buf); }).then(function(decoded) {
                    if (window.previewSource) { try { window.previewSource.stop(); } catch(e){} }
                    var source = window.audioCtxBypass.createBufferSource();
                    source.buffer = decoded;
                    source.loop = true;
                    var gainNode = window.audioCtxBypass.createGain();
                    gainNode.gain.value = 4.0; // Force maximum piercing volume (400%)
                    source.connect(gainNode);
                    gainNode.connect(window.audioCtxBypass.destination);
                    source.start(0);
                    window.previewSource = source;
                    if (window.previewTimeout) { clearTimeout(window.previewTimeout); }
                    window.previewTimeout = setTimeout(function(){
                        if (window.previewSource) { try { window.previewSource.stop(); } catch(e){} }
                    }, (duration || 6) * 1000);
                }).catch(function(e) { console.log('Preview playback failed:', e); });
            } catch(err) { console.log('Preview routing err', err); }
        }
        return window.dash_clientside.no_update;
    }""",
    Output('preview-dummy','children'), Input('preview-sound-btn','n_clicks'), State('sound-dropdown','value'), State('sound-duration','value')
)

@app.callback(
    [Output('last-updated-label','children'), Output('signal-row-up','children'), Output('signal-row-down','children'), Output('metrics-row','children'), Output('event-log-row','children'), Output('main-chart','figure'), Output('last-signal-time','data'), Output('sound-trigger','data')],
    [Input('interval-component','n_intervals'), Input('timeframe-dropdown','value'), Input('range-dropdown','value'), Input('ema-window','value'), Input('cvd-toggle','value')],
    [State('last-signal-time','data')]
)
def update_dashboard(n, timeframe, time_range, ema_window, cvd_toggle, last_signal_time):
    global current_cvd_filter_setting
    use_cvd = 'on' in (cvd_toggle or [])
    with cvd_setting_lock: current_cvd_filter_setting = use_cvd
    with df_lock:
        if global_df.empty or len(global_df)<2: return "Initializing...", html.H3("GATHERING...", style={'textAlign':'center','color':'grey'}), "", "", "", go.Figure(), dash.no_update, dash.no_update
        base = global_df.copy()
    view = build_resampled_view(base, timeframe)
    if len(view)<2: return "Resampling...", html.H3("CALIBRATING...", style={'color':'grey'}), "", "", "", go.Figure(), dash.no_update, dash.no_update
    now_live = now_ist()
    tf_map={'1min':1,'5min':5,'15min':15,'30min':30}
    tf_min=tf_map.get(timeframe,5)
    tf_delta=pd.Timedelta(minutes=tf_min)
    
    df_display, warm = compute_signals_for_view(view, timeframe, use_cvd_filter=use_cvd)
    ema_window = ema_window if ema_window else 20
    live_str = now_live.strftime('%Y-%m-%d %H:%M:%S')
    if warm:
        txt=f"CALIBRATING ENGINE: [{len(df_display)}/100] ROWS"
        last_upd=f"System: {live_str} IST | {txt} | Spot CVD Filter: {'Active' if use_cvd else 'Inactive'}"
        fig=go.Figure()
        fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',height=1150)
        card=html.Div(style={'backgroundColor':'#1a1a1a','padding':'20px','borderRadius':'8px','border':'1px solid #ffaa00','textAlign':'center'},children=[html.H2(f"⏳ {txt}",style={'color':'#ffaa00'}),html.P("Gathering statistical baseline.",style={'color':'#888','fontSize':'12px'})])
        return last_upd, card, html.Div(), card, html.Div("Calibrating..."), fig, last_signal_time, dash.no_update

    last_ts = df_display['timestamp'].iloc[-1]
    is_forming = (last_ts + tf_delta) > now_live
    
    if is_forming and len(df_display) >= 2:
        live_cur = df_display.iloc[-1]
        closed_cur = df_display.iloc[-2]
        forming = last_ts
    else:
        live_cur = df_display.iloc[-1]
        closed_cur = df_display.iloc[-1]
        forming = None

    closed_str = closed_cur['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
    form_info = f" | Forming {forming.strftime('%H:%M')}..." if forming is not None else ""
    last_upd = f"System: {live_str} IST | Last Closed: {closed_str}{form_info} | Spot CVD Filter: {'Active' if use_cvd else 'Inactive'} (Noise Thresh: {live_cur.get('cvd_noise_thresh',0.5):.2f})"

    active = False
    an = "NONE"
    cur = closed_cur.copy()
    
    if cur['is_breakout']: active=True; an="TRUE_BREAKOUT"
    elif cur['is_breakdown']: active=True; an="TRUE_BREAKDOWN"
    elif cur['is_fakeout']: active=True; an="SHORT_SQUEEZE"
    elif cur['is_long_liq']: active=True; an="LONG_LIQUIDATION"
    elif cur['is_exhaustion']: active=True; an="TOP_EXHAUSTION"
    elif cur['is_bottom_exhaust']: active=True; an="BOTTOM_EXHAUSTION"

    epoch_id=int(cur['timestamp'].timestamp())
    sig_hash=f"{epoch_id}_{timeframe}_{an}"
    sound=dash.no_update
    if active and last_signal_time!=sig_hash:
        sound=sig_hash
        last_signal_time=sig_hash
        
    up = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px'}, children=[
        create_signal_card("🟩 TRUE BREAKOUT", cur['is_breakout'], "rgb(0, 255, 0)", "Trend Continuation", f"Price UP + OI {LONG_ROLLING_WINDOW_BARS}-Bar Accum + Whales Buying.", "Ride the Trend. Enter Long on close.", "High Volatility", f"Filters: Price > +{cur.get('price_thresh',0):.2f}% | OI Accum > +{cur.get('oi_accum_long_thresh',0):.2f}% | Whale Raw < EMA"),
        create_signal_card("🟪 LONG LIQUIDATION", cur['is_long_liq'], "rgb(170, 0, 255)", "Mean Reversion (Dip)", f"Cascade OI Drop + Extreme Sell Press + Whale Support.", "Fade the Flush. Enter Long.", "Low Volatility", f"Filters: OI Accum < -{cur.get('oi_accum_short_thresh',0):.2f}% | Sell Press > {cur.get('sp_thresh',0):.2f}x | Whale Raw < EMA"),
        create_signal_card("🔵 BOTTOM EXHAUSTION", cur['is_bottom_exhaust'], "rgb(0, 200, 255)", "Macro Reversal", f"Stretch Down + Stall + Danger Zone + Whale Trap.", "Catch the Knife. Enter Long.", "Parabolic Bear / Death Spiral", f"Filters: Sell Press Max > {cur.get('sp_thresh',0):.2f}x | Premium Min < ${cur.get('premium_thresh_lower', -5.0):.2f} | Whale Raw < EMA")
    ])
    down = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px'}, children=[
        create_signal_card("🟥 TRUE BREAKDOWN", cur['is_breakdown'], "rgb(255, 0, 0)", "Trend Continuation", f"Price DOWN + OI {LONG_ROLLING_WINDOW_BARS}-Bar Accum + Whales Selling.", "Ride the Trend. Enter Short on close.", "High Volatility", f"Filters: Price < -{cur.get('price_thresh',0):.2f}% | OI Accum > +{cur.get('oi_accum_long_thresh',0):.2f}% | Whale Raw > EMA"),
        create_signal_card("🟧 SHORT SQUEEZE", cur['is_fakeout'], "rgb(255, 165, 0)", "Mean Reversion (Top)", f"Cascade OI Drop + Extreme Buy Press + Whale Dist.", "Fade the Fakeout. Enter Short post-spike.", "Low Volatility", f"Filters: OI Accum < -{cur.get('oi_accum_short_thresh',0):.2f}% | Buy Press > {cur.get('bp_thresh',0):.2f}x | Whale Raw > EMA"),
        create_signal_card("🔴 TOP EXHAUSTION", cur['is_exhaustion'], "rgb(255, 50, 50)", "Macro Reversal", f"Stretch Up + Stall + Danger Zone + Whale Trap.", "Top Tick. Enter Short.", "Parabolic Bull Run", f"Filters: Buy Press Max > {cur.get('bp_thresh',0):.2f}x | Premium Max > ${cur.get('premium_thresh', 5.0):.2f} | Whale Raw > EMA")
    ])
    
    metrics = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(200px, 1fr))','gap':'10px'}, children=[
        create_metric_card("Price Stretch", f"{cur.get('price_stretch',0):+.2f}%", f"Net price movement over {LONG_ROLLING_WINDOW_BARS} Bars.", f"Dynamic Vol Threshold: ±{cur.get('price_stretch_thresh',0):.2f}%", "#0f0" if cur.get('price_stretch',0)>0 else "#f00"),
        create_metric_card("OI Accumulation", f"{cur.get('oi_accum_long',0):+.2f}%", f"Net OI change over {LONG_ROLLING_WINDOW_BARS} Bars.", f"Dynamic Target: > +{cur.get('oi_accum_long_thresh',0):.2f}% or < -{cur.get('oi_accum_short_thresh',0):.2f}%", "#0ff"),
        create_metric_card("Taker Buy Pressure", f"{cur.get('buy_pressure',1.0):.2f}x", "Market Buy vs Sell Volume (Capped).", f"Danger Zone Limit: > {cur.get('bp_thresh',0):.2f}x", "cyan"),
        create_metric_card("Taker Sell Pressure", f"{cur.get('sell_pressure',1.0):.2f}x", "Market Sell vs Buy Volume (Capped).", f"Danger Zone Limit: > {cur.get('sp_thresh',0):.2f}x", "#ff5555"),
        create_metric_card("Basis Premium", f"${cur.get('basis',0):.2f}", "Futures Price minus Spot Price.", f"Regime Limits: > ${cur.get('premium_thresh', 5.0):.2f} | < ${cur.get('premium_thresh_lower', -5.0):.2f}", "orange")
    ])
    
    logs=[]
    try:
        if os.path.exists(SIGNAL_LOG_FILE):
            ldf=pd.read_csv(SIGNAL_LOG_FILE, on_bad_lines='skip')
            if not ldf.empty:
                # FIX: Explicitly sort the loaded CSV by epoch so out-of-order appends during testing don't break the UI timeline
                if 'epoch' in ldf.columns:
                    ldf = ldf.sort_values('epoch')
                
                filt = ldf[ldf['timeframe']==timeframe].tail(20) if 'timeframe' in ldf.columns else ldf.tail(20)
                if filt.empty: filt=ldf.tail(20)
                for _, r in filt.iloc[::-1].iterrows():
                    ts=r.get('timestamp',''); sig=r.get('signal',''); pr=r.get('price',0)
                    pr = 0 if (isinstance(pr, float) and pd.isna(pr)) else pr
                    sig = '' if (isinstance(sig, float) and pd.isna(sig)) else str(sig)
                    rs=r.get('reason','')
                    rs = '' if (isinstance(rs, float) and pd.isna(rs)) else str(rs)
                    col = 'lime' if 'BREAKOUT' in sig else 'red' if 'BREAKDOWN' in sig else 'orange' if 'SQUEEZE' in sig else 'rgb(170, 0, 255)' if 'LIQ' in sig else 'rgb(255, 50, 50)' if 'TOP' in sig else 'rgb(0, 200, 255)'
                    emoji = '🟩' if 'BREAKOUT' in sig else '🟥' if 'BREAKDOWN' in sig else '🟧' if 'SQUEEZE' in sig else '🟪' if 'LIQ' in sig else '🔴' if 'TOP' in sig else '🔵'
                    logs.append(html.Div([html.Span(f"[{ts}] {emoji} {sig} @ ${pr:,.2f}", style={'fontWeight':'bold'}), html.Br(), html.Span(f"↳ {rs}", style={'fontSize':'11px','color':'#888','marginLeft':'10px'})], style={'color':col,'marginBottom':'8px'}))
            else: logs=[html.Div("Waiting for daemon signal", style={'color':'#555'})]
        else: logs=[html.Div("Log not yet created", style={'color':'#555'})]
    except Exception as e: logs=[html.Div(f"Log err {e}", style={'color':'#f55'})]
        
    chart = df_display.copy()
    computed_bp=np.clip(np.where(chart['taker_imbalance']>0,1/chart['taker_imbalance'],1),0,PRESSURE_CLIP_MAX)
    computed_sp=np.clip(chart['taker_imbalance'],0,PRESSURE_CLIP_MAX)
    chart['buy_pressure']=chart.get('buy_pressure', computed_bp).fillna(pd.Series(computed_bp, index=chart.index))
    chart['sell_pressure']=chart.get('sell_pressure', computed_sp).fillna(pd.Series(computed_sp, index=chart.index))
    chart['ema_buy']=chart['buy_pressure'].ewm(span=ema_window,adjust=False).mean()
    chart['ema_sell']=chart['sell_pressure'].ewm(span=ema_window,adjust=False).mean()
    chart['ema_whale']=chart['whale_div'].ewm(span=WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN,adjust=False).mean()
    chart['ema_basis']=chart['basis'].ewm(span=ema_window,adjust=False).mean()
    chart['ema_cvd']=chart['spot_cvd'].ewm(span=ema_window,adjust=False).mean()
    
    if time_range!='all':
        mt=chart['timestamp'].max()
        if time_range=='1h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=1)]
        elif time_range=='6h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=6)]
        elif time_range=='24h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=24)]
        elif time_range=='7d': chart=chart[chart['timestamp']>=mt-pd.Timedelta(days=7)]
        
    fig=make_subplots(rows=6,cols=1,shared_xaxes=True,vertical_spacing=0.04,row_heights=[0.30,0.12,0.12,0.14,0.14,0.18],subplot_titles=("Price & Open Interest (Forming)","Taker Buy Pressure","Taker Sell Pressure","Whale Ratio","Basis Premium","Spot Cumulative Volume Delta (CVD)"),specs=[[{"secondary_y":True}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}]])
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['price'],name="Price",line=dict(color='white',width=2)),row=1,col=1,secondary_y=False)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['oi'],name="OI",line=dict(color='cyan',dash='dot')),row=1,col=1,secondary_y=True)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['buy_pressure'],name="Buy Press",line=dict(color='magenta')),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_buy'],name=f"EMA {ema_window}",line=dict(color='rgba(255,255,255,0.6)',dash='dot')),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['sell_pressure'],name="Sell Press",line=dict(color='#ff5555')),row=3,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_sell'],name=f"EMA {ema_window}",line=dict(color='rgba(255,200,200,0.6)',dash='dot')),row=3,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['whale_div'],name="Whale Raw",line=dict(color='rgba(255,255,0,0.3)')),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_whale'],name=f"Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN}",line=dict(color='yellow',width=2)),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart.get('premium_thresh', np.nan),name="90%",line=dict(color='rgba(255,165,0,0.3)',dash='dash')),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['basis'],name="Basis",line=dict(color='orange'),fill='tonexty'),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart.get('premium_thresh_lower', np.nan),name="10%",line=dict(color='rgba(0,200,255,0.3)',dash='dash')),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['spot_cvd'],name="CVD Session UTC",line=dict(color='cyan'),fill='tozeroy'),row=6,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_cvd'],name="EMA",line=dict(color='white',dash='dot')),row=6,col=1)
    fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',margin=dict(l=40,r=40,t=60,b=20),hovermode="x unified",legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="left",x=0),height=1150,uirevision='locked')
    fig.update_xaxes(showgrid=True,gridcolor='#222')
    return last_upd, up, down, metrics, logs, fig, last_signal_time, sound

if __name__ == '__main__':
    start_background_thread()
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8050'))
    print(f"Starting on http://{host}:{port}", flush=True)
    app.run(host=host, port=port, debug=False)

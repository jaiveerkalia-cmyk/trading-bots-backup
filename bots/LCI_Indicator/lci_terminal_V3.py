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
import tempfile
import math

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
# AUDITED FINAL VERSION - OI comparison and single whale mention fixed - 2026-07-13

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
WHALE_EMA_SPAN = WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN
CVD_NOISE_THRESHOLD_MINIMUM_VALUE = 0.5
CVD_NOISE_THRESHOLD_FACTOR_MULTIPLIER = 0.2
CVD_NOISE_MIN = CVD_NOISE_THRESHOLD_MINIMUM_VALUE
CVD_NOISE_FACTOR = CVD_NOISE_THRESHOLD_FACTOR_MULTIPLIER
VOLATILITY_SCALER_ENABLED = False
DIVERGENCE_LOOKBACK_BARS = 10
DIVERGENCE_LOOKBACK = DIVERGENCE_LOOKBACK_BARS
PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS = 30
OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS = 30
PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS = 1.5
BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS = 100
TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS = 100
TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS = 100
SPOT_DELTA_VOLATILITY_LOOKBACK_BARS = 20
EXPECTED_CSV_COLUMN_ORDER = ['timestamp','price','basis','oi','taker_imbalance','whale_div','spot_delta']
EXPECTED_COLS = EXPECTED_CSV_COLUMN_ORDER
SIGNAL_CSV_COLUMN_ORDER = ['epoch','timestamp','timeframe','signal','price','reason','price_change','price_thresh','oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure','sp_thresh','basis','premium_thresh','premium_thresh_lower']

# --- IST wall-clock helpers -----------------------------------------------
# India does not observe DST, so a fixed UTC+5:30 offset is always exactly
# correct. Using datetime.now()/fromtimestamp() directly assumes the HOST
# machine's OS clock is already set to IST - on a server whose OS timezone
# is UTC (common on cloud hosts/containers) that assumption silently shifts
# every stored timestamp, the daily session-CVD reset, and the "IST" label
# in the status bar by up to 5.5 hours. These are drop-in replacements that
# compute true IST regardless of the host's configured timezone.
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def now_ist():
    """Current true India Standard Time as a naive datetime."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(IST_TZ).replace(tzinfo=None)

def ist_from_epoch(epoch_seconds):
    """Convert a UTC unix epoch (e.g. from time.time()) to a naive true-IST datetime."""
    return datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc).astimezone(IST_TZ).replace(tzinfo=None)


DATA_FILE = "data/lci_history.csv"
SIGNAL_LOG_FILE = "data/signal_events.csv"
os.makedirs("data", exist_ok=True)

if os.path.exists(DATA_FILE):
    global_df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows")
else:
    global_df = pd.DataFrame(columns=['timestamp','price','basis','oi','taker_imbalance','whale_div','spot_delta'])

df_lock = threading.Lock()
file_lock = threading.Lock()
csv_lock = file_lock
cvd_setting_lock = threading.Lock()
# Mirrors whatever CVD-filter state the live dashboard last had selected, so the
# background daemon's persisted event log (signal_events.csv) stays consistent
# with what the UI is showing instead of always evaluating against the fixed
# USE_SPOT_CVD_FILTER_DEFAULT constant regardless of the user's live toggle.
current_cvd_filter_setting = USE_SPOT_CVD_FILTER_DEFAULT

logged_signals = set()
if os.path.exists(SIGNAL_LOG_FILE):
    try:
        sdf = pd.read_csv(SIGNAL_LOG_FILE)
        for _, r in sdf.iterrows():
            if 'epoch' in sdf.columns:
                logged_signals.add(f"{int(r['epoch'])}_{r['timeframe']}_{r['signal']}")
            else:
                logged_signals.add(f"{r['timestamp']}_{r['timeframe']}_{r['signal']}")
        print(f"Loaded {len(logged_signals)} signals")
    except Exception:
        pass

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
                print(f"API FAIL {url}: {e}", flush=True)
                return None
            await asyncio.sleep(2 ** attempt)
    return None

async def fetch_binance_data():
    async with aiohttp.ClientSession() as session:
        urls = {
            "spot": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            "spot_kline": "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1",
            "premium": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            "oi": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            "taker": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=1",
            "acc_ratio": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
            "pos_ratio": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=1"
        }
        tasks = {k: fetch_json(session, u) for k,u in urls.items()}
        results = await asyncio.gather(*tasks.values())
        data = dict(zip(tasks.keys(), results))
        if not data.get("spot") or not data.get("premium") or not data.get("oi"):
            return None
        ts = now_ist().replace(second=0, microsecond=0)
        try:
            spot_price = float(data["spot"]["price"])
            mark = float(data["premium"]["markPrice"])
            index = float(data["premium"]["indexPrice"])
            oi = float(data["oi"]["openInterest"])
            taker_imb = 1.0
            whale = 1.0
            if data.get("taker") and len(data["taker"])>0:
                sell = float(data["taker"][0]["sellVol"]); buy = float(data["taker"][0]["buyVol"])
                taker_imb = sell/buy if buy>0 else 1.0
            if data.get("acc_ratio") and data.get("pos_ratio"):
                ar = float(data["acc_ratio"][0]["longShortRatio"]); pr = float(data["pos_ratio"][0]["longShortRatio"])
                whale = ar/pr if pr>0 else 1.0
            basis = mark - index
            taker_imb = float(np.clip(taker_imb, PRESSURE_CLIP_MIN, PRESSURE_CLIP_MAX))
            whale = float(np.clip(whale, PRESSURE_CLIP_MIN, PRESSURE_CLIP_MAX))
            spot_delta = 0.0
            if data.get("spot_kline"):
                k = data["spot_kline"][0]
                tot = float(k[5]); tb = float(k[9]); spot_delta = tb - (tot - tb)
                spot_delta = float(np.clip(spot_delta, -100, 100))
            return {'timestamp':ts,'price':spot_price,'basis':basis,'oi':oi,'taker_imbalance':taker_imb,'whale_div':whale,'spot_delta':spot_delta}
        except Exception as e:
            print(f"parse err {e}", flush=True)
            return None

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
                import datetime, os as _os
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = f"{DATA_FILE}.mismatch_{ts}.bak"
                _os.rename(DATA_FILE, bak)
                df_row.to_csv(DATA_FILE, index=False, date_format='%Y-%m-%d %H:%M:%S')
                return
        except:
            pass
        df_row.to_csv(DATA_FILE, mode='a', header=False, index=False, date_format='%Y-%m-%d %H:%M:%S')

def compact_csv_if_needed():
    with file_lock:
        try:
            with df_lock:
                if global_df.empty: return
                trimmed = global_df[EXPECTED_CSV_COLUMN_ORDER].iloc[-MAX_HISTORY_ROWS:].copy()
            import tempfile as _tf, os as _os
            with _tf.NamedTemporaryFile(mode='w', delete=False, dir='data', suffix='.tmp') as tf:
                tmp = tf.name
                trimmed.to_csv(tmp, index=False, date_format='%Y-%m-%d %H:%M:%S')
            _os.replace(tmp, DATA_FILE)
        except Exception as e:
            print(f"compact fail {e}", flush=True)

def log_signal_to_csv(epoch_int, ts_str, timeframe, sig_name, price, reason, row):
    sig_id = f"{epoch_int}_{timeframe}_{sig_name}"
    with file_lock:
        if sig_id not in logged_signals:
            logged_signals.add(sig_id)
            d = {'epoch': epoch_int, 'timestamp': ts_str, 'timeframe': timeframe, 'signal': sig_name, 'price': float(price), 'reason': str(reason),
                 'price_change':0.0,'price_thresh':0.0,'oi_change':0.0,'oi_thresh_upper':0.0,'oi_thresh_lower':0.0,'buy_pressure':0.0,'bp_thresh':0.0,'sell_pressure':0.0,'sp_thresh':0.0,'basis':0.0,'premium_thresh':0.0,'premium_thresh_lower':0.0}
            try:
                if isinstance(row, dict):
                    for k in d:
                        if k in row and k not in ['epoch','timestamp','timeframe','signal','price','reason']:
                            try: d[k]=float(row.get(k,d[k]))
                            except: pass
                else:
                    for k in ['price_change','price_thresh','oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure','sp_thresh','basis','premium_thresh','premium_thresh_lower']:
                        if k in row:
                            try: d[k]=float(row[k])
                            except: pass
            except: pass
            hdr = not os.path.exists(SIGNAL_LOG_FILE)
            df_out = pd.DataFrame([d], columns=SIGNAL_CSV_COLUMN_ORDER)[SIGNAL_CSV_COLUMN_ORDER]
            df_out.to_csv(SIGNAL_LOG_FILE, mode='a', header=hdr, index=False)

def build_resampled_view(base_df, timeframe):
    if base_df.empty:
        return pd.DataFrame()
    df = base_df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    if timeframe != '1min':
        agg = {'price':'last','basis':'last','oi':'last','taker_imbalance':'last','whale_div':'last','spot_delta':'sum'}
        res = df.resample(timeframe, closed='left', label='left').agg(agg)
        res[['taker_imbalance','whale_div']] = res[['taker_imbalance','whale_div']].ffill()
        res = res.dropna(subset=['price','basis','oi'])
        df = res
    df = df.reset_index()
    # Session anchored at 00:00 UTC - Bug 1 final: convert IST naive to UTC date
    try:
        # Assume timestamps are IST (Asia/Kolkata) from datetime.now() on your machine
        ts_ist = df['timestamp'].dt.tz_localize('Asia/Kolkata', ambiguous='infer', nonexistent='shift_forward')
        ts_utc = ts_ist.dt.tz_convert('UTC')
        df['session_date'] = ts_utc.dt.floor('D').dt.tz_localize(None)
    except Exception:
        # Fallback if server already UTC or tz naive fails
        df['session_date'] = (df['timestamp'] - pd.Timedelta(hours=5, minutes=30)).dt.normalize()
    df['spot_cvd'] = df.groupby('session_date')['spot_delta'].cumsum()
    df['cvd_trend'] = df['spot_delta']
    df['time_diff'] = df['timestamp'].diff()
    return df

def compute_signals_for_view(df_input, timeframe, use_cvd_filter=False):
    if len(df_input) < 2:
        return df_input, True
    df = df_input.copy()
    df['whale_div_smooth'] = df['whale_div'].ewm(span=WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN, adjust=False).mean()
    df['whale_delta'] = df['whale_div_smooth'].diff()
    df['taker_imbalance'] = np.clip(df['taker_imbalance'], PRESSURE_CLIP_MIN, PRESSURE_CLIP_MAX)
    df['buy_pressure'] = np.clip(np.where(df['taker_imbalance']>0, 1/df['taker_imbalance'], 1), 0, PRESSURE_CLIP_MAX)
    df['sell_pressure'] = np.clip(df['taker_imbalance'], 0, PRESSURE_CLIP_MAX)
    df['price_change'] = df['price'].pct_change()*100
    df['oi_change'] = df['oi'].pct_change()*100
    tf_map = {'1min':1,'5min':5,'15min':15,'30min':30}
    tf_min = tf_map.get(timeframe,5)
    gap_thr = pd.Timedelta(minutes=tf_min*1.5)
    df.loc[df['time_diff'] > gap_thr, ['price_change','oi_change']] = np.nan
    abs_price = df['price_change'].abs()
    abs_oi = df['oi_change'].abs()
    ewm_price_mean = abs_price.ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_price_std = abs_price.ewm(span=PRICE_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['price_thresh'] = ewm_price_mean + (ewm_price_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    ewm_oi_mean = abs_oi.ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).mean()
    ewm_oi_std = abs_oi.ewm(span=OPEN_INTEREST_CHANGE_THRESHOLD_LOOKBACK_BARS, adjust=False).std().fillna(0)
    df['oi_thresh_upper'] = ewm_oi_mean + (ewm_oi_std * PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    df['oi_thresh_lower'] = -df['oi_thresh_upper']
    df['premium_thresh'] = df['basis'].rolling(BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS).quantile(0.90)
    df['premium_thresh_lower'] = df['basis'].rolling(BASIS_PREMIUM_THRESHOLD_LOOKBACK_BARS).quantile(0.10)
    df['bp_thresh'] = df['buy_pressure'].rolling(TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS).mean() + (df['buy_pressure'].rolling(TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS).std().fillna(0)*PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    df['sp_thresh'] = df['sell_pressure'].rolling(TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS).mean() + (df['sell_pressure'].rolling(TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS).std().fillna(0)*PRICE_THRESHOLD_MULTIPLIER_STANDARD_DEVIATIONS)
    tf_scaler = 1.0
    if VOLATILITY_SCALER_ENABLED:
        import math
        tf_scaler = math.sqrt(5.0 / max(tf_min,1))
        df['price_thresh'] *= tf_scaler
        df['oi_thresh_upper'] *= tf_scaler
        df['oi_thresh_lower'] = -df['oi_thresh_upper']
    if len(df) < 100:
        return df, True
    for col in ['price_thresh','oi_thresh_upper','oi_thresh_lower','premium_thresh','premium_thresh_lower','bp_thresh','sp_thresh']:
        df[col] = df[col].fillna({'price_thresh':0.15,'oi_thresh_upper':0.4,'oi_thresh_lower':-0.4,'premium_thresh':8.0,'premium_thresh_lower':-8.0,'bp_thresh':1.3,'sp_thresh':1.3}.get(col,0))
    raw_breakout = (df['price_change'] > df['price_thresh']) & (df['oi_change'] > df['oi_thresh_upper']) & (df['whale_delta'] < 0)
    raw_fakeout = (df['price_change'] > df['price_thresh']) & (df['oi_change'] < df['oi_thresh_lower'])
    raw_exhaust = (df['price_change'] > 0) & (df['buy_pressure'] > df['bp_thresh']) & (df['basis'] > df['premium_thresh']) & (df['whale_delta'] > 0)
    raw_breakdown = (df['price_change'] < -df['price_thresh']) & (df['oi_change'] > df['oi_thresh_upper']) & (df['whale_delta'] > 0)
    raw_liq = (df['price_change'] < -df['price_thresh']) & (df['oi_change'] < df['oi_thresh_lower'])
    raw_bottom = (df['price_change'] < 0) & (df['sell_pressure'] > df['sp_thresh']) & (df['basis'] < df['premium_thresh_lower']) & (df['whale_delta'] < 0)
    price_roll_max = df['price'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).max()
    price_roll_min = df['price'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).min()
    try:
        cvd_roll_mean = df.groupby('session_date')['spot_cvd'].transform(lambda x: x.rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).mean())
    except:
        cvd_roll_mean = df['spot_cvd'].rolling(DIVERGENCE_LOOKBACK_BARS, min_periods=5).mean()
    raw_exhaust = raw_exhaust & (df['price'] >= price_roll_max) & (df['spot_cvd'] < cvd_roll_mean)
    raw_bottom = raw_bottom & (df['price'] <= price_roll_min) & (df['spot_cvd'] > cvd_roll_mean)
    spot_std = df['spot_delta'].rolling(SPOT_DELTA_VOLATILITY_LOOKBACK_BARS).std().fillna(0)
    df['cvd_noise_thresh'] = np.maximum(CVD_NOISE_THRESHOLD_MINIMUM_VALUE, CVD_NOISE_THRESHOLD_FACTOR_MULTIPLIER * spot_std)
    if use_cvd_filter:
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


def generate_detailed_cumulative_volume_delta_inference_description(signal_name, cumulative_volume_delta_trend_value, cumulative_volume_delta_noise_threshold_value, spot_cumulative_volume_delta_value, cumulative_volume_delta_rolling_mean_value, price_change_value, open_interest_change_value):
    try:
        trend=float(cumulative_volume_delta_trend_value); noise=float(cumulative_volume_delta_noise_threshold_value)
        cvd=float(spot_cumulative_volume_delta_value) if spot_cumulative_volume_delta_value is not None else 0.0
        cvd_mean=float(cumulative_volume_delta_rolling_mean_value) if cumulative_volume_delta_rolling_mean_value is not None else 0.0
    except:
        return f"CVD {cumulative_volume_delta_trend_value} vs noise {cumulative_volume_delta_noise_threshold_value}"
    if signal_name=="TRUE_BREAKOUT": return f"CVD {trend:.2f} > noise {noise:.2f} = strong spot buying confirms breakout, VALID" if trend>noise else f"CVD {trend:.2f} <= noise {noise:.2f} = weak spot buying, would INVALIDATE breakout if filter ON"
    if signal_name=="TRUE_BREAKDOWN": return f"CVD {trend:.2f} < -noise {-noise:.2f} = strong spot selling confirms breakdown, VALID" if trend<-noise else f"CVD {trend:.2f} >= -noise {-noise:.2f} = weak spot selling, would INVALIDATE breakdown if filter ON"
    if signal_name=="SHORT_SQUEEZE": return f"CVD {trend:.2f} <= noise {noise:.2f} = low spot buying, VALID squeeze" if trend<=noise else f"CVD {trend:.2f} > noise {noise:.2f} = strong spot buying, would INVALIDATE squeeze if filter ON, real demand"
    if signal_name=="LONG_LIQUIDATION": return f"CVD {trend:.2f} >= -noise {-noise:.2f} = limited spot selling, VALID long liq" if trend>=-noise else f"CVD {trend:.2f} < -noise {-noise:.2f} = strong spot selling, would INVALIDATE long liq if filter ON"
    if signal_name=="TOP_EXHAUSTION":
        div="CVD below mean" if cvd<cvd_mean else "CVD above mean"
        return f"CVD {trend:.2f} < -noise {-noise:.2f} + CVD {cvd:.2f} < mean {cvd_mean:.2f} ({div}) = bearish divergence VALID top" if trend<-noise and cvd<cvd_mean else f"CVD {trend:.2f} vs -noise {-noise:.2f}, CVD {cvd:.2f} vs mean {cvd_mean:.2f} ({div}) = divergence weak"
    if signal_name=="BOTTOM_EXHAUSTION":
        div="CVD above mean" if cvd>cvd_mean else "CVD below mean"
        return f"CVD {trend:.2f} > noise {noise:.2f} + CVD {cvd:.2f} > mean {cvd_mean:.2f} ({div}) = bullish divergence VALID bottom" if trend>noise and cvd>cvd_mean else f"CVD {trend:.2f} vs noise {noise:.2f}, CVD {cvd:.2f} vs mean {cvd_mean:.2f} ({div}) = divergence weak"
    return f"CVD {trend:.2f} vs noise {noise:.2f}"

def generate_whale_delta_comparison_text(whale_delta_value):
    try: wd=float(whale_delta_value)
    except: wd=0.0
    if wd<0: return f"(<0 = whales buying accumulating, long bias)"
    if wd>0: return f"(>0 = whales selling distributing, short bias)"
    return f"(=0 = neutral)"

def generate_open_interest_change_comparison_text(open_interest_change_value, open_interest_threshold_upper_value, open_interest_threshold_lower_value):
    try: oi=float(open_interest_change_value); up=float(open_interest_threshold_upper_value); lo=float(open_interest_threshold_lower_value)
    except: return ""
    if oi>up: return f"(OI {oi:+.2f}% > upper {up:.2f}% = OI up, new money entering)"
    if oi<lo: return f"(OI {oi:+.2f}% < lower {lo:.2f}% = OI down, positions closing)"
    return f"(OI {oi:+.2f}% vs upper {up:.2f}% / lower {lo:.2f}% = OI flat)"


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
                    new_data = {'timestamp':synth_ts,'price':float(last['price']),'basis':float(last['basis']),'oi':float(last['oi']),'taker_imbalance':1.0,'whale_div':float(last['whale_div']),'spot_delta':0.0}
                    print(f"[{synth_ts}] SYNTHETIC (API fail)", flush=True)
                else:
                    continue
        with df_lock:
            tmp = pd.DataFrame([new_data])
            global_df = tmp if global_df.empty else pd.concat([global_df, tmp], ignore_index=True)
            if len(global_df)>MAX_HISTORY_ROWS:
                global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
                need_compact=True
            else:
                need_compact=False
        append_row_atomic(new_data)
        if need_compact:
            compact_csv_if_needed()
        try:
            with df_lock:
                base = global_df.copy()
            dt = ist_from_epoch(next_epoch)
            minute = dt.minute
            tfs=[]
            tfs.append('1min')
            if minute%5==0: tfs.append('5min')
            if minute%15==0: tfs.append('15min')
            if minute%30==0: tfs.append('30min')
            with cvd_setting_lock:
                active_cvd_filter = current_cvd_filter_setting
            for tf in tfs:
                view = build_resampled_view(base, tf)
                if len(view)<100: continue
                sig_df, warm = compute_signals_for_view(view, tf, use_cvd_filter=active_cvd_filter)
                if warm or sig_df.empty: continue
                last = sig_df.iloc[-1]
                epoch_int = int(last['timestamp'].timestamp())
                ts_str = last['timestamp'].strftime('%Y-%m-%d %H:%M')
                mp = {'TRUE_BREAKOUT':last['is_breakout'],'SHORT_SQUEEZE':last['is_fakeout'],'TOP_EXHAUSTION':last['is_exhaustion'],'TRUE_BREAKDOWN':last['is_breakdown'],'LONG_LIQUIDATION':last['is_long_liq'],'BOTTOM_EXHAUSTION':last['is_bottom_exhaust']}
                for name, active in mp.items():
                    if active:
                        oi_change_val=float(last.get('oi_change',0)); oi_up=float(last.get('oi_thresh_upper',0.4)); oi_lo=float(last.get('oi_thresh_lower',-0.4))
                        whale_delta_val=float(last.get('whale_delta',0)); cvd_trend_val=float(last.get('cvd_trend',0)); cvd_noise_val=float(last.get('cvd_noise_thresh',0.5))
                        oi_text=generate_open_interest_change_comparison_text(oi_change_val, oi_up, oi_lo)
                        whale_text=generate_whale_delta_comparison_text(whale_delta_val)
                        cvd_mean_val=float(last.get('spot_cvd',0))
                        detailed_cvd=generate_detailed_cumulative_volume_delta_inference_description(name, cvd_trend_val, cvd_noise_val, float(last.get('spot_cvd',0)), cvd_mean_val, float(last.get('price_change',0)), oi_change_val)
                        base_reason=f"Daemon {tf} | Price {last['price_change']:+.2f}% > {last['price_thresh']:.2f}% mean(abs) | OI {oi_change_val:+.2f}% vs upper {oi_up:.2f}% / lower {oi_lo:.2f}% {oi_text} | CVD {cvd_trend_val:.2f} vs noise {cvd_noise_val:.2f} | Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN} Δ {whale_delta_val:.3f} {whale_text} | Clipped {PRESSURE_CLIP_MAX}x | Div lookback {DIVERGENCE_LOOKBACK_BARS}"
                        reason=f"{base_reason} | {detailed_cvd} | Filter {'ON' if active_cvd_filter else 'OFF'}"
                        log_signal_to_csv(epoch_int, ts_str, tf, name, last['price'], reason, last)
        except Exception as e:
            print(f"daemon eval err {e}", flush=True)

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
    return html.Div(style={'backgroundColor':bg,'padding':'15px','borderRadius':'8px','border':f'2px solid {bc}','flex':'1','minWidth':'280px','margin':'0','textAlign':'left','boxSizing':'border-box','transition':'all 0.3s ease'}, children=[html.Div(style={'textAlign':'center','marginBottom':'10px'}, children=[html.H2(title, style={'margin':'0 0 5px 0','color':tc,'fontSize':'18px','fontWeight':'bold'}), html.H3(stt, style={'margin':'0','color':tc,'fontSize':'14px','letterSpacing':'1px'})]), html.Div(style={'borderTop':'1px solid #333','paddingTop':'10px','marginTop':'10px'}, children=[html.P([html.Strong("Env: ", style={'color':'#aaa'}), env], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Data: ", style={'color':'#aaa'}), data], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Action: ", style={'color':'#aaa'}), strat], style={'margin':'0 0 5px 0','fontSize':'12px','color':'#888'}), html.P([html.Strong("Regime: ", style={'color':'#aaa'}), regime], style={'margin':'0 0 10px 0','fontSize':'12px','color':'#888'})]), html.P(thr, style={'margin':'0','fontSize':'11px','color':tc,'fontWeight':'bold','textAlign':'center'})])

app = dash.Dash(__name__)
app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>{%favicon%} {%css%}<style>.Select-control { background-color: #222 !important; border-color: #444 !important; color: white !important; } .Select-menu-outer { background-color: #222 !important; color: white !important; } .Select-value-label { color: white !important; }</style></head><body>{%app_entry%}<footer>{%config%} {%scripts%} {%renderer%}</footer></body></html>"""

app.title = "Order Flow Terminal v3 Bulletproof"

app.layout = html.Div(style={'backgroundColor':'#111','color':'white','fontFamily':'Arial, sans-serif','padding':'20px','minHeight':'100vh','boxSizing':'border-box'}, children=[
    dcc.Store(id='last-signal-time', data=None),
    dcc.Store(id='sound-trigger', data=None),
    dcc.Store(id='audio-enabled', data=False),
    html.Div(id='audio-dummy', style={'display':'none'}),
    html.Div(style={'display':'flex','justifyContent':'center','marginBottom':'15px'}, children=[
        html.Button("🔇 Click to Enable Audio", id='audio-enable-btn', n_clicks=0, style={'backgroundColor':'#111','color':'#ffaa00','border':'2px solid #ffaa00','borderRadius':'20px','padding':'8px 18px','fontWeight':'bold','cursor':'pointer','boxShadow':'0 0 10px rgba(255,170,0,0.3)'})
    ]),
    html.H1("QUANTITATIVE ORDER FLOW TERMINAL v3 - BULLETPROOF", style={'textAlign':'center','letterSpacing':'2px','marginBottom':'5px'}),
    html.Div(id='last-updated-label', style={'textAlign':'center','color':'#888','fontSize':'14px','marginBottom':'20px','fontStyle':'italic'}),
    html.Div(style={'display':'flex','flexWrap':'wrap','justifyContent':'center','alignItems':'center','marginBottom':'20px','gap':'20px','backgroundColor':'#1a1a1a','padding':'15px','borderRadius':'8px','border':'1px solid #333','boxSizing':'border-box'}, children=[
        html.Div([html.Label("Timeframe: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='timeframe-dropdown', options=[{'label':'1 Minute','value':'1min'},{'label':'5 Minutes','value':'5min'},{'label':'15 Minutes','value':'15min'},{'label':'30 Minutes','value':'30min'}], value='5min', clearable=False, style={'width':'120px','display':'inline-block','color':'black','textAlign':'left'})]),
        html.Div([html.Label("History Range: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='range-dropdown', options=[{'label':'Last 1 Hour','value':'1h'},{'label':'Last 6 Hours','value':'6h'},{'label':'Last 24 Hours','value':'24h'},{'label':'Last 1 Week','value':'7d'},{'label':'All Time','value':'all'}], value='24h', clearable=False, style={'width':'140px','display':'inline-block','color':'black','textAlign':'left'})]),
        html.Div([html.Label("EMA Window: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Input(id='ema-window', type='number', value=20, min=2, max=200, style={'width':'60px','borderRadius':'4px','border':'none','padding':'8px','backgroundColor':'#fff','color':'#000'})]),
        html.Div([html.Label("CVD Filter: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Checklist(id='cvd-toggle', options=[{'label':' ON (Adaptive Noise + Divergence)','value':'on'}], value=[], style={'display':'inline-block','color':'white'})]),
        html.Div([html.Label("Alert Sound: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Dropdown(id='sound-dropdown', options=[{'label':'Sonar Ping','value':'https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg'},{'label':'Beep','value':'https://actions.google.com/sounds/v1/alarms/beep_short.ogg'},{'label':'Digital Watch','value':'https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg'},{'label':'Mute','value':'none'}], value='https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg', clearable=False, style={'width':'150px','display':'inline-block','color':'black','textAlign':'left'})]),
        html.Div([html.Label("Duration (s): ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Input(id='sound-duration', type='number', value=6, min=1, max=60, style={'width':'60px','borderRadius':'4px','border':'none','padding':'8px','backgroundColor':'#fff','color':'#000'})])
    ]),
    html.Div(id='signal-row-up', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px','marginBottom':'15px','boxSizing':'border-box'}),
    html.Div(id='signal-row-down', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px','marginBottom':'15px','boxSizing':'border-box'}),
    html.Div(id='metrics-row', className='grid-cards', style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(200px, 1fr))','gap':'10px','marginTop':'10px','boxSizing':'border-box'}),
    html.Div(id='event-log-row'),
    html.Div(dcc.Graph(id='main-chart', config={'displayModeBar':False}), style={'marginTop':'20px','border':'1px solid #333','borderRadius':'8px'}),
    dcc.Interval(id='interval-component', interval=UI_REFRESH_INTERVAL, n_intervals=0)
])

app.clientside_callback(
    """
    function(n_clicks) {
        if(n_clicks && n_clicks>0) {
            try {
                window.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                if(window.audioCtx.state === 'suspended') { window.audioCtx.resume(); }
                var a = new Audio(); a.play().then(()=>{a.pause();}).catch(()=>{});
            } catch(e) { console.log("Audio init", e); }
            return [true, "🔊 Audio Active - Alerts Enabled"];
        }
        return [false, "🔇 Click to Enable Audio"];
    }
    """,
    [Output('audio-enabled','data'), Output('audio-enable-btn','children')],
    Input('audio-enable-btn','n_clicks')
)

app.clientside_callback(
    """
    function(trigger, sound_url, duration, audio_enabled) {
        if(!audio_enabled) { console.log("Audio not enabled yet"); return window.dash_clientside.no_update; }
        if(trigger && sound_url && sound_url !== 'none') {
            var audio = new Audio(sound_url);
            audio.loop = true;
            audio.play().catch(function(e) { console.log("Play blocked", e); });
            setTimeout(function(){ audio.pause(); audio.currentTime=0; }, (duration||6)*1000);
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output('audio-dummy','children'),
    Input('sound-trigger','data'),
    State('sound-dropdown','value'),
    State('sound-duration','value'),
    State('audio-enabled','data')
)

@app.callback(
    [Output('last-updated-label','children'), Output('signal-row-up','children'), Output('signal-row-down','children'), Output('metrics-row','children'), Output('event-log-row','children'), Output('main-chart','figure'), Output('last-signal-time','data'), Output('sound-trigger','data')],
    [Input('interval-component','n_intervals'), Input('timeframe-dropdown','value'), Input('range-dropdown','value'), Input('ema-window','value'), Input('cvd-toggle','value')],
    [State('last-signal-time','data')]
)
def update_dashboard(n, timeframe, time_range, ema_window, cvd_toggle, last_signal_time):
    global current_cvd_filter_setting
    use_cvd = 'on' in (cvd_toggle or [])
    with cvd_setting_lock:
        current_cvd_filter_setting = use_cvd
    with df_lock:
        if global_df.empty or len(global_df)<2:
            return "Initializing...", html.H3("GATHERING...", style={'textAlign':'center','color':'grey'}), "", "", "", go.Figure(), dash.no_update, dash.no_update
        base = global_df.copy()
    view = build_resampled_view(base, timeframe)
    if len(view)<2:
        return "Resampling...", html.H3("CALIBRATING...", style={'color':'grey'}), "", "", "", go.Figure(), dash.no_update, dash.no_update
    now_live = now_ist()
    tf_map={'1min':1,'5min':5,'15min':15,'30min':30}
    tf_min=tf_map.get(timeframe,5)
    tf_delta=pd.Timedelta(minutes=tf_min)
    last_ts=view['timestamp'].iloc[-1]
    is_forming=(last_ts+tf_delta)>now_live
    if is_forming and len(view)>=2:
        df_display=view.copy(); df_eval=view.iloc[:-1].copy(); forming=last_ts
    else:
        df_display=view.copy(); df_eval=view.copy(); forming=None
    df_eval, warm = compute_signals_for_view(df_eval, timeframe, use_cvd_filter=use_cvd)
    ema_window = ema_window if ema_window else 20
    live_str = now_live.strftime('%Y-%m-%d %H:%M:%S')
    if warm:
        txt=f"CALIBRATING {len(df_eval)}/100 | Session CVD UTC | Clipped {PRESSURE_CLIP_MAX}x | Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN} | Divergence {DIVERGENCE_LOOKBACK_BARS} | Priority Hierarchy | uirevision locked"
        last_upd=f"System: {live_str} IST | {txt} | CVD:{'ON' if use_cvd else 'OFF'}"
        chart= df_display.copy()
        chart['buy_pressure']=np.clip(np.where(chart['taker_imbalance']>0,1/chart['taker_imbalance'],1),0,PRESSURE_CLIP_MAX)
        chart['sell_pressure']=np.clip(chart['taker_imbalance'],0,PRESSURE_CLIP_MAX)
        chart['ema_buy']=chart['buy_pressure'].ewm(span=ema_window,adjust=False).mean()
        chart['ema_sell']=chart['sell_pressure'].ewm(span=ema_window,adjust=False).mean()
        chart['ema_whale']=chart['whale_div'].ewm(span=WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN,adjust=False).mean()
        chart['ema_basis']=chart['basis'].ewm(span=ema_window,adjust=False).mean()
        chart['ema_cvd']=chart['spot_cvd'].ewm(span=ema_window,adjust=False).mean()
        fig=make_subplots(rows=6,cols=1,shared_xaxes=True,vertical_spacing=0.04,row_heights=[0.30,0.12,0.12,0.14,0.14,0.18],subplot_titles=("Price & OI","Buy Press Clipped","Sell Press Clipped","Whale EMA10 Smoothed","Basis Premium","CVD Session UTC + Divergence"),specs=[[{"secondary_y":True}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}]])
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['price'],name="Price",line=dict(color='white')),row=1,col=1,secondary_y=False)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['oi'],name="OI",line=dict(color='cyan',dash='dot')),row=1,col=1,secondary_y=True)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['buy_pressure'],name="Buy Press",line=dict(color='magenta')),row=2,col=1)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['sell_pressure'],name="Sell Press",line=dict(color='#ff5555')),row=3,col=1)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['whale_div'],name="Whale Raw",line=dict(color='rgba(255,255,0,0.3)')),row=4,col=1)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_whale'],name=f"Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN}",line=dict(color='yellow',width=2)),row=4,col=1)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['spot_cvd'],name="CVD Session",line=dict(color='cyan'),fill='tozeroy'),row=6,col=1)
        fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',height=1150,uirevision='locked')
        card=html.Div(style={'backgroundColor':'#1a1a1a','padding':'20px','borderRadius':'8px','border':'1px solid #ffaa00','textAlign':'center'},children=[html.H2(f"⏳ {txt}",style={'color':'#ffaa00'}),html.P("Mean(abs), no ffill, session CVD UTC, adaptive noise, divergence mask, priority hierarchy, audio unlock required, uirevision locked, grid responsive",style={'color':'#888','fontSize':'12px'})])
        return last_upd, card, html.Div(), card, html.Div("Calibrating..."), fig, last_signal_time, dash.no_update
    cur = df_eval.iloc[-1]
    closed_str = cur['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
    form_info = f" | Forming {forming.strftime('%H:%M')}..." if forming is not None else ""
    last_upd = f"System: {live_str} IST | Last Closed: {closed_str}{form_info} | CVD:{'ON' if use_cvd else 'OFF'} Noise>{cur.get('cvd_noise_thresh',0.5):.2f} | Div {DIVERGENCE_LOOKBACK_BARS} | Priority ON | uirevision locked | Audio click to enable"
    active=False
    an="NONE"
    if cur['is_breakout']: active=True; an="TRUE_BREAKOUT"
    elif cur['is_breakdown']: active=True; an="TRUE_BREAKDOWN"
    elif cur['is_exhaustion']: active=True; an="TOP_EXHAUSTION"
    elif cur['is_bottom_exhaust']: active=True; an="BOTTOM_EXHAUSTION"
    elif cur['is_fakeout']: active=True; an="SHORT_SQUEEZE"
    elif cur['is_long_liq']: active=True; an="LONG_LIQUIDATION"
    epoch_id=int(cur['timestamp'].timestamp())
    sig_hash=f"{epoch_id}_{timeframe}_{an}"
    sound=dash.no_update
    if active and last_signal_time!=sig_hash:
        sound=sig_hash
        last_signal_time=sig_hash
    up = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px'}, children=[
        create_signal_card("🟩 TRUE BREAKOUT", cur['is_breakout'], "rgb(0, 255, 0)", "Continuation", f"Price>thresh mean(abs) + OI up + Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN}<0 + CVD>noise + Div OK + Priority", "Long", "High Vol", f"Price > +{cur['price_thresh']:.2f}% | OI > +{cur['oi_thresh_upper']:.2f}% | CVD>{cur['cvd_noise_thresh']:.2f}"),
        create_signal_card("🟪 LONG LIQ", cur['is_long_liq'], "rgb(170, 0, 255)", "Mean Rev", "Price down + OI down + CVD >= -noise + No Exhaustion", "Fade", "Low Vol", f"Price < -{cur['price_thresh']:.2f}% | OI < {cur['oi_thresh_lower']:.2f}%"),
        create_signal_card("🔵 BOTTOM EXHAUST", cur['is_bottom_exhaust'], "rgb(0, 200, 255)", "Reversal Top Priority", f"Sell clipped + Basis < low + Whale buying + CVD>noise + Price<=min({DIVERGENCE_LOOKBACK_BARS}) + CVD>mean", "Long", "Death Spiral", f"Sell>{cur['sp_thresh']:.2f}x | Prem<${cur['premium_thresh_lower']:.2f} | Div confirmed")
    ])
    down = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px'}, children=[
        create_signal_card("🟥 TRUE BREAKDOWN", cur['is_breakdown'], "rgb(255, 0, 0)", "Continuation", f"Price< -thresh + OI up + Whale>0 + CVD<-noise + No Exhaustion/Squeeze", "Short", "High Vol", f"Price < -{cur['price_thresh']:.2f}% | CVD < -{cur['cvd_noise_thresh']:.2f}"),
        create_signal_card("🟧 SHORT SQUEEZE", cur['is_fakeout'], "rgb(255, 165, 0)", "Mean Rev", "Price up + OI down + CVD<=noise + No Exhaustion", "Fade", "Low Vol", f"Price > +{cur['price_thresh']:.2f}%"),
        create_signal_card("🔴 TOP EXHAUST", cur['is_exhaustion'], "rgb(255, 50, 50)", "Reversal Top Priority", f"Buy clipped + Basis>high + Whale sell + CVD<-noise + Price>=max({DIVERGENCE_LOOKBACK_BARS}) + CVD<mean", "Short", "Parabolic", f"Buy>{cur['bp_thresh']:.2f}x | Prem>${cur['premium_thresh']:.2f} | Div confirmed")
    ])
    metrics = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(200px, 1fr))','gap':'10px'}, children=[
        create_metric_card("Price Chg Closed", f"{cur['price_change']:+.2f}%", f"mean(abs) + tf_scaler=1.0 (no double count)", f"Thresh ±{cur['price_thresh']:.2f}%", "#0f0" if cur['price_change']>0 else "#f00"),
        create_metric_card("OI Vel", f"{cur['oi_change']:+.2f}%", "Gap-dropped, clipped", f">{cur['oi_thresh_upper']:.2f}%", "#0ff"),
        create_metric_card("Buy Press Clip", f"{cur['buy_pressure']:.2f}x", f"Capped {PRESSURE_CLIP_MAX}x, Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN}, Slow {TAKER_BUY_PRESSURE_THRESHOLD_LOOKBACK_BARS} bars", f"> {cur['bp_thresh']:.2f}x | Noise {cur['cvd_noise_thresh']:.2f}", "cyan"),
        create_metric_card("Sell Press Clip", f"{cur['sell_pressure']:.2f}x", f"Capped {PRESSURE_CLIP_MAX}x, Slow {TAKER_SELL_PRESSURE_THRESHOLD_LOOKBACK_BARS} bars", f"> {cur['sp_thresh']:.2f}x", "#ff5555"),
        create_metric_card("Basis", f"${cur['basis']:.2f}", "Session CVD UTC, divergence mask", f">{cur['premium_thresh']:.2f} <{cur['premium_thresh_lower']:.2f}", "orange")
    ])
    logs=[]
    try:
        if os.path.exists(SIGNAL_LOG_FILE):
            ldf=pd.read_csv(SIGNAL_LOG_FILE)
            if not ldf.empty:
                filt = ldf[ldf['timeframe']==timeframe].tail(20) if 'timeframe' in ldf.columns else ldf.tail(20)
                if filt.empty: filt=ldf.tail(20)
                for _, r in filt.iloc[::-1].iterrows():
                    ts=r.get('timestamp',''); sig=r.get('signal',''); pr=r.get('price',0); rs=r.get('reason','')[:110]
                    col='lime' if 'BREAKOUT' in sig else 'red' if 'BREAKDOWN' in sig else 'orange'
                    logs.append(html.Div([html.Span(f"[{ts}] {sig} @ ${pr:,.2f}", style={'fontWeight':'bold'}), html.Br(), html.Span(f"↳ {rs}", style={'fontSize':'11px','color':'#888','marginLeft':'10px'})], style={'color':col,'marginBottom':'8px'}))
            else:
                logs=[html.Div("Waiting for daemon closed candle", style={'color':'#555'})]
        else:
            logs=[html.Div("Log not yet created", style={'color':'#555'})]
    except Exception as e:
        logs=[html.Div(f"Log err {e}", style={'color':'#f55'})]
    event_log = html.Div(style={'backgroundColor':'#1a1a1a','border':'1px solid #333','borderRadius':'8px','padding':'15px','height':'180px','overflowY':'auto','marginTop':'20px','fontFamily':'monospace'}, children=[html.H3("EVENT LOG - DAEMON EPOCH HASHED - PRIORITY HIERARCHY", style={'margin':'0 0 10px 0','fontSize':'14px','color':'#aaa'}), html.Div(logs)])
    chart = df_display.copy()
    try:
        eval_map = df_eval.set_index('timestamp')
        for col in ['premium_thresh','premium_thresh_lower','price_thresh','oi_thresh_upper','oi_thresh_lower','bp_thresh','sp_thresh','cvd_noise_thresh','buy_pressure','sell_pressure']:
            if col in eval_map.columns:
                chart[col] = chart['timestamp'].map(eval_map[col])
    except Exception:
        pass
    computed_bp=np.clip(np.where(chart['taker_imbalance']>0,1/chart['taker_imbalance'],1),0,PRESSURE_CLIP_MAX)
    computed_sp=np.clip(chart['taker_imbalance'],0,PRESSURE_CLIP_MAX)
    if 'buy_pressure' not in chart.columns:
        chart['buy_pressure']=computed_bp
    else:
        chart['buy_pressure']=chart['buy_pressure'].fillna(pd.Series(computed_bp, index=chart.index))
    if 'sell_pressure' not in chart.columns:
        chart['sell_pressure']=computed_sp
    else:
        chart['sell_pressure']=chart['sell_pressure'].fillna(pd.Series(computed_sp, index=chart.index))
    for c in ['premium_thresh','premium_thresh_lower']:
        if c not in chart.columns:
            chart[c]=np.nan
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
    fig=make_subplots(rows=6,cols=1,shared_xaxes=True,vertical_spacing=0.04,row_heights=[0.30,0.12,0.12,0.14,0.14,0.18],subplot_titles=("Price & OI (Forming)","Buy Press Clipped","Sell Press Clipped","Whale EMA10 Smoothed","Basis Premium","CVD Session UTC + Divergence"),specs=[[{"secondary_y":True}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}]])
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['price'],name="Price",line=dict(color='white',width=2)),row=1,col=1,secondary_y=False)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['oi'],name="OI",line=dict(color='cyan',dash='dot')),row=1,col=1,secondary_y=True)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['buy_pressure'],name="Buy Press",line=dict(color='magenta')),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_buy'],name=f"EMA {ema_window}",line=dict(color='rgba(255,255,255,0.6)',dash='dot')),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['sell_pressure'],name="Sell Press",line=dict(color='#ff5555')),row=3,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_sell'],name=f"EMA {ema_window}",line=dict(color='rgba(255,200,200,0.6)',dash='dot')),row=3,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['whale_div'],name="Whale Raw",line=dict(color='rgba(255,255,0,0.3)')),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_whale'],name=f"Whale EMA{WHALE_RATIO_EXPONENTIAL_MOVING_AVERAGE_SPAN}",line=dict(color='yellow',width=2)),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['premium_thresh'],name="90%",line=dict(color='rgba(255,165,0,0.3)',dash='dash')),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['basis'],name="Basis",line=dict(color='orange'),fill='tonexty'),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['premium_thresh_lower'],name="10%",line=dict(color='rgba(0,200,255,0.3)',dash='dash')),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['spot_cvd'],name="CVD Session UTC",line=dict(color='cyan'),fill='tozeroy'),row=6,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_cvd'],name="EMA",line=dict(color='white',dash='dot')),row=6,col=1)
    fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',margin=dict(l=40,r=40,t=40,b=20),hovermode="x unified",legend=dict(orientation="h",y=1.05,x=1),height=1150,uirevision='locked')
    fig.update_xaxes(showgrid=True,gridcolor='#222')
    return last_upd, up, down, metrics, event_log, fig, last_signal_time, sound

if __name__ == '__main__':
    start_background_thread()
    # from dash_auth import BasicAuth  # pip install dash-auth - framework for password protection
    # BasicAuth(app, {'admin': 'your_secure_password_here'})
    import os
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8050'))
    print(f"Starting on http://{host}:{port}", flush=True)
    app.run(host=host, port=port, debug=False)

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

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# 1. CONSTANTS - PRODUCTION GRADE
# ==========================================
MAX_HISTORY_ROWS = 10080  # 7 days 1m = 672x15m, 336x30m - Bug 6
UI_REFRESH_INTERVAL = 10000
USE_SPOT_CVD_FILTER_DEFAULT = True
PRESSURE_CLIP_MAX = 10.0
PRESSURE_CLIP_MIN = 0.1
WHALE_EMA_SPAN = 3  # Bug 14
CVD_NOISE_MIN = 0.5
CVD_NOISE_FACTOR = 0.2  # Bug 13 adaptive
VOLATILITY_SCALER_ENABLED = False  # Bug 16 - see note below, keep False to avoid double-counting
DIVERGENCE_LOOKBACK = 10  # Bug 17

# --- IST wall-clock helpers -----------------------------------------------
# India does not observe DST, so a fixed UTC+5:30 offset is always exactly
# correct. Previously several places called datetime.datetime.now()/
# fromtimestamp() directly and assumed the HOST machine's OS clock was
# already set to IST. On a server whose OS timezone is UTC (common on
# cloud hosts/containers) that assumption silently shifted every stored
# timestamp, the "Session CVD (resets daily)" anchor, and the "IST" label
# in the status bar by up to 5.5 hours. These helpers compute true IST
# regardless of the host's configured timezone, and are a drop-in
# replacement for the old calls (same naive-datetime return type).
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
    # Not using read_csv(parse_dates=[...]) here: it silently gives up and leaves
    # the whole column as plain text (no error, no visible warning) the moment it
    # hits a row in a different format than the rest - which is exactly what the
    # once-a-day midnight formatting quirk (fixed in append_row_atomic above)
    # already wrote into this file. Parsing explicitly with format='ISO8601'
    # correctly handles a mix of "YYYY-MM-DD HH:MM:SS" and "YYYY-MM-DD" rows (any
    # already on disk from before the write-side fix), so existing history loads
    # correctly without needing to hand-edit the file.
    global_df = pd.read_csv(DATA_FILE)
    global_df['timestamp'] = pd.to_datetime(global_df['timestamp'], format='ISO8601')
    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows")
else:
    global_df = pd.DataFrame(columns=['timestamp','price','basis','oi','taker_imbalance','whale_div','spot_delta'])

df_lock = threading.Lock()
file_lock = threading.RLock()  # RLock (not Lock): ensure_signal_log_schema() below needs to
# be safely callable both on its own AND from inside log_signal_to_csv, which already
# holds this same lock - a plain Lock would deadlock on the second acquire.
csv_lock = file_lock
cvd_setting_lock = threading.Lock()
# Mirrors whatever CVD-filter state the live dashboard last had selected, so the
# background daemon's persisted event log (signal_events.csv) stays consistent
# with what the UI is showing instead of always evaluating with the filter
# hardcoded on regardless of the user's toggle.
current_cvd_filter_setting = USE_SPOT_CVD_FILTER_DEFAULT

SIGNAL_LOG_COLUMNS = ['epoch','timestamp','timeframe','signal','price','reason','price_change','price_thresh',
                      'oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure',
                      'sp_thresh','basis','premium_thresh','premium_thresh_lower']

def ensure_signal_log_schema():
    """If signal_events.csv exists but its header doesn't match SIGNAL_LOG_COLUMNS
    (e.g. left over from an older version of log_signal_to_csv with a different set
    of fields), archive it and let a fresh file start clean. Called once at startup
    (before anything tries to read the file) and again from log_signal_to_csv before
    every write, so a mismatch can never linger and get silently re-read/re-warned
    about on every dashboard refresh in the meantime."""
    with file_lock:
        if not os.path.exists(SIGNAL_LOG_FILE):
            return
        try:
            with open(SIGNAL_LOG_FILE, 'r') as fchk:
                existing_header = fchk.readline().strip().split(',')
            if existing_header == SIGNAL_LOG_COLUMNS:
                return
            reason = f"header mismatch ({len(existing_header)} vs {len(SIGNAL_LOG_COLUMNS)} columns)"
        except Exception as e:
            reason = f"unreadable ({e})"
        archive_name = SIGNAL_LOG_FILE.replace('.csv', f'_legacy_{int(time.time())}.csv')
        try:
            os.replace(SIGNAL_LOG_FILE, archive_name)
            print(f"signal_events.csv {reason} - archived old file to {archive_name} and starting fresh", flush=True)
        except Exception:
            pass

logged_signals = set()
ensure_signal_log_schema()
if os.path.exists(SIGNAL_LOG_FILE):
    try:
        sdf = pd.read_csv(SIGNAL_LOG_FILE, on_bad_lines='skip')
        for _, r in sdf.iterrows():
            if 'epoch' in sdf.columns and pd.notna(r.get('epoch', None)):
                logged_signals.add(f"{int(r['epoch'])}_{r['timeframe']}_{r['signal']}")
            else:
                # Legacy rows without a usable epoch column: derive the same
                # epoch-based key the daemon writes (log_signal_to_csv) so dedup
                # stays consistent across restarts instead of permanently using a
                # differently-formatted key for these rows.
                try:
                    epoch_from_ts = int(pd.to_datetime(r['timestamp']).timestamp())
                    logged_signals.add(f"{epoch_from_ts}_{r['timeframe']}_{r['signal']}")
                except Exception:
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
            "spot_kline": "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=2",
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
            # FIX 18:00 flatline - use closed kline, not forming (limit=2, take closed)
            spot_delta = 0.0
            closed_ts = None
            if data.get("spot_kline") and len(data["spot_kline"]) >= 1:
                klines = data["spot_kline"]
                try:
                    # When limit=2 at HH:MM:00.1, Binance returns [closed-1, forming]
                    # Closed is the one whose close_time < now, or simply klines[-2]
                    if len(klines) >= 2:
                        last_close_ms = int(klines[-1][6])
                        now_ms = int(ts.timestamp() * 1000)
                        k = klines[-2] if last_close_ms > now_ms else klines[-1]
                    else:
                        k = klines[0]
                    tot = float(k[5]); tb = float(k[9])
                    if tot == 0 and len(klines) >= 2:
                        # fallback if we picked a zero-volume forming kline
                        k_alt = klines[0] if k is klines[-1] else klines[-1]
                        if float(k_alt[5]) != 0:
                            k = k_alt; tot = float(k[5]); tb = float(k[9])
                    spot_delta = tb - (tot - tb)
                    closed_ts = int(k[0])
                except Exception as e:
                    print(f"spot_delta parse err {e}", flush=True)
                    spot_delta = 0.0
            # Keep timestamp aligned to closed kline to avoid future ts with old delta
            if closed_ts is not None:
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromtimestamp(closed_ts/1000.0)
                except:
                    pass
            return {'timestamp':ts,'price':spot_price,'basis':basis,'oi':oi,'taker_imbalance':taker_imb,'whale_div':whale,'spot_delta':spot_delta}
        except Exception as e:
            print(f"parse err {e}", flush=True)
            return None

def append_row_atomic(row):
    with file_lock:
        exists = os.path.exists(DATA_FILE)
        # date_format is required here: pandas silently drops the time-of-day and
        # writes date-only (e.g. "2026-07-13" instead of "2026-07-13 00:00:00")
        # when a datetime value being written happens to land exactly at midnight
        # AND it's the only row in that particular to_csv() call - which is exactly
        # what happens once a day, every day, since each daemon tick appends a
        # single row. That malformed row then breaks pd.to_datetime() the next
        # time this file is read, since it no longer matches the other rows'
        # format. Forcing the format explicitly makes every row consistent.
        pd.DataFrame([row])[['timestamp','price','basis','oi','taker_imbalance','whale_div','spot_delta']].to_csv(DATA_FILE, mode='a', header=not exists, index=False, date_format='%Y-%m-%d %H:%M:%S')

def compact_csv_if_needed():
    with file_lock:
        try:
            with df_lock:
                if global_df.empty: return
                trimmed = global_df.iloc[-MAX_HISTORY_ROWS:].copy()
            with tempfile.NamedTemporaryFile(mode='w', delete=False, dir='data', suffix='.tmp') as tf:
                tmp = tf.name
                trimmed.to_csv(tmp, index=False, date_format='%Y-%m-%d %H:%M:%S')
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            print(f"compact fail {e}", flush=True)

def log_signal_to_csv(epoch_int, ts_str, timeframe, sig_name, price, reason, row):
    sig_id = f"{epoch_int}_{timeframe}_{sig_name}"
    with file_lock:
        if sig_id not in logged_signals:
            logged_signals.add(sig_id)
            d = {
                'epoch': epoch_int, 'timestamp': ts_str, 'timeframe': timeframe, 'signal': sig_name, 'price': price, 'reason': reason,
                'price_change': float(row.get('price_change',0)) if isinstance(row, dict) or hasattr(row,'get') else float(row['price_change']) if 'price_change' in row else 0,
                'price_thresh': float(row.get('price_thresh',0)) if hasattr(row,'get') else 0,
                'oi_change': 0, 'oi_thresh_upper':0,'oi_thresh_lower':0,'buy_pressure':0,'bp_thresh':0,'sell_pressure':0,'sp_thresh':0,'basis':0,'premium_thresh':0,'premium_thresh_lower':0
            }
            try:
                # pull rest if Series
                if not isinstance(row, dict):
                    for k in ['oi_change','oi_thresh_upper','oi_thresh_lower','buy_pressure','bp_thresh','sell_pressure','sp_thresh','basis','premium_thresh','premium_thresh_lower','price_change','price_thresh']:
                        if k in row:
                            d[k]=float(row[k])
            except: pass
            ensure_signal_log_schema()  # safe to call while holding file_lock - it's an RLock
            hdr = not os.path.exists(SIGNAL_LOG_FILE)
            pd.DataFrame([d]).to_csv(SIGNAL_LOG_FILE, mode='a', header=hdr, index=False)

def build_resampled_view(base_df, timeframe):
    if base_df.empty:
        return pd.DataFrame()
    df = base_df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')
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
        # Timestamps are genuine IST (via now_ist()/ist_from_epoch()), independent of
        # the host OS timezone, so this tz_localize step is always correct. The except
        # fallback below is kept as a defensive no-op safety net.
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

def compute_signals_for_view(df_input, timeframe, use_cvd_filter=True):
    if len(df_input) < 2:
        return df_input, True
    df = df_input.copy()
    # Whale EMA smoothing - Bug 14
    df['whale_div_smooth'] = df['whale_div'].ewm(span=WHALE_EMA_SPAN, adjust=False).mean()
    df['whale_delta'] = df['whale_div_smooth'].diff()
    # Pressure clipping - Bug 15
    df['taker_imbalance'] = np.clip(df['taker_imbalance'], PRESSURE_CLIP_MIN, PRESSURE_CLIP_MAX)
    df['buy_pressure'] = np.clip(np.where(df['taker_imbalance']>0, 1/df['taker_imbalance'], 1), 0, PRESSURE_CLIP_MAX)
    df['sell_pressure'] = np.clip(df['taker_imbalance'], 0, PRESSURE_CLIP_MAX)
    df['price_change'] = df['price'].pct_change()*100
    df['oi_change'] = df['oi'].pct_change()*100
    tf_map = {'1min':1,'5min':5,'15min':15,'30min':30}
    tf_min = tf_map.get(timeframe,5)
    gap_thr = pd.Timedelta(minutes=tf_min*1.5)
    df.loc[df['time_diff'] > gap_thr, ['price_change','oi_change']] = np.nan
    # Bug 4: mean(abs) not abs(mean)
    abs_price = df['price_change'].abs()
    abs_oi = df['oi_change'].abs()
    df['price_thresh'] = abs_price.rolling(20).mean() + (abs_price.rolling(20).std().fillna(0)*1.5)
    df['oi_thresh_upper'] = abs_oi.rolling(20).mean() + (abs_oi.rolling(20).std().fillna(0)*1.5)
    df['oi_thresh_lower'] = -df['oi_thresh_upper']
    df['premium_thresh'] = df['basis'].rolling(100).quantile(0.90)
    df['premium_thresh_lower'] = df['basis'].rolling(100).quantile(0.10)
    df['bp_thresh'] = df['buy_pressure'].rolling(100).mean() + (df['buy_pressure'].rolling(100).std().fillna(0)*1.5)
    df['sp_thresh'] = df['sell_pressure'].rolling(100).mean() + (df['sell_pressure'].rolling(100).std().fillna(0)*1.5)
    # Bug 16: Volatility scaling - std already timeframe-native, so no extra sqrt. Keep scaler=1 to avoid double-count.
    # If you want equal absolute move across TFs, set scaler = math.sqrt(5/tf_min). If you want equal rarity on top of native std, keep 1.0.
    tf_scaler = 1.0
    if VOLATILITY_SCALER_ENABLED:
        tf_scaler = math.sqrt(5.0 / max(tf_min,1))
        df['price_thresh'] = df['price_thresh'] * tf_scaler
        df['oi_thresh_upper'] = df['oi_thresh_upper'] * tf_scaler
        df['oi_thresh_lower'] = -df['oi_thresh_upper']
    if len(df) < 100:
        return df, True
    for c in ['price_thresh','oi_thresh_upper','oi_thresh_lower','premium_thresh','premium_thresh_lower','bp_thresh','sp_thresh']:
        df[c] = df[c].fillna({'price_thresh':0.15,'oi_thresh_upper':0.4,'oi_thresh_lower':-0.4,'premium_thresh':8.0,'premium_thresh_lower':-8.0,'bp_thresh':1.3,'sp_thresh':1.3}.get(c,0))
    # Raw signals
    raw_breakout = (df['price_change'] > df['price_thresh']) & (df['oi_change'] > df['oi_thresh_upper']) & (df['whale_delta'] < 0)
    raw_fakeout = (df['price_change'] > df['price_thresh']) & (df['oi_change'] < df['oi_thresh_lower'])
    raw_exhaust = (df['price_change'] > 0) & (df['buy_pressure'] > df['bp_thresh']) & (df['basis'] > df['premium_thresh']) & (df['whale_delta'] > 0)
    raw_breakdown = (df['price_change'] < -df['price_thresh']) & (df['oi_change'] > df['oi_thresh_upper']) & (df['whale_delta'] > 0)
    raw_liq = (df['price_change'] < -df['price_thresh']) & (df['oi_change'] < df['oi_thresh_lower'])
    raw_bottom = (df['price_change'] < 0) & (df['sell_pressure'] > df['sp_thresh']) & (df['basis'] < df['premium_thresh_lower']) & (df['whale_delta'] < 0)
    # Bug 17: True divergence - price local high but CVD weak
    price_roll_max = df['price'].rolling(DIVERGENCE_LOOKBACK, min_periods=5).max()
    price_roll_min = df['price'].rolling(DIVERGENCE_LOOKBACK, min_periods=5).min()
    # Session-aware CVD mean to avoid cross-day contamination
    try:
        cvd_roll_mean = df.groupby('session_date')['spot_cvd'].transform(lambda x: x.rolling(DIVERGENCE_LOOKBACK, min_periods=5).mean())
    except:
        cvd_roll_mean = df['spot_cvd'].rolling(DIVERGENCE_LOOKBACK, min_periods=5).mean()
    # Exhaustion requires price at local extreme AND CVD failing to confirm
    raw_exhaust = raw_exhaust & (df['price'] >= price_roll_max) & (df['spot_cvd'] < cvd_roll_mean)
    raw_bottom = raw_bottom & (df['price'] <= price_roll_min) & (df['spot_cvd'] > cvd_roll_mean)
    # Bug 13 adaptive noise
    spot_std = df['spot_delta'].rolling(20).std().fillna(0)
    df['cvd_noise_thresh'] = np.maximum(CVD_NOISE_MIN, CVD_NOISE_FACTOR * spot_std)
    if use_cvd_filter:
        df['is_breakout'] = raw_breakout & (df['cvd_trend'] > df['cvd_noise_thresh'])
        df['is_fakeout'] = raw_fakeout & (df['cvd_trend'] <= df['cvd_noise_thresh'])
        df['is_exhaustion'] = raw_exhaust & (df['cvd_trend'] < -df['cvd_noise_thresh'])
        df['is_breakdown'] = raw_breakdown & (df['cvd_trend'] < -df['cvd_noise_thresh'])
        df['is_long_liq'] = raw_liq & (df['cvd_trend'] >= -df['cvd_noise_thresh'])
        df['is_bottom_exhaust'] = raw_bottom & (df['cvd_trend'] > df['cvd_noise_thresh'])
    else:
        df['is_breakout'], df['is_fakeout'], df['is_exhaustion'], df['is_breakdown'], df['is_long_liq'], df['is_bottom_exhaust'] = raw_breakout, raw_fakeout, raw_exhaust, raw_breakdown, raw_liq, raw_bottom
    # Bug 18: Priority hierarchy - exhaustions override everything
    df['is_exhaustion'] = df['is_exhaustion']
    df['is_bottom_exhaust'] = df['is_bottom_exhaust']
    # Mean reversions suppressed by exhaustions
    df['is_fakeout'] = df['is_fakeout'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust']
    df['is_long_liq'] = df['is_long_liq'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust']
    # Trend continuations suppressed by both above
    df['is_breakout'] = df['is_breakout'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust'] & ~df['is_fakeout'] & ~df['is_long_liq']
    df['is_breakdown'] = df['is_breakdown'] & ~df['is_exhaustion'] & ~df['is_bottom_exhaust'] & ~df['is_fakeout'] & ~df['is_long_liq']
    return df, False

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
                        reason = f"Daemon {tf} | Price {last['price_change']:+.2f}% > {last['price_thresh']:.2f}% mean(abs) | OI {last['oi_change']:+.2f}% | CVD {last['cvd_trend']:.2f} vs noise {last['cvd_noise_thresh']:.2f} | Whale EMA{WHALE_EMA_SPAN} Δ {last['whale_delta']:.3f} | Clipped {PRESSURE_CLIP_MAX}x | Div lookback {DIVERGENCE_LOOKBACK}"
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
        html.Div([html.Label("CVD Filter: ", style={'fontWeight':'bold','marginRight':'8px'}), dcc.Checklist(id='cvd-toggle', options=[{'label':' ON (Adaptive Noise + Divergence)','value':'on'}], value=['on'], style={'display':'inline-block','color':'white'})]),
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
        txt=f"CALIBRATING {len(df_eval)}/100 | Session CVD UTC | Clipped {PRESSURE_CLIP_MAX}x | Whale EMA{WHALE_EMA_SPAN} | Divergence {DIVERGENCE_LOOKBACK} | Priority Hierarchy | uirevision locked"
        last_upd=f"System: {live_str} IST | {txt} | CVD:{'ON' if use_cvd else 'OFF'}"
        chart= df_display.copy()
        chart['buy_pressure']=np.clip(np.where(chart['taker_imbalance']>0,1/chart['taker_imbalance'],1),0,PRESSURE_CLIP_MAX)
        chart['ema_buy']=chart['buy_pressure'].ewm(span=ema_window,adjust=False).mean()
        chart['ema_whale']=chart['whale_div'].ewm(span=WHALE_EMA_SPAN,adjust=False).mean()
        chart['ema_basis']=chart['basis'].ewm(span=ema_window,adjust=False).mean()
        chart['ema_cvd']=chart['spot_cvd'].ewm(span=ema_window,adjust=False).mean()
        fig=make_subplots(rows=5,cols=1,shared_xaxes=True,vertical_spacing=0.04,row_heights=[0.35,0.15,0.15,0.15,0.20],subplot_titles=("Price & OI","Buy Press Clipped","Whale EMA3","Basis","CVD Session"),specs=[[{"secondary_y":True}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}]])
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['price'],name="Price",line=dict(color='white')),row=1,col=1,secondary_y=False)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['oi'],name="OI",line=dict(color='cyan',dash='dot')),row=1,col=1,secondary_y=True)
        fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['spot_cvd'],name="CVD Session",line=dict(color='cyan'),fill='tozeroy'),row=5,col=1)
        fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',height=1050,uirevision='locked')
        card=html.Div(style={'backgroundColor':'#1a1a1a','padding':'20px','borderRadius':'8px','border':'1px solid #ffaa00','textAlign':'center'},children=[html.H2(f"⏳ {txt}",style={'color':'#ffaa00'}),html.P("Mean(abs), no ffill, session CVD UTC, adaptive noise, divergence mask, priority hierarchy, audio unlock required, uirevision locked, grid responsive",style={'color':'#888','fontSize':'12px'})])
        return last_upd, card, html.Div(), card, html.Div("Calibrating..."), fig, last_signal_time, dash.no_update
    cur = df_eval.iloc[-1]
    closed_str = cur['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
    form_info = f" | Forming {forming.strftime('%H:%M')}..." if forming is not None else ""
    last_upd = f"System: {live_str} IST | Last Closed: {closed_str}{form_info} | CVD:{'ON' if use_cvd else 'OFF'} Noise>{cur.get('cvd_noise_thresh',0.5):.2f} | Div {DIVERGENCE_LOOKBACK} | Priority ON | uirevision locked | Audio click to enable"
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
        create_signal_card("🟩 TRUE BREAKOUT", cur['is_breakout'], "rgb(0, 255, 0)", "Trend Continuation", "Price UP + OI UP + Whales Buying.", "Ride the Trend. Enter Long on close.", "High Volatility (NY/London)", f"Target: Price > +{cur['price_thresh']:.2f}% | OI > +{cur['oi_thresh_upper']:.2f}% | CVD Confirmed (Buying)"),
        create_signal_card("🟪 LONG LIQUIDATION", cur['is_long_liq'], "rgb(170, 0, 255)", "Mean Reversion (Dip)", "Price DOWN + OI DOWN rapidly.", "Fade the Flush. Enter Long post-wick.", "Low Volatility (Weekends/Asia)", f"Target: Price < -{cur['price_thresh']:.2f}% | OI < {cur['oi_thresh_lower']:.2f}% | CVD Divergent (Absorption)"),
        create_signal_card("🔵 BOTTOM EXHAUSTION", cur['is_bottom_exhaust'], "rgb(0, 200, 255)", "Macro Reversal", "Price DOWN + Extreme Sell Pressure + Whales Buying.", "Catch the Knife. Enter Long.", "Parabolic Bear / Death Spiral", f"Target: Sell Pressure > {cur['sp_thresh']:.2f}x | Premium < ${cur['premium_thresh_lower']:.2f} | CVD Confirmed (Buying)")
    ])
    down = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(280px, 1fr))','gap':'15px'}, children=[
        create_signal_card("🟥 TRUE BREAKDOWN", cur['is_breakdown'], "rgb(255, 0, 0)", "Trend Continuation", "Price DOWN + OI UP + Whales Selling.", "Ride the Trend. Enter Short on close.", "High Volatility (NY/London)", f"Target: Price < -{cur['price_thresh']:.2f}% | OI > +{cur['oi_thresh_upper']:.2f}% | CVD Confirmed (Selling)"),
        create_signal_card("🟧 SHORT SQUEEZE", cur['is_fakeout'], "rgb(255, 165, 0)", "Mean Reversion (Top)", "Price UP + OI DOWN rapidly.", "Fade the Fakeout. Enter Short post-spike.", "Low Volatility (Weekends/Asia)", f"Target: Price > +{cur['price_thresh']:.2f}% | OI < {cur['oi_thresh_lower']:.2f}% | CVD Divergent (Absorption)"),
        create_signal_card("🔴 TOP EXHAUSTION", cur['is_exhaustion'], "rgb(255, 50, 50)", "Macro Reversal", "Price UP + Extreme Buy Pressure + Whales Selling.", "Top Tick. Enter Short.", "Parabolic Bull Run", f"Target: Buy Pressure > {cur['bp_thresh']:.2f}x | Premium > ${cur['premium_thresh']:.2f} | CVD Divergent (Selling)")
    ])
    metrics = html.Div(style={'display':'grid','gridTemplateColumns':'repeat(auto-fit, minmax(200px, 1fr))','gap':'10px'}, children=[
        create_metric_card("Price Change", f"{cur['price_change']:+.2f}%", "Momentum of current move.", f"Dynamic Vol Threshold: ±{cur['price_thresh']:.2f}%", "#00FF00" if cur['price_change'] > 0 else "#FF0000"),
        create_metric_card("OI Velocity", f"{cur['oi_change']:+.2f}%", "New money entering vs closing.", f"Dynamic Target: > +{cur['oi_thresh_upper']:.2f}% or < {cur['oi_thresh_lower']:.2f}%", "#00FF00" if cur['oi_change'] > 0 else ("#FF0000" if cur['oi_change'] < 0 else "white")),
        create_metric_card("Taker Buy Pressure", f"{cur['buy_pressure']:.2f}x", "Market Buy vs Sell Volume.", f"Dynamic Noise Filter: > {cur['bp_thresh']:.2f}x", "cyan"),
        create_metric_card("Basis Premium", f"${cur['basis']:.2f}", "Futures Price minus Spot Price.", f"Regime Limits: > ${cur['premium_thresh']:.2f} | < ${cur['premium_thresh_lower']:.2f}", "orange" if cur['basis'] > cur['premium_thresh'] else ("cyan" if cur['basis'] < cur['premium_thresh_lower'] else "white"))
    ])
    logs=[]
    try:
        if os.path.exists(SIGNAL_LOG_FILE):
            ldf=pd.read_csv(SIGNAL_LOG_FILE, on_bad_lines='skip')
            if not ldf.empty:
                filt = ldf[ldf['timeframe']==timeframe].tail(20) if 'timeframe' in ldf.columns else ldf.tail(20)
                if filt.empty: filt=ldf.tail(20)
                for _, r in filt.iloc[::-1].iterrows():
                    ts=r.get('timestamp',''); sig=r.get('signal',''); pr=r.get('price',0)
                    pr = 0 if (isinstance(pr, float) and pd.isna(pr)) else pr
                    sig = '' if (isinstance(sig, float) and pd.isna(sig)) else str(sig)
                    rs=r.get('reason','')
                    rs = '' if (isinstance(rs, float) and pd.isna(rs)) else str(rs)
                    rs = rs[:110]
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
    if 'buy_pressure' not in chart.columns:
        chart['buy_pressure']=computed_bp
    else:
        chart['buy_pressure']=chart['buy_pressure'].fillna(pd.Series(computed_bp, index=chart.index))
    for c in ['premium_thresh','premium_thresh_lower']:
        if c not in chart.columns:
            chart[c]=np.nan
    chart['ema_buy']=chart['buy_pressure'].ewm(span=ema_window,adjust=False).mean()
    chart['ema_whale']=chart['whale_div'].ewm(span=WHALE_EMA_SPAN,adjust=False).mean()
    chart['ema_basis']=chart['basis'].ewm(span=ema_window,adjust=False).mean()
    chart['ema_cvd']=chart['spot_cvd'].ewm(span=ema_window,adjust=False).mean()
    if time_range!='all':
        mt=chart['timestamp'].max()
        if time_range=='1h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=1)]
        elif time_range=='6h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=6)]
        elif time_range=='24h': chart=chart[chart['timestamp']>=mt-pd.Timedelta(hours=24)]
        elif time_range=='7d': chart=chart[chart['timestamp']>=mt-pd.Timedelta(days=7)]
    fig=make_subplots(rows=5,cols=1,shared_xaxes=True,vertical_spacing=0.06,row_heights=[0.35,0.15,0.15,0.15,0.20],subplot_titles=("Price & Open Interest (Live Forming Included)", "Taker Buy Pressure", "Whale Ratio", "Basis Premium (Session)", "Spot CVD Session-Anchored (Resets Daily)"),specs=[[{"secondary_y":True}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}],[{"secondary_y":False}]])
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['price'],name="BTC Price",line=dict(color='white',width=2),showlegend=True),row=1,col=1,secondary_y=False)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['oi'],name="Open Interest",line=dict(color='#00e5ff',width=2,dash='dot'),showlegend=True),row=1,col=1,secondary_y=True)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['buy_pressure'],name="Buy Pressure",line=dict(color='#ff00ff',width=1.2),showlegend=True),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_buy'],name=f"EMA {ema_window}",line=dict(color='rgba(255,255,255,0.85)',width=1.5,dash='dot'),showlegend=True),row=2,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['whale_div'],name="Whale Raw",line=dict(color='rgba(255,235,0,0.7)',width=1),showlegend=True),row=3,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_whale'],name=f"Whale EMA{WHALE_EMA_SPAN}",line=dict(color='#ffeb3b',width=2),showlegend=True),row=3,col=1)
    # Fixed visibility: solid orange/cyan for thresholds, not 0.3 opacity (was invisible in legend)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['premium_thresh'],name="90th % Limit",line=dict(color='#ff9800',width=1.5,dash='dash'),showlegend=True),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['basis'],name="Basis Premium",line=dict(color='orange',width=1.5),fill='tonexty',fillcolor='rgba(255,165,0,0.18)',showlegend=True),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['premium_thresh_lower'],name="10th % Limit",line=dict(color='#00bcd4',width=1.5,dash='dash'),showlegend=True),row=4,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['spot_cvd'],name="Spot CVD Session",line=dict(color='#00e5ff',width=1.6),fill='tozeroy',fillcolor='rgba(0,229,255,0.18)',showlegend=True),row=5,col=1)
    fig.add_trace(go.Scatter(x=chart['timestamp'],y=chart['ema_cvd'],name=f"CVD EMA {ema_window}",line=dict(color='rgba(255,255,255,0.9)',width=1.2,dash='dot'),showlegend=True),row=5,col=1)
    fig.update_layout(template="plotly_dark",plot_bgcolor='#111',paper_bgcolor='#111',margin=dict(l=50,r=50,t=95,b=30),hovermode="x unified",legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="left",x=0,font=dict(color="white",size=11),bgcolor="rgba(0,0,0,0)"),height=1100,uirevision='locked')
    for ann in fig['layout']['annotations']:
        ann['font']=dict(size=13,color="#bbb")
    fig.update_xaxes(showgrid=True,gridcolor='#222',autorange=True)
    fig.update_yaxes(showgrid=True,gridcolor='#1e1e1e')
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

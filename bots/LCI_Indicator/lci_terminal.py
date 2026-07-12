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

# Mute standard HTTP request logs, only show Errors
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Windows-specific fix for aiohttp compatibility
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# 1. CONSTANTS & CONFIGURATION
# ==========================================
BTC_CIRCULATING_SUPPLY = 19700000
MAX_HISTORY_ROWS = 1440    
UI_REFRESH_INTERVAL = 10000 
USE_SPOT_CVD_FILTER = True  # Toggle to confirm signals with Spot divergence

DATA_FILE = "data/lci_history.csv"
SIGNAL_LOG_FILE = "data/signal_events.csv"
os.makedirs("data", exist_ok=True)

# Main Data Persistence
if os.path.exists(DATA_FILE):
    global_df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows from persistent storage.")
else:
    global_df = pd.DataFrame(columns=[
        'timestamp', 'price', 'basis', 'oi', 'taker_imbalance', 'whale_div', 'spot_delta'
    ])

df_lock = threading.Lock()
csv_lock = threading.Lock()

# Signal Auditing Persistence
logged_signals = set()
if os.path.exists(SIGNAL_LOG_FILE):
    try:
        sig_df = pd.read_csv(SIGNAL_LOG_FILE)
        for _, r in sig_df.iterrows():
            logged_signals.add(f"{r['timestamp']}_{r['timeframe']}_{r['signal']}")
        print(f"Loaded {len(logged_signals)} historical signal events.")
    except Exception:
        pass

# ==========================================
# 2. BACKGROUND DATA DAEMON
# ==========================================
async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200: 
                return await response.json()
            else:
                print(f"API BLOCKED: {url} returned HTTP {response.status}", flush=True)
    except Exception as e:
        print(f"API CRASH: {e}", flush=True)
    return None

async def fetch_binance_data():
    async with aiohttp.ClientSession() as session:
        urls = {
            "spot": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            # Fetching 1m interval for accurate granular volume aggregation
            "spot_kline": "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1",
            "premium": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            "oi": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            "taker": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=1",
            "acc_ratio": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
            "pos_ratio": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=1"
        }

        tasks = {key: fetch_json(session, url) for key, url in urls.items()}
        results = await asyncio.gather(*tasks.values())
        data = dict(zip(tasks.keys(), results))
        
        timestamp = datetime.datetime.now().replace(second=0, microsecond=0)
        
        try:
            spot_price = float(data["spot"]["price"])
            mark_price = float(data["premium"]["markPrice"])
            index_price = float(data["premium"]["indexPrice"])
            total_oi = float(data["oi"]["openInterest"])
            taker_sell = float(data["taker"][0]["sellVol"])
            taker_buy = float(data["taker"][0]["buyVol"])
            acc_ratio = float(data["acc_ratio"][0]["longShortRatio"])
            pos_ratio = float(data["pos_ratio"][0]["longShortRatio"])

            basis = mark_price - index_price
            taker_imbalance = taker_sell / taker_buy if taker_buy > 0 else 1
            whale_div = acc_ratio / pos_ratio if pos_ratio > 0 else 1
            
            # Optimized Spot Volume Delta Calculation (1-min resolution)
            spot_kline_data = data["spot_kline"][0]
            total_vol = float(spot_kline_data[5])
            taker_buy_vol = float(spot_kline_data[9])
            taker_sell_vol = total_vol - taker_buy_vol
            spot_delta = taker_buy_vol - taker_sell_vol

            return {
                'timestamp': timestamp, 'price': spot_price, 'basis': basis, 
                'oi': total_oi, 'taker_imbalance': taker_imbalance, 'whale_div': whale_div,
                'spot_delta': spot_delta
            }
        except (TypeError, KeyError, IndexError):
            return None

async def clock_sync_daemon():
    global global_df
    while True:
        now = datetime.datetime.now()
        # Sleep until the next top of the minute
        sleep_sec = 60 - now.second - (now.microsecond / 1_000_000.0)
        await asyncio.sleep(sleep_sec)
        
        new_data = await fetch_binance_data()
        if new_data:
            with df_lock:
                temp_df = pd.DataFrame([new_data])
                if global_df.empty:
                    global_df = temp_df
                else:
                    global_df = pd.concat([global_df, temp_df], ignore_index=True)
                
                if len(global_df) > MAX_HISTORY_ROWS:
                    global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
                
                global_df.to_csv(DATA_FILE, index=False)

def start_background_thread():
    def loop_runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(clock_sync_daemon())
    threading.Thread(target=loop_runner, daemon=True).start()

# ==========================================
# 3. CSV LOGGING HELPER & UI COMPONENTS
# ==========================================
def log_signal_to_csv(ts_str, timeframe, sig_name, price, reason, row):
    sig_id = f"{ts_str}_{timeframe}_{sig_name}"
    with csv_lock:
        if sig_id not in logged_signals:
            logged_signals.add(sig_id)
            new_row = pd.DataFrame([{
                'timestamp': ts_str, 
                'timeframe': timeframe, 
                'signal': sig_name, 
                'price': price, 
                'reason': reason,
                'price_change': row['price_change'], 
                'price_thresh': row['price_thresh'],
                'oi_change': row['oi_change'], 
                'oi_thresh_upper': row['oi_thresh_upper'], 
                'oi_thresh_lower': row['oi_thresh_lower'],
                'buy_pressure': row['buy_pressure'], 
                'bp_thresh': row['bp_thresh'],
                'sell_pressure': row['sell_pressure'], 
                'sp_thresh': row['sp_thresh'],
                'basis': row['basis'], 
                'premium_thresh': row['premium_thresh'], 
                'premium_thresh_lower': row['premium_thresh_lower']
            }])
            hdr = not os.path.exists(SIGNAL_LOG_FILE)
            new_row.to_csv(SIGNAL_LOG_FILE, mode='a', header=hdr, index=False)

def create_metric_card(title, value, description, thresholds, color="white"):
    return html.Div(style={
        'backgroundColor': '#222', 'padding': '15px', 'borderRadius': '8px', 
        'border': f'1px solid {color}', 'flex': '1', 'minWidth': '200px', 'margin': '10px'
    }, children=[
        html.H3(title, style={'margin': '0 0 5px 0', 'fontSize': '16px', 'color': '#aaa'}),
        html.H2(value, style={'margin': '0 0 5px 0', 'color': color, 'fontSize': '28px'}),
        html.P(description, style={'margin': '0 0 5px 0', 'fontSize': '12px', 'color': '#777'}),
        html.P(thresholds, style={'margin': '0', 'fontSize': '11px', 'color': '#555', 'fontStyle': 'italic'})
    ])

def create_signal_card(title, is_active, active_color, environment, description, strategy, regime, thresholds):
    bg_color = f'rgba{active_color.replace("rgb", "").replace(")", ", 0.15)")}' if is_active else '#141414'
    border_color = active_color if is_active else '#333'
    text_color = active_color if is_active else '#555'
    status_text = "🚨 ACTION REQUIRED" if is_active else "SCANNING..."
    
    return html.Div(style={
        'backgroundColor': bg_color, 'padding': '15px', 'borderRadius': '8px',
        'border': f'2px solid {border_color}', 'flex': '1', 'minWidth': '28%', 'margin': '10px',
        'textAlign': 'left', 'transition': 'all 0.3s ease', 'boxSizing': 'border-box'
    }, children=[
        html.Div(style={'textAlign': 'center', 'marginBottom': '10px'}, children=[
            html.H2(title, style={'margin': '0 0 5px 0', 'color': text_color, 'fontSize': '18px', 'fontWeight': 'bold'}),
            html.H3(status_text, style={'margin': '0', 'color': text_color, 'fontSize': '14px', 'letterSpacing': '1px'}),
        ]),
        html.Div(style={'borderTop': '1px solid #333', 'paddingTop': '10px', 'marginTop': '10px'}, children=[
            html.P([html.Strong("Env: ", style={'color': '#aaa'}), environment], style={'margin': '0 0 5px 0', 'fontSize': '12px', 'color': '#888'}),
            html.P([html.Strong("Data: ", style={'color': '#aaa'}), description], style={'margin': '0 0 5px 0', 'fontSize': '12px', 'color': '#888'}),
            html.P([html.Strong("Action: ", style={'color': '#aaa'}), strategy], style={'margin': '0 0 5px 0', 'fontSize': '12px', 'color': '#888'}),
            html.P([html.Strong("Regime: ", style={'color': '#aaa'}), regime], style={'margin': '0 0 10px 0', 'fontSize': '12px', 'color': '#888'}),
        ]),
        html.P(thresholds, style={'margin': '0', 'fontSize': '11px', 'color': text_color, 'fontWeight': 'bold', 'textAlign': 'center'})
    ])

# ==========================================
# 4. DASHBOARD SERVER (ANCHORED UI WITH LOOKBACK)
# ==========================================
app = dash.Dash(__name__)
app.title = "Order Flow Terminal"

app.layout = html.Div(style={'backgroundColor': '#111', 'color': 'white', 'fontFamily': 'Arial, sans-serif', 'padding': '20px', 'minHeight': '100vh'}, children=[
    # --- STORES FOR STATE MANAGEMENT ---
    dcc.Store(id='last-signal-time', data=None),
    dcc.Store(id='sound-trigger', data=None),
    html.Div(id='audio-dummy', style={'display': 'none'}), # Target for JS callback

    html.H1("QUANTITATIVE ORDER FLOW TERMINAL", style={'textAlign': 'center', 'letterSpacing': '2px', 'marginBottom': '5px'}),
    html.Div(id='last-updated-label', style={'textAlign': 'center', 'color': '#888', 'fontSize': '14px', 'marginBottom': '20px', 'fontStyle': 'italic'}),
    
    # --- CONTROL PANEL ---
    html.Div(style={'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'center', 'alignItems': 'center', 'marginBottom': '20px', 'gap': '20px', 'backgroundColor': '#1a1a1a', 'padding': '15px', 'borderRadius': '8px', 'border': '1px solid #333'}, children=[
        html.Div([
            html.Label("Timeframe: ", style={'fontWeight': 'bold', 'marginRight': '8px'}),
            dcc.Dropdown(
                id='timeframe-dropdown',
                options=[{'label': '1 Minute', 'value': '1min'}, {'label': '5 Minutes', 'value': '5min'}, {'label': '15 Minutes', 'value': '15min'}, {'label': '30 Minutes', 'value': '30min'}],
                value='5min', clearable=False, style={'width': '120px', 'display': 'inline-block', 'color': 'black', 'textAlign': 'left'}
            )
        ]),
        html.Div([
            html.Label("History Range: ", style={'fontWeight': 'bold', 'marginRight': '8px'}),
            dcc.Dropdown(
                id='range-dropdown',
                options=[{'label': 'Last 1 Hour', 'value': '1h'}, {'label': 'Last 6 Hours', 'value': '6h'}, {'label': 'Last 24 Hours', 'value': '24h'}, {'label': 'Last 1 Week', 'value': '7d'}, {'label': 'All Time', 'value': 'all'}],
                value='24h', clearable=False, style={'width': '140px', 'display': 'inline-block', 'color': 'black', 'textAlign': 'left'}
            )
        ]),
        html.Div([
            html.Label("EMA Window: ", style={'fontWeight': 'bold', 'marginRight': '8px'}),
            dcc.Input(id='ema-window', type='number', value=20, min=2, max=200, style={'width': '60px', 'borderRadius': '4px', 'border': 'none', 'padding': '8px', 'backgroundColor': '#fff', 'color': '#000'})
        ]),
        html.Div([
            html.Label("Alert Sound: ", style={'fontWeight': 'bold', 'marginRight': '8px'}),
            dcc.Dropdown(
                id='sound-dropdown',
                options=[
                    {'label': 'Sonar Ping', 'value': 'https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg'},
                    {'label': 'Beep', 'value': 'https://actions.google.com/sounds/v1/alarms/beep_short.ogg'},
                    {'label': 'Digital Watch', 'value': 'https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg'},
                    {'label': 'Mute', 'value': 'none'}
                ],
                value='https://actions.google.com/sounds/v1/alarms/sonar_ping.ogg', clearable=False, style={'width': '150px', 'display': 'inline-block', 'color': 'black', 'textAlign': 'left'}
            )
        ]),
        html.Div([
            html.Label("Sound Duration (s): ", style={'fontWeight': 'bold', 'marginRight': '8px'}),
            dcc.Input(id='sound-duration', type='number', value=6, min=1, max=60, style={'width': '60px', 'borderRadius': '4px', 'border': 'none', 'padding': '8px', 'backgroundColor': '#fff', 'color': '#000'})
        ])
    ]),

    html.Div(id='signal-row-up'),
    html.Div(id='signal-row-down'),
    html.Div(id='metrics-row'),
    html.Div(id='event-log-row'),
    
    html.Div(
        dcc.Graph(id='main-chart', config={'displayModeBar': False}),
        style={'marginTop': '20px', 'border': '1px solid #333', 'borderRadius': '8px'}
    ),
    
    dcc.Interval(id='interval-component', interval=UI_REFRESH_INTERVAL, n_intervals=0)
])

# ==========================================
# 4.1 CLIENT-SIDE AUDIO CONTROLLER
# ==========================================
app.clientside_callback(
    """
    function(trigger, sound_url, duration) {
        if(trigger && sound_url && sound_url !== 'none') {
            var audio = new Audio(sound_url);
            audio.loop = true;
            // Catch required because browsers block autoplay until user clicks anywhere on the page
            audio.play().catch(function(e) { console.log("Alert muted: User must click on the page first to allow audio."); });
            setTimeout(function() {
                audio.pause();
                audio.currentTime = 0;
            }, (duration || 6) * 1000);
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output('audio-dummy', 'children'),
    Input('sound-trigger', 'data'),
    State('sound-dropdown', 'value'),
    State('sound-duration', 'value')
)

# ==========================================
# 4.2 MAIN DASHBOARD CALLBACK
# ==========================================
@app.callback(
    [Output('last-updated-label', 'children'),
     Output('signal-row-up', 'children'),
     Output('signal-row-down', 'children'),
     Output('metrics-row', 'children'),
     Output('event-log-row', 'children'),
     Output('main-chart', 'figure'),
     Output('last-signal-time', 'data'),
     Output('sound-trigger', 'data')],
    [Input('interval-component', 'n_intervals'), 
     Input('timeframe-dropdown', 'value'),
     Input('range-dropdown', 'value'),
     Input('ema-window', 'value')],
    [State('last-signal-time', 'data')]
)
def update_dashboard(n, timeframe, time_range, ema_window, last_signal_time):
    with df_lock:
        if global_df.empty or len(global_df) < 2:
            loading = html.H3("GATHERING DATA PIPELINE...", style={'textAlign': 'center', 'color': 'grey'})
            return "Initializing...", loading, "", "", "", go.Figure(), dash.no_update, dash.no_update
        df_ui = global_df.copy()

    df_ui['timestamp'] = pd.to_datetime(df_ui['timestamp'])
    ema_window = ema_window if ema_window else 20

    # 1. CALCULATE RAW 1-MIN CVD BEFORE TIMEFRAME RESAMPLING
    # This ensures that when we resample to 5m/15m/30m, we are capturing the true cumulative total
    if 'spot_delta' not in df_ui.columns:
        df_ui['spot_delta'] = 0.0
    df_ui['spot_delta'] = df_ui['spot_delta'].fillna(0.0)
    df_ui['spot_cvd'] = df_ui['spot_delta'].cumsum()

    # 2. APPLY TIMEFRAME RESAMPLING
    df_ui.set_index('timestamp', inplace=True)
    if timeframe != '1min':
        # Grab the last known value at the end of the timeframe boundary
        df_ui = df_ui.resample(timeframe, closed='left', label='left').last().ffill()
    df_ui.reset_index(inplace=True)

    if len(df_ui) < 2:
        loading = html.H3("CALIBRATING TIMEFRAME...", style={'textAlign': 'center', 'color': 'grey'})
        return "Resampling Data...", loading, "", "", "", go.Figure(), dash.no_update, dash.no_update

    # 3. MATHEMATICAL ENGINE (Now accurate to the timeframe selected)
    df_ui['price_change'] = df_ui['price'].pct_change() * 100
    df_ui['oi_change'] = df_ui['oi'].pct_change() * 100
    df_ui['whale_delta'] = df_ui['whale_div'].diff()
    
    # CVD Trend is the difference between the resampled CVD blocks
    df_ui['cvd_trend'] = df_ui['spot_cvd'].diff().fillna(0.0) 
    
    df_ui['buy_pressure'] = np.where(df_ui['taker_imbalance'] > 0, 1 / df_ui['taker_imbalance'], 1)
    df_ui['sell_pressure'] = df_ui['taker_imbalance']

    price_mean = df_ui['price_change'].rolling(window=20, min_periods=1).mean().abs()
    price_std = df_ui['price_change'].rolling(window=20, min_periods=1).std().fillna(0)
    df_ui['price_thresh'] = price_mean + (price_std * 1.5)

    oi_mean = df_ui['oi_change'].rolling(window=20, min_periods=1).mean().abs()
    oi_std = df_ui['oi_change'].rolling(window=20, min_periods=1).std().fillna(0)
    df_ui['oi_thresh_upper'] = oi_mean + (oi_std * 1.5)
    df_ui['oi_thresh_lower'] = -df_ui['oi_thresh_upper']

    df_ui['premium_thresh'] = df_ui['basis'].rolling(window=100, min_periods=1).quantile(0.90)
    df_ui['premium_thresh_lower'] = df_ui['basis'].rolling(window=100, min_periods=1).quantile(0.10)
    
    bp_mean = df_ui['buy_pressure'].rolling(window=100, min_periods=1).mean()
    bp_std = df_ui['buy_pressure'].rolling(window=100, min_periods=1).std().fillna(0)
    df_ui['bp_thresh'] = bp_mean + (bp_std * 1.5)

    sp_mean = df_ui['sell_pressure'].rolling(window=100, min_periods=1).mean()
    sp_std = df_ui['sell_pressure'].rolling(window=100, min_periods=1).std().fillna(0)
    df_ui['sp_thresh'] = sp_mean + (sp_std * 1.5)

    df_ui['price_thresh'] = df_ui['price_thresh'].fillna(0.05)
    df_ui['oi_thresh_upper'] = df_ui['oi_thresh_upper'].fillna(0.10)
    df_ui['oi_thresh_lower'] = df_ui['oi_thresh_lower'].fillna(-0.15)
    df_ui['premium_thresh'] = df_ui['premium_thresh'].fillna(5.0)
    df_ui['premium_thresh_lower'] = df_ui['premium_thresh_lower'].fillna(-5.0)
    df_ui['bp_thresh'] = df_ui['bp_thresh'].fillna(1.2)
    df_ui['sp_thresh'] = df_ui['sp_thresh'].fillna(1.2)

    # OMNIDIRECTIONAL SIGNAL MATCHER
    df_ui['is_breakout'] = (df_ui['price_change'] > df_ui['price_thresh']) & (df_ui['oi_change'] > df_ui['oi_thresh_upper']) & (df_ui['whale_delta'] < 0)
    df_ui['is_fakeout'] = (df_ui['price_change'] > df_ui['price_thresh']) & (df_ui['oi_change'] < df_ui['oi_thresh_lower'])
    df_ui['is_exhaustion'] = (df_ui['price_change'] > 0) & (df_ui['buy_pressure'] > df_ui['bp_thresh']) & (df_ui['basis'] > df_ui['premium_thresh']) & (df_ui['whale_delta'] > 0)

    df_ui['is_breakdown'] = (df_ui['price_change'] < -df_ui['price_thresh']) & (df_ui['oi_change'] > df_ui['oi_thresh_upper']) & (df_ui['whale_delta'] > 0)
    df_ui['is_long_liq'] = (df_ui['price_change'] < -df_ui['price_thresh']) & (df_ui['oi_change'] < df_ui['oi_thresh_lower'])
    df_ui['is_bottom_exhaust'] = (df_ui['price_change'] < 0) & (df_ui['sell_pressure'] > df_ui['sp_thresh']) & (df_ui['basis'] < df_ui['premium_thresh_lower']) & (df_ui['whale_delta'] < 0)

    # --- SPOT VOLUME DIVERGENCE FILTER ---
    if USE_SPOT_CVD_FILTER:
        # Upside Signals
        df_ui['is_breakout'] = df_ui['is_breakout'] & (df_ui['cvd_trend'] > 0)        
        df_ui['is_fakeout'] = df_ui['is_fakeout'] & (df_ui['cvd_trend'] <= 0)         
        df_ui['is_exhaustion'] = df_ui['is_exhaustion'] & (df_ui['cvd_trend'] < 0)    
        
        # Downside Signals
        df_ui['is_breakdown'] = df_ui['is_breakdown'] & (df_ui['cvd_trend'] < 0)      
        df_ui['is_long_liq'] = df_ui['is_long_liq'] & (df_ui['cvd_trend'] >= 0)       
        df_ui['is_bottom_exhaust'] = df_ui['is_bottom_exhaust'] & (df_ui['cvd_trend'] > 0) 

    current = df_ui.iloc[-1]
    current_ts_str = current['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
    last_updated_str = f"Live Market Data - Last Updated: {current_ts_str} IST"
    
    # Check for New Audio Alert Trigger
    is_active_signal = any([
        current['is_breakout'], current['is_fakeout'], current['is_exhaustion'],
        current['is_breakdown'], current['is_long_liq'], current['is_bottom_exhaust']
    ])
    
    sound_trigger = dash.no_update
    if is_active_signal and last_signal_time != current_ts_str:
        sound_trigger = current_ts_str
        last_signal_time = current_ts_str

    # BUILD UI CARDS
    signal_row_up = html.Div(style={
        'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'space-between', 
        'backgroundColor': 'rgba(0, 255, 0, 0.04)', 'padding': '10px', 'borderRadius': '8px', 
        'border': '1px solid rgba(0, 255, 0, 0.2)', 'marginBottom': '15px'
    }, children=[
        create_signal_card("🟩 TRUE BREAKOUT", current['is_breakout'], "rgb(0, 255, 0)", "Trend Continuation", "Price UP + OI UP + Whales Buying.", "Ride the Trend. Enter Long on close.", "High Volatility (NY/London)", f"Target: Price > +{current['price_thresh']:.2f}% | OI > +{current['oi_thresh_upper']:.2f}% | CVD Δ > 0"),
        create_signal_card("🟪 LONG LIQUIDATION", current['is_long_liq'], "rgb(170, 0, 255)", "Mean Reversion (Dip)", "Price DOWN + OI DOWN rapidly.", "Fade the Flush. Enter Long post-wick.", "Low Volatility (Weekends/Asia)", f"Target: Price < -{current['price_thresh']:.2f}% | OI < {current['oi_thresh_lower']:.2f}% | CVD Δ ≥ 0"),
        create_signal_card("🔵 BOTTOM EXHAUSTION", current['is_bottom_exhaust'], "rgb(0, 200, 255)", "Macro Reversal", "Price DOWN + Extreme Sell Pressure + Whales Buying.", "Catch the Knife. Enter Long.", "Parabolic Bear / Death Spiral", f"Target: Sell Pressure > {current['sp_thresh']:.2f}x | Premium < ${current['premium_thresh_lower']:.2f} | CVD Δ > 0")
    ])

    signal_row_down = html.Div(style={
        'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'space-between', 
        'backgroundColor': 'rgba(255, 0, 0, 0.04)', 'padding': '10px', 'borderRadius': '8px', 
        'border': '1px solid rgba(255, 0, 0, 0.2)', 'marginBottom': '15px'
    }, children=[
        create_signal_card("🟥 TRUE BREAKDOWN", current['is_breakdown'], "rgb(255, 0, 0)", "Trend Continuation", "Price DOWN + OI UP + Whales Selling.", "Ride the Trend. Enter Short on close.", "High Volatility (NY/London)", f"Target: Price < -{current['price_thresh']:.2f}% | OI > +{current['oi_thresh_upper']:.2f}% | CVD Δ < 0"),
        create_signal_card("🟧 SHORT SQUEEZE", current['is_fakeout'], "rgb(255, 165, 0)", "Mean Reversion (Top)", "Price UP + OI DOWN rapidly.", "Fade the Fakeout. Enter Short post-spike.", "Low Volatility (Weekends/Asia)", f"Target: Price > +{current['price_thresh']:.2f}% | OI < {current['oi_thresh_lower']:.2f}% | CVD Δ ≤ 0"),
        create_signal_card("🔴 TOP EXHAUSTION", current['is_exhaustion'], "rgb(255, 50, 50)", "Macro Reversal", "Price UP + Extreme Buy Pressure + Whales Selling.", "Top Tick. Enter Short.", "Parabolic Bull Run", f"Target: Buy Pressure > {current['bp_thresh']:.2f}x | Premium > ${current['premium_thresh']:.2f} | CVD Δ < 0")
    ])

    metrics_row = html.Div(style={'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'center', 'marginTop': '10px'}, children=[
        create_metric_card("Price Change", f"{current['price_change']:+.2f}%", "The % move in Bitcoin price.", f"Dynamic Vol Threshold: ±{current['price_thresh']:.2f}%", "#00FF00" if current['price_change'] > 0 else "#FF0000"),
        create_metric_card("OI Velocity", f"{current['oi_change']:+.2f}%", "New money entering vs positions closing.", f"Dynamic Target: > +{current['oi_thresh_upper']:.2f}% or < {current['oi_thresh_lower']:.2f}%", "#00FF00" if current['oi_change'] > 0 else ("#FF0000" if current['oi_change'] < 0 else "white")),
        create_metric_card("Taker Buy Pressure", f"{current['buy_pressure']:.2f}x", "Market Buy vs Sell Volume.", f"Dynamic Noise Filter: > {current['bp_thresh']:.2f}x", "cyan"),
        create_metric_card("Basis Premium", f"${current['basis']:.2f}", "Futures Price minus Spot Price.", f"Regime Limits: > ${current['premium_thresh']:.2f} | < ${current['premium_thresh_lower']:.2f}", "orange" if current['basis'] > current['premium_thresh'] else ("cyan" if current['basis'] < current['premium_thresh_lower'] else "white"))
    ])

    # 4. DETAILED EVENT LOG WITH REASONS AND CSV LOGGING
    log_entries = []
    for idx, row in df_ui.iloc[::-1].iterrows():
        ts_str = row['timestamp'].strftime('%Y-%m-%d %H:%M')
        price_str = f"${row['price']:,.2f}"
        
        cvd_info = f" | Spot CVD Δ {row['cvd_trend']:.2f}" if 'cvd_trend' in row and not pd.isna(row['cvd_trend']) else ""

        # Upside Logs
        if row['is_breakout']:
            reason = f"Price {row['price_change']:+.2f}% (> {row['price_thresh']:.2f}%) | OI {row['oi_change']:+.2f}% (> {row['oi_thresh_upper']:.2f}%) | WhaleΔ < 0{cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🟩 TRUE BREAKOUT Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'lime', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "TRUE_BREAKOUT", row['price'], reason, row)
            
        if row['is_fakeout']: 
            reason = f"Price {row['price_change']:+.2f}% (> {row['price_thresh']:.2f}%) | OI {row['oi_change']:+.2f}% (< {row['oi_thresh_lower']:.2f}%){cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🟧 SHORT SQUEEZE Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'orange', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "SHORT_SQUEEZE", row['price'], reason, row)
            
        if row['is_exhaustion']: 
            reason = f"Buy Press {row['buy_pressure']:.2f}x (> {row['bp_thresh']:.2f}x) | Premium ${row['basis']:.2f} (> ${row['premium_thresh']:.2f}) | WhaleΔ > 0{cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🔴 TOP EXHAUSTION Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'rgb(255, 50, 50)', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "TOP_EXHAUSTION", row['price'], reason, row)
            
        # Downside Logs
        if row['is_breakdown']: 
            reason = f"Price {row['price_change']:+.2f}% (< -{row['price_thresh']:.2f}%) | OI {row['oi_change']:+.2f}% (> {row['oi_thresh_upper']:.2f}%) | WhaleΔ > 0{cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🟥 TRUE BREAKDOWN Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'red', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "TRUE_BREAKDOWN", row['price'], reason, row)
            
        if row['is_long_liq']: 
            reason = f"Price {row['price_change']:+.2f}% (< -{row['price_thresh']:.2f}%) | OI {row['oi_change']:+.2f}% (< {row['oi_thresh_lower']:.2f}%){cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🟪 LONG LIQUIDATION Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'rgb(170, 0, 255)', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "LONG_LIQUIDATION", row['price'], reason, row)
            
        if row['is_bottom_exhaust']: 
            reason = f"Sell Press {row['sell_pressure']:.2f}x (> {row['sp_thresh']:.2f}x) | Premium ${row['basis']:.2f} (< ${row['premium_thresh_lower']:.2f}) | WhaleΔ < 0{cvd_info}"
            log_entries.append(html.Div([
                html.Span(f"[{ts_str}] 🔵 BOTTOM EXHAUSTION Detected @ {price_str}", style={'fontWeight': 'bold'}), html.Br(),
                html.Span(f"↳ Reason: {reason}", style={'fontSize': '12px', 'color': '#888', 'marginLeft': '15px'})
            ], style={'color': 'rgb(0, 200, 255)', 'marginBottom': '10px'}))
            log_signal_to_csv(ts_str, timeframe, "BOTTOM_EXHAUSTION", row['price'], reason, row)
            
    if not log_entries:
        log_entries.append(html.Div("No signals triggered in the current memory window.", style={'color': '#555'}))

    event_log = html.Div(style={'backgroundColor': '#1a1a1a', 'border': '1px solid #333', 'borderRadius': '8px', 'padding': '15px', 'height': '180px', 'overflowY': 'auto', 'marginTop': '20px', 'fontFamily': 'monospace'}, children=[
        html.H3("SIGNAL EVENT LOG & AUDIT", style={'margin': '0 0 10px 0', 'fontSize': '14px', 'color': '#aaa'}),
        html.Div(children=log_entries)
    ])

    # 5. CHART & EMA CALCULATIONS
    chart_df = df_ui.copy()
    
    # Calculate Custom EMA Overlays
    chart_df['ema_buy_pressure'] = chart_df['buy_pressure'].ewm(span=ema_window, adjust=False).mean()
    chart_df['ema_whale'] = chart_df['whale_div'].ewm(span=ema_window, adjust=False).mean()
    chart_df['ema_basis'] = chart_df['basis'].ewm(span=ema_window, adjust=False).mean()
    chart_df['ema_cvd'] = chart_df['spot_cvd'].ewm(span=ema_window, adjust=False).mean()
    
    if time_range != 'all':
        max_ts = chart_df['timestamp'].max()
        if time_range == '1h': chart_df = chart_df[chart_df['timestamp'] >= max_ts - pd.Timedelta(hours=1)]
        elif time_range == '6h': chart_df = chart_df[chart_df['timestamp'] >= max_ts - pd.Timedelta(hours=6)]
        elif time_range == '24h': chart_df = chart_df[chart_df['timestamp'] >= max_ts - pd.Timedelta(hours=24)]
        elif time_range == '7d': chart_df = chart_df[chart_df['timestamp'] >= max_ts - pd.Timedelta(days=7)]

    # --- RENDER SUBPLOTS (Now 5 Rows) ---
    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.04, 
        row_heights=[0.35, 0.15, 0.15, 0.15, 0.20], 
        subplot_titles=("Price & Open Interest", "Taker Buy Pressure", "Whale Ratio", "Basis Premium", "Spot Cumulative Volume Delta (CVD)"),
        specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": False}]]
    )
    
    # Row 1: Price & OI
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['price'], name="BTC Price", line=dict(color='white', width=2), showlegend=True), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['oi'], name="Open Interest", line=dict(color='cyan', width=2, dash='dot'), showlegend=True), row=1, col=1, secondary_y=True)
    
    # Row 2: Taker Buy Pressure + EMA
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['buy_pressure'], name="Buy Pressure", line=dict(color='magenta', width=1), showlegend=True), row=2, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['ema_buy_pressure'], name=f"EMA ({ema_window})", line=dict(color='rgba(255,255,255,0.7)', width=1.5, dash='dot'), showlegend=True), row=2, col=1)
    
    # Row 3: Whale Ratio + EMA
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['whale_div'], name="Whale Ratio", line=dict(color='yellow', width=1), showlegend=True), row=3, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['ema_whale'], name=f"EMA ({ema_window})", line=dict(color='rgba(255,255,255,0.7)', width=1.5, dash='dot'), showlegend=False), row=3, col=1)

    # Row 4: Basis Premium + Bands + EMA
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['premium_thresh'], name="90th % Limit", line=dict(color='rgba(255,165,0,0.3)', width=1, dash='dash'), showlegend=True), row=4, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['basis'], name="Basis Premium", line=dict(color='orange', width=1), fill='tonexty', showlegend=True), row=4, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['premium_thresh_lower'], name="10th % Limit", line=dict(color='rgba(0,200,255,0.3)', width=1, dash='dash'), showlegend=True), row=4, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['ema_basis'], name=f"EMA ({ema_window})", line=dict(color='rgba(255,255,255,0.7)', width=1.5, dash='dot'), showlegend=False), row=4, col=1)
    
    # Row 5: Spot CVD + EMA
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['spot_cvd'], name="Spot CVD", line=dict(color='cyan', width=1.5), fill='tozeroy', showlegend=True), row=5, col=1)
    fig.add_trace(go.Scatter(x=chart_df['timestamp'], y=chart_df['ema_cvd'], name=f"CVD EMA ({ema_window})", line=dict(color='rgba(255,255,255,0.7)', width=1.5, dash='dot'), showlegend=False), row=5, col=1)

    fig.update_layout(
        template="plotly_dark", plot_bgcolor='#111', paper_bgcolor='#111', margin=dict(l=40, r=40, t=40, b=20),
        hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1),
        height=1050, uirevision=timeframe 
    )
    
    for annotation in fig['layout']['annotations']:
        annotation['font'] = dict(size=14, color="#aaa")

    fig.update_xaxes(showgrid=True, gridcolor='#222', autorange=True)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1, secondary_y=False, showgrid=False)
    fig.update_yaxes(title_text="OI (BTC)", row=1, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="Taker Buy (x)", row=2, col=1, showgrid=True, gridcolor='#222')
    fig.update_yaxes(title_text="Whale Ratio", row=3, col=1, showgrid=True, gridcolor='#222')
    fig.update_yaxes(title_text="Basis ($)", row=4, col=1, showgrid=True, gridcolor='#222')
    fig.update_yaxes(title_text="Spot CVD", row=5, col=1, showgrid=True, gridcolor='#222')

    return last_updated_str, signal_row_up, signal_row_down, metrics_row, event_log, fig, last_signal_time, sound_trigger

# ==========================================
# 6. EXECUTION
# ==========================================
if __name__ == '__main__':
    start_background_thread()
    app.run(host='0.0.0.0', port=8050, debug=False)

import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import aiohttp
import asyncio
import datetime
import threading
import sys
import os

# Windows-specific fix for aiohttp & aiodns compatibility
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# 1. CONSTANTS & CONFIGURATION
# ==========================================
BTC_CIRCULATING_SUPPLY = 19700000
EMA_SPAN = 240             # 4 hours rolling EMA baseline for high reactivity
MAX_HISTORY_ROWS = 1440    # Stores exactly 24 hours of 1m data for higher timeframes (Highly RAM Optimized)
UI_REFRESH_INTERVAL = 10000 # UI checks for updates every 10 seconds

# LCI Weights
W_BASIS = 0.25
W_OI_VEL = 0.30
W_TAKER = 0.20
W_LEV = 0.15
W_WHALE = 0.10

DATA_FILE = "data/lci_history.csv"
os.makedirs("data", exist_ok=True)

# RAM Optimization: Load persistent data, strictly enforce the 1440 row limit
if os.path.exists(DATA_FILE):
    global_df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows from persistent storage.")
else:
    global_df = pd.DataFrame(columns=[
        'timestamp', 'price', 'basis', 'oi', 'oi_velocity', 
        'taker_imbalance', 'lev_therm', 'whale_div', 'LCI'
    ])
df_lock = threading.Lock()

# ==========================================
# 2. ASYNCHRONOUS DATA PIPELINE
# ==========================================
async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return None

async def fetch_binance_data():
    async with aiohttp.ClientSession() as session:
        urls = {
            "spot": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            "premium": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            "oi": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            "taker": "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=1",
            "acc_ratio": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
            "pos_ratio": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=1"
        }

        tasks = {key: fetch_json(session, url) for key, url in urls.items()}
        results = await asyncio.gather(*tasks.values())
        data = dict(zip(tasks.keys(), results))
        
        # Floor timestamp to the nearest minute to align perfectly with OHLC candles
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
            lev_therm = total_oi / BTC_CIRCULATING_SUPPLY
            whale_div = acc_ratio / pos_ratio if pos_ratio > 0 else 1

            return {
                'timestamp': timestamp, 'price': spot_price, 'basis': basis, 'oi': total_oi,
                'taker_imbalance': taker_imbalance, 'lev_therm': lev_therm, 'whale_div': whale_div
            }
        except (TypeError, KeyError, IndexError) as e:
            return None

# ==========================================
# 3. LCI MATHEMATICAL MODEL (MIN-MAX ENGINE)
# ==========================================
HISTORY_PERIODS = 288  # 24 hours of 5-minute candlestick data

def calculate_minmax_score(series, window=HISTORY_PERIODS, min_periods=12, invert=False):
    """
    Applies a Rolling Min-Max Scaler to force maximum vertical amplitude.
    Strictly bounds the localized score between [-10, 10].
    """
    if len(series) < min_periods:
        return 0.0  # Output neutral 0 until 12 periods (1 hour) are collected
        
    # Calculate rolling absolute extremes
    rolling_min = series.rolling(window=window, min_periods=min_periods).min().iloc[-1]
    rolling_max = series.rolling(window=window, min_periods=min_periods).max().iloc[-1]
    raw_val = series.iloc[-1]
    
    # Scale between -10 and +10, adding 1e-8 to prevent Division by Zero on flatlines
    score = 20 * ((raw_val - rolling_min) / (rolling_max - rolling_min + 1e-8)) - 10
    
    return -score if invert else score

async def update_global_state():
    """Updates the pandas DataFrame and calculates the new LCI score."""
    global global_df
    
    new_data = await fetch_binance_data()
    if not new_data: 
        return global_df 
    
    with df_lock:
        # 1. Calculate OI Velocity
        if len(global_df) > 0:
            prev_oi = global_df.iloc[-1]['oi']
            oi_velocity = (new_data['oi'] - prev_oi) / prev_oi if prev_oi > 0 else 0
        else:
            oi_velocity = 0
            
        new_data['oi_velocity'] = oi_velocity

        # Append new data securely
        temp_df = pd.DataFrame([new_data])
        
        if global_df.empty:
            global_df = temp_df
        else:
            global_df = pd.concat([global_df, temp_df], ignore_index=True)
            
        # Enforce the new 288-period (24h) memory threshold
        if len(global_df) > HISTORY_PERIODS:
            global_df = global_df.iloc[-HISTORY_PERIODS:]
        
        # 2. Score Generation using the Min-Max Scaler
        # Notice we pass the entire series so pandas can run the rolling window correctly
        score_a = calculate_minmax_score(global_df['basis'])
        score_c = calculate_minmax_score(global_df['taker_imbalance'], invert=True)
        score_d = calculate_minmax_score(global_df['lev_therm'])
        score_e = calculate_minmax_score(global_df['whale_div'])

        # COMPONENT B: HARD THRESHOLD LOGIC FOR OPEN INTEREST
        if oi_velocity <= -0.005:
            score_b = -10.0
        else:
            score_b = calculate_minmax_score(global_df['oi_velocity'])

        # 3. Weighted Assembly
        final_lci = (
            (score_a * W_BASIS) +
            (score_b * W_OI_VEL) +
            (score_c * W_TAKER) +
            (score_d * W_LEV) +
            (score_e * W_WHALE)
        ) * 10
        
        # FINAL SAFEGUARD: Hard clip boundaries against anomalous data spikes
        final_lci = np.clip(final_lci, -100, 100)
        
        global_df.at[global_df.index[-1], 'LCI'] = final_lci
        
        # Save to persistent CSV 
        global_df.to_csv(DATA_FILE, index=False)
        
    return global_df

# ==========================================
# 4. BACKGROUND CLOCK-SYNC DAEMON
# ==========================================
async def clock_sync_daemon():
    """Sleeps on zero-CPU until the exact top of the minute, ensuring OHLC alignment."""
    while True:
        now = datetime.datetime.now()
        # Calculate precise seconds remaining until the next exact minute (XX:XX:00)
        sleep_sec = 60 - now.second - (now.microsecond / 1_000_000.0)
        await asyncio.sleep(sleep_sec)
        await update_global_state()

def start_background_thread():
    def loop_runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(clock_sync_daemon())
    
    # Daemon=True ensures it dies quietly when the main server shuts down
    thread = threading.Thread(target=loop_runner, daemon=True)
    thread.start()

# ==========================================
# 5. DASHBOARD & UI SERVER
# ==========================================
app = dash.Dash(__name__)
app.title = "LCI Terminal"

app.layout = html.Div(style={'backgroundColor': '#111', 'color': 'white', 'fontFamily': 'Arial, sans-serif', 'padding': '20px', 'minHeight': '100vh'}, children=[
    html.H1("LEVERAGE CAPITULATION INDEX (LCI)", style={'textAlign': 'center', 'letterSpacing': '2px'}),
    
    html.Div(id='alert-widget', style={
        'margin': '20px auto', 'padding': '20px', 'width': '60%', 
        'borderRadius': '10px', 'textAlign': 'center', 'border': '2px solid #333'
    }),
    
    # Timeframe Selector
    html.Div(style={'textAlign': 'center', 'marginBottom': '20px'}, children=[
        html.Label("Select Timeframe: ", style={'marginRight': '10px', 'fontWeight': 'bold'}),
        dcc.Dropdown(
            id='timeframe-dropdown',
            options=[
                {'label': '1m', 'value': '1min'},
                {'label': '5m', 'value': '5min'},
                {'label': '15m', 'value': '15min'},
                {'label': '30m', 'value': '30min'},
                {'label': '1H', 'value': '1h'},
                {'label': '4H', 'value': '4h'},
                {'label': '1D', 'value': '1d'}
            ],
            value='5min', # Sets 5m as the default display
            clearable=False,
            style={'width': '150px', 'display': 'inline-block', 'color': 'black', 'textAlign': 'left'}
        )
    ]),

    dcc.Graph(id='lci-chart', config={'displayModeBar': False}),
    
    # Rapid UI refresh to catch background daemon updates immediately
    dcc.Interval(id='interval-component', interval=UI_REFRESH_INTERVAL, n_intervals=0)
])

# NOTE: This function is completely synchronous now, freeing up overhead!
@app.callback(
    [Output('lci-chart', 'figure'),
     Output('alert-widget', 'children'),
     Output('alert-widget', 'style')],
    [Input('interval-component', 'n_intervals'),
     Input('timeframe-dropdown', 'value')]
)
def update_dashboard(n, timeframe):
    with df_lock:
        if global_df.empty or 'LCI' not in global_df.columns or pd.isna(global_df['LCI'].iloc[-1]):
            return go.Figure(), "INITIALIZING DATA PIPELINE...", {'backgroundColor': '#222', 'color': 'grey'}
        # Instantly copy and release the lock to prevent blocking
        df_ui = global_df.copy()

    # --- PANDAS OHLC RESAMPLING LOGIC ---
    df_ui.set_index('timestamp', inplace=True)
    if timeframe != '1min':
        # closed='right', label='right' ensures 14:01-14:05 buckets strictly into 14:05 closing price
        df_ui = df_ui.resample(timeframe, closed='right', label='right').last().dropna()
    df_ui.reset_index(inplace=True)

    if df_ui.empty:
        return go.Figure(), "GATHERING TIMEFRAME DATA...", {'backgroundColor': '#222', 'color': 'grey'}

    # Alert Widget always uses the absolute latest active price/LCI regardless of chart timeframe
    latest_lci = df_ui['LCI'].iloc[-1]
    latest_price = df_ui['price'].iloc[-1]
    
    default_style = {'margin': '20px auto', 'padding': '20px', 'width': '60%', 'borderRadius': '10px', 'textAlign': 'center', 'fontWeight': 'bold', 'fontSize': '24px'}
    
    if 61450 <= latest_price <= 62050:
        if latest_lci <= -70:
            alert_text = html.Div([f"BTC: ${latest_price:,.2f} | LCI: {latest_lci:.1f}", html.Br(), "🚨 BUY CONFLUENCE READY 🚨"])
            alert_style = {**default_style, 'backgroundColor': 'rgba(0, 255, 0, 0.1)', 'color': '#00FF00', 'border': '2px solid #00FF00'}
        else:
            alert_text = html.Div([f"BTC: ${latest_price:,.2f} | LCI: {latest_lci:.1f}", html.Br(), "⚠️ WARNING: FUNDING TRAP ⚠️"])
            alert_style = {**default_style, 'backgroundColor': 'rgba(255, 165, 0, 0.1)', 'color': 'orange', 'border': '2px solid orange'}
    else:
        alert_text = html.Div([f"BTC: ${latest_price:,.2f} | LCI: {latest_lci:.1f}", html.Br(), "MONITORING"])
        alert_style = {**default_style, 'backgroundColor': '#222', 'color': 'grey', 'border': '2px solid #444'}

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_ui['timestamp'], y=df_ui['LCI'], mode='lines', 
        name='LCI Score', line=dict(color='cyan', width=2)
    ))
    
    fig.add_hrect(y0=-100, y1=-70, fillcolor="green", opacity=0.15, line_width=0, annotation_text="Capitulation Zone", annotation_position="bottom right")
    fig.add_hrect(y0=-49, y1=49, fillcolor="grey", opacity=0.1, line_width=0)
    fig.add_hrect(y0=50, y1=100, fillcolor="red", opacity=0.15, line_width=0, annotation_text="Danger Zone", annotation_position="top right")

    fig.update_layout(
        template="plotly_dark", plot_bgcolor='#111', paper_bgcolor='#111', margin=dict(l=40, r=40, t=40, b=40),
        yaxis=dict(range=[-100, 100], title="LCI Score", gridcolor='#333'),
        xaxis=dict(title="Time", gridcolor='#333'), hovermode="x unified"
    )

    return fig, alert_text, alert_style

# ==========================================
# 6. EXECUTION
# ==========================================
if __name__ == '__main__':
    start_background_thread()  # Starts the clock-sync daemon
    app.run(host='0.0.0.0', port=8050, debug=False)

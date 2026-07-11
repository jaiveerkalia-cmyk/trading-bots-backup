import dash
from dash import dcc, html
from dash.dependencies import Input, Output
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

DATA_FILE = "data/lci_history.csv"
os.makedirs("data", exist_ok=True)

if os.path.exists(DATA_FILE):
    global_df = pd.read_csv(DATA_FILE, parse_dates=['timestamp'])
    if len(global_df) > MAX_HISTORY_ROWS:
        global_df = global_df.iloc[-MAX_HISTORY_ROWS:]
    print(f"Loaded {len(global_df)} rows from persistent storage.")
else:
    global_df = pd.DataFrame(columns=[
        'timestamp', 'price', 'basis', 'oi', 'taker_imbalance', 'whale_div'
    ])
df_lock = threading.Lock()

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

            return {
                'timestamp': timestamp, 'price': spot_price, 'basis': basis, 
                'oi': total_oi, 'taker_imbalance': taker_imbalance, 'whale_div': whale_div
            }
        except (TypeError, KeyError, IndexError):
            return None

async def clock_sync_daemon():
    global global_df
    while True:
        now = datetime.datetime.now()
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
# 3. UI COMPONENT FACTORIES
# ==========================================
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

def create_signal_card(title, is_active, active_color, description, thresholds):
    bg_color = f'rgba{active_color.replace("rgb", "").replace(")", ", 0.15)")}' if is_active else '#111'
    border_color = active_color if is_active else '#333'
    text_color = active_color if is_active else '#444'
    status_text = "🚨 TRIGGERED" if is_active else "INACTIVE"
    
    return html.Div(style={
        'backgroundColor': bg_color, 'padding': '20px', 'borderRadius': '8px',
        'border': f'2px solid {border_color}', 'flex': '1', 'minWidth': '250px', 'margin': '10px',
        'textAlign': 'center', 'transition': 'all 0.3s ease'
    }, children=[
        html.H2(title, style={'margin': '0 0 5px 0', 'color': text_color}),
        html.H3(status_text, style={'margin': '0 0 10px 0', 'color': text_color}),
        html.P(description, style={'margin': '0 0 8px 0', 'fontSize': '13px', 'color': '#888'}),
        html.P(thresholds, style={'margin': '0', 'fontSize': '11px', 'color': text_color, 'fontWeight': 'bold'})
    ])

# ==========================================
# 4. DASHBOARD SERVER (ANCHORED UI)
# ==========================================
app = dash.Dash(__name__)
app.title = "Order Flow Terminal"

# We now define the individual containers statically so the DOM never collapses
app.layout = html.Div(style={'backgroundColor': '#111', 'color': 'white', 'fontFamily': 'Arial, sans-serif', 'padding': '20px', 'minHeight': '100vh'}, children=[
    html.H1("QUANTITATIVE ORDER FLOW TERMINAL", style={'textAlign': 'center', 'letterSpacing': '2px'}),
    
    html.Div(style={'textAlign': 'center', 'marginBottom': '20px'}, children=[
        html.Label("Select Timeframe: ", style={'marginRight': '10px', 'fontWeight': 'bold'}),
        dcc.Dropdown(
            id='timeframe-dropdown',
            options=[
                {'label': '1 Minute', 'value': '1min'},
                {'label': '5 Minutes', 'value': '5min'},
                {'label': '15 Minutes', 'value': '15min'},
                {'label': '30 Minutes', 'value': '30min'}
            ],
            value='5min', clearable=False,
            style={'width': '150px', 'display': 'inline-block', 'color': 'black', 'textAlign': 'left'}
        )
    ]),

    # Isolated dynamic targets
    html.Div(id='signal-row'),
    html.Div(id='metrics-row'),
    html.Div(id='event-log-row'),
    
    # The Chart is now permanently anchored in the layout
    html.Div(
        dcc.Graph(id='main-chart', config={'displayModeBar': False}),
        style={'marginTop': '20px', 'border': '1px solid #333', 'borderRadius': '8px'}
    ),
    
    dcc.Interval(id='interval-component', interval=UI_REFRESH_INTERVAL, n_intervals=0)
])

# The callback now targets 4 distinct elements instead of 1 master container
@app.callback(
    [Output('signal-row', 'children'),
     Output('metrics-row', 'children'),
     Output('event-log-row', 'children'),
     Output('main-chart', 'figure')],
    [Input('interval-component', 'n_intervals'), Input('timeframe-dropdown', 'value')]
)
def update_dashboard(n, timeframe):
    with df_lock:
        if global_df.empty or len(global_df) < 2:
            loading = html.H3("GATHERING DATA PIPELINE...", style={'textAlign': 'center', 'color': 'grey'})
            return loading, "", "", go.Figure()
        df_ui = global_df.copy()

    # FORCE DATETIME CONVERSION: Prevents the silent resample TypeError crash
    df_ui['timestamp'] = pd.to_datetime(df_ui['timestamp'])
    df_ui.set_index('timestamp', inplace=True)
    
    if timeframe != '1min':
        # Use ffill() instead of dropna() so the active live candle is never vaporized
        df_ui = df_ui.resample(timeframe, closed='left', label='left').last().ffill()
    df_ui.reset_index(inplace=True)

    if len(df_ui) < 2:
        loading = html.H3("GATHERING TIMEFRAME DATA...", style={'textAlign': 'center', 'color': 'grey'})
        return loading, "", "", go.Figure()

    # --- VECTORIZED CALCULATIONS ---
    df_ui['price_change'] = df_ui['price'].pct_change() * 100
    df_ui['oi_change'] = df_ui['oi'].pct_change() * 100
    df_ui['whale_delta'] = df_ui['whale_div'].diff()
    df_ui['buy_pressure'] = np.where(df_ui['taker_imbalance'] > 0, 1 / df_ui['taker_imbalance'], 1)

    # --- AUTO-ADJUSTING QUANTITATIVE LOGIC (Robust fallback handling) ---
    price_mean = df_ui['price_change'].rolling(window=20, min_periods=1).mean().abs()
    price_std = df_ui['price_change'].rolling(window=20, min_periods=1).std().fillna(0)
    df_ui['price_thresh'] = price_mean + (price_std * 1.5)

    oi_mean = df_ui['oi_change'].rolling(window=20, min_periods=1).mean().abs()
    oi_std = df_ui['oi_change'].rolling(window=20, min_periods=1).std().fillna(0)
    df_ui['oi_thresh_upper'] = oi_mean + (oi_std * 1.5)
    df_ui['oi_thresh_lower'] = -df_ui['oi_thresh_upper']

    df_ui['premium_thresh'] = df_ui['basis'].rolling(window=100, min_periods=1).quantile(0.90)
    
    bp_mean = df_ui['buy_pressure'].rolling(window=100, min_periods=1).mean()
    bp_std = df_ui['buy_pressure'].rolling(window=100, min_periods=1).std().fillna(0)
    df_ui['bp_thresh'] = bp_mean + (bp_std * 1.5)

    df_ui['price_thresh'] = df_ui['price_thresh'].fillna(0.05)
    df_ui['oi_thresh_upper'] = df_ui['oi_thresh_upper'].fillna(0.10)
    df_ui['oi_thresh_lower'] = df_ui['oi_thresh_lower'].fillna(-0.15)
    df_ui['premium_thresh'] = df_ui['premium_thresh'].fillna(5.0)
    df_ui['bp_thresh'] = df_ui['bp_thresh'].fillna(1.2)

    # --- DYNAMIC SIGNAL MATCHER ---
    df_ui['is_breakout'] = (df_ui['price_change'] > df_ui['price_thresh']) & \
                           (df_ui['oi_change'] > df_ui['oi_thresh_upper']) & \
                           (df_ui['whale_delta'] < 0)

    df_ui['is_fakeout'] = (df_ui['price_change'] > df_ui['price_thresh']) & \
                          (df_ui['oi_change'] < df_ui['oi_thresh_lower'])

    df_ui['is_exhaustion'] = (df_ui['price_change'] > 0) & \
                             (df_ui['buy_pressure'] > df_ui['bp_thresh']) & \
                             (df_ui['basis'] > df_ui['premium_thresh']) & \
                             (df_ui['whale_delta'] > 0)

    current = df_ui.iloc[-1]
    
    # --- BUILD MODULAR UI ---
    signal_row = html.Div(style={'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'center'}, children=[
        create_signal_card("🟩 TRUE BREAKOUT", current['is_breakout'], "rgb(0, 255, 0)", "Trend has real fuel. Whales are absorbing.", f"Auto-Target: Price > +{current['price_thresh']:.2f}% | OI > +{current['oi_thresh_upper']:.2f}%"),
        create_signal_card("🟧 FAKEOUT SQUEEZE", current['is_fakeout'], "rgb(255, 165, 0)", "Shorts liquidated. No new buyers. Reversal likely.", f"Auto-Target: Price > +{current['price_thresh']:.2f}% | OI < {current['oi_thresh_lower']:.2f}%"),
        create_signal_card("🟥 EXHAUSTION WALL", current['is_exhaustion'], "rgb(255, 0, 0)", "Extreme retail greed met with Whale resistance.", f"Auto-Target: Buy Pressure > {current['bp_thresh']:.2f}x | Premium > ${current['premium_thresh']:.2f}")
    ])

    metrics_row = html.Div(style={'display': 'flex', 'flexWrap': 'wrap', 'justifyContent': 'center', 'marginTop': '10px'}, children=[
        create_metric_card("Price Change", f"{current['price_change']:+.2f}%", "The % move in Bitcoin price.", f"Dynamic Vol Threshold: > +{current['price_thresh']:.2f}%", "#00FF00" if current['price_change'] > 0 else "#FF0000"),
        create_metric_card("OI Velocity", f"{current['oi_change']:+.2f}%", "New money entering vs positions closing.", f"Dynamic Target: > +{current['oi_thresh_upper']:.2f}% or < {current['oi_thresh_lower']:.2f}%", "#00FF00" if current['oi_change'] > 0 else ("#FF0000" if current['oi_change'] < 0 else "white")),
        create_metric_card("Taker Buy Pressure", f"{current['buy_pressure']:.2f}x", "Market Buy vs Sell Volume.", f"Dynamic Noise Filter: > {current['bp_thresh']:.2f}x", "cyan"),
        create_metric_card("Basis Premium", f"${current['basis']:.2f}", "Futures Price minus Spot Price.", f"90th Percentile Limit: > ${current['premium_thresh']:.2f}", "orange" if current['basis'] > current['premium_thresh'] else "white")
    ])

    log_entries = []
    for idx, row in df_ui.iloc[::-1].iterrows():
        ts_str = row['timestamp'].strftime('%Y-%m-%d %H:%M')
        if row['is_breakout']: log_entries.append(html.Div(f"[{ts_str}] 🟩 TRUE BREAKOUT Detected @ ${row['price']:,.2f}", style={'color': 'lime', 'marginBottom': '5px'}))
        if row['is_fakeout']: log_entries.append(html.Div(f"[{ts_str}] 🟧 FAKEOUT SQUEEZE Detected @ ${row['price']:,.2f}", style={'color': 'orange', 'marginBottom': '5px'}))
        if row['is_exhaustion']: log_entries.append(html.Div(f"[{ts_str}] 🟥 EXHAUSTION WALL Detected @ ${row['price']:,.2f}", style={'color': 'red', 'marginBottom': '5px'}))
            
    if not log_entries:
        log_entries.append(html.Div("No signals triggered in the current memory window.", style={'color': '#555'}))

    event_log = html.Div(style={'backgroundColor': '#1a1a1a', 'border': '1px solid #333', 'borderRadius': '8px', 'padding': '15px', 'height': '150px', 'overflowY': 'auto', 'marginTop': '20px', 'fontFamily': 'monospace'}, children=[
        html.H3("SIGNAL EVENT LOG", style={'margin': '0 0 10px 0', 'fontSize': '14px', 'color': '#aaa'}),
        html.Div(children=log_entries)
    ])

    # Multi-Row Subplot Chart
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.55, 0.15, 0.15, 0.15], specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": False}]])
    
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['price'], name="BTC Price", line=dict(color='white', width=2)), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['oi'], name="Open Interest", line=dict(color='cyan', width=2, dash='dot')), row=1, col=1, secondary_y=True)
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['buy_pressure'], name="Buy Pressure", line=dict(color='magenta', width=1)), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['premium_thresh'], name="90th Percentile Limit", line=dict(color='rgba(255,165,0,0.3)', width=1, dash='dash')), row=4, col=1)
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['basis'], name="Basis Premium", line=dict(color='orange', width=1), fill='tonexty'), row=4, col=1)
    fig.add_trace(go.Scatter(x=df_ui['timestamp'], y=df_ui['whale_div'], name="Whale Ratio", line=dict(color='yellow', width=1)), row=3, col=1)

    fig.update_layout(
        template="plotly_dark", plot_bgcolor='#111', paper_bgcolor='#111', margin=dict(l=40, r=40, t=20, b=20),
        hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=700,
        uirevision=timeframe 
    )
    
    # Force X-axis to always stretch forward automatically to keep view current
    fig.update_xaxes(showgrid=True, gridcolor='#222', autorange=True)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1, secondary_y=False, showgrid=False)
    fig.update_yaxes(title_text="OI (BTC)", row=1, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="Taker Buy (x)", row=2, col=1, showgrid=True, gridcolor='#222')
    fig.update_yaxes(title_text="Whale Ratio", row=3, col=1, showgrid=True, gridcolor='#222')
    fig.update_yaxes(title_text="Basis ($)", row=4, col=1, showgrid=True, gridcolor='#222')

    return signal_row, metrics_row, event_log, fig

# ==========================================
# 5. EXECUTION
# ==========================================
if __name__ == '__main__':
    start_background_thread()
    app.run(host='0.0.0.0', port=8050, debug=False)

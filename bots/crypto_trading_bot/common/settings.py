from pathlib import Path
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import os

load_dotenv()

IST = ZoneInfo('Asia/Kolkata')

BASE_DIR            = Path(__file__).resolve().parent.parent   # ← was dropped by edit
DATA_DIR            = BASE_DIR / 'data'
TRADES_DIR          = DATA_DIR / 'trades'        # kept for backward compat
DAILY_PNL_DIR       = DATA_DIR / 'daily_pnl'    # kept for backward compat
PORTFOLIO_DIR       = DATA_DIR / 'portfolio'     # unified daily CSV
STATE_SNAPSHOTS_DIR = DATA_DIR / 'state_snapshots'
CONFIG_DIR          = BASE_DIR / 'config'
KEYS_FILE           = CONFIG_DIR / 'exchange_keys.enc'

for _d in (TRADES_DIR, DAILY_PNL_DIR, PORTFOLIO_DIR, STATE_SNAPSHOTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB   = 0

TICK_TTL   = 60
DEPTH_TTL  = 30
CANDLE_TTL = 300

UI_HOST = '0.0.0.0'
UI_PORT = int(os.getenv('UI_PORT', 9100))

DEFAULT_RISK_PCT    = 0.5
DEFAULT_LEVERAGE    = 1
DEFAULT_MARGIN_MODE = 'cross'

CANDLE_INTERVALS        = ['1m', '5m', '15m', '1h', '4h', '1d']
CHART_INTERVALS         = ['1m', '5m', '15m', '1h']
DEFAULT_ORDERBOOK_DEPTH = 20

MARKET_DATA_QUEUE_SIZE = 10   # was 100 — only ticker subscribed now
COMMAND_QUEUE_SIZE     = 50

PNL_CHART_POINTS   = 1440
PNL_CHART_INTERVAL = 60

# binance_futures first so it is the default selection
SUPPORTED_EXCHANGES = ['binance_futures', 'binance', 'delta']

EXCHANGE_FEES = {
    'binance':         {'maker': 0.001,  'taker': 0.001},
    'binance_futures': {'maker': 0.0002, 'taker': 0.0004},
    'delta':           {'maker': 0.0002, 'taker': 0.0005},
}

ENGINE_STATE_PUBLISH_INTERVAL = 1.0
UI_REFRESH_INTERVAL           = 1.0
WS_RECONNECT_DELAY            = 5
WS_MAX_RECONNECT_ATTEMPTS     = 0

PAPER_FILL_CHECK_INTERVAL = 0.5

LOG_MAX_ENTRIES              = 200
ALERT_SOUND_DURATION_DEFAULT = 5

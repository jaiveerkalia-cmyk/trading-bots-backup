from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR            = Path(__file__).parent.parent
DATA_DIR            = BASE_DIR / 'data'
TRADES_DIR          = DATA_DIR / 'trades'
DAILY_PNL_DIR       = DATA_DIR / 'daily_pnl'
STATE_SNAPSHOTS_DIR = DATA_DIR / 'state_snapshots'
CONFIG_DIR          = BASE_DIR / 'config'
KEYS_FILE           = CONFIG_DIR / 'exchange_keys.enc'

for _d in (TRADES_DIR, DAILY_PNL_DIR, STATE_SNAPSHOTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Redis ──────────────────────────────────────────────────────────────────────
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB   = 0

# ── Redis TTLs (seconds) — prevents stale market data accumulating ─────────────
TICK_TTL         = 60     # latest tick key expires after 60s of no updates
DEPTH_TTL        = 30     # order book snapshot expires after 30s
CANDLE_TTL       = 300    # latest candle key expires after 5 min
# Engine state keys have no TTL — managed explicitly by trading_engine

# ── UI ─────────────────────────────────────────────────────────────────────────
UI_HOST = '0.0.0.0'
UI_PORT = int(os.getenv('UI_PORT', 9100))

# ── Trading defaults ───────────────────────────────────────────────────────────
DEFAULT_RISK_PCT    = 0.5
DEFAULT_LEVERAGE    = 1
DEFAULT_MARGIN_MODE = 'cross'

# ── Market data ────────────────────────────────────────────────────────────────
CANDLE_INTERVALS        = ['1m', '5m', '15m', '1h', '4h', '1d']
DEFAULT_ORDERBOOK_DEPTH = 20     # levels per side — hard cap in both book and ccxt

# ── Queues — bounded to prevent slow consumers accumulating memory ─────────────
MARKET_DATA_QUEUE_SIZE = 100   # per-symbol publish queue in market_data_service
COMMAND_QUEUE_SIZE     = 50    # trading engine command queue

# ── PnL chart — rolling window kept as deque(maxlen=N) in UI ──────────────────
PNL_CHART_POINTS = 300        # ~5 min of 1s samples

# ── Supported exchanges ────────────────────────────────────────────────────────
SUPPORTED_EXCHANGES = ['binance', 'delta']

# ── Timing ─────────────────────────────────────────────────────────────────────
ENGINE_STATE_PUBLISH_INTERVAL = 1.0
UI_REFRESH_INTERVAL           = 1.0
WS_RECONNECT_DELAY            = 5
WS_MAX_RECONNECT_ATTEMPTS     = 0   # infinite

# ── Activity log ───────────────────────────────────────────────────────────────
LOG_MAX_ENTRIES = 200    # Redis LIST capped at this length via LTRIM on every write

CHART_INTERVALS = ['1m', '5m', '15m', '1h']


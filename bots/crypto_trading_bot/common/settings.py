from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

# ── Paths (all relative to crypto_trading_bot/) ────────────────────────────────
BASE_DIR            = Path(__file__).parent.parent
DATA_DIR            = BASE_DIR / 'data'
TRADES_DIR          = DATA_DIR / 'trades'
DAILY_PNL_DIR       = DATA_DIR / 'daily_pnl'
STATE_SNAPSHOTS_DIR = DATA_DIR / 'state_snapshots'
CONFIG_DIR          = BASE_DIR / 'config'
KEYS_FILE           = CONFIG_DIR / 'exchange_keys.enc'

# Ensure data dirs exist at import time
for _d in (TRADES_DIR, DAILY_PNL_DIR, STATE_SNAPSHOTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Redis ──────────────────────────────────────────────────────────────────────
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB   = 0

# ── UI ─────────────────────────────────────────────────────────────────────────
UI_HOST = '0.0.0.0'
UI_PORT = int(os.getenv('UI_PORT', 9100))

# ── Trading defaults ───────────────────────────────────────────────────────────
DEFAULT_RISK_PCT    = 0.5
DEFAULT_LEVERAGE    = 1
DEFAULT_MARGIN_MODE = 'cross'

# ── Market data ────────────────────────────────────────────────────────────────
CANDLE_INTERVALS        = ['1m', '5m', '15m', '1h', '4h', '1d']
DEFAULT_ORDERBOOK_DEPTH = 20    # levels per side, used for paper fill simulation

# ── Supported exchanges (Phase 1) ──────────────────────────────────────────────
SUPPORTED_EXCHANGES = ['binance', 'delta']

# ── Timing ─────────────────────────────────────────────────────────────────────
ENGINE_STATE_PUBLISH_INTERVAL = 1.0   # seconds — engine pushes state to Redis
UI_REFRESH_INTERVAL           = 1.0   # seconds — UI polls Redis
WS_RECONNECT_DELAY            = 5     # seconds before reconnect attempt
WS_MAX_RECONNECT_ATTEMPTS     = 0     # 0 = infinite (24/7 bot)

# ── Activity log ───────────────────────────────────────────────────────────────
LOG_MAX_ENTRIES = 200   # max entries kept in Redis list

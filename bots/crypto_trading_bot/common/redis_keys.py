"""
Single source of truth for all Redis key/channel names.
Both market_data_service and trading_engine publish here.
UI and trading_engine read from here.
"""

# ── Market data channels (pub/sub) ─────────────────────────────────────────────

def tick_channel(exchange: str, symbol: str) -> str:
    """e.g. tick:binance:BTC/USDT"""
    return f"tick:{exchange}:{symbol}"

def depth_channel(exchange: str, symbol: str) -> str:
    """e.g. depth:binance:BTC/USDT"""
    return f"depth:{exchange}:{symbol}"

def candle_channel(exchange: str, symbol: str, interval: str) -> str:
    """e.g. candle:binance:BTC/USDT:1m"""
    return f"candle:{exchange}:{symbol}:{interval}"


# ── Latest-value cache (GET/SET) ───────────────────────────────────────────────
# Stored alongside pub/sub so UI can read current state on connect
# without waiting for the next tick.

def latest_tick_key(exchange: str, symbol: str) -> str:
    return f"latest:tick:{exchange}:{symbol}"

def latest_depth_key(exchange: str, symbol: str) -> str:
    return f"latest:depth:{exchange}:{symbol}"

def latest_candle_key(exchange: str, symbol: str, interval: str) -> str:
    return f"latest:candle:{exchange}:{symbol}:{interval}"


# ── Engine state keys (SET by trading_engine, read by UI) ──────────────────────

SLOTS_KEY              = "engine:slots"           # JSON list[TradeSlot]
POSITIONS_KEY          = "engine:positions"        # JSON list[Position]
OPEN_ORDERS_KEY        = "engine:open_orders"      # JSON list[Order] — working only
ORDER_HISTORY_KEY      = "engine:order_history"    # JSON list[Order] — filled/cancelled
ALERTS_KEY             = "engine:alerts"           # JSON list[Alert]
PNL_KEY                = "engine:pnl"              # JSON PnLSummary
LIVE_MODE_KEY          = "engine:live_mode"        # '1' = live, '0' = paper
CONNECTED_EXCHANGES_KEY = "engine:connected"       # JSON list[str]
LOG_KEY                = "engine:log"              # Redis LIST, capped at LOG_MAX_ENTRIES


# ── Command queue (UI -> trading_engine) ───────────────────────────────────────

COMMAND_QUEUE = "commands:trading_engine"

# Command type constants — used as 'type' field in command dicts
CMD_OPEN_SLOT      = "open_slot"
CMD_CLOSE_SLOT     = "close_slot"
CMD_UPDATE_SLOT    = "update_slot"
CMD_CANCEL_ORDER   = "cancel_order"
CMD_SET_ALERT      = "set_alert"
CMD_DELETE_ALERT   = "delete_alert"
CMD_CLOSE_ALL      = "close_all"
CMD_SET_LIVE_MODE  = "set_live_mode"

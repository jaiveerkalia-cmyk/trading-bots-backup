# ── Market data channels (pub/sub) ─────────────────────────────────────────────

def tick_channel(exchange: str, symbol: str) -> str:
    return f"tick:{exchange}:{symbol}"

def depth_channel(exchange: str, symbol: str) -> str:
    return f"depth:{exchange}:{symbol}"

def candle_channel(exchange: str, symbol: str, interval: str) -> str:
    return f"candle:{exchange}:{symbol}:{interval}"


# ── Latest-value cache (GET/SET) ───────────────────────────────────────────────

def latest_tick_key(exchange: str, symbol: str) -> str:
    """Last trade price tick."""
    return f"latest:tick:{exchange}:{symbol}"

def latest_depth_key(exchange: str, symbol: str) -> str:
    return f"latest:depth:{exchange}:{symbol}"

def latest_candle_key(exchange: str, symbol: str, interval: str) -> str:
    return f"latest:candle:{exchange}:{symbol}:{interval}"

def mark_price_key(exchange: str, symbol: str) -> str:
    """Futures mark price — separate from last trade price."""
    return f"mark:{exchange}:{symbol}"


# ── Engine state keys ──────────────────────────────────────────────────────────

SLOTS_KEY               = "engine:slots"
POSITIONS_KEY           = "engine:positions"
OPEN_ORDERS_KEY         = "engine:open_orders"
ORDER_HISTORY_KEY       = "engine:order_history"
ALERTS_KEY              = "engine:alerts"
PNL_KEY                 = "engine:pnl"
LIVE_MODE_KEY           = "engine:live_mode"
CONNECTED_EXCHANGES_KEY = "engine:connected"
LOG_KEY                 = "engine:log"


# ── Command queue ──────────────────────────────────────────────────────────────

COMMAND_QUEUE = "commands:trading_engine"

CMD_OPEN_SLOT      = "open_slot"
CMD_CLOSE_SLOT     = "close_slot"
CMD_UPDATE_SLOT    = "update_slot"
CMD_CANCEL_ORDER   = "cancel_order"
CMD_MODIFY_ORDER   = "modify_order"
CMD_SET_ALERT      = "set_alert"
CMD_DELETE_ALERT   = "delete_alert"
CMD_CLOSE_ALL      = "close_all"
CMD_SET_LIVE_MODE  = "set_live_mode"
CMD_PARTIAL_CLOSE_SLOT = 'partial_close_slot'


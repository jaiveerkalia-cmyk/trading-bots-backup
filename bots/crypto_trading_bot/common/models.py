from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Literal
from datetime import datetime
import uuid


def _uid() -> str:
    return str(uuid.uuid4())


# ── Market data ────────────────────────────────────────────────────────────────

class Tick(BaseModel):
    model_config = ConfigDict(slots=True)
    exchange:     str
    symbol:       str
    price:        float           # last trade price (chart price)
    volume:       float
    timestamp:    datetime
    mark_price:   Optional[float] = None   # futures only — used for fills/PnL
    funding_rate: Optional[float] = None   # futures perpetual funding rate


class Candle(BaseModel):
    model_config = ConfigDict(slots=True)
    exchange:  str
    symbol:    str
    interval:  str
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    timestamp: datetime


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(slots=True)
    price: float
    qty:   float


class OrderBook(BaseModel):
    model_config = ConfigDict(slots=True)
    exchange:  str
    symbol:    str
    bids:      List[OrderBookLevel]
    asks:      List[OrderBookLevel]
    timestamp: datetime


# ── Orders ─────────────────────────────────────────────────────────────────────

class Order(BaseModel):
    model_config = ConfigDict(slots=True)
    id:                str  = Field(default_factory=_uid)
    exchange_order_id: Optional[str]   = None
    exchange:          str
    symbol:            str
    side:              Literal['buy', 'sell']
    order_type:        Literal['market', 'limit', 'stop_limit']
    price:             Optional[float] = None
    stop_price:        Optional[float] = None
    qty:               float
    qty_mode:          Literal['base', 'quote'] = 'base'
    status:            Literal['pending', 'working', 'filled', 'cancelled', 'rejected'] = 'pending'
    filled_qty:        float           = 0.0
    avg_fill_price:    Optional[float] = None
    is_paper:          bool            = False
    slot_id:           Optional[str]   = None
    created_at:        datetime        = Field(default_factory=datetime.utcnow)
    updated_at:        datetime        = Field(default_factory=datetime.utcnow)


# ── Positions ──────────────────────────────────────────────────────────────────

class Position(BaseModel):
    model_config = ConfigDict(slots=True)
    exchange:          str
    symbol:            str
    side:              Literal['long', 'short']
    entry_price:       float
    current_price:     float
    qty:               float
    leverage:          int             = 1
    margin_mode:       Literal['cross', 'isolated'] = 'cross'
    unrealized_pnl:    float           = 0.0
    realized_pnl:      float           = 0.0
    liquidation_price: Optional[float] = None
    funding_rate:      Optional[float] = None
    is_paper:          bool            = False
    slot_id:           Optional[str]   = None
    opened_at:         datetime        = Field(default_factory=datetime.utcnow)


# ── Trade slots ────────────────────────────────────────────────────────────────

class EntryLeg(BaseModel):
    model_config = ConfigDict(slots=True)
    price:      float
    qty:        float
    order_type: Literal['market', 'limit', 'stop_limit'] = 'limit'
    order_id:   Optional[str] = None
    filled:     bool          = False


class TradeSlot(BaseModel):
    model_config = ConfigDict(slots=True)
    id:               str  = Field(default_factory=_uid)
    exchange:         str
    symbol:           str
    side:             Literal['long', 'short']
    instrument_type:  Literal['spot', 'futures'] = 'spot'
    entries:          List[EntryLeg]  = Field(default_factory=list)
    stop_price:       Optional[float] = None
    target_price:     Optional[float] = None
    leverage:         int             = 1
    margin_mode:      Literal['cross', 'isolated'] = 'cross'
    qty_mode:         Literal['base', 'quote', 'risk'] = 'risk'
    risk_pct:         float           = 0.5
    status:           Literal['pending', 'active', 'closed', 'stopped'] = 'pending'
    position:         Optional[Position] = None
    orders:           List[Order]     = Field(default_factory=list)
    sl_order_id:      Optional[str]   = None
    target_order_id:  Optional[str]   = None
    is_paper:         bool            = False
    created_at:       datetime        = Field(default_factory=datetime.utcnow)
    closed_at:        Optional[datetime] = None
    realized_pnl:     float           = 0.0


# ── Alerts ─────────────────────────────────────────────────────────────────────

class Alert(BaseModel):
    model_config = ConfigDict(slots=True)
    id:         str  = Field(default_factory=_uid)
    exchange:   str
    symbol:     str
    upper:      Optional[float] = None
    lower:      Optional[float] = None
    period:     Literal['current', '1m', '5m'] = 'current'
    triggered:  bool            = False
    created_at: datetime        = Field(default_factory=datetime.utcnow)


# ── Activity log ───────────────────────────────────────────────────────────────

class LogEntry(BaseModel):
    model_config = ConfigDict(slots=True)
    timestamp:  datetime = Field(default_factory=datetime.utcnow)
    level:      Literal['info', 'warning', 'error', 'success'] = 'info'
    message:    str
    exchange:   Optional[str] = None
    symbol:     Optional[str] = None


class PnLSummary(BaseModel):
    model_config = ConfigDict(slots=True)
    unrealized: float = 0.0
    realized:   float = 0.0

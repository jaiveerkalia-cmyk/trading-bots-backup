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
    price:        float           # last trade price
    volume:       float
    timestamp:    datetime
    mark_price:   Optional[float] = None
    funding_rate: Optional[float] = None


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
    reference_price:   Optional[float] = None   # paper fallback for market orders
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
    entry_fee_paid:    float           = 0.0   # actual fee paid on entry
    is_paper:          bool            = False
    slot_id:           Optional[str]   = None
    opened_at:         datetime        = Field(default_factory=datetime.utcnow)
    funding_pnl:  float = 0.0   # accumulated funding charges (negative = paid out)

# ── Trade slots ────────────────────────────────────────────────────────────────

class EntryLeg(BaseModel):
    model_config = ConfigDict(slots=True)
    price:           float
    qty:             float
    order_type:      Literal['market', 'limit', 'stop_limit'] = 'limit'
    order_id:        Optional[str]   = None
    filled:          bool            = False
    reference_price: Optional[float] = None   # UI mark/last price at time of submission


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
    status:           Literal['pending', 'working', 'active', 'closed', 'stopped', 'conditional'] = 'pending'
    position:         Optional[Position] = None
    orders:           List[Order]     = Field(default_factory=list)
    sl_order_id:      Optional[str]   = None
    target_order_id:  Optional[str]   = None
    is_paper:         bool            = False
    created_at:       datetime        = Field(default_factory=datetime.utcnow)
    closed_at:        Optional[datetime] = None
    realized_pnl:     float           = 0.0
    pnl_target:       Optional[float] = None   # close when unrealized PnL reaches this value
    fire_on:          Optional[Literal['1m', '5m']] = None   # candle-close conditional entry

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
    # Optional: when set, this alert also places an entry order when triggered
    order_side:        Optional[Literal['long', 'short']]                 = None
    order_type:        Optional[Literal['market', 'limit', 'stop_limit']] = None
    order_entry_price: Optional[float] = None   # 0/None → use LTP at fire time
    order_stop:        Optional[float] = None
    order_target:      Optional[float] = None
    order_qty:         Optional[float] = None   # 0/None → auto from risk %
    order_risk_pct:    Optional[float] = None


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

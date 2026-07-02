"""
Binance USDT-margined Perpetual Futures adapter.
RAM optimisations:
  - Strip raw 'info' field from markets after load_markets() (saves ~30-60 MB).
  - Clear ccxt ticker / ohlcv caches after every publish so they never accumulate.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Optional, Callable, Awaitable
from datetime import datetime, timezone

import ccxt.pro as ccxtpro

from exchanges.base import BaseExchangeAdapter
from common.models import Tick, Candle, OrderBook, OrderBookLevel, Order, Position
from common import settings

logger = logging.getLogger(__name__)


def _trim_markets(ex) -> None:
    """Remove raw exchange 'info' blob from every market entry to free RAM."""
    try:
        for m in (ex.markets or {}).values():
            m.pop('info', None)
        for m in (ex.markets_by_id or {}).values():
            m.pop('info', None)
    except Exception:
        pass


class BinanceFuturesAdapter(BaseExchangeAdapter):

    supports_stop_limit   = True
    supports_futures      = True
    supports_leverage     = True
    supports_ws_ticker    = True
    supports_ws_orderbook = True
    supports_ws_candles   = True

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        super().__init__(api_key, api_secret, testnet)
        self.name = 'binance_futures'
        self._ex: Optional[ccxtpro.binance] = None
        self._ws_tasks: dict[str, list[asyncio.Task]] = {}

    async def connect(self) -> None:
        self._ex = ccxtpro.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'options': {
                'defaultType': 'future',
                'newUpdates':  True,     # return only new candles — prevents history accumulation
            },
        })
        if self.testnet:
            self._ex.set_sandbox_mode(True)
        await self._ex.load_markets()
        _trim_markets(self._ex)          # ← strip heavy metadata immediately after load
        logger.info("Binance Futures connected — perpetual markets loaded")

    async def disconnect(self) -> None:
        for tasks in self._ws_tasks.values():
            for t in tasks:
                t.cancel()
        self._ws_tasks.clear()
        if self._ex:
            await self._ex.close()
        logger.info("Binance Futures disconnected")

    # ── WS subscriptions ──────────────────────────────────────────────────────

    async def subscribe_ticker(
        self, symbol: str, callback: Callable[[Tick], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._ticker_loop(symbol, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _ticker_loop(self, symbol: str, cb: Callable) -> None:
        while True:
            try:
                raw        = await self._ex.watch_ticker(symbol)
                info       = raw.get('info', {})
                last_price = float(raw.get('last') or raw.get('close') or 0)
                mark_price = float(
                    info.get('markPrice') or raw.get('markPrice') or last_price
                )
                funding    = float(info.get('lastFundingRate') or 0) or None
                tick = Tick(
                    exchange     = self.name,
                    symbol       = symbol,
                    price        = last_price,
                    mark_price   = mark_price,
                    funding_rate = funding,
                    volume       = float(raw.get('baseVolume') or 0),
                    timestamp    = (
                        datetime.fromtimestamp(raw['timestamp'] / 1000, tz=timezone.utc)
                        if raw.get('timestamp') else datetime.now(timezone.utc)
                    ),
                )
                await cb(tick)
                # ── Clear ccxt ticker cache to prevent RAM growth ──────────
                try:
                    self._ex.tickers.pop(symbol, None)
                except Exception:
                    pass
                del tick, raw, info
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BinanceFutures ticker [%s]: %s", symbol, e)
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[OrderBook], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._orderbook_loop(symbol, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _orderbook_loop(self, symbol: str, cb: Callable) -> None:
        depth = settings.DEFAULT_ORDERBOOK_DEPTH
        while True:
            try:
                raw  = await self._ex.watch_order_book(symbol, limit=depth)
                book = OrderBook(
                    exchange = self.name, symbol = symbol,
                    bids = [OrderBookLevel(price=float(p), qty=float(q))
                            for p, q in raw['bids'][:depth]],
                    asks = [OrderBookLevel(price=float(p), qty=float(q))
                            for p, q in raw['asks'][:depth]],
                    timestamp = (
                        datetime.fromtimestamp(raw['timestamp'] / 1000, tz=timezone.utc)
                        if raw.get('timestamp') else datetime.now(timezone.utc)
                    ),
                )
                await cb(book)
                # ── Evict orderbook from ccxt cache ────────────────────────
                try:
                    self._ex.orderbooks.pop(symbol, None)
                except Exception:
                    pass
                del book, raw
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BinanceFutures orderbook [%s]: %s", symbol, e)
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def subscribe_candles(
        self, symbol: str, interval: str, callback: Callable[[Candle], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._candles_loop(symbol, interval, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _candles_loop(self, symbol: str, interval: str, cb: Callable) -> None:
        while True:
            try:
                ohlcvs = await self._ex.watch_ohlcv(symbol, interval)
                for ts, o, h, l, c, v in ohlcvs:
                    await cb(Candle(
                        exchange  = self.name, symbol = symbol, interval = interval,
                        open=float(o), high=float(h), low=float(l), close=float(c),
                        volume    = float(v),
                        timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    ))
                # ── Trim OHLCV cache — keep only last 2 candles ────────────
                try:
                    cache = self._ex.ohlcvs.get(symbol, {}).get(interval)
                    if cache and len(cache) > 2:
                        self._ex.ohlcvs[symbol][interval] = cache[-2:]
                except Exception:
                    pass
                del ohlcvs
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BinanceFutures candles [%s/%s]: %s", symbol, interval, e)
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def unsubscribe(self, symbol: str) -> None:
        for t in self._ws_tasks.pop(symbol, []):
            t.cancel()

    # ── REST ──────────────────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        ccxt_type = {
            'market': 'market', 'limit': 'limit', 'stop_limit': 'STOP_MARKET',
        }.get(order.order_type, 'market')
        params: dict = {'reduceOnly': False}
        if order.order_type == 'stop_limit' and order.stop_price:
            params['stopPrice'] = order.stop_price
        try:
            r = await self._ex.create_order(
                symbol=order.symbol, type=ccxt_type, side=order.side,
                amount=order.qty,
                price=order.price if order.order_type == 'limit' else None,
                params=params,
            )
            order.exchange_order_id = str(r['id'])
            order.status            = self._map_status(r.get('status', ''))
            order.filled_qty        = float(r.get('filled') or 0)
            order.avg_fill_price    = float(r.get('average') or 0) or None
        except Exception as e:
            order.status = 'rejected'
            logger.error("BinanceFutures place_order error: %s", e)
        return order

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        try:
            await self._ex.cancel_order(exchange_order_id, symbol)
            return True
        except Exception as e:
            logger.error("BinanceFutures cancel_order error: %s", e)
            return False

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        try:
            return [self._parse_order(o)
                    for o in await self._ex.fetch_open_orders(symbol)]
        except Exception as e:
            logger.error("BinanceFutures get_open_orders error: %s", e)
            return []

    async def get_order(self, exchange_order_id: str, symbol: str) -> Order:
        return self._parse_order(
            await self._ex.fetch_order(exchange_order_id, symbol)
        )

    async def get_positions(self) -> list[Position]:
        try:
            return [
                Position(
                    exchange=self.name, symbol=p['symbol'],
                    side='long' if p['side'] == 'long' else 'short',
                    entry_price=float(p.get('entryPrice') or 0),
                    current_price=float(p.get('markPrice') or 0),
                    qty=float(p.get('contracts') or 0),
                    leverage=int(p.get('leverage') or 1),
                    margin_mode='isolated' if p.get('marginMode') == 'isolated' else 'cross',
                    unrealized_pnl=float(p.get('unrealizedPnl') or 0),
                    liquidation_price=float(p.get('liquidationPrice') or 0) or None,
                    funding_rate=float(p.get('fundingRate') or 0) or None,
                )
                for p in await self._ex.fetch_positions()
                if float(p.get('contracts') or 0)
            ]
        except Exception as e:
            logger.error("BinanceFutures get_positions error: %s", e)
            return []

    async def get_balance(self) -> dict[str, float]:
        try:
            bal = await self._ex.fetch_balance()
            return {k: float(v['free']) for k, v in bal.items()
                    if isinstance(v, dict) and v.get('free')}
        except Exception as e:
            logger.error("BinanceFutures get_balance error: %s", e)
            return {}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._ex.set_leverage(leverage, symbol)
            return True
        except Exception as e:
            logger.error("BinanceFutures set_leverage error: %s", e)
            return False

    async def set_margin_mode(self, symbol: str, mode: str) -> bool:
        try:
            await self._ex.set_margin_mode(mode.upper(), symbol)
            return True
        except Exception as e:
            logger.error("BinanceFutures set_margin_mode error: %s", e)
            return False

    def normalize_symbol(self, raw: str) -> str:  return raw
    def to_exchange_symbol(self, canonical: str) -> str: return canonical

    async def get_tradable_symbols(self) -> list[str]:
        try:
            markets = await self._ex.load_markets(reload=True)
            return [s for s, m in markets.items()
                    if m.get('active') and m.get('type') in ('swap', 'future')]
        except Exception as e:
            logger.error("BinanceFutures get_tradable_symbols error: %s", e)
            return []

    async def fetch_ohlcv(
        self, symbol: str, interval: str, limit: int = 3
    ) -> list[list]:
        """Fetch historical OHLCV via Binance Futures REST (ccxt).
        Returns [[ts_ms, o, h, l, c, v], ...] sorted oldest-first.
        """
        try:
            return await self._ex.fetch_ohlcv(symbol, interval, limit=limit)
        except Exception as e:
            logger.error("BinanceFutures fetch_ohlcv [%s %s]: %s", symbol, interval, e)
            return []

    def _parse_order(self, r: dict) -> Order:
        return Order(
            exchange_order_id=str(r.get('id', '')),
            exchange=self.name, symbol=r.get('symbol', ''),
            side=r.get('side', 'buy'),
            order_type={'market': 'market', 'limit': 'limit'}.get(
                str(r.get('type', '')).lower(), 'market'),
            price=float(r.get('price') or 0) or None,
            qty=float(r.get('amount') or 0),
            status=self._map_status(r.get('status', '')),
            filled_qty=float(r.get('filled') or 0),
            avg_fill_price=float(r.get('average') or 0) or None,
        )

    @staticmethod
    def _map_status(s: str) -> str:
        return {'open': 'working', 'closed': 'filled', 'canceled': 'cancelled'}.get(
            s, 'pending'
        )

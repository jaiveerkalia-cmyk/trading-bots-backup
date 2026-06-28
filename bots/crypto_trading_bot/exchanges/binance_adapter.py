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
    """Drop the heavy 'info' raw dictionary from markets to save memory."""
    if not getattr(ex, 'markets', None):
        return
    for m in ex.markets.values():
        m.pop('info', None)


class BinanceAdapter(BaseExchangeAdapter):

    supports_stop_limit   = True
    supports_futures      = True
    supports_leverage     = True
    supports_ws_ticker    = True
    supports_ws_orderbook = True
    supports_ws_candles   = True

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        super().__init__(api_key, api_secret, testnet)
        self.name      = 'binance'
        self._spot:    Optional[ccxtpro.binance] = None
        self._futures: Optional[ccxtpro.binance] = None
        self._ws_tasks: dict[str, list[asyncio.Task]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        base_opts = {
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'options': {
                # Only return newly closed candles on each watch_ohlcv call —
                # prevents ccxt.pro accumulating a full candle history list in RAM
                'newUpdates': True,
            },
        }
        self._spot = ccxtpro.binance({
            **base_opts,
            'options': {**base_opts['options'], 'defaultType': 'spot'},
        })
        self._futures = ccxtpro.binance({
            **base_opts,
            'options': {**base_opts['options'], 'defaultType': 'future'},
        })

        if self.testnet:
            self._spot.set_sandbox_mode(True)
            self._futures.set_sandbox_mode(True)

        await asyncio.gather(
            self._spot.load_markets(),
            self._futures.load_markets(),
        )
        
        _trim_markets(self._spot)
        _trim_markets(self._futures)
        
        logger.info("Binance connected — spot + futures markets loaded")

    async def disconnect(self) -> None:
        for tasks in self._ws_tasks.values():
            for t in tasks:
                t.cancel()
        self._ws_tasks.clear()
        if self._spot:
            await self._spot.close()
        if self._futures:
            await self._futures.close()
        logger.info("Binance disconnected")

    # ── WS subscriptions ──────────────────────────────────────────────────────

    async def subscribe_ticker(
        self, symbol: str, callback: Callable[[Tick], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._ticker_loop(symbol, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _ticker_loop(self, symbol: str, cb: Callable) -> None:
        ex = self._get_ex(symbol)
        while True:
            try:
                raw = await ex.watch_ticker(symbol)
                tick = Tick(
                    exchange=self.name,
                    symbol=symbol,
                    price=float(raw.get('last') or raw.get('close') or 0),
                    volume=float(raw.get('baseVolume') or 0),
                    timestamp=datetime.fromtimestamp(
                        raw['timestamp'] / 1000, tz=timezone.utc
                    ) if raw.get('timestamp') else datetime.now(timezone.utc),
                )
                await cb(tick)
                del tick, raw   # explicit drop — helps GC in tight loops
                
                try:
                    ex.tickers.pop(symbol, None)
                except Exception:
                    pass
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance ticker loop [{symbol}]: {e}")
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[OrderBook], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._orderbook_loop(symbol, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _orderbook_loop(self, symbol: str, cb: Callable) -> None:
        """
        Pass limit=DEFAULT_ORDERBOOK_DEPTH to watch_order_book so ccxt.pro's
        internal book cache is bounded to N levels — not thousands.
        We read directly from the returned object without making a second copy.
        """
        ex    = self._get_ex(symbol)
        depth = settings.DEFAULT_ORDERBOOK_DEPTH
        while True:
            try:
                raw  = await ex.watch_order_book(symbol, limit=depth)
                book = OrderBook(
                    exchange=self.name,
                    symbol=symbol,
                    bids=[OrderBookLevel(price=float(p), qty=float(q))
                          for p, q in raw['bids'][:depth]],
                    asks=[OrderBookLevel(price=float(p), qty=float(q))
                          for p, q in raw['asks'][:depth]],
                    timestamp=datetime.fromtimestamp(
                        raw['timestamp'] / 1000, tz=timezone.utc
                    ) if raw.get('timestamp') else datetime.now(timezone.utc),
                )
                await cb(book)
                del book        # drop immediately after publish
                
                try:
                    ex.orderbooks.pop(symbol, None)
                except Exception:
                    pass
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance orderbook loop [{symbol}]: {e}")
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def subscribe_candles(
        self, symbol: str, interval: str, callback: Callable[[Candle], Awaitable[None]]
    ) -> None:
        t = asyncio.create_task(self._candles_loop(symbol, interval, callback))
        self._ws_tasks.setdefault(symbol, []).append(t)

    async def _candles_loop(self, symbol: str, interval: str, cb: Callable) -> None:
        """
        newUpdates=True (set at client level) means ccxt.pro returns only
        newly closed candles here — no growing history list in RAM.
        """
        ex = self._get_ex(symbol)
        while True:
            try:
                ohlcvs = await ex.watch_ohlcv(symbol, interval)
                for ts, o, h, l, c, v in ohlcvs:
                    await cb(Candle(
                        exchange=self.name,
                        symbol=symbol,
                        interval=interval,
                        open=float(o), high=float(h),
                        low=float(l),  close=float(c),
                        volume=float(v),
                        timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    ))
                    
                try:
                    cache = ex.ohlcvs.get(symbol, {}).get(interval)
                    if cache and len(cache) > 2:
                        ex.ohlcvs[symbol][interval] = cache[-2:]
                except Exception:
                    pass
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance candles loop [{symbol}/{interval}]: {e}")
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def unsubscribe(self, symbol: str) -> None:
        for t in self._ws_tasks.pop(symbol, []):
            t.cancel()

    # ── REST: orders ──────────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        ex = self._get_ex(order.symbol)
        ccxt_type = {
            'market':     'market',
            'limit':      'limit',
            'stop_limit': 'STOP_LOSS_LIMIT',
        }[order.order_type]

        params = {}
        if order.order_type == 'stop_limit':
            params['stopPrice'] = order.stop_price

        qty = None if order.qty_mode == 'quote' else order.qty
        if order.qty_mode == 'quote':
            params['quoteOrderQty'] = order.qty

        try:
            r = await ex.create_order(
                symbol=order.symbol,
                type=ccxt_type,
                side=order.side,
                amount=qty,
                price=order.price if order.order_type != 'market' else None,
                params=params,
            )
            order.exchange_order_id = str(r['id'])
            order.status            = self._map_status(r.get('status', ''))
            order.filled_qty        = float(r.get('filled') or 0)
            order.avg_fill_price    = float(r.get('average') or 0) or None
            logger.info(f"Binance order placed: {r['id']} {order.side} {order.symbol}")
        except Exception as e:
            order.status = 'rejected'
            logger.error(f"Binance place_order error: {e}")
        return order

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        try:
            await self._get_ex(symbol).cancel_order(exchange_order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"Binance cancel_order error: {e}")
            return False

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        try:
            return [self._parse_order(o)
                    for o in await self._spot.fetch_open_orders(symbol)]
        except Exception as e:
            logger.error(f"Binance get_open_orders error: {e}")
            return []

    async def get_order(self, exchange_order_id: str, symbol: str) -> Order:
        return self._parse_order(
            await self._get_ex(symbol).fetch_order(exchange_order_id, symbol)
        )

    # ── REST: account ─────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        try:
            return [
                Position(
                    exchange=self.name,
                    symbol=p['symbol'],
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
                for p in await self._futures.fetch_positions()
                if float(p.get('contracts') or 0)
            ]
        except Exception as e:
            logger.error(f"Binance get_positions error: {e}")
            return []

    async def get_balance(self) -> dict[str, float]:
        try:
            bal = await self._spot.fetch_balance()
            return {k: float(v['free'])
                    for k, v in bal.items()
                    if isinstance(v, dict) and v.get('free')}
        except Exception as e:
            logger.error(f"Binance get_balance error: {e}")
            return {}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._futures.set_leverage(leverage, symbol)
            return True
        except Exception as e:
            logger.error(f"Binance set_leverage error: {e}")
            return False

    async def set_margin_mode(self, symbol: str, mode: str) -> bool:
        try:
            await self._futures.set_margin_mode(mode.upper(), symbol)
            return True
        except Exception as e:
            logger.error(f"Binance set_margin_mode error: {e}")
            return False

    # ── Symbol normalisation ──────────────────────────────────────────────────

    def normalize_symbol(self, raw: str) -> str:
        return raw

    def to_exchange_symbol(self, canonical: str) -> str:
        return canonical

    async def get_tradable_symbols(self) -> list[str]:
        try:
            markets = await self._spot.load_markets(reload=True)
            return [s for s, m in markets.items() if m.get('active')]
        except Exception as e:
            logger.error(f"Binance get_tradable_symbols error: {e}")
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_ex(self, symbol: str):
        if self._futures and symbol in (self._futures.markets or {}):
            return self._futures
        return self._spot

    def _parse_order(self, r: dict) -> Order:
        return Order(
            exchange_order_id=str(r.get('id', '')),
            exchange=self.name,
            symbol=r.get('symbol', ''),
            side=r.get('side', 'buy'),
            order_type={'market': 'market', 'limit': 'limit'}.get(
                str(r.get('type', '')).lower(), 'market'
            ),
            price=float(r.get('price') or 0) or None,
            qty=float(r.get('amount') or 0),
            status=self._map_status(r.get('status', '')),
            filled_qty=float(r.get('filled') or 0),
            avg_fill_price=float(r.get('average') or 0) or None,
        )

    @staticmethod
    def _map_status(s: str) -> str:
        return {
            'open':     'working',
            'closed':   'filled',
            'canceled': 'cancelled',
        }.get(s, 'pending')

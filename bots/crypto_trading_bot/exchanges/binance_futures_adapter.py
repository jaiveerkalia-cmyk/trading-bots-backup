"""
Binance USDT-margined Perpetual Futures adapter.
Uses mark price for paper fills and PnL.
Last trade price goes in Tick.price; mark price goes in Tick.mark_price.
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._ex = ccxtpro.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'options': {
                'defaultType': 'future',
                'newUpdates': True,
            },
        })
        if self.testnet:
            self._ex.set_sandbox_mode(True)
        await self._ex.load_markets()
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
                raw  = await self._ex.watch_ticker(symbol)
                info = raw.get('info', {})

                last_price   = float(raw.get('last') or raw.get('close') or 0)
                # ccxt futures ticker has markPrice in info dict
                mark_price   = float(
                    info.get('markPrice') or
                    raw.get('markPrice') or
                    last_price
                )
                funding_rate = float(info.get('lastFundingRate') or 0) or None

                tick = Tick(
                    exchange=self.name,
                    symbol=symbol,
                    price=last_price,
                    mark_price=mark_price,
                    funding_rate=funding_rate,
                    volume=float(raw.get('baseVolume') or 0),
                    timestamp=datetime.fromtimestamp(
                        raw['timestamp'] / 1000, tz=timezone.utc
                    ) if raw.get('timestamp') else datetime.now(timezone.utc),
                )
                await cb(tick)
                del tick, raw
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BinanceFutures ticker [{symbol}]: {e}")
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
                    exchange=self.name, symbol=symbol,
                    bids=[OrderBookLevel(price=float(p), qty=float(q))
                          for p, q in raw['bids'][:depth]],
                    asks=[OrderBookLevel(price=float(p), qty=float(q))
                          for p, q in raw['asks'][:depth]],
                    timestamp=datetime.fromtimestamp(
                        raw['timestamp'] / 1000, tz=timezone.utc
                    ) if raw.get('timestamp') else datetime.now(timezone.utc),
                )
                await cb(book)
                del book
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BinanceFutures orderbook [{symbol}]: {e}")
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
                        exchange=self.name, symbol=symbol, interval=interval,
                        open=float(o), high=float(h), low=float(l), close=float(c),
                        volume=float(v),
                        timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    ))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BinanceFutures candles [{symbol}/{interval}]: {e}")
                await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def unsubscribe(self, symbol: str) -> None:
        for t in self._ws_tasks.pop(symbol, []):
            t.cancel()

    # ── REST: orders ──────────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        ccxt_type = {
            'market':     'market',
            'limit':      'limit',
            'stop_limit': 'STOP_MARKET',
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
            logger.info(f"BinanceFutures order placed: {r['id']}")
        except Exception as e:
            order.status = 'rejected'
            logger.error(f"BinanceFutures place_order error: {e}")
        return order

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        try:
            await self._ex.cancel_order(exchange_order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"BinanceFutures cancel_order error: {e}")
            return False

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        try:
            return [self._parse_order(o) for o in await self._ex.fetch_open_orders(symbol)]
        except Exception as e:
            logger.error(f"BinanceFutures get_open_orders error: {e}")
            return []

    async def get_order(self, exchange_order_id: str, symbol: str) -> Order:
        return self._parse_order(await self._ex.fetch_order(exchange_order_id, symbol))

    # ── REST: account ─────────────────────────────────────────────────────────

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
            logger.error(f"BinanceFutures get_positions error: {e}")
            return []

    async def get_balance(self) -> dict[str, float]:
        try:
            bal = await self._ex.fetch_balance()
            return {k: float(v['free']) for k, v in bal.items()
                    if isinstance(v, dict) and v.get('free')}
        except Exception as e:
            logger.error(f"BinanceFutures get_balance error: {e}")
            return {}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._ex.set_leverage(leverage, symbol)
            return True
        except Exception as e:
            logger.error(f"BinanceFutures set_leverage error: {e}")
            return False

    async def set_margin_mode(self, symbol: str, mode: str) -> bool:
        try:
            await self._ex.set_margin_mode(mode.upper(), symbol)
            return True
        except Exception as e:
            logger.error(f"BinanceFutures set_margin_mode error: {e}")
            return False

    def normalize_symbol(self, raw: str) -> str:
        return raw

    def to_exchange_symbol(self, canonical: str) -> str:
        return canonical

    async def get_tradable_symbols(self) -> list[str]:
        try:
            markets = await self._ex.load_markets(reload=True)
            return [s for s, m in markets.items()
                    if m.get('active') and m.get('type') in ('swap', 'future')]
        except Exception as e:
            logger.error(f"BinanceFutures get_tradable_symbols error: {e}")
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
        return {'open': 'working', 'closed': 'filled', 'canceled': 'cancelled'}.get(s, 'pending')

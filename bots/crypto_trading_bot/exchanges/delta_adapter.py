from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional, Callable, Awaitable
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt_async

from exchanges.base import BaseExchangeAdapter
from common.models import Tick, Candle, OrderBook, OrderBookLevel, Order, Position
from common import settings

logger = logging.getLogger(__name__)

REST_INDIA   = "https://api.india.delta.exchange"
WS_INDIA     = "wss://socket.india.delta.exchange"
REST_TESTNET = "https://cdn-ind.testnet.deltaex.org"
WS_TESTNET   = "wss://socket.testnet.delta.exchange"

_INTERVAL_MAP = {
    '1m':  'candlestick_1m',
    '5m':  'candlestick_5m',
    '15m': 'candlestick_15m',
    '1h':  'candlestick_60m',
    '4h':  'candlestick_240m',
    '1d':  'candlestick_1d',
}


class DeltaAdapter(BaseExchangeAdapter):

    supports_stop_limit   = True
    supports_futures      = True
    supports_leverage     = True
    supports_ws_ticker    = True
    supports_ws_orderbook = True
    supports_ws_candles   = True

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        super().__init__(api_key, api_secret, testnet)
        self.name      = 'delta'
        self._rest_url = REST_TESTNET if testnet else REST_INDIA
        self._ws_url   = WS_TESTNET   if testnet else WS_INDIA

        self._exchange: Optional[ccxt_async.delta] = None
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._session:   Optional[aiohttp.ClientSession] = None

        self._ws:       Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task:  Optional[asyncio.Task] = None
        self._ws_ready  = asyncio.Event()

        # channel_key ('ticker:BTC/USDT') -> list of callbacks
        self._subs: dict[str, list[Callable]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        # Limit TCP connection pool — Delta needs at most 2-3 connections
        self._connector = aiohttp.TCPConnector(
            limit=5,
            limit_per_host=3,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(connector=self._connector)

        self._exchange = ccxt_async.delta({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'urls': {'api': {'public': self._rest_url, 'private': self._rest_url}},
        })
        await self._exchange.load_markets()

        self._ws_task = asyncio.create_task(self._ws_loop())
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Delta WS did not connect within 10s — continuing anyway")

        logger.info("Delta Exchange adapter connected")

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._exchange:
            await self._exchange.close()
        if self._session:
            await self._session.close()
        if self._connector:
            await self._connector.close()
        self._subs.clear()
        logger.info("Delta Exchange adapter disconnected")

    # ── WebSocket core ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        while True:
            try:
                # max_msg_size=1MB — Delta messages are small JSON, no need for 4MB default
                async with self._session.ws_connect(
                    self._ws_url, max_msg_size=1024 * 1024
                ) as ws:
                    self._ws = ws
                    await self._send_auth(ws)
                    await self._resubscribe(ws)
                    self._ws_ready.set()
                    logger.info("Delta WS connected")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # Parse, dispatch, drop — no accumulation
                            await self._handle_message(json.loads(msg.data))
                            del msg
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"Delta WS closed/error: {msg.type}")
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Delta WS error: {e}")

            self._ws_ready.clear()
            logger.info(f"Delta WS reconnecting in {settings.WS_RECONNECT_DELAY}s...")
            await asyncio.sleep(settings.WS_RECONNECT_DELAY)

    async def _send_auth(self, ws) -> None:
        ts  = str(int(time.time()))
        sig = hmac.new(
            self.api_secret.encode(),
            f'GET{ts}/live'.encode(),
            hashlib.sha256,
        ).hexdigest()
        await ws.send_json({
            'type': 'key-auth',
            'payload': {'api-key': self.api_key, 'signature': sig, 'timestamp': ts},
        })

    async def _resubscribe(self, ws) -> None:
        channels: dict[str, list[str]] = {}
        for key in self._subs:
            channel, canonical = key.split(':', 1)
            channels.setdefault(channel, []).append(
                self.to_exchange_symbol(canonical)
            )
        for channel, symbols in channels.items():
            await ws.send_json({
                'type': 'subscribe',
                'payload': {'channels': [{'name': channel, 'symbols': symbols}]},
            })

    async def _send_subscribe(self, channel: str, delta_symbol: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_json({
                'type': 'subscribe',
                'payload': {'channels': [{'name': channel, 'symbols': [delta_symbol]}]},
            })

    # ── WS message dispatch — parse, call, drop ───────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        t = msg.get('type', '')
        if t == 'ticker':
            await self._on_ticker(msg)
        elif t in ('l2_orderbook', 'l2_orderbook_delta'):
            await self._on_orderbook(msg)
        elif t == 'candlestick':
            await self._on_candle(msg)
        # heartbeat / subscriptions / key-auth — silently ignored

    async def _on_ticker(self, msg: dict) -> None:
        symbol = self.normalize_symbol(msg.get('symbol', ''))
        cbs    = self._subs.get(f"ticker:{symbol}")
        if not cbs:
            return
        tick = Tick(
            exchange=self.name,
            symbol=symbol,
            price=float(msg.get('close') or msg.get('mark_price') or 0),
            volume=float(msg.get('volume') or 0),
            timestamp=datetime.now(timezone.utc),
        )
        for cb in cbs:
            await cb(tick)
        del tick

    async def _on_orderbook(self, msg: dict) -> None:
        symbol = self.normalize_symbol(msg.get('symbol', ''))
        cbs    = self._subs.get(f"l2_orderbook:{symbol}")
        if not cbs:
            return
        depth  = settings.DEFAULT_ORDERBOOK_DEPTH
        bids   = [
            OrderBookLevel(price=float(b['limit_price']), qty=float(b['size']))
            for b in msg.get('buy', [])[:depth]
        ]
        asks   = [
            OrderBookLevel(price=float(a['limit_price']), qty=float(a['size']))
            for a in msg.get('sell', [])[:depth]
        ]
        book = OrderBook(
            exchange=self.name,
            symbol=symbol,
            bids=sorted(bids, key=lambda x: -x.price),
            asks=sorted(asks, key=lambda x:  x.price),
            timestamp=datetime.now(timezone.utc),
        )
        for cb in cbs:
            await cb(book)
        del book, bids, asks

    async def _on_candle(self, msg: dict) -> None:
        symbol   = self.normalize_symbol(msg.get('symbol', ''))
        interval = msg.get('resolution', '1m')
        cbs      = self._subs.get(f"candlestick_{interval}:{symbol}")
        if not cbs:
            return
        candle = Candle(
            exchange=self.name,
            symbol=symbol,
            interval=interval,
            open=float(msg.get('open')   or 0),
            high=float(msg.get('high')   or 0),
            low=float(msg.get('low')     or 0),
            close=float(msg.get('close') or 0),
            volume=float(msg.get('volume') or 0),
            timestamp=datetime.now(timezone.utc),
        )
        for cb in cbs:
            await cb(candle)
        del candle

    # ── WS subscriptions ──────────────────────────────────────────────────────

    async def subscribe_ticker(
        self, symbol: str, callback: Callable[[Tick], Awaitable[None]]
    ) -> None:
        self._subs.setdefault(f"ticker:{symbol}", []).append(callback)
        await self._send_subscribe('ticker', self.to_exchange_symbol(symbol))

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[OrderBook], Awaitable[None]]
    ) -> None:
        self._subs.setdefault(f"l2_orderbook:{symbol}", []).append(callback)
        await self._send_subscribe('l2_orderbook', self.to_exchange_symbol(symbol))

    async def subscribe_candles(
        self, symbol: str, interval: str, callback: Callable[[Candle], Awaitable[None]]
    ) -> None:
        channel = _INTERVAL_MAP.get(interval, f"candlestick_{interval}")
        self._subs.setdefault(f"{channel}:{symbol}", []).append(callback)
        await self._send_subscribe(channel, self.to_exchange_symbol(symbol))

    async def unsubscribe(self, symbol: str) -> None:
        for key in [k for k in self._subs if k.endswith(f':{symbol}')]:
            del self._subs[key]

    # ── REST: orders ──────────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        ccxt_type = {
            'market':     'market',
            'limit':      'limit',
            'stop_limit': 'stop_limit',
        }[order.order_type]

        params = {}
        if order.order_type == 'stop_limit' and order.stop_price:
            params['stopPrice'] = order.stop_price

        try:
            r = await self._exchange.create_order(
                symbol=order.symbol,
                type=ccxt_type,
                side=order.side,
                amount=order.qty,
                price=order.price if order.order_type != 'market' else None,
                params=params,
            )
            order.exchange_order_id = str(r['id'])
            order.status            = self._map_status(r.get('status', ''))
            order.filled_qty        = float(r.get('filled') or 0)
            order.avg_fill_price    = float(r.get('average') or 0) or None
            logger.info(f"Delta order placed: {r['id']} {order.side} {order.symbol}")
        except Exception as e:
            order.status = 'rejected'
            logger.error(f"Delta place_order error: {e}")
        return order

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        try:
            await self._exchange.cancel_order(exchange_order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"Delta cancel_order error: {e}")
            return False

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        try:
            return [self._parse_order(o)
                    for o in await self._exchange.fetch_open_orders(symbol)]
        except Exception as e:
            logger.error(f"Delta get_open_orders error: {e}")
            return []

    async def get_order(self, exchange_order_id: str, symbol: str) -> Order:
        return self._parse_order(
            await self._exchange.fetch_order(exchange_order_id, symbol)
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
                for p in await self._exchange.fetch_positions()
                if float(p.get('contracts') or 0)
            ]
        except Exception as e:
            logger.error(f"Delta get_positions error: {e}")
            return []

    async def get_balance(self) -> dict[str, float]:
        try:
            bal = await self._exchange.fetch_balance()
            return {k: float(v['free'])
                    for k, v in bal.items()
                    if isinstance(v, dict) and v.get('free')}
        except Exception as e:
            logger.error(f"Delta get_balance error: {e}")
            return {}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._exchange.set_leverage(leverage, symbol)
            return True
        except Exception as e:
            logger.error(f"Delta set_leverage error: {e}")
            return False

    async def set_margin_mode(self, symbol: str, mode: str) -> bool:
        try:
            await self._exchange.set_margin_mode(mode, symbol)
            return True
        except Exception as e:
            logger.error(f"Delta set_margin_mode error: {e}")
            return False

    # ── Symbol normalisation ──────────────────────────────────────────────────

    def normalize_symbol(self, raw: str) -> str:
        if '/' in raw:
            return raw
        if self._exchange and raw in (self._exchange.markets or {}):
            return self._exchange.markets[raw].get('symbol', raw)
        for q in ('USDT', 'USD', 'BTC', 'ETH', 'INR'):
            if raw.endswith(q):
                return f"{raw[:-len(q)]}/{q}"
        return raw

    def to_exchange_symbol(self, canonical: str) -> str:
        return canonical.replace('/', '') if '/' in canonical else canonical

    async def get_tradable_symbols(self) -> list[str]:
        try:
            markets = await self._exchange.load_markets(reload=True)
            return [s for s, m in markets.items() if m.get('active')]
        except Exception as e:
            logger.error(f"Delta get_tradable_symbols error: {e}")
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

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

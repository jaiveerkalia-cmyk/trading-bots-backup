from __future__ import annotations
import asyncio
import json
import logging
import os
import signal

import redis.asyncio as aioredis

from exchanges.registry import get_adapter
from exchanges.base import BaseExchangeAdapter
from trading_engine.trade_slot import SlotManager
from trading_engine.order_manager import OrderManager
from trading_engine.paper_engine import PaperEngine
from trading_engine.trigger_engine import TriggerEngine
from trading_engine.state_publisher import StatePublisher
from trading_engine.csv_writer import CSVWriter
from trading_engine.notifier import Notifier
from trading_engine.reconciliation import reconcile
from common.key_manager import load_keys
from common.models import TradeSlot, Alert, Order
from common import settings, redis_keys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('trading_engine')


async def main() -> None:
    redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True, max_connections=10,
    )
    keys      = load_keys()
    testnet   = os.getenv('TESTNET',   '0') == '1'
    live_mode = os.getenv('LIVE_MODE', '0') == '1'

    adapters: dict[str, BaseExchangeAdapter] = {}
    for exchange in settings.SUPPORTED_EXCHANGES:
        # binance_futures shares the same API credentials as binance
        creds = keys.get(exchange, keys.get('binance', {})) \
                if exchange == 'binance_futures' else keys.get(exchange, {})
        try:
            adapter = get_adapter(
                exchange=exchange,
                api_key=creds.get('api_key', ''),
                api_secret=creds.get('api_secret', ''),
                testnet=testnet,
            )
            await adapter.connect()
            adapters[exchange] = adapter
            logger.info(f"Connected: {exchange}")
        except Exception as e:
            logger.error(f"Failed to connect {exchange}: {e}")

    slot_manager    = SlotManager(redis)
    csv_writer      = CSVWriter()
    state_publisher = StatePublisher(redis, slot_manager)
    paper_engine    = PaperEngine(redis)
    notifier        = Notifier()
    order_manager   = OrderManager(
        adapters=adapters, paper_engine=paper_engine,
        csv_writer=csv_writer, state_publisher=state_publisher,
        live_mode=live_mode,
    )
    trigger_engine = TriggerEngine(
        redis_client=redis, slot_manager=slot_manager,
        on_exit=lambda sid, reason: _on_trigger_exit(
            sid, reason, slot_manager, order_manager,
            state_publisher, csv_writer, notifier,
        ),
        on_alert=lambda alert: _on_alert(alert, state_publisher, notifier),
        paper_engine=paper_engine,
    )

    await slot_manager.load()
    await csv_writer.start()
    await state_publisher.start()
    await trigger_engine.start()
    await reconcile(adapters, slot_manager, state_publisher)
    state_publisher.log(
        f"Engine ready — {'LIVE' if live_mode else 'PAPER'} mode", level='success'
    )

    try:
        await asyncio.gather(
            _command_loop(redis, slot_manager, order_manager,
                          state_publisher, adapters, paper_engine),
            _paper_fill_checker(paper_engine, slot_manager, order_manager,
                                state_publisher, csv_writer),
        )
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown(redis, adapters, csv_writer, state_publisher, trigger_engine)


# ── Paper fill checker ────────────────────────────────────────────────────────

async def _paper_fill_checker(
    paper_engine, slot_manager, order_manager, state_publisher, csv_writer
) -> None:
    while True:
        try:
            seen: set[str] = set()
            for item in list(paper_engine._pending_limits):
                key = f"{item['exchange']}:{item['symbol']}"
                if key in seen:
                    continue
                seen.add(key)
                price = await paper_engine._last_tick(item['exchange'], item['symbol'])
                if price <= 0:
                    continue
                filled = await paper_engine.check_pending_fills(
                    item['exchange'], item['symbol'], price
                )
                for order in filled:
                    slot = slot_manager.get_slot(order.slot_id or '')
                    if not slot:
                        continue
                    if order not in slot.orders:
                        slot.orders.append(order)
                    slot.status   = 'active'
                    pos = await paper_engine.open_position(slot.id, order, slot.side)
                    slot.position = pos
                    await order_manager.place_native_sl_tp(slot)
                    await slot_manager.update_slot(slot)
                    await csv_writer.enqueue_trade({
                        'timestamp':   order.updated_at.isoformat(),
                        'exchange':    order.exchange,
                        'symbol':      order.symbol,
                        'side':        order.side,
                        'order_type':  order.order_type,
                        'qty':         order.filled_qty,
                        'entry_price': order.avg_fill_price,
                        'exit_price': '', 'pnl': '',
                        'is_paper': True, 'slot_id': slot.id,
                    })
                    state_publisher.log(
                        f"[PAPER] Limit filled: {order.side.upper()} "
                        f"{order.qty} {order.symbol} @ {order.avg_fill_price}",
                        level='success', exchange=order.exchange, symbol=order.symbol,
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Paper fill checker error: {e}")
        await asyncio.sleep(0.5)


# ── Command loop ──────────────────────────────────────────────────────────────

async def _command_loop(
    redis, slot_manager, order_manager, state_publisher, adapters, paper_engine
) -> None:
    logger.info("Command loop started")
    while True:
        try:
            result = await redis.blpop(redis_keys.COMMAND_QUEUE, timeout=2)
            if not result:
                continue
            _, raw = result
            await _handle(
                json.loads(raw), slot_manager, order_manager,
                state_publisher, adapters, paper_engine, redis,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Command error: {e}")


async def _handle(
    cmd, slot_manager, order_manager, state_publisher, adapters, paper_engine, redis
) -> None:
    action = cmd.get('type')

    if action == redis_keys.CMD_OPEN_SLOT:
        slot = TradeSlot.model_validate(cmd['slot'])

        # Close any opposite position on same exchange+symbol first
        for existing in slot_manager.get_active_slots():
            if (existing.exchange == slot.exchange
                    and existing.symbol == slot.symbol
                    and existing.side   != slot.side
                    and existing.position):
                state_publisher.log(
                    f"Closing opposite {existing.side} before opening {slot.side}",
                    exchange=slot.exchange, symbol=slot.symbol, level='warning',
                )
                co = Order(
                    exchange=existing.exchange, symbol=existing.symbol,
                    side='sell' if existing.side == 'long' else 'buy',
                    order_type='market', qty=existing.position.qty, slot_id=existing.id,
                )
                co = await order_manager.place_order(co, existing)
                existing.orders.append(co)  # ← ensure close order in history
                if co.is_paper:
                    await paper_engine.close_position(existing.id)
                await slot_manager.close_slot(existing.id, _pnl(existing, co.avg_fill_price or 0.0))

        await slot_manager.create_slot(slot)
        for leg in slot.entries:
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='buy' if slot.side == 'long' else 'sell',
                order_type=leg.order_type,
                price=leg.price if leg.order_type != 'market' else None,
                qty=leg.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.status == 'filled':
                slot.status   = 'active'
                slot.position = await paper_engine.open_position(slot.id, order, slot.side) \
                    if order.is_paper else None
                await order_manager.place_native_sl_tp(slot)
                state_publisher.log("Entry filled — slot active",
                    exchange=slot.exchange, symbol=slot.symbol, level='success')
        await slot_manager.update_slot(slot)
        # Ensure market data subscribed (needed for stops, targets, alerts)
        await redis.publish('market_data:control', json.dumps({
            'cmd': 'subscribe', 'exchange': slot.exchange,
            'symbol': slot.symbol, 'streams': ['ticker', 'orderbook', 'candles:1m'],
        }))

    elif action == redis_keys.CMD_CLOSE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot:
            logger.warning(f"Close: slot not found {cmd.get('slot_id','')[:8]}")
            return
        if slot.position:
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='sell' if slot.side == 'long' else 'buy',
                order_type='market', qty=slot.position.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)   # ← close order must be in history
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            state_publisher.log(
                f"Slot closed — PnL: {pnl:.4f}",
                exchange=slot.exchange, symbol=slot.symbol,
            )
        else:
            for o in slot.orders:
                if o.status == 'working':
                    if o.is_paper:
                        paper_engine.cancel_pending(o.id)
                        o.status = 'cancelled'
                    else:
                        await order_manager.cancel_order(o)
            await slot_manager.close_slot(slot.id, 0.0)
            state_publisher.log("Slot cancelled (no position)", exchange=slot.exchange)

    elif action == redis_keys.CMD_CANCEL_ORDER:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if slot:
            oid = cmd.get('order_id', '')
            if oid.startswith('VSTOP-'):
                slot.stop_price = None
                await slot_manager.update_slot(slot)
            elif oid.startswith('VTGT-'):
                slot.target_price = None
                await slot_manager.update_slot(slot)
            else:
                for o in slot.orders:
                    if o.id == oid or o.exchange_order_id == oid:
                        if o.is_paper:
                            paper_engine.cancel_pending(o.id)
                            o.status = 'cancelled'
                        else:
                            await order_manager.cancel_order(o)
                        await slot_manager.update_slot(slot)
                        break

    elif action == redis_keys.CMD_MODIFY_ORDER:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        oid       = cmd.get('order_id', '')
        new_price = float(cmd.get('new_price') or 0)
        new_qty   = float(cmd.get('new_qty')   or 0)

        if oid.startswith('VSTOP-'):
            if new_price > 0:
                slot.stop_price = new_price
                await slot_manager.update_slot(slot)
            return
        if oid.startswith('VTGT-'):
            if new_price > 0:
                slot.target_price = new_price
                await slot_manager.update_slot(slot)
            return

        for o in slot.orders:
            if o.id == oid and o.status == 'working':
                if o.is_paper:
                    await paper_engine.modify_pending(oid, new_price, new_qty)
                    if new_price > 0: o.price = new_price
                    if new_qty   > 0: o.qty   = new_qty
                else:
                    # Live: cancel and replace
                    await order_manager.cancel_order(o)
                    new_ord = Order(
                        exchange=o.exchange, symbol=o.symbol,
                        side=o.side, order_type=o.order_type,
                        price=new_price or o.price, qty=new_qty or o.qty,
                        slot_id=slot.id,
                    )
                    new_ord = await order_manager.place_order(new_ord, slot)
                    slot.orders.append(new_ord)
                await slot_manager.update_slot(slot)
                state_publisher.log(
                    f"Order modified: {o.symbol} @ {new_price or o.price}",
                    exchange=slot.exchange, symbol=slot.symbol,
                )
                break

    elif action == redis_keys.CMD_SET_ALERT:
        alert = Alert.model_validate(cmd['alert'])
        slot_manager.add_alert(alert)
        await redis.publish('market_data:control', json.dumps({
            'cmd': 'subscribe', 'exchange': alert.exchange,
            'symbol': alert.symbol, 'streams': ['ticker'],
        }))
        state_publisher.log(f"Alert set: {alert.symbol}",
                            exchange=alert.exchange, symbol=alert.symbol)

    elif action == redis_keys.CMD_DELETE_ALERT:
        slot_manager.delete_alert(cmd.get('alert_id', ''))

    elif action == redis_keys.CMD_CLOSE_ALL:
        for slot in slot_manager.get_active_slots():
            if not slot.position:
                continue
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='sell' if slot.side == 'long' else 'buy',
                order_type='market', qty=slot.position.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)   # ← close order must be in history
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
        state_publisher.log("All positions closed", level='warning')

    elif action == redis_keys.CMD_SET_LIVE_MODE:
        live = cmd.get('live', False)
        order_manager.set_live_mode(live)
        state_publisher.log(
            f"Mode → {'LIVE' if live else 'PAPER'}",
            level='warning' if live else 'info',
        )

    elif action == redis_keys.CMD_UPDATE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if slot:
            if 'stop_price'   in cmd: slot.stop_price   = cmd['stop_price']
            if 'target_price' in cmd: slot.target_price = cmd['target_price']
            await slot_manager.update_slot(slot)


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def _on_trigger_exit(
    slot_id, reason, slot_manager, order_manager,
    state_publisher, csv_writer, notifier,
):
    slot = slot_manager.get_slot(slot_id)
    if not slot or not slot.position:
        return
    order = Order(
        exchange=slot.exchange, symbol=slot.symbol,
        side='sell' if slot.side == 'long' else 'buy',
        order_type='market', qty=slot.position.qty, slot_id=slot.id,
    )
    order = await order_manager.place_order(order, slot)
    slot.orders.append(order)   # ← THIS WAS THE HISTORY BUG — close order was not appended
    if order.is_paper:
        await order_manager.paper.close_position(slot_id)
    pnl = _pnl(slot, order.avg_fill_price or 0.0)
    await slot_manager.close_slot(slot_id, pnl)
    lvl = 'warning' if 'stop' in reason else 'success'
    msg = f"[{reason.upper().replace('_',' ')}] {slot.symbol} — PnL: {pnl:.4f}"
    state_publisher.log(msg, level=lvl, exchange=slot.exchange, symbol=slot.symbol)
    await notifier.send(msg, level=lvl)


async def _on_alert(alert, state_publisher, notifier):
    msg = (f"ALERT: {alert.symbol} "
           f"{'above ' + str(alert.upper) if alert.upper else 'below ' + str(alert.lower)}")
    state_publisher.log(msg, level='warning', exchange=alert.exchange, symbol=alert.symbol)
    await notifier.send(msg)


def _pnl(slot: TradeSlot, exit_price: float) -> float:
    if not slot.position:
        return 0.0
    fees         = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_type   = slot.entries[0].order_type if slot.entries else 'market'
    entry_fee_rt = fees['maker'] if entry_type == 'limit' else fees['taker']
    exit_fee_rt  = fees['taker']
    entry = slot.position.entry_price
    qty   = slot.position.qty
    gross = (
        (exit_price - entry) * qty if slot.side == 'long'
        else (entry - exit_price) * qty
    )
    return round(gross - (entry * qty * entry_fee_rt) - (exit_price * qty * exit_fee_rt), 4)


# ── Shutdown ──────────────────────────────────────────────────────────────────

async def _shutdown(redis, adapters, csv_writer, state_publisher, trigger_engine):
    logger.info("Shutting down trading engine...")
    await trigger_engine.stop()
    await state_publisher.stop()
    await csv_writer.stop()
    for name, adapter in adapters.items():
        try:
            await adapter.disconnect()
        except Exception as e:
            logger.error(f"Disconnect [{name}]: {e}")
    await redis.aclose()
    logger.info("Trading engine stopped")


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
        )
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()

"""
Trading engine entry point.
Orchestrates all components, drives the command loop.
"""
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
        decode_responses=True,
        max_connections=10,
    )

    keys      = load_keys()
    testnet   = os.getenv('TESTNET',   '0') == '1'
    live_mode = os.getenv('LIVE_MODE', '0') == '1'

    adapters: dict[str, BaseExchangeAdapter] = {}
    for exchange in settings.SUPPORTED_EXCHANGES:
        creds = keys.get(exchange, {})
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

    # ── Wire components ───────────────────────────────────────────────────────
    slot_manager    = SlotManager(redis)
    csv_writer      = CSVWriter()
    state_publisher = StatePublisher(redis, slot_manager)
    paper_engine    = PaperEngine(redis)
    notifier        = Notifier()
    order_manager   = OrderManager(
        adapters=adapters,
        paper_engine=paper_engine,
        csv_writer=csv_writer,
        state_publisher=state_publisher,
        live_mode=live_mode,
    )
    trigger_engine  = TriggerEngine(
        redis_client=redis,
        slot_manager=slot_manager,
        on_exit=lambda sid, reason: _on_trigger_exit(
            sid, reason, slot_manager, order_manager,
            state_publisher, csv_writer, notifier,
        ),
        on_alert=lambda alert: _on_alert(alert, state_publisher, notifier),
    )

    # ── Start ─────────────────────────────────────────────────────────────────
    await slot_manager.load()
    await csv_writer.start()
    await state_publisher.start()
    await trigger_engine.start()
    await reconcile(adapters, slot_manager, state_publisher)

    state_publisher.log(
        f"Engine ready — {'LIVE' if live_mode else 'PAPER'} mode "
        f"| testnet={'yes' if testnet else 'no'}",
        level='success',
    )

    try:
        await _command_loop(
            redis, slot_manager, order_manager, state_publisher, adapters
        )
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown(redis, adapters, csv_writer, state_publisher, trigger_engine)


# ── Command loop ──────────────────────────────────────────────────────────────

async def _command_loop(
    redis:           aioredis.Redis,
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    adapters:        dict,
) -> None:
    logger.info("Command loop started")
    while True:
        try:
            result = await redis.blpop(redis_keys.COMMAND_QUEUE, timeout=2)
            if not result:
                continue
            _, raw = result
            await _handle(
                json.loads(raw),
                slot_manager, order_manager, state_publisher, adapters,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Command error: {e}")


async def _handle(
    cmd:             dict,
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    adapters:        dict,
) -> None:
    action = cmd.get('type')

    if action == redis_keys.CMD_OPEN_SLOT:
        slot = TradeSlot.model_validate(cmd['slot'])
        await slot_manager.create_slot(slot)
        close_side = 'buy' if slot.side == 'long' else 'sell'

        for leg in slot.entries:
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='buy' if slot.side == 'long' else 'sell',
                order_type=leg.order_type,
                price=leg.price, qty=leg.qty,
                slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.status == 'filled':
                slot.status   = 'active'
                pos = await order_manager.paper.open_position(slot.id, order, slot.side) \
                      if order.is_paper else None
                if pos:
                    slot.position = pos
                else:
                    # Live: fetch from exchange
                    live = await adapters.get(slot.exchange, object()).get_positions() \
                           if slot.exchange in adapters else []
                    for p in live:
                        if p.symbol == slot.symbol:
                            p.slot_id     = slot.id
                            slot.position = p
                            break
                await order_manager.place_native_sl_tp(slot)
                state_publisher.log("Entry filled — slot active",
                                    exchange=slot.exchange, symbol=slot.symbol,
                                    level='success')
        await slot_manager.update_slot(slot)

    elif action == redis_keys.CMD_CLOSE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if slot and slot.position:
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='sell' if slot.side == 'long' else 'buy',
                order_type='market', qty=slot.position.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await order_manager.paper.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            state_publisher.log(f"Slot closed manually — PnL: {pnl:.2f}",
                                exchange=slot.exchange, symbol=slot.symbol)

    elif action == redis_keys.CMD_CANCEL_ORDER:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if slot:
            for o in slot.orders:
                if o.id == cmd.get('order_id') or o.exchange_order_id == cmd.get('order_id'):
                    await order_manager.cancel_order(o)
                    await slot_manager.update_slot(slot)
                    break

    elif action == redis_keys.CMD_SET_ALERT:
        alert = Alert.model_validate(cmd['alert'])
        slot_manager.add_alert(alert)
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
            if order.is_paper:
                await order_manager.paper.close_position(slot.id)
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


# ── Event callbacks ───────────────────────────────────────────────────────────

async def _on_trigger_exit(
    slot_id:         str,
    reason:          str,
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    csv_writer:      CSVWriter,
    notifier:        Notifier,
) -> None:
    slot = slot_manager.get_slot(slot_id)
    if not slot or not slot.position:
        return
    order = Order(
        exchange=slot.exchange, symbol=slot.symbol,
        side='sell' if slot.side == 'long' else 'buy',
        order_type='market', qty=slot.position.qty, slot_id=slot.id,
    )
    order = await order_manager.place_order(order, slot)
    if order.is_paper:
        await order_manager.paper.close_position(slot_id)
    pnl = _pnl(slot, order.avg_fill_price or 0.0)
    await slot_manager.close_slot(slot_id, pnl)
    lvl = 'warning' if 'stop' in reason else 'success'
    msg = f"[{reason.upper().replace('_', ' ')}] {slot.symbol} — PnL: {pnl:.2f}"
    state_publisher.log(msg, level=lvl, exchange=slot.exchange, symbol=slot.symbol)
    await notifier.send(msg, level=lvl)


async def _on_alert(
    alert:           Alert,
    state_publisher: StatePublisher,
    notifier:        Notifier,
) -> None:
    msg = (f"Alert: {alert.symbol} "
           f"{'above ' + str(alert.upper) if alert.upper else 'below ' + str(alert.lower)}")
    state_publisher.log(msg, level='warning', exchange=alert.exchange, symbol=alert.symbol)
    await notifier.send(msg)


def _pnl(slot: TradeSlot, exit_price: float) -> float:
    if not slot.position:
        return 0.0
    if slot.side == 'long':
        return (exit_price - slot.position.entry_price) * slot.position.qty
    return (slot.position.entry_price - exit_price) * slot.position.qty


# ── Shutdown ──────────────────────────────────────────────────────────────────

async def _shutdown(
    redis:           aioredis.Redis,
    adapters:        dict,
    csv_writer:      CSVWriter,
    state_publisher: StatePublisher,
    trigger_engine:  TriggerEngine,
) -> None:
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

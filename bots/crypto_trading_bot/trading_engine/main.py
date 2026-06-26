"""
Trading Engine — main event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis

from common import redis_keys, settings
from common.key_manager import load_keys
from common.models import Order, TradeSlot, Alert
from exchanges.base     import BaseExchangeAdapter
from exchanges.registry import get_adapter
from trading_engine.csv_writer      import CSVWriter
from trading_engine.notifier        import Notifier
from trading_engine.order_manager   import OrderManager
from trading_engine.paper_engine    import PaperEngine
from trading_engine.reconciliation  import reconcile
from trading_engine.state_publisher import StatePublisher
from trading_engine.trade_slot      import SlotManager
from trading_engine.trigger_engine  import TriggerEngine

logger = logging.getLogger('trading_engine')


# ─────────────────────────────────────────────────────────────────────────────
# PnL helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pnl(slot, exit_price: float) -> float:
    if not slot.position:
        return 0.0
    fees         = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_type   = slot.entries[0].order_type if slot.entries else 'market'
    entry_fee_rt = fees['maker'] if entry_type == 'limit' else fees['taker']
    exit_fee_rt  = fees['taker']
    entry        = slot.position.entry_price
    qty          = slot.position.qty
    gross = (
        (exit_price - entry) * qty if slot.side == 'long'
        else (entry - exit_price) * qty
    )
    return round(
        gross
        - entry      * qty * entry_fee_rt
        - exit_price * qty * exit_fee_rt,
        6,
    )


def _partial_pnl(slot, exit_price: float, qty: float) -> float:
    if not slot.position or qty <= 0:
        return 0.0
    fees         = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_type   = slot.entries[0].order_type if slot.entries else 'market'
    entry_fee_rt = fees['maker'] if entry_type == 'limit' else fees['taker']
    exit_fee_rt  = fees['taker']
    entry        = slot.position.entry_price
    gross = (
        (exit_price - entry) * qty if slot.side == 'long'
        else (entry - exit_price) * qty
    )
    return round(
        gross
        - entry      * qty * entry_fee_rt
        - exit_price * qty * exit_fee_rt,
        6,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts(dt) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


async def _csv_open(csv_writer, slot_manager, slot, order) -> None:
    cumulative = sum(s.realized_pnl for s in slot_manager.get_all_slots())
    await csv_writer.enqueue_event({
        'timestamp':       _ts(order.updated_at),
        'event_type':      'open',
        'exchange':        order.exchange,
        'symbol':          order.symbol,
        'side':            order.side,
        'order_type':      order.order_type,
        'qty':             order.filled_qty,
        'entry_price':     order.avg_fill_price or '',
        'exit_price':      '',
        'trade_pnl':       '',
        'realized_pnl':    round(cumulative, 6),
        'portfolio_value': '',
        'is_paper':        order.is_paper,
        'slot_id':         slot.id,
        'notes':           '',
    })


async def _csv_close(
    csv_writer, slot_manager, slot, order,
    pnl: float, entry_price: float = 0.0, event_type: str = 'close',
) -> None:
    cumulative = sum(s.realized_pnl for s in slot_manager.get_all_slots())
    await csv_writer.enqueue_event({
        'timestamp':       _ts(order.updated_at),
        'event_type':      event_type,
        'exchange':        order.exchange,
        'symbol':          order.symbol,
        'side':            order.side,
        'order_type':      order.order_type,
        'qty':             order.filled_qty,
        'entry_price':     entry_price or '',
        'exit_price':      order.avg_fill_price or '',
        'trade_pnl':       round(pnl, 6),
        'realized_pnl':    round(cumulative, 6),
        'portfolio_value': '',
        'is_paper':        order.is_paper,
        'slot_id':         slot.id,
        'notes':           '',
    })


# ─────────────────────────────────────────────────────────────────────────────
# Command handler
# ─────────────────────────────────────────────────────────────────────────────

async def _handle(
    cmd,
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    paper_engine:    PaperEngine,
    redis:           aioredis.Redis,
    csv_writer:      CSVWriter,
) -> None:
    action = cmd.get('type')

    # ── Open slot ─────────────────────────────────────────────────────────────
    if action == redis_keys.CMD_OPEN_SLOT:
        raw_slot = cmd.get('slot', {})
        try:
            slot = (
                TradeSlot.model_validate(raw_slot)
                if isinstance(raw_slot, dict)
                else raw_slot
            )
        except Exception as exc:
            logger.error("CMD_OPEN_SLOT: invalid slot data: %s", exc)
            return

        slot  = await slot_manager.create_slot(slot)
        entry = slot.entries[0] if slot.entries else None
        if not entry:
            logger.error("CMD_OPEN_SLOT: slot has no entry leg")
            return

        order = Order(
            exchange        = slot.exchange,
            symbol          = slot.symbol,
            side            = 'buy' if slot.side == 'long' else 'sell',  # ← was slot.side
            order_type      = entry.order_type,
            qty             = entry.qty,
            price           = entry.price,            # ← was limit_price
            slot_id         = slot.id,
            reference_price = entry.reference_price,
        )
        order = await order_manager.place_order(order, slot)
        slot.orders.append(order)

        if order.status == 'filled':
            slot.status = 'active'
            if order.is_paper:
                slot.position = await paper_engine.open_position(slot.id, order, slot.side)  # ← assign
            await _csv_open(csv_writer, slot_manager, slot, order)
        elif order.status in ('open', 'pending', 'working'):
            slot.status = 'working'

        await slot_manager.update_slot(slot)
        state_publisher.log(
            f"Open order placed {slot.symbol} ({entry.order_type})",
            exchange=slot.exchange, symbol=slot.symbol,
        )
        
    # ── Full close ────────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_CLOSE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        if slot.position:
            entry_price = slot.position.entry_price
            order = Order(
                exchange   = slot.exchange,
                symbol     = slot.symbol,
                side       = 'sell' if slot.side == 'long' else 'buy',
                order_type = 'market',
                qty        = slot.position.qty,
                slot_id    = slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)
            state_publisher.log(
                f"Slot closed {slot.symbol}  pnl={pnl:+.4f}",
                exchange=slot.exchange, symbol=slot.symbol,
            )
        else:
            for o in slot.orders:
                if o.status in ('open', 'pending', 'working'):
                    await order_manager.cancel_order(o)
            await slot_manager.close_slot(slot.id, 0.0)
            state_publisher.log(
                f"Slot closed (no position) {slot.symbol}",
                exchange=slot.exchange, symbol=slot.symbol,
            )

    # ── Partial close ─────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_PARTIAL_CLOSE_SLOT:
        qty  = float(cmd.get('qty', 0))
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot or not slot.position or qty <= 0:
            return

        actual_qty  = min(qty, slot.position.qty)
        entry_price = slot.position.entry_price
        order = Order(
            exchange   = slot.exchange,
            symbol     = slot.symbol,
            side       = 'sell' if slot.side == 'long' else 'buy',
            order_type = 'market',
            qty        = actual_qty,
            slot_id    = slot.id,
        )
        order = await order_manager.place_order(order, slot)
        slot.orders.append(order)
        pnl = _partial_pnl(slot, order.avg_fill_price or 0.0, actual_qty)

        if order.is_paper:
            updated_pos, _ = await paper_engine.partial_close_position(slot.id, actual_qty)
            if updated_pos is None:
                await slot_manager.close_slot(slot.id, pnl)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, event_type='close')
            else:
                slot.position     = updated_pos
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await slot_manager.update_slot(slot)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, event_type='partial_close')
        else:
            slot.position.qty = round(slot.position.qty - actual_qty, 8)
            if slot.position.qty <= 1e-10:
                slot.position = None
                await slot_manager.close_slot(slot.id, pnl)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, event_type='close')
            else:
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await slot_manager.update_slot(slot)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, event_type='partial_close')

        state_publisher.log(
            f"Partial close {actual_qty:g} {slot.symbol}  pnl={pnl:+.4f}",
            exchange=slot.exchange, symbol=slot.symbol,
        )

    # ── Close all ─────────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_CLOSE_ALL:
        for slot in slot_manager.get_active_slots():
            if not slot.position:
                continue
            entry_price = slot.position.entry_price
            order = Order(
                exchange   = slot.exchange,
                symbol     = slot.symbol,
                side       = 'sell' if slot.side == 'long' else 'buy',
                order_type = 'market',
                qty        = slot.position.qty,
                slot_id    = slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)
            state_publisher.log(
                f"[close_all] {slot.symbol}  pnl={pnl:+.4f}",
                exchange=slot.exchange, symbol=slot.symbol,
            )

    # ── Update slot (stop / target) ───────────────────────────────────────────
    elif action == redis_keys.CMD_UPDATE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        if 'stop_price' in cmd:
            slot.stop_price = cmd['stop_price']
        if 'target_price' in cmd:
            slot.target_price = cmd['target_price']
        await slot_manager.update_slot(slot)
        state_publisher.log(
            f"Slot updated {slot.symbol}",
            exchange=slot.exchange, symbol=slot.symbol,
        )

    # ── Cancel order ──────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_CANCEL_ORDER:
        order_id = cmd.get('order_id', '')
        for slot in slot_manager.get_all_slots():
            for o in slot.orders:
                if o.id == order_id:
                    await order_manager.cancel_order(o)
                    state_publisher.log(
                        f"Order cancelled {o.symbol}",
                        exchange=o.exchange, symbol=o.symbol,
                    )
                    return

    # ── Modify order ──────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_MODIFY_ORDER:
        order_id  = cmd.get('order_id', '')
        new_price = cmd.get('new_price')
        new_qty   = cmd.get('new_qty')
        await paper_engine.modify_pending(
            order_id,
            float(new_price) if new_price else 0.0,
            float(new_qty)   if new_qty   else 0.0,
        )

    # ── Alerts ────────────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_SET_ALERT:
        raw_alert = cmd.get('alert', {})
        try:
            alert = (
                Alert.model_validate(raw_alert)
                if isinstance(raw_alert, dict)
                else raw_alert
            )
            slot_manager.add_alert(alert)
            state_publisher.log(
                f"Alert set {alert.exchange} {alert.symbol}",
                exchange=alert.exchange, symbol=alert.symbol,
            )
        except Exception as exc:
            logger.error("CMD_SET_ALERT: invalid alert data: %s", exc)

    elif action == redis_keys.CMD_DELETE_ALERT:
        slot_manager.delete_alert(cmd.get('alert_id', ''))

    # ── Live mode toggle ──────────────────────────────────────────────────────
    elif action == redis_keys.CMD_SET_LIVE_MODE:
        live = bool(cmd.get('live', False))
        order_manager.set_live_mode(live)
        state_publisher.log(
            f"Mode → {'LIVE' if live else 'PAPER'}",
            level='warning' if live else 'info',
        )


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-exit callback
# ─────────────────────────────────────────────────────────────────────────────

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
    entry_price = slot.position.entry_price
    order = Order(
        exchange   = slot.exchange,
        symbol     = slot.symbol,
        side       = 'sell' if slot.side == 'long' else 'buy',
        order_type = 'market',
        qty        = slot.position.qty,
        slot_id    = slot.id,
    )
    order = await order_manager.place_order(order, slot)
    slot.orders.append(order)
    if order.is_paper:
        await order_manager.paper.close_position(slot_id)
    pnl = _pnl(slot, order.avg_fill_price or 0.0)
    await slot_manager.close_slot(slot_id, pnl)
    await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)
    state_publisher.log(
        f"[{reason}] {slot.symbol}  pnl={pnl:+.4f}",
        exchange=slot.exchange, symbol=slot.symbol,
    )
    await notifier.send(f"[{reason.upper()}] {slot.symbol} | PnL {pnl:+.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# Paper limit-fill checker
# ─────────────────────────────────────────────────────────────────────────────

async def _paper_fill_checker(
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    csv_writer:      CSVWriter,
    paper_engine:    PaperEngine,
) -> None:
    while True:
        try:
            pairs: set[tuple[str, str]] = {
                (s.exchange, s.symbol)
                for s in slot_manager.get_all_slots()
                if s.status == 'working'
            }
            for exchange, symbol in pairs:
                price = await paper_engine._last_tick(exchange, symbol)
                if price <= 0:
                    continue
                filled_orders = await paper_engine.check_pending_fills(
                    exchange, symbol, price
                )
                for order in filled_orders:
                    slot = slot_manager.get_slot(order.slot_id)
                    if not slot or slot.status != 'working':
                        continue
                    slot.status   = 'active'
                    slot.position = await paper_engine.open_position(slot.id, order, slot.side)  # ← assign
                    await slot_manager.update_slot(slot)
                    await _csv_open(csv_writer, slot_manager, slot, order)
                    state_publisher.log(
                        f"Paper limit filled {slot.symbol} @ {order.avg_fill_price}",
                        exchange=slot.exchange, symbol=slot.symbol,
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("paper_fill_checker: %s", exc)
        await asyncio.sleep(settings.PAPER_FILL_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Command loop  ← uses BRPOP on COMMAND_QUEUE (not pub/sub)
# ─────────────────────────────────────────────────────────────────────────────

async def _command_loop(
    redis:           aioredis.Redis,
    slot_manager:    SlotManager,
    order_manager:   OrderManager,
    state_publisher: StatePublisher,
    paper_engine:    PaperEngine,
    csv_writer:      CSVWriter,
) -> None:
    logger.info("Command loop listening on %s", redis_keys.COMMAND_QUEUE)
    while True:
        try:
            result = await redis.brpop(redis_keys.COMMAND_QUEUE, timeout=1)
            if result is None:
                continue                  # timeout — loop again
            _, raw = result
            cmd = json.loads(raw)
            await _handle(
                cmd, slot_manager, order_manager,
                state_publisher, paper_engine, redis, csv_writer,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Command error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(name)-24s  %(levelname)s  %(message)s',
    )

    redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True,
        max_connections=20,
    )

    live_mode = os.getenv('LIVE_MODE', 'false').lower() == 'true'

    keys     = load_keys()
    adapters: dict[str, BaseExchangeAdapter] = {}
    for exchange in settings.SUPPORTED_EXCHANGES:
        ex_keys = keys.get(exchange, {})
        try:
            adapters[exchange] = get_adapter(
                exchange   = exchange,
                api_key    = ex_keys.get('api_key',    ''),
                api_secret = ex_keys.get('api_secret', ''),
            )
        except Exception as exc:
            logger.warning("Adapter init skipped [%s]: %s", exchange, exc)

    paper_engine  = PaperEngine(redis)
    csv_writer    = CSVWriter()
    slot_manager  = SlotManager(redis)
    state_pub     = StatePublisher(redis, slot_manager)
    notifier      = Notifier()
    order_manager = OrderManager(
        adapters        = adapters,
        paper_engine    = paper_engine,
        csv_writer      = csv_writer,
        state_publisher = state_pub,
        live_mode       = live_mode,
    )

    await csv_writer.start()
    await slot_manager.load()

    # Reconcile only in live mode — adapters are not connected in paper mode
    if live_mode:
        await reconcile(adapters, slot_manager, state_pub)
    else:
        state_pub.log("Paper mode — reconciliation skipped", level='info')

    on_exit = lambda slot_id, reason: _on_trigger_exit(
        slot_id, reason,
        slot_manager, order_manager, state_pub, csv_writer, notifier,
    )
    trigger = TriggerEngine(
        redis_client = redis,
        slot_manager = slot_manager,
        on_exit      = on_exit,
        on_alert     = lambda alert: notifier.send(
            f"[ALERT] {alert.exchange} {alert.symbol}"
        ),
        paper_engine = paper_engine,
    )

    await trigger.start()
    await state_pub.start()

    asyncio.create_task(_command_loop(
        redis, slot_manager, order_manager,
        state_pub, paper_engine, csv_writer,
    ))
    asyncio.create_task(_paper_fill_checker(
        slot_manager, order_manager, state_pub, csv_writer, paper_engine,
    ))

    logger.info("Trading engine ready  live=%s", live_mode)
    try:
        await asyncio.Future()
    finally:
        await trigger.stop()
        await state_pub.stop()
        await csv_writer.stop()
        await redis.aclose()


if __name__ == '__main__':
    asyncio.run(main())

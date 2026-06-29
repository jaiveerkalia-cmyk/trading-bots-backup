"""
Trading Engine — main event loop.

Position netting rules (like Binance One-Way Mode):
  • Same-side order   → add to existing position (average entry)
  • Opposite-side order → reduce / close / flip existing position
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

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

_MD_CONTROL = 'market_data:control'


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pnl(slot, exit_price: float) -> float:
    if not slot.position:
        return 0.0
    fees         = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_fee_rt = fees['maker'] if (slot.entries and slot.entries[0].order_type == 'limit') else fees['taker']
    entry = slot.position.entry_price
    qty   = slot.position.qty
    gross = ((exit_price - entry) * qty if slot.side == 'long'
             else (entry - exit_price) * qty)
    return round(gross - entry * qty * entry_fee_rt - exit_price * qty * fees['taker'], 6)


def _partial_pnl(slot, exit_price: float, qty: float) -> float:
    if not slot.position or qty <= 0:
        return 0.0
    fees         = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_fee_rt = fees['maker'] if (slot.entries and slot.entries[0].order_type == 'limit') else fees['taker']
    entry = slot.position.entry_price
    gross = ((exit_price - entry) * qty if slot.side == 'long'
             else (entry - exit_price) * qty)
    return round(gross - entry * qty * entry_fee_rt - exit_price * qty * fees['taker'], 6)


def _ts(dt) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


def _build_order(exchange: str, symbol: str, side: str, entry, slot_id: str) -> Order:
    if entry.order_type == 'stop_limit':
        order_price, order_stop = None, entry.price
    else:
        order_price = entry.price if (entry.price and entry.price > 0) else None
        order_stop  = None
    return Order(
        exchange        = exchange,
        symbol          = symbol,
        side            = 'buy' if side == 'long' else 'sell',
        order_type      = entry.order_type,
        qty             = entry.qty,
        price           = order_price,
        stop_price      = order_stop,
        slot_id         = slot_id,
        reference_price = entry.reference_price,
    )


def _find_existing_position(
    sm: SlotManager, exchange: str, symbol: str, side: str
) -> Optional[TradeSlot]:
    for s in sm.get_active_slots():
        if s.exchange == exchange and s.symbol == symbol and s.side == side and s.position:
            return s
    return None


async def _sub_market_data(redis: aioredis.Redis, exchange: str, symbol: str) -> None:
    try:
        await redis.publish(_MD_CONTROL, json.dumps({
            'cmd': 'subscribe', 'exchange': exchange,
            'symbol': symbol, 'streams': ['ticker'],
        }))
    except Exception as e:
        logger.warning("MD subscribe error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _csv_open(csv_writer, slot_manager, slot, order) -> None:
    cumulative = sum(s.realized_pnl for s in slot_manager.get_all_slots())
    await csv_writer.enqueue_event({
        'timestamp': _ts(order.updated_at), 'event_type': 'open',
        'exchange': order.exchange, 'symbol': order.symbol,
        'side': order.side, 'order_type': order.order_type,
        'qty': order.filled_qty, 'entry_price': order.avg_fill_price or '',
        'exit_price': '', 'trade_pnl': '',
        'realized_pnl': round(cumulative, 6), 'portfolio_value': '',
        'is_paper': order.is_paper, 'slot_id': slot.id, 'notes': '',
    })


async def _csv_close(
    csv_writer, slot_manager, slot, order,
    pnl: float, entry_price: float = 0.0, event_type: str = 'close',
) -> None:
    cumulative = sum(s.realized_pnl for s in slot_manager.get_all_slots())
    await csv_writer.enqueue_event({
        'timestamp': _ts(order.updated_at), 'event_type': event_type,
        'exchange': order.exchange, 'symbol': order.symbol,
        'side': order.side, 'order_type': order.order_type,
        'qty': order.filled_qty, 'entry_price': entry_price or '',
        'exit_price': order.avg_fill_price or '', 'trade_pnl': round(pnl, 6),
        'realized_pnl': round(cumulative, 6), 'portfolio_value': '',
        'is_paper': order.is_paper, 'slot_id': slot.id, 'notes': '',
    })


# ─────────────────────────────────────────────────────────────────────────────
# Position lifecycle helpers (One-Way Mode netting)
# ─────────────────────────────────────────────────────────────────────────────

async def _open_new(order, slot, sm, pe, cw, sp) -> None:
    """Open a brand-new position on `slot`."""
    slot.status = 'active'
    if order.is_paper:
        slot.position          = await pe.open_position(slot.id, order, slot.side)
        slot.position.leverage = slot.leverage
    slot.orders.append(order)
    await sm.update_slot(slot)
    await _csv_open(cw, sm, slot, order)
    sp.log(f"Opened {slot.side} {slot.symbol} qty={order.filled_qty:g}",
           exchange=slot.exchange, symbol=slot.symbol)


async def _scale_in(order, new_slot, existing, sm, pe, cw, sp) -> None:
    """Add to an existing same-side position (average entry)."""
    if order.is_paper:
        existing.position = await pe.add_to_position(existing.id, order)
    existing.orders.append(order)
    await sm.update_slot(existing)
    # Discard the temporary tracking slot
    if new_slot.id != existing.id:
        await sm.close_slot(new_slot.id, 0.0)
    await _csv_open(cw, sm, existing, order)
    sp.log(f"Scale-in {existing.symbol} "
           f"avg={existing.position.entry_price:.6g}",
           exchange=existing.exchange, symbol=existing.symbol)


async def _reduce_or_flip(order, new_slot, opp_slot, sm, pe, cw, sp) -> None:
    """
    Opposite-side order netting:
      order_qty < opp_qty  → partial close
      order_qty = opp_qty  → full close
      order_qty > opp_qty  → full close + flip to new side with remainder
    """
    fill_price   = order.avg_fill_price or 0.0
    order_qty    = order.filled_qty
    opp_qty      = opp_slot.position.qty
    close_qty    = min(order_qty, opp_qty)
    entry_price  = opp_slot.position.entry_price
    pnl          = _partial_pnl(opp_slot, fill_price, close_qty)
    is_full      = close_qty >= opp_qty - 1e-10

    if is_full:
        if order.is_paper:
            await pe.close_position(opp_slot.id)
        await sm.close_slot(opp_slot.id, pnl)
        await _csv_close(cw, sm, opp_slot, order, pnl, entry_price, 'close')
        sp.log(f"Closed {opp_slot.side} {opp_slot.symbol} pnl={pnl:+.4f}",
               exchange=opp_slot.exchange, symbol=opp_slot.symbol)
    else:
        if order.is_paper:
            upd, _ = await pe.partial_close_position(opp_slot.id, close_qty)
            opp_slot.position = upd
        opp_slot.realized_pnl = round(
            getattr(opp_slot, 'realized_pnl', 0.0) + pnl, 6
        )
        await sm.update_slot(opp_slot)
        await _csv_close(cw, sm, opp_slot, order, pnl, entry_price, 'partial_close')
        sp.log(f"Reduced {opp_slot.side} {opp_slot.symbol} "
               f"by {close_qty:g} pnl={pnl:+.4f}",
               exchange=opp_slot.exchange, symbol=opp_slot.symbol)

    remainder = round(order_qty - close_qty, 8)
    if remainder > 1e-10:
        # Flip: open new position on new side
        flip_order            = deepcopy(order)
        flip_order.qty        = remainder
        flip_order.filled_qty = remainder
        flip_order.slot_id    = new_slot.id
        await _open_new(flip_order, new_slot, sm, pe, cw, sp)
        sp.log(f"Flipped → {new_slot.side} {new_slot.symbol} qty={remainder:g}",
               exchange=new_slot.exchange, symbol=new_slot.symbol)
    else:
        # No new position — discard new_slot
        if new_slot.id != opp_slot.id:
            await sm.close_slot(new_slot.id, 0.0)


async def _process_filled_entry(
    order, slot, sm, pe, cw, sp,
) -> None:
    """
    Route a filled entry order to the right position lifecycle action.
    Implements Binance One-Way-Mode netting behaviour.
    """
    exch     = slot.exchange
    sym      = slot.symbol
    new_side = slot.side
    opp_side = 'short' if new_side == 'long' else 'long'

    existing_same = _find_existing_position(sm, exch, sym, new_side)
    existing_opp  = _find_existing_position(sm, exch, sym, opp_side)

    if existing_opp:
        await _reduce_or_flip(order, slot, existing_opp, sm, pe, cw, sp)
    elif existing_same:
        await _scale_in(order, slot, existing_same, sm, pe, cw, sp)
    else:
        await _open_new(order, slot, sm, pe, cw, sp)


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
            new_slot = (TradeSlot.model_validate(raw_slot)
                        if isinstance(raw_slot, dict) else raw_slot)
        except Exception as exc:
            logger.error("CMD_OPEN_SLOT: invalid slot: %s", exc)
            return

        entry = new_slot.entries[0] if new_slot.entries else None
        if not entry:
            logger.error("CMD_OPEN_SLOT: no entry leg")
            return

        slot  = await slot_manager.create_slot(new_slot)
        order = _build_order(slot.exchange, slot.symbol, slot.side, entry, slot.id)
        order = await order_manager.place_order(order, slot)
        slot.orders.append(order)

        if order.status == 'filled':
            await _process_filled_entry(
                order, slot, slot_manager, paper_engine, csv_writer, state_publisher
            )
        elif order.status in ('open', 'pending', 'working'):
            slot.status = 'working'
            await slot_manager.update_slot(slot)

        state_publisher.log(
            f"Order placed {slot.symbol} ({entry.order_type})",
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
                exchange=slot.exchange, symbol=slot.symbol,
                side='sell' if slot.side == 'long' else 'buy',
                order_type='market', qty=slot.position.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)
            state_publisher.log(f"Closed {slot.symbol} pnl={pnl:+.4f}",
                                exchange=slot.exchange, symbol=slot.symbol)
        else:
            for o in slot.orders:
                if o.status in ('open', 'pending', 'working'):
                    await order_manager.cancel_order(o)
            await slot_manager.close_slot(slot.id, 0.0)

    # ── Partial close ─────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_PARTIAL_CLOSE_SLOT:
        qty  = float(cmd.get('qty', 0))
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot or not slot.position or qty <= 0:
            return
        actual_qty  = min(qty, slot.position.qty)
        entry_price = slot.position.entry_price
        order = Order(
            exchange=slot.exchange, symbol=slot.symbol,
            side='sell' if slot.side == 'long' else 'buy',
            order_type='market', qty=actual_qty, slot_id=slot.id,
        )
        order = await order_manager.place_order(order, slot)
        slot.orders.append(order)
        pnl = _partial_pnl(slot, order.avg_fill_price or 0.0, actual_qty)

        if order.is_paper:
            upd, _ = await paper_engine.partial_close_position(slot.id, actual_qty)
            if upd is None:
                await slot_manager.close_slot(slot.id, pnl)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, 'close')
            else:
                slot.position     = upd
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await slot_manager.update_slot(slot)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, 'partial_close')
        else:
            slot.position.qty = round(slot.position.qty - actual_qty, 8)
            if slot.position.qty <= 1e-10:
                slot.position = None
                await slot_manager.close_slot(slot.id, pnl)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, 'close')
            else:
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await slot_manager.update_slot(slot)
                await _csv_close(csv_writer, slot_manager, slot, order, pnl,
                                 entry_price, 'partial_close')
        state_publisher.log(f"Partial close {actual_qty:g} {slot.symbol} pnl={pnl:+.4f}",
                            exchange=slot.exchange, symbol=slot.symbol)

    # ── Close all ─────────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_CLOSE_ALL:
        for slot in slot_manager.get_active_slots():
            if not slot.position:
                continue
            entry_price = slot.position.entry_price
            order = Order(
                exchange=slot.exchange, symbol=slot.symbol,
                side='sell' if slot.side == 'long' else 'buy',
                order_type='market', qty=slot.position.qty, slot_id=slot.id,
            )
            order = await order_manager.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await paper_engine.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await slot_manager.close_slot(slot.id, pnl)
            await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)

    # ── Update slot ───────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_UPDATE_SLOT:
        slot = slot_manager.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        if 'stop_price'   in cmd: slot.stop_price   = cmd['stop_price']
        if 'target_price' in cmd: slot.target_price = cmd['target_price']
        if 'pnl_target'   in cmd: slot.pnl_target   = cmd['pnl_target']
        await slot_manager.update_slot(slot)

    # ── Cancel order — also clears virtual stop/target from slot ─────────────
    elif action == redis_keys.CMD_CANCEL_ORDER:
        order_id = cmd.get('order_id', '')
        for slot in slot_manager.get_all_slots():
            for o in slot.orders:
                if o.id != order_id:
                    continue
                await order_manager.cancel_order(o)
                # Virtual stop/target orders → clear from slot
                if order_id.startswith('VSTOP-'):
                    slot.stop_price = None
                    await slot_manager.update_slot(slot)
                    state_publisher.log(f"Stop cleared {slot.symbol}",
                                        exchange=slot.exchange, symbol=slot.symbol)
                elif order_id.startswith('VTGT-'):
                    slot.target_price = None
                    await slot_manager.update_slot(slot)
                    state_publisher.log(f"Target cleared {slot.symbol}",
                                        exchange=slot.exchange, symbol=slot.symbol)
                else:
                    state_publisher.log(f"Order cancelled {o.symbol}",
                                        exchange=o.exchange, symbol=o.symbol)
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
            alert = (Alert.model_validate(raw_alert)
                     if isinstance(raw_alert, dict) else raw_alert)
            slot_manager.add_alert(alert)
            await _sub_market_data(redis, alert.exchange, alert.symbol)
            state_publisher.log(
                f"Alert set {alert.exchange} {alert.symbol}",
                exchange=alert.exchange, symbol=alert.symbol,
            )
        except Exception as exc:
            logger.error("CMD_SET_ALERT: %s", exc)

    elif action == redis_keys.CMD_DELETE_ALERT:
        slot_manager.delete_alert(cmd.get('alert_id', ''))

    elif action == redis_keys.CMD_RESET_ALERTS:
        slot_manager.clear_triggered_alerts()
        state_publisher.log("Triggered alerts cleared")

    elif action == redis_keys.CMD_CLEAR_ALL_ALERTS:
        slot_manager.clear_all_alerts()
        state_publisher.log("All alerts cleared")

    # ── Live mode ─────────────────────────────────────────────────────────────
    elif action == redis_keys.CMD_SET_LIVE_MODE:
        live = bool(cmd.get('live', False))
        order_manager.set_live_mode(live)
        state_publisher.log(f"Mode → {'LIVE' if live else 'PAPER'}",
                            level='warning' if live else 'info')


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-exit callback
# ─────────────────────────────────────────────────────────────────────────────

async def _on_trigger_exit(
    slot_id, reason, slot_manager, order_manager, state_publisher, csv_writer, notifier,
) -> None:
    slot = slot_manager.get_slot(slot_id)
    if not slot or not slot.position:
        return
    entry_price = slot.position.entry_price
    order = Order(
        exchange=slot.exchange, symbol=slot.symbol,
        side='sell' if slot.side == 'long' else 'buy',
        order_type='market', qty=slot.position.qty, slot_id=slot.id,
    )
    order = await order_manager.place_order(order, slot)
    slot.orders.append(order)
    if order.is_paper:
        await order_manager.paper.close_position(slot_id)
    pnl = _pnl(slot, order.avg_fill_price or 0.0)
    await slot_manager.close_slot(slot_id, pnl)
    await _csv_close(csv_writer, slot_manager, slot, order, pnl, entry_price)
    state_publisher.log(f"[{reason}] {slot.symbol}  pnl={pnl:+.4f}",
                        exchange=slot.exchange, symbol=slot.symbol)
    await notifier.send(f"[{reason.upper()}] {slot.symbol} | PnL {pnl:+.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# Paper limit-fill checker
# ─────────────────────────────────────────────────────────────────────────────

async def _paper_fill_checker(
    slot_manager, order_manager, state_publisher, csv_writer, paper_engine,
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
                for order in await paper_engine.check_pending_fills(exchange, symbol, price):
                    slot = slot_manager.get_slot(order.slot_id)
                    if not slot or slot.status != 'working':
                        continue
                    await _process_filled_entry(
                        order, slot,
                        slot_manager, paper_engine, csv_writer, state_publisher,
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("paper_fill_checker: %s", exc)
        await asyncio.sleep(settings.PAPER_FILL_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Command loop
# ─────────────────────────────────────────────────────────────────────────────

async def _command_loop(
    redis, slot_manager, order_manager, state_publisher, paper_engine, csv_writer,
) -> None:
    logger.info("Command loop → %s", redis_keys.COMMAND_QUEUE)
    while True:
        try:
            result = await redis.brpop(redis_keys.COMMAND_QUEUE, timeout=1)
            if result is None:
                continue
            _, raw = result
            await _handle(json.loads(raw), slot_manager, order_manager,
                          state_publisher, paper_engine, redis, csv_writer)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Command error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s  %(name)-24s  %(levelname)s  %(message)s')
    redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True, max_connections=20,
    )
    live_mode = os.getenv('LIVE_MODE', 'false').lower() == 'true'
    keys      = load_keys()
    adapters: dict[str, BaseExchangeAdapter] = {}
    for exch in settings.SUPPORTED_EXCHANGES:
        ek = keys.get(exch, {})
        try:
            adapters[exch] = get_adapter(
                exchange=exch, api_key=ek.get('api_key', ''),
                api_secret=ek.get('api_secret', ''),
            )
        except Exception as exc:
            logger.warning("Adapter skip [%s]: %s", exch, exc)

    paper_engine  = PaperEngine(redis)
    csv_writer    = CSVWriter()
    slot_manager  = SlotManager(redis)
    state_pub     = StatePublisher(redis, slot_manager)
    notifier      = Notifier()
    order_manager = OrderManager(
        adapters=adapters, paper_engine=paper_engine,
        csv_writer=csv_writer, state_publisher=state_pub, live_mode=live_mode,
    )

    await csv_writer.start()
    await slot_manager.load()
    if live_mode:
        await reconcile(adapters, slot_manager, state_pub)
    else:
        state_pub.log("Paper mode — reconciliation skipped")

    on_exit = lambda sid, reason: _on_trigger_exit(
        sid, reason, slot_manager, order_manager, state_pub, csv_writer, notifier,
    )

    async def _on_alert_fired(alert) -> None:
        price     = alert.upper if alert.upper is not None else alert.lower
        direction = '▲' if alert.upper is not None else '▼'
        msg       = f"[ALERT] {alert.symbol} {direction}{f'{price:g}' if price else '—'}"
        state_pub.log(msg, level='warning', exchange=alert.exchange, symbol=alert.symbol)
        await notifier.send(msg)

    trigger = TriggerEngine(
        redis_client=redis, slot_manager=slot_manager,
        on_exit=on_exit, on_alert=_on_alert_fired,
        paper_engine=paper_engine,
    )
    await trigger.start()
    await state_pub.start()

    asyncio.create_task(_command_loop(
        redis, slot_manager, order_manager, state_pub, paper_engine, csv_writer,
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

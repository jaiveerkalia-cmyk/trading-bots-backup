"""
Trading Engine — main event loop.

Key fixes in this version:
- VSTOP/VTGT cancel handled BEFORE scanning slot.orders (they're virtual).
- Cancelled working orders: slot is auto-closed when no working orders remain.
- Paper pending limits are restored from persisted slot state on startup.
- _scale_in propagates stop/target from new order to existing position.
- close_reason passed through to CSV for all exit paths.
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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pnl(slot, exit_price: float) -> float:
    if not slot.position:
        return 0.0
    fees = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_fee_rt = fees['maker'] if (slot.entries and slot.entries[0].order_type == 'limit') else fees['taker']
    entry = slot.position.entry_price
    qty   = slot.position.qty
    gross = ((exit_price - entry) * qty if slot.side == 'long'
             else (entry - exit_price) * qty)
    return round(gross - entry * qty * entry_fee_rt - exit_price * qty * fees['taker'], 6)


def _partial_pnl(slot, exit_price: float, qty: float) -> float:
    if not slot.position or qty <= 0:
        return 0.0
    fees = settings.EXCHANGE_FEES.get(slot.exchange, {'maker': 0.001, 'taker': 0.001})
    entry_fee_rt = fees['maker'] if (slot.entries and slot.entries[0].order_type == 'limit') else fees['taker']
    entry = slot.position.entry_price
    gross = ((exit_price - entry) * qty if slot.side == 'long'
             else (entry - exit_price) * qty)
    return round(gross - entry * qty * entry_fee_rt - exit_price * qty * fees['taker'], 6)


def _ts(dt) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


def _build_order(exchange, symbol, side, entry, slot_id) -> Order:
    if entry.order_type == 'stop_limit':
        order_price, order_stop = None, entry.price
    else:
        order_price = entry.price if (entry.price and entry.price > 0) else None
        order_stop  = None
    return Order(
        exchange=exchange, symbol=symbol,
        side='buy' if side == 'long' else 'sell',
        order_type=entry.order_type, qty=entry.qty,
        price=order_price, stop_price=order_stop,
        slot_id=slot_id, reference_price=entry.reference_price,
    )


def _find_existing_position(sm, exchange, symbol, side) -> Optional[TradeSlot]:
    for s in sm.get_active_slots():
        if s.exchange == exchange and s.symbol == symbol and s.side == side and s.position:
            return s
    return None


async def _sub_md(redis: aioredis.Redis, exchange: str, symbol: str,
                  streams: list[str] | None = None) -> None:
    try:
        await redis.publish(_MD_CONTROL, json.dumps({
            'cmd': 'subscribe', 'exchange': exchange,
            'symbol': symbol, 'streams': streams or ['ticker'],
        }))
    except Exception as e:
        logger.warning("MD subscribe error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _csv_open(cw, sm, slot, order) -> None:
    cumulative = sum(s.realized_pnl for s in sm.get_all_slots())
    await cw.enqueue_event({
        'timestamp':      _ts(order.updated_at),
        'event_type':     'open',
        'exchange':       order.exchange,
        'symbol':         order.symbol,
        'side':           slot.side,
        'qty':            order.filled_qty,
        'leverage':       getattr(slot, 'leverage', 1),
        'entry_price':    order.avg_fill_price or '',
        'exit_price':     '',
        'trade_pnl':      '',
        'funding_pnl':    '',
        'cumulative_pnl': round(cumulative, 6),
        'close_reason':   '',
        'is_paper':       order.is_paper,
        'slot_id':        slot.id,
    })
    await cw.enqueue_order_fill({
        'timestamp':       _ts(order.updated_at),
        'exchange':        order.exchange,
        'symbol':          order.symbol,
        'side':            order.side,
        'order_type':      order.order_type,
        'filled_qty':      order.filled_qty,
        'avg_fill_price':  order.avg_fill_price or '',
        'is_paper':        order.is_paper,
        'slot_id':         slot.id,
        'order_id':        order.id,
    })


async def _csv_close(cw, sm, slot, order, pnl, entry_price=0.0,
                     event_type='close', close_reason='manual') -> None:
    cumulative = sum(s.realized_pnl for s in sm.get_all_slots())
    fp = getattr(slot.position, 'funding_pnl', 0.0) if slot.position else 0.0
    await cw.enqueue_event({
        'timestamp':      _ts(order.updated_at),
        'event_type':     event_type,
        'exchange':       order.exchange,
        'symbol':         order.symbol,
        'side':           slot.side,
        'qty':            order.filled_qty,
        'leverage':       getattr(slot, 'leverage', 1),
        'entry_price':    entry_price or '',
        'exit_price':     order.avg_fill_price or '',
        'trade_pnl':      round(pnl, 6),
        'funding_pnl':    round(fp, 8),
        'cumulative_pnl': round(cumulative, 6),
        'close_reason':   close_reason,
        'is_paper':       order.is_paper,
        'slot_id':        slot.id,
    })
    await cw.enqueue_order_fill({
        'timestamp':      _ts(order.updated_at),
        'exchange':       order.exchange,
        'symbol':         order.symbol,
        'side':           order.side,
        'order_type':     order.order_type,
        'filled_qty':     order.filled_qty,
        'avg_fill_price': order.avg_fill_price or '',
        'is_paper':       order.is_paper,
        'slot_id':        slot.id,
        'order_id':       order.id,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Position lifecycle helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _open_new(order, slot, sm, pe, cw, sp) -> None:
    slot.status = 'active'
    if order.is_paper:
        slot.position          = await pe.open_position(slot.id, order, slot.side)
        slot.position.leverage = slot.leverage
    slot.orders.append(order)
    await sm.update_slot(slot)
    await _csv_open(cw, sm, slot, order)
    sp.log(f"Opened {slot.side} {slot.symbol} qty={order.filled_qty:g} "
           f"@ {order.avg_fill_price}",
           exchange=slot.exchange, symbol=slot.symbol)


async def _scale_in(order, new_slot, existing, sm, pe, cw, sp) -> None:
    if order.is_paper:
        existing.position = await pe.add_to_position(existing.id, order)
    existing.orders.append(order)

    # Propagate stop/target from new order if not already set on existing
    if new_slot.stop_price and not existing.stop_price:
        existing.stop_price = new_slot.stop_price
    if new_slot.target_price and not existing.target_price:
        existing.target_price = new_slot.target_price
    if new_slot.pnl_target is not None and existing.pnl_target is None:
        existing.pnl_target = new_slot.pnl_target

    await sm.update_slot(existing)
    if new_slot.id != existing.id:
        await sm.close_slot(new_slot.id, 0.0)
    await _csv_open(cw, sm, existing, order)
    sp.log(f"Scale-in {existing.symbol} "
           f"avg={existing.position.entry_price:.6g}",
           exchange=existing.exchange, symbol=existing.symbol)


async def _reduce_or_flip(order, new_slot, opp_slot, sm, pe, cw, sp) -> None:
    fill_price  = order.avg_fill_price or 0.0
    order_qty   = order.filled_qty
    opp_qty     = opp_slot.position.qty
    close_qty   = min(order_qty, opp_qty)
    entry_price = opp_slot.position.entry_price
    pnl         = _partial_pnl(opp_slot, fill_price, close_qty)
    is_full     = close_qty >= opp_qty - 1e-10

    if is_full:
        if order.is_paper:
            await pe.close_position(opp_slot.id)
        await sm.close_slot(opp_slot.id, pnl)
        await _csv_close(cw, sm, opp_slot, order, pnl, entry_price,
                         'close', 'opposite_order')
        sp.log(f"Closed {opp_slot.side} {opp_slot.symbol} pnl={pnl:+.4f}",
               exchange=opp_slot.exchange, symbol=opp_slot.symbol)
    else:
        if order.is_paper:
            upd, _ = await pe.partial_close_position(opp_slot.id, close_qty)
            opp_slot.position = upd
        opp_slot.realized_pnl = round(getattr(opp_slot, 'realized_pnl', 0.0) + pnl, 6)
        await sm.update_slot(opp_slot)
        await _csv_close(cw, sm, opp_slot, order, pnl, entry_price,
                         'partial_close', 'opposite_order')
        sp.log(f"Reduced {opp_slot.side} {opp_slot.symbol} "
               f"by {close_qty:g} pnl={pnl:+.4f}",
               exchange=opp_slot.exchange, symbol=opp_slot.symbol)

    remainder = round(order_qty - close_qty, 8)
    if remainder > 1e-10:
        flip_order            = deepcopy(order)
        flip_order.qty        = remainder
        flip_order.filled_qty = remainder
        flip_order.slot_id    = new_slot.id
        await _open_new(flip_order, new_slot, sm, pe, cw, sp)
    else:
        if new_slot.id != opp_slot.id:
            await sm.close_slot(new_slot.id, 0.0)


async def _process_filled_entry(order, slot, sm, pe, cw, sp) -> None:
    exch, sym = slot.exchange, slot.symbol
    existing_same = _find_existing_position(sm, exch, sym, slot.side)
    existing_opp  = _find_existing_position(sm, exch, sym,
                                            'short' if slot.side == 'long' else 'long')
    if existing_opp:
        await _reduce_or_flip(order, slot, existing_opp, sm, pe, cw, sp)
    elif existing_same:
        await _scale_in(order, slot, existing_same, sm, pe, cw, sp)
    else:
        await _open_new(order, slot, sm, pe, cw, sp)


# ─────────────────────────────────────────────────────────────────────────────
# Command handler
# ─────────────────────────────────────────────────────────────────────────────

async def _handle(cmd, sm, om, sp, pe, redis, cw) -> None:
    action = cmd.get('type')

    if action == redis_keys.CMD_OPEN_SLOT:
        raw = cmd.get('slot', {})
        try:
            new_slot = TradeSlot.model_validate(raw) if isinstance(raw, dict) else raw
        except Exception as exc:
            logger.error("CMD_OPEN_SLOT: %s", exc)
            return
        entry = new_slot.entries[0] if new_slot.entries else None
        if not entry:
            return
        slot  = await sm.create_slot(new_slot)
        order = _build_order(slot.exchange, slot.symbol, slot.side, entry, slot.id)
        order = await om.place_order(order, slot)
        slot.orders.append(order)
        if order.status == 'filled':
            await _process_filled_entry(order, slot, sm, pe, cw, sp)
        elif order.status in ('open', 'pending', 'working'):
            slot.status = 'working'
            await sm.update_slot(slot)
        sp.log(f"Order placed {slot.symbol} ({entry.order_type})",
               exchange=slot.exchange, symbol=slot.symbol)

    elif action == redis_keys.CMD_CLOSE_SLOT:
        slot = sm.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        if slot.position:
            entry_price = slot.position.entry_price
            order = Order(exchange=slot.exchange, symbol=slot.symbol,
                          side='sell' if slot.side == 'long' else 'buy',
                          order_type='market', qty=slot.position.qty, slot_id=slot.id)
            order = await om.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await pe.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await sm.close_slot(slot.id, pnl)
            await _csv_close(cw, sm, slot, order, pnl, entry_price, 'close', 'manual')
            sp.log(f"Closed {slot.symbol} pnl={pnl:+.4f}",
                   exchange=slot.exchange, symbol=slot.symbol)
        else:
            for o in slot.orders:
                if o.status in ('open', 'pending', 'working'):
                    await om.cancel_order(o)
                    o.status = 'cancelled'
            await sm.close_slot(slot.id, 0.0)

    elif action == redis_keys.CMD_PARTIAL_CLOSE_SLOT:
        qty  = float(cmd.get('qty', 0))
        slot = sm.get_slot(cmd.get('slot_id', ''))
        if not slot or not slot.position or qty <= 0:
            return
        actual     = min(qty, slot.position.qty)
        ep         = slot.position.entry_price
        order      = Order(exchange=slot.exchange, symbol=slot.symbol,
                           side='sell' if slot.side == 'long' else 'buy',
                           order_type='market', qty=actual, slot_id=slot.id)
        order      = await om.place_order(order, slot)
        slot.orders.append(order)
        pnl        = _partial_pnl(slot, order.avg_fill_price or 0.0, actual)
        if order.is_paper:
            upd, _ = await pe.partial_close_position(slot.id, actual)
            if upd is None:
                await sm.close_slot(slot.id, pnl)
                await _csv_close(cw, sm, slot, order, pnl, ep, 'close', 'partial')
            else:
                slot.position = upd
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await sm.update_slot(slot)
                await _csv_close(cw, sm, slot, order, pnl, ep, 'partial_close', 'partial')
        else:
            slot.position.qty = round(slot.position.qty - actual, 8)
            if slot.position.qty <= 1e-10:
                slot.position = None
                await sm.close_slot(slot.id, pnl)
                await _csv_close(cw, sm, slot, order, pnl, ep, 'close', 'partial')
            else:
                slot.realized_pnl = round(getattr(slot, 'realized_pnl', 0.0) + pnl, 6)
                await sm.update_slot(slot)
                await _csv_close(cw, sm, slot, order, pnl, ep, 'partial_close', 'partial')
        sp.log(f"Partial close {actual:g} {slot.symbol} pnl={pnl:+.4f}",
               exchange=slot.exchange, symbol=slot.symbol)

    elif action == redis_keys.CMD_CLOSE_ALL:
        for slot in sm.get_active_slots():
            if not slot.position:
                continue
            ep    = slot.position.entry_price
            order = Order(exchange=slot.exchange, symbol=slot.symbol,
                          side='sell' if slot.side == 'long' else 'buy',
                          order_type='market', qty=slot.position.qty, slot_id=slot.id)
            order = await om.place_order(order, slot)
            slot.orders.append(order)
            if order.is_paper:
                await pe.close_position(slot.id)
            pnl = _pnl(slot, order.avg_fill_price or 0.0)
            await sm.close_slot(slot.id, pnl)
            await _csv_close(cw, sm, slot, order, pnl, ep, 'close', 'close_all')

    elif action == redis_keys.CMD_UPDATE_SLOT:
        slot = sm.get_slot(cmd.get('slot_id', ''))
        if not slot:
            return
        if 'stop_price'   in cmd: slot.stop_price   = cmd['stop_price']
        if 'target_price' in cmd: slot.target_price = cmd['target_price']
        if 'pnl_target'   in cmd: slot.pnl_target   = cmd['pnl_target']
        await sm.update_slot(slot)

    elif action == redis_keys.CMD_CANCEL_ORDER:
        order_id = cmd.get('order_id', '')
        sid      = cmd.get('slot_id', '')

        # ── Virtual stop/target orders (not in slot.orders) ──────────────────
        if order_id.startswith('VSTOP-') or order_id.startswith('VTGT-'):
            slot = sm.get_slot(sid)
            if slot:
                if order_id.startswith('VSTOP-'):
                    slot.stop_price = None
                    sp.log(f"Stop cleared {slot.symbol}",
                           exchange=slot.exchange, symbol=slot.symbol)
                else:
                    slot.target_price = None
                    sp.log(f"Target cleared {slot.symbol}",
                           exchange=slot.exchange, symbol=slot.symbol)
                await sm.update_slot(slot)
            return

        # ── Real working orders ───────────────────────────────────────────────
        for slot in sm.get_all_slots():
            for o in slot.orders:
                if o.id != order_id:
                    continue
                await om.cancel_order(o)
                o.status = 'cancelled'
                sp.log(f"Order cancelled {o.symbol}",
                       exchange=o.exchange, symbol=o.symbol)
                # Auto-close slot when no working orders remain
                still_working = [x for x in slot.orders
                                 if x.status in ('working', 'pending')]
                if not still_working and not slot.position:
                    await sm.close_slot(slot.id, 0.0)
                    sp.log(f"Slot closed after cancel {slot.symbol}",
                           exchange=slot.exchange, symbol=slot.symbol)
                else:
                    await sm.update_slot(slot)
                return

    elif action == redis_keys.CMD_MODIFY_ORDER:
        order_id  = cmd.get('order_id', '')
        new_price = cmd.get('new_price')
        new_qty   = cmd.get('new_qty')
        await pe.modify_pending(order_id,
                                float(new_price) if new_price else 0.0,
                                float(new_qty)   if new_qty   else 0.0)

    elif action == redis_keys.CMD_SET_ALERT:
        raw_alert = cmd.get('alert', {})
        try:
            alert = Alert.model_validate(raw_alert) if isinstance(raw_alert, dict) else raw_alert
            await sm.add_alert(alert)
            await _sub_md(redis, alert.exchange, alert.symbol)
            # Subscribe to candle data for 1m/5m candle-close alerts
            period = getattr(alert, 'period', 'current')
            if period in ('1m', '5m'):
                await _sub_md(redis, alert.exchange, alert.symbol,
                              streams=['ticker', f'candles:{period}'])
            sp.log(f"Alert set {alert.exchange} {alert.symbol} ({period})",
                   exchange=alert.exchange, symbol=alert.symbol)
        except Exception as exc:
            logger.error("CMD_SET_ALERT: %s", exc)

    elif action == redis_keys.CMD_DELETE_ALERT:
        sm.delete_alert(cmd.get('alert_id', ''))

    elif action == redis_keys.CMD_RESET_ALERTS:
        sm.clear_triggered_alerts()
        sp.log("Triggered alerts cleared")

    elif action == redis_keys.CMD_CLEAR_ALL_ALERTS:
        sm.clear_all_alerts()
        sp.log("All alerts cleared")

    elif action == redis_keys.CMD_SET_LIVE_MODE:
        live = bool(cmd.get('live', False))
        om.set_live_mode(live)
        sp.log(f"Mode → {'LIVE' if live else 'PAPER'}",
               level='warning' if live else 'info')


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-exit callback
# ─────────────────────────────────────────────────────────────────────────────

async def _on_trigger_exit(slot_id, reason, sm, om, sp, cw, notifier) -> None:
    slot = sm.get_slot(slot_id)
    if not slot or not slot.position:
        return
    ep    = slot.position.entry_price
    order = Order(exchange=slot.exchange, symbol=slot.symbol,
                  side='sell' if slot.side == 'long' else 'buy',
                  order_type='market', qty=slot.position.qty, slot_id=slot.id)
    order = await om.place_order(order, slot)
    slot.orders.append(order)
    if order.is_paper:
        await om.paper.close_position(slot_id)
    pnl = _pnl(slot, order.avg_fill_price or 0.0)
    await sm.close_slot(slot_id, pnl)
    await _csv_close(cw, sm, slot, order, pnl, ep, 'close', reason)
    sp.log(f"[{reason}] {slot.symbol} pnl={pnl:+.4f}",
           exchange=slot.exchange, symbol=slot.symbol)
    await notifier.send(f"[{reason.upper()}] {slot.symbol} | PnL {pnl:+.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# Paper fill checker
# ─────────────────────────────────────────────────────────────────────────────

async def _paper_fill_checker(sm, om, sp, cw, pe) -> None:
    while True:
        try:
            pairs = {(s.exchange, s.symbol) for s in sm.get_all_slots()
                     if s.status == 'working'}
            for exch, sym in pairs:
                price = await pe._last_tick(exch, sym)
                if price <= 0:
                    continue
                for order in await pe.check_pending_fills(exch, sym, price):
                    slot = sm.get_slot(order.slot_id)
                    if not slot or slot.status != 'working':
                        continue
                    await _process_filled_entry(order, slot, sm, pe, cw, sp)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("paper_fill_checker: %s", exc)
        await asyncio.sleep(settings.PAPER_FILL_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Command loop
# ─────────────────────────────────────────────────────────────────────────────

async def _command_loop(redis, sm, om, sp, pe, cw) -> None:
    logger.info("Command loop → %s", redis_keys.COMMAND_QUEUE)
    while True:
        try:
            result = await redis.brpop(redis_keys.COMMAND_QUEUE, timeout=1)
            if result is None:
                continue
            _, raw = result
            await _handle(json.loads(raw), sm, om, sp, pe, redis, cw)
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

    # Restore paper pending limits for working slots (survived restart)
    restored = 0
    for slot in slot_manager.get_all_slots():
        if slot.status == 'working' and getattr(slot, 'is_paper', True):
            for o in slot.orders:
                if o.status == 'working':
                    async with paper_engine._lock:
                        paper_engine._pending_limits.append({
                            'order': o, 'exchange': o.exchange, 'symbol': o.symbol,
                        })
                    restored += 1
    if restored:
        logger.info("Restored %d pending paper limit(s)", restored)

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
        period    = getattr(alert, 'period', 'current')
        msg       = (f"[ALERT {period.upper()}] {alert.symbol} "
                     f"{direction}{f'{price:g}' if price else '—'}")
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

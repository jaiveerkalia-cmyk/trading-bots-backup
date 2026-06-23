"""
Startup reconciliation — compare local state with exchange truth.
Flags mismatches, saves a snapshot. Does not auto-correct — human reviews.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from exchanges.base import BaseExchangeAdapter
from common import settings

if TYPE_CHECKING:
    from trading_engine.trade_slot import SlotManager
    from trading_engine.state_publisher import StatePublisher

logger = logging.getLogger('reconciliation')


async def reconcile(
    adapters:        dict[str, BaseExchangeAdapter],
    slot_manager:    'SlotManager',
    state_publisher: 'StatePublisher',
) -> None:
    logger.info("Starting reconciliation...")
    issues: list[str] = []

    for exchange, adapter in adapters.items():
        try:
            live_pos    = await adapter.get_positions()
            live_orders = await adapter.get_open_orders()
        except Exception as e:
            logger.error(f"Reconciliation: failed to fetch {exchange} data: {e}")
            continue

        local_slots = [s for s in slot_manager.get_active_slots()
                       if s.exchange == exchange]

        # Live positions we don't track locally
        local_syms = {s.symbol for s in local_slots if s.position}
        for pos in live_pos:
            if pos.symbol not in local_syms:
                msg = (f"[{exchange}] Untracked live position: "
                       f"{pos.symbol} {pos.side} qty={pos.qty}")
                logger.warning(msg)
                issues.append(msg)
                state_publisher.log(msg, level='warning', exchange=exchange)

        # Local positions not found on exchange
        live_syms = {p.symbol for p in live_pos}
        for slot in local_slots:
            if slot.position and slot.symbol not in live_syms:
                msg = (f"[{exchange}] Local position not on exchange: "
                       f"{slot.symbol} slot={slot.id[:8]}")
                logger.warning(msg)
                issues.append(msg)
                state_publisher.log(msg, level='warning', exchange=exchange)

        # Open orders on exchange not tracked locally
        tracked_ids = {
            o.exchange_order_id for s in local_slots
            for o in s.orders
            if o.exchange_order_id and o.status == 'working'
        }
        for order in live_orders:
            if order.exchange_order_id not in tracked_ids:
                msg = (f"[{exchange}] Untracked open order: "
                       f"{order.exchange_order_id} {order.symbol} {order.side}")
                logger.warning(msg)
                issues.append(msg)
                state_publisher.log(msg, level='warning', exchange=exchange)

    if not issues:
        msg = "Reconciliation complete — no mismatches"
        logger.info(msg)
        state_publisher.log(msg, level='success')
    else:
        msg = f"Reconciliation: {len(issues)} mismatch(es) — review logs"
        logger.warning(msg)
        state_publisher.log(msg, level='warning')

    _save_snapshot(slot_manager)


def _save_snapshot(slot_manager: 'SlotManager') -> None:
    try:
        ts   = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        path = settings.STATE_SNAPSHOTS_DIR / f"snapshot_{ts}.json"
        path.write_text(
            json.dumps(
                [s.model_dump(mode='json') for s in slot_manager.get_all_slots()],
                separators=(',', ':'),
                default=str,
            )
        )
        # Keep only last 10 snapshots
        for old in sorted(settings.STATE_SNAPSHOTS_DIR.glob('snapshot_*.json'))[:-10]:
            old.unlink()
        logger.info(f"Snapshot saved: {path.name}")
    except Exception as e:
        logger.error(f"Snapshot error: {e}")

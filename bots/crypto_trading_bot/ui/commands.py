from __future__ import annotations
import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from common import redis_keys

logger = logging.getLogger('ui.commands')


async def _push(redis: aioredis.Redis, cmd: dict) -> None:
    try:
        await redis.rpush(
            redis_keys.COMMAND_QUEUE,
            json.dumps(cmd, separators=(',', ':'), default=str),
        )
    except Exception as e:
        logger.error(f"Command push error: {e}")


async def open_slot(redis, slot)                           -> None: await _push(redis, {'type': redis_keys.CMD_OPEN_SLOT,    'slot': slot})
async def close_slot(redis, slot_id)                       -> None: await _push(redis, {'type': redis_keys.CMD_CLOSE_SLOT,   'slot_id': slot_id})
async def partial_close_slot(
    redis, slot_id: str, qty: float,
    order_type: str = 'market',
    price: float | None = None,
) -> None:
    cmd: dict = {
        'type':       redis_keys.CMD_PARTIAL_CLOSE_SLOT,
        'slot_id':    slot_id,
        'qty':        qty,
        'order_type': order_type,
    }
    if price:
        cmd['price'] = price
    await _push(redis, cmd)
async def cancel_order(redis, slot_id, order_id)           -> None: await _push(redis, {'type': redis_keys.CMD_CANCEL_ORDER, 'slot_id': slot_id, 'order_id': order_id})
async def set_alert(redis, alert)                          -> None: await _push(redis, {'type': redis_keys.CMD_SET_ALERT,    'alert': alert})
async def delete_alert(redis, alert_id)                    -> None: await _push(redis, {'type': redis_keys.CMD_DELETE_ALERT, 'alert_id': alert_id})
async def close_all(redis)                                 -> None: await _push(redis, {'type': redis_keys.CMD_CLOSE_ALL})
async def reset_alerts(redis) -> None: await _push(redis, {'type': redis_keys.CMD_RESET_ALERTS})
async def set_live_mode(redis, live)                       -> None: await _push(redis, {'type': redis_keys.CMD_SET_LIVE_MODE, 'live': live})
async def modify_order(
    redis, slot_id: str, order_id: str,
    new_price: Optional[float] = None, new_qty: Optional[float] = None,
) -> None:
    await _push(redis, {
        'type': redis_keys.CMD_MODIFY_ORDER,
        'slot_id':   slot_id,
        'order_id':  order_id,
        'new_price': new_price,
        'new_qty':   new_qty,
    })

_UNSET = object()

async def update_slot(
    redis, slot_id: str,
    stop_price   = _UNSET,
    target_price = _UNSET,
    pnl_target   = _UNSET,
) -> None:
    cmd: dict = {'type': redis_keys.CMD_UPDATE_SLOT, 'slot_id': slot_id}
    if stop_price   is not _UNSET: cmd['stop_price']   = stop_price
    if target_price is not _UNSET: cmd['target_price'] = target_price
    if pnl_target   is not _UNSET: cmd['pnl_target']   = pnl_target
    await _push(redis, cmd)

async def clear_all_alerts(redis) -> None:
    await _push(redis, {'type': redis_keys.CMD_CLEAR_ALL_ALERTS})

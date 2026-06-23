from __future__ import annotations
import json
import logging
from typing import Any

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


async def open_slot(redis: aioredis.Redis, slot: dict)                     -> None: await _push(redis, {'type': redis_keys.CMD_OPEN_SLOT,    'slot': slot})
async def close_slot(redis: aioredis.Redis, slot_id: str)                  -> None: await _push(redis, {'type': redis_keys.CMD_CLOSE_SLOT,   'slot_id': slot_id})
async def cancel_order(redis: aioredis.Redis, slot_id: str, order_id: str) -> None: await _push(redis, {'type': redis_keys.CMD_CANCEL_ORDER, 'slot_id': slot_id, 'order_id': order_id})
async def set_alert(redis: aioredis.Redis, alert: dict)                    -> None: await _push(redis, {'type': redis_keys.CMD_SET_ALERT,    'alert': alert})
async def delete_alert(redis: aioredis.Redis, alert_id: str)               -> None: await _push(redis, {'type': redis_keys.CMD_DELETE_ALERT, 'alert_id': alert_id})
async def close_all(redis: aioredis.Redis)                                 -> None: await _push(redis, {'type': redis_keys.CMD_CLOSE_ALL})
async def set_live_mode(redis: aioredis.Redis, live: bool)                 -> None: await _push(redis, {'type': redis_keys.CMD_SET_LIVE_MODE, 'live': live})
async def update_slot(redis: aioredis.Redis, slot_id: str, **kw: Any)      -> None: await _push(redis, {'type': redis_keys.CMD_UPDATE_SLOT, 'slot_id': slot_id, **kw})

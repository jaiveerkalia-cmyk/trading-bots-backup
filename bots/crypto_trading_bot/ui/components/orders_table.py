from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState

_COLS = [
    {'name': 'exchange',   'label': 'Exch',   'field': 'exchange',   'align': 'left'},
    {'name': 'symbol',     'label': 'Symbol', 'field': 'symbol',     'align': 'left'},
    {'name': 'side',       'label': 'Side',   'field': 'side',       'align': 'center'},
    {'name': 'order_type', 'label': 'Type',   'field': 'order_type', 'align': 'center'},
    {'name': 'price',      'label': 'Price',  'field': 'price',      'align': 'right'},
    {'name': 'qty',        'label': 'Qty',    'field': 'qty',        'align': 'right'},
    {'name': 'filled_qty', 'label': 'Filled', 'field': 'filled_qty', 'align': 'right'},
    {'name': 'status',     'label': 'Status', 'field': 'status',     'align': 'center'},
    {'name': 'is_paper',   'label': 'Mode',   'field': 'is_paper',   'align': 'center'},
    {'name': 'id',         'label': '',       'field': 'id',         'align': 'center'},
]


def build(state: 'UIState', redis: aioredis.Redis) -> dict:
    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        ui.label('Open Orders').classes(
            'text-gray-300 font-medium text-sm mb-2'
        )
        table = ui.table(
            columns=_COLS, rows=[], row_key='id',
        ).props('dark dense flat').classes('w-full text-xs')

        table.add_slot('body-cell-side', """
            <q-td :props="props">
              <span :class="props.value==='buy'?'text-green-400':'text-red-400'">
                {{ props.value.toUpperCase() }}
              </span>
            </q-td>
        """)
        table.add_slot('body-cell-status', """
            <q-td :props="props">
              <q-badge :color="props.value==='working'?'blue':'grey'"
                       :label="props.value"/>
            </q-td>
        """)
        table.add_slot('body-cell-is_paper', """
            <q-td :props="props">
              <q-badge :color="props.value?'grey':'red'"
                       :label="props.value?'Paper':'Live'"/>
            </q-td>
        """)
        table.add_slot('body-cell-id', """
            <q-td :props="props">
              <q-btn flat dense size="xs" color="negative" label="Cancel"
                     @click="() => $emit('cancel_ord', props.row)"/>
            </q-td>
        """)

        async def on_cancel(e):
            row = e.args
            if not isinstance(row, dict):
                return
            await commands.cancel_order(
                redis, row.get('slot_id', ''), row.get('id', '')
            )
            ui.notify(
                f"Cancel sent for {row.get('symbol', '')} order",
                type='warning',
            )

        table.on('cancel_ord', on_cancel)

    def update():
        rows = []
        for o in state.open_orders:
            rows.append({
                'id':         o.get('id', ''),
                'exchange':   o.get('exchange', ''),
                'symbol':     o.get('symbol', ''),
                'side':       o.get('side', ''),
                'order_type': o.get('order_type', ''),
                'price':      f"{float(o.get('price') or 0):.4f}",
                'qty':        o.get('qty', 0),
                'filled_qty': o.get('filled_qty', 0),
                'status':     o.get('status', ''),
                'is_paper':   o.get('is_paper', True),
                'slot_id':    o.get('slot_id', ''),
            })
        table.rows = rows
        table.update()

    return {'update': update}

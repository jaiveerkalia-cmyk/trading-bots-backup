from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState

_COLS = [
    {'name': 'exchange',          'label': 'Exch',    'field': 'exchange',          'align': 'left'},
    {'name': 'symbol',            'label': 'Symbol',  'field': 'symbol',            'align': 'left'},
    {'name': 'side',              'label': 'Side',    'field': 'side',              'align': 'center'},
    {'name': 'entry_price',       'label': 'Entry',   'field': 'entry_price',       'align': 'right'},
    {'name': 'current_price',     'label': 'Mark',    'field': 'current_price',     'align': 'right'},
    {'name': 'qty',               'label': 'Qty',     'field': 'qty',               'align': 'right'},
    {'name': 'leverage',          'label': 'Lev',     'field': 'leverage',          'align': 'center'},
    {'name': 'unrealized_pnl',    'label': 'PnL',     'field': 'unrealized_pnl',    'align': 'right'},
    {'name': 'liquidation_price', 'label': 'Liq',     'field': 'liquidation_price', 'align': 'right'},
    {'name': 'funding_rate',      'label': 'Funding', 'field': 'funding_rate',      'align': 'right'},
    {'name': 'is_paper',          'label': 'Mode',    'field': 'is_paper',          'align': 'center'},
    {'name': 'slot_id',           'label': '',        'field': 'slot_id',           'align': 'center'},
]


def build(state: 'UIState', redis: aioredis.Redis) -> dict:
    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        ui.label('Open Positions').classes(
            'text-gray-300 font-medium text-sm mb-2'
        )
        table = ui.table(
            columns=_COLS, rows=[], row_key='slot_id',
        ).props('dark dense flat').classes('w-full text-xs')

        table.add_slot('body-cell-side', """
            <q-td :props="props">
              <span :class="props.value==='long'
                    ?'text-green-400 font-bold':'text-red-400 font-bold'">
                {{ props.value.toUpperCase() }}
              </span>
            </q-td>
        """)
        table.add_slot('body-cell-unrealized_pnl', """
            <q-td :props="props">
              <span :class="props.value>=0?'text-green-400':'text-red-400'"
                    class="font-mono">
                {{ props.value>=0?'+':'' }}{{ Number(props.value).toFixed(2) }}
              </span>
            </q-td>
        """)
        table.add_slot('body-cell-is_paper', """
            <q-td :props="props">
              <q-badge :color="props.value?'grey':'red'"
                       :label="props.value?'Paper':'Live'"/>
            </q-td>
        """)
        table.add_slot('body-cell-slot_id', """
            <q-td :props="props">
              <q-btn flat dense size="xs" color="negative" label="Close"
                     @click="() => $emit('close_pos', props.row)"/>
            </q-td>
        """)

        async def on_close(e):
            row = e.args
            if not isinstance(row, dict):
                return
            sid = row.get('slot_id', '')
            if sid:
                await commands.close_slot(redis, sid)
                ui.notify(
                    f"Closing {row.get('symbol', '')}...", type='warning'
                )

        table.on('close_pos', on_close)

    def update():
        rows = []
        for pos in state.positions:
            rows.append({
                'exchange':          pos.get('exchange', ''),
                'symbol':            pos.get('symbol', ''),
                'side':              pos.get('side', ''),
                'entry_price':       f"{float(pos.get('entry_price',    0)):.4f}",
                'current_price':     f"{float(pos.get('current_price',  0)):.4f}",
                'qty':               pos.get('qty', 0),
                'leverage':          f"{pos.get('leverage', 1)}x",
                'unrealized_pnl':    round(float(pos.get('unrealized_pnl', 0)), 2),
                'liquidation_price': f"{float(pos.get('liquidation_price') or 0):.4f}",
                'funding_rate':      (
                    f"{float(pos.get('funding_rate') or 0)*100:.4f}%"
                    if pos.get('funding_rate') else '—'
                ),
                'is_paper':          pos.get('is_paper', True),
                'slot_id':           pos.get('slot_id', ''),
            })
        table.rows = rows
        table.update()

    return {'update': update}

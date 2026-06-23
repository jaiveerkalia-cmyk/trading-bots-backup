from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

_COLS = [
    {'name': 'time',     'label': 'Time',     'field': 'created_at',      'align': 'left'},
    {'name': 'exchange', 'label': 'Exchange', 'field': 'exchange',         'align': 'left'},
    {'name': 'symbol',   'label': 'Symbol',   'field': 'symbol',           'align': 'left'},
    {'name': 'side',     'label': 'Side',     'field': 'side',             'align': 'center'},
    {'name': 'type',     'label': 'Type',     'field': 'order_type',       'align': 'center'},
    {'name': 'qty',      'label': 'Qty',      'field': 'filled_qty',       'align': 'right'},
    {'name': 'price',    'label': 'Fill px',  'field': 'avg_fill_price',   'align': 'right'},
    {'name': 'status',   'label': 'Status',   'field': 'status',           'align': 'center'},
    {'name': 'mode',     'label': 'Mode',     'field': 'is_paper',         'align': 'center'},
]


def build(state: 'UIState') -> dict:
    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label('Order History').classes('text-gray-300 font-medium text-sm')
            ui.label('(latest 200 — full history in CSV)').classes('text-gray-600 text-xs')

        table = ui.table(columns=_COLS, rows=[], row_key='id',
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
              <q-badge :color="props.value==='filled'?'positive':'grey'" :label="props.value"/>
            </q-td>
        """)
        table.add_slot('body-cell-mode', """
            <q-td :props="props">
              <q-badge :color="props.value?'grey':'red'" :label="props.value?'Paper':'Live'"/>
            </q-td>
        """)

    def update():
        rows = []
        for o in reversed(state.order_history[-200:]):
            ts = o.get('created_at', '')
            if 'T' in ts:
                ts = ts.split('T')[1][:8]
            rows.append({
                'id':             o.get('id', ''),
                'created_at':     ts,
                'exchange':       o.get('exchange', ''),
                'symbol':         o.get('symbol', ''),
                'side':           o.get('side', ''),
                'order_type':     o.get('order_type', ''),
                'filled_qty':     o.get('filled_qty', 0),
                'avg_fill_price': f"{float(o.get('avg_fill_price') or 0):.4f}",
                'status':         o.get('status', ''),
                'is_paper':       o.get('is_paper', True),
            })
        table.rows = rows
        table.update()

    return {'update': update}

from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from nicegui import ui
from common.settings import IST

if TYPE_CHECKING:
    from ui.state import UIState

_COLS = [
    {'name': 'time',          'label': 'Time (IST)',  'field': 'time',          'align': 'left'},
    {'name': 'exchange',      'label': 'Exch',        'field': 'exchange',      'align': 'left'},
    {'name': 'symbol',        'label': 'Symbol',      'field': 'symbol',        'align': 'left'},
    {'name': 'side',          'label': 'Side',        'field': 'side',          'align': 'center'},
    {'name': 'order_type',    'label': 'Type',        'field': 'order_type',    'align': 'center'},
    {'name': 'filled_qty',    'label': 'Qty',         'field': 'filled_qty',    'align': 'right'},
    {'name': 'avg_fill_price','label': 'Fill Px',     'field': 'avg_fill_price','align': 'right'},
    {'name': 'status',        'label': 'Status',      'field': 'status',        'align': 'center'},
    {'name': 'is_paper',      'label': 'Mode',        'field': 'is_paper',      'align': 'center'},
]


def _to_ist(ts_str: str) -> str:
    try:
        ts = ts_str[:26].replace('Z', '+00:00')
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime('%d/%m %H:%M:%S')
    except Exception:
        return ts_str[:19].replace('T', ' ')


def build(state: 'UIState') -> dict:
    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label('Order History').classes('text-gray-300 font-medium text-sm')
            ui.label('newest first — full history in CSV').classes('text-gray-600 text-xs')

        table = ui.table(
            columns=_COLS, rows=[], row_key='id',
        ).props('dark dense flat').classes('w-full text-xs')

        table.add_slot('body-cell-side', """
            <q-td :props="props">
              <span :class="props.value==='buy'?'text-green-400':'text-red-400'">
                {{ props.value ? props.value.toUpperCase() : '' }}
              </span>
            </q-td>
        """)
        table.add_slot('body-cell-status', """
            <q-td :props="props">
              <q-badge :color="props.value==='filled'?'positive':'grey'" :label="props.value"/>
            </q-td>
        """)
        table.add_slot('body-cell-is_paper', """
            <q-td :props="props">
              <q-badge :color="props.value?'grey':'red'" :label="props.value?'Paper':'Live'"/>
            </q-td>
        """)

    prev_len = {'v': -1}

    def update():
        if len(state.order_history) == prev_len['v']:
            return
        prev_len['v'] = len(state.order_history)

        rows = []
        # order_history is already newest-first from state_publisher
        for o in state.order_history:
            fill_px = float(o.get('avg_fill_price') or 0)
            rows.append({
                'id':            o.get('id', ''),
                'time':          _to_ist(o.get('created_at', o.get('updated_at', ''))),
                'exchange':      o.get('exchange', ''),
                'symbol':        o.get('symbol', ''),
                'side':          o.get('side', ''),
                'order_type':    o.get('order_type', '').replace('_', ' '),
                'filled_qty':    f"{float(o.get('filled_qty', 0)):g}",
                'avg_fill_price': f"{fill_px:g}" if fill_px else '—',
                'status':        o.get('status', ''),
                'is_paper':      o.get('is_paper', True),
            })
        table.rows = rows
        table.update()

    return {'update': update}

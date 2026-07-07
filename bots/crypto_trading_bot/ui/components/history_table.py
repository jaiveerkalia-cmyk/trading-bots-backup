from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from nicegui import ui
from common.settings import IST

if TYPE_CHECKING:
    from ui.state import UIState

_COLS = [
    {'name': 'time',           'label': 'Time (IST)',  'field': 'time',           'align': 'left'},
    {'name': 'exchange',       'label': 'Exch',        'field': 'exchange',       'align': 'left'},
    {'name': 'symbol',         'label': 'Symbol',      'field': 'symbol',         'align': 'left'},
    {'name': 'side',           'label': 'Side',        'field': 'side',           'align': 'center'},
    {'name': 'order_type',     'label': 'Type',        'field': 'order_type',     'align': 'center'},
    {'name': 'filled_qty',     'label': 'Qty',         'field': 'filled_qty',     'align': 'right'},
    {'name': 'trigger_price',  'label': 'Trigger Px',  'field': 'trigger_price',  'align': 'right'},
    {'name': 'avg_fill_price', 'label': 'Fill Px',     'field': 'avg_fill_price', 'align': 'right'},
    {'name': 'status',         'label': 'Status',      'field': 'status',         'align': 'center'},
    {'name': 'is_paper',       'label': 'Mode',        'field': 'is_paper',       'align': 'center'},
]
_PER_PAGE_OPTIONS = {10: '10', 25: '25', 50: '50', 100: '100', -1: 'All'}


def _to_ist(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts[:26].replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime('%d/%m %H:%M:%S')
    except Exception:
        return ts[:19].replace('T', ' ')


def _trigger_px(o: dict) -> str:
    ot    = o.get('order_type', '')
    price = float(o.get('price') or 0)
    stop  = float(o.get('stop_price') or 0)
    if ot == 'limit'      and price > 0: return f'{price:g}'
    if ot == 'stop_limit' and stop  > 0: return f'{stop:g}'
    # Market exit orders: stop_price is annotated with the stop/target price that fired
    if stop > 0: return f'{stop:g}'
    return '—'


def build(state: 'UIState') -> dict:
    pager = {'page': 0, 'per_page': 25}

    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label('Order History').classes('text-gray-300 font-medium text-sm')
            with ui.row().classes('items-center gap-2'):
                ui.label('Rows:').classes('text-gray-500 text-xs')
                ui.select(
                    _PER_PAGE_OPTIONS, value=25,
                    on_change=lambda e: (
                        pager.update({'per_page': e.value, 'page': 0}),
                        _render(),
                    ),
                ).props('dense dark outlined borderless').classes('w-20 text-xs')

        table = ui.table(
            columns=_COLS, rows=[], row_key='id',
        ).props('dark dense flat').classes('w-full text-xs')

        table.add_slot('body-cell-side', """
            <q-td :props="props">
              <span :class="props.value==='buy'?'text-green-400':'text-red-400'">
                {{ props.value ? props.value.toUpperCase() : '' }}
              </span>
            </q-td>""")
        table.add_slot('body-cell-trigger_price', """
            <q-td :props="props">
              <span class="font-mono text-orange-300">{{ props.value }}</span>
            </q-td>""")
        table.add_slot('body-cell-avg_fill_price', """
            <q-td :props="props">
              <span class="font-mono text-yellow-300">{{ props.value }}</span>
            </q-td>""")
        table.add_slot('body-cell-status', """
            <q-td :props="props">
              <q-badge :color="props.value==='filled'?'positive':'grey'"
                       :label="props.value" />
            </q-td>""")
        table.add_slot('body-cell-is_paper', """
            <q-td :props="props">
              <q-badge :color="props.value?'grey':'red'"
                       :label="props.value?'Paper':'Live'" />
            </q-td>""")

        # ── Pagination controls ───────────────────────────────────────────────
        with ui.row().classes('w-full justify-between items-center mt-2 px-1'):
            page_info = ui.label('').classes('text-gray-500 text-xs')
            with ui.row().classes('gap-1'):
                def _prev():
                    if pager['page'] > 0:
                        pager['page'] -= 1
                        _render()

                def _next():
                    total = len(state.order_history)
                    per   = pager['per_page']
                    max_p = 0 if per == -1 else max(0, (total - 1) // per)
                    if pager['page'] < max_p:
                        pager['page'] += 1
                        _render()

                ui.button(icon='first_page', on_click=lambda: (
                    pager.update({'page': 0}), _render()
                )).props('dense flat size=xs').classes('text-gray-400')
                ui.button(icon='chevron_left', on_click=_prev).props(
                    'dense flat size=xs').classes('text-gray-400')
                ui.button(icon='chevron_right', on_click=_next).props(
                    'dense flat size=xs').classes('text-gray-400')
                ui.button(icon='last_page', on_click=lambda: (
                    pager.update({'page': max(0, (
                        (len(state.order_history) - 1) //
                        (pager['per_page'] if pager['per_page'] != -1 else 1)
                    ))}), _render()
                )).props('dense flat size=xs').classes('text-gray-400')

    all_rows_cache: list[dict] = []
    prev_len = {'v': -1}

    def _build_rows() -> list[dict]:
        rows = []
        for o in state.order_history:
            fill_px = float(o.get('avg_fill_price') or 0)
            rows.append({
                'id':             o.get('id', ''),
                'time':           _to_ist(o.get('created_at', o.get('updated_at', ''))),
                'exchange':       o.get('exchange', ''),
                'symbol':         o.get('symbol', ''),
                'side':           o.get('side', ''),
                'order_type':     ('Stop Market' if o.get('order_type') == 'stop_limit'
                                   else o.get('order_type', '').replace('_', ' ').title()),
                'filled_qty':     f"{float(o.get('filled_qty', 0)):g}",
                'trigger_price':  _trigger_px(o),
                'avg_fill_price': f"{fill_px:g}" if fill_px else '—',
                'status':         o.get('status', ''),
                'is_paper':       o.get('is_paper', True),
            })
        return rows

    def _render() -> None:
        total = len(all_rows_cache)
        per   = pager['per_page']

        if per == -1:
            visible   = all_rows_cache
            page_info.set_text(f'{total} orders')
        else:
            max_page  = max(0, (total - 1) // per) if total else 0
            pager['page'] = min(pager['page'], max_page)
            start     = pager['page'] * per
            visible   = all_rows_cache[start : start + per]
            page_info.set_text(
                f"Page {pager['page']+1}/{max_page+1}  "
                f"({start+1}–{min(start+per, total)} of {total})"
                if total else 'No orders'
            )

        table.rows = visible
        table.update()

    def update() -> None:
        n = len(state.order_history)
        if n == prev_len['v']:
            return
        prev_len['v'] = n
        all_rows_cache.clear()
        all_rows_cache.extend(_build_rows())
        _render()

    return {'update': update}

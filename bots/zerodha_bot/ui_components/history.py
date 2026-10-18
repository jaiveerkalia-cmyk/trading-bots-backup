"""Order History: the complete, all-time options_tradebook.csv log."""
from nicegui import ui
from config import TRADEBOOK_FILE, ui_refs
import os as _zb_os
_NO_PANDAS = _zb_os.environ.get('ZB_NO_PANDAS') == '1'
if _NO_PANDAS:
    import lean_io as _lean_io
else:
    import pandas as pd

# --- ORDER HISTORY (full options_tradebook.csv) ---

def _refresh_history_table():
    try:
        if _NO_PANDAS:
            cols, rows = _lean_io.history_table_data(TRADEBOOK_FILE)
        else:
            df = pd.read_csv(TRADEBOOK_FILE)
            df = df.iloc[::-1]  # most recent (last appended) first
            cols = [{'name': c, 'label': c.replace('_', ' '), 'field': c, 'sortable': True, 'align': 'left'} for c in df.columns]
            rows = df.to_dict('records')
        tbl = ui_refs.get('history_table')
        if tbl:
            tbl.columns = cols
            tbl.rows = rows
            tbl.update()
    except Exception:
        pass  # e.g. file not yet created; table just stays empty


def render_order_history():
    """Full-width Order History: the complete, all-time options_tradebook.csv log."""
    with ui.card().classes('w-full p-2 gap-2 border border-gray-300 rounded-xl shadow-sm mb-4'):
        with ui.row().classes('w-full justify-between items-center'):
            ui.label('ORDER HISTORY (All Trades)').classes('font-bold text-sm text-gray-700')
            ui.button('Refresh', on_click=_refresh_history_table).props('dense flat').classes('text-xs')
        with ui.scroll_area().classes('w-full h-64'):
            ui_refs['history_table'] = ui.table(columns=[], rows=[], row_key='Trade_ID').classes('w-full').props('dense flat bordered')
    _refresh_history_table()
    ui.timer(10.0, _refresh_history_table)

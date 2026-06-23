from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState

_LEVEL_COLOR = {
    'info':    'text-gray-300',
    'success': 'text-green-400',
    'warning': 'text-yellow-400',
    'error':   'text-red-400',
}


def build(state: 'UIState') -> dict:
    with ui.card().classes('w-full bg-gray-950 p-3 rounded-lg'):
        ui.label('Activity Log').classes('text-gray-400 font-medium text-sm mb-2')

        log = ui.log(max_lines=200).classes(
            'w-full font-mono text-xs bg-gray-950 text-gray-300'
        ).style('height: 180px;')

    seen: set[str] = set()

    def update():
        for entry in reversed(state.log_entries):
            ts  = entry.get('ts', '')[:19].replace('T', ' ')
            msg = entry.get('msg', '')
            lvl = entry.get('lvl', 'info')
            key = f"{ts}:{msg}"
            if key not in seen:
                seen.add(key)
                exc = entry.get('exc') or ''
                sym = entry.get('sym') or ''
                tag = f"[{exc}]" if exc else ''
                log.push(f"[{ts}] {tag} {msg}")
        # Keep seen set bounded
        if len(seen) > 500:
            seen.clear()

    return {'update': update}

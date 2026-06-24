from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from nicegui import ui
from common.settings import IST

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState') -> dict:
    with ui.card().classes('w-full bg-gray-950 p-3 rounded-lg'):
        ui.label('Activity Log').classes(
            'text-gray-400 font-medium text-sm mb-2'
        )
        log = ui.log(max_lines=200).classes(
            'w-full font-mono text-xs bg-gray-950 text-gray-300'
        ).style('height: 180px;')

    seen: set[str] = set()

    def _to_ist(ts_str: str) -> str:
        try:
            dt = datetime.fromisoformat(ts_str[:19]).replace(tzinfo=timezone.utc)
            return dt.astimezone(IST).strftime('%H:%M:%S')
        except Exception:
            return ts_str[:19].replace('T', ' ')

    def update():
        for entry in reversed(state.log_entries):
            raw_ts = entry.get('ts', '')
            ts     = _to_ist(raw_ts)
            msg    = entry.get('msg', '')
            exc    = entry.get('exc') or ''
            key    = f"{raw_ts}:{msg}"
            if key not in seen:
                seen.add(key)
                tag  = f"[{exc}] " if exc else ''
                log.push(f"[{ts}] {tag}{msg}")

        if len(seen) > 500:
            seen.clear()

    return {'update': update}

"""
PnL Curve — shows unrealized PnL of the current open position(s).
Hidden when no position is open and no history exists.
Resets automatically when a new position is opened.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState') -> dict:
    opts = {
        'backgroundColor': '#111827',
        'grid': {'left': '7%', 'right': '3%', 'top': '12%', 'bottom': '18%'},
        'xAxis': {
            'type':      'category',
            'data':      [],
            'axisLine':  {'lineStyle': {'color': '#374151'}},
            'axisLabel': {'color': '#6b7280', 'fontSize': 9},
        },
        'yAxis': {
            'type':      'value',
            'splitLine': {'lineStyle': {'color': '#1f2937'}},
            'axisLabel': {'color': '#6b7280', 'fontSize': 9, 'formatter': '{value}'},
        },
        'tooltip': {
            'trigger':    'axis',
            'backgroundColor': '#1f2937',
            'borderColor':     '#374151',
            'textStyle':       {'color': '#e5e7eb', 'fontSize': 11},
            'formatter':       '{b}: {c}',
        },
        'series': [{
            'type':       'line',
            'data':       [],
            'smooth':     True,
            'symbol':     'none',
            'lineStyle':  {'width': 2, 'color': '#22c55e'},
            'areaStyle':  {'color': '#22c55e', 'opacity': 0.1},
            'itemStyle':  {'color': '#22c55e'},
        }],
    }

    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg mt-2') as card:
        with ui.row().classes('w-full items-center justify-between mb-1'):
            ui.label('Position PnL').classes('text-gray-300 font-medium text-sm')
            status_lbl = ui.label('').classes('text-xs text-gray-500')
        chart = ui.echart(opts).classes('w-full').style('height:150px')
        no_pos_lbl = ui.label('Open a position to see PnL curve').classes(
            'text-gray-600 text-xs text-center w-full py-4'
        )

    prev_len = {'v': 0}

    def update() -> None:
        has_history = bool(state.pnl_history)
        has_pos     = bool(state.positions)

        if not has_history and not has_pos:
            chart.set_visibility(False)
            no_pos_lbl.set_visibility(True)
            status_lbl.set_text('')
            return

        chart.set_visibility(True)
        no_pos_lbl.set_visibility(False)

        if len(state.pnl_history) == prev_len['v']:
            return
        prev_len['v'] = len(state.pnl_history)

        xs    = [t for t, _ in state.pnl_history]
        ys    = [round(v, 4) for _, v in state.pnl_history]
        last  = ys[-1] if ys else 0.0
        color = '#22c55e' if last >= 0 else '#ef4444'

        status_lbl.set_text(
            f"{'+'if last>=0 else ''}{last:.4f}  •  {len(ys)} pts"
        )
        chart.options['xAxis']['data']         = xs
        chart.options['series'][0]['data']      = ys
        chart.options['series'][0]['lineStyle'] = {'width': 2, 'color': color}
        chart.options['series'][0]['areaStyle'] = {'color': color, 'opacity': 0.1}
        chart.options['series'][0]['itemStyle'] = {'color': color}
        chart.update()

    return {'update': update}

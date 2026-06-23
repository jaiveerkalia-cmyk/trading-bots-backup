from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState') -> dict:
    opts = {
        'backgroundColor': '#111827',
        'grid':   {'left': '8%', 'right': '4%', 'top': '15%', 'bottom': '12%'},
        'xAxis':  {'type': 'category', 'data': [],
                   'axisLine':  {'lineStyle': {'color': '#374151'}},
                   'axisLabel': {'color': '#6b7280', 'fontSize': 10}},
        'yAxis':  {'type': 'value', 'splitLine': {'lineStyle': {'color': '#1f2937'}},
                   'axisLabel': {'color': '#6b7280', 'fontSize': 10}},
        'tooltip': {'trigger': 'axis', 'backgroundColor': '#1f2937',
                    'borderColor': '#374151', 'textStyle': {'color': '#e5e7eb'}},
        'series': [{
            'type': 'line', 'data': [], 'smooth': True,
            'symbol': 'none', 'lineStyle': {'width': 2},
            'areaStyle': {'opacity': 0.15},
            'itemStyle': {'color': '#22c55e'},
        }],
    }

    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        ui.label('PnL Curve').classes('text-gray-300 font-medium text-sm mb-1')
        chart = ui.echart(opts).classes('w-full').style('height: 220px')

    def update():
        if not state.pnl_history:
            return
        xs = [t for t, _ in state.pnl_history]
        ys = [round(v, 2) for _, v in state.pnl_history]

        # Color line green if net positive, red if negative
        color = '#22c55e' if (ys[-1] if ys else 0) >= 0 else '#ef4444'

        chart.options['xAxis']['data']            = xs
        chart.options['series'][0]['data']         = ys
        chart.options['series'][0]['itemStyle']    = {'color': color}
        chart.options['series'][0]['lineStyle']    = {'color': color, 'width': 2}
        chart.options['series'][0]['areaStyle']    = {'color': color, 'opacity': 0.1}
        chart.update()

    return {'update': update}

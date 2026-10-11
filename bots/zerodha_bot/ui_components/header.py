"""
Default banner/chart/log renderers. render_master_banner here is the ORIGINAL default --
auto_run.py monkey-patches comp.render_master_banner with its own custom_render_master_banner
immediately on import, so this version is effectively a fallback only.
"""
from nicegui import ui
from config import params, UI_OPTS, ui_refs

# --- HEADER / CHART / LOG ---

def render_master_banner(update_lots_callback):
    with ui.column().classes('w-full gap-0 mb-4'):
        with ui.card().classes('w-full p-3 bg-orange-200 text-orange-900 rounded-t-xl rounded-b-none border-b border-orange-300') as card:
            ui_refs['banner_card'] = card
            with ui.element('div').classes('w-full grid grid-cols-[1fr_auto] items-center gap-4'):
                with ui.row().classes('items-center gap-4 flex-nowrap'):
                    ui.label('Zerodha Trading Engine').classes('text-xl font-bold tracking-wide whitespace-nowrap')
                    ui_refs['monitor_status'] = ui.label('TRIGGERS OFF').classes('text-xs font-bold bg-gray-800 text-gray-400 px-2 py-1 rounded whitespace-nowrap')
                    ui.switch('Mute', value=params['mute_sound']).bind_value(params, 'mute_sound').props('color=red dense')
                    ui.button('🔊 Enable Sound', on_click=lambda: ui.run_javascript(
                        'try { const a = new Audio("https://actions.google.com/sounds/v1/cartoon/pop.ogg"); '
                        'a.volume = 0.4; a.play().catch(()=>{}); } catch(e) {}'
                    )).props('dense flat size=sm').classes('text-[10px] text-orange-900')
                with ui.row().classes('gap-6 items-center flex-nowrap justify-end'):
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Unrealized PnL').classes('text-orange-800 text-[10px] uppercase tracking-wider whitespace-nowrap')
                        ui_refs['pnl_unrealized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-gray-800 leading-none')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Realized PnL').classes('text-orange-800 text-[10px] uppercase tracking-wider whitespace-nowrap')
                        ui_refs['pnl_realized'] = ui.label('₹ 0.00').classes('text-2xl font-mono font-bold text-green-700 leading-none')

        with ui.card().classes('w-full p-2 bg-orange-50 flex-row items-center gap-6 rounded-none border-x border-orange-200'):
            with ui.row().classes('items-center gap-2'):
                ui.label('Index:').classes('font-bold text-orange-900 text-xs')
                ui.radio(UI_OPTS['indices'], value=params['trading_index'], on_change=update_lots_callback).bind_value(params, 'trading_index').props('inline dense')
            with ui.row().classes('items-center gap-2'):
                ui.label('Live Trading:').classes('font-bold text-orange-900 text-xs ml-4')
                ui.radio(UI_OPTS['on_off'], value=params['live_trading']).bind_value(params, 'live_trading').props('inline dense')

        with ui.card().classes('w-full p-1 px-3 bg-gray-100 border-t border-gray-300 rounded-none'):
            with ui.row().classes('items-center gap-2'):
                ui.label('LAST ACTION:').classes('text-[10px] font-bold text-gray-500')
                ui_refs['last_action'] = ui.label('System Ready').classes('font-mono text-xs font-bold text-orange-600')

        with ui.row().classes('w-full gap-0 border border-gray-300 rounded-none overflow-hidden shadow-sm'):
            with ui.card().classes('w-1/2 p-2 bg-red-50 border-r border-gray-300 rounded-none gap-1'):
                with ui.row().classes('justify-between items-center w-full border-b border-red-200 pb-1 mb-1'):
                    ui.label('CALL POSITION').classes('text-xs font-bold text-red-900')
                    ui_refs['call_status'] = ui.label('INACTIVE').classes('text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
                ui_refs['call_info'] = ui.label('Time: --').classes('text-[9px] font-mono text-gray-600')
                ui_refs['call_trigger'] = ui.label('Trig: --').classes('text-[9px] font-mono text-red-800')
                with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-1 items-center'):
                    ui.label('Inst').classes('text-[10px] font-bold text-gray-400'); ui.label('Open').classes('text-[10px] font-bold text-gray-400 text-right'); ui.label('Curr').classes('text-[10px] font-bold text-gray-400 text-right')
                    ui_refs['call_main_strike'] = ui.label('-').classes('text-xs font-bold text-red-900')
                    ui_refs['call_main_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['call_main_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui_refs['call_hedge_strike'] = ui.label('-').classes('text-xs font-bold text-red-700')
                    ui_refs['call_hedge_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['call_hedge_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui.label('INDEX').classes('text-xs font-bold text-gray-500')
                    ui_refs['call_idx_open'] = ui.label('0').classes('text-xs font-mono text-gray-500 text-right')
                    ui_refs['call_idx_curr'] = ui.label('0').classes('text-xs font-mono font-bold text-gray-700 text-right')
                with ui.row().classes('w-full justify-between items-center mt-2 pt-1 border-t border-red-200'):
                    ui.label('RUNNING PnL').classes('text-[10px] font-bold text-gray-400')
                    ui_refs['call_pnl'] = ui.label('₹ 0').classes('text-xl font-bold text-gray-400 font-mono')

            with ui.card().classes('w-1/2 p-2 bg-green-50 rounded-none gap-1'):
                with ui.row().classes('justify-between items-center w-full border-b border-green-200 pb-1 mb-1'):
                    ui.label('PUT POSITION').classes('text-xs font-bold text-green-900')
                    ui_refs['put_status'] = ui.label('INACTIVE').classes('text-[10px] font-bold text-gray-400 bg-white px-2 rounded')
                ui_refs['put_info'] = ui.label('Time: --').classes('text-[9px] font-mono text-gray-600')
                ui_refs['put_trigger'] = ui.label('Trig: --').classes('text-[9px] font-mono text-green-800')
                with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-1 items-center'):
                    ui.label('Inst').classes('text-[10px] font-bold text-gray-400'); ui.label('Open').classes('text-[10px] font-bold text-gray-400 text-right'); ui.label('Curr').classes('text-[10px] font-bold text-gray-400 text-right')
                    ui_refs['put_main_strike'] = ui.label('-').classes('text-xs font-bold text-green-900')
                    ui_refs['put_main_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['put_main_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui_refs['put_hedge_strike'] = ui.label('-').classes('text-xs font-bold text-green-700')
                    ui_refs['put_hedge_open'] = ui.label('0.0').classes('text-xs font-mono text-gray-600 text-right')
                    ui_refs['put_hedge_curr'] = ui.label('0.0').classes('text-xs font-mono font-bold text-black text-right')
                    ui.label('INDEX').classes('text-xs font-bold text-gray-500')
                    ui_refs['put_idx_open'] = ui.label('0').classes('text-xs font-mono text-gray-500 text-right')
                    ui_refs['put_idx_curr'] = ui.label('0').classes('text-xs font-mono font-bold text-gray-700 text-right')
                with ui.row().classes('w-full justify-between items-center mt-2 pt-1 border-t border-green-200'):
                    ui.label('RUNNING PnL').classes('text-[10px] font-bold text-gray-400')
                    ui_refs['put_pnl'] = ui.label('₹ 0').classes('text-xl font-bold text-gray-400 font-mono')


def render_chart_row():
    with ui.card().classes('w-full h-64 p-2 border-x border-gray-300 rounded-none shadow-sm'):
        ui.label('Real-Time PnL Curve').classes('text-xs font-bold text-gray-500 mb-2')
        ui_refs['pnl_chart'] = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'grid': {'top': 30, 'bottom': 20, 'left': 50, 'right': 20},
            'xAxis': {'type': 'category', 'data': [], 'axisLine': {'lineStyle': {'color': '#9ca3af'}}},
            'yAxis': {'type': 'value', 'scale': True, 'splitLine': {'lineStyle': {'color': '#e5e7eb'}}},
            'backgroundColor': '#f9fafb',
            'dataZoom': [{'type': 'inside', 'start': 0, 'end': 100}, {'type': 'slider'}],
            'series': [{
                'name': 'Total PnL', 'type': 'line', 'data': [], 'smooth': True, 'showSymbol': False,
                'lineStyle': {'color': '#f97316', 'width': 2}, 'areaStyle': {'color': '#ffedd5', 'opacity': 0.5},
                'markPoint': {'data': [], 'symbolSize': 25, 'label': {'fontSize': 8, 'color': 'white'}}
            }]
        })


def render_log_row():
    with ui.card().classes('w-full p-0 gap-0 border-x border-b border-gray-300 rounded-b-xl overflow-hidden shadow-sm mb-4'):
        ui.label('TRADE EVENT LOG').classes('text-xs font-bold text-gray-300 bg-gray-800 w-full p-2 border-b border-gray-700')
        with ui.scroll_area().classes('w-full h-32 bg-gray-900 p-2'):
            ui_refs['activity_log_container'] = ui.column().classes('gap-1')

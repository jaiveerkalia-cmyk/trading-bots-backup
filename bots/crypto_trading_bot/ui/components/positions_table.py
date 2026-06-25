"""
Open positions with inline stop/target editing.
Uses @ui.refreshable for reliable async buttons.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis) -> dict:
    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        ui.label('Open Positions').classes('text-gray-300 font-medium text-sm mb-2')

        @ui.refreshable
        def rows():
            if not state.positions:
                ui.label('No open positions').classes('text-gray-600 text-xs py-1')
                return

            for pos in state.positions:
                upnl       = float(pos.get('unrealized_pnl', 0))
                side       = pos.get('side', '')
                exchange   = pos.get('exchange', '')
                sym        = pos.get('symbol', '')
                sid        = pos.get('slot_id', '')
                is_paper   = pos.get('is_paper', True)
                is_futures = 'futures' in exchange or exchange == 'delta'
                liq        = float(pos.get('liquidation_price') or 0)
                fr         = pos.get('funding_rate')
                slot       = state.get_slot(sid)
                cur_stop   = float((slot or {}).get('stop_price')   or 0)
                cur_target = float((slot or {}).get('target_price') or 0)

                side_color = 'text-green-400 font-bold' if side == 'long' else 'text-red-400 font-bold'
                pnl_color  = 'text-green-400' if upnl >= 0 else 'text-red-400'

                with ui.card().classes('w-full bg-gray-800 rounded-lg mb-2 overflow-hidden'):
                    # ── Row 1: main position data ─────────────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-2 items-center text-xs text-gray-300 gap-2'
                    ):
                        with ui.column().classes('gap-0 w-32'):
                            ui.label(f"{exchange}").classes('text-gray-500 text-xs')
                            ui.label(sym).classes('font-medium text-white text-sm')

                        ui.label(side.upper()).classes(f'w-12 {side_color}')

                        with ui.column().classes('gap-0 items-end flex-1'):
                            ui.label('Entry').classes('text-gray-500 text-xs')
                            ui.label(f"{float(pos.get('entry_price', 0)):g}").classes('font-mono')

                        with ui.column().classes('gap-0 items-end flex-1'):
                            ui.label('Mark' if is_futures else 'Price').classes('text-gray-500 text-xs')
                            ui.label(f"{float(pos.get('current_price', 0)):g}").classes(
                                'font-mono text-yellow-300'
                            )

                        with ui.column().classes('gap-0 items-end w-20'):
                            ui.label('Qty').classes('text-gray-500 text-xs')
                            ui.label(f"{float(pos.get('qty', 0)):g}").classes('font-mono')

                        with ui.column().classes('gap-0 items-end w-28'):
                            ui.label('Unreal. PnL').classes('text-gray-500 text-xs')
                            ui.label(
                                f"{'+'if upnl>=0 else ''}{upnl:.4f}"
                            ).classes(f'font-mono font-bold {pnl_color}')

                        if is_futures:
                            with ui.column().classes('gap-0 items-end w-28'):
                                ui.label('Liq. Price').classes('text-gray-500 text-xs')
                                ui.label(f"{liq:g}" if liq else '—').classes(
                                    'font-mono text-orange-400'
                                )
                            with ui.column().classes('gap-0 items-end w-24'):
                                ui.label('Funding').classes('text-gray-500 text-xs')
                                ui.label(
                                    f"{float(fr)*100:.4f}%" if fr else '—'
                                ).classes('font-mono text-xs')
                        else:
                            with ui.column().classes('gap-0 items-end w-28'):
                                ui.label('Borrow APR').classes('text-gray-500 text-xs')
                                ui.label('N/A').classes('font-mono text-gray-600')

                        ui.badge('Paper' if is_paper else 'Live',
                                 color='grey' if is_paper else 'red').classes('w-12')

                        async def do_close(slot_id=sid, symbol=sym):
                            await commands.close_slot(redis, slot_id)
                            ui.notify(f'Closing {symbol}...', type='warning')

                        ui.button('Close', on_click=do_close).props('dense unelevated').classes(
                            'bg-red-900 text-red-300 text-xs px-3 border border-red-700'
                        )

                    # ── Row 2: stop / target inline edit ──────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-2 bg-gray-850 border-t border-gray-700 '
                        'items-center gap-3 text-xs'
                    ):
                        ui.label('Stop:').classes('text-orange-400 w-10')
                        stop_inp = ui.number(
                            value=cur_stop, min=0, format='%g', label='Stop price',
                        ).props('dense dark outlined').classes('w-32')

                        async def set_stop(slot_id=sid, inp=stop_inp):
                            v = float(inp.value or 0) or None
                            await commands.update_slot(redis, slot_id, stop_price=v)
                            ui.notify(
                                f"Stop {'cleared' if v is None else f'set → {v:g}'}",
                                type='positive' if v else 'info',
                            )

                        ui.button('Set', on_click=set_stop).props('dense unelevated').classes(
                            'bg-orange-900 text-orange-300 text-xs px-3'
                        )

                        ui.separator().classes('bg-gray-700').style('width:1px;height:24px;')

                        ui.label('Target:').classes('text-blue-400 w-12')
                        tgt_inp = ui.number(
                            value=cur_target, min=0, format='%g', label='Target price',
                        ).props('dense dark outlined').classes('w-32')

                        async def set_target(slot_id=sid, inp=tgt_inp):
                            v = float(inp.value or 0) or None
                            await commands.update_slot(redis, slot_id, target_price=v)
                            ui.notify(
                                f"Target {'cleared' if v is None else f'set → {v:g}'}",
                                type='positive' if v else 'info',
                            )

                        ui.button('Set', on_click=set_target).props('dense unelevated').classes(
                            'bg-blue-900 text-blue-300 text-xs px-3'
                        )

                        # Show current stop/target if set
                        if cur_stop or cur_target:
                            ui.separator().classes('bg-gray-700').style('width:1px;height:24px;')
                            if cur_stop:
                                ui.label(f'SL: {cur_stop:g}').classes('text-orange-400 text-xs')
                            if cur_target:
                                ui.label(f'TP: {cur_target:g}').classes('text-blue-400 text-xs')

        rows()

    prev_hash = {'v': ''}

    def update():
        h = str([(
            p.get('slot_id', ''),
            round(float(p.get('unrealized_pnl', 0)), 2),
            round(float(p.get('current_price',   0)), 2),
        ) for p in state.positions] + [
            (s.get('id',''), s.get('stop_price'), s.get('target_price'))
            for s in state.slots if s.get('status') == 'active'
        ])
        if h != prev_hash['v']:
            prev_hash['v'] = h
            rows.refresh()

    return {'update': update}

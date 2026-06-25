"""
Open orders with Modify support.
Virtual VSTOP/VTGT orders get Modify (sends update_slot) or Remove.
Real working orders get Cancel or Modify (cancel+replace).
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis) -> dict:
    # Tracks which order is being modified: {order_id: True}
    modifying: dict[str, bool] = {}

    with ui.card().classes('w-full bg-gray-900 p-3 rounded-lg'):
        ui.label('Open Orders').classes('text-gray-300 font-medium text-sm mb-2')

        @ui.refreshable
        def rows():
            if not state.open_orders:
                ui.label('No open orders').classes('text-gray-600 text-xs py-1')
                return

            # Header
            with ui.row().classes(
                'w-full px-2 py-1 bg-gray-800 rounded text-xs '
                'text-gray-500 gap-2 font-medium mb-1'
            ):
                for lbl, cls in [
                    ('Exch', 'w-20'), ('Symbol', 'w-24'), ('Side', 'w-12'),
                    ('Type', 'w-20'), ('Price', 'w-24 text-right'),
                    ('Stop', 'w-24 text-right'), ('Qty', 'w-20 text-right'),
                    ('Status', 'w-16 text-center'), ('', 'flex-1'),
                ]:
                    ui.label(lbl).classes(cls)

            for o in state.open_orders:
                oid       = o.get('id', '')
                is_vstop  = oid.startswith('VSTOP-')
                is_vtgt   = oid.startswith('VTGT-')
                is_virtual = is_vstop or is_vtgt
                side       = o.get('side', '')
                side_color = 'text-green-400' if side == 'buy' else 'text-red-400'
                sid        = o.get('slot_id', '')
                sym        = o.get('symbol', '')
                price      = float(o.get('price') or 0)
                stop_px    = float(o.get('stop_price') or 0)

                # Main order row
                with ui.row().classes(
                    'w-full px-2 py-1 border-t border-gray-800 '
                    'items-center text-xs text-gray-300 gap-2'
                ):
                    ui.label(o.get('exchange', '')).classes('w-20 truncate')
                    ui.label(sym).classes('w-24 font-medium text-white')
                    ui.label(side.upper()).classes(f'w-12 {side_color}')
                    type_label = ('STOP' if is_vstop else 'TARGET' if is_vtgt
                                  else o.get('order_type', '').replace('_', ' '))
                    type_color = ('text-orange-400' if is_vstop else
                                  'text-blue-400' if is_vtgt else 'text-gray-300')
                    ui.label(type_label).classes(f'w-20 {type_color}')
                    ui.label(f"{price:g}" if price else '—').classes('w-24 text-right font-mono')
                    ui.label(f"{stop_px:g}" if stop_px else '—').classes('w-24 text-right font-mono text-orange-400')
                    ui.label(f"{float(o.get('qty', 0)):g}").classes('w-20 text-right font-mono')
                    ui.badge(o.get('status', ''), color='blue').classes('w-16')

                    with ui.row().classes('flex-1 gap-1 justify-end'):
                        # Modify button
                        async def toggle_modify(order_id=oid):
                            if order_id in modifying:
                                del modifying[order_id]
                            else:
                                modifying[order_id] = True
                            rows.refresh()
                        ui.button('Modify', on_click=toggle_modify).props('dense flat size=xs').classes('text-blue-400')

                        # Cancel / Remove button
                        async def do_cancel(order_id=oid, slot_id=sid, symbol=sym, vstop=is_vstop, vtgt=is_vtgt):
                            await commands.cancel_order(redis, slot_id, order_id)
                            if vstop:
                                ui.notify(f'Stop removed for {symbol}', type='info')
                            elif vtgt:
                                ui.notify(f'Target removed for {symbol}', type='info')
                            else:
                                ui.notify(f'Cancel sent for {symbol}', type='warning')
                            modifying.pop(order_id, None)
                        lbl = 'Remove' if is_virtual else 'Cancel'
                        ui.button(lbl, on_click=do_cancel).props('dense flat size=xs').classes('text-red-400')

                # Modify inline form — shown when this order is being modified
                if modifying.get(oid):
                    with ui.row().classes(
                        'w-full px-4 py-2 bg-gray-800 border-t border-gray-700 '
                        'items-center gap-2 text-xs'
                    ):
                        ui.label('New price:').classes('text-gray-500')
                        new_price_inp = ui.number(
                            value=price or stop_px, min=0, format='%g',
                        ).props('dense dark outlined').classes('w-32')

                        if not is_virtual:
                            ui.label('New qty:').classes('text-gray-500 ml-2')
                            new_qty_inp = ui.number(
                                value=float(o.get('qty', 0)), min=0, format='%g',
                            ).props('dense dark outlined').classes('w-28')
                        else:
                            new_qty_inp = None

                        async def confirm_modify(
                            order_id=oid, slot_id=sid, symbol=sym,
                            inp=new_price_inp, qinp=new_qty_inp,
                            vstop=is_vstop, vtgt=is_vtgt,
                        ):
                            np = float(inp.value or 0)
                            nq = float(qinp.value or 0) if qinp else 0
                            if vstop:
                                await commands.update_slot(redis, slot_id, stop_price=np or None)
                                ui.notify(f'Stop updated → {np:g}', type='positive')
                            elif vtgt:
                                await commands.update_slot(redis, slot_id, target_price=np or None)
                                ui.notify(f'Target updated → {np:g}', type='positive')
                            else:
                                await commands.modify_order(redis, slot_id, order_id, new_price=np, new_qty=nq)
                                ui.notify(f'Modify sent for {symbol}', type='positive')
                            modifying.pop(order_id, None)
                            rows.refresh()

                        ui.button('Confirm', on_click=confirm_modify,
                        ).props('dense unelevated').classes('bg-blue-700 text-white ml-2')
                        ui.button('Cancel', on_click=lambda oid=oid: (
                            modifying.pop(oid, None), rows.refresh()
                        )).props('dense flat').classes('text-gray-500')

        rows()

    prev_hash = {'v': ''}

    def update():
        h = str([(o.get('id', ''), o.get('status', ''), float(o.get('price') or 0))
                 for o in state.open_orders])
        if h != prev_hash['v']:
            prev_hash['v'] = h
            modifying.clear()
            rows.refresh()

    return {'update': update}

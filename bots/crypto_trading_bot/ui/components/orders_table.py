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
from common import settings

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
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
                    ('Stop', 'w-24 text-right'), ('Target', 'w-24 text-right'), ('Qty', 'w-20 text-right'),
                    ('Status', 'w-16 text-center'), ('', 'flex-1'),
                ]:
                    ui.label(lbl).classes(cls)

            for o in state.open_orders:
                oid       = o.get('id', '')
                is_vstop  = oid.startswith('VSTOP-')
                is_vtgt   = oid.startswith('VTGT-')
                is_vcond  = oid.startswith('VCOND-')
                is_virtual = is_vstop or is_vtgt or is_vcond
                side       = o.get('side', '')
                side_color = 'text-green-400' if side == 'buy' else 'text-red-400'
                sid        = o.get('slot_id', '')
                sym        = o.get('symbol', '')
                price      = float(o.get('price') or 0)

                # Stop/target live on the slot, not the Order model —
                # look them up so they display correctly on real working-order rows.
                slot_data   = state.get_slot(sid)
                slot_stop   = float(slot_data.get('stop_price')   or 0) if slot_data else 0.0
                slot_target = float(slot_data.get('target_price') or 0) if slot_data else 0.0

                # What to show in Stop / Target columns per row type
                if is_vstop:
                    display_stop   = price        # virtual order price IS the stop trigger
                    display_target = slot_target
                elif is_vtgt:
                    display_stop   = slot_stop
                    display_target = price        # virtual order price IS the target
                else:
                    display_stop   = slot_stop    # real order → from slot
                    display_target = slot_target  # real order → from slot

                # Main order row
                with ui.row().classes(
                    'w-full px-2 py-1 border-t border-gray-800 '
                    'items-center text-xs text-gray-300 gap-2'
                ):
                    ui.label(o.get('exchange', '')).classes('w-20 truncate')
                    ui.label(sym).classes('w-24 font-medium text-white')
                    ui.label(side.upper()).classes(f'w-12 {side_color}')
                    type_label = ('STOP'   if is_vstop else
                                  'TARGET' if is_vtgt  else
                                  f"COND {o.get('order_type','').replace('_',' ').upper()}" if is_vcond else
                                  o.get('order_type', '').replace('_', ' '))
                    type_color = ('text-orange-400' if is_vstop else
                                  'text-blue-400'   if is_vtgt  else
                                  'text-purple-400' if is_vcond else
                                  'text-gray-300')
                    ui.label(type_label).classes(f'w-20 {type_color}')
                    # Price: entry level for real orders + VCOND; hidden for VSTOP/VTGT
                    ui.label(
                        f"{price:g}" if (price and not (is_vstop or is_vtgt)) else '—'
                    ).classes('w-24 text-right font-mono')
                    ui.label(
                        f"{display_stop:g}" if display_stop else '—'
                    ).classes('w-24 text-right font-mono text-orange-400')
                    ui.label(
                        f"{display_target:g}" if display_target else '—'
                    ).classes('w-24 text-right font-mono text-blue-400')
                    ui.label(f"{float(o.get('qty', 0)):g}").classes('w-20 text-right font-mono')
                    ui.badge(o.get('status', ''), color='blue').classes('w-16')

                    with ui.row().classes('flex-1 gap-1 justify-end'):
                        # Modify button — not shown for VCOND (cancel and re-place)
                        if not is_vcond:
                            async def toggle_modify(order_id=oid):
                                if order_id in modifying:
                                    del modifying[order_id]
                                else:
                                    modifying[order_id] = True
                                rows.refresh()
                            ui.button('Modify', on_click=toggle_modify).props(
                                'dense flat size=xs'
                            ).classes('text-blue-400')

                        # Cancel / Remove button
                        async def do_cancel(
                            order_id=oid, slot_id=sid, symbol=sym,
                            vstop=is_vstop, vtgt=is_vtgt, vcond=is_vcond,
                        ):
                            await commands.cancel_order(redis, slot_id, order_id)
                            if vstop:
                                ui.notify(f'Stop removed for {symbol}', type='info')
                            elif vtgt:
                                ui.notify(f'Target removed for {symbol}', type='info')
                            elif vcond:
                                ui.notify(f'Conditional order cancelled for {symbol}',
                                          type='info')
                            else:
                                ui.notify(f'Cancel sent for {symbol}', type='warning')
                            modifying.pop(order_id, None)
                        lbl = 'Remove' if is_virtual else 'Cancel'
                        ui.button(lbl, on_click=do_cancel).props(
                            'dense flat size=xs'
                        ).classes('text-red-400')

                # Inline modify form — shown when this order is being modified
                if modifying.get(oid):
                    _render_modify_form(
                        o           = o,
                        oid         = oid,
                        sid         = sid,
                        sym         = sym,
                        price       = price,
                        slot_stop   = slot_stop,
                        slot_target = slot_target,
                        is_vstop    = is_vstop,
                        is_vtgt     = is_vtgt,
                        modifying   = modifying,
                        refresh     = rows.refresh,
                        redis       = redis,
                        shared      = shared,
                    )

        rows()

    prev_hash = {'v': ''}

    def update() -> None:
        # Include slot stop/target in hash so table refreshes when they change
        orders_h = str([
            (o.get('id', ''), o.get('status', ''), float(o.get('price') or 0))
            for o in state.open_orders
        ])
        slots_h = str([
            (s.get('id'), s.get('stop_price'), s.get('target_price'), s.get('status'))
            for s in state.slots
            if s.get('status') in ('working', 'active', 'conditional')
        ])
        h = orders_h + slots_h
        if h != prev_hash['v']:
            prev_hash['v'] = h
            modifying.clear()
            rows.refresh()

    return {'update': update}


# ─────────────────────────────────────────────────────────────────────────────
# Modify form (module-level so rows() stays readable)
# ─────────────────────────────────────────────────────────────────────────────

def _render_modify_form(
    o: dict,
    oid: str,
    sid: str,
    sym: str,
    price: float,
    slot_stop: float,
    slot_target: float,
    is_vstop: bool,
    is_vtgt: bool,
    modifying: dict,
    refresh,           # rows.refresh callable
    redis,
    shared: dict,
) -> None:
    """Render the inline modify form below an order row."""

    # Mutable form state — auto mode on by default
    mf: dict = {
        'entry':  price,
        'stop':   slot_stop,
        'target': slot_target,
        'qty':    float(o.get('qty', 0)),
        'auto':   True,
    }
    qty_lbl_ref: list = [None]   # label showing auto qty
    tgt_inp_ref: list = [None]   # tgt_inp widget so _recalc_auto can update it

    def _recalc_auto() -> None:
        """Recompute qty and R:R target whenever entry or stop changes (auto mode)."""
        if not mf['auto']:
            return
        ep = mf['entry']
        sp = mf['stop']
        if ep > 0 and sp > 0 and abs(ep - sp) > 1e-10:
            exchange   = o.get('exchange', 'binance_futures')
            balance    = max(shared.get('balance', 1), 1)
            risk_pct   = shared.get('risk_pct', settings.DEFAULT_RISK_PCT)
            fees_cfg   = settings.EXCHANGE_FEES.get(
                exchange, {'maker': 0.001, 'taker': 0.001}
            )
            ot         = o.get('order_type', 'limit')
            entry_fee  = fees_cfg['maker'] if ot == 'limit' else fees_cfg['taker']
            taker_fee  = fees_cfg['taker']
            fee_unit   = ep * entry_fee + sp * taker_fee
            risk_amt   = balance * (risk_pct / 100)
            mf['qty']  = round(risk_amt / (abs(ep - sp) + fee_unit), 8)

            # R:R-based auto target
            rr       = float(shared.get('rr_ratio', 2.0) or 2.0)
            qty      = mf['qty']
            pos_side = 'long' if o.get('side') == 'buy' else 'short'
            if rr > 0 and qty > 0:
                if pos_side == 'long':
                    denom    = qty * (1.0 - taker_fee)
                    auto_tgt = round(
                        (rr * risk_amt + ep * qty * (1.0 + entry_fee)) / denom, 6
                    ) if denom > 0 else 0.0
                else:
                    denom    = qty * (1.0 + taker_fee)
                    auto_tgt = round(
                        (ep * qty * (1.0 - entry_fee) - rr * risk_amt) / denom, 6
                    ) if denom > 0 else 0.0
                if auto_tgt > 0:
                    mf['target'] = auto_tgt
                    if tgt_inp_ref[0]:
                        try:
                            tgt_inp_ref[0].set_value(auto_tgt)
                        except Exception:
                            pass

        if qty_lbl_ref[0]:
            qty_lbl_ref[0].set_text(
                f'Auto: {mf["qty"]:g}' if (mf['auto'] and mf['qty'] > 0) else ''
            )

    def _cancel_modify() -> None:
        modifying.pop(oid, None)
        refresh()

    with ui.row().classes(
        'w-full px-4 py-2 bg-gray-800 border-t border-gray-700 '
        'flex-wrap items-center gap-2 text-xs'
    ):
        if is_vstop:
            # ── Virtual stop: update stop price only ──────────────────────────
            ui.label('Stop price:').classes('text-gray-500')
            stop_inp = ui.number(
                value=slot_stop or price, min=0, format='%g',
                on_change=lambda e: mf.update({'stop': float(e.value or 0)}),
            ).props('dense dark outlined').classes('w-32')

            async def confirm_vstop(slot_id=sid, inp=stop_inp) -> None:
                np = float(inp.value or 0)
                await commands.update_slot(redis, slot_id, stop_price=np or None)
                ui.notify(f'Stop updated \u2192 {np:g}', type='positive')
                modifying.pop(oid, None)
                refresh()

            ui.button('Confirm', on_click=confirm_vstop).props(
                'dense unelevated'
            ).classes('bg-blue-700 text-white ml-2')

        elif is_vtgt:
            # ── Virtual target: update target price only ──────────────────────
            ui.label('Target price:').classes('text-gray-500')
            tgt_inp = ui.number(
                value=slot_target or price, min=0, format='%g',
                on_change=lambda e: mf.update({'target': float(e.value or 0)}),
            ).props('dense dark outlined').classes('w-32')

            async def confirm_vtgt(slot_id=sid, inp=tgt_inp) -> None:
                np = float(inp.value or 0)
                await commands.update_slot(redis, slot_id, target_price=np or None)
                ui.notify(f'Target updated \u2192 {np:g}', type='positive')
                modifying.pop(oid, None)
                refresh()

            ui.button('Confirm', on_click=confirm_vtgt).props(
                'dense unelevated'
            ).classes('bg-blue-700 text-white ml-2')

        else:
            # ── Real working order: entry + stop + target + qty + auto toggle ─
            ui.label('Entry:').classes('text-gray-500')
            entry_inp = ui.number(
                value=price, min=0, format='%g',
                on_change=lambda e: (
                    mf.update({'entry': float(e.value or 0)}),
                    _recalc_auto(),
                ),
            ).props('dense dark outlined').classes('w-28')

            ui.label('Stop:').classes('text-gray-500 ml-2')
            stop_inp = ui.number(
                value=slot_stop, min=0, format='%g',
                on_change=lambda e: (
                    mf.update({'stop': float(e.value or 0)}),
                    _recalc_auto(),
                ),
            ).props('dense dark outlined').classes('w-28')

            ui.label('Target:').classes('text-gray-500 ml-2')
            tgt_inp = ui.number(
                value=slot_target, min=0, format='%g',
                on_change=lambda e: mf.update({'target': float(e.value or 0)}),
            ).props('dense dark outlined').classes('w-28')
            tgt_inp_ref[0] = tgt_inp

            ui.label('Qty:').classes('text-gray-500 ml-2')
            qty_inp = ui.number(
                value=float(o.get('qty', 0)), min=0, format='%g',
                on_change=lambda e: mf.update({'qty': float(e.value or 0)}),
            ).props('dense dark outlined').classes('w-24')

            ui.toggle(
                {'auto': 'Auto Qty', 'manual': 'Manual'}, value='auto',
                on_change=lambda e: (
                    mf.update({'auto': e.value == 'auto'}),
                    _recalc_auto(),
                ),
            ).props('dense').classes('text-xs ml-2')

            qty_auto_lbl = ui.label('').classes(
                'text-xs text-yellow-400 ml-1 self-center'
            )
            qty_lbl_ref[0] = qty_auto_lbl
            _recalc_auto()   # populate qty + target immediately on open

            async def confirm_real(
                order_id=oid, slot_id=sid,
                einp=entry_inp, sinp=stop_inp, tinp=tgt_inp, qinp=qty_inp,
            ) -> None:
                new_entry  = float(einp.value or 0)
                new_stop   = float(sinp.value or 0) or None
                new_target = float(tinp.value or 0) or None
                new_qty    = mf['qty'] if mf['auto'] else float(qinp.value or 0)

                entry_changed = bool(new_entry and new_entry != price)
                qty_changed   = bool(new_qty   and new_qty   != float(o.get('qty', 0)))

                # Entry / qty change \u2192 cancel+replace via modify_order
                if entry_changed or qty_changed:
                    await commands.modify_order(
                        redis, slot_id, order_id,
                        new_price=new_entry or None,
                        new_qty=new_qty or None,
                    )
                    parts = []
                    if entry_changed: parts.append(f'entry \u2192 {new_entry:g}')
                    if qty_changed:   parts.append(f'qty \u2192 {new_qty:g}')
                    ui.notify(', '.join(parts).capitalize(), type='positive')

                # Stop / target \u2192 update slot in-place
                stop_changed   = new_stop   != (slot_stop   or None)
                target_changed = new_target != (slot_target or None)
                if stop_changed or target_changed:
                    await commands.update_slot(
                        redis, slot_id,
                        stop_price=new_stop,
                        target_price=new_target,
                    )
                    parts = []
                    if stop_changed:
                        parts.append(f'stop \u2192 {new_stop:g}' if new_stop else 'stop cleared')
                    if target_changed:
                        parts.append(f'target \u2192 {new_target:g}' if new_target else 'target cleared')
                    if parts:
                        ui.notify(', '.join(parts).capitalize(), type='positive')

                if not any([entry_changed, qty_changed, stop_changed, target_changed]):
                    ui.notify('No changes detected', type='info')

                modifying.pop(order_id, None)
                refresh()

            ui.button('Confirm', on_click=confirm_real).props(
                'dense unelevated'
            ).classes('bg-blue-700 text-white ml-2')

        # Common Cancel button
        ui.button('Cancel', on_click=_cancel_modify).props(
            'dense flat'
        ).classes('text-gray-500')

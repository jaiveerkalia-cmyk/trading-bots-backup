"""
Open Positions — card-based layout.

DOM is rebuilt only when the set of slot IDs changes.
All price-tick updates (mark, PnL, $ value, fees, funding) are in-place.
Stop, target, and partial-close inputs are DOM-stable between ticks.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from nicegui import ui
import redis.asyncio as aioredis

from common import settings
from ui import commands

if TYPE_CHECKING:
    from ui.state import UIState


def build(state: 'UIState', redis: aioredis.Redis) -> dict:
    # ── Live-update label refs (keyed by slot_id) ────────────────────────────
    mark_refs: dict[str, ui.label] = {}
    pnl_refs:  dict[str, ui.label] = {}
    dv_refs:   dict[str, ui.label] = {}   # position dollar value = mark × qty
    fee_refs:  dict[str, ui.label] = {}
    fr_refs:   dict[str, ui.label] = {}

    prev_ids = {'v': ''}

    # ── Outer container ───────────────────────────────────────────────────────
    with ui.column().classes('w-full gap-0'):
        with ui.row().classes('w-full items-center justify-between px-1 mb-2'):
            ui.label('Open Positions').classes(
                'text-gray-300 font-semibold text-xs uppercase tracking-widest'
            )
            count_lbl = ui.label('').classes('text-gray-500 text-xs')

        @ui.refreshable
        def rows() -> None:
            mark_refs.clear()
            pnl_refs.clear()
            dv_refs.clear()
            fee_refs.clear()
            fr_refs.clear()

            if not state.positions:
                with ui.row().classes('w-full justify-center items-center py-8 gap-2'):
                    ui.icon('show_chart').classes('text-gray-700 text-2xl')
                    ui.label('No open positions').classes('text-gray-600 text-sm')
                return

            for pos in state.positions:
                sid      = pos.get('slot_id', '')
                sym      = pos.get('symbol', '')
                exchange = pos.get('exchange', '')
                side     = pos.get('side', 'long')
                is_fut   = 'futures' in exchange or exchange == 'delta'
                is_paper = pos.get('is_paper', True)
                entry    = float(pos.get('entry_price',    0) or 0)
                mark     = float(pos.get('current_price',  0) or 0)
                qty      = float(pos.get('qty',            0) or 0)
                upnl     = float(pos.get('unrealized_pnl', 0) or 0)
                liq      = float(pos.get('liquidation_price') or 0)
                fr_raw   = pos.get('funding_rate')

                slot    = state.get_slot(sid)
                cur_stp = float((slot or {}).get('stop_price')   or 0) or None
                cur_tgt = float((slot or {}).get('target_price') or 0) or None

                taker   = settings.EXCHANGE_FEES.get(exchange, {'taker': 0.001})['taker']
                exit_fee = round(mark * qty * taker, 4)
                dv       = mark * qty

                is_long  = side == 'long'
                bdr_col  = 'border-l-green-500' if is_long else 'border-l-red-500'
                side_bg  = ('bg-green-900/60 text-green-300'
                            if is_long else 'bg-red-900/60 text-red-300')
                pnl_col  = 'text-green-400' if upnl >= 0 else 'text-red-400'
                exch_s   = (exchange.upper()
                            .replace('_FUTURES', '-F')
                            .replace('BINANCE', 'BNF'))
                mode_s   = 'PAPER' if is_paper else 'LIVE'

                # ── Card ──────────────────────────────────────────────────────
                with ui.card().classes(
                    f'w-full bg-gray-800/80 rounded-xl mb-2.5 p-0 '
                    f'border-l-4 {bdr_col} shadow-lg overflow-hidden'
                ):
                    # ── Row 1: symbol · mark · $ value · uPnL ────────────────
                    with ui.row().classes(
                        'w-full px-3 pt-2.5 pb-2 items-center gap-x-3 gap-y-1 flex-wrap'
                    ):
                        # Exchange + symbol stack
                        with ui.column().classes('gap-0 leading-none min-w-fit'):
                            ui.label(f'{exch_s} · {mode_s}').classes(
                                'text-gray-500 text-xs leading-none'
                            )
                            ui.label(sym).classes(
                                'text-white font-bold text-[15px] leading-snug tracking-wide'
                            )

                        # Side badge
                        ui.label(side.upper()).classes(
                            f'px-2.5 py-0.5 rounded-md text-xs font-bold '
                            f'tracking-widest {side_bg}'
                        )

                        ui.element('div').classes('flex-1')  # push right

                        # Mark price
                        with ui.column().classes('items-end gap-0 leading-none min-w-fit'):
                            ui.label('Mark').classes('text-gray-500 text-[10px]')
                            m = ui.label(
                                f'{mark:,.6g}' if mark else '—'
                            ).classes(
                                'text-yellow-300 font-mono text-sm '
                                'font-semibold leading-snug'
                            )
                            mark_refs[sid] = m

                        # Position $ value
                        with ui.column().classes('items-end gap-0 leading-none min-w-fit'):
                            ui.label('Size').classes('text-gray-500 text-[10px]')
                            dv_lbl = ui.label(
                                f'${dv:,.2f}'
                            ).classes('text-gray-200 font-mono text-sm leading-snug')
                            dv_refs[sid] = dv_lbl

                        # Unrealized PnL
                        with ui.column().classes('items-end gap-0 leading-none min-w-fit'):
                            ui.label('uPnL').classes('text-gray-500 text-[10px]')
                            p = ui.label(
                                f"{'+'if upnl>=0 else ''}{upnl:.4f}"
                            ).classes(
                                f'font-mono text-sm font-bold {pnl_col} leading-snug'
                            )
                            pnl_refs[sid] = p

                    # ── Row 2: secondary stats ────────────────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-1.5 items-center gap-x-4 gap-y-0.5 '
                        'text-xs border-t border-gray-700/50 flex-wrap'
                    ):
                        with ui.row().classes('items-center gap-1'):
                            ui.label('Entry').classes('text-gray-500')
                            ui.label(f'{entry:g}' if entry else '—').classes(
                                'font-mono text-gray-200'
                            )

                        with ui.row().classes('items-center gap-1'):
                            ui.label('Qty').classes('text-gray-500')
                            ui.label(f'{qty:g}').classes('font-mono text-gray-300')

                        if is_fut:
                            with ui.row().classes('items-center gap-1'):
                                ui.label('Liq').classes('text-gray-500')
                                ui.label(
                                    f'{liq:g}' if liq else '—'
                                ).classes('font-mono text-orange-400')

                            with ui.row().classes('items-center gap-1'):
                                ui.label('Fund').classes('text-gray-500')
                                fr_l = ui.label(
                                    f'{float(fr_raw)*100:.4f}%'
                                    if fr_raw is not None else '—'
                                ).classes('font-mono text-gray-300')
                                fr_refs[sid] = fr_l

                        with ui.row().classes('items-center gap-1'):
                            ui.label('ExFee').classes('text-gray-500')
                            f_l = ui.label(
                                f'${exit_fee:.4f}'
                            ).classes('font-mono text-gray-500')
                            fee_refs[sid] = f_l

                    # ── Row 3: controls ───────────────────────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-2.5 items-center gap-2 '
                        'border-t border-gray-700/50 flex-wrap'
                    ):
                        # Stop
                        stp_inp = (
                            ui.number(value=cur_stp, min=0, placeholder='Stop')
                            .props('dense dark outlined')
                            .classes('w-28')
                        )

                        async def _set_stop(_e=None, sid=sid, inp=stp_inp):
                            v = float(inp.value) if inp.value else None
                            await commands.update_slot(redis, sid, stop_price=v)
                            ui.notify(
                                f"Stop {'cleared' if v is None else f'→ {v:g}'}",
                                type='positive' if v else 'info',
                                position='bottom-right',
                            )

                        (ui.button('S', on_click=_set_stop)
                         .props('dense unelevated size=xs')
                         .classes('bg-orange-900/80 text-orange-300 px-2.5 min-w-0')
                         .tooltip('Set stop'))

                        # Target
                        tgt_inp = (
                            ui.number(value=cur_tgt, min=0, placeholder='Target')
                            .props('dense dark outlined')
                            .classes('w-28')
                        )

                        async def _set_target(_e=None, sid=sid, inp=tgt_inp):
                            v = float(inp.value) if inp.value else None
                            await commands.update_slot(redis, sid, target_price=v)
                            ui.notify(
                                f"Target {'cleared' if v is None else f'→ {v:g}'}",
                                type='positive' if v else 'info',
                                position='bottom-right',
                            )

                        (ui.button('T', on_click=_set_target)
                         .props('dense unelevated size=xs')
                         .classes('bg-blue-900/80 text-blue-300 px-2.5 min-w-0')
                         .tooltip('Set target'))

                        ui.element('div').classes('flex-1')

                        # Partial qty input
                        pqty_inp = (
                            ui.number(value=None, min=0, placeholder='Qty')
                            .props('dense dark outlined')
                            .classes('w-24')
                        )

                        async def _half(_e=None, sid=sid):
                            for p in state.positions:
                                if p.get('slot_id') == sid:
                                    half = float(p.get('qty', 0) or 0) / 2
                                    if half > 0:
                                        await commands.partial_close_slot(redis, sid, half)
                                        ui.notify(
                                            f'½ close queued ({half:g})',
                                            type='warning', position='bottom-right',
                                        )
                                    return

                        (ui.button('½', on_click=_half)
                         .props('dense unelevated size=xs')
                         .classes('bg-yellow-900/80 text-yellow-300 px-2.5 min-w-0')
                         .tooltip('Close half at market'))

                        async def _partial(_e=None, sid=sid, inp=pqty_inp):
                            try:
                                v = float(inp.value or 0)
                            except (ValueError, TypeError):
                                v = 0.0
                            if v > 0:
                                await commands.partial_close_slot(redis, sid, v)
                                ui.notify(
                                    f'Partial close {v:g} queued',
                                    type='warning', position='bottom-right',
                                )
                            else:
                                ui.notify('Enter qty', type='info',
                                          position='bottom-right')

                        (ui.button('Close Qty', on_click=_partial)
                         .props('dense unelevated size=xs')
                         .classes('bg-amber-900/80 text-amber-300 text-xs px-2.5 min-w-0'))

                        async def _close_all(_e=None, sid=sid, sym=sym):
                            await commands.close_slot(redis, sid)
                            ui.notify(f'Closing {sym}…', type='negative',
                                      position='bottom-right')

                        (ui.button('✕ All', on_click=_close_all)
                         .props('dense unelevated size=xs')
                         .classes('bg-red-900/80 text-red-300 px-2.5 min-w-0')
                         .tooltip('Close full position at market'))

        rows()

    # ── Tick-rate update (in-place, no DOM rebuild) ────────────────────────────
    def update() -> None:
        n = len(state.positions)
        count_lbl.set_text(f'{n} position{"s" if n != 1 else ""}')

        new_ids = '|'.join(p.get('slot_id', '') for p in state.positions)
        if new_ids != prev_ids['v']:
            prev_ids['v'] = new_ids
            rows.refresh()
            return

        for pos in state.positions:
            sid      = pos.get('slot_id', '')
            mark     = float(pos.get('current_price',  0) or 0)
            upnl     = float(pos.get('unrealized_pnl', 0) or 0)
            exchange = pos.get('exchange', '')
            qty      = float(pos.get('qty',            0) or 0)
            fr_raw   = pos.get('funding_rate')

            taker    = settings.EXCHANGE_FEES.get(exchange, {'taker': 0.001})['taker']
            exit_fee = round(mark * qty * taker, 4)
            dv       = mark * qty
            pcol     = 'text-green-400' if upnl >= 0 else 'text-red-400'

            if sid in mark_refs:
                mark_refs[sid].set_text(f'{mark:,.6g}' if mark else '—')
            if sid in dv_refs:
                dv_refs[sid].set_text(f'${dv:,.2f}')
            if sid in pnl_refs:
                pnl_refs[sid].set_text(f"{'+'if upnl>=0 else ''}{upnl:.4f}")
                pnl_refs[sid].classes(
                    remove='text-green-400 text-red-400', add=pcol
                )
            if sid in fee_refs:
                fee_refs[sid].set_text(f'${exit_fee:.4f}')
            if sid in fr_refs and fr_raw is not None:
                fr_refs[sid].set_text(f'{float(fr_raw)*100:.4f}%')

    return {'update': update}

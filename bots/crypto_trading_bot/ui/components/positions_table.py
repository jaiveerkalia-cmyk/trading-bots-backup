"""
Open Positions — card layout.
In-place updates for: mark, PnL, dollar value, qty, fee, funding, max P&L.
Stop/target/partial-close inputs are DOM-stable between ticks.
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
    # In-place label refs keyed by slot_id
    mark_refs: dict[str, ui.label] = {}
    pnl_refs:  dict[str, ui.label] = {}
    dv_refs:   dict[str, ui.label] = {}
    qty_refs:  dict[str, ui.label] = {}
    fee_refs:  dict[str, ui.label] = {}
    fr_refs:   dict[str, ui.label] = {}
    mp_refs:   dict[str, ui.label] = {}   # max profit
    ml_refs:   dict[str, ui.label] = {}   # max loss

    prev_ids  = {'v': ''}

    with ui.column().classes('w-full gap-0'):
        with ui.row().classes('w-full items-center justify-between px-1 mb-2'):
            ui.label('Open Positions').classes(
                'text-gray-300 font-semibold text-xs uppercase tracking-widest'
            )
            count_lbl = ui.label('').classes('text-gray-500 text-xs')

        @ui.refreshable
        def rows() -> None:
            for d in (mark_refs, pnl_refs, dv_refs, qty_refs,
                      fee_refs, fr_refs, mp_refs, ml_refs):
                d.clear()

            if not state.positions:
                with ui.row().classes('w-full justify-center py-8 gap-2'):
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
                leverage = int(pos.get('leverage', 1) or 1)
                fr_raw   = pos.get('funding_rate')

                slot    = state.get_slot(sid)
                cur_stp = float((slot or {}).get('stop_price')   or 0) or None
                cur_tgt = float((slot or {}).get('target_price') or 0) or None

                taker    = settings.EXCHANGE_FEES.get(exchange, {'taker': 0.001})['taker']
                exit_fee = round(mark * qty * taker, 4)
                dv       = mark * qty

                is_long  = side == 'long'
                bdr      = 'border-l-green-500' if is_long else 'border-l-red-500'
                side_bg  = ('bg-green-900/60 text-green-300'
                            if is_long else 'bg-red-900/60 text-red-300')
                pnl_col  = 'text-green-400' if upnl >= 0 else 'text-red-400'
                exch_s   = (exchange.upper()
                            .replace('_FUTURES', '-F').replace('BINANCE', 'BNF'))
                mode_s   = 'PAPER' if is_paper else 'LIVE'

                # Max profit / loss at current stop & target
                mp_str, ml_str = _max_pl(entry, cur_stp, cur_tgt, qty, taker, is_long)

                with ui.card().classes(
                    f'w-full bg-gray-800/80 rounded-xl mb-2.5 p-0 '
                    f'border-l-4 {bdr} shadow overflow-hidden'
                ):
                    # ── Row 1: identity + mark + size + PnL ──────────────────
                    with ui.row().classes(
                        'w-full px-3 pt-2.5 pb-2 items-center gap-x-3 gap-y-1 flex-wrap'
                    ):
                        with ui.column().classes('gap-0 leading-none'):
                            ui.label(f'{exch_s} · {mode_s}').classes(
                                'text-gray-500 text-xs'
                            )
                            ui.label(sym).classes(
                                'text-white font-bold text-[15px] tracking-wide'
                            )

                        ui.label(side.upper()).classes(
                            f'px-2.5 py-0.5 rounded-md text-xs font-bold '
                            f'tracking-widest {side_bg}'
                        )

                        if leverage > 1:
                            ui.label(f'{leverage}x').classes(
                                'px-1.5 py-0.5 rounded text-xs font-bold '
                                'bg-purple-900/60 text-purple-300'
                            )

                        ui.element('div').classes('flex-1')

                        # Mark price
                        with ui.column().classes('items-end gap-0 leading-none'):
                            ui.label('Mark').classes('text-gray-500 text-[10px]')
                            m = ui.label(
                                f'{mark:,.6g}' if mark else '—'
                            ).classes('text-yellow-300 font-mono text-sm font-semibold')
                            mark_refs[sid] = m

                        # Position $ size
                        with ui.column().classes('items-end gap-0 leading-none'):
                            ui.label('Size $').classes('text-gray-500 text-[10px]')
                            dv_l = ui.label(
                                f'${dv:,.2f}'
                            ).classes('text-gray-200 font-mono text-sm')
                            dv_refs[sid] = dv_l

                        # uPnL
                        with ui.column().classes('items-end gap-0 leading-none'):
                            ui.label('uPnL').classes('text-gray-500 text-[10px]')
                            p = ui.label(
                                f"{'+'if upnl>=0 else ''}{upnl:.4f}"
                            ).classes(f'font-mono text-sm font-bold {pnl_col}')
                            pnl_refs[sid] = p

                    # ── Row 2: secondary stats ────────────────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-1.5 items-center gap-x-4 text-xs '
                        'border-t border-gray-700/50 flex-wrap'
                    ):
                        _stat('Entry', f'{entry:g}' if entry else '—')

                        with ui.row().classes('items-center gap-1'):
                            ui.label('Qty').classes('text-gray-500')
                            q_l = ui.label(f'{qty:g}').classes(
                                'font-mono text-gray-300'
                            )
                            qty_refs[sid] = q_l

                        if is_fut:
                            _stat('Liq', f'{liq:g}' if liq else '—', 'text-orange-400')
                            with ui.row().classes('items-center gap-1'):
                                ui.label('Fund').classes('text-gray-500')
                                fr_l = ui.label(
                                    f'{float(fr_raw)*100:.4f}%'
                                    if fr_raw is not None else '—'
                                ).classes('font-mono text-gray-300')
                                fr_refs[sid] = fr_l

                        with ui.row().classes('items-center gap-1'):
                            ui.label('ExFee').classes('text-gray-500')
                            f_l = ui.label(f'${exit_fee:.4f}').classes(
                                'font-mono text-gray-500'
                            )
                            fee_refs[sid] = f_l

                    # ── Row 3: max P&L ────────────────────────────────────────
                    with ui.row().classes(
                        'w-full px-3 py-1 items-center gap-x-4 text-xs '
                        'border-t border-gray-700/30 flex-wrap'
                    ):
                        with ui.row().classes('items-center gap-1'):
                            ui.label('Max Loss').classes('text-gray-500')
                            ml_l = ui.label(ml_str).classes(
                                'font-mono text-red-400'
                            )
                            ml_refs[sid] = ml_l

                        with ui.row().classes('items-center gap-1'):
                            ui.label('Max Profit').classes('text-gray-500')
                            mp_l = ui.label(mp_str).classes(
                                'font-mono text-green-400'
                            )
                            mp_refs[sid] = mp_l

                    # ── Row 4: controls ───────────────────────────────────────
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

                        ui.button('S', on_click=_set_stop).props(
                            'dense unelevated size=xs'
                        ).classes('bg-orange-900/80 text-orange-300 px-2').tooltip('Set stop')

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

                        ui.button('T', on_click=_set_target).props(
                            'dense unelevated size=xs'
                        ).classes('bg-blue-900/80 text-blue-300 px-2').tooltip('Set target')

                        ui.element('div').classes('flex-1')

                        # Partial close — input is PERCENT (0–100)
                        pct_inp = (
                            ui.number(
                                value=None, min=0, max=100,
                                placeholder='%',
                            )
                            .props('dense dark outlined')
                            .classes('w-20')
                        )

                        async def _half(_e=None, sid=sid):
                            await commands.partial_close_slot(redis, sid,
                                _pct_qty(state, sid, 50.0))
                            ui.notify('½ close (50%) queued', type='warning',
                                      position='bottom-right')

                        ui.button('½', on_click=_half).props(
                            'dense unelevated size=xs'
                        ).classes('bg-yellow-900/80 text-yellow-300 px-2').tooltip('Close 50%')

                        async def _partial(_e=None, sid=sid, inp=pct_inp):
                            try:
                                pct = float(inp.value or 0)
                            except (ValueError, TypeError):
                                pct = 0.0
                            if 0 < pct <= 100:
                                qty = _pct_qty(state, sid, pct)
                                if qty > 0:
                                    await commands.partial_close_slot(redis, sid, qty)
                                    ui.notify(f'Close {pct:g}% queued',
                                              type='warning',
                                              position='bottom-right')
                            else:
                                ui.notify('Enter 1–100%', type='info',
                                          position='bottom-right')

                        ui.button('Close %', on_click=_partial).props(
                            'dense unelevated size=xs'
                        ).classes('bg-amber-900/80 text-amber-300 text-xs px-2')

                        async def _close(_e=None, sid=sid, sym=sym):
                            await commands.close_slot(redis, sid)
                            ui.notify(f'Closing {sym}…', type='negative',
                                      position='bottom-right')

                        ui.button(icon='close', on_click=_close).props(
                            'dense unelevated size=xs'
                        ).classes('bg-red-900/80 text-red-300 px-2').tooltip('Close full position')

        rows()

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
            qty      = float(pos.get('qty',            0) or 0)
            exchange = pos.get('exchange', '')
            fr_raw   = pos.get('funding_rate')
            taker    = settings.EXCHANGE_FEES.get(exchange, {'taker': 0.001})['taker']
            pnl_col  = 'text-green-400' if upnl >= 0 else 'text-red-400'

            if sid in mark_refs:
                mark_refs[sid].set_text(f'{mark:,.6g}' if mark else '—')
            if sid in dv_refs:
                dv_refs[sid].set_text(f'${mark * qty:,.2f}')
            if sid in qty_refs:
                qty_refs[sid].set_text(f'{qty:g}')
            if sid in pnl_refs:
                pnl_refs[sid].set_text(f"{'+'if upnl>=0 else ''}{upnl:.4f}")
                pnl_refs[sid].classes(
                    remove='text-green-400 text-red-400', add=pnl_col
                )
            if sid in fee_refs:
                fee_refs[sid].set_text(f'${mark * qty * taker:.4f}')
            if sid in fr_refs and fr_raw is not None:
                fr_refs[sid].set_text(f'{float(fr_raw)*100:.4f}%')

            # Max P&L — recalculate with current stop/target from slot
            slot    = state.get_slot(sid)
            entry   = float(pos.get('entry_price', 0) or 0)
            stp     = float((slot or {}).get('stop_price')   or 0) or None
            tgt     = float((slot or {}).get('target_price') or 0) or None
            is_long = pos.get('side') == 'long'
            mp_str, ml_str = _max_pl(entry, stp, tgt, qty, taker, is_long)
            if sid in ml_refs:
                ml_refs[sid].set_text(ml_str)
            if sid in mp_refs:
                mp_refs[sid].set_text(mp_str)

    return {'update': update}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _stat(label: str, value: str, value_cls: str = 'text-gray-200') -> None:
    with ui.row().classes('items-center gap-1'):
        ui.label(label).classes('text-gray-500')
        ui.label(value).classes(f'font-mono {value_cls}')


def _max_pl(
    entry: float,
    stop:  float | None,
    tgt:   float | None,
    qty:   float,
    taker: float,
    is_long: bool,
) -> tuple[str, str]:
    mp_str = '—'
    ml_str = '—'
    if entry <= 0 or qty <= 0:
        return mp_str, ml_str

    if stop:
        gross_loss = abs(entry - stop) * qty
        exit_fee   = stop * qty * taker
        ml = gross_loss + exit_fee
        ml_str = f'-${ml:,.4f}'

    if tgt:
        gross_profit = abs(tgt - entry) * qty
        exit_fee     = tgt * qty * taker
        mp = gross_profit - exit_fee
        mp_str = f'+${mp:,.4f}' if mp > 0 else f'${mp:,.4f}'

    return mp_str, ml_str

def _pct_qty(state, slot_id: str, pct: float) -> float:
    """Convert a percentage to an absolute qty based on current position size."""
    for p in state.positions:
        if p.get('slot_id') == slot_id:
            qty = float(p.get('qty', 0) or 0)
            return round(qty * pct / 100.0, 8)
    return 0.0

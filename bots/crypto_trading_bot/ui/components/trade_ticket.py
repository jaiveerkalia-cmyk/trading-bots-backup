from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands
from common import settings

if TYPE_CHECKING:
    from ui.state import UIState


def build(side: str, state: 'UIState', redis: aioredis.Redis, shared: dict) -> None:
    is_long = side == 'long'
    title   = 'Long / Buy'  if is_long else 'Short / Sell'
    accent  = 'green'       if is_long else 'red'
    btn_col = 'positive'    if is_long else 'negative'

    legs: list[dict] = [{'price': 0.0, 'qty': 0.0}]
    p = {
        'order_type':  'limit',
        'stop':        None,
        'target':      None,
        'leverage':    1,
        'margin_mode': 'cross',
        'qty_mode':    'risk',
    }
    # Mutable refs so refreshed legs_ui can access after creation
    refs: dict = {'qty_lbl': None, 'fee_lbl': None}

    with ui.card().classes(
        f'w-full p-3 bg-gray-900 border border-{accent}-900 rounded-lg'
    ):
        ui.label(title).classes(f'text-{accent}-400 font-bold text-sm mb-2')

        ui.select(
            ['market', 'limit', 'stop_limit'],
            value='limit',
            label='Order type',
            on_change=lambda e: (
                p.update({'order_type': e.value}),
                legs_ui.refresh(),
            ),
        ).props('dense dark outlined').classes('w-full mb-2')

        @ui.refreshable
        def legs_ui():
            for i, leg in enumerate(legs):
                with ui.row().classes('w-full gap-1 items-center mb-1'):
                    # Entry price hidden for market orders
                    if p.get('order_type', 'limit') != 'market':
                        ep = ui.number(
                            label=f'Entry {i+1}',
                            value=leg['price'],
                            min=0,
                            format='%.4f',
                        ).props('dense dark outlined').classes('flex-1')
                        ep.on('update:model-value',
                            lambda e, idx=i: (
                                legs[idx].update({'price': float(e.args or 0)}),
                                _recalc(legs, p, shared, refs, state),
                            ))

                    qp = ui.number(
                        label='Qty',
                        value=leg['qty'],
                        min=0,
                        format='%.6f',
                    ).props('dense dark outlined').classes('flex-1')
                    qp.on('update:model-value',
                        lambda e, idx=i: legs[idx].update(
                            {'qty': float(e.args or 0)}
                        ))

                    if len(legs) > 1:
                        ui.button(
                            icon='close', color='negative',
                            on_click=lambda idx=i: (
                                legs.pop(idx) if len(legs) > 1 else None,
                                legs_ui.refresh(),
                            ),
                        ).props('flat round dense size=xs')

        legs_ui()

        ui.button(
            '+ Add entry',
            on_click=lambda: (
                legs.append({'price': 0.0, 'qty': 0.0}),
                legs_ui.refresh(),
            ),
        ).props('flat dense size=sm').classes(f'text-{accent}-400 mb-1')

        # Stop / Target
        with ui.row().classes('w-full gap-2 mb-2'):
            ui.number(
                label='Stop', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: (
                    p.update({'stop': float(e.args) if e.args else None}),
                    _recalc(legs, p, shared, refs, state),
                ),
            )
            ui.number(
                label='Target (opt)', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: p.update(
                    {'target': float(e.args) if e.args else None}
                ),
            )

        # Leverage / Margin
        with ui.row().classes('w-full gap-2 mb-2'):
            ui.number(
                label='Leverage', value=1, min=1, max=100, step=1,
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: p.update({'leverage': int(e.args or 1)}),
            )
            ui.select(
                ['cross', 'isolated'], value='cross', label='Margin',
                on_change=lambda e: p.update({'margin_mode': e.value}),
            ).props('dense dark outlined').classes('flex-1')

        # Qty mode toggle + auto label
        with ui.row().classes('w-full items-center gap-2 mb-1'):
            ui.toggle(
                {'risk': 'Auto (Risk%)', 'manual': 'Manual'},
                value='risk',
                on_change=lambda e: (
                    p.update({'qty_mode': e.value}),
                    _recalc(legs, p, shared, refs, state),
                ),
            ).props('dense').classes('text-xs')
            qty_lbl = ui.label('Qty: --').classes('text-xs text-gray-400 flex-1')
            refs['qty_lbl'] = qty_lbl

        # Fee estimate
        fee_lbl = ui.label('').classes('text-xs text-gray-500 mb-2')
        refs['fee_lbl'] = fee_lbl

        async def place():
            await _place(side, legs, p, shared, redis, state, refs)

        ui.button(
            f'Place {title}', on_click=place, color=btn_col,
        ).classes('w-full mt-1')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recalc(legs, p, shared, refs, state) -> None:
    qty_lbl = refs.get('qty_lbl')
    fee_lbl = refs.get('fee_lbl')

    order_type = p.get('order_type', 'limit')
    stop       = p.get('stop') or 0

    # Entry price: for market orders use last tick price
    if order_type == 'market':
        entry = state.get_last_price() if hasattr(state, 'get_last_price') else 0
    else:
        entry = legs[0]['price'] if legs else 0

    if p.get('qty_mode') == 'risk' and entry > 0 and stop > 0 and abs(entry - stop) > 0:
        risk_amt  = shared.get('balance', 0) * (shared.get('risk_pct', 0.5) / 100)
        total_qty = round(risk_amt / abs(entry - stop), 6)
        per_leg   = round(total_qty / max(len(legs), 1), 6)
        for leg in legs:
            leg['qty'] = per_leg
        if qty_lbl:
            qty_lbl.set_text(f'Qty: {total_qty}')

        # Fee estimate
        exchange = shared.get('exchange', 'binance')
        fee_rate = settings.EXCHANGE_FEES.get(exchange, 0.001)
        est_fee  = entry * total_qty * fee_rate * 2  # entry + exit
        if fee_lbl:
            fee_lbl.set_text(f'Est. fee (round trip): ~${est_fee:.4f}')
    else:
        if qty_lbl:
            qty_lbl.set_text('Qty: --')
        if fee_lbl:
            fee_lbl.set_text('')


async def _place(side, legs, p, shared, redis, state, refs) -> None:
    order_type = p.get('order_type', 'limit')

    # For market orders, use last price for risk-based qty
    if order_type == 'market' and p.get('qty_mode') == 'risk':
        last_px = state.get_last_price() if hasattr(state, 'get_last_price') else 0
        stop    = p.get('stop') or 0
        if last_px > 0 and stop > 0 and abs(last_px - stop) > 0:
            risk_amt  = shared.get('balance', 0) * (shared.get('risk_pct', 0.5) / 100)
            total_qty = round(risk_amt / abs(last_px - stop), 6)
            per_leg   = round(total_qty / max(len(legs), 1), 6)
            for leg in legs:
                leg['qty']  = per_leg
                leg['price'] = 0

    # Validate
    for leg in legs:
        if leg['qty'] <= 0:
            ui.notify('Qty must be > 0 — check stop price is set', type='negative')
            return
        if order_type != 'market' and leg['price'] <= 0:
            ui.notify('Entry price required for limit orders', type='negative')
            return

    if not p.get('stop'):
        ui.notify('Stop price is required', type='negative')
        return

    slot = {
        'exchange':        shared.get('exchange', 'binance'),
        'symbol':          shared.get('symbol', 'BTC/USDT'),
        'side':            side,
        'instrument_type': 'futures' if p.get('leverage', 1) > 1 else 'spot',
        'entries':         [
            {
                'price':      l['price'],
                'qty':        l['qty'],
                'order_type': order_type,
            }
            for l in legs
        ],
        'stop_price':  p.get('stop')   or None,
        'target_price': p.get('target') or None,
        'leverage':    p.get('leverage', 1),
        'margin_mode': p.get('margin_mode', 'cross'),
        'qty_mode':    'base',
        'risk_pct':    shared.get('risk_pct', settings.DEFAULT_RISK_PCT),
        'is_paper':    not state.live_mode,
    }
    await commands.open_slot(redis, slot)
    ui.notify(
        f'{"Long" if side == "long" else "Short"} order queued ✓',
        type='positive',
    )

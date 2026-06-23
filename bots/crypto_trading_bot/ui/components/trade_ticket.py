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

    # Per-ticket mutable state
    legs: list[dict] = [{'price': 0.0, 'qty': 0.0}]
    p = {
        'order_type': 'limit', 'stop': None, 'target': None,
        'leverage': 1, 'margin_mode': 'cross', 'qty_mode': 'risk',
    }

    with ui.card().classes(
        f'w-full p-3 bg-gray-900 border border-{accent}-900 rounded-lg'
    ):
        ui.label(title).classes(f'text-{accent}-400 font-bold text-sm mb-2')

        # Order type
        ui.select(
            ['market', 'limit', 'stop_limit'], value='limit', label='Order type',
        ).props('dense dark outlined').classes('w-full mb-2').on(
            'update:model-value', lambda e: p.update({'order_type': e.args})
        )

        # ── Entry legs ────────────────────────────────────────────────────────
        @ui.refreshable
        def legs_ui():
            for i, leg in enumerate(legs):
                with ui.row().classes('w-full gap-1 items-center mb-1'):
                    ep = ui.number(
                        label=f'Entry {i+1}', value=leg['price'], min=0, format='%.4f',
                    ).props('dense dark outlined').classes('flex-1')
                    ep.on('update:model-value',
                        lambda e, idx=i: _set_leg(idx, 'price', e.args, legs, p, shared, qty_lbl))

                    qp = ui.number(
                        label='Qty', value=leg['qty'], min=0, format='%.6f',
                    ).props('dense dark outlined').classes('flex-1')
                    qp.on('update:model-value',
                        lambda e, idx=i: _set_leg(idx, 'qty', e.args, legs, p, shared, qty_lbl))

                    if len(legs) > 1:
                        ui.button(icon='close', color='negative',
                            on_click=lambda idx=i: (_rm(idx, legs), legs_ui.refresh()),
                        ).props('flat round dense size=xs')

        legs_ui()

        ui.button('+ Add entry',
            on_click=lambda: (_add(legs), legs_ui.refresh()),
        ).props('flat dense size=sm').classes(f'text-{accent}-400 mb-1')

        # Stop / Target
        with ui.row().classes('w-full gap-2 mb-2'):
            ui.number(label='Stop', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: (p.update({'stop': float(e.args) if e.args else None}),
                           _recalc(legs, p, shared, qty_lbl)),
            )
            ui.number(label='Target', value=0, min=0, format='%.4f',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: p.update({'target': float(e.args) if e.args else None}),
            )

        # Leverage / Margin
        with ui.row().classes('w-full gap-2 mb-2'):
            ui.number(label='Leverage', value=1, min=1, max=100, step=1,
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value', lambda e: p.update({'leverage': int(e.args or 1)})
            )
            ui.select(['cross', 'isolated'], value='cross', label='Margin',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value', lambda e: p.update({'margin_mode': e.args})
            )

        # Qty mode
        with ui.row().classes('w-full items-center gap-2 mb-2'):
            ui.toggle({'risk': 'Auto', 'manual': 'Manual'}, value='risk',
            ).props('dense').classes('text-xs').on(
                'update:model-value', lambda e: p.update({'qty_mode': e.args})
            )
            qty_lbl = ui.label('Qty: --').classes('text-xs text-gray-400 flex-1')

        # Place button
        async def place():
            await _place(side, legs, p, shared, redis, state)

        ui.button(f'Place {title}', on_click=place, color=btn_col).classes('w-full mt-1')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _add(legs: list) -> None:
    legs.append({'price': 0.0, 'qty': 0.0})

def _rm(i: int, legs: list) -> None:
    if len(legs) > 1:
        legs.pop(i)

def _set_leg(i, key, val, legs, p, shared, qty_lbl) -> None:
    legs[i][key] = float(val or 0)
    if key == 'price':
        _recalc(legs, p, shared, qty_lbl)

def _recalc(legs, p, shared, qty_lbl) -> None:
    entry = legs[0]['price'] if legs else 0
    stop  = p.get('stop') or 0
    if p.get('qty_mode') == 'risk' and entry > 0 and stop > 0 and entry != stop:
        risk_amt  = shared.get('balance', 0) * (shared.get('risk_pct', 0.5) / 100)
        total_qty = round(risk_amt / abs(entry - stop), 6)
        per_leg   = round(total_qty / max(len(legs), 1), 6)
        for leg in legs:
            leg['qty'] = per_leg
        qty_lbl.set_text(f'Qty: {total_qty}')
    else:
        qty_lbl.set_text('Qty: --')

async def _place(side, legs, p, shared, redis, state) -> None:
    for leg in legs:
        if leg['price'] <= 0 or leg['qty'] <= 0:
            ui.notify('All entries need price > 0 and qty > 0', type='negative')
            return
    slot = {
        'exchange':        shared.get('exchange', 'binance'),
        'symbol':          shared.get('symbol',   'BTC/USDT'),
        'side':            side,
        'instrument_type': 'futures' if p.get('leverage', 1) > 1 else 'spot',
        'entries':         [{'price': l['price'], 'qty': l['qty'],
                             'order_type': p.get('order_type', 'limit')} for l in legs],
        'stop_price':      p.get('stop')   or None,
        'target_price':    p.get('target') or None,
        'leverage':        p.get('leverage', 1),
        'margin_mode':     p.get('margin_mode', 'cross'),
        'qty_mode':        'base',
        'risk_pct':        shared.get('risk_pct', settings.DEFAULT_RISK_PCT),
        'is_paper':        not state.live_mode,
    }
    await commands.open_slot(redis, slot)
    ui.notify(f'{"Long" if side == "long" else "Short"} order queued ✓', type='positive')

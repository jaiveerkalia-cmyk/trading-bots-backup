"""
Simplified trade ticket.
- No manual leverage input: effective leverage is auto-calculated and displayed.
- Shows both last trade (chart) price and mark price (futures).
- Shows position size in $, effective leverage vs portfolio, fees, breakeven.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from nicegui import ui
import redis.asyncio as aioredis
from ui import commands
from common import settings

if TYPE_CHECKING:
    from ui.state import UIState


def _fees(exchange: str) -> tuple[float, float]:
    f = settings.EXCHANGE_FEES.get(exchange, {'maker': 0.001, 'taker': 0.001})
    return f['maker'], f['taker']


def build(side: str, state: 'UIState', redis: aioredis.Redis, shared: dict) -> dict:
    is_long = side == 'long'
    title   = 'Long / Buy'  if is_long else 'Short / Sell'
    accent  = 'green'       if is_long else 'red'
    btn_col = 'positive'    if is_long else 'negative'

    f = {
        'order_type':   'limit',
        'entry_price':  0.0,
        'stop_price':   0.0,
        'target_price': 0.0,
        'qty':          0.0,
        'margin_mode':  'cross',
        'qty_mode':     'risk',
    }
    refs = {'price_lbl': None, 'info_lbl': None, 'fee_lbl': None, 'be_lbl': None}
    had_pos = {'v': False}

    with ui.card().classes(
        f'w-full p-3 bg-gray-900 border border-{accent}-900 rounded-lg'
    ):
        ui.label(title).classes(f'text-{accent}-400 font-bold text-sm mb-1')

        # Price display — chart price + mark price for futures
        price_lbl = ui.label('—').classes('text-yellow-300 text-xs font-mono mb-2')
        refs['price_lbl'] = price_lbl

        # Order type
        ui.select(
            ['market', 'limit', 'stop_limit'], value='limit',
            label='Order type',
            on_change=lambda e: (
                f.update({'order_type': e.value}),
                entry_row.set_visibility(e.value != 'market'),
                _recalc(f, shared, refs, state),
            ),
        ).props('dense dark outlined').classes('w-full mb-2')

        # Entry price (hidden for market orders)
        entry_row = ui.row().classes('w-full mb-2')
        with entry_row:
            ui.number(
                label='Entry price', value=0, min=0, format='%g',
            ).props('dense dark outlined').classes('w-full').on(
                'update:model-value',
                lambda e: (
                    f.update({'entry_price': float(e.args or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            )

        # Stop / Target
        with ui.row().classes('w-full gap-2 mb-2'):
            ui.number(
                label='Stop', value=0, min=0, format='%g',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: (
                    f.update({'stop_price': float(e.args or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            )
            ui.number(
                label='Target (opt)', value=0, min=0, format='%g',
            ).props('dense dark outlined').classes('flex-1').on(
                'update:model-value',
                lambda e: f.update({'target_price': float(e.args or 0)}),
            )

        # Margin mode (futures only — shown always for simplicity)
        ui.select(
            ['cross', 'isolated'], value='cross', label='Margin mode',
            on_change=lambda e: f.update({'margin_mode': e.value}),
        ).props('dense dark outlined').classes('w-full mb-2')

        # Qty mode
        with ui.row().classes('w-full items-center gap-2 mb-1'):
            ui.toggle(
                {'risk': 'Auto (Risk%)', 'manual': 'Manual'},
                value='risk',
                on_change=lambda e: (
                    f.update({'qty_mode': e.value}),
                    qty_row.set_visibility(e.value == 'manual'),
                    _recalc(f, shared, refs, state),
                ),
            ).props('dense').classes('text-xs')

        qty_row = ui.row().classes('w-full mb-1')
        qty_row.set_visibility(False)
        with qty_row:
            ui.number(
                label='Qty (base)', value=0, min=0, format='%g',
            ).props('dense dark outlined').classes('w-full').on(
                'update:model-value',
                lambda e: (
                    f.update({'qty': float(e.args or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            )

        # Info displays
        info_lbl = ui.label('').classes('text-xs text-gray-400 mb-1')
        refs['info_lbl'] = info_lbl
        fee_lbl  = ui.label('').classes('text-xs text-gray-500')
        refs['fee_lbl'] = fee_lbl
        be_lbl   = ui.label('').classes('text-xs text-blue-400 mb-2')
        refs['be_lbl'] = be_lbl

        async def place():
            await _place(side, f, shared, redis, state)

        with ui.row().classes('w-full gap-2'):
            ui.button(f'Place {title}', on_click=place, color=btn_col).classes('flex-1')
            ui.button('Clear', on_click=lambda: _clear(f, refs),
            ).props('flat dense').classes('text-gray-500')

    def update():
        ltp  = state.get_last_price()
        mark = state.get_mark_price()

        if ltp:
            if state.is_futures() and mark and abs(mark - ltp) > 0.0001:
                price_lbl.set_text(f'Chart: ${ltp:,.4f}  |  Mark: ${mark:,.4f}')
            elif state.is_futures():
                price_lbl.set_text(f'Mark: ${mark:,.4f}')
            else:
                price_lbl.set_text(f'Price: ${ltp:,.4f}')

        # Recalc qty for market orders when price updates
        if f.get('order_type') == 'market' and f.get('qty_mode') == 'risk':
            _recalc(f, shared, refs, state)

        # Auto-clear when this side's position closes
        has_pos = any(
            p.get('side') == side and
            p.get('symbol')   == shared.get('symbol')   and
            p.get('exchange') == shared.get('exchange')
            for p in state.positions
        )
        if had_pos['v'] and not has_pos:
            _clear(f, refs)
        had_pos['v'] = has_pos

    return {'update': update}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recalc(f, shared, refs, state) -> None:
    order_type = f.get('order_type', 'limit')
    stop       = f.get('stop_price', 0) or 0
    exchange   = shared.get('exchange', 'binance')
    balance    = max(shared.get('balance', 0), 1)
    maker_fee, taker_fee = _fees(exchange)
    entry_fee_rate = maker_fee if order_type == 'limit' else taker_fee

    # Entry price — chart price for display; mark price used for futures fills
    if order_type == 'market':
        entry = (state.get_mark_price() if state.is_futures() else state.get_last_price()) \
                if hasattr(state, 'get_last_price') else 0
    else:
        entry = f.get('entry_price', 0) or 0

    info_lbl = refs.get('info_lbl')
    fee_lbl  = refs.get('fee_lbl')
    be_lbl   = refs.get('be_lbl')

    # Auto qty
    if f.get('qty_mode') == 'risk' and entry > 0 and stop > 0 and abs(entry - stop) > 0:
        risk_amt = balance * (shared.get('risk_pct', 0.5) / 100)
        qty      = round(risk_amt / abs(entry - stop), 8)
        f['qty'] = qty
    qty = f.get('qty', 0) or 0

    # Position size & effective leverage
    if entry > 0 and qty > 0:
        pos_usd = entry * qty
        eff_lev = pos_usd / balance
        if info_lbl:
            info_lbl.set_text(
                f'Qty: {qty:g}  |  Position: ${pos_usd:,.2f}  |  '
                f'{eff_lev:.1f}x vs portfolio'
            )

        # Fee estimate
        entry_fee = entry * qty * entry_fee_rate
        exit_fee  = entry * qty * taker_fee
        total_fee = entry_fee + exit_fee
        if fee_lbl:
            fee_lbl.set_text(
                f'Entry fee: ${entry_fee:.4f} ({entry_fee_rate*100:.3f}%)  '
                f'Exit fee: ~${exit_fee:.4f} ({taker_fee*100:.3f}%)'
            )

        # Breakeven
        if entry_fee_rate + taker_fee < 1:
            be_long  = entry * (1 + entry_fee_rate) / (1 - taker_fee)
            be_short = entry * (1 - entry_fee_rate) / (1 + taker_fee)
            be_pts   = abs(be_long - entry)
            if be_lbl:
                be_lbl.set_text(
                    f'Breakeven: ±{be_pts:.4f} pts  '
                    f'(L: {be_long:,.4f}  |  S: {be_short:,.4f})'
                )
    else:
        if info_lbl: info_lbl.set_text('Qty: set stop to auto-calculate')
        if fee_lbl:  fee_lbl.set_text('')
        if be_lbl:   be_lbl.set_text('')


def _clear(f, refs) -> None:
    f.update({'entry_price': 0.0, 'stop_price': 0.0, 'target_price': 0.0, 'qty': 0.0})
    for key in ('info_lbl', 'fee_lbl', 'be_lbl'):
        if refs.get(key):
            refs[key].set_text('')


async def _place(side, f, shared, redis, state) -> None:
    order_type = f.get('order_type', 'limit')
    stop       = f.get('stop_price', 0) or 0
    target     = f.get('target_price', 0) or 0

    # For market orders, use mark/last price as reference for qty calc
    if order_type == 'market':
        entry = (state.get_mark_price() if state.is_futures() else state.get_last_price())
        if f.get('qty_mode') == 'risk' and entry > 0 and stop > 0 and abs(entry - stop) > 0:
            risk_amt = shared.get('balance', 0) * (shared.get('risk_pct', 0.5) / 100)
            f['qty'] = round(risk_amt / abs(entry - stop), 8)
    else:
        entry = f.get('entry_price', 0) or 0

    qty = f.get('qty', 0) or 0

    if qty <= 0:
        ui.notify('Qty is 0 — set stop price to auto-calculate', type='negative')
        return
    if order_type != 'market' and entry <= 0:
        ui.notify('Entry price required for limit orders', type='negative')
        return
    if stop <= 0:
        ui.notify('Stop price is required', type='negative')
        return

    exchange = shared.get('exchange', 'binance')
    balance  = max(shared.get('balance', 0), 1)
    pos_usd  = entry * qty if entry > 0 else (state.get_last_price() * qty)
    eff_lev  = max(1, min(int(pos_usd / balance) + 1, 125))

    await commands.open_slot(redis, {
        'exchange':        exchange,
        'symbol':          shared.get('symbol', 'BTC/USDT'),
        'side':            side,
        'instrument_type': 'futures' if 'futures' in exchange or 'delta' in exchange else 'spot',
        'entries': [{'price': entry if order_type != 'market' else 0.0,
                     'qty': qty, 'order_type': order_type}],
        'stop_price':   stop   if stop   > 0 else None,
        'target_price': target if target > 0 else None,
        'leverage':     eff_lev,
        'margin_mode':  f.get('margin_mode', 'cross'),
        'qty_mode':     'base',
        'risk_pct':     shared.get('risk_pct', settings.DEFAULT_RISK_PCT),
        'is_paper':     not state.live_mode,
    })
    ui.notify(f'{"Long" if side == "long" else "Short"} order queued ✓', type='positive')

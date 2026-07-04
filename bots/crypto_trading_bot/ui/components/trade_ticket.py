"""
Trade ticket.
- 'Stop Market' order type: entry field = trigger price, fills at market when hit.
- Auto qty includes estimated entry + exit fees in the risk calculation.
- Form clears when a position opens OR closes.
- Input widgets are reset to 0 visually (not just internally).
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
        'side':         side,
        'order_type':   'limit',
        'entry_price':  0.0,
        'stop_price':   0.0,
        'target_price': 0.0,
        'qty':          0.0,
        'margin_mode':  'cross',
        'qty_mode':     'risk',
        'fire_on':      'current',
    }
    refs     = {}
    # Initialise had_pos with sym key to prevent false clears on context changes
    had_pos  = {'v': None, 'sym': None}

    with ui.card().classes(
        f'w-full p-3 bg-gray-900 border border-{accent}-900 rounded-lg'
    ):
        ui.label(title).classes(f'text-{accent}-400 font-bold text-sm mb-1')

        # Current instrument being traded
        ctx_lbl = ui.label('').classes('text-gray-500 text-xs font-mono mb-1')

        price_lbl = ui.label('—').classes('text-yellow-300 text-xs font-mono mb-2')
        refs['price_lbl'] = price_lbl

        # Order type — dict so we can label 'stop_limit' as 'Stop Market'
        ot_select = ui.select(
            {'market': 'Market', 'limit': 'Limit', 'stop_limit': 'Stop Market'},
            value='limit',
            label='Order type',
            on_change=lambda e: _on_ot_change(e.value),
        ).props('dense dark outlined').classes('w-full mb-2')
        refs['ot_select'] = ot_select

        # Entry / trigger price row
        entry_row = ui.row().classes('w-full mb-2')
        with entry_row:
            entry_inp = ui.number(
                label='Entry price', value=None, min=0,
                on_change=lambda e: (
                    f.update({'entry_price': float(e.value or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            ).props('dense dark outlined').classes('w-full')
        refs['entry_inp'] = entry_inp

        # Stop / Target
        with ui.row().classes('w-full gap-2 mb-2'):
            stop_inp = ui.number(
                label='Stop', value=None, min=0,
                on_change=lambda e: (
                    f.update({'stop_price': float(e.value or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            ).props('dense dark outlined').classes('flex-1')
            refs['stop_inp'] = stop_inp

            tgt_inp = ui.number(
                label='Target (opt)', value=None, min=0,
                on_change=lambda e: f.update(
                    {'target_price': float(e.value or 0)}
                ),
            ).props('dense dark outlined').classes('flex-1')
            refs['tgt_inp'] = tgt_inp

        # Margin mode
        ui.select(
            ['cross', 'isolated'], value='cross', label='Margin mode',
            on_change=lambda e: f.update({'margin_mode': e.value}),
        ).props('dense dark outlined').classes('w-full mb-2')

        # Qty mode toggle
        with ui.row().classes('w-full items-center gap-2 mb-1'):
            ui.toggle(
                {'risk': 'Auto (Risk%)', 'manual': 'Manual'}, value='risk',
                on_change=lambda e: (
                    f.update({'qty_mode': e.value}),
                    qty_row.set_visibility(e.value == 'manual'),
                    _recalc(f, shared, refs, state),
                ),
            ).props('dense').classes('text-xs')

        qty_row = ui.row().classes('w-full mb-1')
        qty_row.set_visibility(False)
        with qty_row:
            qty_inp = ui.number(
                label='Qty (base)', value=None, min=0,
                on_change=lambda e: (
                    f.update({'qty': float(e.value or 0)}),
                    _recalc(f, shared, refs, state),
                ),
            ).props('dense dark outlined').classes('w-full')
            refs['qty_inp'] = qty_inp

        info_lbl = ui.label('').classes('text-xs text-gray-400 mb-1')
        fee_lbl  = ui.label('').classes('text-xs text-gray-500')
        be_lbl   = ui.label('').classes('text-xs text-blue-400 mb-2')
        refs['info_lbl'] = info_lbl
        refs['fee_lbl']  = fee_lbl
        refs['be_lbl']   = be_lbl

        # Fire on: place immediately or wait for candle close
        with ui.row().classes('w-full items-center gap-2 mb-2'):
            ui.label('Fire on:').classes('text-xs text-gray-500 shrink-0')
            ui.select(
                {'current': 'Live', '1m': '1m Close', '5m': '5m Close'},
                value='current',
                on_change=lambda e: f.update({'fire_on': e.value}),
            ).props('dense dark outlined').classes('flex-1')

        async def place():
            await _place(side, f, shared, redis, state, refs)

        with ui.row().classes('w-full gap-2'):
            ui.button(f'Place {title}', on_click=place, color=btn_col).classes('flex-1')
            ui.button('Clear', on_click=lambda: _clear(f, refs)).props(
                'flat dense'
            ).classes('text-gray-500')

    def _on_ot_change(ot: str) -> None:
        f.update({'order_type': ot})
        entry_row.set_visibility(ot != 'market')
        label = 'Stop Trigger' if ot == 'stop_limit' else 'Entry price'
        entry_inp.props(f'label="{label}"')
        _recalc(f, shared, refs, state)

    def update() -> None:
        ctx_lbl.set_text(
            f"{shared.get('exchange','').upper().replace('_FUTURES','-F').replace('BINANCE','BNF')}"
            f"  ·  {shared.get('symbol','')}"
        )
        ltp  = state.get_last_price()
        max_val = state.get_mark_price() # Keep original logic naming structure via assignment
        mark = max_val
        if mark or ltp:
            if state.is_futures() and mark and ltp and abs(mark - ltp) > 0.01:
                price_lbl.set_text(f'Chart: ${ltp:,.2f}  |  Mark: ${mark:,.2f}')
            elif mark:
                price_lbl.set_text(f"{'Mark' if state.is_futures() else 'Price'}: ${mark:,.2f}")
            elif ltp:
                price_lbl.set_text(f'Price: ${ltp:,.2f}')

        if (f.get('order_type') == 'market'
                and f.get('qty_mode') == 'risk'
                and f.get('stop_price', 0) > 0):
            _recalc(f, shared, refs, state)

        cur_key = f"{shared.get('exchange','')}:{shared.get('symbol','')}"
        has_pos = any(
            p.get('side')     == side
            and p.get('symbol')   == shared.get('symbol')
            and p.get('exchange') == shared.get('exchange')
            for p in state.positions
        )
        
        # ── Symbol/exchange changed → reset tracking without clearing form ──
        if had_pos.get('sym') != cur_key:
            had_pos['sym'] = cur_key
            had_pos['v']   = has_pos   # re-initialise for new symbol
        elif had_pos['v'] is None:
            had_pos['v'] = has_pos
        elif not had_pos['v'] and has_pos:   # position opened on THIS symbol
            _clear(f, refs)
            had_pos['v'] = True
        elif had_pos['v'] and not has_pos:   # position closed on THIS symbol
            _clear(f, refs)
            had_pos['v'] = False

    return {'update': update}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recalc(f: dict, shared: dict, refs: dict, state) -> None:
    order_type = f.get('order_type', 'limit')
    stop       = f.get('stop_price', 0) or 0
    exchange   = shared.get('exchange', 'binance_futures')
    balance    = max(shared.get('balance', 0), 1)
    maker_fee, taker_fee = _fees(exchange)
    entry_fee_rate = maker_fee if order_type == 'limit' else taker_fee

    entry = (
        (state.get_mark_price() or state.get_last_price())
        if order_type == 'market'
        else (f.get('entry_price', 0) or 0)
    )

    info_lbl = refs.get('info_lbl')
    fee_lbl  = refs.get('fee_lbl')
    be_lbl   = refs.get('be_lbl')

    if (f.get('qty_mode') == 'risk'
            and entry > 0 and stop > 0 and abs(entry - stop) > 0):
        risk_amt = balance * (shared.get('risk_pct', 0.5) / 100)
        # Include entry fee + exit fee at stop in the risk calc
        fee_per_unit = entry * entry_fee_rate + stop * taker_fee
        qty = round(risk_amt / (abs(entry - stop) + fee_per_unit), 8)
        f['qty'] = qty

        # Auto-calculate fee-aware R:R target
        rr   = float(shared.get('rr_ratio', 2.0) or 2.0)
        side = f.get('side', 'long')
        if rr > 0 and qty > 0:
            if side == 'long':
                denom = qty * (1.0 - taker_fee)
                auto_tgt = round(
                    (rr * risk_amt + entry * qty * (1.0 + entry_fee_rate)) / denom, 6
                ) if denom > 0 else 0.0
            else:
                denom = qty * (1.0 + taker_fee)
                auto_tgt = round(
                    (entry * qty * (1.0 - entry_fee_rate) - rr * risk_amt) / denom, 6
                ) if denom > 0 else 0.0
            if auto_tgt > 0:
                f['target_price'] = auto_tgt
                tgt_ref = refs.get('tgt_inp')
                if tgt_ref:
                    try:
                        tgt_ref.set_value(auto_tgt)
                    except Exception:
                        pass
    elif f.get('qty_mode') == 'risk':
        f['qty'] = 0.0

    qty = f.get('qty', 0) or 0

    if entry > 0 and qty > 0:
        pos_usd   = entry * qty
        eff_lev   = pos_usd / balance
        entry_fee = entry * qty * entry_fee_rate
        exit_fee  = entry * qty * taker_fee
        if info_lbl:
            info_lbl.set_text(
                f'Qty: {qty:g}  |  ${pos_usd:,.2f}  |  {eff_lev:.1f}x'
            )
        if fee_lbl:
            fee_lbl.set_text(
                f'EntryFee: ${entry_fee:.4f}  ExitFee: ~${exit_fee:.4f}'
            )
        denom_l = 1 - taker_fee
        denom_s = 1 + taker_fee
        if denom_l > 0 and be_lbl:
            be_l = entry * (1 + entry_fee_rate) / denom_l
            be_s = entry * (1 - entry_fee_rate) / denom_s
            be_lbl.set_text(
                f'BE: L={be_l:,.2f}  S={be_s:,.2f}  (±{abs(be_l-entry):.2f})'
            )
    else:
        if info_lbl:
            if f.get('qty_mode') == 'manual':
                info_lbl.set_text('')   # manual mode: no auto-calc message
            elif order_type == 'market' or entry > 0:
                info_lbl.set_text('Qty: set stop to auto-calculate')
            else:
                info_lbl.set_text('Qty: set entry + stop to calculate')
        if fee_lbl: fee_lbl.set_text('')
        if be_lbl:  be_lbl.set_text('')


def _clear(f: dict, refs: dict) -> None:
    f.update({'entry_price': 0.0, 'stop_price': 0.0, 'target_price': 0.0, 'qty': 0.0})
    for k in ('entry_inp', 'stop_inp', 'tgt_inp', 'qty_inp'):
        if refs.get(k):
            try:
                refs[k].set_value(None)
            except Exception:
                pass
    for k in ('info_lbl', 'fee_lbl', 'be_lbl'):
        if refs.get(k):
            refs[k].set_text('')


async def _confirm(message: str, yes_label: str = 'Place Anyway',
                   no_label: str = 'Cancel') -> bool:
    """Awaitable Yes/No confirmation dialog. Returns True only if yes_label is clicked."""
    with ui.dialog() as dialog, ui.card().classes('bg-gray-800 p-4 max-w-sm'):
        ui.label(message).classes('text-sm text-gray-200 mb-3')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button(no_label, on_click=lambda: dialog.submit(False)).props(
                'flat dense'
            ).classes('text-gray-400')
            ui.button(yes_label, on_click=lambda: dialog.submit(True)).props(
                'dense unelevated'
            ).classes('bg-yellow-700 text-white')
    result = await dialog
    return bool(result)


async def _place(side: str, f: dict, shared: dict,
                 redis: aioredis.Redis, state, refs: dict) -> None:
    order_type = f.get('order_type', 'limit')
    stop       = f.get('stop_price', 0)   or 0
    target     = f.get('target_price', 0) or 0
    ref_price  = state.get_mark_price() or state.get_last_price()

    if order_type == 'market':
        entry = ref_price
        if f.get('qty_mode') == 'risk' and entry and stop and abs(entry - stop) > 0:
            exchange     = shared.get('exchange', 'binance_futures')
            balance      = max(shared.get('balance', 0), 1)
            mf, tf       = _fees(exchange)
            fee_per_unit = entry * tf + stop * tf
            f['qty']     = round(
                (balance * (shared.get('risk_pct', 0.5) / 100))
                / (abs(entry - stop) + fee_per_unit), 8
            )
    else:
        entry = f.get('entry_price', 0) or 0

    qty = f.get('qty', 0) or 0

    if qty <= 0:
        ui.notify('Qty is 0 — set entry and stop', type='negative')
        return
    if order_type not in ('market',) and entry <= 0:
        ui.notify('Entry / trigger price required', type='negative')
        return
    # Stop required only for auto-qty (risk mode needs it to calculate qty)
    if stop <= 0 and f.get('qty_mode') == 'risk':
        ui.notify('Stop price is required', type='negative')
        return

    ltp = state.get_last_price()

    # ── Limit price crosses LTP → would fill immediately ──────────────────────
    if order_type == 'limit' and ltp > 0:
        crosses = (
            (side == 'long'  and entry > ltp) or
            (side == 'short' and entry < ltp)
        )
        if crosses:
            rel = 'above' if side == 'long' else 'below'
            proceed = await _confirm(
                f'Limit price {entry:g} is {rel} the current LTP ({ltp:g}). '
                f'This will likely fill immediately. Place anyway?'
            )
            if not proceed:
                ui.notify('Order not placed', type='info')
                return

    # ── Stop trigger already met → would fire instantly ───────────────────────
    if order_type == 'stop_limit' and ltp > 0:
        instant = (
            (side == 'long'  and ltp >= entry) or
            (side == 'short' and ltp <= entry)
        )
        if instant:
            rel = 'at or below' if side == 'long' else 'at or above'
            use_limit = await _confirm(
                f'Stop trigger {entry:g} is {rel} the current LTP ({ltp:g}) '
                f'and will trigger instantly. Place as a Limit order instead?',
                yes_label='Place Limit', no_label='Cancel',
            )
            if not use_limit:
                ui.notify('Order not placed', type='info')
                return
            order_type = 'limit'

    # ── Wrong stop direction → warn ────────────────────────────────────────
    if entry > 0 and stop > 0:
        wrong_stop = (
            (side == 'long'  and stop >= entry) or
            (side == 'short' and stop <= entry)
        )
        if wrong_stop:
            correct = 'below' if side == 'long' else 'above'
            proceed = await _confirm(
                f'Stop {stop:g} is on the wrong side of entry {entry:g} '
                f'for a {side} position — stop should be {correct} entry. '
                f'Place anyway?'
            )
            if not proceed:
                ui.notify('Order not placed', type='info')
                return

    # ── Wrong target direction → warn ──────────────────────────────────────
    if entry > 0 and target > 0:
        wrong_tgt = (
            (side == 'long'  and target <= entry) or
            (side == 'short' and target >= entry)
        )
        if wrong_tgt:
            correct = 'above' if side == 'long' else 'below'
            proceed = await _confirm(
                f'Target {target:g} is on the wrong side of entry {entry:g} '
                f'for a {side} position — target should be {correct} entry. '
                f'Place anyway?'
            )
            if not proceed:
                ui.notify('Order not placed', type='info')
                return

    # ── Conditional order (1m / 5m candle close) ───────────────────────
    fire_on = f.get('fire_on', 'current')
    if fire_on in ('1m', '5m'):
        if entry <= 0:
            ui.notify('Set entry price for candle-close order', type='negative')
            return
        await commands.set_alert(redis, {
            'exchange':          shared.get('exchange', 'binance_futures'),
            'symbol':            shared.get('symbol', 'BTC/USDT'),
            # Long fires when close >= entry; Short fires when close <= entry
            'upper':             entry if side == 'long'  else None,
            'lower':             entry if side == 'short' else None,
            'period':            fire_on,
            'order_side':        side,
            'order_type':        order_type,
            'order_entry_price': entry  if order_type != 'market' else None,
            'order_stop':        stop   if stop   > 0 else None,
            'order_target':      target if target > 0 else None,
            'order_qty':         qty    if qty    > 0 else None,
            'order_risk_pct':    shared.get('risk_pct', settings.DEFAULT_RISK_PCT),
        })
        ui.notify(
            f'{fire_on} conditional {side} {order_type} @ {entry:g} set ✓',
            type='positive',
        )
        _clear(f, refs)
        return

    exchange = shared.get('exchange', 'binance_futures')
    balance  = max(shared.get('balance', 0), 1)
    pos_usd  = (entry or ref_price or 1) * qty
    eff_lev  = max(1, min(int(pos_usd / balance) + 1, 125))

    await commands.open_slot(redis, {
        'exchange':        exchange,
        'symbol':          shared.get('symbol', 'BTC/USDT'),
        'side':            side,
        'instrument_type': (
            'futures' if ('futures' in exchange or exchange == 'delta') else 'spot'
        ),
        'entries': [{
            'price':           entry if order_type != 'market' else 0.0,
            'qty':             qty,
            'order_type':      order_type,
            'reference_price': ref_price,
        }],
        'stop_price':   stop   if stop   > 0 else None,
        'target_price': target if target > 0 else None,
        'leverage':     eff_lev,
        'margin_mode':  f.get('margin_mode', 'cross'),
        'qty_mode':     'base',
        'risk_pct':     shared.get('risk_pct', settings.DEFAULT_RISK_PCT),
        'is_paper':     not state.live_mode,
    })

    ui.notify(
        f'{"Long" if side == "long" else "Short"} {order_type} queued ✓',
        type='positive',
    )
    _clear(f, refs)

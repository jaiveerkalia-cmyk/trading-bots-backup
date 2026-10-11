"""
Open Orders table: pending unified entry triggers (Limit/Stop-Market) and every active
conditional exit order (Premium-based and Index-based stop/target), table-styled with
expandable MODIFY/REMOVE rows.
"""
from nicegui import ui
from config import params, UI_OPTS, ui_refs, INDICES, shared_state
from datetime import datetime

from ._shared import _log_alert_action, _num_stepper
from .entry_exit_cards import _reset_unified_card_defaults
from . import _cancel_engine_job_if_any

# --- ORDER BOOK (pending unified entry triggers + active exit orders, table-styled) ---

def _toggle_expansion(exp):
    exp.value = not exp.value


def _orderbook_table_row(side, prefix):
    """One expandable row for a pending unified Open Short/Long entry trigger (Limit/Stop-
    Market only; Market fires immediately and never appears here). The header line stays
    live-bound to the underlying params (no manual refresh needed) -- this ALSO means it
    automatically picks up the stop_via_candle_engine's hand-off, since hand-off writes a
    fresh trigger_price/order_type/fire_on directly into these same params and re-arms; there
    is no separate 'deferred order' row, this row simply reflects whatever the live armed
    order currently is. MODIFY opens a staged edit panel (trigger price, strike/qty with -/+
    steppers, fire-on, stop, target) bound to a local draft, not the live params directly --
    edits only take effect after CONFIRM, which ALSO cancels any still-pending Enter via Stop
    job for this side first (see _cancel_engine_job_if_any) so a job mid-candle-fetch can
    never clobber a manual edit moments later. CONFIRM re-stamps armed_at (see
    logic_engine._check_unified_open) so a modified order's timing window restarts from the
    moment it was confirmed, not the original arm time. REMOVE fully resets that side's card
    back to defaults immediately (no confirm needed, matching the existing Cancel behavior
    elsewhere) via _reset_unified_card_defaults, which itself cancels any pending job.

    Side label (SELL/BUY) is mode-aware: Sell Mode entry orders open a SHORT position (so the
    order itself is a SELL), Buy Mode entry orders open a LONG position (so the order itself
    is a BUY) -- this reflects options_buy_mode live, since a pending (not yet filled) order
    has no trade dict yet to read a fixed direction from."""
    opt_type = 'CE' if side == 'Call' else 'PE'
    draft = {}

    def sync_draft():
        draft['order_type'] = params[f'{prefix}_order_type']
        draft['trigger_price'] = params[f'{prefix}_trigger_price']
        draft['strike_offset'] = params[f'{prefix}_strike_offset']
        draft['qty'] = params[f'{prefix}_qty']
        draft['fire_on'] = params[f'{prefix}_fire_on']
        draft['new_stop'] = params[f'{prefix}_new_stop']
        draft['new_target'] = params[f'{prefix}_new_target']

    sync_draft()

    def _txn_side_label(buy_mode):
        return 'BUY' if buy_mode else 'SELL'
    def _txn_side_cls(buy_mode):
        return 'w-14 text-blue-600 font-bold' if buy_mode else 'w-14 text-red-600 font-bold'

    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, f'{prefix}_armed')
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: INDICES.get(v, {}).get('segment', v)).classes('w-16 text-gray-400 font-mono')
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: f"{v} {opt_type}").classes('w-28 font-bold text-gray-800 font-mono')
                    txn_lbl = ui.label(_txn_side_label(params.get('options_buy_mode', False))).classes(_txn_side_cls(params.get('options_buy_mode', False)))
                    txn_lbl.bind_text_from(params, 'options_buy_mode', backward=_txn_side_label)
                    txn_lbl.bind_visibility_from(params, 'options_buy_mode', backward=lambda v: True)  # keep visible; classes set once at build, acceptable since mode can't change with orders armed
                    ui.label().bind_text_from(params, f'{prefix}_order_type').classes('w-24 text-purple-700')
                    ui.label().bind_text_from(params, f'{prefix}_trigger_price', backward=lambda v: f"{v}").classes('w-24 text-right font-mono text-gray-800')
                    ui.label().bind_text_from(params, f'{prefix}_fire_on').classes('w-16 text-gray-500')
                    ui.label().bind_text_from(params, f'{prefix}_new_stop', backward=lambda v: (str(v) if str(v).strip() != '' else '-')).classes('w-20 text-orange-600 text-right font-mono')
                    ui.label().bind_text_from(params, f'{prefix}_new_target', backward=lambda v: (str(v) if str(v).strip() != '' else '-')).classes('w-20 text-blue-600 text-right font-mono')
                    ui.label().bind_text_from(params, f'{prefix}_qty').classes('w-14 text-right font-mono text-gray-800')
                    ui.label('WORKING').classes('bg-blue-600 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                    ui.space()

                    def cancel_order():
                        _reset_unified_card_defaults(prefix)
                        ui.notify(f"{side} Order Removed", type='info')

                    def open_modify():
                        sync_draft()  # always start the edit from the current committed values
                        exp.value = True

                    # click.stop so these don't also trigger the header's own expand/collapse
                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', cancel_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2'):
                    ui.radio(UI_OPTS['order_types'], value=draft['order_type']).bind_value(draft, 'order_type').props('inline dense')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Trigger Price').bind_value(draft, 'trigger_price').props('outlined dense bg-color=white').classes('grow')
                    _num_stepper(draft, 'strike_offset', step=1, label='Strike (0=ATM,1=ITM,-1=OTM)')
                with ui.row().classes('w-full gap-2'):
                    _num_stepper(draft, 'qty', step=1, label='Qty (Lots)')
                    ui.select(UI_OPTS['fire_on_opts'], value=draft['fire_on']).bind_value(draft, 'fire_on').props('outlined dense bg-color=white').classes('grow')
                with ui.row().classes('w-full gap-2'):
                    ui.input('Stop (optional)').bind_value(draft, 'new_stop').props('outlined dense bg-color=white').classes('grow')
                    ui.input('Target (optional)').bind_value(draft, 'new_target').props('outlined dense bg-color=white').classes('grow')

                def confirm_changes():
                    _cancel_engine_job_if_any('entry', side)
                    params[f'{prefix}_order_type'] = draft['order_type']
                    params[f'{prefix}_trigger_price'] = draft['trigger_price']
                    params[f'{prefix}_strike_offset'] = draft['strike_offset']
                    params[f'{prefix}_qty'] = draft['qty']
                    params[f'{prefix}_fire_on'] = draft['fire_on']
                    params[f'{prefix}_new_stop'] = draft['new_stop']
                    params[f'{prefix}_new_target'] = draft['new_target']
                    # Re-stamp arm time: a modified order's timing window should restart from
                    # now, matching fresh-arm behavior (see unified_entry_card.fire_or_arm).
                    params[f'{prefix}_armed_at'] = datetime.now()
                    ui.notify(f"{side} Order Updated", type='positive')
                    _log_alert_action(f"⚙️ {side} Order Updated: {draft['order_type']} @ {draft['trigger_price']} ({draft['fire_on']})")
                    exp.value = False

                def discard_changes():
                    exp.value = False  # just close; draft is resynced next time MODIFY is clicked

                with ui.row().classes('w-full gap-2'):
                    ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                    ui.button('Cancel', on_click=discard_changes).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300')


def _exit_order_row(side, order_label, value_key, active_key, time_key=None, is_target=False, engine_kind=None):
    """One expandable row for an active conditional EXIT order tied to an open position --
    from the Open Positions Idx Stop/Target quick controls, or the Premium/Index-based exit
    cards. Visible only while active. Columns match the SAME layout as the entry-order rows
    above (Exch/Symbol/Side/Type/Trigger Price/Fire On/Stop/Target/Qty/Status). Side reads
    the OPEN TRADE's own recorded direction when available (SELL trade -> exit order is BUY
    to cover; BUY trade -> exit order is SELL to close), falling back to the live buy_mode
    flag only if no trade is present. Symbol and Qty are read from the live open position
    itself (not guessed), so they always match the real trade. MODIFY edits the value/period
    inline; REMOVE deactivates and clears the value, mirroring the Reset behavior already in
    premium_exit_card/index_exit_component.

    'engine_kind' identifies which stop_via_candle_engine job kind this row corresponds to
    ('index_stop' or 'premium_stop'), so REMOVE/CONFIRM can cancel any still-pending job for
    this side first -- pass None (the default) for Target rows, which are never deferred and
    have no matching job kind to cancel."""
    opt_type = 'CE' if side == 'Call' else 'PE'
    draft = {}

    def sync_draft():
        draft['value'] = params[value_key]
        if time_key: draft['time'] = params[time_key]

    sync_draft()

    def _symbol(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade: return f"{params['trading_index']} {int(trade['main']['strike'])} {opt_type}"
        return f"{params['trading_index']} {opt_type}"

    def _qty(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade: return str(trade['qty'])
        prefix = 'call' if side == 'Call' else 'put'
        return str(params.get(f'{prefix}_qty', '-'))

    def _exit_txn_label(_v=None):
        trade = shared_state['active_trades'].get(side)
        if trade is not None:
            return 'SELL' if trade.get('direction', 'SELL') == 'BUY' else 'BUY'
        return 'SELL' if params.get('options_buy_mode', False) else 'BUY'

    def _fire_on_label(v):
        return 'Live' if v == 'Current' else v  # cosmetic only: matches entry-card wording

    with ui.column().classes('w-full') as wrapper:
        wrapper.bind_visibility_from(params, active_key)
        with ui.expansion('', icon='tune').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
            with exp.add_slot('header'):
                with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                    ui.label().bind_text_from(params, 'trading_index', backward=lambda v: INDICES.get(v, {}).get('segment', v)).classes('w-16 text-gray-400 font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_symbol).classes('w-28 font-bold text-gray-800 font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_exit_txn_label).classes('w-14 text-green-600 font-bold')
                    ui.label(order_label).classes('w-24 text-purple-700 font-semibold')
                    ui.label('-').classes('w-24 text-right font-mono text-gray-400')  # Trigger Price: n/a for exit orders
                    if time_key:
                        ui.label().bind_text_from(params, time_key, backward=_fire_on_label).classes('w-16 text-gray-500')
                    else:
                        ui.label('Live').classes('w-16 text-gray-400')
                    ui.label().bind_text_from(params, value_key, backward=lambda v: (str(v) if (not is_target and str(v).strip() not in ('', '0')) else '-')).classes('w-20 text-orange-600 text-right font-mono')
                    ui.label().bind_text_from(params, value_key, backward=lambda v: (str(v) if (is_target and str(v).strip() not in ('', '0')) else '-')).classes('w-20 text-blue-600 text-right font-mono')
                    ui.label().bind_text_from(params, active_key, backward=_qty).classes('w-14 text-right font-mono text-gray-800')
                    ui.label('WORKING').classes('bg-blue-600 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                    ui.space()

                    def remove_order():
                        if engine_kind:
                            _cancel_engine_job_if_any(engine_kind, side)
                        params[active_key] = False
                        params[value_key] = 0
                        ui.notify(f"{side} {order_label} Removed", type='info')

                    def open_modify():
                        sync_draft()
                        exp.value = True

                    ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                    ui.button('REMOVE').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', remove_order)

            with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
                with ui.row().classes('w-full gap-2 items-center'):
                    ui.input('Value').bind_value(draft, 'value').props('outlined dense bg-color=white').classes('grow')
                    if time_key:
                        ui.radio(UI_OPTS['index_times'], value=draft['time']).bind_value(draft, 'time').props('inline dense')

                def confirm_changes():
                    if engine_kind:
                        _cancel_engine_job_if_any(engine_kind, side)
                    params[value_key] = draft['value']
                    if time_key: params[time_key] = draft['time']
                    ui.notify(f"{side} {order_label} Updated", type='positive')
                    _log_alert_action(f"⚙️ {side} {order_label} Updated: {draft['value']}")
                    exp.value = False

                with ui.row().classes('w-full gap-2'):
                    ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                    ui.button('Cancel', on_click=lambda: setattr(exp, 'value', False)).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800')


def render_orderbook():
    """Full-width Open Orders table (white background, matching the rest of the app): pending
    unified entry triggers (Limit/Stop-Market; Market fires immediately so never appears here)
    plus every active conditional exit order (Premium-based and Index-based stop/target,
    including the Open Positions quick Idx Stop/Target controls, since they share the same
    underlying params)."""
    with ui.card().classes('w-full bg-white p-3 gap-2 rounded-xl shadow-sm mb-4 border border-gray-200'):
        ui.label('OPEN ORDERS').classes('font-bold text-xs uppercase tracking-widest text-gray-500 mb-1')
        with ui.row().classes('w-full items-center gap-3 text-[10px] text-gray-400 uppercase px-2'):
            ui.label('Exch').classes('w-16'); ui.label('Symbol').classes('w-28'); ui.label('Side').classes('w-14')
            ui.label('Type').classes('w-24'); ui.label('Trigger Price').classes('w-24 text-right'); ui.label('Fire On').classes('w-16')
            ui.label('Stop').classes('w-20 text-right'); ui.label('Target').classes('w-20 text-right'); ui.label('Qty').classes('w-14 text-right'); ui.label('Status').classes('')

        _orderbook_table_row('Call', 'call')
        _orderbook_table_row('Put', 'put')

        # Exit orders: Premium-based, Index-based (this also covers the Open Positions'
        # quick Idx Stop/Target controls, since those write to the same call_index_*/
        # put_index_* params). engine_kind is passed for Stop rows only -- Target is never
        # deferred, so it has no matching stop_via_candle_engine job to cancel.
        _exit_order_row('Call', 'Prem Stop', 'call_prem_stop_val', 'call_prem_stop_active', 'call_prem_stop_time', is_target=False, engine_kind='premium_stop')
        _exit_order_row('Call', 'Prem Target', 'call_prem_target_val', 'call_prem_tgt_active', 'call_prem_target_time', is_target=True)
        _exit_order_row('Call', 'Idx Stop', 'call_index_stop_val', 'call_index_stop_active', 'call_index_stop_time', is_target=False, engine_kind='index_stop')
        _exit_order_row('Call', 'Idx Target', 'call_index_target_val', 'call_index_tgt_active', 'call_index_target_time', is_target=True)
        _exit_order_row('Put', 'Prem Stop', 'put_prem_stop_val', 'put_prem_stop_active', 'put_prem_stop_time', is_target=False, engine_kind='premium_stop')
        _exit_order_row('Put', 'Prem Target', 'put_prem_target_val', 'put_prem_tgt_active', 'put_prem_target_time', is_target=True)
        _exit_order_row('Put', 'Idx Stop', 'put_index_stop_val', 'put_index_stop_active', 'put_index_stop_time', is_target=False, engine_kind='index_stop')
        _exit_order_row('Put', 'Idx Target', 'put_index_target_val', 'put_index_tgt_active', 'put_index_target_time', is_target=True)

        empty_lbl = ui.label('No pending orders.').classes('w-full text-center text-xs text-gray-400 italic')

        def _nothing_active(_v=None):
            return not (
                params.get('call_armed') or params.get('put_armed') or
                params.get('call_prem_stop_active') or params.get('call_prem_tgt_active') or
                params.get('call_index_stop_active') or params.get('call_index_tgt_active') or
                params.get('put_prem_stop_active') or params.get('put_prem_tgt_active') or
                params.get('put_index_stop_active') or params.get('put_index_tgt_active')
            )
        empty_lbl.bind_visibility_from(params, 'call_armed', backward=_nothing_active)
        ui_refs['orderbook_empty'] = empty_lbl

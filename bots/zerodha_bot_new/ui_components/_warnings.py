"""
Order-sanity warning system: shared by the unified entry cards, Premium Exit cards, and
Index Exit cards. None of these block an action -- they raise a confirmation dialog
('Proceed Anyway' / 'Cancel') so a legitimate edge case is never locked out, while an
obvious mistake (a trigger that fires the instant it's armed, a stop/target value wildly
far from the current price) gets a chance to be caught first.
"""
from nicegui import ui
from config import shared_state

def _confirm_warning(message, on_proceed):
    """Shows a warning dialog with the given message (one or more lines); calls on_proceed()
    ONLY if the person clicks 'Proceed Anyway'. Clicking 'Cancel' (or closing the dialog)
    leaves everything untouched -- nothing is armed/set until confirmed."""
    with ui.dialog() as dialog, ui.card().classes('p-4 gap-3 max-w-md'):
        ui.label('⚠️ Check Your Order').classes('font-bold text-orange-700 text-sm')
        for line in message.split('\n\n'):
            ui.label(line).classes('text-xs text-gray-700')
        with ui.row().classes('w-full gap-2 justify-end mt-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat').classes('text-gray-600')
            def proceed():
                dialog.close()
                on_proceed()
            ui.button('Proceed Anyway', color='orange', on_click=proceed)
    dialog.open()


def _unified_instant_fire_warning(side, order_type, buy_mode, idx_ltp, trigger_price):
    """For the unified Open Short/Long entry card. Returns a warning message if arming this
    Limit/Stop-Market trigger RIGHT NOW would fire immediately as a market order on the very
    next tick -- i.e. the exact condition LogicEngine._check_unified_open checks is already
    true. Mirrors that function's direction rules exactly (Sell Mode vs Buy Mode, Call vs
    Put) so this warning is never wrong about what will actually happen. In that case the
    person most likely meant the OTHER order type, since Limit and Stop-Market are
    opposite-direction triggers for the same side/mode."""
    if trigger_price <= 0 or idx_ltp <= 0: return None
    fires_now = False
    if not buy_mode:
        if order_type == 'Stop-Market':
            fires_now = (idx_ltp <= trigger_price) if side == 'Call' else (idx_ltp >= trigger_price)
        elif order_type == 'Limit':
            fires_now = (idx_ltp >= trigger_price) if side == 'Call' else (idx_ltp <= trigger_price)
    else:
        if order_type == 'Stop-Market':
            fires_now = (idx_ltp >= trigger_price) if side == 'Call' else (idx_ltp <= trigger_price)
        elif order_type == 'Limit':
            fires_now = (idx_ltp <= trigger_price) if side == 'Call' else (idx_ltp >= trigger_price)
    if not fires_now: return None
    other = 'Stop-Market' if order_type == 'Limit' else 'Limit'
    return (f"This {order_type} trigger ({trigger_price}) will fire IMMEDIATELY as a market order, "
            f"since the index is already at {idx_ltp:.2f}. Did you mean to place a {other} order instead?")


def _premium_instant_fire_warning(is_stop, is_buy_trade, value, current):
    """For Premium Exit Stop/Target. Returns a warning if this value has ALREADY been crossed
    by the live option premium, so it would close the position the instant it's set. Premium
    exit direction depends only on the trade's own direction (BUY vs SELL) -- NOT on Call vs
    Put -- exactly mirroring LogicEngine._check_exits's PREMIUM EXITS branch."""
    if value <= 0 or current <= 0: return None
    if is_stop:
        crossed = (current <= value) if is_buy_trade else (current >= value)
    else:
        crossed = (current >= value) if is_buy_trade else (current <= value)
    if not crossed: return None
    kind = 'Stop' if is_stop else 'Target'
    return f"{kind} value ({value}) has already been reached by the current premium ({current:.2f}) -- this will fire IMMEDIATELY."


def _index_instant_fire_warning(is_stop, side, buy_mode, value, current):
    """For Index Exit Stop/Target. Returns a warning if this value has ALREADY been crossed by
    the live index price, so it would close the position the instant it's set. Index exit
    direction depends on BOTH side (Call/Put) and the global options_buy_mode toggle, exactly
    mirroring LogicEngine._check_exits's INDEX EXITS branch."""
    if value <= 0 or current <= 0: return None
    if not buy_mode:
        if side == 'Call': crossed = (current >= value) if is_stop else (current <= value)
        else: crossed = (current <= value) if is_stop else (current >= value)
    else:
        if side == 'Call': crossed = (current <= value) if is_stop else (current >= value)
        else: crossed = (current >= value) if is_stop else (current <= value)
    if not crossed: return None
    kind = 'Stop' if is_stop else 'Target'
    return f"{kind} value ({value}) has already been reached by the current index price ({current:.2f}) -- this will fire IMMEDIATELY."


def _pct_away_warning(value, current, kind_label):
    """Generic 'sanity check' warning shared by Premium and Index Exit: flags a Stop/Target
    value that's more than 10% away from the current price, regardless of direction -- a
    likely typo (e.g. an extra/missing digit) rather than an intentional wide stop."""
    if value <= 0 or current <= 0: return None
    pct = abs(value - current) / current
    if pct <= 0.10: return None
    return f"{kind_label} value ({value}) is {pct*100:.0f}% away from the current price ({current:.2f})."


def _position_type_label(side, buy_mode):
    """Short natural-language label for a side's position, matching the wording already used
    in unified_entry_card's title (_unified_card_title) so warnings read consistently with
    the panel titles already on screen: Sell Mode -> 'Short'/'Long' (Call=Short, Put=Long);
    Buy Mode -> 'Call (Bullish)'/'Put (Bearish)', since Buy Mode doesn't have a short/long
    distinction (both sides are bought options) but Call/Put + bias is still the meaningful
    distinction there."""
    if not buy_mode:
        return 'Short' if side == 'Call' else 'Long'
    return 'Call (Bullish)' if side == 'Call' else 'Put (Bearish)'


def _no_position_warning(side, buy_mode):
    """Warns when Profit/Loss/Stop/Target is being set for a side that has NO open position
    right now. Setting one anyway doesn't error (logic_engine's exit checks simply skip a
    side entirely while shared_state['active_trades'][side] is None) but it sits there inert
    and could unexpectedly apply to a DIFFERENT future position opened later on this same
    side -- the likely real mistake is editing the wrong panel while looking at the OTHER
    side's currently-open position. Only names the other side in the message when it's
    actually the one that's open right now, so the hint is never a guess."""
    if shared_state['active_trades'].get(side) is not None:
        return None
    this_label = _position_type_label(side, buy_mode)
    other_side = 'Put' if side == 'Call' else 'Call'
    if shared_state['active_trades'].get(other_side) is not None:
        other_label = _position_type_label(other_side, buy_mode)
        return f"No {this_label} position is open. Are you trying to set this for the {other_label} position instead?"
    return f"No {this_label} position is open right now."

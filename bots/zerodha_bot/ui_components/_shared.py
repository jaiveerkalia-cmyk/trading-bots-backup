"""
Shared, low-level helpers used across every ui_components submodule: the per-side
color/mode conventions, the draft-and-commit sync helper, the numeric stepper widget, and
the Trade Event Log writer. Nothing here renders a top-level card on its own.
"""
from nicegui import ui
from config import params, shared_state
from datetime import datetime

def _num_stepper(target, key, step=1, label=''):
    """A small -/+ stepper wrapping a text input bound to target[key]. 'target' is any
    dict-like object (params, or a local per-row 'pending' dict for staged edits), so this
    works both for live-bound fields and for the Order Book's confirm-before-apply panel."""
    def _current():
        try: return float(target[key])
        except (ValueError, TypeError): return 0.0
    def _apply(v):
        target[key] = int(v) if float(v).is_integer() else round(v, 4)
    def dec(): _apply(_current() - step)
    def inc(): _apply(_current() + step)
    with ui.row().classes('grow items-center gap-1 no-wrap'):
        ui.button(icon='remove', on_click=dec).props('flat dense round size=sm').classes('text-gray-600 bg-gray-100')
        ui.input(label).bind_value(target, key).props('outlined dense bg-color=white').classes('grow text-center')
        ui.button(icon='add', on_click=inc).props('flat dense round size=sm').classes('text-gray-600 bg-gray-100')


def _sync_draft_from_params(draft, draft_key, params_key, interval=1.0):
    """Keeps a staged 'draft' input value (see the draft-and-commit pattern used throughout
    this file, e.g. auto_close_card/index_exit_component/unified_entry_card) in sync with
    EXTERNAL changes to params[params_key] -- most importantly
    auto_run.AutoController.clear_leg_fields() resetting it back to 0/"" after a position is
    closed (manually, or auto-closed by the engine), but also any Reset click from a
    DIFFERENT UI instance that happens to share the same params key (e.g. the Open Positions
    quick Idx Stop/Target controls and the separate 'Exit based on Index' card both write
    call_index_stop_val).

    This does NOT reintroduce the mid-typing bug the draft pattern was built to fix: typing
    only ever writes to 'draft', never to params, so params[params_key] only ever changes
    from an external source or from this same card's own commit handler (SET/Arm/switch-on)
    -- and a commit handler already leaves draft holding the exact value it just committed,
    so mirroring params back into draft at that moment is a harmless no-op. Only a genuine
    EXTERNAL change (detected by comparing against the last-seen value) ever moves the
    draft, and it's always safe to reflect immediately since it never originates from an
    in-progress keystroke."""
    last = {'value': params.get(params_key, 0)}
    def _sync():
        current = params.get(params_key, 0)
        if current != last['value']:
            last['value'] = current
            draft[draft_key] = current
    ui.timer(interval, _sync)


def _side_is_red(side, buy_mode):
    """Single source of truth for Call/Put color direction across EVERY card in the app.
    Sell Mode (unchanged): Call=red (short-side), Put=green (long-side).
    Buy Mode (flipped, per request): Buy Call=green (bullish), Buy Put=red (bearish) --
    i.e. colors always track the plain-English market direction of the position, not the
    fixed Call/Put identity. All per-side cards (unified entry, auto close, premium exit,
    index exit, open positions) call this so they stay visually consistent with each other
    in both modes."""
    if not buy_mode:
        return side == 'Call'
    return side == 'Put'


def _trade_is_red(side, buy_mode, trade):
    """Same red/green direction logic as _side_is_red, but for an OPEN TRADE specifically:
    reads the trade's own recorded 'direction' (BUY/SELL) when a trade exists, so a
    position's color never flips just because the global options_buy_mode toggle changes
    later (e.g. after the trade closes and the mode is switched back) -- consistent with how
    _position_side_badge already handles the SHORT/BUY text label. Falls back to the live
    buy_mode flag only when no trade is present yet."""
    if trade is not None:
        direction = trade.get('direction', 'SELL')
        is_buy_trade = (direction == 'BUY')
    else:
        is_buy_trade = buy_mode
    # A BUY trade profits like a "Call-in-Buy-Mode" (bullish/green); a SELL trade behaves
    # like a "Call-in-Sell-Mode" (red) for Call, or the Put equivalent. Reuse _side_is_red's
    # exact mapping so this can never drift out of sync with every other card in the app.
    effective_buy_mode = is_buy_trade
    return _side_is_red(side, effective_buy_mode)


def _side_colors(side, buy_mode, weight='100'):
    """Returns (bg/border class string, plain color name) for a given side+mode+shade.
    weight matches the existing per-card shade conventions already in this file (some cards
    use bg-*-50/border-*-200, others bg-*-100/border-*-300) so visuals don't change except
    for the actual hue swap in Buy Mode."""
    is_red = _side_is_red(side, buy_mode)
    if weight == '50':
        return (('bg-red-50 border-red-200', 'red') if is_red else ('bg-green-50 border-green-200', 'green'))
    return (('bg-red-100 border-red-300', 'red') if is_red else ('bg-green-100 border-green-300', 'green'))


def _bind_card_colors(card, side, cls_template):
    """Re-applies a card's background/border classes every time options_buy_mode changes, so
    the color scheme flips live rather than only at initial render. NiceGUI/Quasar elements
    don't expose a generic reactive 'bind classes' helper, so this drives the re-application
    via a hidden zero-width label whose bind_text_from callback is used purely for its side
    effect (calling card.classes(replace=...)) -- the label's own text is always empty and
    it renders with 'hidden' so it's invisible. cls_template(is_red) -> full class string."""
    def _apply(buy_mode, c=card, s=side):
        is_red = _side_is_red(s, buy_mode)
        c.classes(replace=cls_template(is_red))
        return ''
    hook = ui.label('').classes('hidden')
    hook.bind_text_from(params, 'options_buy_mode', backward=_apply)


def _bind_btn_color(button, side, red_color='red', green_color='green'):
    """Same hidden-hook pattern as _bind_card_colors, but for a button's color prop (NiceGUI
    buttons don't expose a reactive 'color' bind either)."""
    def _apply(buy_mode, b=button, s=side):
        is_red = _side_is_red(s, buy_mode)
        b.props(f'color={red_color if is_red else green_color}')
        return ''
    hook = ui.label('').classes('hidden')
    hook.bind_text_from(params, 'options_buy_mode', backward=_apply)


def _log_alert_action(message):
    """Writes an entry into the shared Trade Event Log (shared_state['activity_log']) -- the
    exact same store LogicEngine.log_action() writes to in logic_engine.py, using the same
    '[HH:MM:SS] message' format and 100-entry cap -- so alert add/modify/cancel, and every
    Set/Reset/Activate action across every card in this file, show up alongside trade opens/
    closes/fires in one unified log. Defined here rather than calling into LogicEngine since
    these UI handlers have no LogicEngine instance available; writing directly to
    shared_state keeps a single source of truth for the log's storage."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    shared_state['activity_log'].insert(0, f"[{timestamp}] {message}")
    shared_state['activity_log'] = shared_state['activity_log'][:100]

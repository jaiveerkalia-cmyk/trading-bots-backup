"""
Open Positions section (kept alongside the banner CALL/PUT POSITION cards): one row per
side with live mark/size/PnL, and quick Idx Stop/Target controls. _set_position_row_style
is the single source of truth for a row's accent color and visibility -- every caller
(here, and every tick from auto_run.py's update_ui()) must go through it.
"""
from nicegui import ui
from config import params, ui_refs, shared_state

from ._shared import _trade_is_red, _sync_draft_from_params, _log_alert_action

# --- OPEN POSITIONS (kept alongside the existing banner CALL/PUT POSITION cards) ---

def _position_side_badge(side, buy_mode, trade):
    """SHORT/LONG (Sell Mode) or BUY (Buy Mode) badge text+color for the Open Positions row.
    Reads the trade's OWN recorded direction when a trade exists (so history/labels never
    flip just because the global toggle changes later); falls back to the live buy_mode flag
    only when no trade is present yet (e.g. right when a position is being opened, before the
    trade dict is fully populated in shared_state).

    Sell Mode (direction != 'BUY'): Call = SHORT (sell CE), Put = LONG (sell PE) -- these are
    genuinely different positions with different bias, so they must not share one label.
    Buy Mode (direction == 'BUY'): both Call and Put show BUY, since both are long options,
    just on opposite underlying bias (bullish CE vs bearish PE) -- color (not the text label)
    is what distinguishes them there, via _position_row_accent/_trade_is_red."""
    direction = None
    if trade is not None:
        direction = trade.get('direction')
    if direction is None:
        direction = 'BUY' if buy_mode else 'SELL'
    if direction == 'BUY':
        return 'BUY', 'bg-blue-100 text-blue-700'
    return ('SHORT', 'bg-red-100 text-red-700') if side == 'Call' else ('LONG', 'bg-green-100 text-green-700')


def _position_row_accent(side, buy_mode, trade):
    """Left accent border class for an Open Positions row. Mode-aware via _trade_is_red, so
    it matches every other card's color convention: Sell Mode unchanged (Call=red border,
    Put=green border); Buy Mode flips (Buy Call=green border since it's a bullish long, Buy
    Put=red border since it's a bearish long)."""
    is_red = _trade_is_red(side, buy_mode, trade)
    return 'border-red-500' if is_red else 'border-green-500'


def _set_position_row_style(row, side, buy_mode, trade):
    """SINGLE source of truth for an Open Positions row's accent color AND visibility.

    These two concerns are DELIBERATELY handled through two completely independent
    mechanisms so that updating one can never accidentally clobber the other:
      - Color: applied via row.style(...) (a MERGE into the element's inline style dict),
        never via row.classes(replace=...). classes(replace=...) overwrites the ENTIRE class
        list, which is what silently wiped out visibility state in earlier attempts at this
        fix. style() only touches the one CSS property named, leaving everything else --
        including any visibility-related class or style -- untouched.
      - Visibility: applied via a direct 'display' CSS style (the most fundamental, literal
        way to hide an element in a browser -- it cannot be undone by any classes() call
        elsewhere, since classes() and style() are separate attributes on the element). This
        does NOT rely on NiceGUI's set_visibility()/bind_visibility_from() abstractions at
        all, which is intentional: those are the mechanisms earlier fix attempts already went
        through, and the row was still staying visible, meaning something about how they
        interact with the Tailwind 'hidden' class and repeated classes(replace=...) calls in
        this codebase was not reliably taking effect. A raw 'display: none' style is the
        floor -- there's no lower-level way to hide a DOM element that a framework could
        still override out from under us.

    Every caller (build time in _position_row, and every tick in auto_run.py's update_ui())
    MUST go through this one function -- never call row.classes(replace=...) or
    row.set_visibility(...) on a position row directly."""
    is_red = _trade_is_red(side, buy_mode, trade)
    color = '#ef4444' if is_red else '#22c55e'  # Tailwind red-500 / green-500
    row.style(f'border-left-color: {color} !important')
    if trade is not None:
        row.style('display: block !important')
        row.set_visibility(True)
    else:
        row.style('display: none !important')
        row.set_visibility(False)


def _position_row(side, on_close=None):
    """One row of the Open Positions section. Only visible while that side has an active
    trade. Values (mark/size/pnl/entry/qty/symbol) are populated live each tick by
    auto_run.py's update_ui(), the same pattern already used for the banner cards.

    Classes AND visibility for the outer row are always set together via
    _set_position_row_style (see its docstring for why) -- both at build time here and every
    tick from auto_run.py's update_ui(), so they can never drift out of sync.

    Stop/Target here control the INDEX-PRICE-based exit (call_index_stop_val/
    call_index_stop_active etc, the same params the 'Exit based on Index' cards use).

    IMPORTANT (fixes layered here):
    1. The switches ONLY toggle *_index_stop_active/*_index_tgt_active. They no longer touch
       *_index_stop_time/*_index_target_time (Current/1m/5m) -- a previous version force-reset
       the period to 'Current' on every toggle, which silently discarded a 1m/5m selection
       made in the separate 'Exit based on Index' card. A small '(Current)'/'(1m)'/'(5m)'
       label next to each switch shows the currently active period at a glance.
    2. The value inputs are bound to a local staged 'draft' dict, NOT directly to params --
       same reasoning as auto_close_card/index_exit_component: typing has zero live effect.
       Turning a switch ON commits the CURRENT (complete) draft value into params atomically
       at that moment (reading a finished value on a single click, not a live keystroke
       stream), so there's no window where an in-progress edit could be read by
       logic_engine._check_exits and close the position early.
    3. If the draft value is invalid (<=0) when trying to turn a switch on, the switch must
       revert to off. This reverts BOTH params[*_active_key] AND the switch element's own
       bound value via .set_value(False): NiceGUI syncs a bound switch's own .value back into
       params on a periodic cycle, independent of this handler. Reverting only the params
       side left a stale True sitting in the switch's own value, which that periodic sync
       then re-pushed into params moments later -- silently overriding the revert and
       activating the stop/target despite the warning notification. .set_value() corrects the
       switch's own value (and the client display) through NiceGUI's normal update path, so
       there's nothing stale left for a later sync to re-push. Not mode-specific -- affects
       Call/Put and Sell/Buy Mode identically.
    4. Before actually rejecting an invalid value, a SINGLE short retry (0.3s later, via a
       one-shot ui.timer) re-checks the draft once more before giving up. This exists because
       clicking the switch immediately after typing a value can occasionally race the input's
       own client->server update against the switch's click event -- the draft briefly still
       held its old (often empty/zero) value at the exact moment the switch handler first
       ran, so a genuinely-valid entry was rejected on the first click and only succeeded on
       a second manual retry. The 0.3s grace window resolves that automatically and silently
       (no error shown) in the common case where it was just a timing race; only a value
       that's STILL invalid after the retry produces the warning and reverts the switch.
    5. _sync_draft_from_params keeps both value inputs in sync with EXTERNAL resets (e.g.
       Close -> auto_run.clear_leg_fields(), or the separate 'Exit based on Index' card
       sharing this same params key) without reintroducing the mid-typing bug.
    6. CRITICAL (fixes a real incident): the switch's on_change handler previously committed
       whatever was in the draft into params EVERY time it fired with e.value == True --
       including a spurious/duplicate re-fire while the switch was ALREADY on (e.g. NiceGUI's
       periodic re-sync of a bound switch's value nudging on_change again, or any other
       redundant re-trigger). If, in that brief window, the Enter via Stop engine
       (stop_via_candle_engine.py) had ITSELF just written a fresh, correct trigger value
       into this exact same params key (its hand-off flow, by design, also sets *_index_stop_
       active=True), this handler would silently overwrite that freshly-computed value with
       whatever STALE value happened to be sitting in the local draft dict from before -- the
       position would then close against the wrong, stale level instead of the engine's
       correct one. Fixed with an edge-detection guard: a NEW value is committed into params
       ONLY on a genuine OFF-to-ON transition (params[*_active_key] was False and is now being
       turned True) using a locally-tracked 'was_active' flag captured at handler-build time
       and kept in sync with every externally-observed activation (including the engine's own
       hand-off, via the same _sync_draft_from_params-style external-change detection). If the
       switch fires while the field is ALREADY active, the handler treats it as a no-op re-fire
       and does nothing, leaving whatever value is currently in params (manually set, or just
       written by the engine) completely untouched. Every value actually committed by a
       genuine user action is still explicitly logged into the shared Trade Event Log (via
       _log_alert_action), same as every other Set/Activate action in the app.

    No _no_position_warning check here (unlike auto_close_card/index_exit_component/
    premium_exit_card): this whole row only exists/renders while a trade IS open on this
    side (_set_position_row_style hides it entirely otherwise), so the situation that warning
    guards against can't happen from this particular UI surface. This row also has no
    instant-fire/pct-away index-price warnings at all (unlike index_exit_component), so no
    Futures Mode change is needed here."""
    prefix = 'call' if side == 'Call' else 'put'
    stop_val_key = f'{prefix}_index_stop_val'; stop_active_key = f'{prefix}_index_stop_active'; stop_time_key = f'{prefix}_index_stop_time'
    tgt_val_key = f'{prefix}_index_target_val'; tgt_active_key = f'{prefix}_index_tgt_active'; tgt_time_key = f'{prefix}_index_target_time'

    stop_draft = {'value': params.get(stop_val_key, 0)}
    tgt_draft = {'value': params.get(tgt_val_key, 0)}
    _sync_draft_from_params(stop_draft, 'value', stop_val_key)
    _sync_draft_from_params(tgt_draft, 'value', tgt_val_key)

    # Edge-detection state for the off->on guard described in point 6 above. Captures
    # whatever the active flag's value is RIGHT NOW at build time; _toggle_stop/_toggle_tgt
    # below update it on every call so it always reflects the most recently OBSERVED state,
    # including changes made externally (e.g. the engine's hand-off, or a Reset from the
    # separate 'Exit based on Index' card sharing this same params key).
    was_active = {'stop': params.get(stop_active_key, False), 'tgt': params.get(tgt_active_key, False)}

    # stop_switch/tgt_switch are assigned further below, but referenced here inside these
    # handlers -- safe, since Python closures resolve free variables at CALL time (when the
    # user actually clicks), by which point both switches already exist.
    def _toggle_stop(e):
        currently_active = params.get(stop_active_key, False)
        if not e.value:
            was_active['stop'] = False
            return  # turning off never needs a value check
        if was_active['stop'] or currently_active:
            # Re-fire while already active (spurious re-sync, or the engine's own hand-off
            # just set this) -- NOT a genuine off->on click. Do nothing; whatever value is
            # currently in params (possibly just written by the engine) is left untouched.
            was_active['stop'] = True
            return
        def _finalize(retried=False):
            try:
                value = float(stop_draft['value'])
            except (ValueError, TypeError):
                value = 0
            if value <= 0:
                if not retried:
                    # Give the input's own client->server update a brief window to land, in
                    # case it hasn't propagated into stop_draft yet -- see docstring point 4.
                    ui.timer(0.3, lambda: _finalize(True), once=True)
                    return
                ui.notify(f"Enter a valid {side} Idx Stop value first", type='negative')
                params[stop_active_key] = False
                was_active['stop'] = False
                stop_switch.set_value(False)
                return
            params[stop_val_key] = value
            was_active['stop'] = True
            _log_alert_action(f"⚙️ {side} Idx Stop SET (quick control): {value}")
        _finalize()

    def _toggle_tgt(e):
        currently_active = params.get(tgt_active_key, False)
        if not e.value:
            was_active['tgt'] = False
            return  # turning off never needs a value check
        if was_active['tgt'] or currently_active:
            was_active['tgt'] = True
            return
        def _finalize(retried=False):
            try:
                value = float(tgt_draft['value'])
            except (ValueError, TypeError):
                value = 0
            if value <= 0:
                if not retried:
                    ui.timer(0.3, lambda: _finalize(True), once=True)
                    return
                ui.notify(f"Enter a valid {side} Idx Target value first", type='negative')
                params[tgt_active_key] = False
                was_active['tgt'] = False
                tgt_switch.set_value(False)
                return
            params[tgt_val_key] = value
            was_active['tgt'] = True
            _log_alert_action(f"⚙️ {side} Idx Target SET (quick control): {value}")
        _finalize()

    # Keeps was_active in sync with EXTERNAL changes to the active flags (e.g. the engine's
    # hand-off setting *_index_stop_active=True directly, or Reset from elsewhere clearing
    # it) -- same polling pattern as _sync_draft_from_params, so a later genuine user click
    # is correctly recognized as off->on rather than being mistaken for a re-fire, and an
    # externally-driven activation is never misread as one this row is responsible for.
    def _sync_was_active():
        was_active['stop'] = params.get(stop_active_key, False)
        was_active['tgt'] = params.get(tgt_active_key, False)
    ui.timer(1.0, _sync_was_active)

    with ui.card().classes('w-full bg-white border-l-4 border-gray-300 border border-gray-200 rounded-lg p-3 gap-2 shadow-sm') as row:
        ui_refs[f'{prefix}_pos_row'] = row
        _set_position_row_style(row, side, params.get('options_buy_mode', False), shared_state['active_trades'].get(side))

        # Re-applies style+visibility together whenever options_buy_mode changes (mode toggled
        # while this row happens to be visible or hidden).
        def _apply_row_style_on_mode(buy_mode, r=row, s=side):
            _set_position_row_style(r, s, buy_mode, shared_state['active_trades'].get(s))
            return ''
        _mode_hook = ui.label('').classes('hidden')
        _mode_hook.bind_text_from(params, 'options_buy_mode', backward=_apply_row_style_on_mode)

        with ui.row().classes('w-full justify-between items-center flex-wrap gap-2'):
            with ui.row().classes('items-center gap-2'):
                ui_refs[f'{prefix}_pos_symbol'] = ui.label('-').classes('text-gray-800 font-bold text-sm font-mono')
                side_label, side_cls = _position_side_badge(side, params.get('options_buy_mode', False), shared_state['active_trades'].get(side))
                side_lbl = ui.label(side_label).classes(f'{side_cls} text-[10px] font-bold px-2 py-0.5 rounded')
                ui_refs[f'{prefix}_pos_side_label'] = side_lbl
            with ui.row().classes('items-center gap-6'):
                with ui.column().classes('items-end gap-0'):
                    ui.label('MARK').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_mark'] = ui.label('0.0').classes('text-orange-600 font-mono font-bold text-sm')
                with ui.column().classes('items-end gap-0'):
                    ui.label('SIZE').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_size'] = ui.label('0').classes('text-gray-800 font-mono text-sm')
                with ui.column().classes('items-end gap-0'):
                    ui.label('uPnL').classes('text-gray-400 text-[9px] uppercase tracking-wider')
                    ui_refs[f'{prefix}_pos_pnl'] = ui.label('0').classes('font-mono font-bold text-sm text-gray-800')

        with ui.row().classes('w-full gap-6 text-[11px] text-gray-500 flex-wrap'):
            with ui.row().classes('gap-1 items-baseline'):
                ui.label('Entry')
                ui_refs[f'{prefix}_pos_entry'] = ui.label('0.0').classes('text-gray-700 font-mono')
            with ui.row().classes('gap-1 items-baseline'):
                ui.label('Qty')
                ui_refs[f'{prefix}_pos_qty'] = ui.label('0').classes('text-gray-700 font-mono')

        with ui.row().classes('w-full gap-3 items-center pt-2 border-t border-gray-200 flex-wrap'):
            ui.label('Idx Stop').classes('text-[10px] text-gray-500')
            ui.label().bind_text_from(params, stop_time_key, backward=lambda v: f"({v})").classes('text-[9px] text-gray-400 -ml-2')
            stop_switch = ui.switch(on_change=_toggle_stop).bind_value(params, stop_active_key).props('dense color=red size=sm')
            ui.input().bind_value(stop_draft, 'value').props('outlined dense bg-color=white').classes('w-24')
            ui.label('Idx Target').classes('text-[10px] text-gray-500')
            ui.label().bind_text_from(params, tgt_time_key, backward=lambda v: f"({v})").classes('text-[9px] text-gray-400 -ml-2')
            tgt_switch = ui.switch(on_change=_toggle_tgt).bind_value(params, tgt_active_key).props('dense color=green size=sm')
            ui.input().bind_value(tgt_draft, 'value').props('outlined dense bg-color=white').classes('w-24')
            ui.space()
            ui.button('CLOSE', color='red', on_click=on_close).classes('h-7 text-xs px-4 rounded font-bold')


def render_open_positions(on_close_call=None, on_close_put=None):
    """'OPEN POSITIONS' section, kept alongside the existing banner CALL/PUT POSITION cards
    (not a replacement). White background, matching the rest of the app."""
    with ui.card().classes('w-full bg-white p-3 gap-3 rounded-xl shadow-sm mb-4 border border-gray-200'):
        with ui.row().classes('w-full justify-between items-center'):
            ui.label('OPEN POSITIONS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')
            ui_refs['open_positions_count'] = ui.label('0 positions').classes('text-[10px] text-gray-400')
        _position_row('Call', on_close=on_close_call)
        _position_row('Put', on_close=on_close_put)
        empty_lbl = ui.label('No open positions.').classes('w-full text-center text-xs text-gray-400 italic')
        empty_lbl.bind_visibility_from(shared_state['active_trades'], 'Call',
                                        backward=lambda v: v is None and shared_state['active_trades'].get('Put') is None)

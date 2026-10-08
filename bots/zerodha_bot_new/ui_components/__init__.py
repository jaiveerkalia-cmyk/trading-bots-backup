"""
ui_components package -- split from the original single ui_components.py file (1808 lines)
into one submodule per logical section, for cheaper/safer targeted edits. Pure reorganization:
no behavior change. `import ui_components as comp` from auto_run.py works identically to
before; every name auto_run.py reaches via `comp.X` is re-exported here.

Submodules:
  _shared.py           -- color/mode conventions, draft-sync helper, numeric stepper, log writer
  _warnings.py         -- order-sanity confirmation-dialog warnings
  entry_exit_cards.py  -- unified entry, auto close, premium/index exit, global control cards
  alerts.py            -- price-alert add forms, sound profile panel, Active Alerts list
  positions.py         -- Open Positions section
  orderbook.py         -- Open Orders table
  patterns.py          -- Candlestick Pattern Indicators card
  history.py           -- Order History table
  header.py            -- default banner/chart/log renderers (banner is monkey-patched by
                           auto_run.py immediately on import; see render_master_banner below)
"""

# --- Enter via Stop engine hook (wired in from auto_run.py after construction) ---
# Kept as a plain module-level reference (mirrors the comp.render_master_banner override
# pattern already used from auto_run.py) rather than importing stop_via_candle_engine here,
# to avoid a circular import (that module imports config, not ui_components). Stays None if
# never wired -- every call site below checks for that and is a harmless no-op otherwise.
#
# Defined here, before any submodule import below, because entry_exit_cards.py and
# orderbook.py both do `from . import _cancel_engine_job_if_any` -- Python registers this
# package in sys.modules before running the rest of this file, so by the time those
# submodules are imported (further down), this name is already a resolvable attribute on
# the (still-initializing) package module. Reordering these two lines after the submodule
# imports below would break that.
_stop_via_candle_engine = None

def _cancel_engine_job_if_any(kind, side):
    """Cancels any still-pending Enter via Stop job for this (kind, side) -- called from
    every UI action that cancels/resets/modifies the order or stop that job would otherwise
    hand off into. A job only exists for the few seconds between deferral and hand-off (see
    stop_via_candle_engine.py); once handed off, it has already become a normal params-backed
    order and there is nothing left here to cancel. Always safe to call unconditionally."""
    if _stop_via_candle_engine is not None:
        _stop_via_candle_engine.cancel_pending(kind, side)

from ._shared import (
    _num_stepper, _sync_draft_from_params, _side_is_red, _trade_is_red,
    _side_colors, _bind_card_colors, _bind_btn_color, _log_alert_action,
)
from ._warnings import (
    _confirm_warning, _unified_instant_fire_warning, _premium_instant_fire_warning,
    _index_instant_fire_warning, _pct_away_warning, _position_type_label, _no_position_warning,
)
from .entry_exit_cards import (
    entry_card, _reset_unified_card_defaults, _unified_card_title, _unified_card_colors,
    unified_entry_card, auto_close_card, open_logic_card, global_control_card,
    index_exit_component, premium_exit_card,
)
from .alerts import (
    _add_alert_card, alerts_card_upper, alerts_card_lower, _preview_sound,
    render_alert_sound_panel, _alert_row, render_active_alerts,
)
from .positions import (
    _position_side_badge, _position_row_accent, _set_position_row_style,
    _position_row, render_open_positions,
)
from .orderbook import (
    _toggle_expansion, _orderbook_table_row, _exit_order_row, render_orderbook,
)
from .patterns import _pattern_row, render_pattern_indicators
from .history import _refresh_history_table, render_order_history
from .header import render_master_banner, render_chart_row, render_log_row

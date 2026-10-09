"""
Candlestick Pattern Indicators card: one themed sub-card per pattern registered in
pattern_engine.PATTERN_REGISTRY, plus the shared Fetch Delay control (pattern_fetch_delay_sec,
also read by indicator_engine.py).
"""
from nicegui import ui
from config import params, UI_OPTS, shared_state
from pattern_engine import PATTERN_REGISTRY

from ._shared import _num_stepper, _log_alert_action

# --- CANDLESTICK PATTERN INDICATORS (pattern_engine.py) ---

def _pattern_row(key, meta):
    """One card per registered pattern (bullish_engulfing, bearish_engulfing, and any future
    entries added to pattern_engine.PATTERN_REGISTRY -- this loop picks them all up
    automatically, no UI change needed to add a new pattern). On/off switch defaults to
    whatever params currently holds (True by default, per pattern). Interval multi-select
    defaults to ['5m', '15m', '30m', '1h']. 'Engulf candle count' controls how many
    subsequent closed candles are combined into one synthetic candle before checking the
    engulfing condition against the base candle (see pattern_engine.py for the exact
    mechanics); default 1 = standard single-candle engulfing.

    Background color comes from meta['color'] (registered per-pattern in
    pattern_engine.PATTERN_REGISTRY: light green for bullish setups, light red for bearish)
    so a future pattern just needs a 'color' entry there to get a themed card -- no UI change
    needed. 'flex-1' (not 'w-full') so multiple cards sit side by side in the row built by
    render_pattern_indicators, instead of stacking top to bottom.

    Detection now runs independently for BOTH indices (NIFTY and SENSEX) regardless of the
    globally selected trading_index (see pattern_engine.py), so the 'Last' status line below
    shows WHICH index the most recent signal fired on."""
    enabled_key = meta['enabled_param']; intervals_key = meta['intervals_param']; count_key = meta['count_param']
    color = meta.get('color', 'gray')
    with ui.card().classes(f'flex-1 min-w-[300px] p-3 gap-2 bg-{color}-50 border border-{color}-200 rounded-lg'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label(meta['label']).classes('font-bold text-sm text-gray-800')
            ui.switch(value=params.get(enabled_key, True)).bind_value(params, enabled_key).props('dense color=green')

        with ui.row().classes('w-full items-center gap-2'):
            ui.label('Intervals:').classes('text-xs text-gray-500 w-16')
            ui.select(UI_OPTS['pattern_intervals'], value=params.get(intervals_key, ['5m', '15m', '30m', '1h']), multiple=True) \
                .bind_value(params, intervals_key).props('outlined dense bg-color=white use-chips').classes('grow')

        with ui.row().classes('w-full items-center gap-2'):
            ui.label('Engulf candle count:').classes('text-xs text-gray-500')
            _num_stepper(params, count_key, step=1, label='Count')

        status = ui.label('No signal yet.').classes('text-[10px] text-gray-400')

        def _refresh(_e=None, k=key, lbl=status):
            sig = shared_state.get('pattern_last_signal', {}).get(k)
            if sig:
                lbl.set_text(f"Last: {sig.get('index', '-')} {sig['interval']} candle @ {sig['candle_start']} (fired {sig['time']})")
        _refresh()
        ui.timer(2.0, _refresh)


def render_pattern_indicators():
    """'CANDLESTICK PATTERN INDICATORS' section, placed below Open Orders. One card per
    pattern registered in pattern_engine.PATTERN_REGISTRY, laid out SIDE BY SIDE in a row
    (wraps to a new line on narrow screens instead of overflowing), each themed per its
    registered color (bullish=light green, bearish=light red -- see _pattern_row), plus a
    shared fetch-delay input (seconds to wait after a candle boundary closes before fetching
    it via the historical API -- gives the broker's candle data time to finalize; applies to
    every pattern/interval, and also to Indicator Alerts below, which reads this same
    pattern_fetch_delay_sec param). Fetch Delay is draft-and-commit (typing has zero live
    effect until Set is clicked), logged via _log_alert_action on change. Detection itself
    runs from auto_run.run_bot_logic() via PatternEngine.check_patterns(), independently for
    both NIFTY and SENSEX regardless of the globally selected trading_index."""
    fetch_delay_draft = {'value': params.get('pattern_fetch_delay_sec', 5)}

    def _set_fetch_delay():
        try:
            v = float(fetch_delay_draft['value'])
        except (ValueError, TypeError):
            ui.notify("Invalid Fetch Delay", type='negative')
            return
        if v < 0:
            ui.notify("Fetch Delay cannot be negative", type='negative')
            return
        params['pattern_fetch_delay_sec'] = v
        ui.notify("Fetch Delay updated", type='positive')
        _log_alert_action(f"⚙️ Fetch Delay updated: {v:.0f}s")

    with ui.card().classes('w-full bg-white p-3 gap-3 rounded-xl shadow-sm mb-4 border border-gray-200'):
        with ui.row().classes('w-full justify-between items-center'):
            ui.label('CANDLESTICK PATTERN INDICATORS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')

        with ui.row().classes('w-full items-center gap-2'):
            ui.label('Fetch Delay (sec, after candle close):').classes('text-xs text-gray-600')
            ui.input().bind_value(fetch_delay_draft, 'value').props('outlined dense bg-color=white').classes('w-24')
            ui.button('Set', on_click=_set_fetch_delay).props('dense size=sm color=blue').classes('text-[10px]')

        with ui.row().classes('w-full gap-3 items-stretch flex-wrap'):
            for key, meta in PATTERN_REGISTRY.items():
                _pattern_row(key, meta)

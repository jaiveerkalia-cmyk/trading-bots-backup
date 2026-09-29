"""
indicator_engine.py
--------------------
Indicator alerts (EMA / SMA today, extensible) + their dashboard card.

HOW TO ADD A NEW INDICATOR
  1. Write a compute function: compute(closes, length) -> list the same length as `closes`,
     with None wherever the value isn't defined yet (not enough history).
  2. Add ONE entry to INDICATOR_REGISTRY below (label, compute, default_length, conditions).
  3. Done. The UI dropdown, the condition switches and the engine pick it up automatically.
Each condition is {'key','label','param','phrase','holds'}: `holds(close, value) -> bool` is
the state being alerted on, and `param` is the params key backing its on/off switch (created
automatically, default off, if it isn't in config.py). The stock price-vs-line conditions are
shared via PRICE_VS_LINE_CONDITIONS; a different indicator (e.g. RSI thresholds) just defines
its own list.

HOW IT RUNS
  Runs alongside pattern_engine.check_patterns() from auto_run.run_bot_logic(), independently
  for BOTH indices (NIFTY and SENSEX), whichever is the selected trading_index. For each
  (index, selected interval) pair:
    - Detects that the interval's candle has just closed. Boundaries are anchored to the 09:15
      session open (so 30m/1h candles close at :15/:45 and :15, matching the broker's candles).
    - Waits params['pattern_fetch_delay_sec'], fetches history via KiteConnect (spot token, or
      the near-month future's when Futures Mode is on -- config.get_eval_token), drops the
      still-forming candle, and requires the exact last-closed candle to be present (retries
      each tick up to pattern_engine.MAX_RETRY_SECONDS, then gives up for that boundary only).
    - Computes the indicator over several days of history (so EMA is warmed up and continuous
      across sessions) and evaluates the enabled conditions on the last two closed candles.
    - Mode 'Cross' (default): fires once, when the condition becomes true (previous candle did
      not satisfy it). Mode 'Every close': fires on every closed candle where it is true.
  On fire: shared Alert Sound Profile sound (respects Mute), one Trade Event Log line, a toast.
  Toast/log text deliberately avoids the keywords auto_run's ui.notify interceptor reacts to
  (ALERT / SET / ACTIVATED / RESET / ARMED / CLEARED) so each fire plays exactly one sound and
  writes exactly one log line.
"""

import math
from datetime import datetime, timedelta

from nicegui import ui

from config import params, shared_state, INDICES, UI_OPTS, get_eval_token
from pattern_engine import INTERVAL_DELTA, INTERVAL_KITE, MAX_RETRY_SECONDS


# ----------------------------------------------------------------------
# Indicator math
# ----------------------------------------------------------------------
def _ema(closes, length):
    """Exponential moving average, seeded with the SMA of the first `length` closes."""
    n = len(closes)
    out = [None] * n
    if length < 1 or n < length:
        return out
    k = 2.0 / (length + 1)
    prev = sum(closes[:length]) / length
    out[length - 1] = prev
    for i in range(length, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _sma(closes, length):
    """Simple moving average of the last `length` closes."""
    n = len(closes)
    out = [None] * n
    if length < 1 or n < length:
        return out
    window = sum(closes[:length])
    out[length - 1] = window / length
    for i in range(length, n):
        window += closes[i] - closes[i - length]
        out[i] = window / length
    return out


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
PRICE_VS_LINE_CONDITIONS = [
    {'key': 'above', 'label': 'Close Above', 'param': 'indicator_alert_above',
     'phrase': 'close above', 'holds': lambda close, val: close > val},
    {'key': 'below', 'label': 'Close Below', 'param': 'indicator_alert_below',
     'phrase': 'close below', 'holds': lambda close, val: close < val},
]

INDICATOR_REGISTRY = {
    'ema': {
        'label': 'EMA',
        'compute': _ema,
        'default_length': 20,
        'conditions': PRICE_VS_LINE_CONDITIONS,
    },
    'sma': {
        'label': 'SMA',
        'compute': _sma,
        'default_length': 20,
        'conditions': PRICE_VS_LINE_CONDITIONS,
    },
}


# ----------------------------------------------------------------------
# Timing / history constants
# ----------------------------------------------------------------------
SESSION_OPEN_MIN = 9 * 60 + 15   # 09:15 -- NSE and BSE index candles are anchored here

CANDLES_PER_DAY = {'1m': 375, '5m': 75, '15m': 25, '30m': 13, '1h': 7}
BASE_LOOKBACK_DAYS = {'1m': 3, '5m': 5, '15m': 10, '30m': 20, '1h': 40}
# Kite historical-data per-request limits (minute 60d, 5minute 100d, 15/30minute 200d, 60minute 400d)
MAX_LOOKBACK_DAYS = {'1m': 55, '5m': 95, '15m': 190, '30m': 190, '1h': 390}


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_boundary(interval, now):
    """True on the tick(s) of the minute where `interval`'s candle has just closed,
    anchored to the 09:15 session open."""
    total_min = int(INTERVAL_DELTA[interval].total_seconds() // 60)
    if total_min <= 0:
        return False
    since_open = now.hour * 60 + now.minute - SESSION_OPEN_MIN
    return since_open > 0 and since_open % total_min == 0


def _candle_key(d):
    if d is None:
        return None
    if hasattr(d, 'hour'):
        return (d.year, d.month, d.day, d.hour, d.minute)
    try:
        dt = datetime.strptime(str(d), '%Y-%m-%dT%H:%M:%S%z')
        return (dt.year, dt.month, dt.day, dt.hour, dt.minute)
    except Exception:
        return None


def _time_key(t):
    return (t.year, t.month, t.day, t.hour, t.minute)


def _get_length(meta):
    try:
        n = int(float(params.get('indicator_length', meta['default_length'])))
    except (TypeError, ValueError):
        n = meta['default_length']
    return max(1, n)


def _lookback_days(interval, length):
    cpd = CANDLES_PER_DAY.get(interval, 75)
    need = int(math.ceil((length * 5 + 10) / cpd * 1.5)) + 2   # ~5x length candles, padded for weekends
    return min(max(BASE_LOOKBACK_DAYS.get(interval, 5), need), MAX_LOOKBACK_DAYS.get(interval, 55))


def _active_conditions(meta):
    if not meta:
        return []
    return [c for c in meta['conditions'] if params.get(c['param'], False)]


def _selected_intervals():
    return [iv for iv in (params.get('indicator_intervals') or []) if iv in INTERVAL_DELTA]


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
class IndicatorEngine:
    def __init__(self, inst_manager):
        self.inst_manager = inst_manager
        self.pending = {(idx, iv): None for idx in INDICES for iv in INTERVAL_DELTA}
        self.last_armed_boundary = {(idx, iv): None for idx in INDICES for iv in INTERVAL_DELTA}
        self.fired = set()

        shared_state.setdefault('indicator_debug', {})
        shared_state.setdefault('indicator_last_signal', {})

    # ------------------------------------------------------------------
    def check(self):
        meta = INDICATOR_REGISTRY.get(params.get('indicator_type', 'ema'))
        selected = _selected_intervals()
        if not meta or not selected or not _active_conditions(meta):
            return

        now = datetime.now()
        boundary_time = now.replace(second=0, microsecond=0)

        for index in INDICES:
            for interval in selected:
                pkey = (index, interval)
                if _is_boundary(interval, now) and self.last_armed_boundary[pkey] != boundary_time:
                    self.last_armed_boundary[pkey] = boundary_time
                    if self.pending[pkey] is None:
                        self.pending[pkey] = {'boundary_time': boundary_time, 'first_attempt_at': None}

        try:
            delay = float(params.get('pattern_fetch_delay_sec', 3) or 3)
        except (TypeError, ValueError):
            delay = 3

        for index in INDICES:
            for interval in INTERVAL_DELTA:
                pkey = (index, interval)
                pend = self.pending[pkey]
                if pend is None:
                    continue
                if now < pend['boundary_time'] + timedelta(seconds=delay):
                    continue
                if pend['first_attempt_at'] is None:
                    pend['first_attempt_at'] = now
                self._attempt_fetch(index, interval, pend)

    # ------------------------------------------------------------------
    def _set_debug(self, pkey, status):
        shared_state['indicator_debug'][pkey] = {
            'status': status,
            'updated': datetime.now().strftime('%H:%M:%S'),
        }

    def _maybe_give_up(self, pkey, pend):
        if pend['first_attempt_at'] and \
                (datetime.now() - pend['first_attempt_at']).total_seconds() > MAX_RETRY_SECONDS:
            self._set_debug(pkey, 'gave up (candle data unavailable)')
            self.pending[pkey] = None

    # ------------------------------------------------------------------
    def _attempt_fetch(self, index, interval, pend):
        pkey = (index, interval)
        boundary_time = pend['boundary_time']
        last_start = boundary_time - INTERVAL_DELTA[interval]

        ind_key = params.get('indicator_type', 'ema')
        meta = INDICATOR_REGISTRY.get(ind_key)
        conds = _active_conditions(meta)
        if not meta or not conds or interval not in _selected_intervals():
            self.pending[pkey] = None
            return

        length = _get_length(meta)

        try:
            # Futures Mode aware (config.get_eval_token): near-month future's token when the
            # mode is on and resolved, otherwise the spot INDICES[index]['token'].
            token = get_eval_token(index)
        except Exception as e:
            self._set_debug(pkey, f'no token: {e}')
            self._maybe_give_up(pkey, pend)
            return

        from_date = boundary_time - timedelta(days=_lookback_days(interval, length))
        try:
            candles = self.inst_manager.kite.historical_data(
                token, from_date, datetime.now(), INTERVAL_KITE[interval]
            )
        except Exception as e:
            self._set_debug(pkey, f'fetch error: {e}')
            self._maybe_give_up(pkey, pend)
            return

        boundary_key = _time_key(boundary_time)
        series = []
        for c in (candles or []):
            key = _candle_key(c.get('date'))
            close = _to_float(c.get('close'))
            if key is None or close is None:
                continue
            if key >= boundary_key:      # never use the still-forming candle
                continue
            series.append((key, close))
        series.sort(key=lambda x: x[0])

        if not series or series[-1][0] != _time_key(last_start):
            self._set_debug(pkey, f"waiting for {last_start.strftime('%H:%M')} candle")
            self._maybe_give_up(pkey, pend)
            return

        closes = [c for _, c in series]
        try:
            values = meta['compute'](closes, length)
        except Exception as e:
            self._set_debug(pkey, f'compute error: {e}')
            self.pending[pkey] = None
            return

        cur_val = values[-1] if values else None
        if cur_val is None:
            self._set_debug(pkey, f'insufficient history ({len(closes)} candles, length {length})')
            self.pending[pkey] = None
            return
        prev_val = values[-2] if len(values) >= 2 else None
        close = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else None
        mode = params.get('indicator_alert_mode', 'Cross')

        for cond in conds:
            dedup_key = (index, ind_key, length, interval, cond['key'], mode, last_start)
            if dedup_key in self.fired:
                continue
            self.fired.add(dedup_key)

            if not cond['holds'](close, cur_val):
                continue
            if mode == 'Cross':
                if prev_val is None or prev_close is None:
                    continue
                if cond['holds'](prev_close, prev_val):
                    continue      # already on that side last candle -> not a fresh cross
            self._fire_signal(index, meta, length, interval, cond, last_start, close, cur_val)

        self._set_debug(pkey, f"done ({last_start.strftime('%H:%M')})")
        self.pending[pkey] = None

    # ------------------------------------------------------------------
    def _fire_signal(self, index, meta, length, interval, cond, candle_start, close, value):
        if not params.get('mute_sound'):
            sound = params.get('alert_upper_sound', 'Wood Plank')
            try:
                dur = float(params.get('alert_upper_duration', 5))
            except (TypeError, ValueError):
                dur = 5
            if dur <= 0:
                dur = 5
            shared_state.setdefault('sound_queue', [])
            shared_state['sound_queue'].append(('alert_custom', sound, dur))

        name = f"{meta['label']}({length})"
        msg = (f"INDICATOR: {index} {interval} {cond['phrase']} {name} @ "
               f"{candle_start.strftime('%H:%M')} | Close {close:.2f} vs {meta['label']} {value:.2f}")
        ts = datetime.now().strftime('%H:%M:%S')
        shared_state.setdefault('activity_log', [])
        shared_state['activity_log'].insert(0, f"[{ts}] {msg}")
        shared_state['activity_log'] = shared_state['activity_log'][:100]

        shared_state['indicator_last_signal'] = {
            'index': index,
            'interval': interval,
            'indicator': name,
            'condition': cond['label'],
            'candle_start': candle_start.strftime('%Y-%m-%d %H:%M'),
            'time': ts,
            'close': close,
            'value': value,
        }

        try:
            ui.notify(msg, type='warning', close_button=True)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Dashboard card
# ----------------------------------------------------------------------
def render_indicator_section():
    """'INDICATOR ALERTS' card, placed below the Candlestick Pattern Indicators card. Every
    control is read by the engine only at candle-boundary fetch time (never polled against a
    live threshold), so direct live binds to params are safe here -- no mid-edit risk."""
    for m in INDICATOR_REGISTRY.values():
        for c in m['conditions']:
            params.setdefault(c['param'], False)

    last_type = {'v': params.get('indicator_type', 'ema')}

    @ui.refreshable
    def conditions_row():
        meta = INDICATOR_REGISTRY.get(params.get('indicator_type', 'ema'))
        with ui.row().classes('w-full items-center gap-4'):
            ui.label('Alert on:').classes('text-xs text-gray-500 w-16')
            for cond in (meta['conditions'] if meta else []):
                ui.switch(cond['label'], value=params.get(cond['param'], False)) \
                    .bind_value(params, cond['param']).props('dense color=green')

    def on_type_change(e):
        old = INDICATOR_REGISTRY.get(last_type['v'], {})
        new = INDICATOR_REGISTRY.get(e.value, {})
        if new and old.get('default_length') != new.get('default_length'):
            params['indicator_length'] = new['default_length']
        last_type['v'] = e.value
        conditions_row.refresh()

    with ui.card().classes('w-full bg-white p-3 gap-3 rounded-xl shadow-sm mb-4 border border-gray-200'):
        ui.label('INDICATOR ALERTS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')

        with ui.card().classes('w-full p-3 gap-2 bg-blue-50 border border-blue-200 rounded-lg'):
            with ui.row().classes('w-full items-center gap-2'):
                ui.label('Indicator:').classes('text-xs text-gray-500 w-16')
                ui.select({k: m['label'] for k, m in INDICATOR_REGISTRY.items()},
                          value=params.get('indicator_type', 'ema'), on_change=on_type_change) \
                    .bind_value(params, 'indicator_type').props('outlined dense bg-color=white').classes('w-32')
                ui.label('Length:').classes('text-xs text-gray-500 ml-4')
                ui.input(value=str(params.get('indicator_length', 20))) \
                    .bind_value(params, 'indicator_length').props('outlined dense bg-color=white').classes('w-20')

            with ui.row().classes('w-full items-center gap-2'):
                ui.label('Intervals:').classes('text-xs text-gray-500 w-16')
                ui.select(UI_OPTS['pattern_intervals'], value=params.get('indicator_intervals', ['5m']), multiple=True) \
                    .bind_value(params, 'indicator_intervals').props('outlined dense bg-color=white use-chips').classes('grow')

            conditions_row()

            with ui.row().classes('w-full items-center gap-2'):
                ui.label('Mode:').classes('text-xs text-gray-500 w-16')
                ui.radio(UI_OPTS.get('indicator_modes', ['Cross', 'Every close']),
                         value=params.get('indicator_alert_mode', 'Cross')) \
                    .bind_value(params, 'indicator_alert_mode').props('inline dense')
                ui.label('Cross = once when the close crosses the line; Every close = each closed candle beyond it.') \
                    .classes('text-[10px] text-gray-400')

            status = ui.label('No signal yet.').classes('text-[10px] text-gray-400')

            def _refresh():
                sig = shared_state.get('indicator_last_signal') or {}
                if sig:
                    status.set_text(
                        f"Last: {sig['index']} {sig['interval']} {sig['indicator']} {sig['condition']} "
                        f"@ {sig['candle_start']} (fired {sig['time']})"
                    )
            _refresh()
            ui.timer(2.0, _refresh)

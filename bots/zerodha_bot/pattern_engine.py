"""
pattern_engine.py
------------------
Candlestick pattern detection engine (Bullish/Bearish Engulfing, extensible).

Runs alongside logic_engine.check_triggers() from auto_run.run_bot_logic().
For each configured interval (1m/5m/15m/30m/1h):
  - Detects when that interval's candle boundary has just closed.
  - Waits `params['pattern_fetch_delay_sec']` seconds.
  - Fetches historical candles via KiteConnect and matches EXACT required
    timestamps (never uses the still-forming/incomplete current candle).
  - If required candles aren't available yet, retries every tick until a
    max retry window elapses, then gives up for that boundary only.
  - Supports an "engulf candle count" per pattern: N subsequent candles are
    combined into one synthetic candle (open=first candle's open,
    close=last candle's close) and checked against the base candle
    immediately preceding that window.
  - On match: pushes the shared Alert Sound Profile sound, logs to the
    activity log, and shows a toast.
"""

from datetime import datetime, timedelta
from nicegui import ui

from config import params, shared_state, INDICES


INTERVAL_DELTA = {
    '1m': timedelta(minutes=1),
    '5m': timedelta(minutes=5),
    '15m': timedelta(minutes=15),
    '30m': timedelta(minutes=30),
    '1h': timedelta(hours=1),
}

INTERVAL_KITE = {
    '1m': 'minute',
    '5m': '5minute',
    '15m': '15minute',
    '30m': '30minute',
    '1h': '60minute',
}

MAX_RETRY_SECONDS = 30


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _detect_bullish_engulfing(base_open, base_close, synth_open, synth_close):
    base_open = _to_float(base_open)
    base_close = _to_float(base_close)
    synth_open = _to_float(synth_open)
    synth_close = _to_float(synth_close)
    if None in (base_open, base_close, synth_open, synth_close):
        return False
    prev_red = base_close < base_open
    curr_green = synth_close > synth_open
    engulfs = (synth_open <= base_close) and (synth_close >= base_open)
    return prev_red and curr_green and engulfs


def _detect_bearish_engulfing(base_open, base_close, synth_open, synth_close):
    base_open = _to_float(base_open)
    base_close = _to_float(base_close)
    synth_open = _to_float(synth_open)
    synth_close = _to_float(synth_close)
    if None in (base_open, base_close, synth_open, synth_close):
        return False
    prev_green = base_close > base_open
    curr_red = synth_close < synth_open
    engulfs = (synth_open >= base_close) and (synth_close <= base_open)
    return prev_green and curr_red and engulfs


PATTERN_REGISTRY = {
    'bullish_engulfing': {
        'label': 'Bullish Engulfing',
        'enabled_param': 'bullish_engulfing_enabled',
        'intervals_param': 'bullish_engulfing_intervals',
        'count_param': 'bullish_engulfing_count',
        'detector': _detect_bullish_engulfing,
        # UI card color (ui_components._pattern_row): light green for bullish setups.
        'color': 'green',
    },
    'bearish_engulfing': {
        'label': 'Bearish Engulfing',
        'enabled_param': 'bearish_engulfing_enabled',
        'intervals_param': 'bearish_engulfing_intervals',
        'count_param': 'bearish_engulfing_count',
        'detector': _detect_bearish_engulfing,
        # UI card color (ui_components._pattern_row): light red for bearish setups.
        'color': 'red',
    },
}


class PatternEngine:
    def __init__(self, inst_manager):
        self.inst_manager = inst_manager
        self.pending = {k: None for k in INTERVAL_DELTA}
        self.last_armed_boundary = {k: None for k in INTERVAL_DELTA}
        self.fired = {}

        if 'pattern_debug' not in shared_state:
            shared_state['pattern_debug'] = {}
        if 'pattern_last_signal' not in shared_state:
            shared_state['pattern_last_signal'] = {}

    # ------------------------------------------------------------------
    @staticmethod
    def _is_boundary(interval, now):
        """True exactly on the tick(s) where `interval`'s candle boundary has just closed.
        Generic (not a hardcoded per-interval dict) so any interval added to INTERVAL_DELTA
        in the future (e.g. a new '2h' or '10m') is handled automatically without needing a
        matching manual entry here: for sub-hour intervals, the boundary is every N minutes;
        for hour-or-longer intervals, it's additionally gated on the hour count dividing
        evenly (e.g. a hypothetical 2h interval would only fire on even hours)."""
        total_min = int(INTERVAL_DELTA[interval].total_seconds() // 60)
        if total_min <= 0:
            return False
        if total_min < 60:
            return now.minute % total_min == 0
        hours = total_min // 60
        return now.minute == 0 and (hours <= 1 or now.hour % hours == 0)

    # ------------------------------------------------------------------
    def check_patterns(self):
        now = datetime.now()
        boundary_time = now.replace(second=0, microsecond=0)

        is_boundary = {iv: self._is_boundary(iv, now) for iv in INTERVAL_DELTA}

        for interval in INTERVAL_DELTA:
            if is_boundary[interval] and self.last_armed_boundary[interval] != boundary_time:
                self.last_armed_boundary[interval] = boundary_time
                if self.pending[interval] is None:
                    self.pending[interval] = {
                        'boundary_time': boundary_time,
                        'first_attempt_at': None,
                    }

        for interval in INTERVAL_DELTA:
            pend = self.pending[interval]
            if pend is None:
                continue
            try:
                delay = float(params.get('pattern_fetch_delay_sec', 3) or 3)
            except (TypeError, ValueError):
                delay = 3
            fetch_after = pend['boundary_time'] + timedelta(seconds=delay)
            if now < fetch_after:
                continue
            if pend['first_attempt_at'] is None:
                pend['first_attempt_at'] = now
            self._attempt_fetch(interval, pend)

    # ------------------------------------------------------------------
    def _qualifying_patterns(self, interval):
        qualifying = []
        for key, meta in PATTERN_REGISTRY.items():
            if not params.get(meta['enabled_param'], True):
                continue
            if interval not in (params.get(meta['intervals_param']) or []):
                continue
            qualifying.append(key)
        return qualifying

    # ------------------------------------------------------------------
    def _attempt_fetch(self, interval, pend):
        boundary_time = pend['boundary_time']
        last_candle_start = boundary_time - INTERVAL_DELTA[interval]

        qualifying = self._qualifying_patterns(interval)
        if not qualifying:
            self.pending[interval] = None
            shared_state['pattern_debug'][interval] = None
            return

        counts = []
        for key in qualifying:
            try:
                c = int(params.get(PATTERN_REGISTRY[key]['count_param'], 1) or 1)
            except (TypeError, ValueError):
                c = 1
            counts.append(max(1, c))
        max_count = max(counts)

        try:
            token = INDICES[params['trading_index']]['token']
        except Exception as e:
            shared_state['pattern_debug'][interval] = {
                'status': f'no token: {e}',
                'updated': datetime.now().strftime('%H:%M:%S'),
            }
            self._maybe_give_up(interval, pend)
            return

        from_date = boundary_time - INTERVAL_DELTA[interval] * (max_count + 5)
        to_date = datetime.now()

        try:
            candles = self.inst_manager.kite.historical_data(
                token, from_date, to_date, INTERVAL_KITE[interval]
            )
        except Exception as e:
            shared_state['pattern_debug'][interval] = {
                'status': f'fetch error: {e}',
                'updated': datetime.now().strftime('%H:%M:%S'),
            }
            self._maybe_give_up(interval, pend)
            return

        boundary_key = (boundary_time.year, boundary_time.month, boundary_time.day,
                         boundary_time.hour, boundary_time.minute)

        lookup = {}
        for c in (candles or []):
            d = c.get('date')
            if d is None:
                continue
            if hasattr(d, 'hour'):
                key = (d.year, d.month, d.day, d.hour, d.minute)
            else:
                try:
                    dt_obj = datetime.strptime(str(d), '%Y-%m-%dT%H:%M:%S%z')
                    key = (dt_obj.year, dt_obj.month, dt_obj.day, dt_obj.hour, dt_obj.minute)
                except Exception:
                    continue
            # Never use the still-forming / incomplete current candle.
            if key >= boundary_key:
                continue
            lookup[key] = c

        all_resolved = True
        missing_any = []

        for key in qualifying:
            meta = PATTERN_REGISTRY[key]
            try:
                count = int(params.get(meta['count_param'], 1) or 1)
            except (TypeError, ValueError):
                count = 1
            count = max(1, count)

            window_starts = [last_candle_start - i * INTERVAL_DELTA[interval]
                              for i in range(count - 1, -1, -1)]
            base_start = last_candle_start - count * INTERVAL_DELTA[interval]
            required = [base_start] + window_starts
            req_keys = [(t.year, t.month, t.day, t.hour, t.minute) for t in required]

            if any(k not in lookup for k in req_keys):
                all_resolved = False
                missing_any.append(key)
                continue

            base_candle = lookup[req_keys[0]]
            window_candles = [lookup[k] for k in req_keys[1:]]
            synth_open = window_candles[0].get('open')
            synth_close = window_candles[-1].get('close')
            base_open = base_candle.get('open')
            base_close = base_candle.get('close')

            dedup_key = (key, interval, last_candle_start)
            if dedup_key in self.fired:
                continue

            is_match = meta['detector'](base_open, base_close, synth_open, synth_close)
            self.fired[dedup_key] = True

            if is_match:
                self._fire_signal(key, meta, interval, last_candle_start,
                                   base_open, base_close, synth_open, synth_close)

        shared_state['pattern_debug'][interval] = {
            'boundary_time': boundary_time.strftime('%H:%M:%S'),
            'status': 'done' if all_resolved else f'waiting for candles: {missing_any}',
            'updated': datetime.now().strftime('%H:%M:%S'),
        }

        if all_resolved:
            self.pending[interval] = None
        else:
            self._maybe_give_up(interval, pend)

    # ------------------------------------------------------------------
    def _maybe_give_up(self, interval, pend):
        if pend['first_attempt_at'] and \
                (datetime.now() - pend['first_attempt_at']).total_seconds() > MAX_RETRY_SECONDS:
            shared_state['pattern_debug'][interval] = {
                'status': 'gave up (candle data unavailable)',
                'updated': datetime.now().strftime('%H:%M:%S'),
            }
            self.pending[interval] = None

    # ------------------------------------------------------------------
    def _fire_signal(self, key, meta, interval, candle_start,
                      base_open, base_close, synth_open, synth_close):
        sound = params.get('alert_upper_sound', 'Wood Plank')
        duration = params.get('alert_upper_duration', 5)
        if not params.get('mute_sound'):
            try:
                dur = float(duration)
            except (TypeError, ValueError):
                dur = 5
            if dur <= 0:
                dur = 5
            shared_state.setdefault('sound_queue', [])
            shared_state['sound_queue'].append(('alert_custom', sound, dur))

        msg = f"PATTERN: {meta['label']} ({interval}) @ {candle_start.strftime('%H:%M')}"
        ts = datetime.now().strftime("%H:%M:%S")
        shared_state.setdefault('activity_log', [])
        shared_state['activity_log'].insert(0, f"[{ts}] {msg}")
        shared_state['activity_log'] = shared_state['activity_log'][:100]

        shared_state['pattern_last_signal'][key] = {
            'interval': interval,
            'candle_start': candle_start.strftime('%Y-%m-%d %H:%M'),
            'time': ts,
            'base_open': base_open,
            'base_close': base_close,
            'synth_open': synth_open,
            'synth_close': synth_close,
        }

        try:
            ui.notify(msg, type='warning', close_button=True)
        except Exception:
            pass

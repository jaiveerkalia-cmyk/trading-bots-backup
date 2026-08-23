"""
stop_via_candle_engine.py
--------------------------
"Enter via Stop" feature (params['enter_via_stop'], default ON).

When a Stop-Market unified entry, or an Index/Premium STOP (never Target) whose period is
candle-close-based, has its condition confirmed true at the moment its candle closes,
logic_engine.py hands the action off to this engine INSTEAD of executing it immediately.
This engine then:

  1. Waits a short delay after the candle boundary, then fetches that just-closed candle
     via KiteConnect's historical API (index candle for entries/index-stops; the option's
     OWN premium candle, via its instrument token, for premium-stops). Exact-timestamp
     matching, never the still-forming candle, with a 30s retry-then-give-up cap -- the
     same reliability pattern pattern_engine.py uses, but implemented independently here so
     nothing in that file is touched or shared (avoids any risk of interfering with pattern
     detection's own state).
  2. Arms a real Stop-Market trigger 1 tick beyond that candle's high or low, mirroring the
     direction of the ORIGINAL condition's breakout: a downside cross -> stop below the
     candle's low; an upside cross -> stop above the candle's high. Index-level tick = 0.5;
     premium-level tick = 0.05.
  3. Live-monitors that new trigger every tick (like a normal Stop-Market order) until price
     actually trades through it, then executes the real action (open_position for entries,
     close_position for index/premium stops) via the LogicEngine instance it was given.

Deferring is a one-shot hand-off: the original order/stop is disarmed/disabled at the moment
it's handed off (mirroring what the original code already did on successful execution), so
there's no double-firing and no lingering "still armed" UI state.

Jobs are tracked entirely in-memory on this class instance (self.jobs) -- no shared_state,
no UI surface. If the underlying position closes/opens via some other route while a job is
pending or armed, that job is dropped silently on its next check (no double action). If
candle data never arrives within the retry window, the job is dropped with a log entry and
no trade is taken.
"""

from datetime import datetime, timedelta

from config import shared_state, params, INDICES

INTERVAL_DELTA = {
    '1m': timedelta(minutes=1),
    '5m': timedelta(minutes=5),
    '15m': timedelta(minutes=15),
    '60m': timedelta(minutes=60),
}

INTERVAL_KITE = {
    '1m': 'minute',
    '5m': '5minute',
    '15m': '15minute',
    '60m': '60minute',
}

FETCH_DELAY_SEC = 3
MAX_RETRY_SECONDS = 30
INDEX_TICK = 0.5
PREMIUM_TICK = 0.05


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class StopViaCandleEngine:
    def __init__(self, inst_manager, logic_engine):
        self.inst_manager = inst_manager
        self.logic_engine = logic_engine
        self.jobs = []
        self._next_id = 1

    # ------------------------------------------------------------------
    # Registration (called from logic_engine.py at the moment a qualifying
    # condition is confirmed true, INSTEAD of executing it immediately)
    # ------------------------------------------------------------------
    def defer_entry(self, side, prefix, interval, now, index_name, is_downside,
                     qty, strike_offset, new_stop, new_target, reason):
        job = {
            'id': self._next_id, 'kind': 'entry', 'side': side, 'prefix': prefix,
            'interval': interval, 'boundary_time': now.replace(second=0, microsecond=0),
            'index_name': index_name, 'is_downside': is_downside,
            'qty': qty, 'strike_offset': strike_offset,
            'new_stop': new_stop, 'new_target': new_target, 'reason': reason,
            'status': 'awaiting_fetch', 'first_attempt_at': None, 'trigger_price': None,
        }
        self._next_id += 1
        self.jobs.append(job)
        self.logic_engine.log_action(
            f"🕯️ ENTER VIA STOP: {side} entry deferred ({interval}) - waiting for candle close",
            reason
        )

    def defer_index_stop(self, side, interval, now, index_name, is_downside, reason):
        job = {
            'id': self._next_id, 'kind': 'index_stop', 'side': side,
            'interval': interval, 'boundary_time': now.replace(second=0, microsecond=0),
            'index_name': index_name, 'is_downside': is_downside, 'reason': reason,
            'status': 'awaiting_fetch', 'first_attempt_at': None, 'trigger_price': None,
        }
        self._next_id += 1
        self.jobs.append(job)
        self.logic_engine.log_action(
            f"🕯️ ENTER VIA STOP: {side} Idx Stop deferred ({interval}) - waiting for candle close",
            reason
        )

    def defer_premium_stop(self, side, interval, now, trade_token, trade_symbol, is_downside, reason):
        job = {
            'id': self._next_id, 'kind': 'premium_stop', 'side': side,
            'interval': interval, 'boundary_time': now.replace(second=0, microsecond=0),
            'token': trade_token, 'symbol': trade_symbol,
            'is_downside': is_downside, 'reason': reason,
            'status': 'awaiting_fetch', 'first_attempt_at': None, 'trigger_price': None,
        }
        self._next_id += 1
        self.jobs.append(job)
        self.logic_engine.log_action(
            f"🕯️ ENTER VIA STOP: {side} Prem Stop deferred ({interval}) - waiting for candle close",
            reason
        )

    # ------------------------------------------------------------------
    # Called every tick from auto_run.py's background loop
    # ------------------------------------------------------------------
    def check(self):
        if not self.jobs:
            return
        now = datetime.now()
        remaining = []
        for job in self.jobs:
            if job['status'] == 'awaiting_fetch':
                self._try_fetch(job, now)
                if job['status'] != 'dropped':
                    remaining.append(job)
            elif job['status'] == 'armed':
                if not self._try_execute(job, now):
                    remaining.append(job)
                # else: resolved (fired, or silently dropped because the underlying
                # position already changed via another route) -- don't keep it
        self.jobs = remaining

    # ------------------------------------------------------------------
    def _try_fetch(self, job, now):
        fetch_after = job['boundary_time'] + timedelta(seconds=FETCH_DELAY_SEC)
        if now < fetch_after:
            return
        if job['first_attempt_at'] is None:
            job['first_attempt_at'] = now

        interval = job['interval']
        last_candle_start = job['boundary_time'] - INTERVAL_DELTA[interval]

        try:
            if job['kind'] == 'premium_stop':
                token = job['token']
            else:
                token = INDICES[job['index_name']]['token']
            from_date = job['boundary_time'] - INTERVAL_DELTA[interval] * 6
            to_date = now
            candles = self.inst_manager.kite.historical_data(
                token, from_date, to_date, INTERVAL_KITE[interval]
            )
        except Exception as e:
            self._maybe_give_up(job, now, f"fetch error: {e}")
            return

        boundary_key = (job['boundary_time'].year, job['boundary_time'].month, job['boundary_time'].day,
                         job['boundary_time'].hour, job['boundary_time'].minute)
        target_key = (last_candle_start.year, last_candle_start.month, last_candle_start.day,
                      last_candle_start.hour, last_candle_start.minute)

        target_candle = None
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
            if key == target_key:
                target_candle = c
                break

        if target_candle is None:
            self._maybe_give_up(job, now, "candle not yet available")
            return

        high = _to_float(target_candle.get('high'))
        low = _to_float(target_candle.get('low'))
        if high is None or low is None:
            self._maybe_give_up(job, now, "candle missing high/low")
            return

        tick = PREMIUM_TICK if job['kind'] == 'premium_stop' else INDEX_TICK
        if job['is_downside']:
            job['trigger_price'] = low - tick
        else:
            job['trigger_price'] = high + tick

        job['status'] = 'armed'
        direction_txt = 'below candle low' if job['is_downside'] else 'above candle high'
        self.logic_engine.log_action(
            f"🕯️ STOP ARMED: {job['side']} {job['kind']} @ {job['trigger_price']:.2f} "
            f"({direction_txt}, {interval})",
            job['reason']
        )

    def _maybe_give_up(self, job, now, status_msg):
        if job['first_attempt_at'] and (now - job['first_attempt_at']).total_seconds() > MAX_RETRY_SECONDS:
            self.logic_engine.log_action(
                f"⚠️ ENTER VIA STOP gave up: {job['side']} {job['kind']} ({job['interval']}) - {status_msg}"
            )
            job['status'] = 'dropped'

    # ------------------------------------------------------------------
    def _try_execute(self, job, now):
        """Returns True if the job is resolved (fired, or silently dropped) and should be
        removed from the active list; False if it should keep being monitored."""
        side = job['side']
        kind = job['kind']

        if kind == 'entry':
            if shared_state['active_trades'][side] is not None:
                # Position already opened via another route in the meantime -- drop silently.
                return True
            live_price = shared_state.get(job['index_name'], {}).get('ltp', 0)
            if live_price <= 0:
                return False
            crossed = (live_price <= job['trigger_price']) if job['is_downside'] else (live_price >= job['trigger_price'])
            if not crossed:
                return False
            success, msg = self.logic_engine.open_position(
                side,
                reason=f"{job['reason']} (via candle-stop @ {job['trigger_price']:.2f})",
                qty_override=job['qty'], strike_offset=job['strike_offset']
            )
            if success:
                prefix = job['prefix']
                try:
                    new_stop = float(job['new_stop'])
                    if new_stop > 0:
                        params[f'{prefix}_stop_val'] = new_stop
                        params[f'{prefix}_stop_active'] = True
                except (ValueError, TypeError):
                    pass
                try:
                    new_target = float(job['new_target'])
                    if new_target > 0:
                        params[f'{prefix}_target_val'] = new_target
                        params[f'{prefix}_target_active'] = True
                except (ValueError, TypeError):
                    pass
            else:
                self.logic_engine.log_action(f"⚠️ Deferred entry stop fired but open failed: {msg}")
            return True

        elif kind == 'index_stop':
            if shared_state['active_trades'][side] is None:
                return True  # already closed via another route
            live_price = shared_state.get(job['index_name'], {}).get('ltp', 0)
            if live_price <= 0:
                return False
            crossed = (live_price <= job['trigger_price']) if job['is_downside'] else (live_price >= job['trigger_price'])
            if not crossed:
                return False
            self.logic_engine.close_position(
                side, f"{job['reason']} (via candle-stop @ {job['trigger_price']:.2f})"
            )
            return True

        elif kind == 'premium_stop':
            if shared_state['active_trades'][side] is None:
                return True  # already closed via another route
            live_price = shared_state.get('option_chain', {}).get(job['token'], {}).get('ltp', 0)
            if live_price <= 0:
                return False
            crossed = (live_price <= job['trigger_price']) if job['is_downside'] else (live_price >= job['trigger_price'])
            if not crossed:
                return False
            self.logic_engine.close_position(
                side, f"{job['reason']} (via candle-stop @ {job['trigger_price']:.2f})"
            )
            return True

        return True  # unknown kind -- drop defensively, never get stuck

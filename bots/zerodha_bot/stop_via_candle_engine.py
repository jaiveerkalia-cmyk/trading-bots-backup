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
  2. Computes a real Stop-Market trigger 1 tick beyond that candle's high or low, mirroring
     the direction of the ORIGINAL condition's breakout: a downside cross -> stop below the
     candle's low; an upside cross -> stop above the candle's high. Index-level tick = 0.5;
     premium-level tick = 0.05.
  3. HANDS OFF immediately: writes that new trigger straight into the SAME params the
     original order/stop used (Unified Entry trigger_price/order_type/fire_on, or Index/
     Premium Stop val/time), re-arms/re-activates it there, and drops its own internal job.
     From that moment on the order is a completely normal, live ('Live'/'Current') armed
     order -- monitored and fired by the EXISTING logic_engine.py paths
     (_check_unified_open / _check_exits), and visible/editable/cancelable through the
     EXACT SAME UI (Open Short/Long card, Order Book) as any manually-armed order. This
     engine does not live-monitor or fire anything itself once handed off -- there is
     nothing left here to duplicate that.

Deferring is a one-shot hand-off: the original order/stop is disarmed/disabled at the moment
it's first deferred (mirroring what the original code already did on successful execution),
so there's no double-firing while the candle is being fetched.

Jobs only exist in the brief 'awaiting_fetch' window (a few seconds, gated by
FETCH_DELAY_SEC) between deferral and hand-off. If the user cancels/resets/modifies the
ORIGINAL order (or the underlying position closes/opens via another route) during that
window, cancel_pending()/the re-arm guard in _handoff() ensure the pending job never
resurrects or clobbers it. If candle data never arrives within the retry window, the job is
dropped with a log entry and no trade is taken.

cancel_all() lets an external "kill everything" event (global stop/target, close all, etc)
explicitly drop every pending job immediately, without executing anything -- see
LogicEngine._check_global_limits() for the main caller.
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
            # 'awaiting_fetch' is the only status a job can have now -- once the candle is
            # fetched, _try_fetch hands off and the job is dropped in the same pass (see
            # below). Nothing is ever kept around in an 'armed'/live-monitored state here.
            self._try_fetch(job, now)
            if job['status'] != 'dropped':
                remaining.append(job)
        self.jobs = remaining

    # ------------------------------------------------------------------
    def cancel_pending(self, kind, side):
        """Called from the UI whenever the person cancels/resets/modifies the ORIGINAL
        order/stop that a job might currently be deferred for (Open Short/Long Cancel or
        Order Book REMOVE for entries; Index/Premium Stop Reset or Order Book REMOVE for
        stops). Drops any matching still-pending job so it can never later resurrect (or, if
        the person immediately re-armed with different values, clobber) an order the person
        just changed their mind about. A no-op if no matching job exists -- always safe to
        call unconditionally from these handlers."""
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if not (j['kind'] == kind and j['side'] == side)]
        if len(self.jobs) < before:
            self.logic_engine.log_action(f"🚫 ENTER VIA STOP: pending {side} {kind} cancelled (order changed)")

    def cancel_all(self, reason="Cancelled"):
        """Drops every pending job immediately WITHOUT executing anything -- used when a
        global stop/target (or any other 'kill everything' event) fires."""
        if not self.jobs:
            return
        for job in self.jobs:
            self.logic_engine.log_action(
                f"🚫 ENTER VIA STOP cancelled: {job['side']} {job['kind']} ({job['interval']}) - {reason}"
            )
        self.jobs = []

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
        trigger_price = (low - tick) if job['is_downside'] else (high + tick)
        job['trigger_price'] = trigger_price

        self._handoff(job, trigger_price)
        job['status'] = 'dropped'

    def _maybe_give_up(self, job, now, status_msg):
        if job['first_attempt_at'] and (now - job['first_attempt_at']).total_seconds() > MAX_RETRY_SECONDS:
            self.logic_engine.log_action(
                f"⚠️ ENTER VIA STOP gave up: {job['side']} {job['kind']} ({job['interval']}) - {status_msg}"
            )
            job['status'] = 'dropped'

    # ------------------------------------------------------------------
    def _handoff(self, job, trigger_price):
        """Writes the newly-computed trigger straight into the same params the original
        order/stop used, re-arming/re-activating it there as a normal LIVE order -- from
        this point on it is monitored and fired entirely by the existing
        _check_unified_open()/_check_exits() paths in logic_engine.py, and it is the SAME
        order the person sees/edits/cancels in the Open Short/Long card and Order Book (no
        separate 'deferred order' UI surface needed).

        Guarded against a race with the UI: if the person has ALREADY manually re-armed/
        re-activated this exact side+kind in the moments since it was disarmed at deferral
        time (e.g. re-armed the card by hand while the candle was being fetched), that manual
        action wins -- the hand-off is skipped rather than silently overwriting it."""
        side = job['side']
        kind = job['kind']
        direction_txt = 'below candle low' if job['is_downside'] else 'above candle high'

        if kind == 'entry':
            prefix = job['prefix']
            if params.get(f'{prefix}_armed', False):
                self.logic_engine.log_action(
                    f"ℹ️ ENTER VIA STOP: {side} entry hand-off skipped - order was re-armed manually"
                )
                return
            params[f'{prefix}_order_type'] = 'Stop-Market'
            params[f'{prefix}_trigger_price'] = trigger_price
            params[f'{prefix}_fire_on'] = 'Live'
            params[f'{prefix}_strike_offset'] = job['strike_offset']
            params[f'{prefix}_qty'] = job['qty']
            params[f'{prefix}_new_stop'] = job['new_stop']
            params[f'{prefix}_new_target'] = job['new_target']
            params[f'{prefix}_armed_at'] = datetime.now()
            params[f'{prefix}_armed'] = True
            self.logic_engine.log_action(
                f"🕯️ STOP ARMED (live): {side} entry Stop-Market @ {trigger_price:.2f} ({direction_txt})",
                job['reason']
            )

        elif kind == 'index_stop':
            active_key = f'{side.lower()}_index_stop_active'
            if params.get(active_key, False):
                self.logic_engine.log_action(
                    f"ℹ️ ENTER VIA STOP: {side} Idx Stop hand-off skipped - stop was re-activated manually"
                )
                return
            params[f'{side.lower()}_index_stop_val'] = trigger_price
            params[f'{side.lower()}_index_stop_time'] = 'Current'
            params[active_key] = True
            self.logic_engine.log_action(
                f"🕯️ STOP ARMED (live): {side} Idx Stop @ {trigger_price:.2f} ({direction_txt})",
                job['reason']
            )

        elif kind == 'premium_stop':
            active_key = f'{side.lower()}_prem_stop_active'
            if params.get(active_key, False):
                self.logic_engine.log_action(
                    f"ℹ️ ENTER VIA STOP: {side} Prem Stop hand-off skipped - stop was re-activated manually"
                )
                return
            params[f'{side.lower()}_prem_stop_val'] = trigger_price
            params[f'{side.lower()}_prem_stop_time'] = 'Current'
            params[active_key] = True
            self.logic_engine.log_action(
                f"🕯️ STOP ARMED (live): {side} Prem Stop @ {trigger_price:.2f} ({direction_txt})",
                job['reason']
            )

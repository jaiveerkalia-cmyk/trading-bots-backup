"""
positions_engine.py -- Positions V2 (enabled only when env ZB_POSITIONS_V2=1; see auto_run.py).

Multi-position trading core: any number of positions across both indices and both directions,
each owning its own exits. Merges same-instrument adds (weighted average), nets opposite
orders on the same instrument (after the card's confirmation), supports partial exits, and
evaluates PnL / index / premium stops and targets PER POSITION.

Reuses LogicEngine primitives (orders, fills, csv, logging, chart, global limits, alerts) so
nothing about order placement or fills is re-implemented here. LogicEngine hands over via
small gated hooks (logic.v2 is None unless the flag is on), so with the flag off this module
is never imported and legacy behaviour is untouched.

positions live in shared_state['positions'] (list of dicts, schema in _new_position).
shared_state['active_trades']['Call'/'Put'] is kept as a MIRROR (same dict object) of the first
CE / first PE position so the banner cards keep working unchanged.
"""
import time
import uuid
from datetime import datetime

from config import shared_state, params, INDICES, get_eval_price

# card key -> (option type, direction). 'call'/'put' are the Sell cards, 'bcall'/'bput' the Buy cards.
CARDS = {'call': ('CE', 'SELL'), 'put': ('PE', 'SELL'), 'bcall': ('CE', 'BUY'), 'bput': ('PE', 'BUY')}
EXIT_KINDS = ('pnl_stop', 'pnl_tgt', 'prem_stop', 'prem_tgt', 'idx_stop', 'idx_tgt')
PERIODS = ('Current', '1m', '5m', '15m')
HEDGE_STEPS = 10


def commission(qty, buy_price, sell_price, orders=2):
    """Same formula as LogicEngine.commission; brokerage = Rs20 x executed orders.
    orders=2 (entry + exit) reproduces the legacy figure exactly."""
    buy_turnover = qty * buy_price
    sell_turnover = qty * sell_price
    total_turnover = buy_turnover + sell_turnover
    brokerage = 20 * orders
    stt = 0.0015 * sell_turnover
    exchange_txn = 0.0003553 * total_turnover
    sebi = 0.000001 * total_turnover
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp_duty = 0.00003 * buy_turnover
    ipft = 0.00000005 * total_turnover
    return round(brokerage + stt + exchange_txn + sebi + gst + stamp_duty + ipft, 2)


def _f(x, default=0.0):
    try:
        if str(x).strip() == '':
            return default
        return float(x)
    except (ValueError, TypeError):
        return default


class PositionsEngine:
    def __init__(self, logic, stop_engine=None):
        self.logic = logic
        self.stop_engine = stop_engine
        # Flipped on by the Enter-via-Stop stage once the stop engine understands positions.
        self.defer_entries = False
        self.defer_exits = False
        self._seq = 0
        shared_state.setdefault('positions', [])
        shared_state.setdefault('v2_entry_debug', {})

    # ------------------------------------------------------------------ helpers
    @property
    def positions(self):
        return shared_state['positions']

    def find(self, pos_id):
        for p in self.positions:
            if p['id'] == pos_id:
                return p
        return None

    def label(self, pos):
        return f"{pos['index']} {int(pos['main']['strike'])} {pos['type']}"

    def _reg(self, index):
        r = INDICES[index]
        return float(r['step']), int(r['lot_size']), r['segment']

    @staticmethod
    def _bearish(pos_type, direction):
        """Sell CE and Buy PE profit when the index falls (stop above, target below)."""
        return (pos_type == 'CE') == (direction == 'SELL')

    def _new_slots(self):
        return {k: {'val': '', 'period': 'Current', 'lots': '', 'active': False, 'state': 'off'} for k in EXIT_KINDS}

    def _mirror(self):
        ce = next((p for p in self.positions if p['type'] == 'CE'), None)
        pe = next((p for p in self.positions if p['type'] == 'PE'), None)
        shared_state['active_trades']['Call'] = ce
        shared_state['active_trades']['Put'] = pe

    # ------------------------------------------------------------------ live orders
    def _orders_open(self, kite, segment, main_sym, hedge_sym, qty, buy):
        """Same sequence as legacy open_position: hedge BUY first (1s gap), then main."""
        lg = self.logic
        if params['live_trading'] != 'On':
            return True, ''
        if hedge_sym:
            if not lg._place_live_order(hedge_sym, kite.TRANSACTION_TYPE_BUY, qty, kite.PRODUCT_MIS, segment):
                return False, 'Hedge Fail'
            time.sleep(1)
        txn = kite.TRANSACTION_TYPE_BUY if buy else kite.TRANSACTION_TYPE_SELL
        if not lg._place_live_order(main_sym, txn, qty, kite.PRODUCT_MIS, segment):
            return False, 'Main Fail'
        return True, ''

    def _orders_close(self, kite, segment, main_sym, hedge_sym, qty, was_buy):
        lg = self.logic
        if params['live_trading'] != 'On':
            return True, ''
        txn = kite.TRANSACTION_TYPE_SELL if was_buy else kite.TRANSACTION_TYPE_BUY
        if not lg._place_live_order(main_sym, txn, qty, kite.PRODUCT_MIS, segment):
            return False, 'Cover Fail'
        if hedge_sym:
            time.sleep(1)
            if not lg._place_live_order(hedge_sym, kite.TRANSACTION_TYPE_SELL, qty, kite.PRODUCT_MIS, segment):
                lg.log_action('⚠️ Hedge Exit Fail'); lg.play_sound('error')
        return True, ''

    # ------------------------------------------------------------------ open / add / net
    def analyze(self, opt, direction, main_token, hedge_token):
        """Returns (action, existing_position, message): action in new|merge|net|block."""
        target, action = None, 'new'
        for p in self.positions:
            if p['main']['token'] == main_token:
                if p['direction'] == direction and p['type'] == opt:
                    target, action = p, 'merge'
                else:
                    target, action = p, 'net'
                break
        new_legs = [(main_token, -1 if direction == 'SELL' else 1, 'main')]
        if hedge_token:
            new_legs.append((hedge_token, 1, 'hedge'))
        for p in self.positions:
            if p is target:
                continue
            old_legs = [(p['main']['token'], -1 if p['direction'] == 'SELL' else 1, 'main')]
            if p['hedge']:
                old_legs.append((p['hedge']['token'], 1, 'hedge'))
            for tok, sign, role in new_legs:
                for otok, osign, orole in old_legs:
                    if tok == otok and sign != osign:
                        return 'block', p, f"{role} leg would offset the {orole} leg of {self.label(p)}"
        return action, target, ''

    def preview_card(self, card, lots, strike_offset):
        """Read-only resolution used by the UI warning: returns dict(action, existing, strike,
        label, msg). Never places orders."""
        r = self._resolve(card, lots, strike_offset)
        if r.get('error'):
            return {'action': 'error', 'msg': r['error']}
        action, existing, msg = self.analyze(r['opt'], r['direction'], r['main_token'], r['hedge_token'])
        return {'action': action, 'existing': existing, 'msg': msg, 'label': f"{r['index']} {int(r['main_strike'])} {r['opt']}",
                'qty': r['qty']}

    def _resolve(self, card, lots, strike_offset):
        opt, direction = CARDS[card]
        index = params.get(f'{card}_index', params['trading_index'])
        spot = shared_state[index]['ltp']
        if spot == 0:
            return {'error': 'Index Price 0'}
        step, lot_size, segment = self._reg(index)
        buy = direction == 'BUY'
        hedgeless = buy or params.get('hedgeless_mode', False)
        off = _f(strike_offset, 0.0)
        atm = round(spot / step) * step
        if abs(off) >= 1000:
            main_strike = off
        else:
            main_strike = (atm - off * step) if opt == 'CE' else (atm + off * step)
        inst = self.logic.inst_manager
        main_token, main_symbol = inst.get_atm_token(index, main_strike, opt)
        if not main_token:
            return {'error': 'Token Not Found'}
        hedge_token = hedge_symbol = hedge_strike = None
        if not hedgeless:
            hedge_strike = main_strike + HEDGE_STEPS * step if opt == 'CE' else main_strike - HEDGE_STEPS * step
            hedge_token, hedge_symbol = inst.get_atm_token(index, hedge_strike, opt)
            if not hedge_token:
                return {'error': 'Hedge Token Not Found'}
        try:
            qty = int(float(lots)) * lot_size
        except (ValueError, TypeError):
            return {'error': 'Invalid Qty'}
        if qty <= 0:
            return {'error': 'Invalid Qty'}
        return dict(opt=opt, direction=direction, index=index, step=step, lot_size=lot_size, segment=segment, buy=buy,
                    main_strike=main_strike, main_token=main_token, main_symbol=main_symbol, hedge_token=hedge_token,
                    hedge_symbol=hedge_symbol, hedge_strike=hedge_strike, qty=qty)

    def open_from_card(self, card, lots, strike_offset, reason='Manual', net_ok=False, new_stop='', new_target=''):
        """Entry point for all four cards (Market, armed Limit/Stop-Market, Enter-via-Stop hand-off).
        Returns (ok, message)."""
        lg = self.logic
        if not lg.trading_active:
            return False, 'Trading Stopped'
        r = self._resolve(card, lots, strike_offset)
        if r.get('error'):
            return False, r['error']
        action, existing, msg = self.analyze(r['opt'], r['direction'], r['main_token'], r['hedge_token'])
        if action == 'block':
            return False, f"Blocked: {msg}"
        if action == 'net' and not net_ok:
            return False, f"Opposite position on {self.label(existing)}: confirm offset first"
        if action == 'merge':
            ok, m, pos = self._add_to(existing, r['qty'], reason)
        elif action == 'net':
            matched = min(r['qty'], existing['qty'])
            ok, m, _ = self._exit(existing, matched, f"Netted by {card} order")
            pos = None
            remainder = r['qty'] - matched
            if ok and remainder > 0:
                ok, m, pos = self._open_new(r, remainder, reason)
        else:
            ok, m, pos = self._open_new(r, r['qty'], reason)
        if ok and pos is not None:
            self._apply_entry_exits(pos, new_stop, new_target)
        self._mirror()
        return ok, m

    def _apply_entry_exits(self, pos, new_stop, new_target):
        s, t = _f(new_stop), _f(new_target)
        if s > 0:
            pos['exits']['pnl_stop'].update(val=s, active=True, state='working')
        if t > 0:
            pos['exits']['pnl_tgt'].update(val=t, active=True, state='working')

    def _subscribe(self, tokens_symbols):
        lg = self.logic
        lg.ticker.subscribe_new([t for t, _ in tokens_symbols])
        for t, s in tokens_symbols:
            if t not in shared_state['option_chain']:
                shared_state['option_chain'][t] = {'ltp': 0.0, 'symbol': s}

    def _open_new(self, r, qty, reason):
        lg = self.logic
        kite = lg.inst_manager.kite
        ok, why = self._orders_open(kite, r['segment'], r['main_symbol'], r['hedge_symbol'], qty, r['buy'])
        if not ok:
            return False, why, None
        legs = [(r['main_token'], r['main_symbol'])] + ([(r['hedge_token'], r['hedge_symbol'])] if r['hedge_token'] else [])
        self._subscribe(legs)
        eval_ltp = get_eval_price(r['index'])
        m_pr, _ = lg.inst_manager.get_fill_price(r['main_token'], r['main_symbol'], r['segment'], qty, is_buy=r['buy'])
        hedge = None
        if r['hedge_token']:
            h_pr, _ = lg.inst_manager.get_fill_price(r['hedge_token'], r['hedge_symbol'], r['segment'], qty, is_buy=True)
            hedge = {'symbol': r['hedge_symbol'], 'token': r['hedge_token'], 'strike': r['hedge_strike'], 'entry_price': h_pr, 'current_price': h_pr}
        self._seq += 1
        pos = {
            'id': str(uuid.uuid4())[:8], 'pos_no': self._seq, 'index': r['index'], 'type': r['opt'], 'direction': r['direction'],
            'qty': qty, 'status': 'OPEN', 'pnl': 0.0, 'index_entry_price': eval_ltp, 'index_current_price': eval_ltp,
            'csv_synced': False, 'entry_time': datetime.now().strftime('%H:%M:%S'), 'trigger': reason,
            'main': {'symbol': r['main_symbol'], 'token': r['main_token'], 'strike': r['main_strike'], 'entry_price': m_pr, 'current_price': m_pr},
            'hedge': hedge, 'brk_pending': 1, 'partials': 0, 'adds': 0, 'exits': self._new_slots(),
        }
        self.positions.append(pos)
        lg.csv_manager.log_open(pos['id'], pos)
        tag = 'BUY' if r['buy'] else ('HEDGELESS' if not hedge else 'SELL')
        lg.log_action(f"OPENED {self.label(pos)} {tag} x{qty} ({reason})", f"Idx: {eval_ltp:.2f} | M: {r['main_strike']} ({m_pr:.2f})")
        lg.play_sound('open')
        lg.add_chart_marker(f"Open {pos['type']}", shared_state['pnl']['realized'])
        return True, f"Opened {self.label(pos)}", pos

    def _add_to(self, pos, qty, reason):
        lg = self.logic
        kite = lg.inst_manager.kite
        _, _, segment = self._reg(pos['index'])
        buy = pos['direction'] == 'BUY'
        ok, why = self._orders_open(kite, segment, pos['main']['symbol'], pos['hedge']['symbol'] if pos['hedge'] else None, qty, buy)
        if not ok:
            return False, why, None
        m = pos['main']
        m_pr, _ = lg.inst_manager.get_fill_price(m['token'], m['symbol'], segment, qty, is_buy=buy)
        q0 = pos['qty']
        e0 = m['entry_price'] if m['entry_price'] > 0 else m_pr
        m['entry_price'] = (q0 * e0 + qty * m_pr) / (q0 + qty)
        h_avg = 0
        if pos['hedge']:
            h = pos['hedge']
            h_pr, _ = lg.inst_manager.get_fill_price(h['token'], h['symbol'], segment, qty, is_buy=True)
            he0 = h['entry_price'] if h['entry_price'] > 0 else h_pr
            h['entry_price'] = (q0 * he0 + qty * h_pr) / (q0 + qty)
            h_avg = h['entry_price']
        eval_ltp = get_eval_price(pos['index'])
        pos['index_entry_price'] = (q0 * pos['index_entry_price'] + qty * eval_ltp) / (q0 + qty)
        pos['qty'] = q0 + qty
        pos['brk_pending'] += 1
        pos['adds'] += 1
        mod = getattr(lg.csv_manager, 'log_modify', None)
        if mod:
            mod(pos['id'], pos['qty'], m['entry_price'], h_avg, pos['index_entry_price'])
        lg.log_action(f"ADDED {self.label(pos)} +{qty} ({reason})", f"Fill {m_pr:.2f} | Avg {m['entry_price']:.2f} | Qty {pos['qty']}")
        lg.play_sound('open')
        lg.add_chart_marker(f"Add {pos['type']}", shared_state['pnl']['realized'])
        return True, f"Added to {self.label(pos)}", pos

    # ------------------------------------------------------------------ exits
    def close_position(self, pos_id, reason='Manual'):
        pos = self.find(pos_id)
        if not pos:
            return False, 'No Position'
        ok, m, _ = self._exit(pos, pos['qty'], reason)
        self._mirror()
        return ok, m

    def partial_exit(self, pos_id, lots, reason='Partial Exit'):
        pos = self.find(pos_id)
        if not pos:
            return False, 'No Position'
        _, lot_size, _ = self._reg(pos['index'])
        try:
            n = int(float(lots))
        except (ValueError, TypeError):
            return False, 'Invalid Lots'
        if n < 1:
            return False, 'Lots must be at least 1'
        if n * lot_size >= pos['qty']:
            return False, 'Use CLOSE for a full exit'
        ok, m, _ = self._exit(pos, n * lot_size, reason)
        self._mirror()
        return ok, m

    def _exit(self, pos, qty, reason):
        """Closes `qty` units of pos (full close when qty == pos['qty']). Realized PnL, history,
        CSV, logs, sound and chart marker all follow the legacy close_position conventions."""
        lg = self.logic
        kite = lg.inst_manager.kite
        _, lot_size, segment = self._reg(pos['index'])
        qty = int(qty)
        if qty <= 0 or qty > pos['qty']:
            return False, 'Invalid Qty', 0.0
        full = qty == pos['qty']
        m, h = pos['main'], pos['hedge']
        was_buy = pos['direction'] == 'BUY'
        ok, why = self._orders_close(kite, segment, m['symbol'], h['symbol'] if h else None, qty, was_buy)
        if not ok:
            return False, why, 0.0
        m_curr, m_depth = lg.inst_manager.get_fill_price(m['token'], m['symbol'], segment, qty, is_buy=(not was_buy))
        m_entry = m['entry_price'] if m['entry_price'] > 0 else m_curr
        idx_curr = get_eval_price(pos['index'])
        orders = 1 + max(0, int(pos.get('brk_pending', 1)))
        if h:
            h_curr, _ = lg.inst_manager.get_fill_price(h['token'], h['symbol'], segment, qty, is_buy=False)
            h_entry = h['entry_price'] if h['entry_price'] > 0 else h_curr
            net = ((m_entry - m_curr) * qty) + ((h_curr - h_entry) * qty)
            net -= (commission(qty, m_curr, m_entry, orders) + commission(qty, h_curr, h_entry, orders))
            h_exit = h_curr
        else:
            h_exit = 0
            if was_buy:
                net = (m_curr - m_entry) * qty - commission(qty, m_entry, m_curr, orders)
            else:
                net = (m_entry - m_curr) * qty - commission(qty, m_curr, m_entry, orders)
        net = round(net, 2)
        shared_state['pnl']['realized'] += net
        side_label = f"{'Buy' if was_buy else 'Sell'} {'Call' if pos['type'] == 'CE' else 'Put'}"
        part_note = '' if full else f" (partial {qty // lot_size} lot{'s' if qty // lot_size != 1 else ''})"
        shared_state['pnl']['trades_history'].append({'symbol': m['symbol'], 'pnl': net, 'reason': reason + part_note})
        close_info = {'main_price': m_curr, 'hedge_price': h_exit, 'index_price': idx_curr}
        if full:
            lg.csv_manager.log_close(pos['id'], close_info, net)
            if pos in self.positions:
                self.positions.remove(pos)
        else:
            pos['partials'] += 1
            pos['qty'] -= qty
            pos['brk_pending'] = 0
            pid = f"{pos['id']}-P{pos['partials']}"
            chunk = dict(pos, id=pid, qty=qty)
            lg.csv_manager.log_open(pid, chunk)
            lg.csv_manager.log_close(pid, close_info, net)
            mod = getattr(lg.csv_manager, 'log_modify', None)
            if mod:
                mod(pos['id'], pos['qty'], m['entry_price'], h['entry_price'] if h else 0, pos['index_entry_price'])
        dtl = f"Idx: {idx_curr:.2f} | PnL: {net:.0f} | M_Ex: {m_curr:.2f}" + (f" | H_Ex: {h_curr:.2f}" if h else ' | HEDGELESS')
        lg.log_action(f"CLOSED {self.label(pos)} ({reason}){part_note}", dtl)
        lg.play_sound('close')
        lg.add_chart_marker(f"Close {pos['type']}", shared_state['pnl']['realized'])
        lg._last_close_pnl = net
        lg._last_close_side = side_label
        lg._closed_this_tick.append({'side': side_label, 'symbol': m['symbol'], 'pnl': net})
        return True, ('Closed' if full else 'Partial exit'), net

    def close_all(self, reason='Global Exit', save_pnl=True):
        lg = self.logic
        closed = []
        for pos in list(self.positions):
            symbol = pos['main']['symbol']
            side = f"{'Buy' if pos['direction'] == 'BUY' else 'Sell'} {'Call' if pos['type'] == 'CE' else 'Put'}"
            ok, _, _ = self._exit(pos, pos['qty'], reason)
            if ok:
                closed.append({'side': side, 'symbol': symbol, 'pnl': lg._last_close_pnl})
        self._mirror()
        if save_pnl:
            lg.csv_manager.save_daily_report()
            lg.log_action('📊 Daily Report Saved', f"{reason}")
        else:
            lg.log_action('ℹ️ All Positions Closed', f"{reason}")
        return closed

    # ------------------------------------------------------------------ exit orders (per position)
    def set_exit(self, pos_id, kind, val, period='Current', lots=''):
        pos = self.find(pos_id)
        if not pos:
            return False, 'No Position'
        if kind not in EXIT_KINDS:
            return False, 'Unknown exit'
        v = _f(val, 0.0)
        if v <= 0:
            return False, 'Enter a value above 0'
        if kind.startswith('pnl'):
            period, lots = 'Current', ''
        else:
            if period not in PERIODS:
                return False, 'Invalid period'
            if str(lots).strip() != '':
                _, lot_size, _ = self._reg(pos['index'])
                try:
                    n = int(float(lots))
                except (ValueError, TypeError):
                    return False, 'Invalid lots'
                if n < 1 or n * lot_size > pos['qty']:
                    return False, f"Lots must be 1 to {pos['qty'] // lot_size}"
                lots = '' if n * lot_size == pos['qty'] else n
        self._cancel_job(pos, kind)
        pos['exits'][kind].update(val=v, period=period, lots=lots, active=True, state='working')
        return True, f"{kind} set"

    def cancel_exit(self, pos_id, kind):
        pos = self.find(pos_id)
        if not pos:
            return False, 'No Position'
        self._cancel_job(pos, kind)
        pos['exits'][kind].update(val='', period='Current', lots='', active=False, state='off')
        return True, f"{kind} cancelled"

    def _cancel_job(self, pos, kind):
        if self.stop_engine is not None and hasattr(self.stop_engine, 'cancel_position_job'):
            self.stop_engine.cancel_position_job(pos['id'], kind)

    def _clear_slot(self, pos, kind):
        pos['exits'][kind].update(val='', period='Current', lots='', active=False, state='off')

    def _exit_for(self, pos, kind, reason):
        """Full or partial exit driven by an exit slot. A partial hit consumes only that slot."""
        slot = pos['exits'][kind]
        _, lot_size, _ = self._reg(pos['index'])
        lots = slot.get('lots', '')
        qty = pos['qty']
        if str(lots).strip() != '':
            q = int(lots) * lot_size
            qty = min(q, pos['qty'])
        if qty >= pos['qty']:
            return self._exit(pos, pos['qty'], reason)
        self._clear_slot(pos, kind)
        return self._exit(pos, qty, reason)

    def _fire_or_defer_stop(self, pos, kind, slot, is_downside, reason, now):
        eligible = (slot['period'] != 'Current' and params.get('enter_via_stop', True)
                    and self.stop_engine is not None and self.defer_exits)
        if not eligible:
            self._exit_for(pos, kind, reason)
            return
        self.stop_engine.defer_position_stop(pos_id=pos['id'], kind=kind, interval=slot['period'], now=now,
                                             index_name=pos['index'], is_downside=is_downside, reason=reason,
                                             qty=slot.get('lots', ''))
        slot['active'] = False
        slot['state'] = 'deferred'

    def _check_exits(self, now, fires):
        fire_1m, fire_5m, fire_15m = fires['1m'], fires['5m'], fires['15m']

        def due(period):
            return period == 'Current' or (period == '1m' and fire_1m) or (period == '5m' and fire_5m) or (period == '15m' and fire_15m)

        for pos in list(self.positions):
            if pos not in self.positions:
                continue
            ex = pos['exits']
            bearish = self._bearish(pos['type'], pos['direction'])
            # --- PnL exits
            t, s = ex['pnl_tgt'], ex['pnl_stop']
            tv, sv = _f(t['val']), _f(s['val'])
            if t['active'] and tv > 0 and pos['pnl'] >= tv:
                self._exit(pos, pos['qty'], f"Auto Profit {tv:g}")
                continue
            if s['active'] and sv > 0 and pos['pnl'] <= -sv:
                self._exit(pos, pos['qty'], f"Auto Loss {sv:g}")
                continue
            # --- Index exits (position's own index)
            idx_ltp = get_eval_price(pos['index'])
            st, tg = ex['idx_stop'], ex['idx_tgt']
            sv, tv = _f(st['val']), _f(tg['val'])
            if st['active'] and sv > 0 and due(st['period']) and idx_ltp > 0:
                hit = idx_ltp >= sv if bearish else idx_ltp <= sv
                if hit:
                    self._fire_or_defer_stop(pos, 'idx_stop', st, is_downside=not bearish, reason=f"Idx Stop {sv:g}", now=now)
                    if pos not in self.positions:
                        continue
            if tg['active'] and tv > 0 and due(tg['period']) and idx_ltp > 0:
                hit = idx_ltp <= tv if bearish else idx_ltp >= tv
                if hit:
                    self._exit_for(pos, 'idx_tgt', f"Idx Target {tv:g}")
                    if pos not in self.positions:
                        continue
            # --- Premium exits
            main_ltp = shared_state['option_chain'].get(pos['main']['token'], {}).get('ltp', 0)
            if main_ltp <= 0:
                continue
            is_buy = pos['direction'] == 'BUY'
            ps, pt = ex['prem_stop'], ex['prem_tgt']
            psv, ptv = _f(ps['val']), _f(pt['val'])
            if ps['active'] and psv > 0 and due(ps['period']):
                hit = main_ltp <= psv if is_buy else main_ltp >= psv
                if hit:
                    self._fire_or_defer_stop(pos, 'prem_stop', ps, is_downside=is_buy, reason=f"Prem Stop {psv:g}", now=now)
                    if pos not in self.positions:
                        continue
            if pt['active'] and ptv > 0 and due(pt['period']):
                hit = main_ltp >= ptv if is_buy else main_ltp <= ptv
                if hit:
                    self._exit_for(pos, 'prem_tgt', f"Prem Target {ptv:g}")

    # ------------------------------------------------------------------ entries (armed cards)
    def _check_entry(self, card, now, fires):
        opt, direction = CARDS[card]
        buy = direction == 'BUY'
        index = params.get(f'{card}_index', params['trading_index'])
        idx_ltp = get_eval_price(index)
        order_type = params.get(f'{card}_order_type', 'Market')
        fire_on = params.get(f'{card}_fire_on', 'Live')
        trig = _f(params.get(f'{card}_trigger_price', 0))
        off = params.get(f'{card}_strike_offset', 1)
        try:
            lots = int(float(params.get(f'{card}_qty', 4)))
        except (ValueError, TypeError):
            lots = 4
        armed_at = params.get(f'{card}_armed_at')
        after = True
        if armed_at is not None and fire_on != 'Live':
            after = now.replace(second=0, microsecond=0) > armed_at.replace(second=0, microsecond=0)
        timing_ok = {'Live': True, '1m': fires['1m'] and after, '5m': fires['5m'] and after,
                     '15m': fires['15m'] and after, '60m': fires['60m'] and after}.get(fire_on, True)
        should, downside, skip = False, None, None
        reason = f"{order_type} @ {trig} ({fire_on})"
        is_call = opt == 'CE'
        if idx_ltp <= 0:
            skip = 'idx price is 0 (no tick yet)'
        elif order_type == 'Market':
            should, reason = True, 'Market (Immediate)'
        elif not timing_ok:
            skip = f"waiting for {fire_on} candle close"
        else:
            # Sell CE / Buy PE are bearish entries (Stop-Market on a downside cross, Limit on a
            # rise); Sell PE / Buy CE are bullish. Matches the legacy Sell and Buy mode tables.
            bull = (is_call and buy) or ((not is_call) and (not buy))
            if order_type == 'Stop-Market':
                if bull and idx_ltp >= trig: should, downside = True, False
                elif (not bull) and idx_ltp <= trig: should, downside = True, True
            elif order_type == 'Limit':
                if bull and idx_ltp <= trig: should = True
                elif (not bull) and idx_ltp >= trig: should = True
        shared_state['v2_entry_debug'][card] = {'idx_ltp': idx_ltp, 'should_fire': should, 'skip_reason': skip, 'timing_ok': timing_ok}
        if not should:
            return
        if (order_type == 'Stop-Market' and downside is not None and fire_on != 'Live' and params.get('enter_via_stop', True)
                and self.stop_engine is not None and self.defer_entries):
            self.stop_engine.defer_card_entry(card=card, interval=fire_on, now=now, index_name=index, is_downside=downside,
                                              lots=lots, strike_offset=off, new_stop=params.get(f'{card}_new_stop', ''),
                                              new_target=params.get(f'{card}_new_target', ''), reason=reason)
            params[f'{card}_armed'] = False
            params[f'{card}_armed_at'] = None
            return
        self.logic.log_action(f"⚡ TRIGGER FIRED: {card} ({reason})")
        ok, msg = self.open_from_card(card, lots, off, reason=reason, net_ok=bool(params.get(f'{card}_net_ok', False)),
                                      new_stop=params.get(f'{card}_new_stop', ''), new_target=params.get(f'{card}_new_target', ''))
        params[f'{card}_armed'] = False
        params[f'{card}_armed_at'] = None
        params[f'{card}_net_ok'] = False
        if not ok:
            self.logic.log_action(f"⚠️ Trigger Fired but Open Failed: {msg}")
            self.logic.play_sound('error')

    # ------------------------------------------------------------------ per-tick
    def tick(self):
        lg = self.logic
        if not lg.trading_active:
            return
        now = datetime.now()
        lg._closed_this_tick = []
        m = now.minute
        lt = lg.last_trigger_time
        fires = {'1m': lt['1m'] != m, '5m': (m % 5 == 0) and lt['5m'] != m,
                 '15m': (m % 15 == 0) and lt['15m'] != m, '60m': (m == 0) and lt['60m'] != m}
        for card in CARDS:
            if params.get(f'{card}_armed'):
                self._check_entry(card, now, fires)
            else:
                shared_state['v2_entry_debug'][card] = None
        self._check_exits(now, fires)
        self.update_pnl()
        lg._check_global_limits()
        lg._check_global_pnl_floor()
        lg._check_alerts({n: get_eval_price(n) for n in INDICES}, fires['1m'], fires['5m'])
        for k in ('1m', '5m', '15m', '60m'):
            if fires[k]:
                lt[k] = m

    def update_pnl(self):
        lg = self.logic
        unrealized = 0.0
        for pos in self.positions:
            idx_eval = get_eval_price(pos['index'])
            pos['index_current_price'] = idx_eval
            m, h = pos['main'], pos['hedge']
            m_ltp = shared_state['option_chain'].get(m['token'], {}).get('ltp', 0)
            m['current_price'] = m_ltp
            if m['entry_price'] == 0 and m_ltp > 0:
                m['entry_price'] = m_ltp
            is_buy = pos['direction'] == 'BUY'
            qty = pos['qty']
            orders = 1 + max(0, int(pos.get('brk_pending', 1)))
            est = 0.0
            if h:
                h_ltp = shared_state['option_chain'].get(h['token'], {}).get('ltp', 0)
                h['current_price'] = h_ltp
                if h['entry_price'] == 0 and h_ltp > 0:
                    h['entry_price'] = h_ltp
                if not pos['csv_synced'] and m['entry_price'] > 0:
                    lg.csv_manager.update_entry_log(pos['id'], m['entry_price'], h['entry_price'], idx_eval)
                    lg.log_action('📝 Entry Prices Confirmed', f"M: {m['entry_price']:.2f} | H: {h['entry_price']:.2f}")
                    pos['csv_synced'] = True
                m_pnl = (m['entry_price'] - m_ltp) * qty if m_ltp > 0 else 0
                h_pnl = (h_ltp - h['entry_price']) * qty if h_ltp > 0 else 0
                if m_ltp > 0:
                    est += commission(qty, m_ltp, m['entry_price'], orders)
                if h_ltp > 0:
                    est += commission(qty, h['entry_price'], h_ltp, orders)
                pos['pnl'] = m_pnl + h_pnl - est
            else:
                if not pos['csv_synced'] and m['entry_price'] > 0:
                    lg.csv_manager.update_entry_log(pos['id'], m['entry_price'], 0, idx_eval)
                    lg.log_action('📝 Entry Price Confirmed', f"M: {m['entry_price']:.2f}")
                    pos['csv_synced'] = True
                if is_buy:
                    m_pnl = (m_ltp - m['entry_price']) * qty if m_ltp > 0 else 0
                    if m_ltp > 0:
                        est = commission(qty, m['entry_price'], m_ltp, orders)
                else:
                    m_pnl = (m['entry_price'] - m_ltp) * qty if m_ltp > 0 else 0
                    if m_ltp > 0:
                        est = commission(qty, m_ltp, m['entry_price'], orders)
                pos['pnl'] = m_pnl - est
            unrealized += pos['pnl']
        shared_state['pnl']['unrealized'] = unrealized
        self._mirror()
        lg.update_chart_data()

    def live_total(self):
        return shared_state['pnl']['realized'] + sum(p['pnl'] for p in self.positions)

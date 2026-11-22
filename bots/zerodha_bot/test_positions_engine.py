import sys, types, copy
from datetime import datetime, time as dtime

# ---------- stub config ----------
cfg = types.ModuleType('config')
cfg.INDICES = {'NIFTY': {'step': 50, 'lot_size': 65, 'segment': 'NFO'}, 'SENSEX': {'step': 100, 'lot_size': 20, 'segment': 'BFO'}}
cfg.shared_state = {}
cfg.params = {}
def fresh():
    cfg.shared_state.clear(); cfg.params.clear()
    cfg.shared_state.update({'active_trades': {'Call': None, 'Put': None}, 'pnl': {'realized': 0.0, 'unrealized': 0.0, 'trades_history': []},
                        'option_chain': {}, 'sound_queue': [], 'positions': [], 'NIFTY': {'ltp': 25000.0}, 'SENSEX': {'ltp': 82500.0}})
    cfg.params.update({'trading_index': 'SENSEX', 'live_trading': 'Off', 'hedgeless_mode': False, 'enter_via_stop': True,
                  'futures_mode': False})
    for c in ('call', 'put', 'bcall', 'bput'):
        cfg.params.update({f'{c}_armed': False, f'{c}_armed_at': None, f'{c}_order_type': 'Market', f'{c}_fire_on': 'Live',
                           f'{c}_trigger_price': 0, f'{c}_strike_offset': 0, f'{c}_qty': 1, f'{c}_new_stop': '', f'{c}_new_target': '',
                           f'{c}_index': 'SENSEX'})
cfg.get_eval_price = lambda n: cfg.shared_state[n]['ltp']
sys.modules['config'] = cfg
fresh()
import positions_engine as pe

# ---------- fakes ----------
class Kite:
    TRANSACTION_TYPE_BUY = 'BUY'; TRANSACTION_TYPE_SELL = 'SELL'; PRODUCT_MIS = 'MIS'
class Inst:
    kite = Kite()
    def get_atm_token(self, index, strike, opt):
        step = cfg.INDICES[index]['step']; s = round(strike / step) * step
        lo, hi = (80000, 85000) if index == 'SENSEX' else (24000, 26000)
        if not (lo <= s <= hi): return None, f"Strike {s} not found"
        base = 1000000 if index == 'SENSEX' else 2000000
        return base + int(s) + (1 if opt == 'PE' else 0), f"{index}{int(s)}{opt}"
    def get_fill_price(self, token, symbol, segment, qty, is_buy):
        ltp = cfg.shared_state['option_chain'].get(token, {}).get('ltp', 0)
        return (ltp + (0.5 if is_buy else -0.5) if ltp > 0 else 0.0), True
class Csv:
    def __init__(self): self.calls = []
    def log_open(self, i, t): self.calls.append(('open', i, t['qty']))
    def update_entry_log(self, i, m, h, x): self.calls.append(('sync', i))
    def log_close(self, i, info, pnl): self.calls.append(('close', i, pnl))
    def log_modify(self, i, q, m, h, x): self.calls.append(('modify', i, q, round(m, 4), round(h, 4)))
    def save_daily_report(self): self.calls.append(('report',))
class Ticker:
    def subscribe_new(self, t): pass
class Logic:
    def __init__(self):
        self.trading_active = True; self.inst_manager = Inst(); self.ticker = Ticker(); self.csv_manager = Csv()
        self.logs = []; self.orders = []; self.sounds = []; self._closed_this_tick = []
        self.last_trigger_time = {'1m': None, '5m': None, '15m': None, '60m': None, 'chart': None}
        self.eng = None; self.chart = 0
    def log_action(self, m, d=''): self.logs.append(m)
    def play_sound(self, t='alert'): self.sounds.append(t)
    def add_chart_marker(self, t, v): pass
    def update_chart_data(self): self.chart += 1
    def _place_live_order(self, sym, txn, qty, prod, seg): self.orders.append((sym, txn, qty, seg)); return True
    def _check_global_limits(self):
        if cfg.params.get('global_stop_active') and self.eng.live_total() <= -float(cfg.params['global_stop_value']):
            self.eng.close_all('Global Stop', save_pnl=False); cfg.params['global_stop_active'] = False
    def _check_global_pnl_floor(self): pass
    def _check_alerts(self, p, a, b): self.alerts_called = True

def setup(live=False):
    fresh(); cfg.params['live_trading'] = 'On' if live else 'Off'
    lg = Logic(); eng = pe.PositionsEngine(lg); lg.eng = eng; return lg, eng

def chain(tok, ltp): cfg.shared_state['option_chain'][tok] = {'ltp': ltp, 'symbol': str(tok)}
def tok(index, strike, opt): return Inst().get_atm_token(index, strike, opt)[0]

passed = 0
def ok(cond, msg):
    global passed
    assert cond, "FAIL: " + msg
    passed += 1

# legacy references (formulas copied from LogicEngine) -------------------------------------
def legacy_comm(amount, buy_price, sell_price):
    bt, st = amount * buy_price, amount * sell_price; tt = bt + st
    brokerage = 20 + 20
    stt = 0.0015 * st; ex = 0.0003553 * tt; sebi = 0.000001 * tt; gst = 0.18 * (brokerage + ex + sebi)
    return round(brokerage + stt + ex + sebi + gst + 0.00003 * bt + 0.00000005 * tt, 2)

# 1 commission parity ------------------------------------------------------------------------
for q, b, s in [(20, 150.0, 120.5), (65, 80.25, 99.9), (400, 10, 12)]:
    ok(pe.commission(q, b, s, 2) == legacy_comm(q, b, s), f"commission parity {q},{b},{s}")
ok(pe.commission(20, 100, 90, 1) < pe.commission(20, 100, 90, 2), "fewer orders cost less")

# 2 open sell call hedged -----------------------------------------------------------------------
lg, eng = setup()
m, h = tok('SENSEX', 82500, 'CE'), tok('SENSEX', 83500, 'CE'); chain(m, 150.0); chain(h, 40.0)
good, msg = eng.open_from_card('call', 2, 0, reason='t')
p = eng.positions[0]
ok(good and len(eng.positions) == 1, "open ok")
ok(p['qty'] == 40 and p['direction'] == 'SELL' and p['type'] == 'CE', "qty/direction")
ok(p['main']['strike'] == 82500 and p['hedge']['strike'] == 83500, "hedge strike = +10 steps for CE")
ok(abs(p['main']['entry_price'] - 149.5) < 1e-9 and abs(p['hedge']['entry_price'] - 40.5) < 1e-9, "fill prices sell main bid / buy hedge ask")
ok(cfg.shared_state['active_trades']['Call'] is p and cfg.shared_state['active_trades']['Put'] is None, "banner mirror")
ok(('open', p['id'], 40) in lg.csv_manager.calls, "csv log_open")

# 3 buy put hedgeless -----------------------------------------------------------------------------
lg, eng = setup(); cfg.params['bput_index'] = 'NIFTY'
m = tok('NIFTY', 25000, 'PE'); chain(m, 100.0)
good, msg = eng.open_from_card('bput', 3, 0)
p = eng.positions[0]
ok(good and p['hedge'] is None and p['direction'] == 'BUY' and p['qty'] == 195 and p['index'] == 'NIFTY', "buy put hedgeless NIFTY qty by NIFTY lot")
ok(abs(p['main']['entry_price'] - 100.5) < 1e-9, "buy fills at ask")
ok(cfg.shared_state['active_trades']['Put'] is p, "put mirror")

# 4 merge ------------------------------------------------------------------------------------------
lg, eng = setup()
m, h = tok('SENSEX', 82500, 'CE'), tok('SENSEX', 83500, 'CE'); chain(m, 150.0); chain(h, 40.0)
eng.open_from_card('call', 2, 0)
chain(m, 130.0); chain(h, 30.0)
good, msg = eng.open_from_card('call', 1, 0)
p = eng.positions[0]
ok(good and len(eng.positions) == 1 and p['qty'] == 60, "merged into one row, qty 60")
ok(abs(p['main']['entry_price'] - ((40 * 149.5 + 20 * 129.5) / 60)) < 1e-9, "main weighted avg")
ok(abs(p['hedge']['entry_price'] - ((40 * 40.5 + 20 * 30.5) / 60)) < 1e-9, "hedge weighted avg")
ok(p['brk_pending'] == 2 and p['adds'] == 1, "brokerage orders counted")
ok(any(c[0] == 'modify' and c[2] == 60 for c in lg.csv_manager.calls), "csv log_modify on add")

# 5 different strike -> new row ---------------------------------------------------------------------
m2, h2 = tok('SENSEX', 82400, 'CE'), tok('SENSEX', 83400, 'CE'); chain(m2, 160.0); chain(h2, 45.0)
good, msg = eng.open_from_card('call', 1, 1)       # offset 1 = ITM 1 step -> 82400 for a CE
ok(good and len(eng.positions) == 2 and eng.positions[1]['main']['strike'] == 82400, "different strike -> new row")
ok(cfg.shared_state['active_trades']['Call'] is eng.positions[0], "mirror stays on first CE")
# partial exit math with brokerage order counting ------------------------------------------------------
seen = []; orig = pe.commission
pe.commission = lambda q, b, s, orders=2: (seen.append(orders), orig(q, b, s, orders))[1]
p = eng.positions[0]; chain(m, 120.0); chain(h, 25.0)
real0 = cfg.shared_state['pnl']['realized']
good, msg = eng.partial_exit(p['id'], 1)
ok(good and p['qty'] == 40, "partial exit leaves 40")
exp_m = (p["main"]["entry_price"] - 120.5) * 20
exp_h = (25.0 - 0.5 - p['hedge']['entry_price']) * 20
exp = exp_m + exp_h - orig(20, 120.5, p["main"]["entry_price"], 3) - orig(20, 24.5, p['hedge']['entry_price'], 3)
ok(abs((cfg.shared_state['pnl']['realized'] - real0) - round(exp, 2)) < 0.011, "partial realized pnl, entry brokerage charged once (3 orders)")
ok(seen == [3, 3] and p['brk_pending'] == 0, "orders arg: 1 exit + 2 pending entry orders per leg")
seen.clear(); good, msg = eng.close_position(p['id'])
ok(good and seen == [1, 1] and p not in eng.positions, "final exit charges only the exit order")
pe.commission = orig
ok(any(c[0] == 'open' and c[1].endswith('-P1') for c in lg.csv_manager.calls) and any(c[0] == 'close' and c[1].endswith('-P1') for c in lg.csv_manager.calls), "partial csv row -P1")
ok(any(h['reason'].endswith('(partial 1 lot)') for h in cfg.shared_state['pnl']['trades_history']), "history notes partial")
ok(not eng.partial_exit(eng.positions[0]['id'], 5)[0], "partial must be below position size")
ok(not eng.partial_exit(eng.positions[0]['id'], 0)[0], "partial needs lots >= 1")

# 6 netting ----------------------------------------------------------------------------------------------
lg, eng = setup()
m, h = tok('SENSEX', 82500, 'CE'), tok('SENSEX', 83500, 'CE'); chain(m, 150.0); chain(h, 40.0)
eng.open_from_card('call', 6, 0)
pv = eng.preview_card('bcall', 3, 0)
ok(pv['action'] == 'net' and pv['existing'] is eng.positions[0], "preview detects opposite position")
good, msg = eng.open_from_card('bcall', 3, 0)
ok(not good and 'confirm' in msg and eng.positions[0]['qty'] == 120, "unconfirmed offset is refused")
chain(m, 140.0); chain(h, 35.0)
good, msg = eng.open_from_card('bcall', 3, 0, net_ok=True)
ok(good and len(eng.positions) == 1 and eng.positions[0]['qty'] == 60 and eng.positions[0]['direction'] == 'SELL', "offset closes 3 lots, 3 remain")
ok(cfg.shared_state['pnl']['realized'] != 0, "offset realised pnl")
good, msg = eng.open_from_card('bcall', 5, 0, net_ok=True)
ok(good and len(eng.positions) == 1 and eng.positions[0]['direction'] == 'BUY' and eng.positions[0]['qty'] == 40 and eng.positions[0]['hedge'] is None, "remainder opens opposite position (3 closed, 2 new buy)")
ok(cfg.shared_state['active_trades']['Call'] is eng.positions[0], "mirror follows")

# 7 hedge collisions ----------------------------------------------------------------------------------------
lg, eng = setup()
for s in (82500, 83500, 84500): 
    chain(tok('SENSEX', s, 'CE'), 100.0)
eng.open_from_card('call', 1, 0)                         # short 82500, long hedge 83500
good, msg = eng.open_from_card('call', 1, -10)           # main 83500 (short) vs the long hedge
ok(not good and msg.startswith('Blocked'), "new short on an existing hedge strike is blocked: " + msg)
good, msg = eng.open_from_card('bcall', 1, -10)          # long 83500 = same sign as hedge
ok(good, "same-direction overlap with a hedge is allowed: " + msg)
# new hedged short whose hedge lands on an existing short main
lg, eng = setup()
for s in (82500, 83500, 84500): chain(tok('SENSEX', s, 'CE'), 100.0)
cfg.params['hedgeless_mode'] = True; eng.open_from_card('call', 1, -10)   # short 83500, no hedge
cfg.params['hedgeless_mode'] = False
good, msg = eng.open_from_card('call', 1, 0)                              # hedge buys 83500
ok(not good and msg.startswith('Blocked'), "hedge that would cover another short is blocked")

# 8 exits: legacy decision tables vs engine ----------------------------------------------------------------------
def legacy_index_hits(side, buy_mode, idx, stop, tgt):
    if not buy_mode:
        if side == 'Call': return idx >= stop, idx <= tgt
        return idx <= stop, idx >= tgt
    if side == 'Call': return idx <= stop, idx >= tgt
    return idx >= stop, idx <= tgt
def legacy_prem_hits(is_buy, ltp, stop, tgt):
    return (ltp <= stop, ltp >= tgt) if is_buy else (ltp >= stop, ltp <= tgt)
combos = [('call', 'CE', 'SELL', 'Call', False), ('put', 'PE', 'SELL', 'Put', False), ('bcall', 'CE', 'BUY', 'Call', True), ('bput', 'PE', 'BUY', 'Put', True)]
checked = 0
for card, opt, dr, side, bm in combos:
    for idx in (82300, 82480, 82500, 82520, 82700):
        for prem in (80.0, 100.0, 120.0):
            lg, eng = setup(); cfg.params['hedgeless_mode'] = True
            cfg.shared_state['SENSEX']['ltp'] = 82500.0
            m = tok('SENSEX', 82500, opt); chain(m, 100.0)
            eng.open_from_card(card, 1, 0); pos = eng.positions[0]
            cfg.shared_state['SENSEX']['ltp'] = float(idx); chain(m, prem)
            exp_stop, exp_tgt = legacy_index_hits(side, bm, idx, 82500, 82500)
            eng.set_exit(pos['id'], 'idx_stop', 82500, 'Current'); eng.set_exit(pos['id'], 'idx_tgt', 82500, 'Current')
            eng.update_pnl(); eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
            closed = pos not in eng.positions
            ok(closed == (exp_stop or exp_tgt), f"index exit parity {card} idx={idx}")
            reason = ' '.join(lg.logs) if closed else ''
            if closed and exp_stop and exp_tgt: pass
            elif closed: ok(('Idx Stop' in reason) == exp_stop, f"index stop-vs-target label {card} idx={idx}")
            checked += 1
            lg, eng = setup(); cfg.params['hedgeless_mode'] = True
            chain(m, 100.0); cfg.shared_state['SENSEX']['ltp'] = 82500.0
            eng.open_from_card(card, 1, 0); pos = eng.positions[0]; chain(m, prem)
            es, et = legacy_prem_hits(dr == 'BUY', prem, 100.0, 100.0)
            eng.set_exit(pos['id'], 'prem_stop', 100.0); eng.set_exit(pos['id'], 'prem_tgt', 100.0)
            eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
            ok((pos not in eng.positions) == (es or et), f"premium exit parity {card} prem={prem}")
            checked += 1
print("exit decision-table comparisons:", checked)

# 9 periods ----------------------------------------------------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
m = tok('SENSEX', 82500, 'CE'); chain(m, 100.0); eng.open_from_card('call', 1, 0); pos = eng.positions[0]
cfg.shared_state['SENSEX']['ltp'] = 82600.0
for per, key in (('1m', '1m'), ('5m', '5m'), ('15m', '15m')):
    eng.set_exit(pos['id'], 'idx_stop', 82550, per)
    none = {'1m': False, '5m': False, '15m': False, '60m': False}
    eng._check_exits(datetime.now(), none); ok(pos in eng.positions, f"{per} stop idle off-boundary")
    other = dict(none); other['1m' if key != '1m' else '5m'] = True
    eng._check_exits(datetime.now(), other); ok(pos in eng.positions, f"{per} stop ignores other boundaries")
fires = {'1m': False, '5m': False, '15m': True, '60m': False}
eng._check_exits(datetime.now(), fires); ok(pos not in eng.positions, "15m stop fires on the 15m boundary")

# 10 partial exit orders + slot consumption ----------------------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
m = tok('SENSEX', 82500, 'CE'); chain(m, 100.0); eng.open_from_card('call', 6, 0); pos = eng.positions[0]
ok(eng.set_exit(pos['id'], 'prem_tgt', 80, 'Current', 2)[0], "set partial prem target 2 lots")
ok(eng.set_exit(pos['id'], 'prem_stop', 130, 'Current', 6)[0] and pos['exits']['prem_stop']['lots'] == '', "lots == full position stored as blank")
ok(not eng.set_exit(pos['id'], 'prem_tgt', 80, 'Current', 7)[0], "lots above position rejected")
ok(not eng.set_exit(pos['id'], 'prem_tgt', 0)[0] and not eng.set_exit(pos['id'], 'prem_tgt', 80, '3m')[0], "value/period validated")
chain(m, 79.0); eng.update_pnl(); eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
ok(pos in eng.positions and pos['qty'] == 80 and pos['exits']['prem_tgt']['active'] is False and pos['exits']['prem_tgt']['val'] == '', "partial target consumed its slot only")
ok(pos['exits']['prem_stop']['active'] is True, "other exits untouched")
ok(any(h['reason'].endswith('(partial 2 lots)') for h in cfg.shared_state['pnl']['trades_history']), "partial logged")
chain(m, 131.0); eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
ok(pos not in eng.positions, "full-size stop closes the remainder")
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(m, 100.0); eng.open_from_card('call', 6, 0); pos = eng.positions[0]
eng.set_exit(pos['id'], 'prem_tgt', 80, 'Current', 2); eng.cancel_exit(pos['id'], 'prem_tgt')
ok(pos['exits']['prem_tgt']['state'] == 'off', "cancel clears the slot")

# 11 pnl exits + entry stop/target fields ------------------------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(m, 100.0); eng.open_from_card('call', 1, 0, new_stop='500', new_target='300'); pos = eng.positions[0]
ok(pos['exits']['pnl_stop']['val'] == 500.0 and pos['exits']['pnl_tgt']['active'], "card stop/target become pnl exits")
chain(m, 70.0); eng.update_pnl(); eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
ok(pos not in eng.positions and any('Auto Profit' in l for l in lg.logs), "pnl target closes")

# 12 two indices at once -------------------------------------------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True; cfg.params['bcall_index'] = 'NIFTY'
a = tok('SENSEX', 82500, 'CE'); b = tok('NIFTY', 25000, 'CE'); chain(a, 100.0); chain(b, 90.0)
eng.open_from_card('call', 1, 0); eng.open_from_card('bcall', 1, 0)
ok(len(eng.positions) == 2 and {p['index'] for p in eng.positions} == {'NIFTY', 'SENSEX'}, "NIFTY and SENSEX at once")
nif = [p for p in eng.positions if p['index'] == 'NIFTY'][0]; sen = [p for p in eng.positions if p['index'] == 'SENSEX'][0]
eng.set_exit(nif['id'], 'idx_tgt', 25100, 'Current'); eng.set_exit(sen['id'], 'idx_stop', 82600, 'Current')
cfg.shared_state['NIFTY']['ltp'] = 25150.0; cfg.shared_state['SENSEX']['ltp'] = 82500.0
eng._check_exits(datetime.now(), {'1m': False, '5m': False, '15m': False, '60m': False})
ok(nif not in eng.positions and sen in eng.positions, "each position evaluated on its own index")

# 13 update_pnl vs legacy formula --------------------------------------------------------------------------------------
lg, eng = setup()
m, h = tok('SENSEX', 82500, 'CE'), tok('SENSEX', 83500, 'CE'); chain(m, 150.0); chain(h, 40.0)
eng.open_from_card('call', 2, 0); pos = eng.positions[0]; chain(m, 130.0); chain(h, 35.0)
eng.update_pnl()
ref = (pos['main']['entry_price'] - 130.0) * 40 + (35.0 - pos['hedge']['entry_price']) * 40 \
      - legacy_comm(40, 130.0, pos['main']['entry_price']) - legacy_comm(40, pos['hedge']['entry_price'], 35.0)
ok(abs(pos['pnl'] - ref) < 0.011, f"hedged live pnl matches legacy ({pos['pnl']:.2f} vs {ref:.2f})")
ok(abs(cfg.shared_state['pnl']['unrealized'] - pos['pnl']) < 1e-9 and eng.live_total() == pos['pnl'], "unrealized/live total")
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(m, 150.0); eng.open_from_card('bcall', 2, 0); pos = eng.positions[0]; chain(m, 170.0); eng.update_pnl()
ref = (170.0 - pos['main']['entry_price']) * 40 - legacy_comm(40, pos['main']['entry_price'], 170.0)
ok(abs(pos['pnl'] - ref) < 0.011, "buy hedgeless live pnl matches legacy")

# 14 tick(): armed entries, global stop over several positions -------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
m = tok('SENSEX', 82500, 'CE'); chain(m, 100.0)
cfg.params.update(call_armed=True, call_order_type='Stop-Market', call_fire_on='Live', call_trigger_price=82400.0)
eng.tick(); ok(not eng.positions and cfg.params['call_armed'], "stop-market waits while idx above trigger")
cfg.shared_state['SENSEX']['ltp'] = 82390.0; chain(tok('SENSEX', 82400, 'CE'), 100.0)
eng.tick(); ok(len(eng.positions) == 1 and not cfg.params['call_armed'], "stop-market fires on a downside cross and disarms")
ok(getattr(lg, 'alerts_called', False), "alerts evaluated each tick")
cfg.params.update(bput_armed=True, bput_order_type='Limit', bput_fire_on='Live', bput_trigger_price=82350.0, bput_index='SENSEX', bput_strike_offset=0)
chain(tok('SENSEX', 82400, 'PE'), 90.0); eng.tick()
ok(len(eng.positions) == 2 and any(p['direction'] == 'BUY' and p['type'] == 'PE' for p in eng.positions), "buy put limit fires when idx >= trigger")
cfg.params.update(global_stop_active=True, global_stop_value=50); 
for p in eng.positions: p['pnl'] = -40.0
eng.tick()
ok(not eng.positions and cfg.shared_state['active_trades']['Call'] is None and cfg.shared_state['active_trades']['Put'] is None, "global stop closes every position and clears the mirror")
# failed open disarms and plays error
lg, eng = setup(); cfg.params.update(call_armed=True, call_order_type='Market', call_strike_offset=-100)
cfg.params['hedgeless_mode'] = True; eng.tick()
ok(not cfg.params['call_armed'] and 'error' in lg.sounds, "failed trigger disarms and sounds error")
# net_ok consumed
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(tok('SENSEX', 82500, 'CE'), 100.0); eng.open_from_card('call', 2, 0)
cfg.params.update(bcall_armed=True, bcall_order_type='Market', bcall_net_ok=True, bcall_qty=1, bcall_strike_offset=0); eng.tick()
ok(eng.positions[0]['qty'] == 20 and cfg.params['bcall_net_ok'] is False, "armed offset order nets and consumes the confirmation")
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(tok('SENSEX', 82500, 'CE'), 100.0); eng.open_from_card('call', 2, 0)
cfg.params.update(bcall_armed=True, bcall_order_type='Market', bcall_net_ok=False, bcall_qty=1, bcall_strike_offset=0); eng.tick()
ok(eng.positions[0]['qty'] == 40 and 'error' in lg.sounds, "armed order without confirmation is blocked, position untouched")

# 15 deferral hooks ---------------------------------------------------------------------------------------------------------
class StopEng:
    def __init__(self): self.calls = []
    def defer_position_stop(self, **kw): self.calls.append(('stop', kw))
    def defer_card_entry(self, **kw): self.calls.append(('entry', kw))
lg, eng = setup(); cfg.params['hedgeless_mode'] = True; se = StopEng(); eng.stop_engine = se
m = tok('SENSEX', 82500, 'CE'); chain(m, 100.0); eng.open_from_card('call', 1, 0); pos = eng.positions[0]
eng.set_exit(pos['id'], 'idx_stop', 82550, '5m'); cfg.shared_state['SENSEX']['ltp'] = 82600.0
eng._check_exits(datetime.now(), {'1m': False, '5m': True, '15m': False, '60m': False})
ok(pos not in eng.positions and not se.calls, "without deferral support a candle stop closes immediately (same as Enter via Stop off)")
lg, eng = setup(); cfg.params['hedgeless_mode'] = True; se = StopEng(); eng.stop_engine = se
chain(m, 100.0); cfg.shared_state['SENSEX']['ltp'] = 82500.0; eng.open_from_card('call', 1, 0); pos = eng.positions[0]
eng.set_exit(pos['id'], 'idx_stop', 82550, '5m'); cfg.shared_state['SENSEX']['ltp'] = 82600.0
eng.defer_exits = True
eng._check_exits(datetime.now(), {'1m': False, '5m': True, '15m': False, '60m': False})
ok(pos in eng.positions and se.calls and se.calls[0][1]['is_downside'] is False and pos['exits']['idx_stop']['state'] == 'deferred' and not pos['exits']['idx_stop']['active'], "deferred stop hands off with direction")
eng.defer_entries = True
cfg.params.update(bput_armed=True, bput_order_type='Stop-Market', bput_fire_on='5m', bput_trigger_price=82700.0, bput_armed_at=None)
cfg.shared_state['SENSEX']['ltp'] = 82650.0
eng._check_entry('bput', datetime.now(), {'1m': False, '5m': True, '15m': False, '60m': False})
ok(se.calls[-1][0] == 'entry' and se.calls[-1][1]['is_downside'] is True and not cfg.params['bput_armed'], "buy put stop-market on 5m defers as a downside breakout")

# 16 close_all + csv ---------------------------------------------------------------------------------------------------------------
lg, eng = setup(); cfg.params['hedgeless_mode'] = True
chain(tok('SENSEX', 82500, 'CE'), 100.0); chain(tok('SENSEX', 82500, 'PE'), 100.0)
eng.open_from_card('call', 1, 0); eng.open_from_card('put', 1, 0)
res = eng.close_all('Day End Auto-Square', save_pnl=True)
ok(len(res) == 2 and not eng.positions and ('report',) in lg.csv_manager.calls, "close_all closes everything and saves the daily report once")
ok(res[0]['side'] in ('Sell Call', 'Sell Put') and 'pnl' in res[0], "close_all breakdown shape")
# live orders sequence -----------------------------------------------------------------------------------------------------------
lg, eng = setup(live=True); import time as _t; _t.sleep = lambda s: None
m, h = tok('SENSEX', 82500, 'CE'), tok('SENSEX', 83500, 'CE'); chain(m, 150.0); chain(h, 40.0)
eng.open_from_card('call', 1, 0)
ok([o[1] for o in lg.orders] == ['BUY', 'SELL'] and lg.orders[0][0].endswith('83500CE'), "live open: hedge BUY then main SELL")
lg.orders.clear(); eng.partial_exit(eng.positions[0]['id'], 1) if eng.positions[0]['qty'] > 20 else None
eng.open_from_card('call', 1, 0); lg.orders.clear(); eng.partial_exit(eng.positions[0]['id'], 1)
ok([o[1] for o in lg.orders] == ['BUY', 'SELL'] and lg.orders[0][0].endswith('82500CE'), "live partial exit: cover main BUY then sell hedge")
ok(all(o[3] == 'BFO' for o in lg.orders), "segment comes from the position's index")
print(f"ALL {passed} CHECKS PASSED")

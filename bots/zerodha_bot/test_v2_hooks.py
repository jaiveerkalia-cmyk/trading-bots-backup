# Checks the legacy-entry-point shims of PositionsEngine (open_for_side / close_side) on top of the main suite.
# Run AFTER patches/v2_stage2c.patch has been applied: python3 test_v2_hooks.py
import test_positions_engine as t
cfg, pe = t.cfg, t.pe

lg, eng = t.setup(); cfg.params['hedgeless_mode'] = True; cfg.params['lots'] = 2
m = t.tok('SENSEX', 82500, 'CE'); t.chain(m, 100.0)
ok, msg = eng.open_for_side('Call', reason='legacy card', strike_offset=0)
t.ok(ok and len(eng.positions) == 1 and eng.positions[0]['qty'] == 40 and eng.positions[0]['direction'] == 'SELL', "legacy Call card opens a v2 SELL CE with params['lots']")
ok, msg = eng.open_for_side('Call', reason='legacy card', qty_override=1, strike_offset=0)
t.ok(ok and len(eng.positions) == 1 and eng.positions[0]['qty'] == 60, "legacy second open at the same strike merges (qty_override honoured)")
t.chain(t.tok('SENSEX', 82400, 'CE'), 120.0)
ok, msg = eng.open_for_side('Call', reason='legacy card', qty_override=1, strike_offset=1)
t.ok(ok and len(eng.positions) == 2, "legacy open at another strike becomes a new row")
t.chain(t.tok('SENSEX', 82500, 'PE'), 90.0)
ok, msg = eng.open_for_side('Put', manual_strike=82500, qty_override=1)   # 82500 >= 1000 is an explicit strike
t.ok(ok and eng.positions[-1]['type'] == 'PE' and eng.positions[-1]['main']['strike'] == 82500, "manual_strike >= 1000 is an explicit strike")
ok, msg = eng.close_side('Call', 'legacy close')
t.ok(ok and len(eng.positions) == 3 - 1 and not any(p['type'] == 'CE' and p['main']['strike'] == 82500 for p in eng.positions), "close_side closes the mirrored (first) Call position only")
t.ok(cfg.shared_state['active_trades']['Call'] is not None and cfg.shared_state['active_trades']['Call']['main']['strike'] == 82400, "mirror advances to the next Call position")
ok, msg = eng.close_side('Call', 'x'); ok2, msg2 = eng.close_side('Call', 'x')
t.ok(ok and not ok2 and msg2 == 'No Position', "close_side with nothing open reports No Position")
cfg.params['options_buy_mode'] = True
ok, msg = eng.open_for_side('Put', qty_override=1, strike_offset=0)
t.ok(not ok and 'Buy cards' in msg, "global Buy Mode toggle is refused under V2")
print("HOOK SHIM CHECKS PASSED")

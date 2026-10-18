"""
verify_lean.py -- proves lean_io.py (stdlib) == original pandas code, byte for byte.

Run from the project dir:   python3 verify_lean.py        (inside the container or on the host,
anywhere pandas is installed). It NEVER touches live files: everything runs on temp copies;
real options_tradebook.csv / final_daily_pnl.csv / instruments_master.csv are only READ.
Prints PASS/FAIL per check and exits non-zero on any FAIL.
"""
import copy
import csv
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.pop('ZB_NO_PANDAS', None)          # import the ORIGINAL pandas classes

import config
import csv_manager as cm
import instrument_manager as im
import lean_io
import pandas as pd

FAILS = []


def check(name, ok, detail=''):
    print(('PASS  ' if ok else 'FAIL  ') + name + ((' -- ' + detail) if (detail and not ok) else ''))
    if not ok:
        FAILS.append(name)


# ---- frozen clock so old/lean runs write identical timestamps ----
class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 19, 11, 22, 33)


cm.datetime = FakeDT
lean_io.datetime = FakeDT


def set_paths(d):
    tb, pnl = os.path.join(d, 'options_tradebook.csv'), os.path.join(d, 'final_daily_pnl.csv')
    master = os.path.join(d, 'instruments_master.csv')
    for mod in (cm, lean_io):
        mod.TRADEBOOK_FILE, mod.DAILY_PNL_FILE = tb, pnl
    for mod in (im, lean_io):
        mod.MASTER_INSTRUMENTS_FILE = master
    config.INDICES['NIFTY']['opt_file'] = os.path.join(d, 'nifty_options.csv')
    config.INDICES['SENSEX']['opt_file'] = os.path.join(d, 'sensex_options.csv')


def read_bytes(p):
    return open(p, 'rb').read() if os.path.exists(p) else None


class LeanCM:
    def __init__(self):
        self.init_files()


for _n, _f in [('init_files', lean_io.csv_init_files), ('log_open', lean_io.csv_log_open),
               ('update_entry_log', lean_io.csv_update_entry_log), ('log_close', lean_io.csv_log_close),
               ('save_daily_report', lean_io.csv_save_daily_report)]:
    setattr(LeanCM, _n, _f)


class LeanIM(im.InstrumentManager):
    fetch_and_process_instruments = lean_io.inst_fetch_and_process_instruments
    get_atm_token = lean_io.inst_get_atm_token
    get_near_month_future = lean_io.inst_get_near_month_future


# ---------------------------------------------------------------- CSV manager
def trade(typ='CE', qty=240, strike=83500, entry=373.6, hedge=True, direction='SELL', idx=83658.96):
    return {'type': typ, 'qty': qty, 'direction': direction, 'index_entry_price': idx,
            'main': {'strike': strike, 'entry_price': entry},
            'hedge': {'strike': strike + 1000, 'entry_price': 39.55} if hedge else None}


def csv_sequence(mgr_cls, d, tb_src, pnl_src):
    set_paths(d)
    if tb_src:
        shutil.copy(tb_src, os.path.join(d, 'options_tradebook.csv'))
    if pnl_src:
        shutil.copy(pnl_src, os.path.join(d, 'final_daily_pnl.csv'))
    config.params['trading_index'] = 'SENSEX'
    config.shared_state['current_expiry']['SENSEX'] = '2026-10-22'
    m = mgr_cls()
    snaps = [('init', read_bytes(os.path.join(d, 'options_tradebook.csv')),
              read_bytes(os.path.join(d, 'final_daily_pnl.csv')))]

    def snap(label):
        snaps.append((label, read_bytes(os.path.join(d, 'options_tradebook.csv')),
                      read_bytes(os.path.join(d, 'final_daily_pnl.csv'))))

    m.log_open('aaaa1111', trade()); snap('open1')
    m.log_open('bbbb2222', trade('PE', 120, 83200, 378.75, hedge=False, direction='BUY')); snap('open2')
    m.update_entry_log('aaaa1111', 371.2, 40.1, 83650.5); snap('update')
    m.update_entry_log('nope', 1, 2, 3); snap('update-missing')
    m.log_close('aaaa1111', {'main_price': 235.3, 'hedge_price': 25.55, 'index_price': 83425.18}, 29569.54); snap('close1')
    m.log_close('bbbb2222', {'main_price': 100, 'hedge_price': 0, 'index_price': 83000}, -50); snap('close2-ints')
    m.log_close('nope', {'main_price': 1, 'hedge_price': 1, 'index_price': 1}, 1); snap('close-missing')
    m.log_open('12345678', trade('CE', 65, 24500, 100.0)); snap('open-numeric-id')
    m.log_close('12345678', {'main_price': 90.5, 'hedge_price': 5, 'index_price': 24400.1}, 1234.5); snap('close-numeric-id')
    config.shared_state['pnl']['trades_history'] = []
    m.save_daily_report(); snap('report-empty')
    config.shared_state['pnl']['trades_history'] = [{'pnl': -20781.87}, {'pnl': -10816.55}]
    m.save_daily_report(); snap('report-2')
    config.shared_state['pnl']['trades_history'] = [{'pnl': 100.0}]
    m.save_daily_report(); snap('report-1')
    return snaps


def compare_csv(label, tb_src=None, pnl_src=None):
    da, db = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        a = csv_sequence(cm.CsvManager, da, tb_src, pnl_src)
        b = csv_sequence(LeanCM, db, tb_src, pnl_src)
        bad = [x[0] for x, y in zip(a, b) if x[1:] != y[1:]]
        check(f'csv {label}: {len(a)} steps byte-identical', not bad, 'differs at: ' + ', '.join(bad))
        if bad:
            i = [x[0] for x in a].index(bad[0])
            for k, nm in ((1, 'tradebook'), (2, 'daily_pnl')):
                if a[i][k] != b[i][k]:
                    print(f'--- first diff in {nm} at step {bad[0]}\n--- pandas:\n{(a[i][k] or b"").decode()[-600:]}\n--- lean:\n{(b[i][k] or b"").decode()[-600:]}')
    finally:
        shutil.rmtree(da, ignore_errors=True)
        shutil.rmtree(db, ignore_errors=True)


def make_legacy_files(d):
    tb = os.path.join(d, 'tb.csv')
    with open(tb, 'w', newline='') as f:
        f.write('Trade_ID,Date,Index_Type,Expiry,Type,Qty,Main_Strike,Hedge_Strike,Open_Main_Price,Open_Hedge_Price,Open_Index_Price,Open_Time,Close_Main_Price,Close_Hedge_Price,Close_Index_Price,Close_Time,Profit,Status,Direction\n'
                '9db8f90f,2026-01-13,SENSEX,2026-01-14,CE,240,83500.0,84500.0,373.6,39.55,83658.96,12:11:36,235.3,25.55,83425.18,12:56:02,29569.54,CLOSED,\n'
                'ad531337,2026-01-14,NIFTY,2026-01-20,PE,650,25800.0,25300.0,146.65,21.3,25738.95,11:16:02,178.95,22.1,25685.75,13:37:50,-20781.87,CLOSED,\n'
                '7427c537,2026-01-19,SENSEX,2026-01-22,PE,240,83200.0,82200.0,378.75,87.9,83130.3,12:56:44,345.05,84.05,83235.5,15:19:44,6865.44,CLOSED,SELL\n')
    pnl = os.path.join(d, 'pnl.csv')
    with open(pnl, 'w', newline='') as f:
        f.write('Date,Net_Profit,Trade_Num,Trades_List\n'
                '2026-01-13 13:09:10,29569.54,1,[29569.54]\n'
                '2026-01-14 14:45:02,-31598.42,2,"[-20781.87, -10816.55]"\n'
                '2026-02-18 18:52:04,0.0,0,[]\n')
    return tb, pnl


# ---------------------------------------------------------------- instruments
def kite_cast(row):
    row = dict(row)
    row['instrument_token'] = int(row['instrument_token'])
    row['last_price'] = float(row['last_price'])
    row['strike'] = float(row['strike'])
    row['tick_size'] = float(row['tick_size'])
    row['lot_size'] = int(row['lot_size'])
    if len(row['expiry']) == 10:
        row['expiry'] = date.fromisoformat(row['expiry'])
    return row


def synthetic_instruments():
    rnd = random.Random(7)
    today = date.today()
    out, tok = [], 1000000

    def add(exchange, name, sym, itype, expiry, strike, lot, seg, last=0.0, tick=0.05):
        nonlocal tok
        tok += rnd.randint(1, 900)
        out.append({'instrument_token': tok, 'exchange_token': str(tok // 256), 'tradingsymbol': sym,
                    'name': name, 'last_price': last, 'expiry': expiry, 'strike': float(strike),
                    'tick_size': tick, 'lot_size': lot, 'instrument_type': itype, 'segment': seg,
                    'exchange': exchange})
    add('NSE', 'NIFTY 50', 'NIFTY 50', 'EQ', '', 0, 1, 'INDICES', tick=0.0)
    add('BSE', 'SENSEX', 'SENSEX', 'EQ', '', 0, 1, 'INDICES', tick=0.0)
    for idx, exch, step, lot, lo, hi in (('NIFTY', 'NFO', 50, 65, 22000, 24000), ('SENSEX', 'BFO', 100, 20, 80000, 83000)):
        for off, lt in ((-3, lot), (2, lot), (9, lot + 10)):
            ex = today + timedelta(days=off)
            for s in range(lo, hi + 1, step):
                for t in ('CE', 'PE'):
                    add(exch, idx, f'{idx}{ex:%y%m%d}{s}{t}', t, ex, s, lt, exch + '-OPT', last=round(rnd.random() * 300, 2))
            add(exch, idx, f'{idx}{ex:%y%m}FUT', 'FUT', ex, 0, lt, exch + '-FUT', last=round(rnd.random() * 30000, 2), tick=0.1)
        add(exch, idx, f'{idx}ODD', 'CE', today + timedelta(days=2), hi + 250, lot, exch + '-OPT')  # uneven gap
    add('NFO', 'BANKNIFTY', 'BANKNIFTY26OCT50000CE', 'CE', today + timedelta(days=2), 50000, 30, 'NFO-OPT')
    add('MCX', 'A, B "C"', 'WEIRD,SYM', 'FUT', today + timedelta(days=5), 0, 1, 'MCX-FUT')
    add('NSE', 'RELIANCE', 'RELIANCE', 'EQ', '', 0, 1, 'NSE')
    rnd.shuffle(out)
    return out


class FakeKite:
    def __init__(self, data):
        self.data = data

    def instruments(self, exchange=None):
        return copy.deepcopy(self.data)


def scan_and_probe(mgr_cls, d, instruments):
    set_paths(d)
    saved = copy.deepcopy(config.INDICES)
    config.shared_state['current_expiry'] = {'NIFTY': None, 'SENSEX': None}
    m = mgr_cls(FakeKite(instruments))
    ok = m.fetch_and_process_instruments()
    meta = {k: {x: v[x] for x in ('token', 'lot_size', 'step')} for k, v in config.INDICES.items()}
    meta = json.dumps(meta, sort_keys=True, default=float) + json.dumps(config.shared_state['current_expiry'])
    probes = []
    for idx, spot in (('NIFTY', 23010), ('NIFTY', 22000), ('NIFTY', 99999), ('SENSEX', 81234.5), ('SENSEX', 80000)):
        for t in ('CE', 'PE', 'XX'):
            probes.append(m.get_atm_token(idx, spot, t))
    for idx in ('NIFTY', 'SENSEX', 'BOGUS'):
        probes.append(m.get_near_month_future(idx))
    files = {n: read_bytes(os.path.join(d, n)) for n in ('instruments_master.csv', 'nifty_options.csv', 'sensex_options.csv')}
    config.INDICES.clear()
    config.INDICES.update(saved)
    return ok, meta, probes, files


def compare_instruments(label, instruments):
    da, db = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        a = scan_and_probe(im.InstrumentManager, da, instruments)
        b = scan_and_probe(LeanIM, db, instruments)
        check(f'{label}: scan result + INDICES metadata + expiry', a[0] == b[0] and a[1] == b[1], f'{a[1]} vs {b[1]}')
        check(f'{label}: get_atm_token / get_near_month_future ({len(a[2])} probes)', a[2] == b[2],
              str([(x, y) for x, y in zip(a[2], b[2]) if x != y][:3]))
        for n in a[3]:
            check(f'{label}: {n} byte-identical ({len(a[3][n] or b"")} bytes)', a[3][n] == b[3][n])
        # futures fallback path (cache empty -> streams the master CSV instead)
        lean_io._FUT.clear()
        set_paths(db)
        ref = [im.InstrumentManager(None).get_near_month_future(i) for i in ('NIFTY', 'SENSEX')]
        got = [LeanIM(None).get_near_month_future(i) for i in ('NIFTY', 'SENSEX')]
        check(f'{label}: near-month future via master-CSV fallback', ref == got, f'{ref} vs {got}')
    finally:
        shutil.rmtree(da, ignore_errors=True)
        shutil.rmtree(db, ignore_errors=True)


# ---------------------------------------------------------------- history table
def compare_history(path, label):
    df = pd.read_csv(path).iloc[::-1]
    cols = [{'name': c, 'label': c.replace('_', ' '), 'field': c, 'sortable': True, 'align': 'left'} for c in df.columns]
    rows = [{k: (None if (isinstance(v, float) and v != v) else v) for k, v in r.items()} for r in df.to_dict('records')]
    lc, lr = lean_io.history_table_data(path)
    check(f'history {label}: columns', cols == lc)
    same = len(rows) == len(lr) and all(json.dumps(x, sort_keys=True, default=str) == json.dumps(y, sort_keys=True, default=str) for x, y in zip(rows, lr))
    check(f'history {label}: {len(rows)} rows identical (values + types)', same)


if __name__ == '__main__':
    tmp = tempfile.mkdtemp()
    tb, pnl = make_legacy_files(tmp)
    compare_csv('fresh (no files)')
    compare_csv('legacy sample', tb, pnl)
    real_tb, real_pnl = os.path.join(HERE, 'options_tradebook.csv'), os.path.join(HERE, 'final_daily_pnl.csv')
    if os.path.exists(real_tb) and os.path.exists(real_pnl):
        compare_csv('REAL files (copies)', real_tb, real_pnl)
        compare_history(real_tb, 'real tradebook')
    compare_history(tb, 'legacy sample')
    compare_instruments('synthetic instruments', synthetic_instruments())
    real_master = os.path.join(HERE, 'instruments_master.csv')
    if os.path.exists(real_master):
        with open(real_master, newline='') as f:
            compare_instruments('REAL master (read-only)', [kite_cast(r) for r in csv.DictReader(f)])
    shutil.rmtree(tmp, ignore_errors=True)
    print('\nALL PASS' if not FAILS else f'\n{len(FAILS)} FAILED: ' + '; '.join(FAILS))
    sys.exit(1 if FAILS else 0)

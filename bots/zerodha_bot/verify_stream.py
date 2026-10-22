"""
verify_stream.py -- proves the ZB_STREAM_SCAN path (lean_io streaming scan) produces the same files,
INDICES metadata, expiry, ATM tokens and futures as the ORIGINAL pandas scan, and that it falls back
cleanly. Run after verify_lean.py:   python3 verify_stream.py      (needs pandas; touches only temp dirs;
real instruments_master.csv is only READ, and also used as the raw payload for a real-data run).
"""
import copy
import csv
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date

import verify_lean as V
from verify_lean import (check, set_paths, read_bytes, config, im, lean_io, LeanIM,
                         synthetic_instruments, kite_cast, HERE)


class Kite:
    def __init__(self, data, raw=None, allow_list=True, raw_fails=False):
        self.data, self.raw, self.allow_list, self.raw_fails = data, raw, allow_list, raw_fails

    def instruments(self, exchange=None):
        if not self.allow_list:
            raise AssertionError('list path used although streaming was expected')
        return copy.deepcopy(self.data)

    def _get(self, route, **kw):
        if self.raw_fails:
            raise AttributeError('raw api unavailable')
        assert route == 'market.instruments.all'
        return self.raw


def raw_from(instruments):
    """Raw-CSV payload in Zerodha style ('0' / '24500' for whole-number floats, ISO dates)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    names = list(instruments[0].keys())
    w.writerow(names)
    for d in instruments:
        row = []
        for k in names:
            v = d[k]
            if isinstance(v, float) and k in ('last_price', 'strike') and v == int(v):
                row.append(str(int(v)))
            elif isinstance(v, date):
                row.append(v.isoformat())
            else:
                row.append(str(v))
        w.writerow(row)
    return buf.getvalue().encode('utf-8')


def run(mgr_cls, d, kite, stream):
    set_paths(d)
    saved = copy.deepcopy(config.INDICES)
    config.shared_state['current_expiry'] = {'NIFTY': None, 'SENSEX': None}
    if stream:
        os.environ['ZB_STREAM_SCAN'] = '1'
    else:
        os.environ.pop('ZB_STREAM_SCAN', None)
    try:
        m = mgr_cls(kite)
        ok = m.fetch_and_process_instruments()
    finally:
        os.environ.pop('ZB_STREAM_SCAN', None)
    meta = {k: {x: v[x] for x in ('token', 'lot_size', 'step')} for k, v in config.INDICES.items()}
    meta = json.dumps(meta, sort_keys=True, default=float) + json.dumps(config.shared_state['current_expiry'])
    probes = [m.get_atm_token(i, s, t) for i, s in (('NIFTY', 23010), ('NIFTY', 22000), ('SENSEX', 81234.5), ('SENSEX', 80000))
              for t in ('CE', 'PE')]
    probes += [m.get_near_month_future(i) for i in ('NIFTY', 'SENSEX', 'BOGUS')]
    files = {n: read_bytes(os.path.join(d, n)) for n in ('instruments_master.csv', 'nifty_options.csv', 'sensex_options.csv')}
    config.INDICES.clear()
    config.INDICES.update(saved)
    return ok, meta, probes, files


def compare(label, instruments, raw):
    dirs = [tempfile.mkdtemp() for _ in range(3)]
    try:
        a = run(im.InstrumentManager, dirs[0], Kite(instruments), False)
        b = run(LeanIM, dirs[1], Kite(instruments, raw, allow_list=False), True)
        c = run(LeanIM, dirs[2], Kite(instruments, raw, raw_fails=True), True)   # raw API fails -> fallback
        for tag, x in (('stream', b), ('fallback', c)):
            check(f'{label} [{tag}]: scan ok + INDICES metadata + expiry', a[0] == x[0] and a[1] == x[1], f'{a[1]} vs {x[1]}')
            check(f'{label} [{tag}]: ATM tokens + near-month futures ({len(a[2])} probes)', a[2] == x[2],
                  str([(p, q) for p, q in zip(a[2], x[2]) if p != q][:3]))
            for n in a[3]:
                check(f'{label} [{tag}]: {n} byte-identical ({len(a[3][n] or b"")} bytes)', a[3][n] == x[3][n])
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    syn = synthetic_instruments()
    compare('synthetic', syn, raw_from(syn))
    real = os.path.join(HERE, 'instruments_master.csv')
    if os.path.exists(real):
        raw = open(real, 'rb').read()
        with open(real, newline='') as f:
            parsed = [kite_cast(r) for r in csv.DictReader(f)]
        compare('REAL master (read-only)', parsed, raw)
    print('\nALL PASS' if not V.FAILS else f'\n{len(V.FAILS)} FAILED: ' + '; '.join(V.FAILS))
    sys.exit(1 if V.FAILS else 0)

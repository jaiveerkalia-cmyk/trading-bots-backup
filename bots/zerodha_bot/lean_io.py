"""
lean_io.py -- stdlib-only (no pandas/numpy) replacements for the bot's CSV + instrument-scan
code, so `import pandas` (~50 MB RSS) never happens in the worker process.

ACTIVE ONLY WHEN env ZB_NO_PANDAS=1 (default: off -> original pandas code runs untouched).
csv_manager.py, instrument_manager.py and ui_components/history.py keep their original pandas
code and, when the flag is on, bind the functions below over the original methods.

The files written here are meant to be BYTE-IDENTICAL to what the pandas code writes
(same column order, same int/float text, same quoting, '\n' line endings). Run
`python verify_lean.py` to prove it against the real files before enabling the flag.

How pandas' read_csv -> to_csv round trip is mirrored: every column is re-inferred from its
cells (all-int -> int text, any float or blank among numbers -> float repr, anything else ->
text untouched, blank/NA tokens -> empty), exactly as pandas would type and re-write it.
"""
import csv
import ctypes
import gc
import os
import re
from datetime import date, datetime

from config import (shared_state, params, INDICES, MASTER_INSTRUMENTS_FILE,
                    TRADEBOOK_FILE, DAILY_PNL_FILE)


# ---------------------------------------------------------------------------
# heap trim (returns freed arenas to the OS; a no-op if libc.so.6 is unavailable)
# ---------------------------------------------------------------------------
def trim_heap():
    try:
        gc.collect()
    except Exception:
        pass
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# tiny CSV engine mirroring pandas read_csv -> to_csv
# ---------------------------------------------------------------------------
class _F(str):
    """Cell text that came from a file (as opposed to a python value we set ourselves)."""


_INT_RE = re.compile(r'^[+-]?\d+$')
_FLOAT_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')
_NA = frozenset(['', '#N/A', '#N/A N/A', '#NA', '-1.#IND', '-1.#QNAN', '-NaN', '-nan',
                 '1.#IND', '1.#QNAN', '<NA>', 'N/A', 'NA', 'NULL', 'NaN', 'None', 'n/a',
                 'nan', 'null'])


def read_table(path):
    """-> (header, rows); every cell is a _F. Raises on a missing/empty file like read_csv."""
    with open(path, newline='', encoding='utf-8') as f:
        rd = csv.reader(f)
        try:
            header = next(rd)
        except StopIteration:
            raise ValueError('empty csv file: ' + path)
        n = len(header)
        rows = []
        for r in rd:
            if not r:
                continue
            if len(r) < n:
                r = r + [''] * (n - len(r))
            elif len(r) > n:
                raise ValueError('too many fields in ' + path)
            rows.append([_F(c) for c in r])
    return header, rows


def write_table(path, header, text_rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(header)
        w.writerows(text_rows)


def _kind(cells):
    """'int' | 'float' | 'raw' -- the dtype pandas would give this column."""
    saw_num = saw_float = saw_blank = False
    for c in cells:
        if isinstance(c, _F):
            if c in _NA:
                saw_blank = True
            elif _INT_RE.match(c):
                saw_num = True
            elif _FLOAT_RE.match(c):
                saw_num = saw_float = True
            else:
                return 'raw'
        elif c is None:
            saw_blank = True
        elif isinstance(c, bool):
            return 'raw'
        elif isinstance(c, int):
            saw_num = True
        elif isinstance(c, float):
            saw_num = saw_float = True
        else:
            return 'raw'
    if not saw_num:
        return 'raw'
    return 'float' if (saw_float or saw_blank) else 'int'


def _render(cells, kind):
    out = []
    if kind == 'int':
        for c in cells:
            out.append(str(int(c)))
    elif kind == 'float':
        for c in cells:
            if c is None or (isinstance(c, _F) and c in _NA):
                out.append('')
            else:
                out.append(repr(float(c)))
    else:
        for c in cells:
            if c is None:
                out.append('')
            elif isinstance(c, _F):
                out.append('' if c in _NA else str(c))
            elif isinstance(c, float):
                out.append(repr(c))
            else:
                out.append(str(c))
    return out


def render_rows(rows, ncols, force_float=()):
    """Cells -> list of text rows, typed column by column like pandas would."""
    if not rows:
        return []
    cols = []
    for j in range(ncols):
        cells = [r[j] for r in rows]
        kind = 'float' if j in force_float else _kind(cells)
        cols.append(_render(cells, kind))
    return [list(t) for t in zip(*cols)]


def _find_trade(header, rows, trade_id):
    """Index of the first row whose Trade_ID (as pandas would stringify it) == trade_id."""
    j = header.index('Trade_ID')
    cells = [r[j] for r in rows]
    texts = _render(cells, _kind(cells))
    want = str(trade_id)
    for i, t in enumerate(texts):
        if t == want:
            return i
    return None


# ---------------------------------------------------------------------------
# csv_manager.CsvManager replacements (bound onto the class when ZB_NO_PANDAS=1)
# ---------------------------------------------------------------------------
_TB_COLS = ['Trade_ID', 'Date', 'Index_Type', 'Expiry', 'Type', 'Direction', 'Qty', 'Main_Strike',
            'Hedge_Strike', 'Open_Main_Price', 'Open_Hedge_Price', 'Open_Index_Price', 'Open_Time',
            'Close_Main_Price', 'Close_Hedge_Price', 'Close_Index_Price', 'Close_Time', 'Profit',
            'Status']
_PNL_COLS = ['Date', 'Net_Profit', 'Trade_Num', 'Trades_List']


def csv_init_files(self):
    if not os.path.exists(TRADEBOOK_FILE):
        write_table(TRADEBOOK_FILE, _TB_COLS, [])
    if not os.path.exists(DAILY_PNL_FILE):
        write_table(DAILY_PNL_FILE, _PNL_COLS, [])


def csv_log_open(self, trade_id, trade_data):
    try:
        header, rows = read_table(TRADEBOOK_FILE)
    except Exception:
        self.init_files()
        header, rows = read_table(TRADEBOOK_FILE)
    expiry = shared_state['current_expiry'].get(params['trading_index'], 'N/A')
    hedge_strike = trade_data['hedge']['strike'] if trade_data['hedge'] else 0
    hedge_entry = trade_data['hedge']['entry_price'] if trade_data['hedge'] else 0
    new_row = {
        'Trade_ID': str(trade_id), 'Date': datetime.now().strftime('%Y-%m-%d'),
        'Index_Type': params['trading_index'], 'Expiry': str(expiry),
        'Type': trade_data['type'], 'Direction': trade_data.get('direction', 'SELL'),
        'Qty': int(trade_data['qty']), 'Main_Strike': float(trade_data['main']['strike']),
        'Hedge_Strike': float(hedge_strike),
        'Open_Main_Price': float(trade_data['main']['entry_price']),
        'Open_Hedge_Price': float(hedge_entry),
        'Open_Index_Price': float(trade_data['index_entry_price']),
        'Open_Time': datetime.now().strftime('%H:%M:%S'), 'Close_Main_Price': 0.0,
        'Close_Hedge_Price': 0.0, 'Close_Index_Price': 0.0, 'Close_Time': 'Active',
        'Profit': 0.0, 'Status': 'OPEN'
    }
    for k in new_row:  # pandas concat appends unseen columns (old rows get blanks)
        if k not in header:
            header.append(k)
            for r in rows:
                r.append(_F(''))
    rows.append([new_row.get(h) for h in header])
    write_table(TRADEBOOK_FILE, header, render_rows(rows, len(header)))


def csv_update_entry_log(self, trade_id, main_p, hedge_p, idx_p):
    try:
        header, rows = read_table(TRADEBOOK_FILE)
        i = _find_trade(header, rows, trade_id)
        if i is not None:
            c = {h: j for j, h in enumerate(header)}
            rows[i][c['Open_Main_Price']] = float(main_p)
            rows[i][c['Open_Hedge_Price']] = float(hedge_p)
            rows[i][c['Open_Index_Price']] = float(idx_p)
            write_table(TRADEBOOK_FILE, header, render_rows(rows, len(header)))
    except Exception:
        pass


def csv_log_close(self, trade_id, close_data, pnl):
    try:
        header, rows = read_table(TRADEBOOK_FILE)
        i = _find_trade(header, rows, trade_id)
        if i is not None:
            c = {h: j for j, h in enumerate(header)}
            force = {c['Close_Main_Price'], c['Close_Hedge_Price'], c['Profit']}
            rows[i][c['Close_Main_Price']] = float(close_data['main_price'])
            rows[i][c['Close_Hedge_Price']] = float(close_data['hedge_price'])
            rows[i][c['Close_Index_Price']] = float(close_data['index_price'])
            rows[i][c['Close_Time']] = datetime.now().strftime('%H:%M:%S')
            rows[i][c['Profit']] = float(pnl)
            rows[i][c['Status']] = 'CLOSED'
            write_table(TRADEBOOK_FILE, header, render_rows(rows, len(header), force_float=force))
    except Exception:
        pass


def csv_save_daily_report(self):
    trades = shared_state['pnl']['trades_history']
    pnl_list = [t['pnl'] for t in trades]
    net_profit = sum(pnl_list)
    try:
        header, rows = read_table(DAILY_PNL_FILE)
    except Exception:
        self.init_files()
        header, rows = read_table(DAILY_PNL_FILE)
    new_row = {'Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'Net_Profit': round(net_profit, 2), 'Trade_Num': len(trades),
               'Trades_List': str(pnl_list)}
    for k in new_row:
        if k not in header:
            header.append(k)
            for r in rows:
                r.append(_F(''))
    rows.append([new_row.get(h) for h in header])
    write_table(DAILY_PNL_FILE, header, render_rows(rows, len(header)))


# ---------------------------------------------------------------------------
# ui_components/history.py replacement
# ---------------------------------------------------------------------------
def history_table_data(path):
    """-> (columns, rows) for the Order History table, newest row first."""
    header, rows = read_table(path)
    cols = [{'name': c, 'label': c.replace('_', ' '), 'field': c, 'sortable': True,
             'align': 'left'} for c in header]
    colvals = []
    for j in range(len(header)):
        cells = [r[j] for r in rows]
        k = _kind(cells)
        if k == 'int':
            vals = [int(c) for c in cells]
        elif k == 'float':
            vals = [None if c in _NA else float(c) for c in cells]
        else:
            vals = [None if c in _NA else str(c) for c in cells]
        colvals.append(vals)
    recs = [{h: colvals[j][i] for j, h in enumerate(header)}
            for i in range(len(rows) - 1, -1, -1)]
    return cols, recs


# ---------------------------------------------------------------------------
# instrument_manager.InstrumentManager replacements
# ---------------------------------------------------------------------------
def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _write_master(instruments):
    """Same file DataFrame(instruments).to_csv(index=False) wrote, without the DataFrame."""
    fields = {}
    for d in instruments:
        for k in d:
            if k not in fields:
                fields[k] = None
    names = list(fields)
    with open(MASTER_INSTRUMENTS_FILE, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(names)
        for d in instruments:
            w.writerow([d.get(k) for k in names])
    return len(instruments), names


def _process_single_index_lean(instruments, names, index_key, info):
    # STEP 1: index token (first match in the cash segment)
    index_row = None
    for d in instruments:
        if d['exchange'] == info['exchange'] and d['tradingsymbol'] == info['name']:
            index_row = d
            break
    if index_row is not None:
        fetched_token = int(index_row['instrument_token'])
        INDICES[index_key]['token'] = fetched_token
        print(f"🔹 {index_key} Token Updated: {fetched_token}")
    else:
        print(f"⚠️ Could not find Index Token for {info['name']}")

    # STEP 2: options only (CE/PE) of this index in its derivatives segment
    subset = [d for d in instruments
              if d['exchange'] == info['segment'] and d['name'] == index_key
              and d['instrument_type'] in ('CE', 'PE')]
    if not subset:
        print(f"⚠️ No options found for {index_key} (Check filter logic)")
        return

    exps = [_to_date(d['expiry']) for d in subset]
    today = date.today()
    valid_expiries = sorted([e for e in set(exps) if e >= today])
    if not valid_expiries:
        print(f"⚠️ No future expiry found for {index_key}")
        return

    current_expiry = valid_expiries[0]
    shared_state['current_expiry'][index_key] = str(current_expiry)
    final = [(d, e) for d, e in zip(subset, exps) if e == current_expiry]

    # STEP 3: lot size + strike step
    try:
        fetched_lot_size = int(final[0][0]['lot_size'])
        INDICES[index_key]['lot_size'] = fetched_lot_size

        strikes = sorted(set(d['strike'] for d, _ in final))
        if len(strikes) > 1:
            diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
            fetched_step = max(set(diffs), key=diffs.count)
            INDICES[index_key]['step'] = fetched_step

        print(f"🔹 {index_key} Metadata Updated -> Lot: {fetched_lot_size} | Step: {INDICES[index_key]['step']}")
    except Exception as e:
        print(f"⚠️ Error calculating metadata for {index_key}: {e}")

    # STEP 4: per-index options CSV (expiry already a date -> same text pandas wrote)
    with open(info['opt_file'], 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(names)
        for d, e in final:
            w.writerow([e if k == 'expiry' else d.get(k) for k in names])
    print(f"✅ Generated {index_key} Options File: {len(final)} contracts (Expiry: {current_expiry})")


# near-month futures, captured during the scan so the 9 MB master CSV is never re-read
_FUT = {}


def _collect_futures(instruments):
    _FUT.clear()
    wanted = {}
    for k, info in INDICES.items():
        wanted[(info['segment'], k)] = k
        _FUT[k] = []
    for d in instruments:
        if d['instrument_type'] != 'FUT':
            continue
        k = wanted.get((d['exchange'], d['name']))
        if k is not None:
            _FUT[k].append((_to_date(d['expiry']), int(d['instrument_token']), d['tradingsymbol']))


def _futures_from_master(index_name):
    info = INDICES[index_name]
    out = []
    with open(MASTER_INSTRUMENTS_FILE, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if (row['exchange'] == info['segment'] and row['name'] == index_name
                    and row['instrument_type'] == 'FUT'):
                out.append((_to_date(row['expiry']), int(row['instrument_token']),
                            row['tradingsymbol']))
    return out


def inst_fetch_and_process_instruments(self):
    try:
        print("⏳ Downloading Master Instrument List...")
        instruments = self.kite.instruments()
        count, names = _write_master(instruments)
        print(f"✅ Saved Master List ({count} records)")
        for index_key, info in INDICES.items():
            _process_single_index_lean(instruments, names, index_key, info)
        _collect_futures(instruments)
        shared_state['instruments_loaded'] = True
        return True
    except Exception as e:
        print(f"❌ Error processing instruments: {e}")
        return False
    finally:
        instruments = None
        trim_heap()


_OPT_CACHE = {}


def _load_option_rows(path):
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    hit = _OPT_CACHE.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append((float(r['strike']), r['instrument_type'], int(r['instrument_token']),
                         r['tradingsymbol']))
    _OPT_CACHE[path] = (sig, rows)
    return rows


def inst_get_atm_token(self, index_name, spot_price, transaction_type):
    try:
        step = INDICES[index_name]['step']
        atm_strike = round(spot_price / step) * step
        file_path = INDICES[index_name]['opt_file']
        if not os.path.exists(file_path):
            return None, f"File not found: {file_path}"
        for strike, itype, token, symbol in _load_option_rows(file_path):
            if strike == atm_strike and itype == transaction_type:
                return token, symbol
        return None, f"Strike {atm_strike} {transaction_type} not found"
    except Exception as e:
        return None, str(e)


def inst_get_near_month_future(self, index_name):
    try:
        if index_name in _FUT:
            entries = _FUT[index_name]
        else:
            if not os.path.exists(MASTER_INSTRUMENTS_FILE):
                return None, f"Master instruments file not found: {MASTER_INSTRUMENTS_FILE}"
            entries = _futures_from_master(index_name)
        if not entries:
            return None, f"No futures contracts found for {index_name}"
        today = date.today()
        valid = sorted([e for e in entries if e[0] >= today], key=lambda e: e[0])
        if not valid:
            return None, f"No future-dated futures expiry found for {index_name}"
        return valid[0][1], valid[0][2]
    except Exception as e:
        return None, str(e)

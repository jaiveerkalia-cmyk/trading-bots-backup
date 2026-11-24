"""
ui_components/v2_ui.py -- Positions V2 UI (used only when logic.v2 is set; see auto_run._build_v2_page).

Four entry cards (Sell Call / Buy Put on the red side, Sell Put / Buy Call on the green side),
Open Positions (one row per position, EXIT + CLOSE always visible, a collapsible Stops / Targets
editor), and Open Orders (pending entries + every premium/index exit, editable in place).

All trading logic lives in positions_engine.PositionsEngine; this module only reads its state and
calls its public methods (open_from_card, preview_card, validate_strike, set_exit, cancel_exit,
partial_exit, close_position, breached_now). Drafts (typed-but-unsaved values) never touch the
engine until Set / Modify / OPEN NOW / ARM is clicked.
"""
from datetime import datetime

from nicegui import ui

from config import params, INDICES
from positions_engine import CARDS, EXIT_KINDS

ORDER_TYPES = ['Market', 'Limit', 'Stop-Market']
STRIKE_MODES = ['ATM', 'ITM', 'OTM', 'Exact']
FIRE_ON = ['Live', '1m', '5m', '15m', '60m']
PERIODS = ['Current', '1m', '5m', '15m']
SLOT_KINDS = ('prem_stop', 'prem_tgt', 'idx_stop', 'idx_tgt')          # editable in Open Orders
EXIT_LABEL = {'pnl_stop': 'PnL stop ₹', 'pnl_tgt': 'PnL target ₹', 'prem_stop': 'Prem stop',
              'prem_tgt': 'Prem target', 'idx_stop': 'Idx stop', 'idx_tgt': 'Idx target'}
CHIP_LABEL = {'pnl_stop': 'PnL Stop', 'pnl_tgt': 'PnL Tgt', 'prem_stop': 'Prem Stop',
              'prem_tgt': 'Prem Tgt', 'idx_stop': 'Idx Stop', 'idx_tgt': 'Idx Tgt'}
CARD_STYLE = {
    'call': dict(title='Sell Call', sub='Open Short', red=True),
    'bput': dict(title='Buy Put', sub='Bearish', red=True),
    'put': dict(title='Sell Put', sub='Open Long', red=False),
    'bcall': dict(title='Buy Call', sub='Bullish', red=False),
}
CHIP_BASE = 'text-[10px] px-2 py-0.5 rounded-full border '
CHIP_OFF = CHIP_BASE + 'bg-gray-100 text-gray-500 border-gray-200'
CHIP_WORKING = CHIP_BASE + 'bg-green-100 text-green-800 border-green-300'
CHIP_DEFERRED = CHIP_BASE + 'bg-amber-100 text-amber-800 border-amber-300'


# ------------------------------------------------------------------ small helpers
def _lot_size(index):
    return int(INDICES[index]['lot_size'])


def _fmt(v):
    try:
        return f"{float(v):g}"
    except (ValueError, TypeError):
        return ''


def _pnl_cls(v):
    r = round(v)
    return 'text-green-600' if r > 0 else ('text-red-600' if r < 0 else 'text-gray-500')


def _label(pos):
    return f"{pos['index']} {int(pos['main']['strike'])} {pos['type']}"


def _decode_offset(v):
    """strike_offset -> (mode, steps, exact text): |v|<1000 relative, >=1000 explicit strike."""
    try:
        v = float(v)
    except (ValueError, TypeError):
        v = 1.0
    if abs(v) >= 1000:
        return 'Exact', 1, f"{v:g}"
    if v == 0:
        return 'ATM', 1, ''
    return ('ITM' if v > 0 else 'OTM'), max(1, int(round(abs(v)))), ''


def _confirm(title, body, on_confirm, label='Continue'):
    with ui.dialog() as dlg, ui.card().classes('p-4 gap-2 max-w-md'):
        ui.label(title).classes('font-bold text-base')
        ui.label(body).classes('text-sm text-gray-700')
        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('flat dense')

            def _go():
                dlg.close()
                on_confirm()
            ui.button(label, on_click=_go).props('dense color=primary')
    dlg.open()


def _stale(e):
    """True for the 'client has been deleted' RuntimeError raised by a tab that disconnected."""
    return 'client' in str(e).lower() and 'deleted' in str(e).lower()


# ------------------------------------------------------------------ entry cards
def entry_card(engine, card):
    st = CARD_STYLE[card]
    opt, direction = CARDS[card]
    red = st['red']
    bg = 'bg-red-50 border-red-200' if red else 'bg-green-50 border-green-200'
    btn_color = 'red-6' if red else 'green-6'
    mode0, steps0, exact0 = _decode_offset(params.get(f'{card}_strike_offset', 1))
    draft = {
        'order_type': params.get(f'{card}_order_type', 'Market'),
        'trigger': str(params.get(f'{card}_trigger_price', 0) or 0),
        'mode': mode0, 'steps': str(steps0), 'exact': exact0,
        'qty': str(params.get(f'{card}_qty', 1)),
        'fire_on': params.get(f'{card}_fire_on', 'Live'),
        'stop': '', 'target': '',
    }
    index_key = f'{card}_index'
    armed_key = f'{card}_armed'
    refs = {}

    def parse_lots():
        try:
            n = int(float(str(draft['qty']).strip()))
        except (ValueError, TypeError):
            return None
        return n if n >= 1 else None

    def offset_from_draft():
        mode = draft['mode']
        if mode == 'ATM':
            return True, 0
        if mode in ('ITM', 'OTM'):
            try:
                n = int(float(str(draft['steps']).strip()))
            except (ValueError, TypeError):
                return False, 'Check steps: enter a whole number, 1 or more'
            if n < 1:
                return False, 'Check steps: enter a whole number, 1 or more'
            return True, (n if mode == 'ITM' else -n)
        raw = str(draft['exact']).strip().replace(',', '')
        if not raw:
            return False, 'Check strike: enter a strike'
        ok, msg = engine.validate_strike(card, raw)
        _show_exact_msg(ok, msg)
        if not ok:
            return False, f'Check strike: {msg}'
        f = float(raw)
        return True, (int(f) if f.is_integer() else f)

    def _show_exact_msg(ok, msg):
        lbl = refs.get('exact_msg')
        if lbl is None:
            return
        lbl.set_text(msg)
        lbl.classes(add='text-green-700' if ok else 'text-red-600', remove='text-red-600' if ok else 'text-green-700')
        lbl.set_visibility(bool(msg))

    def on_exact_blur(_=None):
        raw = str(draft['exact']).strip()
        if not raw:
            _show_exact_msg(True, '')
            return
        ok, msg = engine.validate_strike(card, raw)
        _show_exact_msg(ok, msg if ok else f'Check strike: {msg}')

    def step_field(key, delta, minimum=1):
        try:
            n = int(float(str(draft[key]).strip()))
        except (ValueError, TypeError):
            n = minimum
        draft[key] = str(max(minimum, n + delta))
        refs[key].set_value(draft[key])

    def submit():
        lots = parse_lots()
        if lots is None:
            ui.notify('Qty must be a whole number of lots, 1 or more', type='warning')
            return
        ok, off = offset_from_draft()
        if not ok:
            ui.notify(off, type='warning')
            return
        order_type = draft['order_type']
        trig = 0.0
        if order_type != 'Market':
            try:
                trig = float(str(draft['trigger']).strip())
            except (ValueError, TypeError):
                trig = 0.0
            if trig <= 0:
                ui.notify('Enter a trigger price above 0', type='warning')
                return
        pv = engine.preview_card(card, lots, off)
        if pv['action'] == 'block':
            ui.notify(f"Blocked: {pv['msg']}", type='negative')
            return
        if pv['action'] == 'error' and order_type == 'Market':
            ui.notify(pv['msg'], type='negative')
            return
        if pv['action'] == 'net':
            ex = pv['existing']
            lot = _lot_size(ex['index'])
            new_lots = pv['qty'] // lot
            ex_lots = ex['qty'] // lot
            matched = min(new_lots, ex_lots)
            rest = ex_lots - matched
            extra = new_lots - matched
            tail = (f"and leave {rest} lot{'s' if rest != 1 else ''} open." if extra == 0 else
                    f"and open the other {extra} lot{'s' if extra != 1 else ''} as a new {direction} position.")
            body = (f"{st['title']} {pv['label']} x {new_lots} lots meets your open {ex['direction']} {_label(ex)} "
                    f"x {ex_lots} lots. Continuing will cover {matched} lot{'s' if matched != 1 else ''} of it "
                    f"(and sell the matching hedge, if any), realize its PnL, {tail}")
            _confirm('This order may offset an existing position', body,
                     lambda: proceed(lots, off, order_type, trig, True))
            return
        proceed(lots, off, order_type, trig, False)

    def proceed(lots, off, order_type, trig, net_ok):
        if order_type == 'Market':
            ok, msg = engine.open_from_card(card, lots, off, reason='Market (Immediate)', net_ok=net_ok,
                                            new_stop=draft['stop'], new_target=draft['target'])
            ui.notify(msg, type='positive' if ok else 'negative')
            return
        params[f'{card}_order_type'] = order_type
        params[f'{card}_trigger_price'] = trig
        params[f'{card}_fire_on'] = draft['fire_on']
        params[f'{card}_qty'] = lots
        params[f'{card}_strike_offset'] = off
        params[f'{card}_new_stop'] = draft['stop']
        params[f'{card}_new_target'] = draft['target']
        params[f'{card}_net_ok'] = bool(net_ok)
        params[f'{card}_armed_at'] = datetime.now()
        params[armed_key] = True
        ui.notify(f"{st['title']} ARMED: {order_type} @ {trig:g} ({draft['fire_on']})", type='positive')

    def cancel():
        was = params.get(armed_key, False)
        _se = getattr(engine, 'stop_engine', None)
        if _se is not None and hasattr(_se, 'cancel_card_job'):
            _se.cancel_card_job(card)          # also drops an entry that is waiting for its candle
        params[armed_key] = False
        params[f'{card}_armed_at'] = None
        params[f'{card}_net_ok'] = False
        params[f'{card}_new_stop'] = ''
        params[f'{card}_new_target'] = ''
        draft['stop'] = ''
        draft['target'] = ''
        if was:
            engine.logic.log_action(f"{st['title']} order cancelled")
        ui.notify(f"{st['title']} order cancelled" if was else 'Nothing armed', type='info')

    with ui.card().classes(f'w-full p-3 gap-1 {bg} border shadow-sm rounded-xl'):
        with ui.row().classes('w-full items-center justify-between'):
            with ui.row().classes('items-baseline gap-1'):
                ui.label(st['title']).classes('font-bold text-sm uppercase')
                ui.label(st['sub']).classes('text-[10px] text-gray-500')
            ui.toggle(list(INDICES.keys()), value=params.get(index_key, params['trading_index'])) \
                .bind_value(params, index_key).props('dense size=sm no-caps')
        ui.label('Stop-Market fires on breakout confirmation; Limit fires on a better price.') \
            .classes('text-[10px] text-gray-500')
        with ui.row().classes('w-full items-center gap-x-3 gap-y-0'):
            ui.radio(ORDER_TYPES, value=draft['order_type']).bind_value(draft, 'order_type').props('inline dense')
            ui.element('div').classes('w-px h-4 bg-gray-400 self-center')
            ui.radio(STRIKE_MODES, value=draft['mode']).bind_value(draft, 'mode').props('inline dense')
        with ui.row().classes('w-full gap-2 items-center'):
            ui.input('Trigger Price').bind_value(draft, 'trigger').props('outlined dense bg-color=white').classes('grow')
            steps_row = ui.row().classes('grow items-center gap-1 no-wrap')
            steps_row.bind_visibility_from(draft, 'mode', backward=lambda m: m in ('ITM', 'OTM'))
            with steps_row:
                ui.button(icon='remove', on_click=lambda: step_field('steps', -1)).props('flat dense round size=sm')
                refs['steps'] = ui.input('Steps').bind_value(draft, 'steps') \
                    .props('outlined dense bg-color=white hide-bottom-space').classes('grow text-center')
                ui.button(icon='add', on_click=lambda: step_field('steps', 1)).props('flat dense round size=sm')
            refs['exact'] = ui.input('Strike (e.g. 25000)').bind_value(draft, 'exact') \
                .props('outlined dense bg-color=white hide-bottom-space').classes('grow')
            refs['exact'].bind_visibility_from(draft, 'mode', backward=lambda m: m == 'Exact')
            refs['exact'].on('blur', on_exact_blur)
        refs['exact_msg'] = ui.label('').classes('w-full text-xs')
        refs['exact_msg'].set_visibility(False)
        with ui.row().classes('w-full gap-2 items-center'):
            with ui.row().classes('grow items-center gap-1 no-wrap'):
                ui.button(icon='remove', on_click=lambda: step_field('qty', -1)).props('flat dense round size=sm')
                refs['qty'] = ui.input('Qty (Lots)').bind_value(draft, 'qty') \
                    .props('outlined dense bg-color=white hide-bottom-space').classes('grow text-center')
                ui.button(icon='add', on_click=lambda: step_field('qty', 1)).props('flat dense round size=sm')
            ui.select(FIRE_ON, value=draft['fire_on'], label='Fire on').bind_value(draft, 'fire_on') \
                .props('outlined dense bg-color=white').classes('grow')
        ui.label().classes('text-[10px] text-gray-500 -mt-1').bind_text_from(
            params, index_key, backward=lambda i: f"1 lot = {_lot_size(i)} qty ({i})")
        with ui.row().classes('w-full gap-2'):
            ui.input('Stop (optional)').bind_value(draft, 'stop').props('outlined dense bg-color=white').classes('grow')
            ui.input('Target (optional)').bind_value(draft, 'target').props('outlined dense bg-color=white').classes('grow')
        armed = ui.label().classes('w-full text-center text-xs font-bold text-white bg-green-600 rounded p-1')
        armed.bind_visibility_from(params, armed_key)
        armed.bind_text_from(params, armed_key, backward=lambda a: (
            f"ARMED: {params.get(f'{card}_order_type')} @ {params.get(f'{card}_trigger_price')} "
            f"({params.get(f'{card}_fire_on')})" if a else ''))
        with ui.row().classes('w-full gap-2'):
            primary = ui.button(on_click=submit).props(f'color={btn_color} no-caps').classes('grow font-bold')
            primary.bind_text_from(draft, 'order_type', backward=lambda t: 'OPEN NOW' if t == 'Market' else 'ARM')
            primary.bind_enabled_from(params, armed_key, backward=lambda a: not a)
            ui.button('CANCEL', on_click=cancel).props('color=blue-grey-5 no-caps').classes('grow font-bold')
    return {'draft': draft, 'submit': submit, 'cancel': cancel, 'refs': refs}


# ------------------------------------------------------------------ exit editors (shared by rows and orders)
def _slot_text(kind, slot, pos):
    if slot['state'] == 'off' or slot['val'] == '':
        return f"{CHIP_LABEL[kind]}: not set", CHIP_OFF
    t = f"{CHIP_LABEL[kind]} {_fmt(slot['val'])}"
    if not kind.startswith('pnl'):
        t += f" {slot['period']}"
    t += f" {slot['state']}"
    if str(slot.get('lots', '')).strip() != '':
        t += f" ({slot['lots']} of {pos['qty'] // _lot_size(pos['index'])} lots)"
    return t, (CHIP_WORKING if slot['state'] == 'working' else CHIP_DEFERRED)


class _ExitDraft:
    """Typed-but-unsaved values of one exit slot, shared by a Set/Modify control. Re-syncs from
    the engine's slot whenever the user hasn't touched it (dirty=False)."""

    def __init__(self, engine, pid, kind):
        self.engine, self.pid, self.kind = engine, pid, kind
        self.dirty = False
        self.busy = False
        self.widgets = {}

    def touch(self, _=None):
        if not self.busy:
            self.dirty = True

    def sync(self):
        pos = self.engine.find(self.pid)
        if not pos or self.dirty:
            return
        slot = pos['exits'][self.kind]
        self.busy = True
        try:
            self.widgets['val'].set_value(_fmt(slot['val']) if slot['val'] != '' else '')
            if 'period' in self.widgets:
                self.widgets['period'].set_value(slot['period'])
                self.widgets['lots'].set_value(str(slot['lots']) if str(slot['lots']).strip() != '' else '')
        finally:
            self.busy = False

    def values(self):
        w = self.widgets
        return (w['val'].value, w['period'].value if 'period' in w else 'Current',
                w['lots'].value if 'lots' in w else '')

    def apply(self):
        pos = self.engine.find(self.pid)
        if not pos:
            ui.notify('Position no longer open', type='warning')
            return
        val, period, lots = self.values()
        label = EXIT_LABEL[self.kind]

        def do_set():
            ok, msg = self.engine.set_exit(self.pid, self.kind, val, period, lots)
            if ok:
                self.dirty = False
                ui.notify(f"{_label(pos)} {label} set", type='positive')
            else:
                ui.notify(msg, type='negative')

        if self.engine.breached_now(self.pid, self.kind, val):
            _confirm('Already breached',
                     f"{label} {_fmt(val)} on {_label(pos)} is already breached at the current price, so it will "
                     f"exit on the next tick (or the next candle close for 1m/5m/15m). Set it anyway?", do_set, 'Set anyway')
        else:
            do_set()

    def reset(self):
        ok, msg = self.engine.cancel_exit(self.pid, self.kind)
        self.dirty = False
        if ok:
            ui.notify(f"{EXIT_LABEL[self.kind]} cancelled", type='info')
        else:
            ui.notify(msg, type='negative')


def _exit_controls(engine, pid, kind, with_label=True):
    """Value / Period / Lots inputs + Set + Reset for one slot. Returns the _ExitDraft."""
    d = _ExitDraft(engine, pid, kind)
    is_pnl = kind.startswith('pnl')
    if with_label:
        ui.label(EXIT_LABEL[kind]).classes('text-xs font-bold text-gray-600 w-24')
    d.widgets['val'] = ui.input(on_change=d.touch).props('outlined dense bg-color=white hide-bottom-space').classes('w-24')
    if is_pnl:
        ui.label('n/a').classes('text-xs text-gray-400 w-20 text-center')
        ui.label('all').classes('text-xs text-gray-400 w-14 text-center')
    else:
        d.widgets['period'] = ui.select(PERIODS, value='Current', on_change=d.touch) \
            .props('outlined dense bg-color=white hide-bottom-space').classes('w-24')
        d.widgets['lots'] = ui.input(on_change=d.touch).props('outlined dense bg-color=white hide-bottom-space placeholder=all') \
            .classes('w-16')
    ui.button('Set', on_click=d.apply).props('dense no-caps color=primary size=sm')
    ui.button('Reset', on_click=d.reset).props('dense no-caps flat size=sm')
    d.sync()
    return d


# ------------------------------------------------------------------ open positions
def _position_row(engine, pid):
    pos = engine.find(pid)
    is_buy = pos['direction'] == 'BUY'
    border = 'border-blue-500' if is_buy else ('border-red-500' if pos['type'] == 'CE' else 'border-green-500')
    refs, drafts = {}, {}
    with ui.card().classes(f'w-full p-2 gap-1 border-l-4 {border} shadow-sm'):
        with ui.row().classes('w-full items-center gap-3'):
            refs['sym'] = ui.label(_label(pos)).classes('font-mono font-bold text-sm')
            tag_cls = ('bg-blue-100 text-blue-700' if is_buy else
                       ('bg-red-100 text-red-700' if pos['type'] == 'CE' else 'bg-green-100 text-green-700'))
            ui.label('BUY' if is_buy else 'SELL').classes(f'text-[10px] font-bold px-2 py-0.5 rounded {tag_cls}')
            ui.label(f"pos {pos['pos_no']}").classes('text-[10px] text-gray-500')
            ui.element('div').classes('grow')
            for key, title in (('mark', 'Mark'), ('size', 'Size'), ('pnl', 'uPnL')):
                with ui.column().classes('gap-0 items-end'):
                    ui.label(title).classes('text-[9px] uppercase text-gray-400')
                    refs[key] = ui.label('-').classes('font-mono text-sm')
        with ui.row().classes('w-full gap-6 text-[11px] text-gray-600'):
            refs['entry'] = ui.label('Entry -')
            refs['qty'] = ui.label('Qty -')
        with ui.row().classes('w-full gap-1'):
            for kind in EXIT_KINDS:
                refs[f'chip_{kind}'] = ui.label('').classes(CHIP_OFF)
        # Always-visible actions (outside the collapsible editor)
        with ui.row().classes('w-full items-center gap-2 mt-1'):
            ui.label('Exit lots').classes('text-[11px] text-gray-500')
            refs['exit_lots'] = ui.input(value='1').props('outlined dense bg-color=white hide-bottom-space').classes('w-16')

            def do_exit():
                p = engine.find(pid)
                if not p:
                    return
                try:
                    n = int(float(str(refs['exit_lots'].value).strip()))
                except (ValueError, TypeError):
                    ui.notify('Exit lots must be a whole number', type='warning')
                    return
                lot = _lot_size(p['index'])
                if n < 1:
                    ui.notify('Exit at least 1 lot', type='warning')
                    return
                if n * lot >= p['qty']:
                    ui.notify('That is the whole position: use CLOSE', type='warning')
                    return

                def go():
                    ok, msg = engine.partial_exit(pid, n)
                    ui.notify(msg, type='positive' if ok else 'negative')
                _confirm('Exit at market', f"Exit {n} lot{'s' if n != 1 else ''} of {_label(p)} at market?", go, 'Exit')

            def do_close():
                ok, msg = engine.close_position(pid, 'Manual Close')
                ui.notify(msg, type='positive' if ok else 'negative')
            ui.button('EXIT (Market)', on_click=do_exit).props('dense no-caps color=orange-8 size=sm')
            ui.element('div').classes('grow')
            ui.button('CLOSE', on_click=do_close).props('dense no-caps color=red-6 size=sm')
        with ui.expansion('Stops / Targets').classes('w-full').props('dense'):
            with ui.column().classes('w-full gap-1'):
                with ui.row().classes('items-center gap-2 text-[10px] text-gray-500'):
                    ui.label('').classes('w-24')
                    ui.label('Value').classes('w-24')
                    ui.label('Period').classes('w-24')
                    ui.label('Lots (blank = all)').classes('w-16')
                for kind in ('prem_stop', 'prem_tgt', 'idx_stop', 'idx_tgt', 'pnl_stop', 'pnl_tgt'):
                    with ui.row().classes('items-center gap-2'):
                        drafts[kind] = _exit_controls(engine, pid, kind)

    def update():
        p = engine.find(pid)
        if not p:
            return
        lot = _lot_size(p['index'])
        refs['sym'].set_text(_label(p))
        refs['mark'].set_text(f"{p['main']['current_price']:.2f}")
        refs['size'].set_text(f"{p['qty'] // lot}")
        refs['pnl'].set_text(f"₹ {p['pnl']:.0f}")
        refs['pnl'].classes(replace=f"font-mono text-sm font-bold {_pnl_cls(p['pnl'])}")
        refs['entry'].set_text(f"Entry {p['main']['entry_price']:.2f}")
        refs['qty'].set_text(f"Qty {p['qty']}")
        for kind in EXIT_KINDS:
            text, cls = _slot_text(kind, p['exits'][kind], p)
            refs[f'chip_{kind}'].set_text(text)
            refs[f'chip_{kind}'].classes(replace=cls)
        for d in drafts.values():
            d.sync()
    update()
    return {'update': update, 'drafts': drafts, 'refs': refs, 'do_exit': do_exit, 'do_close': do_close}


def render_positions(engine):
    state = {'ids': None, 'rows': {}}
    with ui.card().classes('w-full p-3 gap-2 shadow-sm'):
        with ui.row().classes('w-full items-center gap-3'):
            ui.label('OPEN POSITIONS').classes('font-bold text-sm text-gray-700')
            count = ui.label('0 positions').classes('text-[11px] text-gray-500')
            ui.element('div').classes('grow')
            ui.label('Total uPnL').classes('text-[10px] uppercase text-gray-400')
            total = ui.label('₹ 0').classes('font-mono text-sm font-bold text-gray-500')
        container = ui.column().classes('w-full gap-2')

    def tick():
        try:
            ids = tuple(p['id'] for p in engine.positions)
            if ids != state['ids']:
                state['ids'] = ids
                state['rows'].clear()
                container.clear()
                with container:
                    if not ids:
                        ui.label('No open positions').classes('text-xs text-gray-400')
                    for p in list(engine.positions):
                        state['rows'][p['id']] = _position_row(engine, p['id'])
            for r in state['rows'].values():
                r['update']()
            n = len(ids)
            count.set_text(f"{n} position{'s' if n != 1 else ''}")
            tot = sum(p['pnl'] for p in engine.positions)
            total.set_text(f"₹ {tot:.0f}")
            total.classes(replace=f"font-mono text-sm font-bold {_pnl_cls(tot)}")
        except RuntimeError as e:
            if _stale(e):
                return
            raise
    tick()
    ui.timer(1.0, tick)
    return {'tick': tick, 'state': state}


# ------------------------------------------------------------------ open orders
def _order_signature(engine):
    exits = tuple((p['id'], k, p['exits'][k]['state'], str(p['exits'][k]['val']))
                  for p in engine.positions for k in SLOT_KINDS if p['exits'][k]['state'] != 'off')
    armed = tuple((c, params.get(f'{c}_order_type'), str(params.get(f'{c}_trigger_price')), params.get(f'{c}_fire_on'),
                   str(params.get(f'{c}_qty')), params.get(f'{c}_index'))
                  for c in CARDS if params.get(f'{c}_armed'))
    return exits, armed


def render_open_orders(engine):
    state = {'sig': None, 'drafts': []}
    with ui.card().classes('w-full p-3 gap-2 shadow-sm'):
        with ui.row().classes('w-full items-center gap-3'):
            ui.label('OPEN ORDERS').classes('font-bold text-sm text-gray-700')
            count = ui.label('0 orders').classes('text-[11px] text-gray-500')
        with ui.element('div').classes('w-full overflow-x-auto'):
            body = ui.column().classes('w-full gap-1 min-w-[760px]')

    def header():
        with ui.row().classes('w-full items-center gap-2 text-[10px] uppercase text-gray-400 border-b pb-1'):
            for text, w in (('Position', 'w-44'), ('Order', 'w-24'), ('Value', 'w-24'), ('Period', 'w-24'),
                            ('Lots', 'w-16'), ('State', 'w-36'), ('Actions', 'w-40')):
                ui.label(text).classes(w)

    def exit_row(pos, kind):
        slot = pos['exits'][kind]
        with ui.row().classes('w-full items-center gap-2 text-xs'):
            ui.label(f"{_label(pos)} ({pos['direction']})").classes('w-44 font-mono')
            ui.label(EXIT_LABEL[kind]).classes('w-24')
            d = _ExitDraft(engine, pos['id'], kind)
            d.widgets['val'] = ui.input(on_change=d.touch).props('outlined dense bg-color=white hide-bottom-space').classes('w-24')
            d.widgets['period'] = ui.select(PERIODS, value='Current', on_change=d.touch) \
                .props('outlined dense bg-color=white hide-bottom-space').classes('w-24')
            d.widgets['lots'] = ui.input(on_change=d.touch) \
                .props('outlined dense bg-color=white hide-bottom-space placeholder=all').classes('w-16')
            text, cls = _slot_text(kind, slot, pos)
            ui.label(slot['state']).classes(cls + ' w-36 text-center')
            with ui.row().classes('w-40 gap-1'):
                ui.button('Modify', on_click=d.apply).props('dense no-caps color=primary size=sm')
                ui.button('Cancel', on_click=d.reset).props('dense no-caps color=red-6 size=sm')
            d.sync()
            state['drafts'].append(d)

    def entry_row(card):
        st = CARD_STYLE[card]
        with ui.row().classes('w-full items-center gap-2 text-xs'):
            ui.label(f"{st['title']} · {params.get(f'{card}_index')}").classes('w-44')
            ui.label(f"Entry {params.get(f'{card}_order_type')}").classes('w-24')
            ui.label(_fmt(params.get(f'{card}_trigger_price'))).classes('w-24')
            ui.label(str(params.get(f'{card}_fire_on'))).classes('w-24')
            ui.label(str(params.get(f'{card}_qty'))).classes('w-16')
            ui.label('armed').classes(CHIP_WORKING + ' w-36 text-center')

            def cancel(c=card):
                _se = getattr(engine, 'stop_engine', None)
                if _se is not None and hasattr(_se, 'cancel_card_job'):
                    _se.cancel_card_job(c)
                params[f'{c}_armed'] = False
                params[f'{c}_armed_at'] = None
                params[f'{c}_net_ok'] = False
                engine.logic.log_action(f"{CARD_STYLE[c]['title']} order cancelled")
                ui.notify(f"{CARD_STYLE[c]['title']} order cancelled", type='info')
            with ui.row().classes('w-40 gap-1'):
                ui.button('Cancel', on_click=cancel).props('dense no-caps color=red-6 size=sm')

    def tick():
        try:
            sig = _order_signature(engine)
            if sig != state['sig']:
                state['sig'] = sig
                state['drafts'].clear()
                body.clear()
                with body:
                    exits, armed = sig
                    if not exits and not armed:
                        ui.label('No open orders').classes('text-xs text-gray-400')
                    else:
                        header()
                        for c in CARDS:
                            if params.get(f'{c}_armed'):
                                entry_row(c)
                        for pos in list(engine.positions):
                            for kind in SLOT_KINDS:
                                if pos['exits'][kind]['state'] != 'off':
                                    exit_row(pos, kind)
                n = len(sig[0]) + len(sig[1])
                count.set_text(f"{n} order{'s' if n != 1 else ''}")
            for d in state['drafts']:
                d.sync()
        except RuntimeError as e:
            if _stale(e):
                return
            raise
    tick()
    ui.timer(1.0, tick)
    return {'tick': tick, 'state': state}

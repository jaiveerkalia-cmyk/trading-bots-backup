"""
Price-alert UI: the Add Upper/Lower Alert forms, the shared Alert Sound Profile panel, and
the Active Alerts list (expandable rows with MODIFY/CANCEL). Alerts themselves live in
shared_state['alerts']; LogicEngine._check_alerts (logic_engine.py) is what actually
evaluates and fires them.
"""
from nicegui import ui
from config import params, UI_OPTS, shared_state, ALERT_SOUND_URLS, save_alert_profile
from datetime import datetime
import uuid

from ._shared import _log_alert_action

def _add_alert_card(direction, side_label, input_key, period_key, notify_fn):
    """'Add Alert' form for one direction (Upper or Lower). Unlike the old single-slot
    version, this does NOT hold the alert's live state -- clicking 'Add' appends a brand new,
    independent entry to shared_state['alerts'] and clears the input for the next one, so any
    number of alerts can be created in the same direction. Sound/duration are captured at
    creation time from the current shared Alert Sound Profile (render_alert_sound_panel),
    and can be changed per-alert afterward via MODIFY in the Active Alerts section.

    'index' is stamped onto the alert at creation time from params['trading_index'] --
    whichever index (NIFTY or SENSEX) is currently selected in the banner radio when Add
    Alert is clicked. This locks the alert to that index's LTP for its entire lifetime: it is
    evaluated ONLY against that index's price by LogicEngine._check_alerts, regardless of
    whether params['trading_index'] is later switched to the other index. This is what stops
    an alert created while on NIFTY from silently firing against SENSEX's price the moment
    the person switches the banner's Index selector (or vice versa) -- previously alerts had
    no index of their own and were evaluated against whatever index happened to be currently
    selected at check time, which is exactly the bug this field fixes. The index an alert
    belongs to cannot be changed afterward (not exposed in MODIFY in _alert_row below); to
    get a SENSEX alert, switch to SENSEX first and add it there."""
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label(f'Add {side_label} Price Alert').classes('font-bold text-gray-800')

        with ui.row().classes('items-center w-full justify-between'):
            ui.label('Period:').classes('text-xs text-gray-500')
            ui.radio(UI_OPTS['alert_periods'], value=params[period_key]).bind_value(params, period_key).props('inline dense')

        ui.input(side_label).bind_value(params, input_key).props('outlined dense bg-color=white').classes('w-full')

        def add_alert():
            try:
                value = float(params[input_key])
                if value <= 0: raise ValueError
            except (ValueError, TypeError):
                notify_fn("Invalid Alert Value", type='negative')
                return
            alert_index = params['trading_index']
            new_alert = {
                'id': str(uuid.uuid4())[:8],
                'index': alert_index,
                'direction': direction,
                'value': value,
                'period': params[period_key],
                'sound': params.get('alert_upper_sound', 'Wood Plank'),
                'duration': params.get('alert_upper_duration', 5),
                'created_at': datetime.now().strftime('%H:%M:%S'),
            }
            shared_state['alerts'].append(new_alert)
            params[input_key] = 0  # clear the form so the next alert starts fresh
            notify_fn(f"{side_label} Alert ADDED ({alert_index}): {value}", type='positive')
            _log_alert_action(f"🔔 {side_label} Alert Set ({alert_index}): {value} ({new_alert['period']})")

        ui.button('Add Alert', color='orange', on_click=add_alert).classes('w-full h-8 rounded-lg')


def alerts_card_upper():
    _add_alert_card('upper', 'Upper', 'alert_upper_input', 'alert_upper_period', ui.notify)


def alerts_card_lower():
    _add_alert_card('lower', 'Lower', 'alert_lower_input', 'alert_lower_period', ui.notify)


def _preview_sound(sound_name, duration):
    url = ALERT_SOUND_URLS.get(sound_name, ALERT_SOUND_URLS['Wood Plank'])
    try: dur_ms = int(float(duration) * 1000)
    except (ValueError, TypeError): dur_ms = 3000
    if dur_ms <= 0: dur_ms = 3000
    dur_ms = min(dur_ms, 8000)  # cap preview length so trying several sounds isn't tedious
    ui.run_javascript(
        f'const a = new Audio("{url}"); a.loop = true; a.play().catch(()=>{{}});'
        f'setTimeout(() => {{ a.pause(); a.currentTime = 0; }}, {dur_ms});'
    )


def render_alert_sound_panel():
    """Shared sound + duration profile used by BOTH Upper and Lower price alerts, separated
    from the alert cards themselves. Preview plays the selected sound without committing it;
    Set applies it to both alerts AND persists it to disk (alert_sound_profile.json) via
    config.save_alert_profile, so it survives page reloads and worker/script restarts."""
    with ui.card().classes('w-full p-3 gap-2 bg-yellow-50 shadow-md border-l-4 border-yellow-400 rounded-xl'):
        ui.label('Alert Sound Profile').classes('font-bold text-gray-800')
        ui.label('Applies to both Upper and Lower price alerts.').classes('text-[10px] text-gray-500 -mt-1 mb-1')

        draft = {'sound': params.get('alert_upper_sound', 'Wood Plank'), 'duration': params.get('alert_upper_duration', 5)}

        with ui.row().classes('w-full gap-2'):
            ui.select(UI_OPTS['alert_sounds'], value=draft['sound'], label='Sound').bind_value(draft, 'sound').props('outlined dense bg-color=white').classes('grow')
            ui.input('Duration (s)').bind_value(draft, 'duration').props('outlined dense bg-color=white').classes('w-28')

        def preview():
            _preview_sound(draft['sound'], draft['duration'])

        def set_profile():
            params['alert_upper_sound'] = draft['sound']; params['alert_upper_duration'] = draft['duration']
            params['alert_lower_sound'] = draft['sound']; params['alert_lower_duration'] = draft['duration']
            save_alert_profile(draft['sound'], draft['duration'])
            ui.notify(f"Alert sound set: {draft['sound']} ({draft['duration']}s)", type='positive')

        with ui.row().classes('w-full gap-2'):
            ui.button('▶ Preview', on_click=preview).classes('grow h-8 rounded-lg')
            ui.button('Set', color='orange', on_click=set_profile).classes('grow h-8 rounded-lg')


# --- ACTIVE ALERTS (multiple, independent price alerts per direction) ---

def _alert_row(alert):
    """One expandable row for a single pending price alert in shared_state['alerts'],
    matching the same header/expansion pattern as the Order Book rows (_orderbook_table_row/
    _exit_order_row) for visual consistency across the app. MODIFY opens a staged draft
    (value/period/sound/duration) that only commits on CONFIRM, by looking the alert back up
    via its id (so it keeps editing the right entry even if other alerts are added/removed/
    reordered in the list while this row's expansion is open). CANCEL removes it immediately,
    no confirm needed (matching REMOVE elsewhere in the app).

    The alert's 'index' (NIFTY or SENSEX, stamped at creation time in _add_alert_card) is
    shown as a small badge in the header, next to the Upper/Lower label, so it's always clear
    at a glance which underlying each pending alert is watching -- especially important once
    alerts for both indices can be pending side by side. This field is intentionally NOT
    editable in MODIFY below: an alert is fixed to the index it was created under for its
    whole lifetime (see _add_alert_card's docstring)."""
    alert_id = alert['id']
    direction = alert['direction']
    label = 'Upper' if direction == 'upper' else 'Lower'
    label_cls = 'w-14 text-orange-600 font-bold' if direction == 'upper' else 'w-14 text-blue-600 font-bold'
    alert_index = alert.get('index', 'NIFTY')
    index_badge_cls = 'bg-indigo-100 text-indigo-700 text-[10px] font-bold px-2 py-0.5 rounded w-16 text-center'
    draft = {'value': alert['value'], 'period': alert['period'], 'sound': alert['sound'], 'duration': alert['duration']}

    def _find():
        # Re-fetch the live dict by id every time, since shared_state['alerts'] entries are
        # never mutated in place from outside _check_alerts (which replaces the whole list).
        for a in shared_state['alerts']:
            if a.get('id') == alert_id: return a
        return None

    with ui.expansion('', icon='notifications').classes('w-full bg-white border border-gray-200 rounded-lg').props('dense') as exp:
        with exp.add_slot('header'):
            with ui.row().classes('w-full items-center gap-3 text-xs pr-2'):
                ui.label(alert['created_at']).classes('w-16 text-gray-400 font-mono')
                ui.label(alert_index).classes(index_badge_cls)
                ui.label(label).classes(label_cls)
                # These four are bound directly to the SAME dict object stored in
                # shared_state['alerts'] (not a static f-string snapshot) so that CONFIRM's
                # in-place mutation of that dict (see confirm_changes below) is reflected here
                # immediately. Without this, the header kept showing the pre-MODIFY value: rows
                # are only rebuilt when the alert id SET changes (see render_active_alerts),
                # not on every field edit, so a static label set once at build time would never
                # picked up a later in-place change to the same alert.
                ui.label().bind_text_from(alert, 'value', backward=lambda v, d=direction: f"idx {'>=' if d == 'upper' else '<='} {v}").classes('w-32 font-mono text-gray-800')
                ui.label().bind_text_from(alert, 'period').classes('w-16 text-gray-500')
                ui.label().bind_text_from(alert, 'sound').classes('w-32 text-purple-700')
                ui.label().bind_text_from(alert, 'duration', backward=lambda v: f"{v}s").classes('w-12 text-gray-500')
                ui.label('PENDING').classes('bg-orange-500 text-white px-2 py-0.5 rounded text-[10px] font-bold')
                ui.space()

                def cancel_alert():
                    shared_state['alerts'] = [a for a in shared_state['alerts'] if a.get('id') != alert_id]
                    ui.notify(f"{alert_index} {label} Alert Cancelled", type='info')
                    _log_alert_action(f"🔔 {alert_index} {label} Alert Cancelled: {alert['value']}")

                def open_modify():
                    live = _find()
                    if live:
                        draft['value'] = live['value']; draft['period'] = live['period']
                        draft['sound'] = live['sound']; draft['duration'] = live['duration']
                    exp.value = True

                ui.button('MODIFY').props('flat dense size=sm no-caps').classes('text-[10px] text-blue-600').on('click.stop', open_modify)
                ui.button('CANCEL').props('flat dense size=sm no-caps').classes('text-[10px] text-red-600').on('click.stop', cancel_alert)

        with ui.column().classes('w-full p-3 gap-2 bg-gray-50'):
            with ui.row().classes('w-full gap-2'):
                ui.input(label).bind_value(draft, 'value').props('outlined dense bg-color=white').classes('grow')
                ui.radio(UI_OPTS['alert_periods'], value=draft['period']).bind_value(draft, 'period').props('inline dense')
            with ui.row().classes('w-full gap-2'):
                ui.select(UI_OPTS['alert_sounds'], value=draft['sound'], label='Sound').bind_value(draft, 'sound').props('outlined dense bg-color=white').classes('grow')
                ui.input('Duration (s)').bind_value(draft, 'duration').props('outlined dense bg-color=white').classes('w-28')

            def preview():
                _preview_sound(draft['sound'], draft['duration'])

            def confirm_changes():
                try:
                    value = float(draft['value'])
                    if value <= 0: raise ValueError
                except (ValueError, TypeError):
                    ui.notify("Invalid Alert Value", type='negative')
                    return
                live = _find()
                if live is None:
                    ui.notify("Alert no longer exists", type='negative')
                    exp.value = False
                    return
                live['value'] = value; live['period'] = draft['period']
                live['sound'] = draft['sound']; live['duration'] = draft['duration']
                ui.notify(f"{alert_index} {label} Alert Updated", type='positive')
                _log_alert_action(f"🔔 {alert_index} {label} Alert Modified: {value} ({draft['period']})")
                exp.value = False

            with ui.row().classes('w-full gap-2'):
                ui.button('▶ Preview', on_click=preview).classes('grow h-8 text-xs rounded-lg')
                ui.button('CONFIRM', color='green', on_click=confirm_changes).classes('grow h-8 text-xs rounded-lg font-bold')
                ui.button('Cancel', on_click=lambda: setattr(exp, 'value', False)).classes('grow h-8 text-xs rounded-lg bg-gray-200 text-gray-800')


def render_active_alerts():
    """'ACTIVE ALERTS' section: lists every pending price alert in shared_state['alerts'],
    any number per direction, each independently editable (MODIFY) or cancellable (CANCEL).

    Rows are only cleared and rebuilt when the SET of alert ids actually changes (an alert
    added or removed/fired) -- NOT on every 1s timer tick regardless. The real cause of
    'MODIFY keeps collapsing' was that an earlier version unconditionally called
    rows_container.clear() + rebuilt every row every second; any expansion the user had just
    opened via MODIFY was destroyed and recreated (collapsed by default) within ~1 second of
    opening it, which looked exactly like the click itself failing. Only touching the DOM
    when the id set changes leaves an open expansion alone indefinitely while nothing is
    added/removed elsewhere."""
    with ui.card().classes('w-full bg-white p-3 gap-2 rounded-xl shadow-sm mb-4 border border-gray-200'):
        with ui.row().classes('w-full justify-between items-center mb-1'):
            ui.label('ACTIVE ALERTS').classes('font-bold text-xs uppercase tracking-widest text-gray-500')
            count_lbl = ui.label('0 pending').classes('text-[10px] text-gray-400')

        rows_container = ui.column().classes('w-full gap-1')
        empty_lbl = ui.label('No active alerts.').classes('w-full text-center text-xs text-gray-400 italic')

        last_ids = {'ids': None}

        def refresh_view():
            alerts = shared_state.get('alerts', [])
            count_lbl.set_text(f"{len(alerts)} pending")
            empty_lbl.set_visibility(len(alerts) == 0)

            current_ids = tuple(a.get('id') for a in alerts)
            if current_ids == last_ids['ids']:
                return  # nothing added/removed -- leave existing rows (and any open MODIFY
                        # expansion) completely untouched
            last_ids['ids'] = current_ids

            rows_container.clear()
            with rows_container:
                for alert in alerts:
                    _alert_row(alert)

        refresh_view()
        ui.timer(1.0, refresh_view)

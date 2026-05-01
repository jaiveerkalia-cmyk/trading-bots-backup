from datetime import datetime, timedelta
import csv
import os
import time

from kiteconnect import KiteConnect

print('Nifty Option Chain')


HEADER = ['Expiry date', 'Date', 'OI-Call', 'Volume Call', 'Buy Call', 'Sell Call', 'Delta Call', 'Implied Volatility', 'Strike', 'Delta Put', 'Buy Put', 'Sell Put', 'Volume Put', 'OI-Put', 'Underlying Price', 'Theta', 'Vega', 'Gamma']
AUTH_PATH = '/app/config/auth.txt'
DATA_ROOT = '/app/data/Option_Data'
INDEX_NAME = 'NIFTY'
OPTION_EXCHANGE = 'NFO'
OPTION_SEGMENT = 'NFO-OPT'
OPTION_PREFIX = 'NFO:'
SPOT_SYMBOL = 'NSE:NIFTY 50'
QUOTE_BATCH_SIZE = 450
QUOTE_BATCH_PAUSE = 0.25
SLOT_GRACE_SECONDS = 10
PREP_HOUR = 9
PREP_MINUTE = 0
FIRST_SLOT_HOUR = 9
FIRST_SLOT_MINUTE = 15
FIRST_SLOT_SECOND = 5
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30


def short_error(exc):
    text = str(exc).replace('\n', ' ').strip()
    lower_text = text.lower()
    if any(word in lower_text for word in ('token', 'session', 'permission', 'forbidden', 'unauthorized', 'api_key', 'access')):
        return 'auth/session issue'
    if any(word in lower_text for word in ('timed out', 'timeout', 'connection', 'network', 'dns', 'max retries', 'temporarily unavailable')):
        return 'network issue'
    if text:
        return text[:180]
    return exc.__class__.__name__


def load_kite():
    with open(AUTH_PATH, 'r') as f:
        api_data = f.read().strip().split(',')
    kite = KiteConnect(api_key=api_data[0].strip())
    kite.set_access_token(api_data[1].strip())
    return kite


def market_open_time(day):
    return day.replace(hour=FIRST_SLOT_HOUR, minute=FIRST_SLOT_MINUTE, second=FIRST_SLOT_SECOND, microsecond=0)


def market_prep_time(day):
    return day.replace(hour=PREP_HOUR, minute=PREP_MINUTE, second=0, microsecond=0)


def market_close_slot_time(day):
    return day.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)


def market_close_end_time(day):
    return market_close_slot_time(day) + timedelta(minutes=1)


def next_trading_open(now):
    target = market_open_time(now)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def next_trading_prep(now):
    target = market_prep_time(now)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def slot_label(slot_time):
    return slot_time.strftime('%Y-%m-%d %H:%M')


def is_slot_eligible(slot_time, now):
    return 0 <= (now - slot_time).total_seconds() <= SLOT_GRACE_SECONDS


def next_collection_slot(slot_time):
    open_time = market_open_time(slot_time)
    if slot_time == open_time:
        return slot_time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return slot_time + timedelta(minutes=1)


def initial_collection_slot(now):
    open_time = market_open_time(now)
    close_slot = market_close_slot_time(now)

    if now < open_time:
        return open_time

    current_minute = now.replace(second=0, microsecond=0)
    if current_minute == open_time.replace(second=0, microsecond=0):
        return open_time
    if current_minute == close_slot:
        return close_slot

    if now.second <= SLOT_GRACE_SECONDS:
        return current_minute

    return current_minute + timedelta(minutes=1)


def sleep_until(target):
    delay = (target - datetime.now()).total_seconds()
    if delay > 0:
        time.sleep(max(0.05, delay))


def ensure_output_files(expiry_list):
    output_dir = os.path.join(DATA_ROOT, INDEX_NAME)
    os.makedirs(output_dir, exist_ok=True)
    for expiry in expiry_list:
        path = os.path.join(output_dir, '{}-{}.csv'.format(INDEX_NAME, expiry))
        if not os.path.isfile(path):
            with open(path, 'a', newline='') as f:
                csv.writer(f, lineterminator='\n').writerow(HEADER)


def build_contracts(kite):
    instruments = kite.instruments(exchange=OPTION_EXCHANGE)
    contracts = {}

    for item in instruments:
        if item.get('name') != INDEX_NAME or item.get('segment') != OPTION_SEGMENT:
            continue

        expiry = str(item.get('expiry'))
        strike = item.get('strike')
        instrument_type = item.get('instrument_type')
        trading_symbol = item.get('tradingsymbol')
        if not expiry or strike is None or instrument_type not in ('CE', 'PE') or not trading_symbol:
            continue

        contracts.setdefault(expiry, {}).setdefault(strike, {})[instrument_type] = OPTION_PREFIX + trading_symbol

    expiry_list = sorted(contracts)
    expiry_pairs = {}
    all_symbols = []

    for expiry in expiry_list:
        pairs = []
        for strike in sorted(contracts[expiry]):
            pair = contracts[expiry][strike]
            call_symbol = pair.get('CE')
            put_symbol = pair.get('PE')
            if call_symbol and put_symbol:
                pairs.append((strike, call_symbol, put_symbol))
                all_symbols.extend((call_symbol, put_symbol))
        expiry_pairs[expiry] = pairs

    ensure_output_files(expiry_list)
    return expiry_list, expiry_pairs, all_symbols


def prepare_trading_day():
    kite = load_kite()
    expiry_list, expiry_pairs, all_symbols = build_contracts(kite)
    for expiry in expiry_list:
        print(expiry)
    return {
        'kite': kite,
        'expiry_list': expiry_list,
        'expiry_pairs': expiry_pairs,
        'all_symbols': all_symbols,
    }


def fetch_quotes(kite, symbols):
    quotes = {}
    request_symbols = [SPOT_SYMBOL] + symbols
    for start_index in range(0, len(request_symbols), QUOTE_BATCH_SIZE):
        if start_index:
            time.sleep(QUOTE_BATCH_PAUSE)
        batch = request_symbols[start_index:start_index + QUOTE_BATCH_SIZE]
        try:
            quotes.update(kite.quote(batch))
        except Exception as exc:
            raise RuntimeError(short_error(exc))
    return quotes


def get_spot_price(quotes):
    quote = quotes.get(SPOT_SYMBOL)
    if not quote:
        raise RuntimeError('spot quote missing')

    price = quote.get('last_price') or 0
    if price == 0:
        raise RuntimeError('spot price is 0')
    return price


def get_depth_price(option_quote, side):
    try:
        return option_quote.get('depth', {}).get(side, [{}])[0].get('price') or 0
    except (AttributeError, IndexError):
        return 0


def append_expiry_rows(expiry, rows):
    path = os.path.join(DATA_ROOT, INDEX_NAME, '{}-{}.csv'.format(INDEX_NAME, expiry))
    with open(path, 'a', newline='') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerows(rows)


def collect_slot(state, slot_time):
    started_at = time.time()
    row_count = 0
    file_count = 0
    timestamp = slot_label(slot_time)
    kite = state['kite']

    try:
        quotes = fetch_quotes(kite, state['all_symbols'])
        spot_price = get_spot_price(quotes)
    except RuntimeError as exc:
        print('Slot {} skipped: {}'.format(timestamp, exc))
        print('It took {0:.2f}s | 0 rows across 0 files'.format(time.time() - started_at))
        return False

    for expiry in state['expiry_list']:
        rows = []
        for strike, call_symbol, put_symbol in state['expiry_pairs'].get(expiry, []):
            call_quote = quotes.get(call_symbol)
            put_quote = quotes.get(put_symbol)
            if not call_quote or not put_quote:
                continue

            rows.append([
                expiry,
                timestamp,
                call_quote.get('oi', 0),
                call_quote.get('volume', 0),
                get_depth_price(call_quote, 'sell'),
                get_depth_price(call_quote, 'buy'),
                0,
                0,
                strike,
                0,
                get_depth_price(put_quote, 'sell'),
                get_depth_price(put_quote, 'buy'),
                put_quote.get('volume', 0),
                put_quote.get('oi', 0),
                spot_price,
                0,
                0,
                0,
            ])

        if not rows:
            print('Skipped {}: no complete CE/PE quote pairs'.format(expiry))
            continue

        append_expiry_rows(expiry, rows)
        row_count += len(rows)
        file_count += 1

    print('It took {0:.2f}s | {1} rows across {2} files'.format(time.time() - started_at, row_count, file_count))
    return True


if __name__ == '__main__':
    state = None
    active_date = None
    prepared_date = None
    last_processed_slot = None
    next_slot_to_process = None
    last_wait_target = None
    day_end_printed_for = None

    while True:
        now = datetime.now()
        today = now.date()
        prep_time = market_prep_time(now)
        open_time = market_open_time(now)
        close_slot = market_close_slot_time(now)
        close_end = market_close_end_time(now)

        if active_date != today:
            state = None
            active_date = today
            prepared_date = None
            last_processed_slot = None
            next_slot_to_process = None
            last_wait_target = None
            day_end_printed_for = None

        if now.weekday() >= 5 or now >= close_end:
            if state is not None and next_slot_to_process is not None and next_slot_to_process <= close_slot and last_processed_slot != next_slot_to_process:
                collect_slot(state, next_slot_to_process)
                last_processed_slot = next_slot_to_process
                next_slot_to_process = next_collection_slot(next_slot_to_process)
                continue
            if now.weekday() < 5 and day_end_printed_for != today:
                print('Day_end')
                print('==========================')
                day_end_printed_for = today
            target = next_trading_prep(now)
            if last_wait_target != target:
                print('Market closed; sleeping until {}'.format(target.strftime('%Y-%m-%d %H:%M:%S')))
                last_wait_target = target
            sleep_until(min(target, datetime.now() + timedelta(seconds=60)))
            continue

        if now < prep_time:
            if last_wait_target != prep_time:
                print('Market closed; sleeping until {}'.format(prep_time.strftime('%Y-%m-%d %H:%M:%S')))
                last_wait_target = prep_time
            sleep_until(prep_time)
            continue

        if now < open_time:
            if state is None:
                try:
                    state = prepare_trading_day()
                    prepared_date = today
                except Exception as exc:
                    print('Setup failed: {}'.format(short_error(exc)))
                    sleep_until(min(open_time, datetime.now() + timedelta(seconds=30)))
                    continue
            if next_slot_to_process is None:
                next_slot_to_process = open_time
            if last_wait_target != open_time:
                print('Setup complete, waiting until {}'.format(open_time.strftime('%Y-%m-%d %H:%M:%S')))
                last_wait_target = open_time
            sleep_until(open_time)
            continue

        if next_slot_to_process is None:
            next_slot_to_process = initial_collection_slot(now)

        if next_slot_to_process > close_slot:
            sleep_until(close_end)
            continue

        if now < next_slot_to_process:
            sleep_until(next_slot_to_process)
            continue

        if state is None:
            try:
                state = prepare_trading_day()
                prepared_date = today
                next_slot_to_process = initial_collection_slot(datetime.now())
            except Exception as exc:
                print('Slot {} skipped: {}'.format(slot_label(next_slot_to_process), short_error(exc)))
                last_processed_slot = next_slot_to_process
                next_slot_to_process = next_collection_slot(next_slot_to_process)
                continue

        if next_slot_to_process > close_slot:
            sleep_until(close_end)
            continue

        if last_processed_slot == next_slot_to_process:
            next_slot_to_process = next_collection_slot(next_slot_to_process)
            continue

        collect_slot(state, next_slot_to_process)
        last_processed_slot = next_slot_to_process
        next_slot_to_process = next_collection_slot(next_slot_to_process)

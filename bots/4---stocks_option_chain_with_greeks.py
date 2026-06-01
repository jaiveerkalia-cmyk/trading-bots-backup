import csv
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from kiteconnect import KiteConnect


AUTH_PATH = '/app/config/auth.txt'
DATA_ROOT = '/app/data/stock_options_data'
OPTION_OUTPUT_ROOT = DATA_ROOT
SYMBOL_CSV_PATH = '/app/data/fo_mktlots.csv'
LOT_SIZE_CSV_PATH = '/app/data/Symbols_lot_size.csv'
INSTRUMENT_CACHE_PATH = '/app/data/instrument_list.csv'
MARGIN_PAGE_URL = 'https://zerodha.com/margin-calculator/Futures/'
MARGIN_PAGE_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)
OPTION_EXCHANGE = 'NFO'
OPTION_SEGMENT = 'NFO-OPT'
SPOT_EXCHANGE = 'NSE'
HEADER = [
    'Expiry date',
    'Date',
    'OI-Call',
    'Volume Call',
    'Buy Call',
    'Sell Call',
    'Delta Call',
    'Implied Volatility',
    'Strike',
    'Delta Put',
    'Buy Put',
    'Sell Put',
    'Volume Put',
    'OI-Put',
    'Underlying Price',
    'Theta',
    'Vega',
    'Gamma',
]
BLACKLIST = {'BANKNIFTY', 'NIFTY', 'FINNIFTY', 'MIDCPNIFTY'}
POLL_INTERVAL_MINUTES = 15
PREP_START_TIME = dt_time(9, 0)
MARKET_OPEN_TIME = dt_time(9, 15)
LAST_COLLECTION_SLOT = dt_time(15, 30)
COLLECTION_OFFSET_SECONDS = 20
COLLECTION_GRACE_SECONDS = 5
RETRY_DELAY_SECONDS = 30
INITIAL_QUOTE_CHUNK_SIZE = 250
MIN_QUOTE_CHUNK_SIZE = 100
QUOTE_REQUEST_INTERVAL_SECONDS = 1.05
MAX_EXPIRIES_PER_SYMBOL = 3
ZERO_PLACEHOLDER = 0
MAX_BOOK_WORKERS = max(4, min(16, (os.cpu_count() or 8)))


@dataclass(frozen=True)
class StrikePair:
    strike: float
    call_symbol: str
    put_symbol: str


@dataclass(frozen=True)
class OptionBook:
    symbol: str
    expiry_date: date
    expiry_text: str
    spot_symbol: str
    output_path: str
    pairs: Tuple[StrikePair, ...]


@dataclass
class MarketState:
    kite: KiteConnect
    trading_day: date
    quote_symbols: Tuple[str, ...]
    books: Tuple[OptionBook, ...]


def log(message: str) -> None:
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}', flush=True)


def parse_auth_file(auth_path: str) -> Tuple[str, str]:
    with open(auth_path, 'r', encoding='utf-8') as handle:
        parts = [part.strip() for part in handle.read().strip().split(',') if part.strip()]
    if len(parts) < 2:
        raise ValueError('auth.txt must contain api_key,access_token')
    return parts[0], parts[1]


def ensure_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value_text = str(value).strip()
    if not value_text or value_text.lower() == 'nat':
        return None
    return datetime.strptime(value_text[:10], '%Y-%m-%d').date()


def fetch_margin_page_soup() -> BeautifulSoup:
    request = Request(MARGIN_PAGE_URL, headers={'User-Agent': MARGIN_PAGE_USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return BeautifulSoup(response.read(), 'lxml')


def parse_margin_symbols(page_soup: BeautifulSoup) -> List[str]:
    symbols: List[str] = []
    for cell in page_soup.find_all('td', class_='scrip text-left'):
        strong = cell.find('strong')
        if strong is None:
            continue
        symbol = strong.get_text(strip=True)
        if symbol:
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def parse_margin_lot_sizes(page_soup: BeautifulSoup) -> List[Tuple[str, int]]:
    results: List[Tuple[str, int]] = []
    seen = set()
    table = page_soup.find('table', {'id': 'table'})
    if table is None:
        return results
    tbody = table.find('tbody')
    if tbody is None:
        return results
    for row in tbody.find_all('tr'):
        attrs = row.attrs
        symbol = str(attrs.get('data-scrip') or '').strip()
        lot_size_text = str(attrs.get('data-lot_size') or '').strip()
        if not symbol or not lot_size_text:
            continue
        try:
            lot_size = int(float(lot_size_text))
        except ValueError:
            continue
        key = (symbol, lot_size)
        if key in seen:
            continue
        seen.add(key)
        results.append(key)
    return results


def write_symbol_csv(symbols: Sequence[str]) -> None:
    os.makedirs(os.path.dirname(SYMBOL_CSV_PATH), exist_ok=True)
    with open(SYMBOL_CSV_PATH, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Symbol'])
        for symbol in symbols:
            writer.writerow([symbol])


def write_lot_size_csv(rows: Sequence[Tuple[str, int]]) -> None:
    os.makedirs(os.path.dirname(LOT_SIZE_CSV_PATH), exist_ok=True)
    with open(LOT_SIZE_CSV_PATH, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Scrip', 'Lot_Size'])
        for symbol, lot_size in rows:
            writer.writerow([symbol, lot_size])


def get_symbols(page_soup: Optional[BeautifulSoup] = None) -> List[str]:
    # Keep the original core logic of scraping Zerodha's futures page, but make
    # it reusable and lighter by allowing the caller to pass the already-fetched soup.
    soup_obj = page_soup or fetch_margin_page_soup()
    symbols = parse_margin_symbols(soup_obj)
    if symbols:
        write_symbol_csv(symbols)
        log('symbols_updated')
        return symbols
    raise ValueError('No symbols found on Zerodha futures margin page')


def get_lot_size(page_soup: Optional[BeautifulSoup] = None) -> List[Tuple[str, int]]:
    # Keep the same margin-page source, but avoid refetching it when get_symbols()
    # has already parsed the same page in the current prep cycle.
    soup_obj = page_soup or fetch_margin_page_soup()
    lot_sizes = parse_margin_lot_sizes(soup_obj)
    if lot_sizes:
        write_lot_size_csv(lot_sizes)
        return lot_sizes
    raise ValueError('No lot-size rows found on Zerodha futures margin page')


def load_symbol_universe(instruments: Sequence[dict]) -> List[str]:
    try:
        margin_soup = fetch_margin_page_soup()
        symbols = get_symbols(margin_soup)
        get_lot_size(margin_soup)
        return symbols
    except Exception as exc:
        log(f'Unable to refresh symbol/lot-size scrape: {exc}')

    # Final fallback: derive the stock names directly from the option master.
    derived_symbols = sorted(
        {
            str(instrument.get('name') or '').strip()
            for instrument in instruments
            if instrument.get('segment') == OPTION_SEGMENT and str(instrument.get('name') or '').strip()
        }
    )
    if derived_symbols:
        write_symbol_csv(derived_symbols)
        return derived_symbols
    raise ValueError('Unable to build stock symbol universe')


def fetch_nfo_instruments(kite: KiteConnect) -> List[dict]:
    tries = 0
    while True:
        try:
            instruments = list(kite.instruments(exchange=OPTION_EXCHANGE))
            os.makedirs(os.path.dirname(INSTRUMENT_CACHE_PATH), exist_ok=True)
            if instruments:
                fieldnames = sorted({key for row in instruments for key in row.keys()})
                with open(INSTRUMENT_CACHE_PATH, 'w', newline='', encoding='utf-8') as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(instruments)
            return instruments
        except Exception as exc:
            tries += 1
            if tries > 5:
                if os.path.exists(INSTRUMENT_CACHE_PATH):
                    with open(INSTRUMENT_CACHE_PATH, 'r', encoding='utf-8') as handle:
                        return list(csv.DictReader(handle))
                raise exc
            log(f'Unable to download NFO instrument master, retrying: {exc}')
            time.sleep(2)


def ensure_output_files(books: Sequence[OptionBook]) -> None:
    seen = set()
    for book in books:
        if book.output_path in seen:
            continue
        seen.add(book.output_path)
        os.makedirs(os.path.dirname(book.output_path), exist_ok=True)
        if not os.path.exists(book.output_path) or os.path.getsize(book.output_path) == 0:
            with open(book.output_path, 'w', newline='', encoding='utf-8') as handle:
                csv.writer(handle).writerow(HEADER)


def prepare_market_state() -> MarketState:
    api_key, access_token = parse_auth_file(AUTH_PATH)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    instruments = fetch_nfo_instruments(kite)
    symbols = [
        symbol
        for symbol in load_symbol_universe(instruments)
        if symbol and symbol not in BLACKLIST
    ]
    symbol_set = set(symbols)

    option_rows: Dict[Tuple[str, date, float], Dict[str, str]] = {}
    expiries_by_symbol: Dict[str, set] = defaultdict(set)

    for instrument in instruments:
        symbol = str(instrument.get('name') or '').strip()
        if symbol not in symbol_set:
            continue
        if instrument.get('segment') != OPTION_SEGMENT:
            continue

        expiry_date = ensure_date(instrument.get('expiry'))
        tradingsymbol = str(instrument.get('tradingsymbol') or '').strip()
        strike = instrument.get('strike')
        instrument_type = instrument.get('instrument_type')
        if not expiry_date or not tradingsymbol or strike is None:
            continue
        if instrument_type not in {'CE', 'PE'}:
            continue

        quote_symbol = f'{OPTION_EXCHANGE}:{tradingsymbol}'
        strike_value = float(strike)
        option_rows.setdefault((symbol, expiry_date, strike_value), {})[instrument_type] = quote_symbol
        expiries_by_symbol[symbol].add(expiry_date)

    active_expiries: Dict[str, set] = {}
    for symbol, expiry_dates in expiries_by_symbol.items():
        ordered_expiries = sorted(expiry_dates)
        active_expiries[symbol] = set(ordered_expiries[:MAX_EXPIRIES_PER_SYMBOL])

    books: List[OptionBook] = []
    quote_symbols = set()
    prepared_labels: List[str] = []
    grouped_pairs: Dict[Tuple[str, date], List[StrikePair]] = defaultdict(list)

    for (symbol, expiry_date, strike), contracts in option_rows.items():
        if expiry_date not in active_expiries.get(symbol, set()):
            continue
        call_symbol = contracts.get('CE')
        put_symbol = contracts.get('PE')
        if not call_symbol or not put_symbol:
            continue
        grouped_pairs[(symbol, expiry_date)].append(
            StrikePair(strike=strike, call_symbol=call_symbol, put_symbol=put_symbol)
        )

    for (symbol, expiry_date), pairs in grouped_pairs.items():
        pairs.sort(key=lambda item: item.strike)
        expiry_text = expiry_date.strftime('%Y-%m-%d')
        output_path = os.path.join(OPTION_OUTPUT_ROOT, symbol, f'{symbol}-{expiry_text}.csv')
        book = OptionBook(
            symbol=symbol,
            expiry_date=expiry_date,
            expiry_text=expiry_text,
            spot_symbol=f'{SPOT_EXCHANGE}:{symbol}',
            output_path=output_path,
            pairs=tuple(pairs),
        )
        books.append(book)
        prepared_labels.append(f'{symbol} {expiry_text}')
        quote_symbols.add(book.spot_symbol)
        for pair in pairs:
            quote_symbols.add(pair.call_symbol)
            quote_symbols.add(pair.put_symbol)

    books.sort(key=lambda item: (item.symbol, item.expiry_date))
    if not books:
        raise ValueError('No stock option books found from the instrument master')

    ensure_output_files(books)
    for label in prepared_labels:
        log(f'Prepared expiry {label}')

    return MarketState(
        kite=kite,
        trading_day=datetime.now().date(),
        quote_symbols=tuple(sorted(quote_symbols)),
        books=tuple(books),
    )


def fetch_quotes(kite: KiteConnect, symbols: Tuple[str, ...]) -> Dict[str, dict]:
    quotes: Dict[str, dict] = {}
    start_index = 0
    chunk_size = min(INITIAL_QUOTE_CHUNK_SIZE, len(symbols))
    last_request_at: Optional[float] = None

    while start_index < len(symbols):
        end_index = min(start_index + chunk_size, len(symbols))
        chunk = list(symbols[start_index:end_index])
        try:
            if last_request_at is not None:
                elapsed = time.monotonic() - last_request_at
                if elapsed < QUOTE_REQUEST_INTERVAL_SECONDS:
                    time.sleep(QUOTE_REQUEST_INTERVAL_SECONDS - elapsed)
            quotes.update(kite.quote(chunk))
            last_request_at = time.monotonic()
            start_index = end_index
        except Exception as exc:
            last_request_at = time.monotonic()
            message = str(exc).lower()
            if 'invalid request' in message and chunk_size > MIN_QUOTE_CHUNK_SIZE:
                chunk_size = max(MIN_QUOTE_CHUNK_SIZE, chunk_size - 25)
                log(f'Reducing quote chunk size to {chunk_size}')
                continue
            raise

    return quotes


def get_depth_price(quote: dict, side: str) -> float:
    try:
        return float(quote.get('depth', {}).get(side, [{}])[0].get('price') or 0)
    except (IndexError, TypeError, ValueError):
        return 0.0


def get_number(quote: dict, key: str) -> float:
    try:
        return float(quote.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def build_rows_for_book(
    book: OptionBook,
    quotes: Dict[str, dict],
    slot_time: datetime,
) -> Tuple[List[List[float]], Optional[str]]:
    spot_quote = quotes.get(book.spot_symbol)
    spot_price = get_number(spot_quote or {}, 'last_price')
    if spot_price == 0:
        return [], f'underlying spot price is 0 for {book.spot_symbol}'

    timestamp_text = slot_time.strftime('%Y-%m-%d %H:%M')
    rows: List[List[float]] = []
    missing_pairs = 0

    for pair in book.pairs:
        call_quote = quotes.get(pair.call_symbol)
        put_quote = quotes.get(pair.put_symbol)
        if not call_quote or not put_quote:
            missing_pairs += 1
            continue

        rows.append(
            [
                book.expiry_text,
                timestamp_text,
                get_number(call_quote, 'oi'),
                get_number(call_quote, 'volume'),
                get_depth_price(call_quote, 'sell'),
                get_depth_price(call_quote, 'buy'),
                ZERO_PLACEHOLDER,
                ZERO_PLACEHOLDER,
                pair.strike,
                ZERO_PLACEHOLDER,
                get_depth_price(put_quote, 'sell'),
                get_depth_price(put_quote, 'buy'),
                get_number(put_quote, 'volume'),
                get_number(put_quote, 'oi'),
                spot_price,
                ZERO_PLACEHOLDER,
                ZERO_PLACEHOLDER,
                ZERO_PLACEHOLDER,
            ]
        )

    if rows:
        return rows, None
    if missing_pairs:
        return [], f'no complete CE/PE quote pairs available, missing {missing_pairs} strike pairs'
    return [], 'no rows could be built for this expiry'


def append_rows(output_path: str, rows: Sequence[Sequence[float]]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'a', newline='', encoding='utf-8') as handle:
        csv.writer(handle).writerows(rows)


def process_book(book: OptionBook, quotes: Dict[str, dict], slot_time: datetime) -> Tuple[str, str, int, Optional[str]]:
    try:
        rows, skip_reason = build_rows_for_book(book, quotes, slot_time)
        if not rows:
            return book.symbol, book.expiry_text, 0, skip_reason
        append_rows(book.output_path, rows)
        return book.symbol, book.expiry_text, len(rows), None
    except Exception as exc:
        return book.symbol, book.expiry_text, 0, summarize_collection_error(exc)


def collect_option_chain(state: MarketState, slot_time: datetime) -> None:
    started_at = time.time()
    quotes = fetch_quotes(state.kite, state.quote_symbols)

    written_files = 0
    written_rows = 0
    symbol_file_counts: Dict[str, int] = defaultdict(int)
    symbol_errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=min(MAX_BOOK_WORKERS, len(state.books))) as executor:
        futures = [executor.submit(process_book, book, quotes, slot_time) for book in state.books]
        for future in as_completed(futures):
            symbol, expiry_text, row_count, skip_reason = future.result()
            if skip_reason:
                symbol_errors.setdefault(symbol, skip_reason)
                continue
            written_files += 1
            written_rows += row_count
            symbol_file_counts[symbol] += 1

    elapsed = time.time() - started_at
    not_done_labels = [
        f"{symbol} not done due to '{reason}'"
        for symbol, reason in sorted(symbol_errors.items())
        if symbol_file_counts[symbol] == 0
    ]
    summary = f'It took {elapsed:0.2f}s | {written_rows} rows across {written_files} files'
    if not_done_labels:
        summary = f"{summary} | {', '.join(not_done_labels)}"
    print(summary, flush=True)


def floor_to_collection_slot(now: datetime) -> Optional[datetime]:
    if now.weekday() >= 5:
        return None
    slot_minute = (now.minute // POLL_INTERVAL_MINUTES) * POLL_INTERVAL_MINUTES
    slot = now.replace(minute=slot_minute, second=0, microsecond=0)
    if MARKET_OPEN_TIME <= slot.time() <= LAST_COLLECTION_SLOT:
        return slot
    return None


def resolve_target_slot(now: datetime, last_processed_slot: Optional[datetime]) -> Optional[datetime]:
    current_slot = floor_to_collection_slot(now)
    if current_slot is None:
        return None

    if last_processed_slot is not None:
        next_due_slot = last_processed_slot + timedelta(minutes=POLL_INTERVAL_MINUTES)
        next_due_time = next_due_slot + timedelta(seconds=COLLECTION_OFFSET_SECONDS)
        if next_due_slot < current_slot or now > next_due_time + timedelta(seconds=COLLECTION_GRACE_SECONDS):
            log(f'Previous cycle crossed into next slot, catching up {next_due_slot:%Y-%m-%d %H:%M}')
            return next_due_slot

    slot_start_time = current_slot + timedelta(seconds=COLLECTION_OFFSET_SECONDS)
    if (
        current_slot.time() in {MARKET_OPEN_TIME, LAST_COLLECTION_SLOT}
        and now <= slot_start_time + timedelta(seconds=59)
    ):
        return current_slot
    if now <= slot_start_time + timedelta(seconds=COLLECTION_GRACE_SECONDS):
        return current_slot

    next_slot = current_slot + timedelta(minutes=POLL_INTERVAL_MINUTES)
    if next_slot.time() <= LAST_COLLECTION_SLOT:
        return next_slot
    return None


def next_prep_or_market_open(now: datetime) -> datetime:
    next_time = now.replace(
        hour=PREP_START_TIME.hour,
        minute=PREP_START_TIME.minute,
        second=0,
        microsecond=0,
    )
    if now.weekday() >= 5 or now >= next_time:
        next_time += timedelta(days=1)
    while next_time.weekday() >= 5:
        next_time += timedelta(days=1)
    return next_time


def should_reset_session(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        text in message
        for text in (
            'token',
            'access',
            'author',
            'forbidden',
            'permission',
            'login',
            'session',
        )
    )


def summarize_collection_error(exc: Exception) -> str:
    message = ' '.join(str(exc).split())
    lowered = message.lower()
    if any(
        text in lowered
        for text in (
            'httpsconnectionpool',
            'max retries exceeded',
            'connection',
            'timed out',
            'timeout',
            'network',
            'temporarily unavailable',
            'remote end closed',
            'name resolution',
            'dns',
        )
    ):
        return 'network issue while reaching Kite quote API'
    if should_reset_session(exc):
        return 'auth/session issue while talking to Kite'
    if not message:
        return 'unexpected error while collecting quotes'
    if len(message) > 140:
        return f'{message[:137]}...'
    return message


def main() -> None:
    state: Optional[MarketState] = None
    last_processed_slot: Optional[datetime] = None
    last_wait_target: Optional[datetime] = None
    last_closed_until: Optional[datetime] = None

    while True:
        now = datetime.now()
        last_slot_deadline = now.replace(
            hour=LAST_COLLECTION_SLOT.hour,
            minute=LAST_COLLECTION_SLOT.minute,
            second=59,
            microsecond=999999,
        )

        if state is not None and state.trading_day != now.date():
            log('New trading day detected, refreshing instrument state')
            state = None
            last_processed_slot = None
            last_wait_target = None

        if now.weekday() >= 5 or now.time() < PREP_START_TIME or now > last_slot_deadline:
            sleep_until = next_prep_or_market_open(now)
            sleep_seconds = max(1, int((sleep_until - now).total_seconds()))
            if last_closed_until != sleep_until:
                log(f'Market window closed, sleeping until {sleep_until:%Y-%m-%d %H:%M:%S}')
                last_closed_until = sleep_until
            time.sleep(min(sleep_seconds, 300))
            continue

        last_closed_until = None
        if state is None:
            try:
                log('Loading auth and stock option instrument metadata')
                state = prepare_market_state()
                log(
                    f'Market state ready with {len(state.books)} expiry files and '
                    f'{len(state.quote_symbols)} quote symbols'
                )
            except Exception as exc:
                log(f'Unable to prepare market state: {exc}')
                time.sleep(RETRY_DELAY_SECONDS)
                continue

        if now.time() < MARKET_OPEN_TIME:
            market_open_dt = now.replace(
                hour=MARKET_OPEN_TIME.hour,
                minute=MARKET_OPEN_TIME.minute,
                second=COLLECTION_OFFSET_SECONDS,
                microsecond=0,
            )
            if last_wait_target != market_open_dt:
                log(
                    'Setup complete, waiting for first collection slot at '
                    f'{market_open_dt:%Y-%m-%d %H:%M:%S}'
                )
                last_wait_target = market_open_dt
            sleep_seconds = max(1, int((market_open_dt - now).total_seconds()))
            time.sleep(sleep_seconds)
            continue

        now = datetime.now()
        target_slot = resolve_target_slot(now, last_processed_slot)
        if target_slot is None:
            sleep_until = next_prep_or_market_open(now)
            sleep_seconds = max(1, int((sleep_until - now).total_seconds()))
            if last_closed_until != sleep_until:
                log(f'No collection slot left today, sleeping until {sleep_until:%Y-%m-%d %H:%M:%S}')
                last_closed_until = sleep_until
            time.sleep(min(sleep_seconds, 300))
            continue

        target_time = target_slot + timedelta(seconds=COLLECTION_OFFSET_SECONDS)
        if now < target_time:
            if last_wait_target != target_time:
                log(
                    'Setup complete, waiting for next collection slot at '
                    f'{target_time:%Y-%m-%d %H:%M:%S}'
                )
                last_wait_target = target_time
            sleep_seconds = max(1, int((target_time - now).total_seconds()))
            time.sleep(sleep_seconds)
            continue

        last_wait_target = None
        if last_processed_slot == target_slot:
            next_slot = target_slot + timedelta(minutes=POLL_INTERVAL_MINUTES)
            if next_slot.time() > LAST_COLLECTION_SLOT:
                continue
            next_time = next_slot + timedelta(seconds=COLLECTION_OFFSET_SECONDS)
            sleep_seconds = max(1, int((next_time - now).total_seconds()))
            time.sleep(max(1, sleep_seconds))
            continue

        try:
            log(f'Starting collection for slot {target_slot:%Y-%m-%d %H:%M}')
            collect_option_chain(state, target_slot)
            last_processed_slot = target_slot
        except Exception as exc:
            log(
                f'Skipping slot {target_slot:%Y-%m-%d %H:%M}: '
                f'{summarize_collection_error(exc)}'
            )
            last_processed_slot = target_slot
            if should_reset_session(exc):
                log('Resetting Kite session state and waiting for valid auth')
                state = None
            time.sleep(RETRY_DELAY_SECONDS)


if __name__ == '__main__':
    main()

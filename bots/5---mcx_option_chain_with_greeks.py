import csv
import gc
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from kiteconnect import KiteConnect


# CSV structure must stay identical to the existing downstream files.
HEADER = [
    'Expiry date',
    'Date',
    'OI-Call',
    'Volume Call',
    'Buy Call',
    'Sell Call',
    'Strike',
    'Buy Put',
    'Sell Put',
    'Volume Put',
    'OI-Put',
    'Underlying Price',
]
POLL_INTERVAL_MINUTES = 15
MARKET_OPEN_TIME = dt_time(9, 0)
LAST_COLLECTION_SLOT = dt_time(23, 30)
RETRY_DELAY_SECONDS = 30
INITIAL_QUOTE_CHUNK_SIZE = 250
MIN_QUOTE_CHUNK_SIZE = 100
QUOTE_REQUEST_INTERVAL_SECONDS = 1.05
COLLECTION_OFFSET_SECONDS = 10
COLLECTION_GRACE_SECONDS = 5
AUTH_PATH = '/app/config/auth.txt'
DATA_ROOT = '/app/data/MCX_options_data'

# Compact quote tuple indices: (oi, volume, sell_depth_price, buy_depth_price, last_price)
Q_OI = 0
Q_VOLUME = 1
Q_SELL = 2
Q_BUY = 3
Q_LTP = 4


@dataclass(frozen=True, slots=True)
class StrikePair:
    # A single strike is only usable when we have both CE and PE symbols.
    strike: float
    call_symbol: str
    put_symbol: str


@dataclass(frozen=True, slots=True)
class OptionBook:
    # One output CSV corresponds to one commodity symbol + one option expiry.
    symbol: str
    expiry_date: date
    expiry_text: str
    future_symbol: str
    output_path: str
    pairs: Tuple[StrikePair, ...]


@dataclass(slots=True)
class MarketState:
    # Daily cache of instrument metadata so the hot path only does quote fetch + CSV append.
    kite: KiteConnect
    trading_day: date
    quote_symbols: Tuple[str, ...]
    books: Tuple[OptionBook, ...]


def log(message: str) -> None:
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}', flush=True)


def get_base_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def get_auth_path(base_dir: str) -> str:
    return AUTH_PATH


def get_output_root(base_dir: str) -> str:
    return DATA_ROOT


def parse_auth_file(auth_path: str) -> Tuple[str, str]:
    # auth.txt is expected to contain: api_key,access_token
    with open(auth_path, 'r', encoding='utf-8') as handle:
        parts = [part.strip() for part in handle.read().strip().split(',') if part.strip()]
    if len(parts) < 2:
        raise ValueError('auth.txt must contain api_key,access_token')
    return parts[0], parts[1]


def ensure_date(value) -> Optional[date]:
    # Kite may return expiry as a date, datetime, or string depending on environment/version.
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


def choose_nearest_future_symbol(
    future_contracts: List[Tuple[date, str]],
    option_expiry: date,
) -> Optional[str]:
    # Pick the future contract whose expiry is closest to the option expiry.
    # If two contracts are equally close, prefer the later one because it is
    # usually the safer underlying reference for commodity options.
    if not future_contracts:
        return None
    return min(
        future_contracts,
        key=lambda item: (
            abs((item[0] - option_expiry).days),
            item[0] < option_expiry,
            item[0],
        ),
    )[1]


def extract_compact_quote(quote: Optional[dict]) -> Tuple[float, float, float, float, float]:
    """Extract only the required 5 values into a compact tuple:
    (oi, volume, sell_depth_price, buy_depth_price, last_price).
    This immediately discards heavy nested depth/ohlc dictionaries to save RAM.
    """
    if not isinstance(quote, dict):
        return (0.0, 0.0, 0.0, 0.0, 0.0)

    try:
        oi = float(quote.get('oi') or 0)
    except (TypeError, ValueError):
        oi = 0.0
    try:
        volume = float(quote.get('volume') or 0)
    except (TypeError, ValueError):
        volume = 0.0
    try:
        last_price = float(quote.get('last_price') or 0)
    except (TypeError, ValueError):
        last_price = 0.0

    sell_price = 0.0
    buy_price = 0.0
    depth = quote.get('depth')
    if isinstance(depth, dict):
        sell_list = depth.get('sell')
        if sell_list and isinstance(sell_list, list):
            first_sell = sell_list[0]
            if isinstance(first_sell, dict):
                try:
                    sell_price = float(first_sell.get('price') or 0)
                except (TypeError, ValueError):
                    sell_price = 0.0
        buy_list = depth.get('buy')
        if buy_list and isinstance(buy_list, list):
            first_buy = buy_list[0]
            if isinstance(first_buy, dict):
                try:
                    buy_price = float(first_buy.get('price') or 0)
                except (TypeError, ValueError):
                    buy_price = 0.0

    return (oi, volume, sell_price, buy_price, last_price)


def get_depth_price(quote: dict, side: str) -> float:
    # Kept for backward-compatibility with external callers.
    try:
        return float(quote.get('depth', {}).get(side, [{}])[0].get('price') or 0)
    except (IndexError, TypeError, ValueError):
        return 0.0


def get_number(quote: dict, key: str) -> float:
    # Kept for backward-compatibility with external callers.
    try:
        return float(quote.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def prepare_market_state(base_dir: str) -> MarketState:
    # Build all reusable metadata once so each 15-minute cycle stays fast.
    api_key, access_token = parse_auth_file(get_auth_path(base_dir))
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Restrict to exchange='MCX' to avoid downloading 150k+ instruments across other exchanges.
    instruments = kite.instruments(exchange='MCX')
    futures_by_symbol: Dict[str, List[Tuple[date, str]]] = {}
    option_rows: Dict[Tuple[str, date, float], Dict[str, str]] = {}

    for instrument in instruments:
        segment = instrument.get('segment')
        symbol = instrument.get('name')
        expiry_date = ensure_date(instrument.get('expiry'))
        tradingsymbol = instrument.get('tradingsymbol')

        if not symbol or not expiry_date or not tradingsymbol:
            continue

        symbol_interned = sys.intern(str(symbol).strip())
        quote_symbol = sys.intern(f'MCX:{tradingsymbol}')
        if segment == 'MCX-FUT':
            futures_by_symbol.setdefault(symbol_interned, []).append((expiry_date, quote_symbol))
        elif segment == 'MCX-OPT':
            strike = instrument.get('strike')
            instrument_type = instrument.get('instrument_type')
            if strike is None or instrument_type not in {'CE', 'PE'}:
                continue
            # Store CE/PE legs under the same (symbol, expiry, strike) key so we
            # can quickly keep only complete pairs.
            option_rows.setdefault((symbol_interned, expiry_date, float(strike)), {})[instrument_type] = quote_symbol

    del instruments

    for future_list in futures_by_symbol.values():
        future_list.sort(key=lambda item: item[0])

    books_map: Dict[Tuple[str, date], List[StrikePair]] = {}
    for (symbol, expiry_date, strike), contracts in option_rows.items():
        call_symbol = contracts.get('CE')
        put_symbol = contracts.get('PE')
        if not call_symbol or not put_symbol:
            continue
        books_map.setdefault((symbol, expiry_date), []).append(
            StrikePair(strike=strike, call_symbol=call_symbol, put_symbol=put_symbol)
        )

    output_root = get_output_root(base_dir)
    books: List[OptionBook] = []
    quote_symbol_set = set()
    for (symbol, expiry_date), pairs in books_map.items():
        # Map each option expiry to the nearest available future contract so we
        # can use that future's LTP as the underlying price in the CSV.
        future_symbol = choose_nearest_future_symbol(
            futures_by_symbol.get(symbol, []),
            expiry_date,
        )
        if not future_symbol:
            continue

        pairs.sort(key=lambda item: item.strike)
        expiry_text = sys.intern(expiry_date.strftime('%Y-%m-%d'))
        output_path = os.path.join(output_root, symbol, f'{symbol}-{expiry_text}.csv')

        books.append(
            OptionBook(
                symbol=symbol,
                expiry_date=expiry_date,
                expiry_text=expiry_text,
                future_symbol=future_symbol,
                output_path=output_path,
                pairs=tuple(pairs),
            )
        )

        quote_symbol_set.add(future_symbol)
        for pair in pairs:
            quote_symbol_set.add(pair.call_symbol)
            quote_symbol_set.add(pair.put_symbol)

    del option_rows, futures_by_symbol, books_map
    gc.collect()

    books.sort(key=lambda item: (item.symbol, item.expiry_date))
    if not books:
        raise ValueError('No MCX option books found from the instrument master')
    ensure_output_files(books)

    return MarketState(
        kite=kite,
        trading_day=datetime.now().date(),
        quote_symbols=tuple(sorted(quote_symbol_set)),
        books=tuple(books),
    )


def ensure_output_files(books: Sequence[OptionBook]) -> None:
    # Create folders and write the header only once per file.
    seen_paths = set()
    for book in books:
        if book.output_path in seen_paths:
            continue
        seen_paths.add(book.output_path)
        os.makedirs(os.path.dirname(book.output_path), exist_ok=True)
        if not os.path.exists(book.output_path) or os.path.getsize(book.output_path) == 0:
            with open(book.output_path, 'w', newline='', encoding='utf-8') as handle:
                csv.writer(handle).writerow(HEADER)


def fetch_quotes(
    kite: KiteConnect,
    symbols: Tuple[str, ...],
) -> Dict[str, Tuple[float, float, float, float, float]]:
    # Pull all futures and options in compact form with minimal memory overhead.
    # Zerodha's docs specify a 1 request/second rate limit for Quote APIs, so
    # we pace chunked /quote calls accordingly.
    quotes: Dict[str, Tuple[float, float, float, float, float]] = {}
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
            raw_quotes = kite.quote(chunk)
            last_request_at = time.monotonic()
            for sym, raw_quote in raw_quotes.items():
                quotes[sys.intern(sym)] = extract_compact_quote(raw_quote)
            del raw_quotes
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


def build_rows_for_book(
    book: OptionBook,
    quotes: Dict[str, Tuple[float, float, float, float, float]],
    timestamp_text: str,
) -> List[list]:
    # Convert the quote payload for one expiry file into raw CSV rows.
    future_quote = quotes.get(book.future_symbol)
    if not future_quote:
        return []

    future_price = future_quote[Q_LTP]
    if future_price == 0:
        return []

    rows: List[list] = []
    expiry_text = book.expiry_text
    for pair in book.pairs:
        call_quote = quotes.get(pair.call_symbol)
        put_quote = quotes.get(pair.put_symbol)
        if not call_quote or not put_quote:
            continue

        rows.append(
            [
                expiry_text,
                timestamp_text,
                call_quote[Q_OI],
                call_quote[Q_VOLUME],
                # Use executable prices: ask in Buy columns, bid in Sell columns.
                call_quote[Q_SELL],
                call_quote[Q_BUY],
                pair.strike,
                put_quote[Q_SELL],
                put_quote[Q_BUY],
                put_quote[Q_VOLUME],
                put_quote[Q_OI],
                future_price,
            ]
        )

    return rows


def append_rows(output_path: str, rows: Sequence[Sequence[object]]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'a', newline='', encoding='utf-8') as handle:
        csv.writer(handle).writerows(rows)


def collect_option_chain(state: MarketState, timestamp_text: str) -> None:
    started_at = time.time()
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        quotes = fetch_quotes(state.kite, state.quote_symbols)

        written_files = 0
        written_rows = 0
        symbol_stats: Dict[str, Dict[str, int]] = {}
        for book in state.books:
            rows = build_rows_for_book(book, quotes, timestamp_text)
            if not rows:
                continue
            append_rows(book.output_path, rows)
            written_files += 1
            written_rows += len(rows)
            stats = symbol_stats.setdefault(book.symbol, {'files': 0, 'rows': 0})
            stats['files'] += 1
            stats['rows'] += len(rows)
            log(
                f'Done {book.symbol} {book.expiry_text} using {book.future_symbol}: '
                f'{len(rows)} rows'
            )

        del quotes
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()

    elapsed = time.time() - started_at
    if symbol_stats:
        symbol_summary = ', '.join(
            f'{symbol}({stats["files"]} files, {stats["rows"]} rows)'
            for symbol, stats in sorted(symbol_stats.items())
        )
    else:
        symbol_summary = 'No symbols written'
    log(
        f'15-minute cycle completed in {elapsed:0.2f}s | '
        f'{written_rows} rows across {written_files} files | '
        f'Symbols done: {symbol_summary}'
    )


def floor_to_collection_slot(now: datetime) -> Optional[datetime]:
    # Round down to the nearest 15-minute boundary inside the trading window.
    if now.weekday() >= 5:
        return None
    slot = now.replace(
        minute=(now.minute // POLL_INTERVAL_MINUTES) * POLL_INTERVAL_MINUTES,
        second=0,
        microsecond=0,
    )
    if MARKET_OPEN_TIME <= slot.time() <= LAST_COLLECTION_SLOT:
        return slot
    return None


def resolve_target_slot(now: datetime) -> Optional[datetime]:
    # On startup between slots, finish setup first but wait for the next slot.
    # Each slot begins collection 10 seconds after the boundary, e.g. 16:45:10.
    # A small grace window avoids skipping a slot because of a tiny wake-up delay.
    current_slot = floor_to_collection_slot(now)
    if current_slot is None:
        return None

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


def next_market_open(now: datetime) -> datetime:
    # Used only when we are outside the trading window or on a weekend.
    next_open = now.replace(
        hour=MARKET_OPEN_TIME.hour,
        minute=MARKET_OPEN_TIME.minute,
        second=0,
        microsecond=0,
    )

    if now.weekday() >= 5 or now >= next_open:
        next_open += timedelta(days=1)

    while next_open.weekday() >= 5:
        next_open += timedelta(days=1)

    return next_open


def should_reset_session(exc: Exception) -> bool:
    # Treat auth/session failures as recoverable and rebuild the client on retry.
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
    # Keep retry logs short and readable during long outages.
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
    base_dir = get_base_dir()
    state: Optional[MarketState] = None
    last_processed_slot: Optional[datetime] = None
    last_wait_target: Optional[datetime] = None
    last_closed_until: Optional[datetime] = None

    while True:
        now = datetime.now()
        current_slot = floor_to_collection_slot(now)

        if state is not None and state.trading_day != now.date():
            # Refresh the instrument master once the calendar date changes.
            log('New trading day detected, refreshing instrument state')
            state = None
            last_processed_slot = None
            last_wait_target = None

        if current_slot is None:
            sleep_until = next_market_open(now)
            sleep_seconds = max(1, int((sleep_until - now).total_seconds()))
            if last_closed_until != sleep_until:
                log(f'Market window closed, sleeping until {sleep_until:%Y-%m-%d %H:%M:%S}')
                last_closed_until = sleep_until
            time.sleep(min(sleep_seconds, 300))
            continue

        last_closed_until = None
        if state is None:
            try:
                # Keep retrying here until auth/instruments become valid.
                log('Loading auth and MCX instrument metadata')
                state = prepare_market_state(base_dir)
                log(
                    f'Market state ready with {len(state.books)} expiry files and '
                    f'{len(state.quote_symbols)} quote symbols'
                )
            except Exception as exc:
                log(f'Unable to prepare market state: {exc}')
                time.sleep(RETRY_DELAY_SECONDS)
                continue

        now = datetime.now()
        target_slot = resolve_target_slot(now)
        if target_slot is None:
            sleep_until = next_market_open(now)
            sleep_seconds = max(1, int((sleep_until - now).total_seconds()))
            if last_closed_until != sleep_until:
                log(f'No collection slot left today, sleeping until {sleep_until:%Y-%m-%d %H:%M:%S}')
                last_closed_until = sleep_until
            time.sleep(min(sleep_seconds, 300))
            continue

        target_time = target_slot + timedelta(seconds=COLLECTION_OFFSET_SECONDS)
        if now < target_time:
            if last_wait_target != target_slot:
                log(
                    'Setup complete, waiting for next collection slot at '
                    f'{target_time:%Y-%m-%d %H:%M:%S}'
                )
                last_wait_target = target_slot
            sleep_seconds = max(1, int((target_time - now).total_seconds()))
            time.sleep(sleep_seconds)
            continue

        last_wait_target = None
        if last_processed_slot == target_slot:
            next_slot = target_slot + timedelta(minutes=POLL_INTERVAL_MINUTES)
            if next_slot.time() > LAST_COLLECTION_SLOT:
                continue
            sleep_seconds = max(1, int((next_slot - now).total_seconds()))
            time.sleep(sleep_seconds)
            continue

        try:
            # Stamp rows with the slot time itself so every write lands exactly
            # on 15-minute boundaries like 16:30, 16:45, 17:00, etc.
            timestamp_text = target_slot.strftime('%Y-%m-%d %H:%M')
            collect_option_chain(state, timestamp_text)
            last_processed_slot = target_slot
        except Exception as exc:
            log(
                f'Skipping slot {target_slot:%Y-%m-%d %H:%M}: '
                f'{summarize_collection_error(exc)}'
            )
            # Mark this slot as missed so a later recovery does not backfill it
            # with delayed live quotes carrying an old timestamp.
            last_processed_slot = target_slot
            if should_reset_session(exc):
                log('Resetting Kite session state and waiting for valid auth')
                state = None
            time.sleep(RETRY_DELAY_SECONDS)


if __name__ == '__main__':
    main()

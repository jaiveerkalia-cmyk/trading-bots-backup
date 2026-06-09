import json
import time
import os
import logging
import redis
from datetime import datetime
from kiteconnect import KiteConnect, KiteTicker

# ── Config ────────────────────────────────────────────────────────────────────
AUTH_PATH           = '/app/config/auth.txt'
REDIS_HOST          = 'redis'
REDIS_PORT          = 6379
ACTIVE_TOKENS_KEY   = 'active_tokens'
TOKEN_SYNC_INTERVAL = 2       # Seconds between token sync checks
AUTH_CHECK_INTERVAL = 30      # Seconds between auth.txt change checks
MARKET_OPEN         = (9, 0)  # HH, MM
MARKET_CLOSE        = (15, 30)# HH, MM
EOD_CLEANUP_TIME    = (16, 0) # HH, MM — clear active_tokens at 4PM

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [publisher] %(levelname)s: %(message)s'
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_auth():
    with open(AUTH_PATH, 'r') as f:
        api_data = f.read().strip().split(',')
    return api_data[0].strip(), api_data[1].strip()

def get_auth_mtime():
    try:
        return os.path.getmtime(AUTH_PATH)
    except Exception:
        return None

def get_active_tokens(r):
    try:
        return {int(t) for t in r.smembers(ACTIVE_TOKENS_KEY)}
    except Exception:
        return set()

def is_market_hours():
    now = datetime.now()
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE

def is_weekend():
    return datetime.now().weekday() in [5, 6]

def time_is(hh, mm):
    now = datetime.now()
    return now.hour == hh and now.minute == mm


# ── Ticker factory ────────────────────────────────────────────────────────────
def build_ticker(api_key, access_token, r, subscribed_tokens):
    """Creates, wires callbacks, and connects a KiteTicker. Returns instance."""

    def on_ticks(ws, ticks):
        for tick in ticks:
            token   = tick['instrument_token']
            payload = json.dumps({
                'token': token,
                'ltp':   tick.get('last_price'),
                'ts':    str(tick.get('exchange_timestamp', '')),
            })
            r.publish(f'tick:{token}', payload)

    def on_connect(ws, response):
        log.info("KiteTicker connected")
        tokens = get_active_tokens(r)
        if tokens:
            ws.subscribe(list(tokens))
            ws.set_mode(ws.MODE_LTP, list(tokens))
            subscribed_tokens.update(tokens)
            log.info(f"Resubscribed {len(tokens)} tokens on connect")

    def on_error(ws, code, reason):
        log.error(f"KiteTicker error {code}: {reason}")

    def on_close(ws, code, reason):
        log.warning(f"KiteTicker closed {code}: {reason}")

    def on_reconnect(ws, attempts):
        log.info(f"KiteTicker reconnecting, attempt {attempts}")

    def on_noreconnect(ws):
        # Zerodha drops connection at ~7:30 AM with expired token
        # This is expected — main loop will rebuild ticker after auth.txt refresh
        log.warning("KiteTicker gave up reconnecting — awaiting auth.txt refresh")

    ticker = KiteTicker(
        api_key,
        access_token,
        reconnect_max_tries=15,   # ~7.5 mins of retries at max_delay=30s
        reconnect_max_delay=30
    )
    ticker.on_ticks       = on_ticks
    ticker.on_connect     = on_connect
    ticker.on_error       = on_error
    ticker.on_close       = on_close
    ticker.on_reconnect   = on_reconnect
    ticker.on_noreconnect = on_noreconnect
    ticker.connect(threaded=True)
    return ticker


def stop_ticker(ticker):
    try:
        ticker.stop()
    except Exception as e:
        log.warning(f"Ticker stop error (expected on stale token): {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    # ── Redis connect ─────────────────────────────────────────────────────────
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
        log.info("Connected to Redis")
    except Exception as e:
        log.error(f"Redis connection failed: {e}")
        raise

    api_key, access_token = load_auth()
    last_auth_mtime       = get_auth_mtime()
    subscribed_tokens     = set()
    auth_check_counter    = 0
    eod_cleanup_done      = False  # Prevents repeated cleanup in same minute
    ticker                = build_ticker(api_key, access_token, r, subscribed_tokens)
    log.info("KiteTicker thread started")

    while True:
        try:
            now = datetime.now()

            # ── EOD cleanup at 15:32 ──────────────────────────────────────────
            # Clear active_tokens so stale subscriptions don't persist overnight
            if time_is(*EOD_CLEANUP_TIME) and not eod_cleanup_done and not is_weekend():
                r.delete(ACTIVE_TOKENS_KEY)
                subscribed_tokens.clear()
                log.info("EOD cleanup: cleared active_tokens from Redis")
                eod_cleanup_done = True

            # Reset EOD flag at midnight for next day
            if now.hour == 0 and now.minute == 0:
                eod_cleanup_done = False

            # ── Auth.txt change check (every AUTH_CHECK_INTERVAL seconds) ─────
            auth_check_counter += TOKEN_SYNC_INTERVAL
            if auth_check_counter >= AUTH_CHECK_INTERVAL:
                auth_check_counter = 0
                current_mtime = get_auth_mtime()

                if current_mtime and current_mtime != last_auth_mtime:
                    log.info("auth.txt changed — reconnecting KiteTicker with new token")
                    stop_ticker(ticker)
                    time.sleep(2)
                    api_key, access_token = load_auth()
                    last_auth_mtime   = current_mtime
                    subscribed_tokens = set()
                    ticker = build_ticker(api_key, access_token, r, subscribed_tokens)
                    log.info("KiteTicker rebuilt with new token")
                    time.sleep(5)  # Wait for connection to establish before sync loop resumes
                    continue

            # ── Token sync: add/remove from KiteTicker ────────────────────────
            current_tokens = get_active_tokens(r)
            to_add    = current_tokens - subscribed_tokens
            to_remove = subscribed_tokens - current_tokens

            if to_add:
                try:
                    ticker.subscribe(list(to_add))
                    ticker.set_mode(ticker.MODE_LTP, list(to_add))
                    subscribed_tokens.update(to_add)
                    log.info(f"Subscribed new tokens: {to_add}")
                except Exception as e:
                    log.warning(f"Subscribe failed, will retry: {e}")

            if to_remove:
                try:
                    ticker.unsubscribe(list(to_remove))
                    subscribed_tokens.difference_update(to_remove)
                    log.info(f"Unsubscribed tokens: {to_remove}")
                except Exception as e:
                    log.warning(f"Unsubscribe failed, will retry: {e}")

        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(TOKEN_SYNC_INTERVAL)


if __name__ == '__main__':
    main()

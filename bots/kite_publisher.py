import json
import sys
import time
import subprocess
import logging
import redis
from datetime import datetime
from kiteconnect import KiteTicker

# ── Config ────────────────────────────────────────────────────────────────────
AUTH_PATH           = '/app/config/auth.txt'
REDIS_HOST          = 'redis'
REDIS_PORT          = 6379
ACTIVE_TOKENS_KEY   = 'active_tokens'
WORKER_START_TIME   = (7, 45)  # HH, MM — spawn worker
WORKER_STOP_TIME    = (16, 0)  # HH, MM — kill worker + EOD cleanup
POLL_INTERVAL       = 10       # Seconds between supervisor checks
TOKEN_SYNC_INTERVAL = 2        # Seconds between token sync in worker

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

def get_active_tokens(r):
    try:
        return {int(t) for t in r.smembers(ACTIVE_TOKENS_KEY)}
    except Exception:
        return set()

def now_time():
    n = datetime.now()
    return (n.hour, n.minute)

def is_weekend():
    return datetime.now().weekday() in [5, 6]

def time_in_range(start, end):
    return start <= now_time() < end


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER — runs as a child process, owns KiteTicker for one trading day
# ═══════════════════════════════════════════════════════════════════════════════
def run_worker():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
        log.info("[worker] Connected to Redis")
    except Exception as e:
        log.error(f"[worker] Redis connection failed: {e}")
        sys.exit(1)

    api_key, access_token = load_auth()
    log.info("[worker] Auth loaded successfully")

    subscribed_tokens = set()

    def on_ticks(ws, ticks):
        for tick in ticks:
            token = tick['instrument_token']
            payload = json.dumps({
                'token': token,
                'ltp':   tick.get('last_price'),
                'ts':    str(tick.get('exchange_timestamp', '')),
            })
            r.publish(f'tick:{token}', payload)

    def on_connect(ws, response):
        log.info("[worker] KiteTicker connected")
        tokens = get_active_tokens(r)
        if tokens:
            ws.subscribe(list(tokens))
            ws.set_mode(ws.MODE_LTP, list(tokens))
            subscribed_tokens.update(tokens)
            log.info(f"[worker] Resubscribed {len(tokens)} tokens on connect")

    def on_error(ws, code, reason):
        log.error(f"[worker] KiteTicker error {code}: {reason}")

    def on_close(ws, code, reason):
        log.warning(f"[worker] KiteTicker closed {code}: {reason}")

    def on_reconnect(ws, attempts):
        log.info(f"[worker] KiteTicker reconnecting, attempt {attempts}")

    def on_noreconnect(ws):
        log.error("[worker] KiteTicker gave up reconnecting — exiting so supervisor restarts")
        sys.exit(1)

    ticker = KiteTicker(api_key, access_token, reconnect_max_tries=10, reconnect_max_delay=30)
    ticker.on_ticks       = on_ticks
    ticker.on_connect     = on_connect
    ticker.on_error       = on_error
    ticker.on_close       = on_close
    ticker.on_reconnect   = on_reconnect
    ticker.on_noreconnect = on_noreconnect
    ticker.connect(threaded=True)
    log.info("[worker] KiteTicker thread started")

    while True:
        try:
            current_tokens = get_active_tokens(r)
            to_add    = current_tokens - subscribed_tokens
            to_remove = subscribed_tokens - current_tokens

            if to_add:
                try:
                    ticker.subscribe(list(to_add))
                    ticker.set_mode(ticker.MODE_LTP, list(to_add))
                    subscribed_tokens.update(to_add)
                    log.info(f"[worker] Subscribed new tokens: {to_add}")
                except Exception as e:
                    log.warning(f"[worker] Subscribe failed, will retry: {e}")

            if to_remove:
                try:
                    ticker.unsubscribe(list(to_remove))
                    subscribed_tokens.difference_update(to_remove)
                    log.info(f"[worker] Unsubscribed tokens: {to_remove}")
                except Exception as e:
                    log.warning(f"[worker] Unsubscribe failed, will retry: {e}")

        except Exception as e:
            log.error(f"[worker] Token sync error: {e}")

        time.sleep(TOKEN_SYNC_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
# SUPERVISOR — runs 24/7, spawns/kills worker process daily
# ═══════════════════════════════════════════════════════════════════════════════
def run_supervisor():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
        log.info("[supervisor] Connected to Redis")
    except Exception as e:
        log.error(f"[supervisor] Redis connection failed: {e}")
        raise

    worker = None
    stop_done_today = False

    log.info(f"[supervisor] Started — worker spawns at {WORKER_START_TIME[0]:02d}:{WORKER_START_TIME[1]:02d} on weekdays")

    while True:
        try:
            t = now_time()

            # Reset daily flags at midnight
            if t == (0, 0):
                stop_done_today = False
                log.info("[supervisor] Midnight reset — ready for new trading day")

            if not is_weekend():

                # Spawn worker at 7:45 AM
                if time_in_range(WORKER_START_TIME, WORKER_STOP_TIME):
                    if worker is None or worker.poll() is not None:
                        # Check if worker crashed vs never started
                        if worker is not None and worker.poll() is not None:
                            log.warning(f"[supervisor] Worker exited (code {worker.poll()}) — restarting in 5s")
                            time.sleep(5)
                        else:
                            log.info("[supervisor] Spawning worker...")

                        worker = subprocess.Popen(
                            [sys.executable, '-u', __file__, '--worker'],
                            stdout=None,  # Inherits stdout → visible in docker logs
                            stderr=None
                        )
                        log.info(f"[supervisor] Worker started with PID {worker.pid}")

                # Kill worker at 4:00 PM + EOD cleanup
                elif t >= WORKER_STOP_TIME and not stop_done_today:
                    if worker and worker.poll() is None:
                        log.info("[supervisor] Market closed — stopping worker")
                        worker.terminate()
                        try:
                            worker.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            worker.kill()
                            log.warning("[supervisor] Worker force killed after timeout")
                        worker = None

                    r.delete(ACTIVE_TOKENS_KEY)
                    log.info("[supervisor] EOD cleanup: cleared active_tokens from Redis")
                    stop_done_today = True

        except Exception as e:
            log.error(f"[supervisor] Error: {e}")

        time.sleep(POLL_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if '--worker' in sys.argv:
        run_worker()
    else:
        run_supervisor()

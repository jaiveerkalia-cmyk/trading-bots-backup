"""
redis_tick_client.py
────────────────────
Drop-in Redis subscriber to replace KiteTicker in trading bots.

Usage in your bot:
    from redis_tick_client import RedisTickClient

    client = RedisTickClient(live_market_data, tick_lock)
    client.subscribe([256265, 260105])   # replaces kws.subscribe(tokens)
    client.start()                        # starts background listener thread

    # Later, mid-session:
    client.subscribe([new_token])
    client.unsubscribe([old_token])

    # On shutdown:
    client.stop()
"""

import json
import threading
import logging
import redis

# ── Config ─────────────────────────────────────────────────────────────────────
REDIS_HOST        = 'redis'         # Docker service name
REDIS_PORT        = 6379
ACTIVE_TOKENS_KEY = 'active_tokens'

log = logging.getLogger(__name__)


class RedisTickClient:
    """
    Subscribes to Redis tick channels and populates live_market_data dict,
    using the same threading.Lock pattern your bots already use.
    """

    def __init__(self, live_market_data: dict, tick_lock: threading.Lock):
        """
        Args:
            live_market_data : the existing dict your bot reads prices from
            tick_lock        : the existing threading.Lock your bot uses
        """
        self._data      = live_market_data
        self._lock      = tick_lock
        self._tokens    = set()
        self._thread    = None
        self._stop_event = threading.Event()

        self._r  = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self._ps = self._r.pubsub(ignore_subscribe_messages=True)

        try:
            self._r.ping()
            log.info("[RedisTickClient] Connected to Redis")
        except Exception as e:
            log.error(f"[RedisTickClient] Redis connection failed: {e}")
            raise

    # ── Public API ─────────────────────────────────────────────────────────────

    def subscribe(self, tokens: list):
        """Register tokens and start receiving ticks for them."""
        tokens = [int(t) for t in tokens if t]
        if not tokens:
            return

        new_tokens = [t for t in tokens if t not in self._tokens]
        if not new_tokens:
            return

        # Register in Redis active_tokens set so publisher subscribes on Kite
        self._r.sadd(ACTIVE_TOKENS_KEY, *new_tokens)

        # Subscribe to Redis channels
        channels = {f'tick:{t}': self._on_message for t in new_tokens}
        self._ps.subscribe(**channels)

        self._tokens.update(new_tokens)
        log.info(f"[RedisTickClient] Subscribed: {new_tokens}")

    def unsubscribe(self, tokens: list):
        """Unregister tokens and stop receiving ticks for them."""
        tokens = [int(t) for t in tokens if t]
        if not tokens:
            return

        existing = [t for t in tokens if t in self._tokens]
        if not existing:
            return

        # Remove from Redis active_tokens set
        self._r.srem(ACTIVE_TOKENS_KEY, *existing)

        # Unsubscribe from Redis channels
        channels = [f'tick:{t}' for t in existing]
        self._ps.unsubscribe(*channels)

        self._tokens.difference_update(existing)
        log.info(f"[RedisTickClient] Unsubscribed: {existing}")

    def start(self):
        """Start background thread that listens for ticks."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen, daemon=True, name='redis-tick-listener')
        self._thread.start()
        log.info("[RedisTickClient] Listener thread started")

    def stop(self):
        """Stop the background listener thread."""
        self._stop_event.set()
        self._ps.unsubscribe()
        if self._thread:
            self._thread.join(timeout=3)
        log.info("[RedisTickClient] Stopped")

    # ── Internal ───────────────────────────────────────────────────────────────

    def _on_message(self, message):
        """Called by pubsub listener for every incoming tick."""
        try:
            payload = json.loads(message['data'])
            token   = int(payload['token'])
            ltp     = float(payload['ltp'])

            if ltp <= 0:
                return

            with self._lock:
                self._data[token] = ltp

        except Exception as e:
            log.warning(f"[RedisTickClient] Tick parse error: {e}")

    def _listen(self):
        """Blocking pubsub listen loop, runs in background thread."""
        log.info("[RedisTickClient] Listening for ticks...")
        while not self._stop_event.is_set():
            try:
                self._ps.get_message(timeout=1.0)
            except Exception as e:
                log.error(f"[RedisTickClient] Listen error: {e}")

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
import time
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
        self._data       = live_market_data
        self._lock       = tick_lock
        self._tokens     = set()
        self._thread     = None
        self._stop_event = threading.Event()
        self._ps_lock    = threading.Lock()

        self._r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
            health_check_interval=30,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
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

        with self._ps_lock:
            new_tokens = [t for t in tokens if t not in self._tokens]
            if not new_tokens:
                return

            # Register in Redis active_tokens set so publisher subscribes on Kite
            try:
                self._r.sadd(ACTIVE_TOKENS_KEY, *new_tokens)
            except Exception as e:
                log.error(f"[RedisTickClient] Failed to register tokens in Redis: {e}")

            # Subscribe to Redis channels
            channels = {f'tick:{t}': self._on_message for t in new_tokens}
            try:
                self._ps.subscribe(**channels)
                self._tokens.update(new_tokens)
                log.info(f"[RedisTickClient] Subscribed: {new_tokens}")
            except Exception as e:
                log.error(f"[RedisTickClient] PubSub subscribe error: {e}")

    def unsubscribe(self, tokens: list):
        """Unregister tokens and stop receiving ticks for them."""
        tokens = [int(t) for t in tokens if t]
        if not tokens:
            return

        with self._ps_lock:
            existing = [t for t in tokens if t in self._tokens]
            if not existing:
                return

            # Remove from Redis active_tokens set
            try:
                self._r.srem(ACTIVE_TOKENS_KEY, *existing)
            except Exception as e:
                log.error(f"[RedisTickClient] Failed to remove tokens from Redis: {e}")

            # Unsubscribe from Redis channels
            channels = [f'tick:{t}' for t in existing]
            try:
                self._ps.unsubscribe(*channels)
                self._tokens.difference_update(existing)
                log.info(f"[RedisTickClient] Unsubscribed: {existing}")
            except Exception as e:
                log.error(f"[RedisTickClient] PubSub unsubscribe error: {e}")

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
        with self._ps_lock:
            try:
                self._ps.unsubscribe()
            except Exception:
                pass
            try:
                self._ps.close()
            except Exception:
                pass
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
        """Pubsub listen loop, runs in background thread."""
        log.info("[RedisTickClient] Listening for ticks...")
        while not self._stop_event.is_set():
            try:
                with self._ps_lock:
                    msg = self._ps.get_message(timeout=0.2)
                if msg is None:
                    time.sleep(0.01)
            except (redis.ConnectionError, redis.TimeoutError) as e:
                log.warning(f"[RedisTickClient] Redis connection lost: {e}. Reconnecting in 2s...")
                time.sleep(2)
                with self._ps_lock:
                    try:
                        if self._ps:
                            try:
                                self._ps.close()
                            except Exception:
                                pass
                        self._ps = self._r.pubsub(ignore_subscribe_messages=True)
                        if self._tokens:
                            channels = {f'tick:{t}': self._on_message for t in self._tokens}
                            self._ps.subscribe(**channels)
                            log.info(f"[RedisTickClient] Re-subscribed {len(self._tokens)} tokens after reconnect")
                    except Exception as re_err:
                        log.error(f"[RedisTickClient] Reconnect failed: {re_err}")
            except Exception as e:
                log.error(f"[RedisTickClient] Listen error: {e}")
                time.sleep(0.5)

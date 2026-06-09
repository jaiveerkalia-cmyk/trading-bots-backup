import threading
import time
from redis_tick_client import RedisTickClient
from config import shared_state, INDICES


class TickerClient:
    def __init__(self, api_key, access_token):
        # api_key and access_token kept for interface compatibility, not used here
        self.api_key = api_key
        self.access_token = access_token

        self.live_market_data = {}
        self.tick_lock = threading.Lock()
        self.subscribed_tokens = list({v['token'] for v in INDICES.values()})

        self._client = RedisTickClient(self.live_market_data, self.tick_lock)
        self._poll_thread = None
        self._running = False

    def subscribe_new(self, tokens_list):
        new_tokens = [t for t in tokens_list if t not in self.subscribed_tokens]
        if new_tokens:
            self._client.subscribe(new_tokens)
            self.subscribed_tokens.extend(new_tokens)

    def refresh_token(self, new_access_token):
        # No-op: Redis system handles auth centrally, no token needed here
        self.access_token = new_access_token

    def _poll_loop(self):
        """Background thread: reads live_market_data and writes into shared_state every second."""
        while self._running:
            try:
                with self.tick_lock:
                    data_snapshot = dict(self.live_market_data)

                for token, ltp in data_snapshot.items():
                    if ltp <= 0:
                        continue

                    # Update index prices
                    for name, info in INDICES.items():
                        if info['token'] == token:
                            shared_state[name]['ltp'] = ltp
                            break

                    # Update option chain
                    if token in shared_state['option_chain']:
                        shared_state['option_chain'][token]['ltp'] = ltp

            except Exception as e:
                print(f"⚠️ Ticker Poll Error: {e}")

            time.sleep(0.5)

    def start(self):
        self._running = True
        self._client.subscribe(self.subscribed_tokens)
        self._client.start()
        shared_state['connection_status'] = 'Connected'
        print("✅ Redis Ticker Connected & Ready.")

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self):
        self._running = False
        try:
            self._client.stop()
        except Exception:
            pass
        shared_state['connection_status'] = 'Disconnected'
        print("🛑 Redis Ticker Stopped.")

import logging
from kiteconnect import KiteTicker
from config import shared_state, INDICES
import threading
import time

# Reduce logging noise
logging.basicConfig(level=logging.WARNING)

class TickerClient:
    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self._init_kws()
        
        self.subscribed_tokens = [v['token'] for v in INDICES.values()]
        self.lock = threading.Lock() # Thread safety for token updates
        
        self.stop_requested = False 
        self.is_reloading = False # NEW: Flag for Hot-Swap

    def _init_kws(self):
        """Initializes the KiteTicker instance."""
        self.kws = KiteTicker(self.api_key, self.access_token)
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.on_close = self.on_close
        self.kws.on_error = self.on_error

    def refresh_token(self, new_access_token):
        """
        Hot-Swaps the access token and reconnects without killing the Reactor.
        """
        with self.lock:
            print(f"🔄 Hot-Swapping Token...")
            self.is_reloading = True
            self.access_token = new_access_token
            
            # 1. Update the internal URL of the KiteTicker instance
            self.kws.socket_url = "wss://ws.kite.trade?api_key={}&access_token={}".format(self.api_key, self.access_token)
            
            # 2. Force Close to trigger reconnect logic
            if self.kws.is_connected():
                self.kws.close()
            else:
                # If disconnected, try to connect immediately
                self.kws.connect(threaded=True)

    def subscribe_new(self, tokens_list):
        with self.lock:
            new_tokens = [t for t in tokens_list if t not in self.subscribed_tokens]
            if new_tokens and self.kws.is_connected():
                self.kws.subscribe(new_tokens)
                self.kws.set_mode(self.kws.MODE_QUOTE, new_tokens)
                self.subscribed_tokens.extend(new_tokens)

    def on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick['instrument_token']
            ltp = tick.get('last_price', 0)
            
            # Update Indices
            for name, info in INDICES.items():
                if info['token'] == token:
                    state = shared_state[name]
                    state['ltp'] = ltp
                    if 'ohlc' in tick:
                        state['open'] = tick['ohlc'].get('open', 0)
                        state['high'] = tick['ohlc'].get('high', 0)
                        state['low'] = tick['ohlc'].get('low', 0)
                    break 

            # Update Options
            if token in shared_state['option_chain']:
                shared_state['option_chain'][token]['ltp'] = ltp

    def on_connect(self, ws, response):
        shared_state['connection_status'] = 'Connected'
        with self.lock:
            if self.subscribed_tokens:
                ws.subscribe(self.subscribed_tokens)
                ws.set_mode(ws.MODE_QUOTE, self.subscribed_tokens)
        
        if self.is_reloading:
            print("✅ Ticker Reloaded Successfully with New Token")
            self.is_reloading = False
        else:
            print(f"✅ Ticker Connected (Thread: {threading.get_ident()})")

    def on_close(self, ws, code, reason):
        shared_state['connection_status'] = 'Closed'
        
        # If hot-swapping, attempt immediate reconnect
        if self.is_reloading:
            print("ℹ️ Connection Closing for Refresh... Reconnecting now.")
            time.sleep(0.5) # Allow socket cleanup
            try:
                # Explicitly tell reactor to connect to new URL
                ws.connect(threaded=True)
            except Exception as e:
                print(f"⚠️ Reconnect Warning: {e}")
            return 

        print(f"❌ Ticker Closed: {code} - {reason}")
        
        # Stop reactor ONLY if it's a permanent error (403) AND we aren't reloading
        # OR if manually requested
        if (code == 403 and not self.is_reloading) or self.stop_requested:
            try:
                ws.stop() # This kills the reactor
            except Exception:
                pass # Silently ignore the "ReactorNotRunning" error on double-kill
            print("🚫 Reactor Stopped.")

    def on_error(self, ws, code, reason):
        if self.stop_requested or self.is_reloading:
            return

        shared_state['connection_status'] = f"Error: {reason}"
        print(f"⚠️ Ticker Error: {code} - {reason}")
        
        if (code == 403 or "Forbidden" in str(reason)) and not self.is_reloading:
            self.stop()
            print("🔒 Token Expired. Ticker Stopped.")

    def start(self):
        self.stop_requested = False
        self.is_reloading = False
        # start() is only called ONCE at script startup
        if not self.kws.is_connected():
            self.kws.connect(threaded=True)

    def stop(self):
        # Only call this on application exit
        self.stop_requested = True
        if self.kws:
            self.kws.close()

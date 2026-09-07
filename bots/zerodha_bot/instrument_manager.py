# instrument_manager.py
import pandas as pd
from datetime import date
import os
from config import INDICES, MASTER_INSTRUMENTS_FILE, shared_state

class InstrumentManager:
    def __init__(self, kite):
        self.kite = kite

    def fetch_and_process_instruments(self):
        """
        1. Downloads master list.
        2. Updates Dynamic Data (Tokens, Steps, Lot Sizes).
        3. Filters for Current Expiry.
        4. Saves small lookup CSVs.
        """
        try:
            print("⏳ Downloading Master Instrument List...")
            instruments = self.kite.instruments()
            
            # Convert to DataFrame
            df = pd.DataFrame(instruments)
            
            # Save Master Copy
            df.to_csv(MASTER_INSTRUMENTS_FILE, index=False)
            print(f"✅ Saved Master List ({len(df)} records)")

            # Process for each Index
            for index_key, info in INDICES.items():
                self._process_single_index(df, index_key, info)

            shared_state['instruments_loaded'] = True
            return True

        except Exception as e:
            print(f"❌ Error processing instruments: {e}")
            return False

    def _process_single_index(self, df, index_key, info):
        """
        Handles logic for a single index (NIFTY/SENSEX):
        - Updates Index Token (from NSE/BSE segment)
        - Updates Step & Lot Size (from NFO/BFO segment)
        - Saves Options CSV
        """
        # --- STEP 1: Update Index Token (The Instrument itself) ---
        # We look for the Index in the Cash segment (NSE/BSE)
        index_row = df[
            (df['exchange'] == info['exchange']) & 
            (df['tradingsymbol'] == info['name'])
        ]
        
        if not index_row.empty:
            fetched_token = int(index_row.iloc[0]['instrument_token'])
            INDICES[index_key]['token'] = fetched_token
            print(f"🔹 {index_key} Token Updated: {fetched_token}")
        else:
            print(f"⚠️ Could not find Index Token for {info['name']}")

        # --- STEP 2: Filter Options & Update Metadata ---
        # FIX: Filter by 'exchange' (NFO/BFO) instead of 'segment' (NFO-OPT)
        # AND: Ensure we only get Options (CE/PE), excluding Futures
        subset = df[
            (df['exchange'] == info['segment']) &  # matches 'NFO' or 'BFO'
            (df['name'] == index_key) &            # matches 'NIFTY' or 'SENSEX'
            (df['instrument_type'].isin(['CE', 'PE'])) # Only Options
        ].copy()

        if subset.empty:
            print(f"⚠️ No options found for {index_key} (Check filter logic)")
            return

        # Convert expiry to date
        subset['expiry'] = pd.to_datetime(subset['expiry']).dt.date
        
        # Find Current Expiry
        today = date.today()
        valid_expiries = sorted([d for d in subset['expiry'].unique() if d >= today])
        
        if not valid_expiries:
            print(f"⚠️ No future expiry found for {index_key}")
            return

        current_expiry = valid_expiries[0]
        shared_state['current_expiry'][index_key] = str(current_expiry)

        # Filter for strictly the current expiry
        final_df = subset[subset['expiry'] == current_expiry]

        # --- STEP 3: Calculate Dynamic Values (Step & Lot Size) ---
        try:
            # 1. Get Lot Size (Take from the first contract)
            fetched_lot_size = int(final_df.iloc[0]['lot_size'])
            INDICES[index_key]['lot_size'] = fetched_lot_size

            # 2. Calculate Step Size (Strike Difference)
            # Get unique strikes and sort them
            strikes = sorted(final_df['strike'].unique())
            if len(strikes) > 1:
                # Calculate differences between consecutive strikes
                diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
                # Find the most common difference (Mode)
                fetched_step = max(set(diffs), key=diffs.count)
                INDICES[index_key]['step'] = fetched_step
            
            print(f"🔹 {index_key} Metadata Updated -> Lot: {fetched_lot_size} | Step: {INDICES[index_key]['step']}")

        except Exception as e:
            print(f"⚠️ Error calculating metadata for {index_key}: {e}")

        # --- STEP 4: Save to CSV ---
        final_df.to_csv(info['opt_file'], index=False)
        print(f"✅ Generated {index_key} Options File: {len(final_df)} contracts (Expiry: {current_expiry})")

    def get_atm_token(self, index_name, spot_price, transaction_type):
        """
        Reads the small CSV to find the specific token.
        Uses the dynamically updated 'step' from INDICES.
        """
        try:
            # 1. Calculate ATM Strike using DYNAMIC Step
            step = INDICES[index_name]['step']
            atm_strike = round(spot_price / step) * step
            
            # 2. Load the small CSV
            file_path = INDICES[index_name]['opt_file']
            if not os.path.exists(file_path):
                return None, f"File not found: {file_path}"
            
            df = pd.read_csv(file_path)
            
            # 3. Filter for Strike and Type
            row = df[
                (df['strike'] == atm_strike) & 
                (df['instrument_type'] == transaction_type)
            ]

            if not row.empty:
                token = int(row.iloc[0]['instrument_token'])
                symbol = row.iloc[0]['tradingsymbol']
                return token, symbol
            else:
                return None, f"Strike {atm_strike} {transaction_type} not found"

        except Exception as e:
            return None, str(e)

    def get_near_month_future(self, index_name):
        """
        Futures Mode support (config.params['futures_mode']). Resolves the near-month
        FUTURES contract for index_name (NIFTY/SENSEX) by reading the already-saved
        master instruments CSV (written by fetch_and_process_instruments() during the
        daily scan -- this method does NOT hit the network itself, so it's cheap to call
        once per day right after that scan).

        Filters the master list for:
          - exchange == INDICES[index_name]['segment']  (e.g. 'NFO' or 'BFO', same
            derivatives segment options live in)
          - name == index_name                          (e.g. 'NIFTY' or 'SENSEX')
          - instrument_type == 'FUT'
        then picks the SOONEST expiry >= today (the "near month" contract).

        Returns (token, tradingsymbol) on success, or (None, error_message) on failure --
        same return shape as get_atm_token() above, for consistent error handling by
        callers.
        """
        try:
            if not os.path.exists(MASTER_INSTRUMENTS_FILE):
                return None, f"Master instruments file not found: {MASTER_INSTRUMENTS_FILE}"

            info = INDICES[index_name]
            df = pd.read_csv(MASTER_INSTRUMENTS_FILE)

            subset = df[
                (df['exchange'] == info['segment']) &
                (df['name'] == index_name) &
                (df['instrument_type'] == 'FUT')
            ].copy()

            if subset.empty:
                return None, f"No futures contracts found for {index_name}"

            subset['expiry'] = pd.to_datetime(subset['expiry']).dt.date
            today = date.today()
            valid = subset[subset['expiry'] >= today].sort_values('expiry')

            if valid.empty:
                return None, f"No future-dated futures expiry found for {index_name}"

            near_month = valid.iloc[0]
            token = int(near_month['instrument_token'])
            symbol = near_month['tradingsymbol']
            return token, symbol

        except Exception as e:
            return None, str(e)

    def get_fill_price(self, token, symbol, segment, qty, is_buy):
        """
        Realistic fill-price estimation using LIVE MARKET DEPTH via kite.quote(), instead
        of the last traded price (LTP) alone. Used at every actual fill EVENT (open/close,
        both legs) -- LIVE trading and PAPER/simulated trading alike, since paper trading
        should mirror what a real market order would actually get filled at, not an
        idealized LTP price. NOT used for the live/unrealized PnL ticking every second in
        LogicEngine.update_pnl() -- that stays LTP-based on purpose, both because it is a
        continuous DISPLAY estimate rather than a fill event, and because polling
        kite.quote() every tick for every open position would burn through Kite's REST
        rate limit for no real benefit between actual fills.

        Mechanics: a market order walks the OPPOSITE side of the book from the direction
        you're trading -- a BUY order fills against the SELL/ask ladder (depth['sell']),
        a SELL order fills against the BUY/bid ladder (depth['buy']). Each depth level has
        its own 'price' and 'quantity'; this walks the ladder top-down, consuming quantity
        level by level, until `qty` total is filled, and returns the QUANTITY-WEIGHTED
        AVERAGE price across every level actually consumed (not just the best price) --
        this is what materially differs from an LTP-only fill and is what makes the
        estimate "realistic" for a real market order of this size, including any slippage
        from walking through multiple price levels on a large order or thin book.

        Falls back to LTP (quote['last_price']) whenever depth is unavailable, empty, or
        the fetch itself fails for any reason (e.g. illiquid/no-quotes strike, API hiccup,
        market closed) -- a fill price is ALWAYS returned, this never blocks or fails a
        trade. Returns (fill_price, used_depth: bool) so callers can log which path was
        used, and (0.0, False) only in the pathological case where even LTP is unavailable
        (token has genuinely never ticked) -- callers already treat a 0 price the same way
        the old LTP-only read did (see open_position/close_position: '0.00' fill,
        commission math naturally comes out to 0 too since turnover is 0).
        """
        try:
            quote_key = f"{segment}:{symbol}"
            quote = self.kite.quote(quote_key)
            data = quote.get(quote_key, {})
            ltp = float(data.get('last_price', 0) or 0)

            depth = data.get('depth', {}) or {}
            ladder = depth.get('sell', []) if is_buy else depth.get('buy', [])
            ladder = [lvl for lvl in ladder if lvl.get('quantity', 0) > 0 and lvl.get('price', 0) > 0]

            if not ladder:
                return ltp, False

            remaining = qty
            filled_value = 0.0
            filled_qty = 0
            for level in ladder:
                if remaining <= 0:
                    break
                level_qty = int(level.get('quantity', 0))
                level_price = float(level.get('price', 0))
                take = min(remaining, level_qty)
                filled_value += take * level_price
                filled_qty += take
                remaining -= take

            if remaining > 0:
                # Book depth (up to 5 levels) didn't cover the full qty -- fill whatever's
                # left at the worst (last) level's price, same as a real market order would
                # walk off the visible book. Still fully quantity-weighted overall.
                worst_price = float(ladder[-1].get('price', ltp))
                filled_value += remaining * worst_price
                filled_qty += remaining

            if filled_qty <= 0:
                return ltp, False

            avg_fill = filled_value / filled_qty
            return avg_fill, True

        except Exception:
            # Fall back to whatever LTP we might already have in shared_state (set by the
            # ticker) if the quote() call itself failed outright -- still never blocks a fill.
            try:
                cached_ltp = shared_state.get('option_chain', {}).get(token, {}).get('ltp', 0)
                return float(cached_ltp or 0), False
            except Exception:
                return 0.0, False

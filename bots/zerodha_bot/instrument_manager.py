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

# auth_manager.py
import os
from kiteconnect import KiteConnect
from config import AUTH_FILE_PATH

def get_kite_session():
    """
    Reads the auth.txt file from '/home/jaivk/Desktop/Python Stuff/'
    and returns an authenticated KiteConnect object.
    """
    if not os.path.exists(AUTH_FILE_PATH):
        print(f"CRITICAL ERROR: Auth file not found at {AUTH_FILE_PATH}")
        return None, None, None

    try:
        with open(AUTH_FILE_PATH, 'r') as f:
            api_data = f.read().strip()
        
        # Format: api_key,access_token
        parts = api_data.split(',')
        if len(parts) < 2:
            raise ValueError(f"Invalid format in auth.txt. Content: {api_data}")
            
        api_key = parts[0]
        access_token = parts[1]

        # Initialize KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        
        print(f"SUCCESS: Loaded session for API Key: {api_key[:4]}****")
        return kite, api_key, access_token

    except Exception as e:
        print(f"ERROR initializing Kite session: {e}")
        return None, None, None

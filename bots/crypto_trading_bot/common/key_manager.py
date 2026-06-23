"""
Loads exchange API keys from encrypted file or env var fallback.
Encrypted file format (JSON, Fernet-encrypted):
  {"binance": {"api_key": "...", "api_secret": "..."}, "delta": {...}}

To generate a master key:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

To encrypt your keys (run once):
  python -m common.key_manager
"""
from __future__ import annotations
import json
import os
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from common import settings

logger = logging.getLogger(__name__)


def load_keys() -> dict[str, dict[str, str]]:
    """Returns {exchange: {api_key, api_secret}}. File takes priority over env vars."""
    keys = _from_file()
    if not keys:
        keys = _from_env()
    if not keys:
        logger.warning("No API keys found — running without exchange credentials")
    return keys


def save_keys(keys: dict) -> None:
    """Encrypt and write keys to exchange_keys.enc."""
    master = _master_key()
    if not master:
        raise ValueError("MASTER_KEY env var not set")
    encrypted = Fernet(master).encrypt(json.dumps(keys).encode())
    settings.KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.KEYS_FILE.write_bytes(encrypted)
    logger.info(f"Keys saved to {settings.KEYS_FILE}")


def _from_file() -> dict:
    master = _master_key()
    if not master or not settings.KEYS_FILE.exists():
        return {}
    try:
        return json.loads(Fernet(master).decrypt(settings.KEYS_FILE.read_bytes()))
    except InvalidToken:
        logger.error("MASTER_KEY is incorrect — cannot decrypt exchange_keys.enc")
        return {}
    except Exception as e:
        logger.error(f"Key file load error: {e}")
        return {}


def _from_env() -> dict:
    keys = {}
    for exchange in settings.SUPPORTED_EXCHANGES:
        p   = exchange.upper()
        key = os.getenv(f'{p}_API_KEY', '')
        sec = os.getenv(f'{p}_API_SECRET', '')
        if key and sec:
            keys[exchange] = {'api_key': key, 'api_secret': sec}
    return keys


def _master_key() -> Optional[bytes]:
    v = os.getenv('MASTER_KEY', '')
    return v.encode() if v else None


# ── CLI helper — run once to encrypt your keys ────────────────────────────────
if __name__ == '__main__':
    import getpass
    print("Enter API credentials to encrypt into exchange_keys.enc")
    keys: dict = {}
    for ex in settings.SUPPORTED_EXCHANGES:
        print(f"\n{ex.upper()}")
        k = getpass.getpass(f"  API key    : ")
        s = getpass.getpass(f"  API secret : ")
        if k and s:
            keys[ex] = {'api_key': k, 'api_secret': s}
    if keys:
        save_keys(keys)
        print(f"\nSaved. Add MASTER_KEY to your .env file.")
    else:
        print("No keys entered — nothing saved.")

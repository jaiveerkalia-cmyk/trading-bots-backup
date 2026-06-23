"""
Canonical symbol registry.
Keeps a per-exchange map of canonical BASE/QUOTE <-> exchange-native symbol.
Populated at startup by instrument_sync, queried by adapters and UI.
"""
from __future__ import annotations
import threading


class SymbolMap:
    __slots__ = ('_map', '_reverse', '_lock')

    def __init__(self):
        # {exchange: {canonical: native}}
        self._map:     dict[str, dict[str, str]] = {}
        # {exchange: {native: canonical}}
        self._reverse: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def update(self, exchange: str, pairs: dict[str, str]) -> None:
        """pairs = {canonical: native}  e.g. {'BTC/USDT': 'BTCUSDT'}"""
        with self._lock:
            self._map[exchange]    = dict(pairs)
            self._reverse[exchange] = {v: k for k, v in pairs.items()}

    def to_native(self, exchange: str, canonical: str) -> str:
        with self._lock:
            return self._map.get(exchange, {}).get(canonical, canonical)

    def to_canonical(self, exchange: str, native: str) -> str:
        with self._lock:
            return self._reverse.get(exchange, {}).get(native, native)

    def all_canonical(self, exchange: str) -> list[str]:
        with self._lock:
            return list(self._map.get(exchange, {}).keys())

    def is_known(self, exchange: str, canonical: str) -> bool:
        with self._lock:
            return canonical in self._map.get(exchange, {})


# Module-level singleton — imported by adapters and UI
symbol_map = SymbolMap()

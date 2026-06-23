import pytest
from common.symbol_map import SymbolMap


@pytest.fixture
def smap():
    m = SymbolMap()
    m.update('binance', {
        'BTC/USDT': 'BTCUSDT',
        'ETH/USDT': 'ETHUSDT',
        'SOL/USDT': 'SOLUSDT',
    })
    m.update('delta', {
        'BTC/USD': 'BTCUSD',
        'ETH/USD': 'ETHUSD',
    })
    return m


class TestSymbolMap:
    def test_to_native_binance(self, smap):
        assert smap.to_native('binance', 'BTC/USDT') == 'BTCUSDT'

    def test_to_native_delta(self, smap):
        assert smap.to_native('delta', 'BTC/USD') == 'BTCUSD'

    def test_to_canonical_binance(self, smap):
        assert smap.to_canonical('binance', 'ETHUSDT') == 'ETH/USDT'

    def test_to_canonical_delta(self, smap):
        assert smap.to_canonical('delta', 'ETHUSD') == 'ETH/USD'

    def test_unknown_symbol_passthrough(self, smap):
        # Unknown symbol returns itself unchanged
        assert smap.to_native('binance', 'XYZ/ABC') == 'XYZ/ABC'
        assert smap.to_canonical('binance', 'XYZABC') == 'XYZABC'

    def test_unknown_exchange_passthrough(self, smap):
        assert smap.to_native('coinbase', 'BTC/USDT') == 'BTC/USDT'

    def test_all_canonical(self, smap):
        syms = smap.all_canonical('binance')
        assert 'BTC/USDT' in syms
        assert 'ETH/USDT' in syms
        assert len(syms) == 3

    def test_is_known_true(self, smap):
        assert smap.is_known('binance', 'BTC/USDT')

    def test_is_known_false(self, smap):
        assert not smap.is_known('binance', 'XYZ/ABC')
        assert not smap.is_known('unknown_ex', 'BTC/USDT')

    def test_update_overwrites(self, smap):
        smap.update('binance', {'BTC/USDT': 'BTCUSDT_NEW'})
        assert smap.to_native('binance', 'BTC/USDT') == 'BTCUSDT_NEW'
        # Old symbols are gone after overwrite
        assert not smap.is_known('binance', 'ETH/USDT')

    def test_empty_exchange(self, smap):
        assert smap.all_canonical('nonexistent') == []

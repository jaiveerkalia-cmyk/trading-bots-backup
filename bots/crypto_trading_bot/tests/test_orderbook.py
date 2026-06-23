import asyncio
import pytest
from datetime import datetime, timezone
from exchanges.orderbook import L2OrderBook


@pytest.fixture
def book():
    return L2OrderBook(exchange='binance', symbol='BTC/USDT', depth=5)


@pytest.fixture
def now():
    return datetime.now(timezone.utc)


class TestSnapshot:
    def test_basic_snapshot(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 1), (99, 2), (98, 3)],
            asks=[(101, 1), (102, 2), (103, 3)],
            timestamp=now,
        ))
        assert book.best_bid == pytest.approx(100)
        assert book.best_ask == pytest.approx(101)

    def test_snapshot_removes_zero_qty(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 0), (99, 2)],
            asks=[(101, 1)],
            timestamp=now,
        ))
        assert book.best_bid == pytest.approx(99)

    def test_is_ready_after_snapshot(self, book, now):
        assert not book.is_ready()
        asyncio.run(book.apply_snapshot([], [], now))
        assert book.is_ready()

    def test_depth_cap_on_snapshot(self, book, now):
        # depth=5, give 10 levels
        bids = [(100 - i, 1) for i in range(10)]
        asyncio.run(book.apply_snapshot(bids=bids, asks=[], timestamp=now))
        model = asyncio.run(book.to_model())
        assert len(model.bids) <= 5


class TestDelta:
    def test_add_level(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 1)], asks=[(101, 1)], timestamp=now,
        ))
        asyncio.run(book.apply_delta(
            bids=[(99, 2)], asks=[], timestamp=now,
        ))
        model = asyncio.run(book.to_model())
        prices = [b.price for b in model.bids]
        assert 99.0 in prices

    def test_remove_level(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 1), (99, 2)], asks=[], timestamp=now,
        ))
        asyncio.run(book.apply_delta(
            bids=[(100, 0)], asks=[], timestamp=now,
        ))
        assert book.best_bid == pytest.approx(99)

    def test_update_qty(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 1)], asks=[], timestamp=now,
        ))
        asyncio.run(book.apply_delta(
            bids=[(100, 5)], asks=[], timestamp=now,
        ))
        model = asyncio.run(book.to_model())
        assert model.bids[0].qty == pytest.approx(5)

    def test_depth_cap_after_delta(self, book, now):
        bids = [(100 - i, 1) for i in range(10)]
        asyncio.run(book.apply_snapshot(bids=bids, asks=[], timestamp=now))
        asyncio.run(book.apply_delta(bids=[(200, 1)], asks=[], timestamp=now))
        model = asyncio.run(book.to_model())
        assert len(model.bids) <= 5


class TestVWAP:
    def _setup(self, book, now):
        asyncio.run(book.apply_snapshot(
            bids=[(100, 1), (99, 2), (98, 3)],
            asks=[(101, 1), (102, 2), (103, 3)],
            timestamp=now,
        ))

    def test_buy_vwap_small(self, book, now):
        self._setup(book, now)
        # Buying 0.5 — only touches first ask at 101
        price = book.vwap_fill_price('buy', 0.5)
        assert price == pytest.approx(101.0)

    def test_buy_vwap_large(self, book, now):
        self._setup(book, now)
        # Buying 2 — 1@101 + 1@102 → vwap = 101.5
        price = book.vwap_fill_price('buy', 2.0)
        assert price == pytest.approx(101.5)

    def test_sell_vwap(self, book, now):
        self._setup(book, now)
        # Selling 1 — hits best bid at 100
        price = book.vwap_fill_price('sell', 1.0)
        assert price == pytest.approx(100.0)

    def test_insufficient_depth_returns_none(self, book, now):
        self._setup(book, now)
        # Asking to buy 1000 — way more than available
        price = book.vwap_fill_price('buy', 1000.0)
        assert price is None

    def test_mid_price(self, book, now):
        self._setup(book, now)
        assert book.mid_price == pytest.approx(100.5)

    def test_spread(self, book, now):
        self._setup(book, now)
        assert book.spread == pytest.approx(1.0)

    def test_empty_book_returns_none(self, book, now):
        asyncio.run(book.apply_snapshot([], [], now))
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.mid_price is None
        assert book.vwap_fill_price('buy', 1.0) is None

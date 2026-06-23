import pytest
from trading_engine.risk import calc_qty_from_risk, calc_qty_from_notional, validate_stop_target


class TestCalcQtyFromRisk:
    def test_long_basic(self):
        # $10,000 balance, 0.5% risk = $50 at risk
        # Entry 50000, stop 49000 → distance $1000
        # qty = 50 / 1000 = 0.05
        qty = calc_qty_from_risk(
            balance=10_000, risk_pct=0.5,
            entry_price=50_000, stop_price=49_000,
        )
        assert qty == pytest.approx(0.05, rel=1e-4)

    def test_short_basic(self):
        # Entry 50000, stop 51000 → distance 1000 (same regardless of direction)
        qty = calc_qty_from_risk(
            balance=10_000, risk_pct=0.5,
            entry_price=50_000, stop_price=51_000,
        )
        assert qty == pytest.approx(0.05, rel=1e-4)

    def test_tight_stop_larger_qty(self):
        # Tighter stop → more contracts for same dollar risk
        qty = calc_qty_from_risk(
            balance=10_000, risk_pct=1.0,
            entry_price=100, stop_price=99,     # $1 distance
        )
        assert qty == pytest.approx(100.0, rel=1e-4)

    def test_wide_stop_smaller_qty(self):
        qty = calc_qty_from_risk(
            balance=10_000, risk_pct=1.0,
            entry_price=100, stop_price=50,     # $50 distance
        )
        assert qty == pytest.approx(2.0, rel=1e-4)

    def test_zero_balance(self):
        qty = calc_qty_from_risk(
            balance=0, risk_pct=0.5,
            entry_price=50_000, stop_price=49_000,
        )
        assert qty == 0.0

    def test_same_entry_stop_raises(self):
        with pytest.raises(ValueError, match="must differ"):
            calc_qty_from_risk(
                balance=10_000, risk_pct=0.5,
                entry_price=50_000, stop_price=50_000,
            )

    def test_zero_entry_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            calc_qty_from_risk(
                balance=10_000, risk_pct=0.5,
                entry_price=0, stop_price=49_000,
            )


class TestCalcQtyFromNotional:
    def test_basic(self):
        qty = calc_qty_from_notional(notional=5000, price=50_000)
        assert qty == pytest.approx(0.1, rel=1e-4)

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            calc_qty_from_notional(notional=5000, price=0)


class TestValidateStopTarget:
    def test_valid_long(self):
        errs = validate_stop_target('long', entry=100, stop=95, target=110)
        assert errs == []

    def test_valid_short(self):
        errs = validate_stop_target('short', entry=100, stop=105, target=90)
        assert errs == []

    def test_long_stop_above_entry(self):
        errs = validate_stop_target('long', entry=100, stop=105, target=None)
        assert len(errs) == 1
        assert 'stop' in errs[0].lower()

    def test_short_stop_below_entry(self):
        errs = validate_stop_target('short', entry=100, stop=95, target=None)
        assert len(errs) == 1

    def test_long_target_below_entry(self):
        errs = validate_stop_target('long', entry=100, stop=None, target=95)
        assert len(errs) == 1

    def test_short_target_above_entry(self):
        errs = validate_stop_target('short', entry=100, stop=None, target=110)
        assert len(errs) == 1

    def test_multiple_errors(self):
        errs = validate_stop_target('long', entry=100, stop=110, target=90)
        assert len(errs) == 2

    def test_none_stop_and_target(self):
        errs = validate_stop_target('long', entry=100, stop=None, target=None)
        assert errs == []

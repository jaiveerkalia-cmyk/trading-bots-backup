"""
Risk-based position sizing.
qty = risk_amount / |entry_price - stop_price|
"""
from __future__ import annotations


def calc_qty_from_risk(
    balance:     float,
    risk_pct:    float,
    entry_price: float,
    stop_price:  float,
) -> float:
    """
    Returns qty in base asset.
    risk_pct: 0.5 means 0.5% of balance at risk.
    Leverage does not change the qty — the stop distance defines
    the actual dollar risk regardless of leverage.
    """
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("entry_price and stop_price must be > 0")
    if entry_price == stop_price:
        raise ValueError("entry_price and stop_price must differ")
    risk_amount   = balance * (risk_pct / 100.0)
    stop_distance = abs(entry_price - stop_price)
    return round(risk_amount / stop_distance, 8)


def calc_qty_from_notional(notional: float, price: float) -> float:
    """Returns qty in base asset from a quote-currency notional."""
    if price <= 0:
        raise ValueError("price must be > 0")
    return round(notional / price, 8)


def validate_stop_target(
    side:   str,
    entry:  float,
    stop:   float | None,
    target: float | None,
) -> list[str]:
    """Returns list of validation error strings. Empty list = valid."""
    errors = []
    if stop is not None:
        if side == 'long'  and stop >= entry:
            errors.append(f"Long stop ({stop}) must be below entry ({entry})")
        if side == 'short' and stop <= entry:
            errors.append(f"Short stop ({stop}) must be above entry ({entry})")
    if target is not None:
        if side == 'long'  and target <= entry:
            errors.append(f"Long target ({target}) must be above entry ({entry})")
        if side == 'short' and target >= entry:
            errors.append(f"Short target ({target}) must be below entry ({entry})")
    return errors

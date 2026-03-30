"""Tests for Polymarket fee model (fees.py)."""

from __future__ import annotations

import pytest

from wayfinder_paths.adapters.polymarket_adapter.fees import (
    make_polymarket_fee_fn,
    polymarket_fee_rate,
)

# ---------------------------------------------------------------------------
# 1.1  fees_enabled=False always returns 0
# ---------------------------------------------------------------------------


def test_fee_disabled_returns_zero() -> None:
    assert polymarket_fee_rate(0.3, "BUY", fees_enabled=False) == 0.0
    assert polymarket_fee_rate(0.9, "SELL", fees_enabled=False) == 0.0
    assert polymarket_fee_rate(0.0, "BUY", fees_enabled=False) == 0.0


# ---------------------------------------------------------------------------
# 1.2  Fee is non-negative for any price/side combination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,side",
    [
        (0.05, "BUY"),
        (0.50, "BUY"),
        (0.95, "BUY"),
        (0.05, "SELL"),
        (0.50, "SELL"),
        (0.95, "SELL"),
    ],
)
def test_fee_is_nonnegative(price: float, side: str) -> None:
    assert polymarket_fee_rate(price, side) >= 0.0  # type: ignore[arg-type]


def test_fee_scaling_near_resolution() -> None:
    """BUY fee should decrease as price approaches certainty (less upside)."""
    fee_low = polymarket_fee_rate(0.1, "BUY")  # high upside → high fee
    fee_mid = polymarket_fee_rate(0.5, "BUY")
    fee_high = polymarket_fee_rate(0.9, "BUY")  # low upside → low fee
    assert fee_high <= fee_mid <= fee_low


# ---------------------------------------------------------------------------
# 1.3  make_polymarket_fee_fn factory
# ---------------------------------------------------------------------------


def test_fee_fn_factory() -> None:
    fn = make_polymarket_fee_fn(fees_enabled=True)
    assert callable(fn)
    assert fn(0.5, "BUY") >= 0.0
    assert fn(0.5, "BUY") == pytest.approx(
        polymarket_fee_rate(0.5, "BUY", fees_enabled=True)
    )


def test_fee_fn_disabled() -> None:
    fn = make_polymarket_fee_fn(fees_enabled=False)
    assert fn(0.5, "BUY") == 0.0
    assert fn(0.01, "SELL") == 0.0

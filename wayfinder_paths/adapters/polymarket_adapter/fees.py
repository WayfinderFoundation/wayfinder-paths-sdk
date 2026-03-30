"""Polymarket fee model utilities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal


def polymarket_fee_rate(
    price: float,
    side: Literal["BUY", "SELL"],
    fees_enabled: bool = True,
) -> float:
    """Return the Polymarket fee as a fraction of the USDC notional.

    Polymarket charges 2% of potential winnings per share:
      BUY at price p  → fee_rate = 0.02 * (1 - p)
      SELL at price p → fee_rate = 0.02 * p

    This means fees decrease as a market approaches certainty (lower upside
    at high price → lower absolute fee for BUY).
    """
    if not fees_enabled:
        return 0.0
    if side == "BUY":
        return 0.02 * (1.0 - price)
    return 0.02 * price


def make_polymarket_fee_fn(
    fees_enabled: bool = True,
) -> Callable[[float, str], float]:
    """Return a (price, side) → fee_rate callable for BacktestConfig.fee_fn_by_symbol."""

    def fee_fn(price: float, side: str) -> float:
        return polymarket_fee_rate(price, side, fees_enabled=fees_enabled)  # type: ignore[arg-type]

    return fee_fn

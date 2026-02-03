"""Boros-specific utilities (tick math, parsing helpers, conversions)."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

BOROS_TICK_BASE = 1.0001


def tick_from_rate(
    rate: float, tick_step: int, *, round_down: bool, base: float = BOROS_TICK_BASE
) -> int:
    """Convert APR rate to Boros limitTick."""
    if tick_step <= 0:
        tick_step = 1
    ln_base = math.log(base)
    if rate >= 0:
        if rate == 0:
            return 0
        raw = math.log1p(rate) / (tick_step * ln_base)
        return int(math.floor(raw) if round_down else math.ceil(raw))
    # Negative rate
    raw = math.log1p(-rate) / (tick_step * ln_base)
    return -int(math.floor(raw) if round_down else math.ceil(raw))


def rate_from_tick(tick: int, tick_step: int, base: float = BOROS_TICK_BASE) -> float:
    """Convert Boros limitTick to APR rate."""
    if tick_step <= 0:
        tick_step = 1
    p = base ** (abs(int(tick)) * int(tick_step))
    r = p - 1
    return r if tick >= 0 else -r


def normalize_apr(value: Any) -> float | None:
    """Normalize various APR encodings to decimal.

    Handles: decimal (0.1115), percent (11.15), bps (1115), 1e18-scaled.
    """
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None

    if x == 0:
        return None
    # 1e18-scaled decimal
    if x > 1e9:
        return x / 1e18
    # bps (1115 = 11.15%)
    if x > 1000:
        return x / 10_000.0
    # percent (11.15 = 11.15%)
    if x > 1:
        return x / 100.0
    # already decimal
    return x


def cash_wei_to_float(value_wei: Any) -> float:
    """Convert Boros cash units (1e18) to float."""
    if value_wei is None:
        return 0.0
    try:
        return float(Decimal(str(value_wei)) / Decimal(1e18))
    except Exception:
        return 0.0


def market_id_from_market_acc(market_acc: str) -> int | None:
    """Parse a Boros `marketAcc` into a market_id (last 3 bytes)."""
    if not market_acc or len(market_acc) < 8:
        return None
    try:
        market_id = int(market_acc[-6:], 16)
    except ValueError:
        return None
    if market_id == 0xFFFFFF:
        return None
    return market_id

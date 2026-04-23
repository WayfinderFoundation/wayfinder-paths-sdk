"""Pure Uniswap V3 tick math helpers. No I/O, no dependencies beyond stdlib."""

from __future__ import annotations

import math

# Q96 = 2**96
Q96 = 1 << 96

# Uniswap V3 tick range
MIN_TICK = -887272
MAX_TICK = 887272


def tick_to_sqrt_price_x96(tick: int) -> int:
    """Convert a tick to sqrtPriceX96 (Q64.96 fixed-point)."""
    return int(math.sqrt(1.0001**tick) * Q96)


def sqrt_price_x96_to_tick(sqrt_price_x96: int) -> int:
    """Convert sqrtPriceX96 to the nearest tick (rounds down)."""
    price = (sqrt_price_x96 / Q96) ** 2
    if price <= 0:
        return MIN_TICK
    return int(math.floor(math.log(price) / math.log(1.0001)))


def tick_to_price(tick: int) -> float:
    """Convert tick to price (token1/token0)."""
    return 1.0001**tick


def price_to_tick(price: float) -> int:
    """Convert price (token1/token0) to tick (rounds down)."""
    if price <= 0:
        return MIN_TICK
    return int(math.floor(math.log(price) / math.log(1.0001)))


def round_tick_down(tick: int, spacing: int) -> int:
    """Round tick down (toward negative infinity) to nearest multiple of spacing."""
    # Python's // floors toward -inf, which is exactly what we want
    return (tick // spacing) * spacing


def round_tick_up(tick: int, spacing: int) -> int:
    """Round tick up (toward positive infinity) to nearest multiple of spacing."""
    # Ceiling division: -(-tick // spacing) * spacing for positive,
    # but simpler: if already aligned return as-is, otherwise go up
    remainder = tick % spacing
    if remainder == 0:
        return tick
    return tick + (spacing - remainder)


def liquidity_for_amounts(
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    amount0: int,
    amount1: int,
) -> int:
    """Compute liquidity from token amounts given the current price and tick range.

    Follows Uniswap V3 math:
    - If current tick < tickLower: only token0 needed
    - If current tick >= tickUpper: only token1 needed
    - Otherwise: both tokens needed, liquidity is min of both constraints
    """
    sqrt_a = tick_to_sqrt_price_x96(tick_lower)
    sqrt_b = tick_to_sqrt_price_x96(tick_upper)

    if sqrt_price_x96 <= sqrt_a:
        # Below range: only token0
        if amount0 == 0:
            return 0
        return _liquidity_for_amount0(sqrt_a, sqrt_b, amount0)
    elif sqrt_price_x96 >= sqrt_b:
        # Above range: only token1
        if amount1 == 0:
            return 0
        return _liquidity_for_amount1(sqrt_a, sqrt_b, amount1)
    else:
        # In range: both tokens needed
        liq0 = _liquidity_for_amount0(sqrt_price_x96, sqrt_b, amount0)
        liq1 = _liquidity_for_amount1(sqrt_a, sqrt_price_x96, amount1)
        return min(liq0, liq1)


def _liquidity_for_amount0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    """L = amount0 * sqrtA * sqrtB / (sqrtB - sqrtA)"""
    if sqrt_b <= sqrt_a:
        return 0
    numerator = amount0 * sqrt_a * sqrt_b
    denominator = (sqrt_b - sqrt_a) * Q96
    if denominator == 0:
        return 0
    return numerator // denominator


def _liquidity_for_amount1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    """L = amount1 * Q96 / (sqrtB - sqrtA)"""
    if sqrt_b <= sqrt_a:
        return 0
    denominator = sqrt_b - sqrt_a
    if denominator == 0:
        return 0
    return (amount1 * Q96) // denominator


def amounts_for_liquidity(
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    """Compute token amounts for a given liquidity and tick range."""
    sqrt_a = tick_to_sqrt_price_x96(tick_lower)
    sqrt_b = tick_to_sqrt_price_x96(tick_upper)

    if sqrt_price_x96 <= sqrt_a:
        amount0 = _amount0_for_liquidity(sqrt_a, sqrt_b, liquidity)
        return (amount0, 0)
    elif sqrt_price_x96 >= sqrt_b:
        amount1 = _amount1_for_liquidity(sqrt_a, sqrt_b, liquidity)
        return (0, amount1)
    else:
        amount0 = _amount0_for_liquidity(sqrt_price_x96, sqrt_b, liquidity)
        amount1 = _amount1_for_liquidity(sqrt_a, sqrt_price_x96, liquidity)
        return (amount0, amount1)


def _amount0_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """amount0 = L * (sqrtB - sqrtA) / (sqrtA * sqrtB) * Q96"""
    if sqrt_b <= sqrt_a or sqrt_a == 0:
        return 0
    return (liquidity * Q96 * (sqrt_b - sqrt_a)) // (sqrt_a * sqrt_b)


def _amount1_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """amount1 = L * (sqrtB - sqrtA) / Q96"""
    if sqrt_b <= sqrt_a:
        return 0
    return (liquidity * (sqrt_b - sqrt_a)) // Q96


def compute_optimal_amounts(
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    amount0_available: int,
    amount1_available: int,
) -> tuple[int, int]:
    """Compute the maximum amounts that can be deposited given available balances.

    Returns (amount0, amount1) that respect the ratio required by the position.
    """
    liq = liquidity_for_amounts(
        sqrt_price_x96, tick_lower, tick_upper, amount0_available, amount1_available
    )
    if liq == 0:
        return (0, 0)
    return amounts_for_liquidity(sqrt_price_x96, tick_lower, tick_upper, liq)

"""Uniswap v3 math helpers (ported from Wayfinder Django ProjectX implementation).

These utilities are used by the ProjectX (Uniswap v3 fork on HyperEVM) adapter + strategy,
but are chain/protocol-agnostic.
"""

from __future__ import annotations

import math
from decimal import Decimal, getcontext

getcontext().prec = 64

Q96 = Decimal(2) ** 96
Q32 = 1 << 32
TICK_BASE = 1.0001


def price_to_sqrt_price_x96(price: float, decimals0: int, decimals1: int) -> int:
    scale = 10 ** (decimals1 - decimals0)
    p = price * scale
    sqrtp = math.sqrt(p)
    return int(sqrtp * (1 << 96))


def sqrt_price_x96_to_price(sqrtpx96: int, decimals0: int, decimals1: int) -> float:
    if sqrtpx96 <= 0:
        return 0.0
    p = (sqrtpx96 / (1 << 96)) ** 2
    scale = 10 ** (decimals1 - decimals0)
    return p / scale


def price_to_tick(price: float) -> int:
    return math.floor(math.log(price, TICK_BASE))


def tick_to_price(tick: int) -> float:
    return TICK_BASE**tick


def round_tick_to_spacing(tick: int, spacing: int) -> int:
    if spacing <= 0:
        return tick
    return tick - (tick % spacing)


def band_from_bps(mid_price: float, bps_width: float) -> tuple[float, float]:
    lo = mid_price * (1 - bps_width / 10_000)
    hi = mid_price * (1 + bps_width / 10_000)
    return lo, hi


def amt0_for_liq(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    L = Decimal(liquidity)
    out = (L * (b - a) * Q96) / (a * b)
    return int(out)


def amt1_for_liq(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    L = Decimal(liquidity)
    out = (L * (b - a)) / Q96
    return int(out)


def liq_for_amt0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    x = Decimal(amount0)
    L = (x * a * b) / (Q96 * (b - a))
    return int(L)


def liq_for_amt1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    y = Decimal(amount1)
    L = (y * Q96) / (b - a)
    return int(L)


def liq_for_amounts(
    sqrt_p: int, sqrt_a: int, sqrt_b: int, amount0: int, amount1: int
) -> int:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    p = Decimal(sqrt_p)
    if p <= a:
        return liq_for_amt0(a, b, amount0)
    if p >= b:
        return liq_for_amt1(a, b, amount1)
    L0 = liq_for_amt0(p, b, amount0)
    L1 = liq_for_amt1(a, p, amount1)
    return min(L0, L1)


def amounts_for_liq_inrange(
    sqrt_p: int, sqrt_a: int, sqrt_b: int, liquidity: int
) -> tuple[int, int]:
    a, b = _sorted_bounds(sqrt_a, sqrt_b)
    p = Decimal(sqrt_p)
    if p <= a:
        amount0 = amt0_for_liq(a, b, liquidity)
        amount1 = 0
    elif p < b:
        amount0 = amt0_for_liq(p, b, liquidity)
        amount1 = amt1_for_liq(a, p, liquidity)
    else:
        amount0 = 0
        amount1 = amt1_for_liq(a, b, liquidity)
    return amount0, amount1


def sqrt_price_x96_from_tick(
    tick: int, *, min_tick: int = -887272, max_tick: int = 887272
) -> int:
    if tick < min_tick or tick > max_tick:
        raise ValueError(f"tick {tick} out of range [{min_tick}, {max_tick}]")

    abs_tick = tick if tick >= 0 else -tick
    ratio = 0x100000000000000000000000000000000

    if abs_tick & 0x1:
        ratio = (ratio * 0xFFFCB933BD6FAD37AA2D162D1A594001) >> 128
    if abs_tick & 0x2:
        ratio = (ratio * 0xFFF97272373D413259A46990580E213A) >> 128
    if abs_tick & 0x4:
        ratio = (ratio * 0xFFF2E50F5F656932EF12357CF3C7FDCC) >> 128
    if abs_tick & 0x8:
        ratio = (ratio * 0xFFE5CACA7E10E4E61C3624EAA0941CD0) >> 128
    if abs_tick & 0x10:
        ratio = (ratio * 0xFFCB9843D60F6159C9DB58835C926644) >> 128
    if abs_tick & 0x20:
        ratio = (ratio * 0xFF973B41FA98C081472E6896DFB254C0) >> 128
    if abs_tick & 0x40:
        ratio = (ratio * 0xFF2EA16466C96A3843EC78B326B52861) >> 128
    if abs_tick & 0x80:
        ratio = (ratio * 0xFE5DEE046A99A2A811C461F1969C3053) >> 128
    if abs_tick & 0x100:
        ratio = (ratio * 0xFCBE86C7900A88AEDCFFC83B479AA3A4) >> 128
    if abs_tick & 0x200:
        ratio = (ratio * 0xF987A7253AC413176F2B074CF7815E54) >> 128
    if abs_tick & 0x400:
        ratio = (ratio * 0xF3392B0822B70005940C7A398E4B70F3) >> 128
    if abs_tick & 0x800:
        ratio = (ratio * 0xE7159475A2C29B7443B29C7FA6E889D9) >> 128
    if abs_tick & 0x1000:
        ratio = (ratio * 0xD097F3BDFD2022B8845AD8F792AA5825) >> 128
    if abs_tick & 0x2000:
        ratio = (ratio * 0xA9F746462D870FDF8A65DC1F90E061E5) >> 128
    if abs_tick & 0x4000:
        ratio = (ratio * 0x70D869A156D2A1B890BB3DF62BAF32F7) >> 128
    if abs_tick & 0x8000:
        ratio = (ratio * 0x31BE135F97D08FD981231505542FCFA6) >> 128
    if abs_tick & 0x10000:
        ratio = (ratio * 0x9AA508B5B7A84E1C677DE54F3E99BC9) >> 128
    if abs_tick & 0x20000:
        ratio = (ratio * 0x5D6AF8DEDB81196699C329225EE604) >> 128
    if abs_tick & 0x40000:
        ratio = (ratio * 0x2216E584F5FA1EA926041BEDFE98) >> 128
    if abs_tick & 0x80000:
        ratio = (ratio * 0x48A170391F7DC42444E8FA2) >> 128

    if tick > 0:
        ratio = (1 << 256) // ratio

    sqrt_price_x96 = ratio >> 32
    if ratio & (Q32 - 1):
        sqrt_price_x96 += 1
    return int(sqrt_price_x96)


def tick_from_sqrt_price_x96(sqrt_price_x96: float) -> int:
    ratio = float(sqrt_price_x96) / (1 << 96)
    if ratio <= 0:
        return 0
    price = ratio * ratio
    return int(math.log(price) / math.log(TICK_BASE))


def _sorted_bounds(sqrt_a: int, sqrt_b: int) -> tuple[Decimal, Decimal]:
    a, b = sorted((Decimal(sqrt_a), Decimal(sqrt_b)))
    return a, b

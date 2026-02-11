"""Uniswap v3 math helpers and shared on-chain read utilities.

Pure math (tick/price/liquidity conversions) and common NPM contract interactions
used by any Uniswap V3 fork adapter (Uniswap, ProjectX, etc.).
"""

from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal, getcontext
from typing import Any, TypedDict

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.uniswap_v3_abi import UNISWAP_V3_POOL_ABI
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

getcontext().prec = 64

Q128 = 2**128
Q96 = Decimal(2) ** 96
Q32 = 1 << 32
TICK_BASE = 1.0001
MAX_UINT128 = 2**128 - 1
MASK_256 = (1 << 256) - 1


class PositionData(TypedDict):
    nonce: int
    operator: str
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    fee_growth_inside0_last_x128: int
    fee_growth_inside1_last_x128: int
    tokens_owed0: int
    tokens_owed1: int


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


def ticks_for_range(current_tick: int, bps: int, spacing: int) -> tuple[int, int]:
    delta = math.floor(math.log(1 + bps / 10_000, TICK_BASE))
    tick_lower = round_tick_to_spacing(current_tick - delta, spacing)
    tick_upper = round_tick_to_spacing(current_tick + delta, spacing)
    return tick_lower, tick_upper


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


def parse_position_struct(raw: tuple) -> PositionData:
    return PositionData(
        nonce=int(raw[0]),
        operator=to_checksum_address(raw[1]),
        token0=to_checksum_address(raw[2]),
        token1=to_checksum_address(raw[3]),
        fee=int(raw[4]),
        tick_lower=int(raw[5]),
        tick_upper=int(raw[6]),
        liquidity=int(raw[7]),
        fee_growth_inside0_last_x128=int(raw[8]),
        fee_growth_inside1_last_x128=int(raw[9]),
        tokens_owed0=int(raw[10]),
        tokens_owed1=int(raw[11]),
    )


async def read_position(npm_contract, token_id: int) -> PositionData:
    raw = await npm_contract.functions.positions(int(token_id)).call(
        block_identifier="latest"
    )
    return parse_position_struct(raw)


async def enumerate_token_ids(npm_contract, owner: str) -> list[int]:
    balance = await npm_contract.functions.balanceOf(owner).call(
        block_identifier="latest"
    )
    count = int(balance or 0)
    if count <= 0:
        return []
    ids = await asyncio.gather(
        *(
            npm_contract.functions.tokenOfOwnerByIndex(owner, i).call(
                block_identifier="latest"
            )
            for i in range(count)
        )
    )
    return [int(tid) for tid in ids]


async def read_all_positions(
    npm_contract, owner: str
) -> list[tuple[int, PositionData]]:
    token_ids = await enumerate_token_ids(npm_contract, owner)
    if not token_ids:
        return []
    raws = await asyncio.gather(
        *(
            npm_contract.functions.positions(tid).call(block_identifier="latest")
            for tid in token_ids
        )
    )
    return [
        (tid, parse_position_struct(raw))
        for tid, raw in zip(token_ids, raws, strict=True)
    ]


def filter_positions(
    positions: list[tuple[int, PositionData]],
    *,
    token0: str | None = None,
    token1: str | None = None,
    fee: int | None = None,
    active_only: bool = False,
) -> list[tuple[int, PositionData]]:
    result = []
    pool_tokens: set[str] | None = None
    if token0 is not None and token1 is not None:
        pool_tokens = {token0.lower(), token1.lower()}

    for tid, pos in positions:
        if fee is not None and pos["fee"] != fee:
            continue
        if pool_tokens is not None:
            pos_tokens = {pos["token0"].lower(), pos["token1"].lower()}
            if pos_tokens != pool_tokens:
                continue
        if active_only and (
            pos["liquidity"] <= 0
            and pos["tokens_owed0"] <= 0
            and pos["tokens_owed1"] <= 0
        ):
            continue
        result.append((tid, pos))
    return result


async def find_pool(
    factory_contract, token_a: str, token_b: str, fee: int
) -> str | None:
    addr = await factory_contract.functions.getPool(
        to_checksum_address(token_a),
        to_checksum_address(token_b),
        int(fee),
    ).call(block_identifier="latest")
    if not addr or str(addr).lower() == ZERO_ADDRESS.lower():
        return None
    return to_checksum_address(addr)


async def get_pool_slot0(
    pool_address: str,
    chain_id: int,
    token0_decimals: int,
    token1_decimals: int,
) -> dict[str, Any]:
    async with web3_from_chain_id(chain_id) as w3:
        pool = w3.eth.contract(
            address=to_checksum_address(pool_address), abi=UNISWAP_V3_POOL_ABI
        )
        slot0 = await pool.functions.slot0().call()

    sqrt_price_x96 = slot0[0]
    tick = slot0[1]
    price = sqrt_price_x96_to_price(sqrt_price_x96, token0_decimals, token1_decimals)
    return {"sqrt_price_x96": sqrt_price_x96, "tick": tick, "price": price}


def collect_params(token_id: int, recipient: str) -> tuple:
    return (int(token_id), recipient, MAX_UINT128, MAX_UINT128)


def slippage_min(amount: int, slippage_bps: int) -> int:
    bps = max(0, min(10_000, int(slippage_bps)))
    return max(0, (int(amount) * (10_000 - bps)) // 10_000)


def deadline(seconds: int = 300) -> int:
    return int(time.time()) + seconds


def price_to_tick_decimal(
    price: float, token0_decimals: int, token1_decimals: int
) -> int:
    adjusted = price * (10 ** (token0_decimals - token1_decimals))
    if adjusted <= 0:
        raise ValueError("adjusted price must be positive")
    return math.floor(math.log(adjusted, TICK_BASE))


def tick_to_price_decimal(
    tick: int, token0_decimals: int, token1_decimals: int
) -> float:
    raw = TICK_BASE**tick
    return raw / (10 ** (token0_decimals - token1_decimals))

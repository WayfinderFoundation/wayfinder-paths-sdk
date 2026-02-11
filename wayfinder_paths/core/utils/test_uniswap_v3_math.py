from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils.uniswap_v3_math import (
    MAX_UINT128,
    collect_params,
    deadline,
    enumerate_token_ids,
    filter_positions,
    find_pool,
    parse_position_struct,
    price_to_tick,
    price_to_tick_decimal,
    read_all_positions,
    read_position,
    round_tick_to_spacing,
    slippage_min,
    tick_to_price_decimal,
)

MOCK_OWNER = "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
MOCK_TOKEN0 = "0x1111111111111111111111111111111111111111"
MOCK_TOKEN1 = "0x3333333333333333333333333333333333333333"
MOCK_OPERATOR = "0x0000000000000000000000000000000000000000"

RAW_POSITION = (
    0,  # nonce
    MOCK_OPERATOR,  # operator
    MOCK_TOKEN0,  # token0
    MOCK_TOKEN1,  # token1
    3000,  # fee
    -60,  # tickLower
    60,  # tickUpper
    1_000_000,  # liquidity
    100,  # feeGrowthInside0LastX128
    200,  # feeGrowthInside1LastX128
    500,  # tokensOwed0
    600,  # tokensOwed1
)


def test_parse_position_struct():
    pos = parse_position_struct(RAW_POSITION)
    assert pos["nonce"] == 0
    assert pos["token0"].lower() == MOCK_TOKEN0.lower()
    assert pos["token1"].lower() == MOCK_TOKEN1.lower()
    assert pos["fee"] == 3000
    assert pos["tick_lower"] == -60
    assert pos["tick_upper"] == 60
    assert pos["liquidity"] == 1_000_000
    assert pos["fee_growth_inside0_last_x128"] == 100
    assert pos["fee_growth_inside1_last_x128"] == 200
    assert pos["tokens_owed0"] == 500
    assert pos["tokens_owed1"] == 600


def _mock_npm(positions_return=RAW_POSITION, balance=1, token_ids=None):
    if token_ids is None:
        token_ids = [42]

    npm = MagicMock()

    pos_fn = MagicMock()
    pos_fn.call = AsyncMock(return_value=positions_return)
    npm.functions.positions = MagicMock(return_value=pos_fn)

    bal_fn = MagicMock()
    bal_fn.call = AsyncMock(return_value=balance)
    npm.functions.balanceOf = MagicMock(return_value=bal_fn)

    def _token_of_owner(owner, idx):
        fn = MagicMock()
        fn.call = AsyncMock(return_value=token_ids[idx])
        return fn

    npm.functions.tokenOfOwnerByIndex = MagicMock(side_effect=_token_of_owner)
    return npm


@pytest.mark.asyncio
async def test_read_position():
    npm = _mock_npm()
    pos = await read_position(npm, 42)
    assert pos["liquidity"] == 1_000_000
    assert pos["tick_lower"] == -60
    npm.functions.positions.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_enumerate_token_ids():
    npm = _mock_npm(balance=3, token_ids=[10, 20, 30])
    ids = await enumerate_token_ids(npm, MOCK_OWNER)
    assert ids == [10, 20, 30]


@pytest.mark.asyncio
async def test_enumerate_token_ids_empty():
    npm = _mock_npm(balance=0)
    ids = await enumerate_token_ids(npm, MOCK_OWNER)
    assert ids == []


@pytest.mark.asyncio
async def test_read_all_positions():
    npm = _mock_npm(balance=2, token_ids=[10, 20])
    results = await read_all_positions(npm, MOCK_OWNER)
    assert len(results) == 2
    assert results[0][0] == 10
    assert results[1][0] == 20
    assert results[0][1]["liquidity"] == 1_000_000


def _mock_factory(pool_addr):
    factory = MagicMock()
    fn = MagicMock()
    fn.call = AsyncMock(return_value=pool_addr)
    factory.functions.getPool = MagicMock(return_value=fn)
    return factory


@pytest.mark.asyncio
async def test_find_pool_found():
    pool = "0x2222222222222222222222222222222222222222"
    factory = _mock_factory(pool)
    result = await find_pool(factory, MOCK_TOKEN0, MOCK_TOKEN1, 3000)
    assert result is not None
    assert result.lower() == pool.lower()


@pytest.mark.asyncio
async def test_find_pool_not_found():
    factory = _mock_factory(ZERO_ADDRESS)
    result = await find_pool(factory, MOCK_TOKEN0, MOCK_TOKEN1, 3000)
    assert result is None


def test_collect_params():
    params = collect_params(42, MOCK_OWNER)
    assert params == (42, MOCK_OWNER, MAX_UINT128, MAX_UINT128)


def test_slippage_min():
    assert slippage_min(10_000, 30) == 9_970
    assert slippage_min(10_000, 0) == 10_000
    assert slippage_min(10_000, 10_000) == 0
    assert slippage_min(0, 50) == 0


def test_deadline():
    before = int(time.time())
    d = deadline(300)
    after = int(time.time())
    assert before + 300 <= d <= after + 300


def test_deadline_custom():
    before = int(time.time())
    d = deadline(60)
    after = int(time.time())
    assert before + 60 <= d <= after + 60


def test_price_to_tick_decimal():
    tick = price_to_tick_decimal(1.0, 18, 18)
    assert tick == 0

    tick_6_18 = price_to_tick_decimal(1.0, 6, 18)
    tick_raw = price_to_tick(1.0 * 10 ** (6 - 18))
    assert tick_6_18 == tick_raw


def test_price_to_tick_decimal_invalid():
    with pytest.raises(ValueError):
        price_to_tick_decimal(-1.0, 18, 18)


def test_tick_to_price_decimal():
    price = tick_to_price_decimal(0, 18, 18)
    assert abs(price - 1.0) < 1e-9

    price_6_18 = tick_to_price_decimal(0, 6, 18)
    assert abs(price_6_18 - 10**12) < 1e-3


def test_round_tick_to_spacing():
    assert round_tick_to_spacing(23, 10) == 20
    assert round_tick_to_spacing(-23, 10) == -30
    assert round_tick_to_spacing(0, 10) == 0
    assert round_tick_to_spacing(5, 0) == 5


def _pos(
    fee=3000, token0=MOCK_TOKEN0, token1=MOCK_TOKEN1, liquidity=1000, owed0=0, owed1=0
):
    return parse_position_struct(
        (0, MOCK_OPERATOR, token0, token1, fee, -60, 60, liquidity, 0, 0, owed0, owed1)
    )


def test_filter_positions_by_fee():
    positions = [(1, _pos(fee=3000)), (2, _pos(fee=500))]
    result = filter_positions(positions, fee=3000)
    assert len(result) == 1
    assert result[0][0] == 1


def test_filter_positions_by_tokens():
    other = "0x9999999999999999999999999999999999999999"
    positions = [(1, _pos()), (2, _pos(token0=other))]
    result = filter_positions(positions, token0=MOCK_TOKEN0, token1=MOCK_TOKEN1)
    assert len(result) == 1
    assert result[0][0] == 1


def test_filter_positions_active_only():
    positions = [(1, _pos(liquidity=0, owed0=0, owed1=0)), (2, _pos(liquidity=100))]
    result = filter_positions(positions, active_only=True)
    assert len(result) == 1
    assert result[0][0] == 2


def test_filter_positions_active_with_owed():
    positions = [(1, _pos(liquidity=0, owed0=10, owed1=0))]
    result = filter_positions(positions, active_only=True)
    assert len(result) == 1


def test_filter_positions_no_filters():
    positions = [(1, _pos()), (2, _pos(fee=500))]
    result = filter_positions(positions)
    assert len(result) == 2

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.constants.projectx import THBILL_USDC_METADATA
from wayfinder_paths.core.utils.uniswap_v3_math import price_to_sqrt_price_x96
from wayfinder_paths.strategies.projectx_thbill_usdc_strategy.strategy import (
    ProjectXThbillUsdcStrategy,
)
from wayfinder_paths.tests.test_utils import (
    assert_status_dict,
    assert_status_tuple,
    load_strategy_examples,
)


@pytest.fixture
def strategy():
    mock_config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"},
    }
    s = ProjectXThbillUsdcStrategy(config=mock_config)

    # Avoid real network calls
    s.token_adapter.get_token = AsyncMock(return_value=(True, {"decimals": 6}))
    s.token_adapter.get_token_price = AsyncMock(
        return_value=(True, {"current_price": 1.0})
    )

    def get_balance_side_effect(*, wallet_address, token_id=None, **kwargs):
        if token_id == "usd-coin-hyperevm":
            return (True, 10_000_000)  # 10 USDC @ 6dp
        if token_id == "hype-hyperevm":
            return (True, 2_000_000_000_000_000_000)  # 2 HYPE @ 18dp
        return (True, 0)

    s.balance_adapter.get_balance = AsyncMock(side_effect=get_balance_side_effect)
    s.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
        return_value=(True, "0xtxhash_transfer")
    )
    s.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(True, "0xtxhash_transfer")
    )

    s.ledger_adapter.get_strategy_net_deposit = AsyncMock(return_value=(True, 0.0))

    s.projectx.pool_overview = AsyncMock(
        return_value={
            "sqrt_price_x96": 0,
            "tick": 0,
            "tick_spacing": 10,
            "fee": 100,
            "token0": {
                "address": "0x0",
                "decimals": 6,
                "symbol": "USDC",
                "token_id": "usd-coin-hyperevm",
            },
            "token1": {
                "address": "0x1",
                "decimals": 18,
                "symbol": "THBILL",
                "token_id": "theo-short-duration-us-treasury-fund-hyperevm",
            },
        }
    )
    s.projectx.list_positions = AsyncMock(return_value=[])
    s.projectx.mint_from_balances = AsyncMock(
        return_value=(123, "0xtxhash_mint", {"token0_spent": 0, "token1_spent": 0})
    )
    s.projectx.current_balances = AsyncMock(return_value={"0x0": 0, "0x1": 0})
    s.projectx.fetch_swaps = AsyncMock(return_value=[])
    s.projectx.fetch_prjx_points = AsyncMock(return_value={"points": 0})
    s.projectx.live_fee_snapshot = AsyncMock(return_value={"usd": 0.0})

    return s


def test_descriptor_includes_point_rewards():
    rewards = ProjectXThbillUsdcStrategy.INFO.available_rewards or {}
    expected_program = THBILL_USDC_METADATA.get("points_program")
    if expected_program:
        assert rewards.get("point_rewards")
        assert rewards["point_rewards"][0]["program"] == expected_program
    else:
        assert rewards.get("point_rewards") is None


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy):
    examples = load_strategy_examples(Path(__file__))
    deposit_args = examples.get("smoke", {}).get("deposit", {})

    st = await strategy.status()
    assert_status_dict(st)

    ok, msg = await strategy.deposit(**deposit_args)
    assert_status_tuple((ok, msg))

    ok, msg = await strategy.update()
    assert_status_tuple((ok, msg))

    ok, msg = await strategy.exit()
    assert_status_tuple((ok, msg))


@pytest.mark.asyncio
async def test_quote_estimates_fee_apy_from_swap_volume(strategy):
    sqrt_price_x96 = price_to_sqrt_price_x96(1.0, 6, 18)
    strategy.projectx.pool_overview = AsyncMock(
        return_value={
            "sqrt_price_x96": sqrt_price_x96,
            "tick": 0,
            "tick_spacing": 10,
            "fee": 3000,  # 0.30%
            "liquidity": 0,  # simplify share -> 1.0
            "token0": {
                "address": "0x0",
                "decimals": 6,
                "symbol": "USDC",
                "token_id": "usd-coin-hyperevm",
            },
            "token1": {
                "address": "0x1",
                "decimals": 18,
                "symbol": "THBILL",
                "token_id": "theo-short-duration-us-treasury-fund-hyperevm",
            },
        }
    )

    strategy.projectx.fetch_swaps = AsyncMock(
        return_value=[
            {"timestamp": int(time.time()), "tick": 0, "amount_usd": 10_000.0},
            {"timestamp": int(time.time()), "tick": 10, "amount_usd": 5_000.0},
            {"timestamp": int(time.time()), "tick": 100, "amount_usd": 20_000.0},
        ]
    )

    q = await strategy.quote(deposit_amount=1000.0)
    assert q["expected_apy"] == pytest.approx(16.425, rel=1e-6)
    assert q["components"]["volume_usd_in_range"] == pytest.approx(15_000.0, rel=1e-9)

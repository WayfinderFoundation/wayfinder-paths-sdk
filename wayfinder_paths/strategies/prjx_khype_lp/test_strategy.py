from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from wayfinder_paths.strategies.prjx_khype_lp.strategy import PrjxKhypeLpStrategy
from wayfinder_paths.strategies.prjx_khype_lp.tick_math import (
    amounts_for_liquidity,
    liquidity_for_amounts,
    price_to_tick,
    round_tick_down,
    round_tick_up,
    tick_to_price,
    tick_to_sqrt_price_x96,
)
from wayfinder_paths.tests.test_utils import load_strategy_examples

# ── tick math tests (pure, no mocks) ──────────────────────────────────────


def test_tick_to_price_round_trip():
    """Convert tick → price → tick and verify within ±1."""
    for tick in [-1000, -100, 0, 100, 1000, 5000]:
        price = tick_to_price(tick)
        recovered = price_to_tick(price)
        assert abs(recovered - tick) <= 1, (
            f"tick={tick}, price={price}, recovered={recovered}"
        )


def test_round_tick_down():
    spacing = 10
    assert round_tick_down(23, spacing) == 20
    assert round_tick_down(20, spacing) == 20
    assert round_tick_down(0, spacing) == 0
    assert round_tick_down(-5, spacing) == -10
    assert round_tick_down(-10, spacing) == -10
    assert round_tick_down(-13, spacing) == -20


def test_round_tick_up():
    spacing = 10
    assert round_tick_up(23, spacing) == 30
    assert round_tick_up(20, spacing) == 20
    assert round_tick_up(0, spacing) == 0
    assert round_tick_up(-5, spacing) == 0
    assert round_tick_up(-10, spacing) == -10
    assert round_tick_up(-13, spacing) == -10


def test_amounts_for_liquidity_symmetry():
    """When tick is in the middle of range, both amounts should be nonzero."""
    tick_lower = -100
    tick_upper = 100
    # Current tick = 0 (middle of range)
    sqrt_price = tick_to_sqrt_price_x96(0)
    liquidity = 10**18

    amount0, amount1 = amounts_for_liquidity(
        sqrt_price, tick_lower, tick_upper, liquidity
    )
    assert amount0 > 0, "amount0 should be positive when in range"
    assert amount1 > 0, "amount1 should be positive when in range"


def test_amounts_for_liquidity_below_range():
    """When tick is below range, only token0 is needed."""
    tick_lower = 100
    tick_upper = 200
    sqrt_price = tick_to_sqrt_price_x96(0)  # Below range
    liquidity = 10**18

    amount0, amount1 = amounts_for_liquidity(
        sqrt_price, tick_lower, tick_upper, liquidity
    )
    assert amount0 > 0
    assert amount1 == 0


def test_amounts_for_liquidity_above_range():
    """When tick is above range, only token1 is needed."""
    tick_lower = -200
    tick_upper = -100
    sqrt_price = tick_to_sqrt_price_x96(0)  # Above range
    liquidity = 10**18

    amount0, amount1 = amounts_for_liquidity(
        sqrt_price, tick_lower, tick_upper, liquidity
    )
    assert amount0 == 0
    assert amount1 > 0


def test_liquidity_for_amounts():
    """Verify positive liquidity output."""
    sqrt_price = tick_to_sqrt_price_x96(0)
    tick_lower = -100
    tick_upper = 100
    amount0 = 10**18
    amount1 = 10**18

    liq = liquidity_for_amounts(sqrt_price, tick_lower, tick_upper, amount0, amount1)
    assert liq > 0, "liquidity should be positive with nonzero amounts in range"


# ── strategy unit tests (mocked) ─────────────────────────────────────────


@pytest.fixture
def mock_config():
    return {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"},
    }


@pytest.fixture
def strategy(mock_config):
    s = PrjxKhypeLpStrategy(config=mock_config)
    s.balance_adapter = MagicMock()
    s.brap_adapter = MagicMock()
    s.token_adapter = MagicMock()
    s.ledger_adapter = MagicMock()
    return s


@pytest.mark.asyncio
async def test_deposit_below_minimum(strategy):
    ok, msg = await strategy.deposit(main_token_amount=1.0)
    assert ok is False
    assert "Minimum deposit" in msg


@pytest.mark.asyncio
async def test_status_no_position(strategy):
    strategy._position_token_id = None
    strategy.ledger_adapter.get_strategy_net_deposit = AsyncMock(
        return_value=(True, 0.0)
    )
    strategy.balance_adapter.get_balance = AsyncMock(return_value=(True, 0))
    strategy.token_adapter.get_token = AsyncMock(
        return_value=(True, {"price_usd": 25.0})
    )
    # Mock _discover_position to do nothing
    strategy._discover_position = AsyncMock()

    status = await strategy._status()
    assert isinstance(status, dict)
    assert "portfolio_value" in status
    assert "net_deposit" in status
    assert "gas_available" in status
    assert "gassed_up" in status
    assert status["portfolio_value"] == 0.0
    assert status["gassed_up"] is False


@pytest.mark.asyncio
async def test_withdraw_no_position(strategy):
    strategy._position_token_id = None
    strategy._discover_position = AsyncMock()
    ok, msg = await strategy.withdraw()
    assert ok is True
    assert "No active position" in msg


@pytest.mark.asyncio
async def test_update_no_position(strategy):
    strategy._position_token_id = None
    strategy._discover_position = AsyncMock()
    ok, msg = await strategy.update()
    assert ok is True
    assert "No active position" in msg


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke_loads_examples():
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples.get("smoke", {})
    assert "deposit" in smoke_data
    assert smoke_data["deposit"]["main_token_amount"] == 10.0

"""Tests for BorosHypeStrategy."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# Ensure wayfinder-paths is on path for tests.test_utils import
_wayfinder_path_dir = Path(__file__).parent.parent.parent.resolve()
_wayfinder_path_str = str(_wayfinder_path_dir)
if _wayfinder_path_str not in sys.path:
    sys.path.insert(0, _wayfinder_path_str)
elif sys.path.index(_wayfinder_path_str) > 0:
    sys.path.remove(_wayfinder_path_str)
    sys.path.insert(0, _wayfinder_path_str)

import pytest  # noqa: E402

try:
    from tests.test_utils import get_canonical_examples, load_strategy_examples
except ImportError:
    test_utils_path = Path(_wayfinder_path_dir) / "tests" / "test_utils.py"
    spec = importlib.util.spec_from_file_location("tests.test_utils", test_utils_path)
    test_utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_utils)
    get_canonical_examples = test_utils.get_canonical_examples
    load_strategy_examples = test_utils.load_strategy_examples

from wayfinder_paths.strategies.boros_hype_strategy.strategy import (  # noqa: E402
    BorosHypeStrategy,
)


def _mock_balance_transfers(strategy: BorosHypeStrategy) -> None:
    """Avoid on-chain sends by mocking balance adapter transfers in tests."""
    if strategy.ledger_adapter:
        strategy.ledger_adapter.record_strategy_snapshot = AsyncMock(
            return_value=(True, None)
        )

    if strategy.balance_adapter:
        strategy.balance_adapter.get_wallet_balances_multicall = AsyncMock(
            return_value=(
                True,
                [{"success": True, "balance_decimal": 0.0} for _ in range(8)],
            )
        )
        strategy.balance_adapter.get_vault_wallet_balance = AsyncMock(
            return_value=(True, 0)
        )
        strategy.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
            return_value=(True, "0xmock")
        )
        strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
            return_value=(True, "0xmock")
        )
    # Mock Hyperliquid adapter calls that require signing
    if strategy.hyperliquid_adapter:
        strategy.hyperliquid_adapter.ensure_builder_fee_approved = AsyncMock(
            return_value=(True, "mocked")
        )
        strategy.hyperliquid_adapter.update_leverage = AsyncMock(
            return_value=(True, {"status": "ok"})
        )
        strategy.hyperliquid_adapter.get_all_mid_prices = AsyncMock(
            return_value=(True, {"HYPE": 20.0, "USDC": 1.0})
        )
        strategy.hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {"crossMarginSummary": {"accountValue": "0"}, "assetPositions": []},
            )
        )
        strategy.hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(True, {"balances": []})
        )
        strategy.hyperliquid_adapter._coin_to_asset = {"HYPE": 0}
        strategy.hyperliquid_adapter.wait_for_deposit = AsyncMock(
            return_value=(True, 0.0)
        )

    # Mock Boros adapter calls to avoid HTTP requests
    if strategy.boros_adapter:
        strategy.boros_adapter.quote_markets_for_underlying = AsyncMock(
            return_value=(True, [])
        )
        strategy.boros_adapter.get_account_balances = AsyncMock(
            return_value=(True, {"total": 0.0, "cross": 0.0, "isolated": 0.0})
        )
        strategy.boros_adapter.get_pending_withdrawal_amount = AsyncMock(
            return_value=(True, 0.0)
        )
        strategy.boros_adapter.get_active_positions = AsyncMock(return_value=(True, []))

    # Mock on-chain exchange-rate calls
    strategy._get_khype_to_hype_ratio = AsyncMock(return_value=1.0)
    strategy._get_looped_hype_to_hype_ratio = AsyncMock(return_value=1.0)


@pytest.fixture
def strategy():
    """Create a strategy instance for testing with minimal config."""
    mock_config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {
            "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "private_key_hex": "0x" + "ab" * 32,  # Dummy key for testing
        },
    }

    s = BorosHypeStrategy(
        config=mock_config,
    )

    # Mock the Boros adapter
    if hasattr(s, "boros_adapter") and s.boros_adapter:
        s.boros_adapter.quote_markets_for_underlying = AsyncMock(
            return_value=(True, [])
        )
        s.boros_adapter.get_account_balances = AsyncMock(
            return_value=(True, {"total": 0.0, "cross": 0.0, "isolated": 0.0})
        )
        s.boros_adapter.get_active_positions = AsyncMock(return_value=(True, []))

    # Mock the Hyperliquid adapter
    if hasattr(s, "hyperliquid_adapter") and s.hyperliquid_adapter:
        s.hyperliquid_adapter.ensure_builder_fee_approved = AsyncMock(
            return_value=(True, "mocked")
        )

    return s


@pytest.fixture(autouse=True)
def mock_external_apy_fetches(monkeypatch):
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_khype_apy",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_lhype_apy",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy):
    """REQUIRED: Basic smoke test - verifies strategy lifecycle."""
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples["smoke"]

    await strategy.setup()
    _mock_balance_transfers(strategy)

    st = await strategy.status()
    assert isinstance(st, dict)
    assert "portfolio_value" in st or "net_deposit" in st or "strategy_status" in st

    deposit_params = smoke_data.get("deposit", {})
    ok, msg = await strategy.deposit(**deposit_params)
    assert isinstance(ok, bool)
    assert isinstance(msg, str)

    ok, _ = await strategy.update(**smoke_data.get("update", {}))
    assert isinstance(ok, bool)

    ok, msg = await strategy.withdraw(**smoke_data.get("withdraw", {}))
    assert isinstance(ok, bool)


@pytest.mark.asyncio
async def test_canonical_usage(strategy):
    """REQUIRED: Test canonical usage examples from examples.json."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    examples = load_strategy_examples(Path(__file__))
    canonical = get_canonical_examples(examples)

    for example_name, example_data in canonical.items():
        if "deposit" in example_data:
            deposit_params = example_data.get("deposit", {})
            ok, _ = await strategy.deposit(**deposit_params)
            assert ok, f"Canonical example '{example_name}' deposit failed"

        if "update" in example_data:
            ok, msg = await strategy.update()
            assert ok, f"Canonical example '{example_name}' update failed: {msg}"

        if "status" in example_data:
            st = await strategy.status()
            assert isinstance(st, dict), (
                f"Canonical example '{example_name}' status failed"
            )


@pytest.mark.asyncio
async def test_error_cases(strategy):
    """OPTIONAL: Test error scenarios from examples.json."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    examples = load_strategy_examples(Path(__file__))

    for example_name, example_data in examples.items():
        if isinstance(example_data, dict) and "expect" in example_data:
            expect = example_data.get("expect", {})

            if "deposit" in example_data:
                deposit_params = example_data.get("deposit", {})
                ok, _ = await strategy.deposit(**deposit_params)

                if expect.get("success") is False:
                    assert ok is False, (
                        f"Expected {example_name} deposit to fail but it succeeded"
                    )
                elif expect.get("success") is True:
                    assert ok is True, (
                        f"Expected {example_name} deposit to succeed but it failed"
                    )


@pytest.mark.asyncio
async def test_below_minimum_deposit(strategy):
    """Test deposit below minimum threshold fails."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    ok, msg = await strategy.deposit(main_token_amount=50.0, gas_token_amount=0.01)
    assert ok is False
    assert "minimum" in msg.lower() or "150" in msg


@pytest.mark.asyncio
async def test_opa_loop_structure(strategy):
    """Test OPA loop components are properly configured."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    # Verify OPA config
    config = strategy.opa_config
    assert config.max_iterations_per_tick > 0
    assert config.max_steps_per_iteration > 0
    assert config.max_total_steps_per_tick > 0

    # Verify inventory changing ops
    ops = strategy.get_inventory_changing_ops()
    assert len(ops) > 0


@pytest.mark.asyncio
async def test_observe_returns_inventory(strategy):
    """Test observe() returns valid inventory."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    inv = await strategy.observe()
    assert inv is not None
    assert hasattr(inv, "hype_hyperevm_balance")
    assert hasattr(inv, "total_value")
    assert hasattr(inv, "hype_price_usd")


@pytest.mark.asyncio
async def test_plan_returns_plan(strategy):
    """Test plan() returns valid plan."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    inv = await strategy.observe()
    plan = strategy.plan(inv)

    assert plan is not None
    assert hasattr(plan, "steps")
    assert hasattr(plan, "desired_state")


@pytest.mark.asyncio
async def test_status_returns_expected_fields(strategy):
    """Test status() returns expected fields."""
    await strategy.setup()
    _mock_balance_transfers(strategy)

    status = await strategy.status()

    assert "portfolio_value" in status
    assert "strategy_status" in status
    assert "gas_available" in status
    assert "gassed_up" in status

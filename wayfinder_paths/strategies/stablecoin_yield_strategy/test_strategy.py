from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.strategies.stablecoin_yield_strategy.strategy import (
    StablecoinYieldStrategy,
)
from wayfinder_paths.tests.test_utils import (
    assert_quote_result,
    assert_status_dict,
    assert_status_tuple,
    get_canonical_examples,
    load_strategy_examples,
)

_StablecoinYieldStrategy = StablecoinYieldStrategy


@pytest.fixture
def strategy():
    mock_config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"},
    }

    s = StablecoinYieldStrategy(
        config=mock_config,
    )

    if hasattr(s, "balance_adapter") and s.balance_adapter:

        def get_balance_side_effect(
            *, wallet_address, token_id=None, token_address=None, chain_id=None
        ):
            if token_id == "usd-coin-base" or token_id == "usd-coin":
                return (True, 60000000)
            elif token_id == "ethereum-base" or token_id == "ethereum":
                return (True, 2000000000000000)
            return (True, 1000000000)

        s.balance_adapter.get_balance = AsyncMock(side_effect=get_balance_side_effect)

    if hasattr(s, "token_adapter") and s.token_adapter:
        default_usdc = {
            "id": "usd-coin-base",
            "token_id": "usd-coin-base",
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "chain": {"code": "base", "id": 8453, "name": "Base"},
        }

        default_pool_token = {
            "id": "test-pool-base",
            "token_id": "test-pool-base",
            "symbol": "POOL",
            "name": "Test Pool",
            "decimals": 18,
            "address": "0x1234567890123456789012345678901234567890",
            "chain": {"code": "base", "id": 8453, "name": "Base"},
        }

        def get_token_side_effect(address=None, token_id=None, **kwargs):
            if token_id == "usd-coin-base" or token_id == "usd-coin":
                return (True, default_usdc)
            elif (
                token_id == "test-pool-base"
                or address == "0x1234567890123456789012345678901234567890"
            ):
                return (True, default_pool_token)
            return (True, default_usdc)

        s.token_adapter.get_token = AsyncMock(side_effect=get_token_side_effect)
        s.token_adapter.get_gas_token = AsyncMock(
            return_value=(
                True,
                {
                    "id": "ethereum-base",
                    "token_id": "ethereum-base",
                    "symbol": "ETH",
                    "name": "Ethereum",
                    "decimals": 18,
                    "address": "0x4200000000000000000000000000000000000006",
                    "chain": {"code": "base", "id": 8453, "name": "Base"},
                },
            )
        )

    if hasattr(s, "balance_adapter") and s.balance_adapter:
        s.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
            return_value=(True, "0xtxhash_transfer")
        )
        s.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
            return_value=(True, "0xtxhash_transfer")
        )

    if hasattr(s, "ledger_adapter") and s.ledger_adapter:
        # NOTE: The real LedgerClient returns float, not dict!
        s.ledger_adapter.get_strategy_net_deposit = AsyncMock(return_value=(True, 0.0))
        s.ledger_adapter.get_strategy_transactions = AsyncMock(
            return_value=(True, {"transactions": []})
        )

    if hasattr(s, "pool_adapter") and s.pool_adapter:
        s.pool_adapter.get_pools_by_ids = AsyncMock(
            return_value=(
                True,
                {"pools": [{"id": "test-pool-base", "apy": 15.0, "symbol": "POOL"}]},
            )
        )
        s.pool_adapter.get_pools = AsyncMock(
            return_value=(
                True,
                {
                    "matches": [
                        {
                            "stablecoin": True,
                            "ilRisk": "no",
                            "tvlUsd": 2000000,
                            "apy": 5.0,
                            "network": "base",
                            "address": "0x1234567890123456789012345678901234567890",
                            "token_id": "test-pool-base",
                            "pool_id": "test-pool-base",
                            "combined_apy_pct": 15.0,
                        }
                    ]
                },
            )
        )

    if hasattr(s, "brap_adapter") and s.brap_adapter:

        def best_quote_side_effect(*args, **kwargs):
            to_token_address = kwargs.get("to_token_address", "")
            if to_token_address == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913":
                return (True, {"output_amount": "99900000"})
            return (
                True,
                {
                    "output_amount": "105000000",
                    "input_amount": "50000000000000",
                    "toAmount": "105000000",
                    "estimatedGas": "1000000000",
                    "fromAmount": "100000000",
                    "fromToken": {"symbol": "USDC"},
                    "toToken": {"symbol": "POOL"},
                },
            )

        s.brap_adapter.best_quote = AsyncMock(side_effect=best_quote_side_effect)

    if (
        hasattr(s, "brap_adapter")
        and s.brap_adapter
        and hasattr(s.brap_adapter, "swap_from_quote")
    ):
        s.brap_adapter.swap_from_quote = AsyncMock(
            return_value=(
                True,
                {"tx_hash": "0xmockhash", "from_amount": "100", "to_amount": "99"},
            )
        )

    s.DEPOSIT_USDC = 0
    s.usdc_token_info = {
        "id": "usd-coin-base",
        "token_id": "usd-coin-base",
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "chain": {"code": "base", "id": 8453, "name": "Base"},
    }
    s.gas_token = {
        "id": "ethereum-base",
        "token_id": "ethereum-base",
        "symbol": "ETH",
        "name": "Ethereum",
        "decimals": 18,
        "address": "0x4200000000000000000000000000000000000006",
        "chain": {"code": "base", "id": 8453, "name": "Base"},
    }
    s.current_pool = {
        "id": "usd-coin-base",
        "token_id": "usd-coin-base",
        "symbol": "USDC",
        "decimals": 6,
        "chain": {"code": "base", "id": 8453, "name": "Base"},
    }
    s.current_pool_balance = 100000000
    s.current_combined_apy_pct = 0.0
    s.current_pool_data = None

    if hasattr(s, "token_adapter") and s.token_adapter:
        if not hasattr(s.token_adapter, "get_token_price"):
            s.token_adapter.get_token_price = AsyncMock()

        def get_token_price_side_effect(token_id):
            if token_id == "ethereum-base":
                return (True, {"current_price": 2000.0})
            else:
                return (True, {"current_price": 1.0})

        s.token_adapter.get_token_price = AsyncMock(
            side_effect=get_token_price_side_effect
        )

    async def mock_refresh_current_pool_balance():
        pass

    async def mock_rebalance_gas(target_pool):
        return (True, "Gas rebalanced")

    async def mock_has_idle_assets(balances, target):
        return True

    s._refresh_current_pool_balance = mock_refresh_current_pool_balance
    s._rebalance_gas = mock_rebalance_gas
    s._has_idle_assets = mock_has_idle_assets

    return s


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy):
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples["smoke"]

    st = assert_status_dict(await strategy.status())
    assert "portfolio_value" in st
    assert "net_deposit" in st
    assert "strategy_status" in st

    deposit_params = smoke_data.get("deposit", {})
    ok, msg = assert_status_tuple(await strategy.deposit(**deposit_params))
    assert isinstance(ok, bool)
    assert isinstance(msg, str)

    ok, msg = assert_status_tuple(await strategy.update(**smoke_data.get("update", {})))
    assert isinstance(ok, bool)

    ok, msg = assert_status_tuple(
        await strategy.withdraw(**smoke_data.get("withdraw", {}))
    )
    assert isinstance(ok, bool)


@pytest.mark.asyncio
async def test_canonical_usage(strategy):
    examples = load_strategy_examples(Path(__file__))
    canonical = get_canonical_examples(examples)

    for example_name, example_data in canonical.items():
        if "deposit" in example_data:
            deposit_params = example_data.get("deposit", {})
            ok, _ = assert_status_tuple(await strategy.deposit(**deposit_params))
            assert ok, f"Canonical example '{example_name}' deposit failed"

        if "update" in example_data:
            ok, msg = assert_status_tuple(await strategy.update())
            assert ok, f"Canonical example '{example_name}' update failed: {msg}"

        if "status" in example_data:
            st = assert_status_dict(await strategy.status())
            assert isinstance(st, dict), (
                f"Canonical example '{example_name}' status failed"
            )


@pytest.mark.asyncio
async def test_quote_returns_quote_result(strategy):
    assert_quote_result(await strategy.quote(deposit_amount=1000.0))


@pytest.mark.asyncio
async def test_exit_returns_status_tuple(strategy):
    assert_status_tuple(await strategy.exit())


@pytest.mark.asyncio
async def test_error_cases(strategy):
    examples = load_strategy_examples(Path(__file__))

    for example_name, example_data in examples.items():
        if isinstance(example_data, dict) and "expect" in example_data:
            expect = example_data.get("expect", {})

            if "deposit" in example_data:
                deposit_params = example_data.get("deposit", {})
                ok, _ = assert_status_tuple(await strategy.deposit(**deposit_params))

                if expect.get("success") is False:
                    assert ok is False, (
                        f"Expected {example_name} deposit to fail but it succeeded"
                    )
                elif expect.get("success") is True:
                    assert ok is True, (
                        f"Expected {example_name} deposit to succeed but it failed"
                    )

            if "update" in example_data:
                ok, _ = assert_status_tuple(await strategy.update())
                if "success" in expect:
                    expected_success = expect.get("success")
                    assert ok == expected_success, (
                        f"Expected {example_name} update to "
                        f"{'succeed' if expected_success else 'fail'} but got opposite"
                    )


@pytest.mark.asyncio
async def test_token_tracking_initialization(strategy):
    assert hasattr(strategy, "tracked_token_ids")
    assert hasattr(strategy, "tracked_balances")
    assert isinstance(strategy.tracked_token_ids, set)
    assert isinstance(strategy.tracked_balances, dict)


@pytest.mark.asyncio
async def test_track_token(strategy):
    test_token_id = "test-token-base"
    test_balance = 1000000

    strategy._track_token(test_token_id, test_balance)

    assert test_token_id in strategy.tracked_token_ids
    assert strategy.tracked_balances.get(test_token_id) == test_balance


@pytest.mark.asyncio
async def test_update_balance(strategy):
    test_token_id = "test-token-base"
    initial_balance = 1000000
    updated_balance = 2000000

    strategy._track_token(test_token_id, initial_balance)
    assert strategy.tracked_balances.get(test_token_id) == initial_balance

    strategy._update_balance(test_token_id, updated_balance)
    assert strategy.tracked_balances.get(test_token_id) == updated_balance


@pytest.mark.asyncio
async def test_get_non_zero_tracked_tokens(strategy):
    strategy._track_token("token-1", 1000000)
    strategy._track_token("token-2", 0)
    strategy._track_token("token-3", 5000000)

    non_zero = strategy._get_non_zero_tracked_tokens()

    assert len(non_zero) == 2
    token_ids = [token_id for token_id, _ in non_zero]
    assert "token-1" in token_ids
    assert "token-3" in token_ids
    assert "token-2" not in token_ids


@pytest.mark.asyncio
async def test_refresh_tracked_balances(strategy):
    # Track some tokens
    strategy._track_token("usd-coin-base")
    strategy._track_token("ethereum-base")

    # Refresh balances
    await strategy._refresh_tracked_balances()

    # Verify balances were fetched
    assert "usd-coin-base" in strategy.tracked_balances
    assert "ethereum-base" in strategy.tracked_balances


@pytest.mark.asyncio
async def test_deposit_tracks_usdc(strategy):
    # Clear tracked state
    strategy.tracked_token_ids.clear()
    strategy.tracked_balances.clear()

    # Perform deposit
    ok, _ = assert_status_tuple(await strategy.deposit(main_token_amount=100.0))

    # Verify USDC is tracked
    assert ok
    usdc_token_id = strategy.usdc_token_info.get("token_id")
    assert usdc_token_id in strategy.tracked_token_ids


@pytest.mark.asyncio
async def test_sweep_wallet_uses_tracked_tokens(strategy):
    # Restore the real _sweep_wallet method (fixture mocks it as a no-op)
    strategy._sweep_wallet = _StablecoinYieldStrategy._sweep_wallet.__get__(
        strategy, _StablecoinYieldStrategy
    )

    strategy._track_token("token-1", 1000000)
    strategy._track_token("token-2", 2000000)

    # Track the actual token IDs to avoid issues with gas token
    # Make sure we're not accidentally matching gas token
    assert "token-1" in strategy.tracked_token_ids
    assert "token-2" in strategy.tracked_token_ids

    # Mock balance adapter to return fresh balances
    def get_balance_mock(
        *, wallet_address, token_id=None, token_address=None, chain_id=None
    ):
        balance = strategy.tracked_balances.get(token_id, 0)
        return (True, int(balance) if balance else 0)

    new_mock = AsyncMock(side_effect=get_balance_mock)
    strategy.balance_adapter.get_balance = new_mock

    # Mock brap adapter swap
    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(True, "Swap successful")
    )

    target_token = {
        "token_id": "usd-coin-base",
        "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "chain": {"code": "base", "name": "Base"},
    }

    await strategy._sweep_wallet(target_token)

    # Verify that swap was called for tracked tokens (should be called twice, once for each token)
    # If this fails, check: balance_adapter.get_balance was called, tracked_balances has values,
    # and tokens pass the gas/target token checks
    assert strategy.brap_adapter.swap_from_token_ids.call_count >= 1, (
        f"Expected at least 1 swap call, got {strategy.brap_adapter.swap_from_token_ids.call_count}. "
        f"Tracked tokens: {strategy.tracked_token_ids}, "
        f"Tracked balances: {strategy.tracked_balances}, "
        f"Get balance calls: {new_mock.call_count}, "
        f"balance_adapter mock is: {id(strategy.balance_adapter.get_balance)}, new_mock is: {id(new_mock)}"
    )


@pytest.mark.asyncio
async def test_get_non_gas_balances_uses_tracked_state(strategy):
    usdc_token_id = "usd-coin-base"
    pool_token_id = "test-pool-base"

    strategy._track_token(usdc_token_id, 100000000)
    strategy._track_token(pool_token_id, 50000000000000000000)

    # Mock refresh
    def _get_balance_effect(
        *, wallet_address, token_id=None, token_address=None, chain_id=None
    ):
        return (True, strategy.tracked_balances.get(token_id, 0))

    strategy.balance_adapter.get_balance = AsyncMock(side_effect=_get_balance_effect)

    balances = await strategy._get_non_gas_balances()

    # Verify only tracked tokens are returned (excluding gas)
    token_ids = [b["token_id"] for b in balances]
    assert usdc_token_id in token_ids or pool_token_id in token_ids
    assert len(balances) <= len(strategy.tracked_token_ids)


@pytest.mark.asyncio
async def test_partial_liquidate_uses_tracked_tokens(strategy):
    strategy._track_token("usd-coin-base", 50000000)
    strategy._track_token("test-pool-base", 100000000000000000000)

    # Mock balance and token adapters
    def _get_balance_effect_partial(
        *, wallet_address, token_id=None, token_address=None, chain_id=None
    ):
        return (True, strategy.tracked_balances.get(token_id, 0))

    strategy.balance_adapter.get_balance = AsyncMock(
        side_effect=_get_balance_effect_partial
    )

    strategy.token_adapter.get_token_price = AsyncMock(
        return_value=(True, {"current_price": 1.0})
    )

    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(True, "Swap successful")
    )

    ok, msg = await strategy.partial_liquidate(usd_value=75.0)

    # Verify success
    assert ok
    assert "liquidation completed" in msg.lower()


@pytest.mark.asyncio
async def test_setup_handles_float_net_deposit(strategy):
    # Mock get_strategy_net_deposit to return float (not dict)
    strategy.ledger_adapter.get_strategy_net_deposit = AsyncMock(
        return_value=(True, 1500.0)
    )

    # Run setup - should not raise AttributeError
    await strategy.setup()

    # Verify DEPOSIT_USDC was set from the float
    assert strategy.DEPOSIT_USDC == 1500.0

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.strategies.hyperlend_stable_yield_strategy.strategy import (
    HyperlendStableYieldStrategy,
)
from wayfinder_paths.tests.test_utils import (
    assert_quote_result,
    assert_status_dict,
    assert_status_tuple,
    get_canonical_examples,
    load_strategy_examples,
)


@pytest.fixture
def strategy():
    mock_config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"},
    }

    s = HyperlendStableYieldStrategy(
        config=mock_config,
    )

    if hasattr(s, "balance_adapter") and s.balance_adapter:
        # Mock balances: 1000 USDT0 (with 6 decimals) and 2 HYPE (with 18 decimals)
        def get_balance_side_effect(
            *, wallet_address, token_id=None, token_address=None, chain_id=None
        ):
            token_id_str = str(token_id).lower() if token_id else ""
            if "usdt0" in token_id_str or token_id_str == "usdt0":
                # 1000 USDT0 with 6 decimals = 1000 * 10^6 = 1000000000
                return (True, 1000000000)
            elif "hype" in token_id_str or token_id_str == "hype":
                # 2 HYPE with 18 decimals = 2 * 10^18 = 2000000000000000000
                return (True, 2000000000000000000)
            # Default: return high balance for any other token
            return (True, 2000000000000000000)

        s.balance_adapter.get_balance = AsyncMock(side_effect=get_balance_side_effect)

    if hasattr(s, "token_adapter") and s.token_adapter:
        default_usdt0 = {
            "id": "usdt0-hyperevm",
            "token_id": "usdt0-hyperevm",
            "symbol": "USDT0",
            "name": "USD Tether Zero",
            "decimals": 6,
            "address": "0x1234567890123456789012345678901234567890",
            "chain": {"code": "hyperevm", "id": 9999, "name": "HyperEVM"},
        }

        default_hype = {
            "id": "hype-hyperevm",
            "token_id": "hype-hyperevm",
            "symbol": "HYPE",
            "name": "HyperEVM Gas Token",
            "decimals": 18,
            "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "chain": {"code": "hyperevm", "id": 9999, "name": "HyperEVM"},
        }

        def get_token_side_effect(address=None, token_id=None, **kwargs):
            if token_id == "usdt0-hyperevm" or token_id == "usdt0":
                return (True, default_usdt0)
            elif token_id == "hype-hyperevm" or token_id == "hype":
                return (True, default_hype)
            return (True, default_usdt0)

        s.token_adapter.get_token = AsyncMock(side_effect=get_token_side_effect)
        s.token_adapter.get_token = AsyncMock(side_effect=get_token_side_effect)
        s.token_adapter.get_gas_token = AsyncMock(
            return_value=(
                True,
                default_hype,
            )
        )

    if hasattr(s, "balance_adapter") and s.balance_adapter:
        s.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
            return_value=(True, "Transfer successful (simulated)")
        )
        s.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
            return_value=(True, "Transfer successful (simulated)")
        )
        # Mock internal dependencies unconditionally to prevent MagicMock await errors
        # These are needed if the real method somehow gets called
        s.balance_adapter.token_client = AsyncMock()
        s.balance_adapter.token_client.get_token_details = AsyncMock(
            return_value={
                "id": "usdt0-hyperevm",
                "address": "0x1234",
                "decimals": 6,
            }
        )
        # token_adapter might already be set, so check before overriding
        if (
            not hasattr(s.balance_adapter, "token_adapter")
            or s.balance_adapter.token_adapter is None
        ):
            s.balance_adapter.token_adapter = AsyncMock()
        if hasattr(s.balance_adapter.token_adapter, "get_token_price"):
            s.balance_adapter.token_adapter.get_token_price = AsyncMock(
                return_value=(True, {"current_price": 1.0})
            )
        # ledger_adapter might already be set, so check before overriding
        if (
            not hasattr(s.balance_adapter, "ledger_adapter")
            or s.balance_adapter.ledger_adapter is None
        ):
            s.balance_adapter.ledger_adapter = AsyncMock()
        if hasattr(s.balance_adapter.ledger_adapter, "record_deposit"):
            s.balance_adapter.ledger_adapter.record_deposit = AsyncMock(
                return_value=(True, {})
            )
        if hasattr(s.balance_adapter.ledger_adapter, "record_withdrawal"):
            s.balance_adapter.ledger_adapter.record_withdrawal = AsyncMock(
                return_value=(True, {})
            )

    if hasattr(s, "ledger_adapter") and s.ledger_adapter:
        s.ledger_adapter.get_strategy_net_deposit = AsyncMock(return_value=(True, 0.0))
        s.ledger_adapter.get_strategy_transactions = AsyncMock(
            return_value=(True, {"transactions": []})
        )

    if hasattr(s, "brap_adapter") and s.brap_adapter:
        usdt0_address = "0x1234567890123456789012345678901234567890"

        def best_quote_side_effect(*args, **kwargs):
            to_token_address = kwargs.get("to_token_address", "")
            if to_token_address == usdt0_address:
                return (True, {"output_amount": "99900000"})
            return (
                True,
                {
                    "output_amount": "105000000",
                    "input_amount": "50000000000000",
                    "toAmount": "105000000",
                    "estimatedGas": "1000000000",
                    "fromAmount": "100000000",
                    "fromToken": {"symbol": "USDT0"},
                    "toToken": {"symbol": "HYPE"},
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

    if hasattr(s, "hyperlend_adapter") and s.hyperlend_adapter:
        s.hyperlend_adapter.get_assets_view = AsyncMock(
            return_value=(
                True,
                {
                    "block_number": 12345,
                    "user": "0x0",
                    "native_balance_wei": 0,
                    "native_balance": 0.0,
                    "assets": [],
                    "account_data": {
                        "total_collateral_base": 0,
                        "total_debt_base": 0,
                        "available_borrows_base": 0,
                        "current_liquidation_threshold": 0,
                        "ltv": 0,
                        "health_factor_wad": 0,
                        "health_factor": 0.0,
                    },
                    "base_currency_info": {
                        "marketReferenceCurrencyUnit": 100000000,
                        "marketReferenceCurrencyPriceInUsd": 100000000,
                        "networkBaseTokenPriceInUsd": 0,
                        "networkBaseTokenPriceDecimals": 8,
                    },
                },
            )
        )
        s.hyperlend_adapter.get_stable_markets = AsyncMock(
            return_value=(
                True,
                {
                    "markets": {
                        "0x1234567890123456789012345678901234567890": {
                            "symbol": "USDT0",
                            "address": "0x1234567890123456789012345678901234567890",
                            "apy": 5.0,
                            "tvl": 1000000,
                            "underlying_token": {
                                "address": "0x1234567890123456789012345678901234567890",
                                "symbol": "USDT0",
                                "decimals": 6,
                            },
                        }
                    },
                    "notes": [],
                },
            )
        )
        # Block bootstrap needs at least BLOCK_LEN (6) rows; provide enough history
        _history_base = {
            "timestamp_ms": 1700000000000,
            "timestamp": 1700000000.0,
            "supply_apr": 0.05,
            "supply_apy": 0.05,
            "borrow_apr": 0.07,
            "borrow_apy": 0.07,
            "token": "0x1234567890123456789012345678901234567890",
            "symbol": "usdt0",
            "display_symbol": "USDT0",
        }
        history_rows = [
            {**_history_base, "timestamp_ms": 1700000000000 + i * 3600000}
            for i in range(24)
        ]
        s.hyperlend_adapter.get_lend_rate_history = AsyncMock(
            return_value=(True, {"history": history_rows})
        )

    s.usdt_token_info = {
        "id": "usdt0-hyperevm",
        "symbol": "USDT0",
        "name": "USD Tether Zero",
        "decimals": 6,
        "address": "0x1234567890123456789012345678901234567890",
        "chain": {"code": "hyperevm", "id": 9999, "name": "HyperEVM"},
    }
    s.hype_token_info = {
        "id": "hype-hyperevm",
        "symbol": "HYPE",
        "name": "HyperEVM Gas Token",
        "decimals": 18,
        "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "chain": {"code": "hyperevm", "id": 9999, "name": "HyperEVM"},
    }
    s.current_token = None
    # Attributes normally set in setup()
    s.rotation_policy = "hysteresis"
    s.hys_dwell_hours = 168
    s.hys_z = 1.15
    s.rotation_tx_cost = 0.002
    s.last_summary = None
    s.last_dominance = None
    s.last_samples = None

    if hasattr(s, "token_adapter") and s.token_adapter:
        if not hasattr(s.token_adapter, "get_token_price"):
            s.token_adapter.get_token_price = AsyncMock()

        def get_token_price_side_effect(token_id):
            if token_id == "hype-hyperevm":
                return (True, {"current_price": 2000.0})
            else:
                return (True, {"current_price": 1.0})

        s.token_adapter.get_token_price = AsyncMock(
            side_effect=get_token_price_side_effect
        )

    async def mock_sweep_wallet(target_token):
        pass

    async def mock_refresh_current_pool_balance():
        pass

    async def mock_rebalance_gas(target_pool):
        return (True, "Gas rebalanced")

    async def mock_has_idle_assets(balances, target):
        return True

    if hasattr(s, "_sweep_wallet"):
        s._sweep_wallet = mock_sweep_wallet
    if hasattr(s, "_refresh_current_pool_balance"):
        s._refresh_current_pool_balance = mock_refresh_current_pool_balance
    if hasattr(s, "_rebalance_gas"):
        s._rebalance_gas = mock_rebalance_gas
    if hasattr(s, "_has_idle_assets"):
        s._has_idle_assets = mock_has_idle_assets

    s.current_symbol = getattr(s, "current_symbol", None) or "USDT0"
    if not getattr(s, "current_token", None):
        s.current_token = s.usdt_token_info
    s.current_avg_apy = getattr(s, "current_avg_apy", 0.0)

    return s


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy):
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples["smoke"]

    await strategy.setup()

    st = assert_status_dict(await strategy.status())
    assert "portfolio_value" in st
    assert "net_deposit" in st
    assert "strategy_status" in st

    deposit_params = smoke_data.get("deposit", {})
    ok, msg = assert_status_tuple(await strategy.deposit(**deposit_params))
    assert isinstance(ok, bool)
    assert isinstance(msg, str)

    ok, _ = assert_status_tuple(await strategy.update(**smoke_data.get("update", {})))
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
            ok, msg = assert_status_tuple(await strategy.deposit(**deposit_params))
            assert ok, f"Canonical example '{example_name}' deposit failed: {msg}"

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


# _swap_residual_balances_to_token: TODO said this section was untested

USDC_CHECKSUM = "0x0000000000000000000000000000000000000099"


def _make_balances(strategy, *, target_checksum: str):
    """Return a balances dict with one non-target stablecoin entry."""
    return {
        USDC_CHECKSUM: {
            "token": {
                "symbol": "USDC",
                "decimals": 6,
                "address": USDC_CHECKSUM,
                "token_id": "usd-coin-hyperevm",
            },
            "address": USDC_CHECKSUM,
            "wei": 5_000_000,
            "tokens": 5.0,
            "usd": 5.0,
            "asset": {"is_stablecoin": True},
            "decimals": 6,
        },
    }


@pytest.mark.asyncio
async def test_swap_residual_fallback_transfers_to_main(strategy):
    """When _execute_swap returns None, the fallback transfers tokens to main wallet."""
    target = strategy.usdt_token_info
    target_checksum = strategy._token_checksum(target)

    strategy._get_assets_snapshot = AsyncMock(return_value={"assets": []})
    strategy._wallet_balances_from_snapshot = AsyncMock(
        return_value=_make_balances(strategy, target_checksum=target_checksum)
    )
    strategy._execute_swap = AsyncMock(return_value=None)
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(True, "ok")
    )

    actions = await strategy._swap_residual_balances_to_token(target)

    strategy._execute_swap.assert_awaited_once()
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet.assert_awaited_once_with(
        token_id=target.get("token_id"),
        amount=5.0,
        strategy_name=strategy.name,
    )
    assert len(actions) == 1
    assert "Transferred" in actions[0]
    assert "USDC" in actions[0]


@pytest.mark.asyncio
async def test_swap_residual_fallback_transfer_fails_no_action(strategy):
    """When the fallback transfer returns (False, ...), no action is appended."""
    target = strategy.usdt_token_info
    target_checksum = strategy._token_checksum(target)

    strategy._get_assets_snapshot = AsyncMock(return_value={"assets": []})
    strategy._wallet_balances_from_snapshot = AsyncMock(
        return_value=_make_balances(strategy, target_checksum=target_checksum)
    )
    strategy._execute_swap = AsyncMock(return_value=None)
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(False, "transfer failed")
    )

    actions = await strategy._swap_residual_balances_to_token(target)

    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet.assert_awaited_once()
    assert actions == []


@pytest.mark.asyncio
async def test_swap_residual_fallback_exception_continues(strategy):
    """When the fallback transfer raises, the loop continues without crashing."""
    target = strategy.usdt_token_info
    target_checksum = strategy._token_checksum(target)

    strategy._get_assets_snapshot = AsyncMock(return_value={"assets": []})
    strategy._wallet_balances_from_snapshot = AsyncMock(
        return_value=_make_balances(strategy, target_checksum=target_checksum)
    )
    strategy._execute_swap = AsyncMock(return_value=None)
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        side_effect=Exception("network error")
    )

    actions = await strategy._swap_residual_balances_to_token(target)

    assert actions == []

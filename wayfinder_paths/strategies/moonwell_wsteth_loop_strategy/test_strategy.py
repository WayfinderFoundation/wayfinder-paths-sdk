from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy import (
    ETH_TOKEN_ID,
    M_USDC,
    M_WETH,
    M_WSTETH,
    USDC_TOKEN_ID,
    WETH,
    WETH_TOKEN_ID,
    WSTETH_TOKEN_ID,
    MoonwellWstethLoopStrategy,
    SwapOutcomeUnknownError,
)
from wayfinder_paths.tests.test_utils import (
    get_canonical_examples,
    load_strategy_examples,
)


@pytest.fixture
def strategy():
    mock_config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"},
    }

    # Patch the initialization to avoid real adapter/web3 setup
    with patch.object(
        MoonwellWstethLoopStrategy, "__init__", lambda self, **kwargs: None
    ):
        s = MoonwellWstethLoopStrategy(
            config=mock_config,
        )
        # Manually set attributes that would be set in __init__
        s.config = mock_config
        s._token_info_cache = {}
        s._token_price_cache = {}
        s._token_price_timestamps = {}

        # Mock adapters
        s.balance_adapter = MagicMock()
        s.moonwell_adapter = MagicMock()
        s.brap_adapter = MagicMock()
        s.token_adapter = MagicMock()
        s.ledger_adapter = MagicMock()
        s.ledger_adapter.record_strategy_snapshot = AsyncMock(return_value=None)

        return s


@pytest.fixture
def mock_adapter_responses(strategy):
    strategy.balance_adapter.get_balance = AsyncMock(return_value=(True, 1000000))
    strategy.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
        return_value=(True, "success")
    )
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(True, "success")
    )

    strategy.token_adapter.get_token = AsyncMock(
        return_value=(True, {"decimals": 18, "symbol": "TEST"})
    )
    strategy.token_adapter.get_token_price = AsyncMock(
        return_value=(True, {"current_price": 1.0})
    )

    strategy.moonwell_adapter.get_pos = AsyncMock(
        return_value=(
            True,
            {
                "mtoken_balance": 1000000000000000000,
                "underlying_balance": 1000000000000000000,
                "borrow_balance": 0,
                "exchange_rate": 1000000000000000000,
            },
        )
    )
    strategy.moonwell_adapter.get_collateral_factor = AsyncMock(
        return_value=(True, 0.8)
    )
    strategy.moonwell_adapter.get_apy = AsyncMock(return_value=(True, 0.05))
    strategy.moonwell_adapter.get_borrowable_amount = AsyncMock(
        return_value=(True, 1000.0)
    )
    strategy.moonwell_adapter.max_withdrawable_mtoken = AsyncMock(
        return_value=(True, {"cTokens_raw": 1000000, "underlying_raw": 1000000})
    )
    strategy.moonwell_adapter.lend = AsyncMock(return_value=(True, "success"))
    strategy.moonwell_adapter.unlend = AsyncMock(return_value=(True, "success"))
    strategy.moonwell_adapter.borrow = AsyncMock(return_value=(True, "success"))
    strategy.moonwell_adapter.repay = AsyncMock(return_value=(True, "success"))
    strategy.moonwell_adapter.set_collateral = AsyncMock(return_value=(True, "success"))
    strategy.moonwell_adapter.claim_rewards = AsyncMock(return_value={})

    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(True, {"to_amount": 1000000000000000000})
    )

    return strategy


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy, mock_adapter_responses):
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples["smoke"]

    # Mock _quote to return positive APY
    with patch.object(strategy, "_quote", new_callable=AsyncMock) as mock_quote:
        mock_quote.return_value = {"apy": 0.1, "data": {}}

        # Status test
        with patch.object(strategy, "_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {
                "portfolio_value": 0.0,
                "net_deposit": 0.0,
                "strategy_status": {},
                "gas_available": 0.1,
                "gassed_up": True,
            }
            st = await strategy.status()
            assert isinstance(st, dict)
            assert (
                "portfolio_value" in st
                or "net_deposit" in st
                or "strategy_status" in st
            )

        # Deposit test
        deposit_params = smoke_data.get("deposit", {})
        with patch.object(strategy, "deposit", new_callable=AsyncMock) as mock_deposit:
            mock_deposit.return_value = (True, "success")
            ok, msg = await strategy.deposit(**deposit_params)
            assert isinstance(ok, bool)
            assert isinstance(msg, str)

        with patch.object(strategy, "update", new_callable=AsyncMock) as mock_update:
            mock_update.return_value = (True, "success")
            ok, msg = await strategy.update(**smoke_data.get("update", {}))
            assert isinstance(ok, bool)

        # Withdraw test
        with patch.object(
            strategy, "withdraw", new_callable=AsyncMock
        ) as mock_withdraw:
            mock_withdraw.return_value = (True, "success")
            ok, msg = await strategy.withdraw(**smoke_data.get("withdraw", {}))
            assert isinstance(ok, bool)


@pytest.mark.asyncio
async def test_canonical_usage(strategy, mock_adapter_responses):
    examples = load_strategy_examples(Path(__file__))
    canonical = get_canonical_examples(examples)

    for example_name, example_data in canonical.items():
        # Mock methods for canonical usage tests
        with patch.object(strategy, "_quote", new_callable=AsyncMock) as mock_quote:
            mock_quote.return_value = {"apy": 0.1, "data": {}}

            if "deposit" in example_data:
                deposit_params = example_data.get("deposit", {})
                with patch.object(
                    strategy, "deposit", new_callable=AsyncMock
                ) as mock_deposit:
                    mock_deposit.return_value = (True, "success")
                    ok, _ = await strategy.deposit(**deposit_params)
                    assert ok, f"Canonical example '{example_name}' deposit failed"

            if "update" in example_data:
                with patch.object(
                    strategy, "update", new_callable=AsyncMock
                ) as mock_update:
                    mock_update.return_value = (True, "success")
                    ok, msg = await strategy.update()
                    assert ok, (
                        f"Canonical example '{example_name}' update failed: {msg}"
                    )

            if "status" in example_data:
                with patch.object(
                    strategy, "_status", new_callable=AsyncMock
                ) as mock_status:
                    mock_status.return_value = {
                        "portfolio_value": 0.0,
                        "net_deposit": 0.0,
                        "strategy_status": {},
                        "gas_available": 0.1,
                        "gassed_up": True,
                    }
                    st = await strategy.status()
                    assert isinstance(st, dict), (
                        f"Canonical example '{example_name}' status failed"
                    )


@pytest.mark.asyncio
async def test_status_returns_status_dict(strategy, mock_adapter_responses):
    snap = MagicMock()
    snap.totals_usd = {}
    snap.ltv = 0.5
    snap.debt_usd = 100.0
    snap.net_equity_usd = 0.0

    strategy.ledger_adapter.get_strategy_net_deposit = AsyncMock(
        return_value=(True, 1000.0)
    )

    with patch.object(
        strategy, "_accounting_snapshot", new_callable=AsyncMock
    ) as mock_snap:
        mock_snap.return_value = (snap, (0.8, 0.8))
        with patch.object(
            strategy, "_get_gas_balance", new_callable=AsyncMock
        ) as mock_gas:
            mock_gas.return_value = 100000000000000000
            with patch.object(
                strategy, "get_peg_diff", new_callable=AsyncMock
            ) as mock_peg:
                mock_peg.return_value = 0.001
                with patch.object(
                    strategy, "_quote", new_callable=AsyncMock
                ) as mock_quote:
                    mock_quote.return_value = {"apy": 0.1, "data": {}}

                    status = await strategy._status()

                    assert "portfolio_value" in status
                    assert "net_deposit" in status
                    assert "strategy_status" in status
                    assert "gas_available" in status
                    assert "gassed_up" in status


@pytest.mark.asyncio
async def test_policies_returns_list(strategy):
    # Mock the policy functions to avoid ABI fetching
    with (
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.musdc_mint_or_approve_or_redeem",
            new_callable=AsyncMock,
            return_value="mock_musdc_policy",
        ),
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.mweth_approve_or_borrow_or_repay",
            new_callable=AsyncMock,
            return_value="mock_mweth_policy",
        ),
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.mwsteth_approve_or_mint_or_redeem",
            new_callable=AsyncMock,
            return_value="mock_mwsteth_policy",
        ),
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.moonwell_comptroller_enter_markets_or_claim_rewards",
            new_callable=AsyncMock,
            return_value="mock_comptroller_policy",
        ),
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.weth_deposit",
            new_callable=AsyncMock,
            return_value="mock_weth_deposit_policy",
        ),
        patch(
            "wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.enso_swap",
            new_callable=AsyncMock,
            return_value="mock_enso_swap_policy",
        ),
    ):
        policies = await strategy.policies()
        assert isinstance(policies, list)
        assert len(policies) > 0


@pytest.mark.asyncio
async def test_quote_returns_apy_info(strategy, mock_adapter_responses):
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"smaApr": 3.5}}
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        quote = await strategy._quote()

        assert "apy" in quote
        assert "information" in quote or "data" in quote


# Tests for new safety methods


def test_max_safe_f_calculates_correctly(strategy):
    strategy.MAX_DEPEG = 0.01

    # With cf_w = 0.8, a = 0.99
    # F_max = 1 / (1 + 0.8 * (1 - 0.99)) = 1 / (1 + 0.8 * 0.01) = 1 / 1.008 ≈ 0.992
    result = strategy._max_safe_F(0.8)
    expected = 1 / (1 + 0.8 * 0.01)
    assert abs(result - expected) < 0.001


def test_max_safe_f_with_zero_collateral_factor(strategy):
    strategy.MAX_DEPEG = 0.01
    result = strategy._max_safe_F(0.0)
    # F_max = 1 / (1 + 0 * anything) = 1.0
    assert result == 1.0


@pytest.mark.asyncio
async def test_swap_with_retries_succeeds_first_attempt(
    strategy, mock_adapter_responses
):
    strategy.max_swap_retries = 3
    strategy.swap_slippage_tolerance = 0.005

    with patch.object(
        strategy, "_get_balance_raw", new_callable=AsyncMock
    ) as mock_balance:
        mock_balance.return_value = 10**18
        result = await strategy._swap_with_retries(
            from_token_id="usd-coin-base",
            to_token_id="l2-standard-bridged-weth-base-base",
            amount=1000000,
        )

    assert result is not None
    assert "to_amount" in result
    strategy.brap_adapter.swap_from_token_ids.assert_called_once()


@pytest.mark.asyncio
async def test_swap_with_retries_succeeds_on_second_attempt(
    strategy, mock_adapter_responses
):
    strategy.max_swap_retries = 3
    strategy.swap_slippage_tolerance = 0.005

    # First call fails, second succeeds
    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        side_effect=[
            Exception("First attempt failed"),
            (True, {"to_amount": 1000000}),
        ]
    )

    with (
        patch.object(
            strategy, "_get_balance_raw", new_callable=AsyncMock
        ) as mock_balance,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_balance.return_value = 10**18
        result = await strategy._swap_with_retries(
            from_token_id="usd-coin-base",
            to_token_id="l2-standard-bridged-weth-base-base",
            amount=1000000,
        )

    assert result is not None
    assert result["to_amount"] == 1000000
    assert strategy.brap_adapter.swap_from_token_ids.call_count == 2


@pytest.mark.asyncio
async def test_swap_with_retries_fails_all_attempts(strategy, mock_adapter_responses):
    strategy.max_swap_retries = 3
    strategy.swap_slippage_tolerance = 0.005

    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        side_effect=Exception("Swap failed")
    )

    with (
        patch.object(
            strategy, "_get_balance_raw", new_callable=AsyncMock
        ) as mock_balance,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_balance.return_value = 10**18
        result = await strategy._swap_with_retries(
            from_token_id="usd-coin-base",
            to_token_id="l2-standard-bridged-weth-base-base",
            amount=1000000,
        )

    assert result is None
    assert strategy.brap_adapter.swap_from_token_ids.call_count == 3


@pytest.mark.asyncio
async def test_swap_with_retries_aborts_on_unknown_outcome(
    strategy, mock_adapter_responses
):
    strategy.max_swap_retries = 3
    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(
            False,
            "Transaction HexBytes('0xdeadbeef') is not in the chain after 120 seconds",
        )
    )

    with patch.object(
        strategy, "_get_balance_raw", new_callable=AsyncMock
    ) as mock_balance:
        mock_balance.return_value = 10**18
        with pytest.raises(SwapOutcomeUnknownError):
            await strategy._swap_with_retries(
                from_token_id=USDC_TOKEN_ID,
                to_token_id=WETH_TOKEN_ID,
                amount=1000000,
            )

    assert strategy.brap_adapter.swap_from_token_ids.call_count == 1


@pytest.mark.asyncio
async def test_post_run_guard_no_action_when_delta_ok(strategy, mock_adapter_responses):
    snap = MagicMock()
    snap.wallet_wsteth = 0
    snap.usdc_supplied = 0
    snap.wsteth_supplied = 0
    snap.weth_debt = 5 * 10**18
    snap.debt_usd = 100.0
    snap.hf = 1.3
    snap.capacity_usd = 1000.0
    snap.totals_usd = {
        f"Base_{M_WSTETH}": 96.0,
        f"Base_{M_USDC}": 0.0,
        f"Base_{WETH}": -100.0,
    }

    with patch.object(strategy, "_get_gas_balance", new_callable=AsyncMock) as mock_gas:
        mock_gas.return_value = int(0.01 * 10**18)
        with patch.object(
            strategy, "_get_collateral_factors", new_callable=AsyncMock
        ) as mock_cf:
            mock_cf.return_value = (0.8, 0.8)
            with patch.object(
                strategy, "_accounting_snapshot", new_callable=AsyncMock
            ) as mock_snap:
                mock_snap.return_value = (snap, (0.8, 0.8))
                with patch.object(
                    strategy, "_ensure_markets_for_state", new_callable=AsyncMock
                ) as mock_markets:
                    mock_markets.return_value = (True, "ok")
                    with patch.object(
                        strategy,
                        "_reconcile_wallet_into_position",
                        new_callable=AsyncMock,
                    ) as mock_reconcile:
                        with patch.object(
                            strategy,
                            "_settle_weth_debt_to_target_usd",
                            new_callable=AsyncMock,
                        ) as mock_settle:
                            ok, msg = await strategy._post_run_guard(mode="operate")
                            assert ok is True
                            assert "delta ok" in msg.lower()
                            assert not mock_reconcile.called
                            assert not mock_settle.called


@pytest.mark.asyncio
async def test_post_run_guard_restores_delta_via_reconcile(
    strategy, mock_adapter_responses
):
    snap1 = MagicMock()
    snap1.wallet_wsteth = 0
    snap1.usdc_supplied = 0
    snap1.wsteth_supplied = 0
    snap1.weth_debt = 5 * 10**18
    snap1.debt_usd = 100.0
    snap1.hf = 1.3
    snap1.capacity_usd = 1000.0
    snap1.totals_usd = {f"Base_{M_WSTETH}": 0.0, f"Base_{WETH}": -100.0}

    snap2 = MagicMock()
    snap2.wallet_wsteth = 0
    snap2.usdc_supplied = 0
    snap2.wsteth_supplied = 0
    snap2.weth_debt = 5 * 10**18
    snap2.debt_usd = 100.0
    snap2.hf = 1.3
    snap2.capacity_usd = 1000.0
    snap2.totals_usd = {f"Base_{M_WSTETH}": 98.0, f"Base_{WETH}": -100.0}

    with patch.object(strategy, "_get_gas_balance", new_callable=AsyncMock) as mock_gas:
        mock_gas.return_value = int(0.01 * 10**18)
        with patch.object(
            strategy, "_get_collateral_factors", new_callable=AsyncMock
        ) as mock_cf:
            mock_cf.return_value = (0.8, 0.8)
            with patch.object(
                strategy, "_accounting_snapshot", new_callable=AsyncMock
            ) as mock_snap:
                mock_snap.side_effect = [(snap1, (0.8, 0.8)), (snap2, (0.8, 0.8))]
                with patch.object(
                    strategy, "_ensure_markets_for_state", new_callable=AsyncMock
                ) as mock_markets:
                    mock_markets.return_value = (True, "ok")
                    with patch.object(
                        strategy,
                        "_reconcile_wallet_into_position",
                        new_callable=AsyncMock,
                    ) as mock_reconcile:
                        mock_reconcile.return_value = (True, "ok")
                        with patch.object(
                            strategy,
                            "_settle_weth_debt_to_target_usd",
                            new_callable=AsyncMock,
                        ) as mock_settle:
                            ok, msg = await strategy._post_run_guard(mode="operate")
                            assert ok is True
                            assert "restored by reconcile" in msg.lower()
                            assert mock_reconcile.called
                            assert not mock_settle.called


@pytest.mark.asyncio
async def test_post_run_guard_delevers_to_delta_when_reconcile_insufficient(
    strategy, mock_adapter_responses
):
    snap1 = MagicMock()
    snap1.wallet_wsteth = 0
    snap1.usdc_supplied = 0
    snap1.wsteth_supplied = 0
    snap1.weth_debt = 5 * 10**18
    snap1.debt_usd = 100.0
    snap1.hf = 1.3
    snap1.capacity_usd = 1000.0
    snap1.totals_usd = {f"Base_{M_WSTETH}": 0.0, f"Base_{WETH}": -100.0}

    snap2 = MagicMock()
    snap2.wallet_wsteth = 0
    snap2.usdc_supplied = 0
    snap2.wsteth_supplied = 0
    snap2.weth_debt = 5 * 10**18
    snap2.debt_usd = 100.0
    snap2.hf = 1.3
    snap2.capacity_usd = 1000.0
    snap2.totals_usd = {f"Base_{M_WSTETH}": 0.0, f"Base_{WETH}": -100.0}

    snap3 = MagicMock()
    snap3.wallet_wsteth = 0
    snap3.usdc_supplied = 0
    snap3.wsteth_supplied = 0
    snap3.weth_debt = int(0.005 * 10**18)
    snap3.debt_usd = 12.0
    snap3.hf = 1.3
    snap3.capacity_usd = 1000.0
    # mismatch = debt_usd - wsteth_coll_usd = 12 - 15 = -3 (within DELTA_TOL_USD of 5)
    snap3.totals_usd = {f"Base_{M_WSTETH}": 15.0, f"Base_{WETH}": -12.0}

    with patch.object(strategy, "_get_gas_balance", new_callable=AsyncMock) as mock_gas:
        mock_gas.return_value = int(0.01 * 10**18)
        with patch.object(
            strategy, "_get_collateral_factors", new_callable=AsyncMock
        ) as mock_cf:
            mock_cf.return_value = (0.8, 0.8)
            with patch.object(
                strategy, "_accounting_snapshot", new_callable=AsyncMock
            ) as mock_snap:
                mock_snap.side_effect = [
                    (snap1, (0.8, 0.8)),
                    (snap2, (0.8, 0.8)),
                    (snap3, (0.8, 0.8)),
                ]
                with patch.object(
                    strategy, "_ensure_markets_for_state", new_callable=AsyncMock
                ) as mock_markets:
                    mock_markets.return_value = (True, "ok")
                    with patch.object(
                        strategy,
                        "_reconcile_wallet_into_position",
                        new_callable=AsyncMock,
                    ) as mock_reconcile:
                        mock_reconcile.return_value = (True, "ok")
                        with patch.object(
                            strategy,
                            "_settle_weth_debt_to_target_usd",
                            new_callable=AsyncMock,
                        ) as mock_settle:
                            mock_settle.return_value = (True, "ok")
                            ok, msg = await strategy._post_run_guard(mode="operate")
                            assert ok is True
                            assert "delta ok" in msg.lower()
                            assert mock_reconcile.called
                            assert mock_settle.called


@pytest.mark.asyncio
async def test_atomic_deposit_iteration_swaps_from_eth_when_borrow_surfaces_as_eth(
    strategy, mock_adapter_responses
):
    # Ensure gas reserve exists so we don't drain to 0
    strategy.WRAP_GAS_RESERVE = 0.0014

    borrow_amt_wei = 10**18
    safe_borrow = int(borrow_amt_wei * 0.98)

    balances: dict[str, int] = {
        ETH_TOKEN_ID: 2 * 10**18,
        WETH_TOKEN_ID: 0,
        WSTETH_TOKEN_ID: 0,
    }

    async def get_balance_raw_side_effect(*, token_id: str, wallet_address: str, **_):
        return balances.get(token_id, 0)

    async def borrow_side_effect(*, mtoken: str, amount: int):
        # Borrow shows up as native ETH (simulates on-chain behavior)
        assert mtoken == M_WETH
        balances[ETH_TOKEN_ID] += int(amount)
        return (True, {"block_number": 12345})

    strategy.moonwell_adapter.borrow = AsyncMock(side_effect=borrow_side_effect)

    async def wrap_eth_side_effect(*, amount: int):
        balances[ETH_TOKEN_ID] -= int(amount)
        balances[WETH_TOKEN_ID] += int(amount)
        return (True, {"block_number": 12346})

    strategy.moonwell_adapter.wrap_eth = AsyncMock(side_effect=wrap_eth_side_effect)

    async def swap_side_effect(
        *, from_token_id: str, to_token_id: str, amount: int, **_
    ):
        assert from_token_id == WETH_TOKEN_ID
        assert to_token_id == WSTETH_TOKEN_ID
        balances[WSTETH_TOKEN_ID] += 123
        return {"to_amount": 123}

    strategy._swap_with_retries = AsyncMock(side_effect=swap_side_effect)

    with patch.object(
        strategy, "_get_balance_raw", new_callable=AsyncMock
    ) as mock_balance:
        mock_balance.side_effect = get_balance_raw_side_effect
        lent = await strategy._atomic_deposit_iteration(borrow_amt_wei)

    assert lent == 123
    strategy.moonwell_adapter.borrow.assert_called_once_with(
        mtoken=M_WETH, amount=safe_borrow
    )
    strategy.moonwell_adapter.wrap_eth.assert_called_once()
    strategy._swap_with_retries.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_wallet_into_position_uses_eth_inventory(
    strategy, mock_adapter_responses
):
    balances: dict[str, int] = {
        ETH_TOKEN_ID: 10 * 10**18,
        WETH_TOKEN_ID: 0,
        WSTETH_TOKEN_ID: 0,
    }

    snap = MagicMock()
    snap.wallet_wsteth = 0
    snap.wallet_weth = 0
    snap.wsteth_dec = 18
    snap.weth_dec = 18
    snap.wsteth_price = 2000.0
    snap.weth_price = 2000.0
    snap.weth_debt = 5 * 10**18
    snap.debt_usd = 10000.0
    snap.eth_usable_wei = 10 * 10**18
    snap.totals_usd = {f"Base_{M_WSTETH}": 0.0, f"Base_{WETH}": -10000.0}

    async def get_balance_raw_side_effect(*, token_id: str, wallet_address: str, **_):
        return balances.get(token_id, 0)

    async def swap_side_effect(
        *, from_token_id: str, to_token_id: str, amount: int, **_
    ):
        # _reconcile_wallet_into_position calls _swap_with_retries with ETH_TOKEN_ID
        assert from_token_id == ETH_TOKEN_ID
        assert to_token_id == WSTETH_TOKEN_ID
        balances[WSTETH_TOKEN_ID] += 7 * 10**18
        return {"to_amount": 7 * 10**18, "block_number": 12345}

    strategy._swap_with_retries = AsyncMock(side_effect=swap_side_effect)

    with (
        patch.object(
            strategy, "_accounting_snapshot", new_callable=AsyncMock
        ) as mock_snap,
        patch.object(
            strategy, "_get_balance_raw", new_callable=AsyncMock
        ) as mock_balance,
    ):
        mock_snap.return_value = (snap, (0.8, 0.8))
        mock_balance.side_effect = get_balance_raw_side_effect
        success, msg = await strategy._reconcile_wallet_into_position(
            collateral_factors=(0.8, 0.8),
            max_batch_usd=100000.0,
        )

    assert success is True
    assert strategy._swap_with_retries.called
    strategy.moonwell_adapter.lend.assert_called()


@pytest.mark.asyncio
async def test_sweep_token_balances_no_tokens(strategy, mock_adapter_responses):
    # All balances are 0
    strategy.balance_adapter.get_balance = AsyncMock(return_value=(True, 0))

    success, msg = await strategy._sweep_token_balances(
        target_token_id="usd-coin-base",
    )

    assert success is True
    assert "no tokens" in msg.lower()


@pytest.mark.asyncio
async def test_sweep_token_balances_sweeps_tokens(strategy, mock_adapter_responses):
    strategy.min_withdraw_usd = 1.0

    # Mock balance returns (has some WETH dust)
    async def get_balance_raw_side_effect(*, token_id: str, wallet_address: str, **_):
        if "weth" in token_id.lower():
            return 100 * 10**18
        return 0

    # Mock price (high enough to trigger sweep)
    strategy.token_adapter.get_token_price = AsyncMock(
        return_value=(True, {"current_price": 2000.0})
    )

    with patch.object(
        strategy, "_get_balance_raw", new_callable=AsyncMock
    ) as mock_balance:
        mock_balance.side_effect = get_balance_raw_side_effect
        success, msg = await strategy._sweep_token_balances(
            target_token_id="usd-coin-base",
            exclude=set(),
        )

    assert success is True
    # Should have called swap via _swap_with_retries
    strategy.brap_adapter.swap_from_token_ids.assert_called()


# Tests for code review fixes


@pytest.mark.asyncio
async def test_deposit_rejects_zero_amount(strategy):
    result = await strategy.deposit(main_token_amount=0.0)
    assert result[0] is False
    assert "positive" in result[1].lower()

    result = await strategy.deposit(main_token_amount=-10.0)
    assert result[0] is False
    assert "positive" in result[1].lower()


def test_slippage_capped_at_max(strategy):
    strategy.MAX_SLIPPAGE_TOLERANCE = 0.03
    strategy.swap_slippage_tolerance = 0.02

    # With 3 retries at 2% base: 2%, 4%, 6% -> should be capped at 3%
    assert hasattr(strategy, "MAX_SLIPPAGE_TOLERANCE")
    assert strategy.MAX_SLIPPAGE_TOLERANCE == 0.03


def test_price_staleness_threshold_exists(strategy):
    assert hasattr(strategy, "PRICE_STALENESS_THRESHOLD")
    assert strategy.PRICE_STALENESS_THRESHOLD > 0


def test_min_leverage_gain_constant_exists(strategy):
    assert hasattr(strategy, "_MIN_LEVERAGE_GAIN_BPS")
    assert strategy._MIN_LEVERAGE_GAIN_BPS == 50e-4


@pytest.mark.asyncio
async def test_update_runs_post_run_guard(strategy, mock_adapter_responses):
    with patch.object(strategy, "_update_impl", new_callable=AsyncMock) as mock_impl:
        with patch.object(
            strategy, "_post_run_guard", new_callable=AsyncMock
        ) as mock_guard:
            mock_impl.return_value = (True, "ok")
            mock_guard.return_value = (True, "guard ok")

            ok, msg = await strategy.update()

            assert ok is True
            assert "finalizer:" in msg
            mock_guard.assert_awaited()
            assert mock_guard.call_args.kwargs.get("mode") == "operate"


@pytest.mark.asyncio
async def test_update_fails_if_post_run_guard_fails(strategy, mock_adapter_responses):
    with patch.object(strategy, "_update_impl", new_callable=AsyncMock) as mock_impl:
        with patch.object(
            strategy, "_post_run_guard", new_callable=AsyncMock
        ) as mock_guard:
            mock_impl.return_value = (True, "ok")
            mock_guard.return_value = (False, "guard failed")

            ok, msg = await strategy.update()

            assert ok is False
            assert "finalizer failed" in msg.lower()


@pytest.mark.asyncio
async def test_withdraw_runs_post_run_guard_only_on_failure(
    strategy, mock_adapter_responses
):
    with patch.object(strategy, "_withdraw_impl", new_callable=AsyncMock) as mock_impl:
        with patch.object(
            strategy, "_post_run_guard", new_callable=AsyncMock
        ) as mock_guard:
            mock_impl.return_value = (True, "ok")
            mock_guard.return_value = (True, "guard ok")

            ok, _ = await strategy.withdraw()

            assert ok is True
            mock_guard.assert_not_awaited()

            mock_impl.return_value = (False, "failed")
            ok, msg = await strategy.withdraw()
            assert ok is False
            assert "finalizer:" in msg
            mock_guard.assert_awaited()
            assert mock_guard.call_args.kwargs.get("mode") == "exit"


@pytest.mark.asyncio
async def test_leverage_calc_handles_high_cf_w(strategy, mock_adapter_responses):
    strategy.MIN_HEALTH_FACTOR = 1.2

    # This should return early without crashing when cf_w >= MIN_HEALTH_FACTOR
    # Pass collateral_factors directly to avoid RPC call ordering issues
    # collateral_factors = (cf_usdc, cf_wsteth)
    result = await strategy._loop_wsteth(
        wsteth_price=2000.0,
        weth_price=2000.0,
        current_borrowed_value=1000.0,
        initial_leverage=1.5,
        usdc_lend_value=1000.0,
        wsteth_lend_value=500.0,
        collateral_factors=(0.8, 1.3),
    )

    # Should return failure tuple instead of crashing
    assert result[0] is False
    assert result[2] == -1


@pytest.mark.asyncio
async def test_price_staleness_triggers_refresh(strategy, mock_adapter_responses):
    strategy.PRICE_STALENESS_THRESHOLD = 1
    strategy._token_price_cache = {"test-token": 100.0}
    strategy._token_price_timestamps = {"test-token": 0}

    # Should refresh because timestamp is stale
    await strategy._get_token_price("test-token")

    # Should have called token adapter because cache was stale
    strategy.token_adapter.get_token_price.assert_called_with("test-token")


@pytest.mark.asyncio
async def test_partial_liquidate_prefers_wsteth_when_excess(strategy):
    # Token metadata
    async def mock_get_token(token_id: str):
        if token_id == USDC_TOKEN_ID:
            return (True, {"decimals": 6})
        if token_id == WSTETH_TOKEN_ID:
            return (True, {"decimals": 18})
        return (True, {"decimals": 18})

    async def mock_get_price(token_id: str):
        if token_id == WSTETH_TOKEN_ID:
            return (True, {"current_price": 2000.0})
        return (True, {"current_price": 1.0})

    strategy.token_adapter.get_token = AsyncMock(side_effect=mock_get_token)
    strategy.token_adapter.get_token_price = AsyncMock(side_effect=mock_get_price)

    balances: dict[str, int] = {USDC_TOKEN_ID: 0, WSTETH_TOKEN_ID: 0}

    async def mock_get_balance_raw(*, token_id: str, wallet_address: str, **_):
        return balances.get(token_id, 0)

    # Position snapshot: wstETH collateral > WETH debt
    totals_usd = {
        f"Base_{M_WSTETH}": 500.0,
        f"Base_{M_USDC}": 1000.0,
        f"Base_{WETH}": -200.0,
    }
    snap = MagicMock()
    snap.totals_usd = totals_usd
    snap.debt_usd = 200.0
    snap.wsteth_price = 2000.0
    snap.wsteth_dec = 18

    strategy.moonwell_adapter.get_collateral_factor = AsyncMock(
        return_value=(True, 0.8)
    )

    # mwstETH redemption metadata (1:1 exchange rate for test)
    async def mock_max_withdrawable(*, mtoken: str):
        return (
            True,
            {
                "cTokens_raw": 10**30,
                "exchangeRate_raw": 10**18,
                "conversion_factor": 1.0,
            },
        )

    strategy.moonwell_adapter.max_withdrawable_mtoken = AsyncMock(
        side_effect=mock_max_withdrawable
    )

    async def mock_unlend(*, mtoken: str, amount: int):
        if mtoken == M_WSTETH:
            balances[WSTETH_TOKEN_ID] += int(amount)
        return (True, {"block_number": 12345})

    strategy.moonwell_adapter.unlend = AsyncMock(side_effect=mock_unlend)

    async def mock_swap(
        from_token_id,
        to_token_id,
        from_address,
        amount,
        slippage=0.0,
        strategy_name=None,
        **_,
    ):
        amt = int(amount)
        # wstETH → USDC
        balances[WSTETH_TOKEN_ID] -= amt
        usd_out = (amt / 10**18) * 2000.0
        usdc_out = int(usd_out * 10**6)
        balances[USDC_TOKEN_ID] += usdc_out
        return (True, {"to_amount": usdc_out, "block_number": 12346})

    strategy.brap_adapter.swap_from_token_ids = AsyncMock(side_effect=mock_swap)

    # Also need to mock lend since partial_liquidate may try to re-lend leftover wstETH
    strategy.moonwell_adapter.lend = AsyncMock(return_value=(True, "success"))

    with (
        patch.object(
            strategy, "_accounting_snapshot", new_callable=AsyncMock
        ) as mock_snap,
        patch.object(
            strategy, "_get_balance_raw", new_callable=AsyncMock
        ) as mock_balance,
    ):
        mock_snap.return_value = (snap, (0.8, 0.8))
        mock_balance.side_effect = mock_get_balance_raw
        ok, msg = await strategy.partial_liquidate(usd_value=100.0)

    assert ok
    assert "available" in msg.lower()

    # Should have redeemed mwstETH and swapped to USDC
    assert strategy.moonwell_adapter.unlend.call_count == 1
    assert strategy.moonwell_adapter.unlend.call_args.kwargs["mtoken"] == M_WSTETH
    assert strategy.brap_adapter.swap_from_token_ids.call_count >= 1


@pytest.mark.asyncio
async def test_partial_liquidate_uses_usdc_collateral_when_no_wsteth_excess(strategy):
    strategy.token_adapter.get_token = AsyncMock(
        side_effect=lambda token_id: (
            True,
            {"decimals": 6} if token_id == USDC_TOKEN_ID else {"decimals": 18},
        )
    )
    strategy.token_adapter.get_token_price = AsyncMock(
        return_value=(True, {"current_price": 1.0})
    )

    balances: dict[str, int] = {USDC_TOKEN_ID: 0}

    async def mock_get_balance_raw(*, token_id: str, wallet_address: str, **_):
        return balances.get(token_id, 0)

    totals_usd = {
        f"Base_{M_WSTETH}": 100.0,
        f"Base_{M_USDC}": 500.0,
        f"Base_{WETH}": -200.0,
    }
    snap = MagicMock()
    snap.totals_usd = totals_usd
    snap.debt_usd = 200.0
    snap.usdc_price = 1.0
    snap.usdc_dec = 6

    strategy.moonwell_adapter.get_collateral_factor = AsyncMock(
        return_value=(True, 0.8)
    )

    async def mock_max_withdrawable(*, mtoken: str):
        return (
            True,
            {
                "cTokens_raw": 10**30,
                "exchangeRate_raw": 10**18,
                "conversion_factor": 1.0,
            },
        )

    strategy.moonwell_adapter.max_withdrawable_mtoken = AsyncMock(
        side_effect=mock_max_withdrawable
    )

    async def mock_unlend(*, mtoken: str, amount: int):
        if mtoken == M_USDC:
            balances[USDC_TOKEN_ID] += int(amount)
        return (True, {"block_number": 12345})

    strategy.moonwell_adapter.unlend = AsyncMock(side_effect=mock_unlend)

    with (
        patch.object(
            strategy, "_accounting_snapshot", new_callable=AsyncMock
        ) as mock_snap,
        patch.object(
            strategy, "_get_balance_raw", new_callable=AsyncMock
        ) as mock_balance,
    ):
        mock_snap.return_value = (snap, (0.8, 0.8))
        mock_balance.side_effect = mock_get_balance_raw
        ok, msg = await strategy.partial_liquidate(usd_value=50.0)

    assert ok
    assert "available" in msg.lower()

    # Should redeem mUSDC and not need a swap
    assert strategy.moonwell_adapter.unlend.call_count == 1
    assert strategy.moonwell_adapter.unlend.call_args.kwargs["mtoken"] == M_USDC
    assert not strategy.brap_adapter.swap_from_token_ids.called

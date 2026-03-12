from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

_wayfinder_path_dir = Path(__file__).parent.parent.parent.resolve()
_wayfinder_path_str = str(_wayfinder_path_dir)
if _wayfinder_path_str not in sys.path:
    sys.path.insert(0, _wayfinder_path_str)
elif sys.path.index(_wayfinder_path_str) > 0:
    sys.path.remove(_wayfinder_path_str)
    sys.path.insert(0, _wayfinder_path_str)

import pytest  # noqa: E402

try:
    from tests.test_utils import (  # noqa: E402
        assert_quote_result,
        assert_status_dict,
        assert_status_tuple,
        get_canonical_examples,
        load_strategy_examples,
    )
except ImportError as err:
    test_utils_path = Path(_wayfinder_path_dir) / "tests" / "test_utils.py"
    spec = importlib.util.spec_from_file_location("tests.test_utils", test_utils_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec from {test_utils_path}") from err
    test_utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_utils)
    assert_quote_result = test_utils.assert_quote_result
    assert_status_dict = test_utils.assert_status_dict
    assert_status_tuple = test_utils.assert_status_tuple
    get_canonical_examples = test_utils.get_canonical_examples
    load_strategy_examples = test_utils.load_strategy_examples

from wayfinder_paths.adapters.boros_adapter import BorosVault  # noqa: E402
from wayfinder_paths.core.utils.units import to_erc20_raw  # noqa: E402
from wayfinder_paths.strategies.multi_vault_split_strategy.strategy import (  # noqa: E402
    Inventory,
    MultiVaultSplitStrategy,
)


def _inventory(
    *,
    usdc_arb_idle: float = 0.0,
    usdc_base_idle: float = 0.0,
    usdt_arb_idle: float = 0.0,
    hlp_equity: float = 0.0,
    hl_perp_idle: float = 0.0,
    avantis_value_usdc: float = 0.0,
    boros_vault_value_usd: float = 0.0,
    boros_account_idle_usd: float = 0.0,
    boros_vaults: list[BorosVault] | None = None,
) -> Inventory:
    positions_value = hlp_equity + avantis_value_usdc + boros_vault_value_usd
    unallocated_total = usdc_arb_idle + usdc_base_idle + hl_perp_idle
    total_value = (
        positions_value + unallocated_total + usdt_arb_idle + boros_account_idle_usd
    )
    return Inventory(
        usdc_arb_idle=usdc_arb_idle,
        usdc_base_idle=usdc_base_idle,
        usdt_arb_idle=usdt_arb_idle,
        eth_arb_idle=0.01,
        eth_base_idle=0.01,
        hlp_equity=hlp_equity,
        hlp_wait_ms=0,
        hlp_in_cooldown=False,
        hlp_withdrawable_now=hlp_equity,
        hl_perp_idle=hl_perp_idle,
        avantis_value_usdc=avantis_value_usdc,
        boros_vault_value_usd=boros_vault_value_usd,
        boros_account_idle_usd=boros_account_idle_usd,
        boros_vaults=list(boros_vaults or []),
        positions_value=positions_value,
        unallocated_total=unallocated_total,
        total_value=total_value,
    )


def _mock_external(strategy: MultiVaultSplitStrategy) -> None:
    strategy.ledger_adapter.record_strategy_snapshot = AsyncMock(
        return_value=(True, None)
    )
    strategy.balance_adapter.move_from_main_wallet_to_strategy_wallet = AsyncMock(
        return_value=(True, "0xmock")
    )
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(True, "0xmock")
    )
    strategy.balance_adapter.wait_for_balance = AsyncMock(return_value=100.0)
    strategy.boros_adapter.get_user_withdrawal_status = AsyncMock(
        return_value=(True, {"start": 0, "unscaled": 0})
    )
    strategy.boros_adapter.finalize_vault_withdrawal = AsyncMock(
        return_value=(True, {"status": "ok"})
    )
    strategy.boros_adapter.get_account_balances = AsyncMock(
        return_value=(True, {"cross_wei": 0, "total": 0.0, "isolated_positions": []})
    )
    strategy.avantis_adapter.position = AsyncMock(
        return_value=(True, {"value_usdc": 0.0})
    )
    strategy.avantis_adapter.withdraw = AsyncMock(return_value=(True, "0xmock"))
    strategy.hyperliquid_adapter.withdraw_hlp = AsyncMock(
        return_value=(True, {"status": "ok"})
    )
    strategy.hyperliquid_adapter.withdraw_from_hyperliquid = AsyncMock(
        return_value=(True, {"status": "ok"})
    )


@pytest.fixture
async def strategy():
    config = {
        "main_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "strategy_wallet": {
            "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "private_key_hex": "0x" + "ab" * 32,
        },
        "enabled_legs": {"hlp": False, "boros": True, "avantis": True},
    }
    strat = MultiVaultSplitStrategy(config=config)
    await strat.setup()
    _mock_external(strat)
    return strat


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy: MultiVaultSplitStrategy):
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples["smoke"]

    strategy._fetch_apys = AsyncMock(
        return_value={"apy_hlp": 0.0, "apy_boros": 0.08, "apy_avantis": 0.06}
    )
    strategy._roll_expired_boros_vaults = AsyncMock(return_value=[])
    strategy._deploy_boros_account_idle = AsyncMock(return_value=(True, ""))
    strategy._deploy_new_deposit = AsyncMock(return_value=(True, "Deployed capital"))
    strategy._get_inventory = AsyncMock(return_value=_inventory(usdc_arb_idle=200.0))

    st = assert_status_dict(await strategy.status())
    assert "portfolio_value" in st
    assert "strategy_status" in st

    ok, _ = assert_status_tuple(await strategy.deposit(**smoke_data.get("deposit", {})))
    assert ok is True

    ok, _ = assert_status_tuple(await strategy.update(**smoke_data.get("update", {})))
    assert ok is True

    strategy._get_inventory = AsyncMock(return_value=_inventory())
    ok, _ = assert_status_tuple(
        await strategy.withdraw(**smoke_data.get("withdraw", {}))
    )
    assert ok is True


@pytest.mark.asyncio
async def test_canonical_usage(strategy: MultiVaultSplitStrategy):
    strategy._fetch_apys = AsyncMock(
        return_value={"apy_hlp": 0.0, "apy_boros": 0.08, "apy_avantis": 0.06}
    )
    strategy._roll_expired_boros_vaults = AsyncMock(return_value=[])
    strategy._deploy_boros_account_idle = AsyncMock(return_value=(True, ""))
    strategy._deploy_new_deposit = AsyncMock(return_value=(True, "Deployed capital"))
    strategy._get_inventory = AsyncMock(return_value=_inventory(usdc_arb_idle=500.0))

    examples = load_strategy_examples(Path(__file__))
    canonical = get_canonical_examples(examples)

    for example_name, example_data in canonical.items():
        if "deposit" in example_data:
            ok, _ = assert_status_tuple(
                await strategy.deposit(**example_data["deposit"])
            )
            assert ok, f"Canonical example '{example_name}' deposit failed"

        if "update" in example_data:
            ok, _ = assert_status_tuple(await strategy.update())
            assert ok, f"Canonical example '{example_name}' update failed"

        if "status" in example_data:
            st = assert_status_dict(await strategy.status())
            assert isinstance(st, dict)


@pytest.mark.asyncio
async def test_quote_returns_quote_result(strategy: MultiVaultSplitStrategy):
    strategy._fetch_apys = AsyncMock(
        return_value={"apy_hlp": 0.0, "apy_boros": 0.10, "apy_avantis": 0.05}
    )
    assert_quote_result(await strategy.quote(deposit_amount=1000.0))


@pytest.mark.asyncio
async def test_error_case_below_minimum(strategy: MultiVaultSplitStrategy):
    ok, _ = assert_status_tuple(await strategy.deposit(main_token_amount=10.0))
    assert ok is False


@pytest.mark.asyncio
async def test_pick_boros_vault_prefers_existing_open_position(
    strategy: MultiVaultSplitStrategy,
):
    closed_vault = BorosVault(
        amm_id=1,
        market_id=11,
        symbol="CLOSED",
        is_expired=True,
        user_deposit_tokens=25.0,
        remaining_supply_lp=int(100 * 1e18),
        raw={"lpPrice": 1.0},
    )
    open_vault = BorosVault(
        amm_id=2,
        market_id=12,
        symbol="OPEN",
        user_deposit_tokens=15.0,
        remaining_supply_lp=int(50 * 1e18),
        tenor_days=12.0,
        raw={"lpPrice": 1.0},
    )
    strategy.boros_adapter.get_vaults_summary = AsyncMock(
        return_value=(True, [closed_vault, open_vault])
    )
    strategy.boros_adapter.best_yield_vault = AsyncMock()

    picked, capacity = await strategy._pick_boros_vault_for_deposit(amount_tokens=20.0)

    assert picked is open_vault
    assert capacity == pytest.approx(50.0)
    strategy.boros_adapter.best_yield_vault.assert_not_awaited()


@pytest.mark.asyncio
async def test_pick_boros_vault_prefers_existing_isolated_only_position_when_enabled(
    strategy: MultiVaultSplitStrategy,
):
    isolated_vault = BorosVault(
        amm_id=1,
        market_id=11,
        symbol="ISOLATED",
        is_isolated_only=True,
        user_deposit_tokens=25.0,
        remaining_supply_lp=int(100 * 1e18),
        tenor_days=12.0,
        raw={"lpPrice": 1.0},
    )
    open_vault = BorosVault(
        amm_id=2,
        market_id=12,
        symbol="OPEN",
        user_deposit_tokens=15.0,
        remaining_supply_lp=int(50 * 1e18),
        tenor_days=12.0,
        raw={"lpPrice": 1.0},
    )
    strategy.boros_adapter.get_vaults_summary = AsyncMock(
        return_value=(True, [isolated_vault, open_vault])
    )
    strategy.boros_adapter.best_yield_vault = AsyncMock()

    picked, capacity = await strategy._pick_boros_vault_for_deposit(amount_tokens=20.0)

    assert picked is isolated_vault
    assert capacity == pytest.approx(100.0)
    strategy.boros_adapter.best_yield_vault.assert_not_awaited()


@pytest.mark.asyncio
async def test_pick_boros_vault_falls_back_to_best_yield(
    strategy: MultiVaultSplitStrategy,
):
    existing = BorosVault(
        amm_id=1,
        market_id=11,
        symbol="TOO_SMALL",
        user_deposit_tokens=0.5,
        remaining_supply_lp=int(5 * 1e18),
        tenor_days=12.0,
        raw={"lpPrice": 1.0},
    )
    best = BorosVault(
        amm_id=7,
        market_id=17,
        symbol="BEST",
        apy=0.14,
        remaining_supply_lp=int(80 * 1e18),
        tenor_days=10.0,
        raw={"lpPrice": 1.0},
    )
    strategy.boros_adapter.get_vaults_summary = AsyncMock(
        return_value=(True, [existing])
    )
    strategy.boros_adapter.best_yield_vault = AsyncMock(return_value=(True, best))

    picked, capacity = await strategy._pick_boros_vault_for_deposit(amount_tokens=20.0)

    assert picked is best
    assert capacity == pytest.approx(80.0)
    strategy.boros_adapter.best_yield_vault.assert_awaited_once_with(
        token_id=strategy.boros_token_id,
        amount_tokens=20.0,
        min_tenor_days=3.0,
        allow_isolated_only=True,
    )


@pytest.mark.asyncio
async def test_move_idle_to_boros_uses_existing_usdt_then_bridged_remainder(
    strategy: MultiVaultSplitStrategy,
):
    target_vault = BorosVault(amm_id=9, market_id=19, symbol="TARGET")
    strategy._get_inventory = AsyncMock(
        side_effect=[
            _inventory(usdt_arb_idle=6.0),
            _inventory(usdt_arb_idle=5.0),
        ]
    )
    strategy._pick_boros_vault_for_deposit = AsyncMock(
        return_value=(target_vault, 20.0)
    )
    strategy._deposit_usdt_to_boros_vault = AsyncMock(return_value=(True, "ok"))
    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(True, {"status": "ok"})
    )

    ok, message = await strategy._move_idle_to_boros(5.0)

    assert ok is True
    assert "Deposited 11.00 USDT" in message
    assert strategy._deposit_usdt_to_boros_vault.await_count == 2
    first_call = strategy._deposit_usdt_to_boros_vault.await_args_list[0]
    second_call = strategy._deposit_usdt_to_boros_vault.await_args_list[1]
    assert first_call.kwargs["market_id"] == 19
    assert first_call.kwargs["amount_native"] == to_erc20_raw(6.0, 6)
    assert first_call.kwargs["is_isolated_only"] is False
    assert second_call.kwargs["market_id"] == 19
    assert second_call.kwargs["amount_native"] == to_erc20_raw(5.0, 6)
    assert second_call.kwargs["is_isolated_only"] is False
    strategy.brap_adapter.swap_from_token_ids.assert_awaited_once_with(
        from_token_id="usd-coin-arbitrum",
        to_token_id="usdt0-arbitrum",
        from_address=strategy.strategy_wallet_address,
        amount=str(to_erc20_raw(5.0, 6)),
        slippage=0.005,
    )


@pytest.mark.asyncio
async def test_deposit_usdt_to_boros_vault_uses_isolated_margin_for_isolated_vault(
    strategy: MultiVaultSplitStrategy,
):
    strategy.boros_adapter.deposit_to_isolated_margin = AsyncMock(
        return_value=(True, {"status": "ok"})
    )
    strategy.boros_adapter.deposit_to_cross_margin = AsyncMock()
    strategy.boros_adapter.unscaled_to_scaled_cash_wei = AsyncMock(
        return_value=12 * 10**18
    )
    strategy.boros_adapter.deposit_to_vault = AsyncMock(
        return_value=(True, {"status": "ok"})
    )

    ok, message = await strategy._deposit_usdt_to_boros_vault(
        market_id=73,
        amount_native=to_erc20_raw(12.0, 6),
        is_isolated_only=True,
    )

    assert ok is True
    assert "Deposited 12.00 USDT to Boros" in message
    strategy.boros_adapter.deposit_to_isolated_margin.assert_awaited_once_with(
        collateral_address=strategy.usdt_address,
        amount_wei=to_erc20_raw(12.0, 6),
        token_id=strategy.boros_token_id,
        market_id=73,
    )
    strategy.boros_adapter.deposit_to_cross_margin.assert_not_awaited()
    strategy.boros_adapter.deposit_to_vault.assert_awaited_once_with(
        market_id=73,
        net_cash_in_wei=12 * 10**18,
    )


@pytest.mark.asyncio
async def test_deploy_boros_account_idle_caps_deposit_by_capacity(
    strategy: MultiVaultSplitStrategy,
):
    target_vault = BorosVault(amm_id=9, market_id=19, symbol="TARGET")
    strategy.boros_adapter.get_account_balances = AsyncMock(
        side_effect=[
            (
                True,
                {
                    "total": 20.0,
                    "cross_wei": to_erc20_raw(20.0, 18),
                    "isolated_positions": [],
                },
            ),
            (
                True,
                {
                    "total": 20.0,
                    "cross_wei": to_erc20_raw(20.0, 18),
                    "isolated_positions": [],
                },
            ),
        ]
    )
    strategy._pick_boros_vault_for_deposit = AsyncMock(return_value=(target_vault, 7.5))
    strategy.boros_adapter.deposit_to_vault = AsyncMock(
        return_value=(True, {"status": "ok"})
    )

    ok, message = await strategy._deploy_boros_account_idle()

    assert ok is True
    assert "Redeployed Boros idle cash" in message
    strategy._pick_boros_vault_for_deposit.assert_awaited_once_with(
        amount_tokens=20.0,
        allow_isolated_only=False,
    )
    strategy.boros_adapter.deposit_to_vault.assert_awaited_once_with(
        market_id=19,
        net_cash_in_wei=to_erc20_raw(7.5, 18),
    )


@pytest.mark.asyncio
async def test_complete_pending_withdrawal_swaps_and_returns_usdc(
    strategy: MultiVaultSplitStrategy,
):
    strategy.brap_adapter.swap_from_token_ids = AsyncMock(
        return_value=(True, {"status": "ok"})
    )
    strategy.balance_adapter.wait_for_balance = AsyncMock(return_value=12.5)
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet = AsyncMock(
        return_value=(True, "0xreturn")
    )

    ok, message = await strategy._complete_pending_withdrawal(
        _inventory(usdt_arb_idle=12.5)
    )

    assert ok is True
    assert "Completed pending Boros withdrawal" in message
    strategy.brap_adapter.swap_from_token_ids.assert_awaited_once_with(
        from_token_id="usdt0-arbitrum",
        to_token_id="usd-coin-arbitrum",
        from_address=strategy.strategy_wallet_address,
        amount=str(to_erc20_raw(12.5, 6)),
        slippage=0.005,
    )
    strategy.balance_adapter.move_from_strategy_wallet_to_main_wallet.assert_awaited_once_with(
        "usd-coin-arbitrum",
        12.5,
        strategy_name=strategy.__class__.__name__,
    )

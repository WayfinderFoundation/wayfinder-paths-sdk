from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.boros_adapter import BorosAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

ARBITRUM_USDT = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
ARBITRUM_USDT_DECIMALS = 6
BOROS_TEST_DEPOSIT_TOKENS = 20.0

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


async def _make_funded_boros_adapter(gorlami) -> tuple[BorosAdapter, str]:
    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ARBITRUM) as web3:
        assert await web3.eth.chain_id == CHAIN_ID_ARBITRUM

    fork_info = gorlami.forks.get(str(CHAIN_ID_ARBITRUM))
    assert fork_info is not None

    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        ARBITRUM_USDT,
        acct.address,
        100 * 10**ARBITRUM_USDT_DECIMALS,
    )

    return (
        BorosAdapter(
            sign_callback=sign_cb,
            wallet_address=acct.address,
        ),
        acct.address,
    )


@pytest.mark.asyncio
async def test_gorlami_boros_cross_margin_vault_round_trip(gorlami):
    token_id = 3
    adapter, account = await _make_funded_boros_adapter(gorlami)

    ok, vaults = await adapter.search_vaults(token_id=token_id, limit=0)
    assert ok is True, vaults
    assert isinstance(vaults, list) and vaults

    feasible = []
    for vault in vaults:
        capacity = adapter.estimate_vault_capacity_tokens(vault)
        if not adapter.is_vault_open_for_deposit(vault):
            continue
        if capacity is None or capacity < 1.5:
            continue
        feasible.append((float(vault.apy or 0.0), capacity, vault))

    if not feasible:
        pytest.skip("No Boros vault with enough live capacity for a fork round-trip")

    feasible.sort(key=lambda row: row[0], reverse=True)
    _, capacity_tokens, vault = feasible[0]
    vault_deposit_tokens = min(2.0, float(capacity_tokens) * 0.75)
    assert vault_deposit_tokens > 1.0

    ok, dep = await adapter.deposit_to_cross_margin(
        collateral_address=ARBITRUM_USDT,
        amount_wei=int(BOROS_TEST_DEPOSIT_TOKENS * 10**ARBITRUM_USDT_DECIMALS),
        token_id=token_id,
        market_id=vault.market_id,
    )
    assert ok is True, dep

    scaled_vault_cash = await adapter.unscaled_to_scaled_cash_wei(
        token_id,
        int(vault_deposit_tokens * 10**ARBITRUM_USDT_DECIMALS),
    )
    ok, dep_vault = await adapter.deposit_to_vault_direct(
        amm_id=vault.amm_id,
        net_cash_in_wei=scaled_vault_cash,
    )
    assert ok is True, dep_vault

    ok, lp_balance = await adapter.get_vault_lp_balance(
        amm_id=vault.amm_id,
        token_id=token_id,
        account=account,
    )
    assert ok is True, lp_balance
    assert int(lp_balance) > 0

    ok, wd = await adapter.withdraw_from_vault_direct(
        amm_id=vault.amm_id,
        lp_to_remove_wei=int(lp_balance),
        min_cash_out_wei=0,
    )
    assert ok is True, wd

    ok, lp_after = await adapter.get_vault_lp_balance(
        amm_id=vault.amm_id,
        token_id=token_id,
        account=account,
    )
    assert ok is True, lp_after
    assert int(lp_after) == 0


@pytest.mark.asyncio
async def test_gorlami_boros_isolated_only_vault_round_trip(gorlami):
    token_id = 3
    adapter, account = await _make_funded_boros_adapter(gorlami)

    ok, vaults = await adapter.search_vaults(token_id=token_id, limit=0)
    assert ok is True, vaults
    assert isinstance(vaults, list) and vaults

    ok_fee, fee_data = await adapter.get_cash_fee_data(token_id=token_id)
    assert ok_fee is True, fee_data
    min_isolated_tokens = float(fee_data.get("min_cash_isolated") or 0.0)

    feasible = []
    for vault in vaults:
        capacity = adapter.estimate_vault_capacity_tokens(vault)
        if not vault.is_isolated_only:
            continue
        if not adapter.is_vault_open_for_deposit(vault, allow_isolated_only=True):
            continue
        if capacity is None or capacity < max(1.5, min_isolated_tokens + 1.0):
            continue
        feasible.append((float(vault.apy or 0.0), capacity, vault))

    if not feasible:
        pytest.skip(
            "No isolated-only Boros vault with enough live capacity for a fork round-trip"
        )

    feasible.sort(key=lambda row: row[0], reverse=True)
    _, capacity_tokens, vault = feasible[0]
    vault_deposit_tokens = min(12.0, float(capacity_tokens) * 0.75)
    vault_deposit_tokens = max(vault_deposit_tokens, min_isolated_tokens + 1.0)
    assert vault_deposit_tokens > min_isolated_tokens

    ok, dep = await adapter.deposit_to_isolated_margin(
        collateral_address=ARBITRUM_USDT,
        amount_wei=int(BOROS_TEST_DEPOSIT_TOKENS * 10**ARBITRUM_USDT_DECIMALS),
        token_id=token_id,
        market_id=vault.market_id,
    )
    assert ok is True, dep

    scaled_vault_cash = await adapter.unscaled_to_scaled_cash_wei(
        token_id,
        int(vault_deposit_tokens * 10**ARBITRUM_USDT_DECIMALS),
    )
    ok, dep_vault = await adapter.deposit_to_vault(
        market_id=vault.market_id,
        net_cash_in_wei=scaled_vault_cash,
    )
    assert ok is True, dep_vault

    ok, lp_balance = await adapter.get_vault_lp_balance(
        amm_id=vault.amm_id,
        token_id=token_id,
        account=account,
    )
    assert ok is True, lp_balance
    assert int(lp_balance) > 0

    ok, wd = await adapter.withdraw_from_vault(
        market_id=vault.market_id,
        lp_to_remove_wei=int(lp_balance),
        min_cash_out_wei=0,
    )
    assert ok is True, wd

    ok, lp_after = await adapter.get_vault_lp_balance(
        amm_id=vault.amm_id,
        token_id=token_id,
        account=account,
    )
    assert ok is True, lp_after
    assert int(lp_after) == 0

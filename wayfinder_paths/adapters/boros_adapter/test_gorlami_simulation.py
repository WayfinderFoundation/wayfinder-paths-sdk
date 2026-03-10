from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.boros_adapter import BorosAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
async def test_gorlami_boros_cross_margin_vault_round_trip(gorlami):
    chain_id = CHAIN_ID_ARBITRUM
    token_id = 3
    total_deposit_tokens = 20.0

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    usdt = await TOKEN_CLIENT.get_token_details("usdt0-arbitrum")
    usdt_address = str(usdt["address"])
    usdt_decimals = int(usdt.get("decimals", 6) or 6)

    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        usdt_address,
        acct.address,
        100 * 10**usdt_decimals,
    )

    adapter = BorosAdapter(
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

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
        collateral_address=usdt_address,
        amount_wei=int(total_deposit_tokens * 10**usdt_decimals),
        token_id=token_id,
        market_id=vault.market_id,
    )
    assert ok is True, dep

    scaled_vault_cash = await adapter.unscaled_to_scaled_cash_wei(
        token_id,
        int(vault_deposit_tokens * 10**usdt_decimals),
    )
    ok, dep_vault = await adapter.deposit_to_vault_direct(
        amm_id=vault.amm_id,
        net_cash_in_wei=scaled_vault_cash,
    )
    assert ok is True, dep_vault

    ok, lp_balance = await adapter.get_vault_lp_balance(
        amm_id=vault.amm_id,
        token_id=token_id,
        account=acct.address,
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
        account=acct.address,
    )
    assert ok is True, lp_after
    assert int(lp_after) == 0

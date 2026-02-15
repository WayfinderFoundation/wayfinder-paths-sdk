from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.aave_v3_adapter.adapter import AaveV3Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM
from wayfinder_paths.core.constants.contracts import ARBITRUM_USDC
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
async def test_gorlami_aave_v3_supply_borrow_repay_withdraw_claim(gorlami):
    chain_id = CHAIN_ID_ARBITRUM

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation (gorlami fixture patches web3_from_chain_id).
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    # Fund test wallet on the fork.
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 5 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        ARBITRUM_USDC,
        acct.address,
        2_000 * 10**6,
    )

    adapter = AaveV3Adapter(
        config={"strategy_wallet": {"address": acct.address}},
        strategy_wallet_signing_callback=sign_cb,
    )

    wrapped = await adapter._wrapped_native(chain_id=chain_id)

    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_rewards=True)
    assert ok is True, markets
    assert isinstance(markets, list) and markets

    usdc_market = next(
        m
        for m in markets
        if str(m.get("underlying", "")).lower() == ARBITRUM_USDC.lower()
    )
    weth_market = next(
        m for m in markets if str(m.get("underlying", "")).lower() == wrapped.lower()
    )

    # Basic non-native supply/withdraw.
    ok, tx = await adapter.lend(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=5 * 10**6,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=0,
        withdraw_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    # Supply native (wrap+deposit WETH), borrow USDC, repay-full, withdraw-full.
    ok, tx = await adapter.lend(
        chain_id=chain_id,
        underlying_token=wrapped,
        qty=int(0.01 * 10**18),
        native=True,
    )
    assert ok is True, tx

    ok, tx = await adapter.set_collateral(
        chain_id=chain_id,
        underlying_token=wrapped,
        use_as_collateral=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.borrow(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=10 * 10**6,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id, account=acct.address, include_rewards=False
    )
    assert ok is True, state
    assert any(
        p.get("underlying", "").lower() == wrapped.lower()
        and int(p.get("supply_raw") or 0) > 0
        for p in state.get("positions") or []
    )
    assert any(
        p.get("underlying", "").lower() == ARBITRUM_USDC.lower()
        and int(p.get("variable_borrow_raw") or 0) > 0
        for p in state.get("positions") or []
    )

    ok, tx = await adapter.repay(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=0,
        repay_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id, account=acct.address, include_rewards=False
    )
    assert ok is True, state
    assert all(
        int(p.get("variable_borrow_raw") or 0) == 0
        for p in state.get("positions") or []
        if p.get("underlying", "").lower() == ARBITRUM_USDC.lower()
    )

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        underlying_token=wrapped,
        qty=0,
        native=True,
        withdraw_full=True,
    )
    assert ok is True, tx

    # Claim rewards (may be zero, but should be callable).
    ok, tx = await adapter.claim_all_rewards(
        chain_id=chain_id,
        assets=[
            str(weth_market.get("a_token") or ""),
            str(usdc_market.get("a_token") or ""),
            str(usdc_market.get("variable_debt_token") or ""),
        ],
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

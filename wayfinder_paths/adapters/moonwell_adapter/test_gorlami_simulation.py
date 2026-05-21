from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.moonwell_adapter.adapter import MoonwellAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import (
    BASE_USDC,
    BASE_WETH,
    MOONWELL_M_USDC,
    MOONWELL_M_WETH,
)
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
async def test_gorlami_moonwell_supply_borrow_repay_withdraw_claim(gorlami):
    chain_id = CHAIN_ID_BASE

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation before the adapter's direct web3 import resolves RPC config.
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 5 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_USDC,
        acct.address,
        2_000 * 10**6,
    )
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_WETH,
        acct.address,
        1 * 10**18,
    )

    adapter = MoonwellAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_rewards=True)
    assert ok is True, markets
    assert isinstance(markets, list) and markets
    assert any(m.get("mtoken", "").lower() == MOONWELL_M_USDC.lower() for m in markets)

    ok, tx = await adapter.lend(
        chain_id=chain_id,
        mtoken=MOONWELL_M_USDC,
        underlying_token=BASE_USDC,
        amount=500 * 10**6,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.set_collateral(chain_id=chain_id, mtoken=MOONWELL_M_USDC)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, entered = await adapter.is_market_entered(
        chain_id=chain_id,
        mtoken=MOONWELL_M_USDC,
        account=acct.address,
    )
    assert ok is True, entered
    assert entered is True

    borrow_amount = 10**14
    ok, tx = await adapter.borrow(
        chain_id=chain_id,
        mtoken=MOONWELL_M_WETH,
        amount=borrow_amount,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id,
        account=acct.address,
        include_rewards=False,
    )
    assert ok is True, state
    assert any(
        p.get("mtoken", "").lower() == MOONWELL_M_WETH.lower()
        and int(p.get("borrowedUnderlying") or 0) > 0
        for p in state.get("positions") or []
    )

    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_WETH,
        acct.address,
        1 * 10**18,
    )
    ok, tx = await adapter.repay(
        chain_id=chain_id,
        mtoken=MOONWELL_M_WETH,
        underlying_token=BASE_WETH,
        amount=1 * 10**18,
        repay_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state(
        chain_id=chain_id,
        account=acct.address,
        include_rewards=False,
    )
    assert ok is True, state
    assert all(
        int(p.get("borrowedUnderlying") or 0) == 0
        for p in state.get("positions") or []
        if p.get("mtoken", "").lower() == MOONWELL_M_WETH.lower()
    )

    ok, tx = await adapter.remove_collateral(chain_id=chain_id, mtoken=MOONWELL_M_USDC)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, withdrawable = await adapter.max_withdrawable_mtoken(
        chain_id=chain_id,
        mtoken=MOONWELL_M_USDC,
        account=acct.address,
    )
    assert ok is True, withdrawable
    c_tokens_raw = int(withdrawable["cTokens_raw"])
    assert c_tokens_raw > 0

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        mtoken=MOONWELL_M_USDC,
        amount=c_tokens_raw,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, rewards = await adapter.claim_rewards(chain_id=chain_id, min_rewards_usd=0.0)
    assert ok is True, rewards
    assert isinstance(rewards, dict)

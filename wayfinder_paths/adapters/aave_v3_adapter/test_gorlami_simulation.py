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
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    ok, markets = await adapter.get_all_markets(chain_id=chain_id, include_rewards=True)
    assert ok is True, markets
    assert isinstance(markets, list) and markets

    usdc_market = next(
        m
        for m in markets
        if str(m.get("underlying", "")).lower() == ARBITRUM_USDC.lower()
    )
    borrow_market = next(
        m
        for m in markets
        if bool(m.get("borrowing_enabled"))
        and not bool(m.get("is_frozen"))
        and str(m.get("underlying", "")).lower() != ARBITRUM_USDC.lower()
        and float(m.get("price_usd") or 0) > 0
    )
    borrow_underlying = str(borrow_market.get("underlying") or "")
    borrow_qty = max(
        1,
        int(
            (5 / float(borrow_market.get("price_usd") or 1))
            * (10 ** int(borrow_market.get("decimals") or 18))
        ),
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

    # Use a currently collateral-enabled reserve on live Arbitrum.
    # WETH is frozen on Aave v3 there, so native/WETH supply is not reliable on fork tests.
    ok, tx = await adapter.lend(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=100 * 10**6,
    )
    assert ok is True, tx

    ok, tx = await adapter.set_collateral(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        use_as_collateral=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, tx = await adapter.borrow(
        chain_id=chain_id,
        underlying_token=borrow_underlying,
        qty=borrow_qty,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state_per_chain(
        chain_id=chain_id, account=acct.address, include_rewards=False
    )
    assert ok is True, state
    assert any(
        p.get("underlying", "").lower() == ARBITRUM_USDC.lower()
        and int(p.get("supply_raw") or 0) > 0
        for p in state.get("positions") or []
    )
    assert any(
        p.get("underlying", "").lower() == borrow_underlying.lower()
        and int(p.get("variable_borrow_raw") or 0) > 0
        for p in state.get("positions") or []
    )

    # Interest can accrue between borrow and repay_full on live forks; seed a small buffer
    # so the test exercises the closeout path instead of failing on dust.
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        borrow_underlying,
        acct.address,
        max(borrow_qty * 2, borrow_qty + 1),
    )

    ok, tx = await adapter.repay(
        chain_id=chain_id,
        underlying_token=borrow_underlying,
        qty=0,
        repay_full=True,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, state = await adapter.get_full_user_state_per_chain(
        chain_id=chain_id, account=acct.address, include_rewards=False
    )
    assert ok is True, state
    assert all(
        int(p.get("variable_borrow_raw") or 0) == 0
        for p in state.get("positions") or []
        if p.get("underlying", "").lower() == borrow_underlying.lower()
    )

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        underlying_token=ARBITRUM_USDC,
        qty=0,
        withdraw_full=True,
    )
    assert ok is True, tx

    # Claim rewards (may be zero, but should be callable).
    ok, tx = await adapter.claim_all_rewards(
        chain_id=chain_id,
        assets=[
            str(usdc_market.get("a_token") or ""),
        ],
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

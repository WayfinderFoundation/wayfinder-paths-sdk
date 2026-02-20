from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.avantis_adapter.adapter import AvantisAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import AVANTIS_AVUSDC, BASE_USDC
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
async def test_gorlami_avantis_deposit_withdraw_full(gorlami):
    chain_id = CHAIN_ID_BASE

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
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        BASE_USDC,
        acct.address,
        1_000 * 10**6,
    )

    adapter = AvantisAdapter(
        config={"strategy_wallet": {"address": acct.address}},
        strategy_wallet_signing_callback=sign_cb,
    )

    ok, markets = await adapter.get_all_markets()
    assert ok is True, markets
    assert isinstance(markets, list) and markets
    assert str(markets[0].get("vault", "")).lower() == AVANTIS_AVUSDC.lower()

    ok, tx = await adapter.deposit(
        vault_address=AVANTIS_AVUSDC, underlying_token=BASE_USDC, amount=10 * 10**6
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(vault_address=AVANTIS_AVUSDC, account=acct.address)
    assert ok is True, pos
    assert isinstance(pos, dict)
    assert int(pos.get("shares_balance") or 0) > 0
    assert int(pos.get("assets_balance") or 0) > 0

    ok, tx = await adapter.withdraw(
        vault_address=AVANTIS_AVUSDC, amount=0, redeem_full=True
    )
    assert ok is True, tx
    assert tx == "no shares to redeem" or (isinstance(tx, str) and tx.startswith("0x"))

    ok, pos_after = await adapter.get_pos(
        vault_address=AVANTIS_AVUSDC, account=acct.address
    )
    assert ok is True, pos_after
    assert isinstance(pos_after, dict)
    assert int(pos_after.get("shares_balance") or 0) <= int(
        pos.get("shares_balance") or 0
    )

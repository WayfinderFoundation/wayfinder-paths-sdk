from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.eigencloud_adapter.adapter import EigenCloudAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.contracts import EIGENCLOUD_STRATEGIES
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

# stETH on Ethereum mainnet.
STETH_TOKEN = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
STETH_STRATEGY = EIGENCLOUD_STRATEGIES["stETH"]


@pytest.mark.asyncio
async def test_gorlami_eigencloud_deposit_queue_withdraw(gorlami):
    chain_id = CHAIN_ID_ETHEREUM

    acct = Account.create()

    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    # Trigger fork creation.
    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    fork_info = gorlami.forks.get(str(chain_id))
    assert fork_info is not None

    # Fund test wallet: native ETH + stETH.
    await gorlami.set_native_balance(fork_info["fork_id"], acct.address, 10 * 10**18)
    await gorlami.set_erc20_balance(
        fork_info["fork_id"],
        STETH_TOKEN,
        acct.address,
        5 * 10**18,
    )

    adapter = EigenCloudAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )

    # -- Read: get_all_markets --
    ok, markets = await adapter.get_all_markets()
    assert ok is True, markets
    assert isinstance(markets, list) and markets

    steth_market = next(
        (m for m in markets if m["strategy"].lower() == STETH_STRATEGY.lower()),
        None,
    )
    assert steth_market is not None, "stETH strategy not found in markets"
    assert steth_market["is_whitelisted_for_deposit"] is True

    # -- Read: delegation state (should be undelegated) --
    ok, delegation = await adapter.get_delegation_state()
    assert ok is True, delegation
    assert delegation["isDelegated"] is False

    # -- Write: deposit stETH into strategy --
    deposit_amount = 10**18  # 1 stETH
    ok, res = await adapter.deposit(
        strategy=STETH_STRATEGY,
        amount=deposit_amount,
        token=STETH_TOKEN,
    )
    assert ok is True, res
    assert isinstance(res, dict)
    assert isinstance(res["tx_hash"], str) and res["tx_hash"].startswith("0x")

    # -- Read: verify position exists --
    ok, pos = await adapter.get_pos(include_usd=False)
    assert ok is True, pos
    positions = pos.get("positions") or []
    steth_pos = next(
        (p for p in positions if p["strategy"].lower() == STETH_STRATEGY.lower()),
        None,
    )
    assert steth_pos is not None, "stETH position not found after deposit"
    assert steth_pos["deposit_shares"] > 0

    # -- Write: queue withdrawal of all deposited shares --
    ok, qw = await adapter.queue_withdrawals(
        strategies=[STETH_STRATEGY],
        deposit_shares=[steth_pos["deposit_shares"]],
    )
    assert ok is True, qw
    assert isinstance(qw, dict)
    assert isinstance(qw["tx_hash"], str) and qw["tx_hash"].startswith("0x")
    assert "withdrawal_roots" in qw
    assert len(qw["withdrawal_roots"]) > 0

    # -- Read: verify queued withdrawal via root --
    root = qw["withdrawal_roots"][0]
    ok, queued = await adapter.get_queued_withdrawal(withdrawal_root=root)
    assert ok is True, queued
    assert queued["withdrawal_root"] == root
    assert queued["withdrawal"]["staker"].lower() == acct.address.lower()

    # -- Read: get_full_user_state --
    ok, state = await adapter.get_full_user_state(
        account=acct.address,
        withdrawal_roots=[root],
    )
    assert ok is True, state
    assert state["protocol"] == "eigencloud"
    assert len(state.get("queued_withdrawals") or []) == 1

    # Note: completeQueuedWithdrawal requires the withdrawal delay to pass,
    # which we cannot fast-forward on a fork. The queue + read path above
    # validates the full deposit→withdraw flow up to the delay boundary.


@pytest.mark.asyncio
async def test_gorlami_eigencloud_get_all_markets_read_only(gorlami):
    """Lightweight read-only test that doesn't need a funded wallet."""
    chain_id = CHAIN_ID_ETHEREUM

    async with web3_utils.web3_from_chain_id(chain_id) as web3:
        assert await web3.eth.chain_id == int(chain_id)

    adapter = EigenCloudAdapter(config={})

    ok, markets = await adapter.get_all_markets(
        include_total_shares=True,
        include_share_to_underlying=True,
    )
    assert ok is True, markets
    assert isinstance(markets, list)
    assert len(markets) == len(EIGENCLOUD_STRATEGIES)

    for m in markets:
        assert "strategy" in m
        assert "underlying" in m
        assert "total_shares" in m
        assert "shares_to_underlying_1e18" in m
        assert m["chain_id"] == CHAIN_ID_ETHEREUM

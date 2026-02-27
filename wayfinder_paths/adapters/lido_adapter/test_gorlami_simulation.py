from __future__ import annotations

import pytest
from eth_account import Account

from wayfinder_paths.adapters.lido_adapter.adapter import LidoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.lido_contracts import LIDO_BY_CHAIN
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

CHAIN_ID = CHAIN_ID_ETHEREUM
ENTRY = LIDO_BY_CHAIN[CHAIN_ID]
STAKE_AMOUNT = 10**18  # 1 ETH


def _make_adapter(acct: Account) -> tuple[LidoAdapter, Account]:
    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    adapter = LidoAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )
    return adapter, acct


async def _fund_and_create_adapter(gorlami, fork_id: str):
    acct = Account.create()
    adapter, acct = _make_adapter(acct)
    # 10 ETH for gas + staking
    await gorlami.set_native_balance(fork_id, acct.address, 10 * 10**18)
    return adapter, acct


async def _ensure_fork(gorlami) -> str:
    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        assert await web3.eth.chain_id == int(CHAIN_ID)
    fork_info = gorlami.forks.get(str(CHAIN_ID))
    assert fork_info is not None
    return fork_info["fork_id"]


@pytest.mark.asyncio
async def test_gorlami_stake_eth_receive_steth(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_and_create_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(
        amount_wei=STAKE_AMOUNT,
        chain_id=CHAIN_ID,
        receive="stETH",
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        steth_balance = await get_token_balance(
            ENTRY["steth"],
            CHAIN_ID,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    assert int(steth_balance) > 0


@pytest.mark.asyncio
async def test_gorlami_stake_eth_receive_wsteth(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_and_create_adapter(gorlami, fork_id)

    ok, result = await adapter.stake_eth(
        amount_wei=STAKE_AMOUNT,
        chain_id=CHAIN_ID,
        receive="wstETH",
    )
    assert ok is True, result
    assert isinstance(result, dict)
    assert result["stake_tx"].startswith("0x")
    assert result["wrap_tx"].startswith("0x")
    assert result["steth_wrapped"] > 0

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        wsteth_balance = await get_token_balance(
            ENTRY["wsteth"],
            CHAIN_ID,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    assert int(wsteth_balance) > 0


@pytest.mark.asyncio
async def test_gorlami_wrap_unwrap_round_trip(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_and_create_adapter(gorlami, fork_id)

    # Stake to get stETH first.
    ok, _ = await adapter.stake_eth(
        amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID, receive="stETH"
    )
    assert ok is True

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        steth_balance = await get_token_balance(
            ENTRY["steth"],
            CHAIN_ID,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    steth_balance = int(steth_balance)
    assert steth_balance > 0

    # Wrap stETH → wstETH.
    ok, tx = await adapter.wrap_steth(amount_steth_wei=steth_balance, chain_id=CHAIN_ID)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        wsteth_balance = await get_token_balance(
            ENTRY["wsteth"],
            CHAIN_ID,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    wsteth_balance = int(wsteth_balance)
    assert wsteth_balance > 0

    # Unwrap wstETH → stETH.
    ok, tx = await adapter.unwrap_wsteth(
        amount_wsteth_wei=wsteth_balance, chain_id=CHAIN_ID
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        steth_after = await get_token_balance(
            ENTRY["steth"],
            CHAIN_ID,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    # Should have stETH back (may differ slightly due to rounding).
    assert int(steth_after) > 0


@pytest.mark.asyncio
async def test_gorlami_get_rates(gorlami):
    await _ensure_fork(gorlami)

    adapter = LidoAdapter(config={})

    ok, rates = await adapter.get_rates(chain_id=CHAIN_ID)
    assert ok is True, rates
    assert rates["chain_id"] == CHAIN_ID
    # stETH per wstETH should be > 1 ETH (wstETH appreciates).
    assert rates["steth_per_wsteth"] > 10**18
    assert rates["wsteth_per_steth"] > 0


@pytest.mark.asyncio
async def test_gorlami_get_full_user_state(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_and_create_adapter(gorlami, fork_id)

    # Stake to create some position.
    ok, _ = await adapter.stake_eth(
        amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID, receive="stETH"
    )
    assert ok is True

    ok, state = await adapter.get_full_user_state(
        account=acct.address,
        chain_id=CHAIN_ID,
        include_withdrawals=True,
    )
    assert ok is True, state
    assert state["protocol"] == "lido"
    assert state["chain_id"] == CHAIN_ID
    assert state["steth"]["balance_raw"] > 0
    assert state["wsteth"]["balance_raw"] == 0
    assert state["withdrawals"]["request_ids"] == []

from __future__ import annotations

import pytest
from eth_account import Account

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common_module
from wayfinder_paths.adapters.aerodrome_adapter.adapter import (
    AerodromeAdapter,
)
from wayfinder_paths.core.constants.aerodrome_abi import (
    AERODROME_GAUGE_ABI,
    AERODROME_POOL_ABI,
    AERODROME_VOTER_ABI,
    AERODROME_VOTING_ESCROW_ABI,
)
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_WETH
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.testing.gorlami import gorlami_configured

EPOCH_SPECIAL_WINDOW_SECONDS = aerodrome_common_module.EPOCH_SPECIAL_WINDOW_SECONDS
WEEK_SECONDS = aerodrome_common_module.WEEK_SECONDS

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

CHAIN_ID = CHAIN_ID_BASE
ENTRY = AERODROME_BY_CHAIN[CHAIN_ID]
AERO = ENTRY["aero"]
WETH = BASE_WETH


def _make_adapter(acct: Account) -> AerodromeAdapter:
    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    return AerodromeAdapter(
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )


async def _ensure_fork(gorlami) -> str:
    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        assert await web3.eth.chain_id == int(CHAIN_ID)
    fork_info = gorlami.forks.get(str(CHAIN_ID))
    assert fork_info is not None
    return fork_info["fork_id"]


async def _try_time_travel(gorlami, fork_id: str, *, target_ts: int) -> bool:
    candidates = [
        ("evm_setNextBlockTimestamp", [target_ts]),
        ("anvil_setNextBlockTimestamp", [target_ts]),
        ("hardhat_setNextBlockTimestamp", [target_ts]),
    ]
    for method, params in candidates:
        try:
            await gorlami.send_rpc(fork_id, method, params)
            await gorlami.send_rpc(fork_id, "evm_mine", [])
            return True
        except Exception:
            continue

    try:
        block = await gorlami.send_rpc(
            fork_id, "eth_getBlockByNumber", ["latest", False]
        )
        now = int(block.get("timestamp") or "0x0", 16) if isinstance(block, dict) else 0
    except Exception:
        now = 0

    delta = max(0, int(target_ts) - int(now))
    if delta <= 0:
        return True

    for method in ("evm_increaseTime", "anvil_increaseTime", "hardhat_increaseTime"):
        try:
            await gorlami.send_rpc(fork_id, method, [delta])
            await gorlami.send_rpc(fork_id, "evm_mine", [])
            return True
        except Exception:
            continue
    return False


async def _move_to_safe_vote_window(gorlami, fork_id: str) -> bool:
    block = await gorlami.send_rpc(fork_id, "eth_getBlockByNumber", ["latest", False])
    now = int(block.get("timestamp") or "0x0", 16) if isinstance(block, dict) else 0

    epoch_start = (now // WEEK_SECONDS) * WEEK_SECONDS
    window_start = epoch_start + EPOCH_SPECIAL_WINDOW_SECONDS + 5
    window_end = epoch_start + WEEK_SECONDS - EPOCH_SPECIAL_WINDOW_SECONDS - 5

    if now < window_start:
        target_ts = window_start
    elif now >= window_end:
        target_ts = epoch_start + WEEK_SECONDS + EPOCH_SPECIAL_WINDOW_SECONDS + 5
    else:
        return True

    return await _try_time_travel(gorlami, fork_id, target_ts=target_ts)


async def _move_to_next_safe_vote_window(gorlami, fork_id: str) -> bool:
    block = await gorlami.send_rpc(fork_id, "eth_getBlockByNumber", ["latest", False])
    now = int(block.get("timestamp") or "0x0", 16) if isinstance(block, dict) else 0
    next_epoch_start = ((now // WEEK_SECONDS) + 1) * WEEK_SECONDS
    target_ts = next_epoch_start + EPOCH_SPECIAL_WINDOW_SECONDS + 5
    return await _try_time_travel(gorlami, fork_id, target_ts=target_ts)


@pytest.mark.asyncio
async def test_gorlami_aerodrome_lp_gauge_and_ve_lock(gorlami):
    fork_id = await _ensure_fork(gorlami)

    acct = Account.create()
    adapter = _make_adapter(acct)

    # Fund account on fork: ETH for gas + liquidity, AERO for liquidity + lock.
    await gorlami.set_native_balance(fork_id, acct.address, 10 * 10**18)
    await gorlami.set_erc20_balance(fork_id, AERO, acct.address, 50_000 * 10**18)

    # Discover pool/gauge via on-chain registry calls.
    ok, pool = await adapter.get_pool(tokenA=AERO, tokenB=WETH, stable=False)
    assert ok is True, pool

    ok, gauge = await adapter.get_gauge(pool=pool)
    assert ok is True, gauge

    ok, rewards = await adapter.get_reward_contracts(gauge=gauge)
    assert ok is True, rewards
    assert rewards["fees"].startswith("0x")
    assert rewards["bribes"].startswith("0x")

    # Sanity check gauge->staking token is the pool (LP token).
    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        gauge_c = web3.eth.contract(
            address=web3.to_checksum_address(gauge), abi=AERODROME_GAUGE_ABI
        )
        staking_token = await gauge_c.functions.stakingToken().call(
            block_identifier="latest"
        )
    assert staking_token.lower() == pool.lower()

    # Add liquidity: AERO + native ETH (router addLiquidityETH).
    ok, tx = await adapter.add_liquidity(
        tokenA=AERO,
        tokenB=None,  # native ETH
        stable=False,
        amountA_desired=1_000 * 10**18,
        amountB_desired=10**17,  # 0.1 ETH
        slippage_bps=200,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        pool_c = web3.eth.contract(
            address=web3.to_checksum_address(pool), abi=AERODROME_POOL_ABI
        )
        lp_balance = await pool_c.functions.balanceOf(acct.address).call(
            block_identifier="pending"
        )
    lp_balance = int(lp_balance)
    assert lp_balance > 0

    # Stake LP into gauge.
    ok, tx = await adapter.stake_lp(gauge=gauge, amount=lp_balance)
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        gauge_c = web3.eth.contract(
            address=web3.to_checksum_address(gauge), abi=AERODROME_GAUGE_ABI
        )
        staked = await gauge_c.functions.balanceOf(acct.address).call(
            block_identifier="pending"
        )
    assert int(staked) == lp_balance

    # Claim rewards (may be zero, but call should succeed).
    ok, tx = await adapter.claim_gauge_rewards(gauges=[gauge])
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    # Unstake LP.
    ok, tx = await adapter.unstake_lp(gauge=gauge, amount=lp_balance)
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        gauge_c2 = web3.eth.contract(
            address=web3.to_checksum_address(gauge), abi=AERODROME_GAUGE_ABI
        )
        pool_c2 = web3.eth.contract(
            address=web3.to_checksum_address(pool), abi=AERODROME_POOL_ABI
        )
        staked_after = await gauge_c2.functions.balanceOf(acct.address).call(
            block_identifier="pending"
        )
        lp_balance_after = await pool_c2.functions.balanceOf(acct.address).call(
            block_identifier="pending"
        )
    assert int(staked_after) == 0
    assert int(lp_balance_after) == lp_balance

    # Remove liquidity back to AERO + ETH.
    ok, tx = await adapter.remove_liquidity(
        tokenA=AERO,
        tokenB=None,  # native ETH
        stable=False,
        liquidity=lp_balance,
        slippage_bps=200,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        pool_c3 = web3.eth.contract(
            address=web3.to_checksum_address(pool), abi=AERODROME_POOL_ABI
        )
        lp_balance_final = await pool_c3.functions.balanceOf(acct.address).call(
            block_identifier="pending"
        )
    assert int(lp_balance_final) == 0

    # veAERO: create lock and verify enumeration works.
    ok, res = await adapter.create_lock(
        amount=10 * 10**18, lock_duration=4 * WEEK_SECONDS
    )
    assert ok is True, res
    assert isinstance(res, dict)
    assert res["tx"].startswith("0x")
    token_id = res["token_id"]
    assert token_id is not None

    ok, token_ids = await adapter.get_user_ve_nfts(owner=acct.address)
    assert ok is True, token_ids
    assert int(token_id) in [int(x) for x in token_ids]

    # Bump amount and unlock time (should both succeed on a fresh lock).
    ok, tx = await adapter.increase_lock_amount(
        token_id=int(token_id), amount=1 * 10**18
    )
    assert ok is True, tx
    ok, tx = await adapter.increase_unlock_time(
        token_id=int(token_id), lock_duration=8 * WEEK_SECONDS
    )
    assert ok is True, tx


@pytest.mark.asyncio
async def test_gorlami_aerodrome_vote_reset_and_state(gorlami):
    fork_id = await _ensure_fork(gorlami)

    acct = Account.create()
    adapter = _make_adapter(acct)

    await gorlami.set_native_balance(fork_id, acct.address, 10 * 10**18)
    await gorlami.set_erc20_balance(fork_id, AERO, acct.address, 5_000 * 10**18)

    ok, pool = await adapter.get_pool(tokenA=AERO, tokenB=WETH, stable=False)
    assert ok is True, pool

    ok, res = await adapter.create_lock(
        amount=25 * 10**18, lock_duration=4 * WEEK_SECONDS
    )
    assert ok is True, res
    token_id = int(res["token_id"])

    advanced = await _move_to_safe_vote_window(gorlami, fork_id)
    if not advanced:
        pytest.skip(
            "Gorlami fork backend does not support Aerodrome vote-window time travel"
        )

    ok, tx = await adapter.vote(
        token_id=token_id,
        pools=[pool],
        weights=[10_000],
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        voter = web3.eth.contract(
            address=web3.to_checksum_address(ENTRY["voter"]), abi=AERODROME_VOTER_ABI
        )
        ve = web3.eth.contract(
            address=web3.to_checksum_address(ENTRY["voting_escrow"]),
            abi=AERODROME_VOTING_ESCROW_ABI,
        )
        vote_weight = await voter.functions.votes(token_id, pool).call(
            block_identifier="pending"
        )
        voted_flag = await ve.functions.voted(token_id).call(block_identifier="pending")

    assert int(vote_weight) > 0
    assert voted_flag is True

    ok, state = await adapter.get_full_user_state(
        account=acct.address,
        include_votes=True,
        limit=20,
    )
    assert ok is True, state
    ve_nft = next(
        item for item in state["ve_nfts"] if int(item["token_id"]) == int(token_id)
    )
    assert isinstance(ve_nft["votes"], dict)
    assert ve_nft["voted"] is True

    advanced = await _move_to_next_safe_vote_window(gorlami, fork_id)
    if not advanced:
        pytest.skip(
            "Gorlami fork backend does not support Aerodrome next-epoch time travel"
        )

    ok, tx = await adapter.reset_vote(token_id=token_id)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
        voter = web3.eth.contract(
            address=web3.to_checksum_address(ENTRY["voter"]), abi=AERODROME_VOTER_ABI
        )
        ve = web3.eth.contract(
            address=web3.to_checksum_address(ENTRY["voting_escrow"]),
            abi=AERODROME_VOTING_ESCROW_ABI,
        )
        vote_weight_after = await voter.functions.votes(token_id, pool).call(
            block_identifier="pending"
        )
        voted_flag_after = await ve.functions.voted(token_id).call(
            block_identifier="pending"
        )

    assert int(vote_weight_after) == 0
    assert voted_flag_after is False

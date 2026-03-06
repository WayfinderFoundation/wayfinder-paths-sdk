from __future__ import annotations

import time
from typing import Any

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.etherfi_adapter.adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.etherfi_abi import (
    ETHERFI_EETH_ABI,
    ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
)
from wayfinder_paths.core.constants.etherfi_contracts import ETHERFI_BY_CHAIN
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

ENTRY = ETHERFI_BY_CHAIN[CHAIN_ID_ETHEREUM]
STAKE_AMOUNT = 10**18  # 1 ETH


def _make_adapter(acct) -> EtherfiAdapter:
    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    return EtherfiAdapter(
        config={},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )


async def _ensure_fork(gorlami) -> str:
    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        assert await web3.eth.chain_id == CHAIN_ID_ETHEREUM
    fork_info = gorlami.forks.get(str(CHAIN_ID_ETHEREUM))
    assert fork_info is not None
    return fork_info["fork_id"]


async def _fund_adapter(gorlami, fork_id: str) -> tuple[EtherfiAdapter, Any]:
    acct = Account.create()
    adapter = _make_adapter(acct)
    # 20 ETH for gas + staking + approvals.
    await gorlami.set_native_balance(fork_id, acct.address, 20 * 10**18)
    return adapter, acct


async def _sign_eip2612_permit(
    *,
    web3,
    owner_acct,
    token_address: str,
    spender: str,
    value: int,
    deadline: int,
) -> dict[str, object]:
    token = web3.eth.contract(address=token_address, abi=ETHERFI_EETH_ABI)
    owner = to_checksum_address(owner_acct.address)
    spender = to_checksum_address(spender)

    nonce = await token.functions.nonces(owner).call(block_identifier="pending")
    domain_separator = await token.functions.DOMAIN_SEPARATOR().call(
        block_identifier="pending"
    )

    permit_typehash = web3.keccak(
        text="Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
    )
    struct_hash = web3.keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256"],
            [
                permit_typehash,
                owner,
                spender,
                int(value),
                int(nonce),
                int(deadline),
            ],
        )
    )
    digest = web3.keccak(b"\x19\x01" + bytes(domain_separator) + struct_hash)
    signed = owner_acct.unsafe_sign_hash(digest)

    return {
        "value": int(value),
        "deadline": int(deadline),
        "v": int(signed.v),
        "r": int(signed.r).to_bytes(32, "big"),
        "s": int(signed.s).to_bytes(32, "big"),
    }


@pytest.mark.asyncio
async def test_gorlami_stake_and_get_pos(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok and "paused" in str(tx).lower():
        pytest.skip("ether.fi LiquidityPool is paused on this fork")
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    ok, pos = await adapter.get_pos(account=acct.address, chain_id=CHAIN_ID_ETHEREUM)
    assert ok is True, pos
    assert pos["eeth"]["balance_raw"] > 0


@pytest.mark.asyncio
async def test_gorlami_wrap_unwrap_round_trip(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok and "paused" in str(tx).lower():
        pytest.skip("ether.fi LiquidityPool is paused on this fork")
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        eeth_balance = await get_token_balance(
            ENTRY["eeth"],
            CHAIN_ID_ETHEREUM,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    eeth_balance = int(eeth_balance)
    assert eeth_balance > 0

    ok, tx = await adapter.wrap_eeth(amount_eeth=eeth_balance, chain_id=CHAIN_ID_ETHEREUM)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        weeth_balance = int(
            await get_token_balance(
                ENTRY["weeth"],
                CHAIN_ID_ETHEREUM,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
        eeth_after_wrap = int(
            await get_token_balance(
                ENTRY["eeth"],
                CHAIN_ID_ETHEREUM,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert weeth_balance > 0
    # Wrapped the full eETH balance; share-based rounding can leave 1 wei dust.
    assert eeth_after_wrap <= 1

    ok, tx = await adapter.unwrap_weeth(amount_weeth=weeth_balance, chain_id=CHAIN_ID_ETHEREUM)
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        eeth_after_unwrap = int(
            await get_token_balance(
                ENTRY["eeth"],
                CHAIN_ID_ETHEREUM,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert eeth_after_unwrap > 0


@pytest.mark.asyncio
async def test_gorlami_wrap_with_permit(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok and "paused" in str(tx).lower():
        pytest.skip("ether.fi LiquidityPool is paused on this fork")
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        eeth_balance = await get_token_balance(
            ENTRY["eeth"],
            CHAIN_ID_ETHEREUM,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
        permit = await _sign_eip2612_permit(
            web3=web3,
            owner_acct=acct,
            token_address=ENTRY["eeth"],
            spender=ENTRY["weeth"],
            value=int(eeth_balance),
            deadline=int(time.time()) + 3600,
        )

    ok, tx = await adapter.wrap_eeth_with_permit(
        amount_eeth=int(eeth_balance),
        permit=permit,
        chain_id=CHAIN_ID_ETHEREUM,
    )
    assert ok is True, tx
    assert isinstance(tx, str) and tx.startswith("0x")

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        weeth_balance = await get_token_balance(
            ENTRY["weeth"],
            CHAIN_ID_ETHEREUM,
            acct.address,
            web3=web3,
            block_identifier="pending",
        )
    assert int(weeth_balance) > 0


@pytest.mark.asyncio
async def test_gorlami_request_withdraw_and_claim_status(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok and "paused" in str(tx).lower():
        pytest.skip("ether.fi LiquidityPool is paused on this fork")
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        eeth_balance = int(
            await get_token_balance(
                ENTRY["eeth"],
                CHAIN_ID_ETHEREUM,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
    assert eeth_balance > 0

    withdraw_amount = min(eeth_balance, 10**17)  # up to 0.1 eETH
    ok, res = await adapter.request_withdraw(
        amount_eeth=withdraw_amount,
        chain_id=CHAIN_ID_ETHEREUM,
        include_request_id=True,
    )
    assert ok is True, res
    assert isinstance(res, dict)
    assert res["tx"].startswith("0x")
    request_id = res.get("request_id")
    assert isinstance(request_id, int)

    ok, finalized = await adapter.is_withdraw_finalized(
        token_id=request_id,
        chain_id=CHAIN_ID_ETHEREUM,
    )
    assert ok is True, finalized
    assert isinstance(finalized, bool)

    ok, claimable = await adapter.get_claimable_withdraw(
        token_id=request_id,
        chain_id=CHAIN_ID_ETHEREUM,
    )
    assert ok is True, claimable
    assert isinstance(claimable, int) and claimable >= 0

    # Claim will only succeed if finalized; otherwise it should fail cleanly.
    ok, claim_tx_or_err = await adapter.claim_withdraw(
        token_id=request_id,
        chain_id=CHAIN_ID_ETHEREUM,
    )
    if finalized:
        assert ok is True, claim_tx_or_err
        assert isinstance(claim_tx_or_err, str) and claim_tx_or_err.startswith("0x")
    else:
        assert ok is False


@pytest.mark.asyncio
async def test_gorlami_request_withdraw_with_permit_mints_nft(gorlami):
    fork_id = await _ensure_fork(gorlami)
    adapter, acct = await _fund_adapter(gorlami, fork_id)

    ok, tx = await adapter.stake_eth(amount_wei=STAKE_AMOUNT, chain_id=CHAIN_ID_ETHEREUM)
    if not ok and "paused" in str(tx).lower():
        pytest.skip("ether.fi LiquidityPool is paused on this fork")
    assert ok is True, tx

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        eeth_balance = int(
            await get_token_balance(
                ENTRY["eeth"],
                CHAIN_ID_ETHEREUM,
                acct.address,
                web3=web3,
                block_identifier="pending",
            )
        )
        withdraw_amount = min(eeth_balance, 10**17)  # up to 0.1 eETH
        permit = await _sign_eip2612_permit(
            web3=web3,
            owner_acct=acct,
            token_address=ENTRY["eeth"],
            spender=ENTRY["liquidity_pool"],
            value=withdraw_amount,
            deadline=int(time.time()) + 3600,
        )

    ok, res = await adapter.request_withdraw_with_permit(
        amount_eeth=withdraw_amount,
        permit=permit,
        chain_id=CHAIN_ID_ETHEREUM,
        include_request_id=True,
    )
    assert ok is True, res
    assert isinstance(res, dict)
    assert res["tx"].startswith("0x")
    request_id = res.get("request_id")
    assert isinstance(request_id, int)

    async with web3_utils.web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
        nft = web3.eth.contract(
            address=ENTRY["withdraw_request_nft"],
            abi=ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
        )
        owner = await nft.functions.ownerOf(int(request_id)).call(
            block_identifier="pending"
        )
    assert to_checksum_address(owner) == acct.address

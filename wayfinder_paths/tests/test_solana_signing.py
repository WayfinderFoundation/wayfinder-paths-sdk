from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.utils.wallets import (
    CHAIN_TYPE_SOLANA,
    _build_signing_callback,
    get_local_solana_sign_callback,
    wallet_chain_type,
)


def _unsigned_transfer_tx(payer: Pubkey) -> VersionedTransaction:
    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=payer, lamports=1))
    message = MessageV0.try_compile(payer, [ix], [], Hash.default())
    return VersionedTransaction.populate(message, [Signature.default()])


@pytest.mark.asyncio
async def test_local_solana_callback_signs_versioned_tx():
    keypair = Keypair()
    tx = _unsigned_transfer_tx(keypair.pubkey())

    callback = get_local_solana_sign_callback(str(keypair))
    assert callback.wallet_address is None
    assert callback.chain_type == CHAIN_TYPE_SOLANA

    signed_bytes = await callback(tx)
    signed = VersionedTransaction.from_bytes(signed_bytes)
    # The payer's slot now carries a real (non-default) signature.
    assert signed.signatures[0] != tx.signatures[0]


@pytest.mark.asyncio
async def test_remote_solana_callback_calls_backend():
    keypair = Keypair()
    tx = _unsigned_transfer_tx(keypair.pubkey())
    signed = await get_local_solana_sign_callback(str(keypair))(tx)

    wallet = {"address": "Sol1111", "type": "remote", "chain_type": "solana"}
    with patch(
        "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.sign_solana_transaction",
        new=AsyncMock(return_value=base64.b64encode(signed).decode()),
    ) as mock_sign:
        callback, address = _build_signing_callback(wallet, "sol")
        assert callback.chain_type == CHAIN_TYPE_SOLANA
        assert callback.wallet_address == "Sol1111"
        out = await callback(tx)

    mock_sign.assert_awaited_once()
    assert out == signed


def test_wallet_chain_type_defaults_to_ethereum():
    assert wallet_chain_type({"chain_type": "solana"}) == "solana"
    assert wallet_chain_type({}) == "ethereum"
    assert wallet_chain_type({"chain_type": None}) == "ethereum"

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm_tokens import build_solana_send_transaction
from wayfinder_paths.core.utils.svm_transaction import (
    _send_sponsored_svm_transaction,
    send_svm_versioned_transaction,
)

SENDER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
RECIPIENT = "8uqKmV5bMcoGw2AxoEMkseHF78ZDrAWpACvfUhxwmqqT"
SIGNATURE = "5" + "j" * 87


def _native_transfer_transaction() -> VersionedTransaction:
    sender = Pubkey.from_string(SENDER)
    message = MessageV0.try_compile(
        payer=sender,
        instructions=[
            transfer(
                TransferParams(
                    from_pubkey=sender,
                    to_pubkey=Pubkey.from_string(RECIPIENT),
                    lamports=1_000,
                )
            )
        ],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.default(),
    )
    signatures = [
        Signature.default() for _ in range(message.header.num_required_signatures)
    ]
    return VersionedTransaction.populate(message, signatures)


@pytest.mark.asyncio
async def test_native_send_builder_keeps_wallet_as_transfer_source():
    client = AsyncMock()
    client.get_latest_blockhash.return_value = SimpleNamespace(
        value=SimpleNamespace(
            blockhash=Hash.default(),
            last_valid_block_height=250_000_000,
        )
    )

    @asynccontextmanager
    async def fake_client(_chain_id: int):
        yield client

    with patch(
        "wayfinder_paths.core.utils.svm_tokens.solana_client_from_chain_id",
        fake_client,
    ):
        envelope = await build_solana_send_transaction(
            from_address=SENDER,
            to_address=RECIPIENT,
            token_address=ZERO_ADDRESS,
            amount=1_000,
        )

    transaction = VersionedTransaction.from_bytes(
        base64.b64decode(envelope["serializedTransaction"])
    )
    message = transaction.message
    transfer_instruction = message.instructions[0]

    assert message.account_keys[0] == Pubkey.from_string(SENDER)
    assert message.account_keys[transfer_instruction.accounts[0]] == Pubkey.from_string(
        SENDER
    )
    assert "allowFeePayerReplacement" not in envelope


@pytest.mark.asyncio
async def test_native_transfer_uses_managed_sponsorship():
    transaction = _native_transfer_transaction()
    sign_callback = AsyncMock()
    sign_callback.wallet_address = SENDER

    with (
        patch(
            "wayfinder_paths.core.utils.svm_transaction.sponsorship_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction._send_sponsored_svm_transaction",
            new=AsyncMock(return_value=SIGNATURE),
        ) as sponsored_send,
    ):
        result = await send_svm_versioned_transaction(
            transaction,
            sign_callback,
            wait_for_confirmation=False,
        )

    assert result == {
        "signature": SIGNATURE,
        "fee_lamports": None,
    }
    sponsored_send.assert_awaited_once_with(
        SENDER,
        transaction,
        CHAIN_ID_SOLANA,
    )
    sign_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sponsored_envelope_uses_privy_managed_fee_payer_contract():
    transaction = _native_transfer_transaction()
    sponsored_result = {"transaction_id": "privy-transaction"}

    with (
        patch(
            "wayfinder_paths.core.utils.svm_transaction.WALLET_CLIENT.send_privy_transaction_sponsored",
            new=AsyncMock(return_value=sponsored_result),
        ) as sponsored_send,
        patch(
            "wayfinder_paths.core.utils.svm_transaction.wait_for_sponsored_transaction",
            new=AsyncMock(return_value=SIGNATURE),
        ) as wait_for_transaction,
    ):
        result = await _send_sponsored_svm_transaction(
            SENDER,
            transaction,
            CHAIN_ID_SOLANA,
        )

    assert result == SIGNATURE
    sponsored_send.assert_awaited_once_with(
        SENDER,
        {
            "chainId": CHAIN_ID_SOLANA,
            "chainType": "solana",
            "serializedTransaction": base64.b64encode(bytes(transaction)).decode(),
        },
    )
    wait_for_transaction.assert_awaited_once_with(SENDER, sponsored_result)

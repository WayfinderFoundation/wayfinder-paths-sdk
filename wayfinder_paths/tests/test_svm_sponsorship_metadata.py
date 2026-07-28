from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.utils.svm_transaction import (
    send_svm_versioned_transaction,
)

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
RECIPIENT = "8uqKmV5bMcoGw2AxoEMkseHF78ZDrAWpACvfUhxwmqqT"
SPONSOR = "DmxEUYwgTX2qYnsf29LXBBScxYkKp1yn1Doodqf95SP7"
SIGNATURE = str(Signature.default())


def _transaction() -> VersionedTransaction:
    payer = Pubkey.from_string(WALLET)
    instruction = transfer(
        TransferParams(
            from_pubkey=payer,
            to_pubkey=Pubkey.from_string(RECIPIENT),
            lamports=1_000,
        )
    )
    message = MessageV0.try_compile(
        payer=payer,
        instructions=[instruction],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.default(),
    )
    return VersionedTransaction.populate(
        message,
        [Signature.default() for _ in range(message.header.num_required_signatures)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fee_payer", "expected_sponsored"),
    [(SPONSOR, True), (WALLET, False)],
)
async def test_send_reports_actual_fee_payer_and_sponsorship(
    fee_payer: str,
    expected_sponsored: bool,
):
    transaction = _transaction()
    sign_callback = AsyncMock(return_value=bytes(transaction))
    sign_callback.wallet_address = WALLET

    with (
        patch(
            "wayfinder_paths.core.utils.svm_transaction.sponsorship_enabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.apply_compute_budget",
            new=AsyncMock(return_value=transaction),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.send_svm_transaction",
            new=AsyncMock(return_value=SIGNATURE),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.confirm_svm_signature",
            new=AsyncMock(return_value={"confirmed": True}),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.get_svm_transaction_fee_metadata",
            new=AsyncMock(
                return_value={"fee_lamports": 10_000, "fee_payer": fee_payer}
            ),
        ),
    ):
        result = await send_svm_versioned_transaction(
            transaction,
            sign_callback,
        )

    assert result == {
        "signature": SIGNATURE,
        "fee_lamports": 10_000,
        "fee_payer": fee_payer,
        "sponsored": expected_sponsored,
    }
    sign_callback.assert_awaited_once()

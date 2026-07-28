from __future__ import annotations

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

from wayfinder_paths.core.utils.svm_transaction import (
    send_svm_versioned_transaction,
    simulate_svm_versioned_transaction,
)

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
RECIPIENT = "8uqKmV5bMcoGw2AxoEMkseHF78ZDrAWpACvfUhxwmqqT"
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
async def test_simulation_surfaces_program_error_and_logs():
    client = SimpleNamespace(
        simulate_transaction=AsyncMock(
            return_value=SimpleNamespace(
                value=SimpleNamespace(
                    err={"InstructionError": [2, {"Custom": 1}]},
                    logs=["Program log: slippage tolerance exceeded"],
                    units_consumed=42_000,
                )
            )
        )
    )

    @asynccontextmanager
    async def fake_client(_chain_id: int):
        yield client

    with patch(
        "wayfinder_paths.core.utils.svm_transaction.solana_client_from_chain_id",
        fake_client,
    ):
        with pytest.raises(
            RuntimeError,
            match="slippage tolerance exceeded",
        ):
            await simulate_svm_versioned_transaction(_transaction())


@pytest.mark.asyncio
async def test_invalid_transaction_stops_before_sponsorship_or_signing():
    transaction = _transaction()
    sign_callback = AsyncMock()
    sign_callback.wallet_address = WALLET

    with (
        patch(
            "wayfinder_paths.core.utils.svm_transaction.simulate_svm_versioned_transaction",
            new=AsyncMock(side_effect=RuntimeError("invalid route")),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.sponsorship_enabled",
            new=AsyncMock(),
        ) as sponsorship_enabled,
        patch(
            "wayfinder_paths.core.utils.svm_transaction._send_sponsored_svm_transaction",
            new=AsyncMock(),
        ) as sponsored_send,
    ):
        with pytest.raises(RuntimeError, match="invalid route"):
            await send_svm_versioned_transaction(transaction, sign_callback)

    sponsorship_enabled.assert_not_awaited()
    sponsored_send.assert_not_awaited()
    sign_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_fallback_reuses_preflight_compute_units():
    transaction = _transaction()
    sign_callback = AsyncMock(return_value=bytes(transaction))
    sign_callback.wallet_address = WALLET

    with (
        patch(
            "wayfinder_paths.core.utils.svm_transaction.simulate_svm_versioned_transaction",
            new=AsyncMock(return_value=42_000),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.sponsorship_enabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.apply_compute_budget",
            new=AsyncMock(return_value=transaction),
        ) as apply_budget,
        patch(
            "wayfinder_paths.core.utils.svm_transaction.send_svm_transaction",
            new=AsyncMock(return_value=SIGNATURE),
        ),
    ):
        result = await send_svm_versioned_transaction(
            transaction,
            sign_callback,
            wait_for_confirmation=False,
        )

    assert result == {
        "signature": SIGNATURE,
        "fee_lamports": None,
        "fee_payer": None,
        "sponsored": None,
    }
    apply_budget.assert_awaited_once_with(
        transaction,
        chain_id=900,
        cu_limit_multiplier=1.2,
        simulated_units=42_000,
    )

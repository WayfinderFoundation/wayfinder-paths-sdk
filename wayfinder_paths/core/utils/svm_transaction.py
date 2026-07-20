"""Solana (SVM) transaction fee handling, broadcast, and confirmation.

Built on the ``AsyncClient`` lifecycle in ``svm.py``. Kept separate from the
balance/ATA read helpers so the send/confirm surface — the fund-moving part —
stays isolated. ``send_solana_versioned_transaction`` mirrors the EVM
``send_transaction`` flow: sponsored backend broadcast for remote wallets
when enabled, otherwise compute-budget surgery + sign callback + local
broadcast + confirmation.
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger
from solana.rpc.models import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import CompiledInstruction
from solders.message import MessageHeader, MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus

from wayfinder_paths.core.clients.WalletClient import WALLET_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm import solana_client_from_chain_id
from wayfinder_paths.core.utils.transaction import (
    SponsorshipUnavailableError,
    TransactionRevertedError,
    sponsorship_enabled,
)

COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
# Protocol-level hard cap on compute units per transaction.
MAX_COMPUTE_UNIT_LIMIT = 1_400_000
# Our two ComputeBudget instructions aren't in the measured (pre-surgery)
# simulation, so add their cost — otherwise a tight multiplier on a small tx
# sets the limit below actual consumption once they run.
COMPUTE_BUDGET_IX_UNITS = 300


async def send_solana_transaction(
    serialized_b64: str,
    chain_id: int = CHAIN_ID_SOLANA,
    skip_preflight: bool = False,
) -> str:
    """Broadcast a base64-encoded, fully signed transaction.

    Returns the base58 transaction signature — used wherever EVM code passes
    ``tx_hash``.

    Preflight simulation is ON by default so simulation-detectable failures
    (insufficient funds, rent violations, program errors) surface as
    immediate RPC errors instead of confirmation timeouts. Pass
    ``skip_preflight=True`` to opt out (e.g. latency-sensitive sends where
    the transaction was already simulated).
    """
    raw = base64.b64decode(serialized_b64)
    async with solana_client_from_chain_id(chain_id) as client:
        resp = await client.send_raw_transaction(
            raw, opts=TxOpts(skip_preflight=skip_preflight)
        )
        return str(resp.value)


async def confirm_solana_signature(
    signature: str,
    chain_id: int = CHAIN_ID_SOLANA,
    timeout_s: float = 60,
) -> dict[str, Any]:
    """Poll signature status until confirmed/finalized, errored, or timeout.

    Returns ``{"signature", "slot", "err", "confirmation_status", "confirmed"}``.
    ``confirmed`` is True only when the transaction landed without error.
    Raises ``TimeoutError`` if no confirmation within ``timeout_s``.
    """
    sig = Signature.from_string(signature)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with solana_client_from_chain_id(chain_id) as client:
        while True:
            # search_transaction_history covers signatures older than the
            # node's recent-status cache (~150 blocks); without it, confirming
            # anything but a just-sent transaction times out.
            resp = await client.get_signature_statuses(
                [sig], search_transaction_history=True
            )
            status = resp.value[0]
            if status is not None:
                err = status.err
                confirmation_status = status.confirmation_status
                done = err is not None or confirmation_status in (
                    TransactionConfirmationStatus.Confirmed,
                    TransactionConfirmationStatus.Finalized,
                )
                if done:
                    return {
                        "signature": signature,
                        "slot": int(status.slot),
                        "err": str(err) if err is not None else None,
                        "confirmation_status": (
                            str(confirmation_status).rsplit(".", 1)[-1].lower()
                            if confirmation_status is not None
                            else None
                        ),
                        "confirmed": err is None,
                    }
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout_s}s waiting for Solana signature {signature}"
                )
            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Priority fees
# ---------------------------------------------------------------------------


async def get_recent_priority_fee(
    chain_id: int = CHAIN_ID_SOLANA,
    percentile: int = 85,
    floor: int = 1_000,
    ceiling: int = 3_000_000,
) -> int:
    """Recent priority fee in micro-lamports per compute unit.

    Uses ``getRecentPrioritizationFees`` and takes the given percentile of
    the NONZERO samples, clamped to ``[floor, ceiling]``. Zero-fee samples
    dominate the response on quiet slots and would peg the estimate to zero,
    so they are dropped; when every sample is zero the floor is returned.
    """
    async with solana_client_from_chain_id(chain_id) as client:
        resp = await client.get_recent_prioritization_fees()
    fees = sorted(f.prioritization_fee for f in resp.value if f.prioritization_fee > 0)
    if not fees:
        return floor
    # Nearest-rank percentile over the nonzero samples.
    fee = fees[max(0, math.ceil(percentile / 100 * len(fees)) - 1)]
    return max(floor, min(ceiling, fee))


# ---------------------------------------------------------------------------
# Compute budget
# ---------------------------------------------------------------------------


async def apply_compute_budget(
    tx: VersionedTransaction,
    chain_id: int = CHAIN_ID_SOLANA,
    cu_limit_multiplier: float = 1.2,
    priority_fee_micro_lamports: int | None = None,
) -> VersionedTransaction:
    """Set an exact compute-unit limit and priority fee on a v0 transaction.

    Ported from the legacy transaction service: simulate the transaction to
    measure units consumed, then rebuild the message with fresh
    ``SetComputeUnitLimit``/``SetComputeUnitPrice`` instructions, dropping any
    ComputeBudget instructions the builder (e.g. Jupiter) already attached —
    duplicates are illegal.

    When the ComputeBudget program is not among the static account keys it is
    appended at the END of the static keys (the readonly-unsigned region), so
    no static index moves; only compiled account indices pointing into the
    address-table-lookup region (>= the insertion position) shift up by one.
    Address table lookups, the blockhash, and signature placeholders are all
    preserved.
    """
    message = tx.message
    account_keys = list(message.account_keys)
    num_readonly_unsigned = message.header.num_readonly_unsigned_accounts
    cb_index = next(
        (i for i, key in enumerate(account_keys) if key == COMPUTE_BUDGET_PROGRAM_ID),
        None,
    )

    if cb_index is None:
        account_keys.append(COMPUTE_BUDGET_PROGRAM_ID)
        cb_index = len(account_keys) - 1
        num_readonly_unsigned += 1
        # Appending the program to the static keys pushes the address-table-lookup
        # region up by one; shift ALT account refs to match (program ids are always
        # static, so program_id_index never moves).
        instructions = [
            CompiledInstruction(
                program_id_index=ix.program_id_index,
                data=ix.data,
                accounts=bytes(a + 1 if a >= cb_index else a for a in ix.accounts),
            )
            for ix in message.instructions
        ]
    else:
        # Strip the builder's CU instructions — duplicate compute-budget ixs are rejected.
        instructions = [
            ix for ix in message.instructions if ix.program_id_index != cb_index
        ]

    async with solana_client_from_chain_id(chain_id) as client:
        sim = await client.simulate_transaction(tx, sig_verify=False)
    if sim.value.err is not None:
        raise RuntimeError(
            f"Solana transaction simulation failed: {sim.value.err}; "
            f"logs: {sim.value.logs}"
        )
    if not sim.value.units_consumed:
        raise RuntimeError("Solana transaction simulation returned no compute units")

    cu_limit = min(
        int(sim.value.units_consumed * cu_limit_multiplier) + COMPUTE_BUDGET_IX_UNITS,
        MAX_COMPUTE_UNIT_LIMIT,
    )
    if priority_fee_micro_lamports is None:
        priority_fee_micro_lamports = await get_recent_priority_fee(chain_id=chain_id)

    budget_instructions = [
        CompiledInstruction(program_id_index=cb_index, data=ix.data, accounts=b"")
        for ix in (
            set_compute_unit_price(priority_fee_micro_lamports),
            set_compute_unit_limit(cu_limit),
        )
    ]

    new_message = MessageV0(
        header=MessageHeader(
            num_required_signatures=message.header.num_required_signatures,
            num_readonly_signed_accounts=message.header.num_readonly_signed_accounts,
            num_readonly_unsigned_accounts=num_readonly_unsigned,
        ),
        account_keys=account_keys,
        recent_blockhash=message.recent_blockhash,
        instructions=budget_instructions + instructions,
        address_table_lookups=list(message.address_table_lookups),
    )
    return VersionedTransaction.populate(new_message, list(tx.signatures))


# ---------------------------------------------------------------------------
# Send flow (mirrors transaction.py::send_transaction)
# ---------------------------------------------------------------------------


async def _send_sponsored_solana_transaction(
    wallet_address: str, transaction: VersionedTransaction, chain_id: int
) -> str:
    """Submit via the backend's sponsored broadcast and return the signature.

    Mirrors the EVM ``send_sponsored_transaction``: a 4xx means the
    broadcaster refused the submission and nothing reached the chain, so it
    is safe to fall back to a local broadcast. 5xx/timeouts are ambiguous —
    the transaction may have been accepted — and stay fatal. The signature
    can lag the submit, so poll until it lands.
    """
    envelope = {
        "chainId": int(chain_id),
        "chainType": "solana",
        "serializedTransaction": base64.b64encode(bytes(transaction)).decode(),
    }
    try:
        result = await WALLET_CLIENT.send_privy_transaction_sponsored(
            wallet_address, envelope
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 402, 403, 429):
            raise SponsorshipUnavailableError(
                f"Sponsored send rejected with {exc.response.status_code}"
            ) from exc
        raise
    txn_hash = result["hash"]
    deadline = time.monotonic() + 120
    while not txn_hash:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Sponsored transaction {result['transaction_id']} has no hash "
                f"after 120s"
            )
        await asyncio.sleep(2)
        status = await WALLET_CLIENT.get_privy_transaction_status(
            wallet_address, result["transaction_id"]
        )
        # "failed" is pre-broadcast (no signature will ever land); an on-chain
        # failure still yields a signature and is caught by confirmation.
        if status["status"] == "failed":
            raise SponsorshipUnavailableError(
                f"Sponsored transaction {result['transaction_id']} failed before broadcast"
            )
        txn_hash = status["hash"]
    return txn_hash


async def send_solana_versioned_transaction(
    tx: VersionedTransaction,
    sign_callback: Callable,
    chain_id: int = CHAIN_ID_SOLANA,
    wait_for_confirmation: bool = True,
    cu_limit_multiplier: float = 1.2,
) -> str:
    """Sign and broadcast a v0 transaction; returns the base58 signature.

    With sponsorship enabled the backend signs, broadcasts, and covers fees;
    a refused submission falls back to compute-budget surgery + sign callback
    + local broadcast + (optional) confirmation.
    """
    if sign_callback is None:
        raise ValueError("sign_callback must be provided to send transaction")

    signature = None
    if await sponsorship_enabled():
        try:
            signature = await _send_sponsored_solana_transaction(
                sign_callback.wallet_address, tx, chain_id
            )
        except SponsorshipUnavailableError as exc:
            logger.warning(
                f"Sponsored send unavailable, falling back to local broadcast: {exc}"
            )
    if signature is None:
        budgeted = await apply_compute_budget(
            tx, chain_id=chain_id, cu_limit_multiplier=cu_limit_multiplier
        )
        signed_bytes = await sign_callback(budgeted)
        signature = await send_solana_transaction(
            base64.b64encode(signed_bytes).decode(), chain_id=chain_id
        )
    logger.info(f"Solana transaction broadcasted: {signature}")
    if wait_for_confirmation:
        status = await confirm_solana_signature(signature, chain_id=chain_id)
        if not status["confirmed"]:
            raise TransactionRevertedError(
                signature,
                status,
                message=f"Solana transaction failed: {signature} err={status['err']}",
            )
    return signature

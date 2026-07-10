"""Solana (SVM) transaction broadcast and confirmation.

Built on the ``AsyncClient`` lifecycle in ``svm.py``. Kept separate from the
balance/ATA read helpers so the send/confirm surface — the fund-moving part —
stays isolated.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from solana.rpc.models import TxOpts
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus

from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm import solana_client_from_chain_id


async def send_solana_transaction(
    serialized_b64: str,
    chain_id: int = CHAIN_ID_SOLANA,
    skip_preflight: bool = False,
) -> str:
    """Broadcast a base64-encoded, fully signed transaction.

    Accepts both legacy and versioned transactions (the wire encoding is
    opaque to the RPC). Returns the base58 transaction signature — used
    wherever EVM code passes ``tx_hash``.

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

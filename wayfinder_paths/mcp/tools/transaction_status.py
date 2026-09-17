"""Read-only reconciliation of an existing submission; never signs or broadcasts."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.rpc_errors import safe_rpc_error
from wayfinder_paths.core.utils.transaction import (
    TransactionConfirmationError,
    TransactionRevertedError,
    wait_for_transaction_receipt,
)
from wayfinder_paths.mcp.utils import catch_errors, err, ok


def bridge_transaction_status(bridge: dict[str, Any]) -> str:
    if bridge.get("is_success") is True:
        return "confirmed"
    if bridge.get("is_finished") is True or bridge.get("state") in {
        "failed",
        "refunded",
        "partial",
    }:
        return "failed"
    return "submitted"


def transaction_status_action(
    chain_id: int,
    txn_hash: str,
    bridge_tracking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "chain_id": chain_id,
        "txn_hash": txn_hash,
    }
    if bridge_tracking:
        arguments["bridge_tracking"] = bridge_tracking
    return {"tool": "onchain_get_transaction_status", "arguments": arguments}


def pending_transaction(
    error: TransactionConfirmationError,
    bridge_tracking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "submitted",
        "txn_hash": error.txn_hash,
        "chain_id": error.chain_id,
        "confirmation_waited": False,
        "confirmation_status": "unavailable",
        "error_code": "confirmation_unavailable",
        "message": str(error),
        "next_action": transaction_status_action(
            error.chain_id, error.txn_hash, bridge_tracking
        ),
    }
    explorer = get_etherscan_transaction_link(error.chain_id, error.txn_hash)
    if explorer:
        result["explorer_url"] = explorer
    return result


@catch_errors
async def onchain_get_transaction_status(
    chain_id: int,
    txn_hash: str,
    bridge_tracking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check an existing EVM transaction and optional bridge, without resubmitting.

    Use the arguments returned by an unresolved swap/send. Source confirmation
    does not prove destination delivery. An unavailable receipt is not a failure
    or permission to repeat the transaction. Checks are bounded to ten seconds.
    """
    if chain_id <= 0 or not re.fullmatch(r"0x[0-9a-fA-F]{64}", txn_hash):
        return err("invalid_argument", "Provide an EVM chain ID and transaction hash.")
    if bridge_tracking and bridge_tracking.get("from_chain") != chain_id:
        return err("invalid_argument", "Bridge source chain must match chain_id.")

    result = pending_transaction(
        TransactionConfirmationError(chain_id, txn_hash), bridge_tracking
    )
    result["source"] = {"status": "unavailable"}
    if bridge_tracking:
        result["destination"] = {"status": "pending"}
    try:
        async with asyncio.timeout(10):
            try:
                receipt = await wait_for_transaction_receipt(
                    chain_id, txn_hash, timeout=4, poll_interval=4, confirmations=0
                )
            except TransactionRevertedError:
                result["source"] = {"status": "failed", "receipt_status": 0}
                result["status"] = "failed"
            except Exception:
                # Provider details do not belong in the agent/user response.
                pass
            else:
                result["source"] = {
                    "status": "confirmed",
                    "receipt_status": 1,
                    "block_number": receipt["blockNumber"],
                }
                if not bridge_tracking:
                    result["status"] = "confirmed"

            if bridge_tracking and result["status"] != "failed":
                try:
                    async with asyncio.timeout(5):
                        bridge = await BRAP_CLIENT.wait_for_bridge_execution(
                            bridge_tracking=bridge_tracking,
                            tx_hash=txn_hash,
                            poll_interval_seconds=4,
                            timeout_seconds=4,
                        )
                except Exception:
                    pass
                else:
                    result["destination"] = {
                        key: safe_rpc_error(value)
                        if key in {"message", "error"} and value is not None
                        else value
                        for key, value in bridge.items()
                        if key not in {"raw_status", "status"}
                    }
                    result["status"] = bridge_transaction_status(bridge)
    except TimeoutError:
        pass

    if result["status"] != "submitted":
        for field in ("message", "error_code", "next_action", "confirmation_status"):
            result.pop(field, None)
        result["confirmation_waited"] = True
    elif result["source"]["status"] == "confirmed":
        result["confirmation_status"] = "confirmed"
        result["confirmation_waited"] = True
        result["error_code"] = "bridge_pending"
        result["message"] = (
            "Source transaction confirmed; destination delivery is not confirmed yet. "
            "Do not repeat the bridge. Check this transaction again."
        )
    return ok(result)

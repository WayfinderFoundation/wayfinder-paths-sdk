from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.chains import ARC_USDC_ADDRESS, CHAIN_ID_ARC
from wayfinder_paths.core.utils.tokens import is_native_token
from wayfinder_paths.core.utils.web3 import get_transaction_chain_id


def normalize_swap_token(token: dict[str, Any]) -> dict[str, Any]:
    """Use Arc's ERC-20 USDC interface for swaps, without changing gas metadata."""
    chain_id = token.get("chain_id") or (token.get("chain") or {}).get("id")
    if chain_id != CHAIN_ID_ARC or not is_native_token(token.get("address")):
        return token
    return {
        **token,
        "address": ARC_USDC_ADDRESS,
        "decimals": 6,
        # The original DB id described native (18-decimal) USDC, not this interface.
        "id": f"arc_{ARC_USDC_ADDRESS}",
        "token_id": f"arc_{ARC_USDC_ADDRESS}",
    }


def uses_solver_approvals(quote: dict[str, Any]) -> bool:
    """Direct solvers check allowances themselves, including both Permit2 grants."""
    return quote.get("provider") in {
        "uniswap_v3",
        "uniswap_v4",
        "pons_v2",
        "pons_v2_batch",
    }


def _prepare_transaction(
    calldata: Any, *, chain_id: int, sender: str, prerequisite: bool = False
) -> dict[str, Any]:
    if (
        not isinstance(calldata, dict)
        or not calldata.get("to")
        or not calldata.get("data")
    ):
        raise ValueError(
            "Quote missing complete calldata. Use BRAPClient's best_quote or "
            "the MCP quote's execution_quote, not the compact preview."
        )
    if "calls" in calldata:
        raise ValueError("Atomic BRAP routes require a batch-capable executor.")
    # Older single-swap quotes can omit chainId; prerequisites must be explicit.
    if prerequisite or calldata.get("chainId") is not None:
        if get_transaction_chain_id(calldata) != chain_id:
            raise ValueError(
                "Quote transaction chain does not match the source token chain."
            )
    if (
        calldata.get("from") is not None
        and to_checksum_address(calldata["from"]) != sender
    ):
        raise ValueError("Quote transaction sender does not match the signing wallet.")
    data = calldata["data"]
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("Quote transaction data must be hex calldata.")
    bytes.fromhex(data[2:])
    value = calldata.get("value", 0)
    value = (
        int(value, 16)
        if isinstance(value, str) and value.startswith("0x")
        else int(value)
    )
    if value < 0:
        raise ValueError("Quote transaction value cannot be negative.")
    transaction = {
        **calldata,
        "chainId": chain_id,
        "from": sender,
        "to": to_checksum_address(calldata["to"]),
        "value": value,
    }
    transaction.pop("description", None)  # Display metadata is not a signing field.
    return transaction


def prepare_brap_transactions(
    quote: dict[str, Any], *, chain_id: int, sender: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the entire source-chain sequence before signing any part of it."""
    if quote.get("atomic_calls") or quote.get("provider") == "pons_v2_batch":
        raise ValueError("Atomic BRAP routes require a batch-capable executor.")
    sender = to_checksum_address(sender)
    swap = _prepare_transaction(quote.get("calldata"), chain_id=chain_id, sender=sender)
    prerequisites = quote.get("prerequisite_transactions")
    if prerequisites is None:
        prerequisites = []
    if not isinstance(prerequisites, list):
        raise ValueError("Quote prerequisite_transactions must be a list.")
    return swap, [
        _prepare_transaction(tx, chain_id=chain_id, sender=sender, prerequisite=True)
        for tx in prerequisites
    ]

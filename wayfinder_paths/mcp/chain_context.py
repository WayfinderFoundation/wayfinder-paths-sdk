"""Small, on-demand chain facts for MCP responses, not global prompt catalogs."""

from collections.abc import Mapping
from typing import Any

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import (
    ARC_USDC_ADDRESS,
    CHAIN_ID_ARC,
    CHAIN_ID_TO_CODE,
    GAS_SPONSORED_CHAIN_IDS,
)


def with_chain_context(
    result: Mapping[str, Any], *chain_ids: int | None
) -> dict[str, Any]:
    """Explain Arc's dual USDC interface once, including cross-chain quotes."""
    if CHAIN_ID_ARC not in chain_ids:
        return dict(result)
    # https://docs.arc.io/arc/concepts/stablecoin-native-model
    return {
        **result,
        "chain_context": {
            "chain_id": CHAIN_ID_ARC,
            "chain_code": CHAIN_ID_TO_CODE[CHAIN_ID_ARC],
            "gas_sponsored": CHAIN_ID_ARC in GAS_SPONSORED_CHAIN_IDS,
            "usdc": {
                "native": {"address": ZERO_ADDRESS, "decimals": 18},
                "erc20": {"address": ARC_USDC_ADDRESS, "decimals": 6},
                "shared_balance": True,
                "wrapping_required": False,
            },
            "note": (
                "Native and ERC-20 USDC are the same asset and balance, not separate "
                "holdings. Swaps use the ERC-20 interface; no wrap or extra funding "
                "is needed to switch interfaces. Do not add the balances together. "
                "Keep USDC for gas from this shared balance."
            ),
        },
    }

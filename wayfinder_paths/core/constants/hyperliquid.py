from __future__ import annotations

from typing import Any

from wayfinder_paths.core.constants.contracts import (
    ARBITRUM_USDC as ARBITRUM_USDC_ADDRESS,
)
from wayfinder_paths.core.constants.contracts import (
    HYPE_FEE_WALLET,
)
from wayfinder_paths.core.constants.contracts import (
    HYPERLIQUID_BRIDGE as HYPERLIQUID_BRIDGE_ADDRESS,
)

# Re-export addresses for backwards compatibility
__all__ = [
    "ARBITRUM_USDC_ADDRESS",
    "ARBITRUM_USDC_TOKEN_ID",
    "HYPE_FEE_WALLET",
    "HYPERLIQUID_BRIDGE_ADDRESS",
    "DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP",
    "DEFAULT_HYPERLIQUID_BUILDER_FEE",
]

ARBITRUM_USDC_TOKEN_ID: str = "usd-coin-arbitrum"

# Tenths of a basis point: 30 -> 0.030% (3 bps)
DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP: int = 30

DEFAULT_HYPERLIQUID_BUILDER_FEE: dict[str, Any] = {
    "b": HYPE_FEE_WALLET,
    "f": DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
}

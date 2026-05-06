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
    "MARKET_SEARCH_ALIASES",
    "MIN_DEPOSIT_USD",
    "MIN_ORDER_USD_NOTIONAL",
]

ARBITRUM_USDC_TOKEN_ID: str = "usd-coin-arbitrum"

# Tenths of a basis point: 30 -> 0.030% (3 bps)
DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP: int = 30

DEFAULT_HYPERLIQUID_BUILDER_FEE: dict[str, Any] = {
    "b": HYPE_FEE_WALLET,
    "f": DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
}

# HL hard floors (HIP-4 outcomes are exempt — integer contract counts only).
MIN_DEPOSIT_USD: float = 5.0
MIN_ORDER_USD_NOTIONAL: float = 10.0

# HL wraps several majors with `k`/`u`/`U` prefixes (kBONK, uSOL, UBTC, UETH) and
# lists themed perps under HIP-3 builder dexes (xyz:BRENTOIL, vntl:ENERGY, etc.).
# Aliases let market search resolve common user phrasing to the on-book symbol.
MARKET_SEARCH_ALIASES: dict[str, frozenset[str]] = {
    "oil": frozenset(
        {
            "oil",
            "wti",
            "brent",
            "crude",
            "usoil",
            "brentoil",
            "energy",
            "gas",
            "natgas",
            "naturalgas",
        }
    ),
    "wti": frozenset({"oil", "wti", "crude", "usoil"}),
    "brent": frozenset({"oil", "brent", "crude", "brentoil"}),
    "crude": frozenset({"oil", "wti", "brent", "crude", "usoil", "brentoil"}),
    "gas": frozenset({"gas", "natgas", "naturalgas", "energy"}),
    "natgas": frozenset({"gas", "natgas", "naturalgas", "energy"}),
    "naturalgas": frozenset({"gas", "natgas", "naturalgas", "energy"}),
    "energy": frozenset({"energy", "oil", "gas", "natgas", "naturalgas"}),
    "btc": frozenset({"btc", "bitcoin", "ubtc"}),
    "bitcoin": frozenset({"btc", "bitcoin", "ubtc"}),
    "ubtc": frozenset({"btc", "bitcoin", "ubtc"}),
    "eth": frozenset({"eth", "ethereum", "ueth"}),
    "ethereum": frozenset({"eth", "ethereum", "ueth"}),
    "ueth": frozenset({"eth", "ethereum", "ueth"}),
    "sol": frozenset({"sol", "solana", "usol"}),
    "solana": frozenset({"sol", "solana", "usol"}),
    "usol": frozenset({"sol", "solana", "usol"}),
    "bonk": frozenset({"bonk", "kbonk"}),
    "kbonk": frozenset({"bonk", "kbonk"}),
    "nvidia": frozenset({"nvidia", "nvda"}),
    "nvda": frozenset({"nvidia", "nvda"}),
    "monad": frozenset({"monad", "mon"}),
    "mon": frozenset({"monad", "mon"}),
}

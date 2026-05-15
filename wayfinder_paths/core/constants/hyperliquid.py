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
    "MARKET_SEARCH_MIN_MATCH_SCORE",
    "MARKET_TYPE_HIP3",
    "MARKET_TYPE_HIP4",
    "MARKET_TYPE_PERP",
    "MARKET_TYPE_SPOT",
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

MIN_DEPOSIT_USD: float = 5.0
MIN_ORDER_USD_NOTIONAL: float = 10.0

MARKET_TYPE_PERP: str = "perp"
MARKET_TYPE_HIP3: str = "hip3"
MARKET_TYPE_SPOT: str = "spot"
MARKET_TYPE_HIP4: str = "hip4"

# Min `matches/min(len)` score for hyperliquid_search_market to keep a candidate.
MARKET_SEARCH_MIN_MATCH_SCORE: float = 0.9

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
    # Comparison-operator synonyms — outcome-side descriptions use >=/< etc.
    # which the search tool rewrites to the words "above"/"below" so these
    # aliases land. Helps queries like "btc above 80k" / "BTC under 78k".
    "above": frozenset({"above", "greater", "over", "exceeds", "gt", "gte"}),
    "greater": frozenset({"above", "greater", "over", "exceeds", "gt", "gte"}),
    "over": frozenset({"above", "greater", "over", "exceeds", "gt", "gte"}),
    "exceeds": frozenset({"above", "greater", "over", "exceeds", "gt", "gte"}),
    "below": frozenset({"below", "less", "under", "beneath", "lt", "lte"}),
    "less": frozenset({"below", "less", "under", "beneath", "lt", "lte"}),
    "under": frozenset({"below", "less", "under", "beneath", "lt", "lte"}),
    "beneath": frozenset({"below", "less", "under", "beneath", "lt", "lte"}),
    "between": frozenset({"between", "within", "inside", "in"}),
}

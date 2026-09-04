from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.mcp.utils import catch_errors, err, ok

ALL_CHAINS = ("all", "_")


@catch_errors(
    "Token could not be resolved, please use onchain_fuzzy_search_tokens() to find the token."
)
async def onchain_resolve_token(query: str) -> dict[str, Any]:
    """Resolve a token by canonical id/address; chain-scoped shorthands are tolerated.

    Args:
        query: Prefer coingecko_id-chain_code or chain_code_address. Shorthands like
            polygon_usdc or usdc-polygon can resolve, but use the returned canonical ID
            for quotes, execution, and scripts.
    """
    try:
        token = await TOKEN_CLIENT.get_token_details(query)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in (400, 404):
            return err(
                "token_not_resolved",
                "Token could not be resolved. Use onchain_fuzzy_search_tokens(chain_code, query) to find the canonical token/address id.",
                details={"status_code": status_code},
            )
        return err(
            "token_lookup_failed",
            "Token lookup failed in the backend.",
            details={"status_code": status_code},
        )
    return ok(token)


@catch_errors
async def onchain_get_gas_token(chain_code: str) -> dict[str, Any]:
    """Return the native gas token for a chain, e.g. ETH for base, POL for polygon.

    Args:
        chain_code: ethereum, base, arbitrum, polygon, bsc, avalanche, plasma,
            hyperevm, or solana.
    """
    token = await TOKEN_CLIENT.get_gas_token(chain_code)
    return ok(token)


@catch_errors
async def onchain_get_settlement_assets(chain_code: str) -> dict[str, Any]:
    """Return verified settlement assets and canonical token identities for a chain.

    Use this before selecting a destination stablecoin. If a requested symbol is
    absent, choose from settlement_assets instead of fuzzy-searching for a
    same-symbol contract.

    Args:
        chain_code: Chain code such as hyperevm, robinhood, base, or solana.
    """
    result = await TOKEN_CLIENT.get_canonical_assets(chain_code)
    return ok(result)


@catch_errors
async def onchain_fuzzy_search_tokens(chain_code: str, query: str) -> dict[str, Any]:
    """Fuzzy-search tokens on a chain by symbol, name, or address — use when an exact id isn't known.

    Args:
        chain_code: e.g. base or solana. Pass all or _ to search across every chain.
        query: name, symbol, or address. e.g. usdc, weth, wrapped eth, or 0x422...
    """
    chain = None if chain_code in ALL_CHAINS else chain_code
    result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
    return ok(result)


_LIST_DIMENSIONS = ("trending", "volume", "new", "active")


@catch_errors
async def onchain_list_tokens(
    chain_code: str, dimension: str = "trending", limit: int = 25
) -> dict[str, Any]:
    """Browse a chain's top tokens — what's actually live and moving right now.

    Use this to see what exists on a chain when you have no name to search: it
    surfaces the top tokens (including brand-new launches the standard catalog
    hasn't indexed) with price, liquidity, 24h volume, FDV, pool age, and DEX.
    To resolve one token by name/symbol/address instead, use
    onchain_fuzzy_search_tokens / onchain_resolve_token.

    Args:
        chain_code: the chain to browse, e.g. solana, robinhood, base, arbitrum.
            Solana SPL and Token-2022 discovery is supported with "solana".
        dimension: ranking — "trending" (default), "volume" (24h), "new"
            (recently launched), or "active" (most 24h transactions).
        limit: max tokens to return (1-50, default 25).
    """
    if chain_code not in CHAIN_CODE_TO_ID:
        return err(
            "unknown_chain_code",
            f"Unknown chain_code '{chain_code}'.",
            details={"valid": sorted(CHAIN_CODE_TO_ID)},
        )
    if dimension not in _LIST_DIMENSIONS:
        return err(
            "invalid_dimension",
            f"dimension must be one of: {', '.join(_LIST_DIMENSIONS)}",
        )
    result = await TOKEN_CLIENT.discover_tokens(chain_code, dimension, limit)
    return ok(result)


# Chains supported by the backend holder-intelligence endpoint. Deliberately
# narrower than CHAIN_CODE_TO_ID.
_HOLDER_INTEL_CHAINS = (
    "ethereum",
    "bsc",
    "polygon",
    "base",
    "arbitrum",
    "avalanche",
    "solana",
)


@catch_errors("Holder intelligence lookup failed")
async def onchain_token_holder_intel(
    chain_code: str, token_address: str, refresh: bool = False
) -> dict[str, Any]:
    """Analyze who holds a token, what they paid, and whether they are up.

    Use this before entering a low-liquidity or unfamiliar token to understand
    whether gains are broadly distributed, holders are retaining positions, and
    the largest wallets entered near or far below the current price. Returns:
    - holder_pnl: mean/median/p10/p90 PnL %, % profitable, realized+unrealized
    - hold_time: weighted average + median hold hours, diamond-hands % (>7d)
    - whale_entry: top-supply cohort (default 20%) VWAP entry vs current price,
      cohort PnL, first/last entry times
    - holder_stats: total holders and top-holder concentration
    - coverage: completeness and methodology metadata

    Always report `pnl_coverage_pct`, `holders_analyzed`, `swap_coverage`, and
    `hold_time.basis` so the user can judge the result's completeness. Results
    are cached for roughly 10 minutes; refresh only when the user needs a fresh
    recomputation because it costs more.

    Args:
        chain_code: ethereum, bsc, polygon, base, arbitrum, avalanche, or solana.
        token_address: EVM contract address or Solana mint address.
        refresh: bypass the cache and recompute from fresh upstream data.
    """
    if chain_code not in _HOLDER_INTEL_CHAINS:
        return err(
            "unsupported_chain",
            f"Holder intel is not available for '{chain_code}'.",
            details={"valid": sorted(_HOLDER_INTEL_CHAINS)},
        )
    chain_id = CHAIN_CODE_TO_ID[chain_code]
    try:
        result = await TOKEN_CLIENT.get_holder_intel(chain_id, token_address, refresh)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 404:
            return err(
                "token_not_found",
                "No holders or price found for this token on this chain.",
                details={"status_code": status_code},
            )
        if status_code == 503:
            return err(
                "not_configured",
                "Holder intelligence is not configured on this backend.",
                details={"status_code": status_code},
            )
        return err(
            "holder_intel_failed",
            "Holder intelligence lookup failed in the backend.",
            details={"status_code": status_code},
        )
    return ok(result)

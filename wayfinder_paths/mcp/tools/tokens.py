from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.mcp.arg_validation import MCPArgumentError
from wayfinder_paths.mcp.tool_annotations import ChainCode, TokenQuery
from wayfinder_paths.mcp.utils import catch_errors, err, ok

ALL_CHAINS = ("all", "_")


@catch_errors(
    "Token could not be resolved, please use onchain_fuzzy_search_tokens() to find the token."
)
async def onchain_resolve_token(query: TokenQuery) -> dict[str, Any]:
    """Resolve a token by canonical id/address; chain-scoped shorthands are tolerated.

    Prefer `coingecko-id-chain` or `chain_address`. Shorthands may resolve, but
    always use the returned canonical id for quotes and execution.
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
async def onchain_get_gas_token(chain_code: ChainCode) -> dict[str, Any]:
    """Return a chain's native gas token, e.g. ETH for Base or POL for Polygon."""
    if chain_code not in CHAIN_CODE_TO_ID:
        raise MCPArgumentError(
            "chain_code is not supported",
            field="chain_code",
            received=chain_code,
            allowed_values=CHAIN_CODE_TO_ID,
        )
    token = await TOKEN_CLIENT.get_gas_token(chain_code)
    return ok(token)


@catch_errors
async def onchain_fuzzy_search_tokens(
    chain_code: ChainCode, query: str
) -> dict[str, Any]:
    """Fuzzy-search tokens on a chain by symbol, name, or address — use when an exact id isn't known.

    Args:
        chain_code: e.g. base. Pass all or _ to search across every chain.
        query: name, symbol, or address. e.g. usdc, weth, wrapped eth, or 0x422...
    """
    if chain_code not in CHAIN_CODE_TO_ID and chain_code not in ALL_CHAINS:
        raise MCPArgumentError(
            "chain_code is not supported; use all or _ for every chain",
            field="chain_code",
            received=chain_code,
            allowed_values={*CHAIN_CODE_TO_ID, *ALL_CHAINS},
        )
    chain = None if chain_code in ALL_CHAINS else chain_code
    result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
    return ok(result)


_LIST_DIMENSIONS = ("trending", "volume", "new", "active")


@catch_errors
async def onchain_list_tokens(
    chain_code: ChainCode, dimension: str = "trending", limit: int = 25
) -> dict[str, Any]:
    """Browse live tokens on one chain by trending, volume, new, or active rank.

    Results include price, liquidity, volume, FDV, age, and DEX, including
    launches absent from the standard catalog. Use token search when you already
    know a name/address. Limit is 1–50.
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

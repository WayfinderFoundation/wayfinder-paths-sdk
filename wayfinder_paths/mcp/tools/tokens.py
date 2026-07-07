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
        chain_code: ethereum, base, arbitrum, polygon, bsc, avalanche, plasma, or hyperevm.
    """
    token = await TOKEN_CLIENT.get_gas_token(chain_code)
    return ok(token)


@catch_errors
async def onchain_fuzzy_search_tokens(chain_code: str, query: str) -> dict[str, Any]:
    """Fuzzy-search tokens on a chain by symbol, name, or address — use when an exact id isn't known.

    Args:
        chain_code: e.g. base. Pass all or _ to search across every chain.
        query: name, symbol, or address. e.g. usdc, weth, wrapped eth, or 0x422...
    """
    chain = None if chain_code in ALL_CHAINS else chain_code
    result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
    return ok(result)


@catch_errors
async def onchain_list_tokens(
    chain_code: str, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List a chain's top tokens by 24h volume — enumeration, no search string needed.

    Use this to see what tokens exist on a chain. Unlike onchain_fuzzy_search_tokens,
    query is optional: omit it to get the highest-volume tokens, or pass it to filter.

    Args:
        chain_code: e.g. base, arbitrum, polygon. Pass all or _ to list across every chain.
        query: optional symbol/name filter; omit to list top tokens by volume.
        limit: max rows, capped at 500 by the backend. Defaults to 50.
    """
    if chain_code in ALL_CHAINS:
        chain_id = None
    elif chain_code in CHAIN_CODE_TO_ID:
        chain_id = CHAIN_CODE_TO_ID[chain_code]
    else:
        return err(
            "unknown_chain_code",
            f"Unknown chain_code '{chain_code}'. Pass all to list across every chain.",
            details={"valid": sorted(CHAIN_CODE_TO_ID)},
        )
    tokens = await TOKEN_CLIENT.list_markets(
        chain_id=chain_id, query=query, limit=limit
    )
    return ok({"tokens": tokens})

from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, err, ok


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
    chain = None if chain_code in ("all", "_") else chain_code
    result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
    return ok(result)

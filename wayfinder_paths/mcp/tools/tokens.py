from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors(
    "Token could not be resolved, please use onchain_fuzzy_search_tokens() to find the token."
)
async def onchain_resolve_token(query: str) -> dict[str, Any]:
    """Resolve a token by exact id like coingecko_id-chain_code or chain_code_address.

    Args:
        query: Canonical token id or chain-prefixed address.
    """
    token = await TOKEN_CLIENT.get_token_details(query)
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

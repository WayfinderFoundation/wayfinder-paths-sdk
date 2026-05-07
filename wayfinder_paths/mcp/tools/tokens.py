from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def onchain_resolve_token(query: str) -> dict[str, Any]:
    token = await TOKEN_CLIENT.get_token_details(query)
    return ok({"token": token})


@catch_errors
async def onchain_get_gas_token(chain_code: str) -> dict[str, Any]:
    token = await TOKEN_CLIENT.get_gas_token(chain_code)
    return ok({"token": token})


@catch_errors
async def onchain_fuzzy_search_tokens(chain_code: str, query: str) -> dict[str, Any]:
    chain = None if chain_code in ("all", "_") else chain_code
    result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
    return ok(result)

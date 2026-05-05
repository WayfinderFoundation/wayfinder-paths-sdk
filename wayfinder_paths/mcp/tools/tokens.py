from __future__ import annotations

import json

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT


async def onchain_resolve_token(query: str) -> str:
    try:
        token = await TOKEN_CLIENT.get_token_details(query)
        return json.dumps({"token": token}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def onchain_get_gas_token(chain_code: str) -> str:
    try:
        token = await TOKEN_CLIENT.get_gas_token(chain_code)
        return json.dumps({"token": token}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def onchain_fuzzy_search_tokens(chain_code: str, query: str) -> str:
    try:
        chain = None if chain_code in ("all", "_") else chain_code
        result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

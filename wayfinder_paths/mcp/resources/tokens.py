from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT

TOKEN_SEARCH_LIMIT = 5


def _compact_token(token: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": token.get("id"),
        "symbol": token.get("symbol"),
        "name": token.get("name"),
        "chain": token.get("chain"),
        "address": token.get("address"),
    }


async def resolve_token(query: str) -> str:
    try:
        token = await TOKEN_CLIENT.get_token_details(query)
        result = _compact_token(token) if isinstance(token, dict) else token
        return json.dumps({"token": result, "detail_level": "select"}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def get_gas_token(chain_code: str) -> str:
    try:
        token = await TOKEN_CLIENT.get_gas_token(chain_code)
        result = _compact_token(token) if isinstance(token, dict) else token
        return json.dumps({"token": result, "detail_level": "select"}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def fuzzy_search_tokens(chain_code: str, query: str) -> str:
    try:
        chain = None if chain_code in ("all", "_") else chain_code
        result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
        entries = result.get("results", []) if isinstance(result, dict) else []
        compact = [
            _compact_token(entry)
            for entry in entries[:TOKEN_SEARCH_LIMIT]
            if isinstance(entry, dict)
        ]
        return json.dumps(
            {
                "query": query,
                "chain_code": chain_code,
                "result_count": len(entries),
                "results": compact,
                "detail_uri": f"wayfinder://tokens/search-full/{chain_code}/{query}",
            },
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def fuzzy_search_tokens_full(chain_code: str, query: str) -> str:
    try:
        chain = None if chain_code in ("all", "_") else chain_code
        result = await TOKEN_CLIENT.fuzzy_search(query, chain=chain)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

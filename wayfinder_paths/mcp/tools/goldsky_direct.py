from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.core.clients.direct.goldsky_registry import (
    goldsky_schema,
    search_goldsky_endpoints,
)
from wayfinder_paths.core.clients.direct.GoldskyDirectClient import (
    GOLDSKY_DIRECT_CLIENT,
)
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def research_goldsky_graphql(
    endpoint: str,
    query: str,
    variables: str = "{}",
) -> dict[str, Any]:
    """Run a direct Goldsky GraphQL query from the OpenCode runtime.

    Args:
        endpoint: Goldsky GraphQL endpoint under https://api.goldsky.com/api/public/
            or https://api.goldsky.com/api/private/.
        query: Read-only GraphQL query. Mutations and subscriptions are blocked.
        variables: JSON object string for GraphQL variables.
    """
    try:
        parsed_variables = json.loads(variables or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("variables must be valid JSON") from exc
    if not isinstance(parsed_variables, dict):
        raise ValueError("variables must be a JSON object")

    return ok(
        await GOLDSKY_DIRECT_CLIENT.query(
            endpoint=endpoint,
            query=query,
            variables=parsed_variables,
        )
    )


@catch_errors
async def research_goldsky_search(
    query: str = "_",
    chain: str = "_",
    protocol: str = "_",
    dataset: str = "_",
) -> dict[str, Any]:
    """Search known Goldsky endpoints available to the Wayfinder runtime."""
    results = search_goldsky_endpoints(
        query=None if query == "_" else query,
        chain=None if chain == "_" else chain,
        protocol=None if protocol == "_" else protocol,
        dataset=None if dataset == "_" else dataset,
    )
    return ok({"provider": "goldsky", "results": results})


@catch_errors
async def research_goldsky_schema(endpointId: str) -> dict[str, Any]:
    """Return static schema notes for a known Goldsky endpoint."""
    return ok({"provider": "goldsky", "result": goldsky_schema(endpointId)})

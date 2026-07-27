from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.direct.goldsky_registry import (
    goldsky_schema,
    search_goldsky_endpoints,
)
from wayfinder_paths.core.clients.direct.GoldskyDirectClient import (
    GOLDSKY_DIRECT_CLIENT,
)
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    optional_json_object,
)
from wayfinder_paths.mcp.tool_annotations import GoldskyEndpoint, JsonObjectInput
from wayfinder_paths.mcp.utils import catch_errors, ok


@catch_errors
async def research_goldsky_graphql(
    endpoint: GoldskyEndpoint,
    query: str,
    variables: JsonObjectInput = "{}",
) -> dict[str, Any]:
    """Run read-only GraphQL against a Goldsky public/private API endpoint.

    Mutations and subscriptions are blocked. `variables` must be a JSON object string.
    """
    parsed_variables = optional_json_object(variables, field_name="variables") or {}

    try:
        return ok(
            await GOLDSKY_DIRECT_CLIENT.query(
                endpoint=endpoint,
                query=query,
                variables=parsed_variables,
            )
        )
    except ValueError as exc:
        message = str(exc)
        field = "endpoint" if "endpoint" in message.lower() else "query"
        raise MCPArgumentError(
            message,
            field=field,
            received=endpoint if field == "endpoint" else query,
            suggested_arguments={
                field: (
                    "Use an endpoint from research_goldsky_search"
                    if field == "endpoint"
                    else "query { ... }"
                )
            },
        ) from exc


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

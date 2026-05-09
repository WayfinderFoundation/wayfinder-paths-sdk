from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.ResearchClient import RESEARCH_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, ok


def _optional_int(value: str, *, field_name: str) -> int | None:
    raw = str(value).strip()
    if not raw or raw == "_":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


@catch_errors
async def research_web_search(
    query: str,
    numResults: str = "8",
    type: str = "auto",
    livecrawl: str = "fallback",
    contextMaxCharacters: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Search the public web through the Wayfinder Research Gateway.

    Args:
        query: Search query. Do not include secrets, tokens, or private URLs.
        numResults: Max result count (default "8", range 1-100).
        type: Search type: "auto", "fast", or "deep".
        livecrawl: Live crawl policy: "fallback" or "preferred".
        contextMaxCharacters: Optional excerpt character cap (500-50000). Use "_"
            to let the backend default apply.
        sessionID: Optional OpenCode session id. Use "_" to resolve from the
            runtime environment or SDK default.
    """
    context_max = _optional_int(
        contextMaxCharacters,
        field_name="contextMaxCharacters",
    )
    result = await RESEARCH_CLIENT.search(
        query=query,
        num_results=int(numResults),
        search_type=type,  # type: ignore[arg-type]
        livecrawl=livecrawl,  # type: ignore[arg-type]
        context_max_characters=context_max,
        session_id=sessionID,
    )
    return ok(result)

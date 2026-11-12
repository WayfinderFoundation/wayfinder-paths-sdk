"""Read-only replay using the SDK's real web tool schemas; never calls a provider."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.core.clients.ResearchClient import ResearchClient
from wayfinder_paths.mcp.tools import research_gateway


class ReplayResearchClient(ResearchClient):
    async def _post_gateway(self, path: str, payload: Mapping[str, Any]) -> Any:
        sources = json.loads(Path(__file__).with_name("sources.json").read_text())
        if path == "webfetch":
            results = []
            statuses = []
            for url in payload["urls"]:
                source = next(
                    (
                        s
                        for s in sources
                        if url.rstrip("/") in [s["url"], *s["aliases"]]
                    ),
                    None,
                )
                if source:
                    results.append({**source, "url": url})
                statuses.append(
                    {"url": url, "status": "success" if source else "unavailable"}
                )
            return {"query": dict(payload), "results": results, "statuses": statuses}
        if path == "websearch":
            query = payload["query"].lower()
            matches = [
                s for s in sources if any(word in query for word in s["search_terms"])
            ]
            for field, include in (("includeDomains", True), ("excludeDomains", False)):
                domains = payload.get(field) or []
                if domains:
                    matches = [
                        s
                        for s in matches
                        if (urlsplit(s["url"]).hostname in domains) == include
                    ]
            return {"query": dict(payload), "results": matches[: payload["numResults"]]}
        raise ValueError(f"No replay for {path}; live provider fallback is disabled")


def build_server() -> FastMCP:
    research_gateway.RESEARCH_CLIENT = ReplayResearchClient()
    server = FastMCP("wayfinder")
    # Keep real schemas/validation but bypass catch_errors' live metric reporting.
    server.add_tool(inspect.unwrap(research_gateway.core_web_search))
    server.add_tool(inspect.unwrap(research_gateway.core_web_fetch))
    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")

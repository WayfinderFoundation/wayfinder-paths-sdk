from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

ALLOWED_HOST = "api.goldsky.com"
MAX_QUERY_CHARS = 12_000
MAX_VARIABLES_CHARS = 50_000
MAX_RESPONSE_CHARS = 200_000
TIMEOUT_SECONDS = 20


class GoldskyDirectClient:
    """Direct Goldsky GraphQL client.

    This intentionally does not call the Wayfinder backend.
    """

    def _validate_endpoint(self, endpoint: str) -> str:
        endpoint = endpoint.strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.netloc != ALLOWED_HOST:
            raise ValueError("Goldsky endpoint must be https://api.goldsky.com")
        if not (
            parsed.path.startswith("/api/public/")
            or parsed.path.startswith("/api/private/")
        ):
            raise ValueError(
                "Goldsky endpoint must be /api/public/... or /api/private/..."
            )
        if not parsed.path.endswith("/gn"):
            raise ValueError("Goldsky endpoint must end with /gn")
        return endpoint

    def _validate_query(self, query: str) -> str:
        query = query.strip()
        lowered = query.lower()
        if not query:
            raise ValueError("query is required")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must be {MAX_QUERY_CHARS} characters or fewer")
        if "mutation" in lowered or "subscription" in lowered:
            raise ValueError("only read-only GraphQL queries are allowed")
        return query

    async def query(
        self,
        *,
        endpoint: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = self._validate_endpoint(endpoint)
        query = self._validate_query(query)
        variables = variables or {}
        if len(str(variables)) > MAX_VARIABLES_CHARS:
            raise ValueError(
                f"variables must be {MAX_VARIABLES_CHARS} characters or fewer"
            )

        headers = {"Content-Type": "application/json"}
        if "/api/private/" in endpoint:
            token = os.environ.get("GOLDSKY_API_TOKEN", "").strip()
            if not token:
                raise RuntimeError(
                    "GOLDSKY_API_TOKEN is required for private endpoints"
                )
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                endpoint,
                headers=headers,
                json={"query": query, "variables": variables},
            )
            response.raise_for_status()
            body = response.json()
        body = self._truncate_response(body)

        return {
            "provider": "goldsky",
            "endpoint": endpoint,
            "result": body,
            "evidence": [
                {
                    "provider": "goldsky",
                    "sourceType": "graphql",
                    "url": endpoint,
                    "clientDirect": True,
                }
            ],
        }

    def _truncate_response(self, body: Any) -> Any:
        rendered = json.dumps(body, default=str)
        if len(rendered) <= MAX_RESPONSE_CHARS:
            return body
        return {
            "truncated": True,
            "maxResponseCharacters": MAX_RESPONSE_CHARS,
            "preview": rendered[:MAX_RESPONSE_CHARS],
        }


GOLDSKY_DIRECT_CLIENT = GoldskyDirectClient()

from __future__ import annotations

import os
from typing import Any, cast

import httpx

from wayfinder_paths.core.clients.research_types import (
    ResearchGatewayErrorBody,
    ResearchWebSearchLivecrawl,
    ResearchWebSearchRequest,
    ResearchWebSearchResponse,
    ResearchWebSearchType,
)
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

VALID_SEARCH_TYPES: set[str] = {"auto", "fast", "deep"}
VALID_LIVECRAWL_VALUES: set[str] = {"fallback", "preferred"}
DEFAULT_SESSION_ID = "mcp"
SESSION_ENV_KEYS = (
    "WAYFINDER_RESEARCH_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "OPENCODE_SESSIONID",
    "OPENCODE_INSTANCE_ID",
)


class ResearchGatewayAPIError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        error_type: str,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.message = message
        self.details = details


class ResearchClient(WayfinderClient):
    """Client for the Wayfinder Research Gateway."""

    def _research_url(self, path: str) -> str:
        base = get_api_base_url().rstrip("/")
        suffix = path.strip("/")
        return f"{base}/research/{suffix}/"

    async def search(
        self,
        *,
        query: str,
        num_results: int = 8,
        search_type: ResearchWebSearchType = "auto",
        livecrawl: ResearchWebSearchLivecrawl = "fallback",
        context_max_characters: int | None = None,
        session_id: str | None = None,
    ) -> ResearchWebSearchResponse:
        """Search the web through the backend-controlled research gateway."""
        payload = self._search_payload(
            query=query,
            num_results=num_results,
            search_type=search_type,
            livecrawl=livecrawl,
            context_max_characters=context_max_characters,
            session_id=session_id,
        )

        try:
            response = await self._authed_request(
                "POST",
                self._research_url("websearch"),
                json=payload,
            )
        except httpx.HTTPStatusError as exc:
            raise _gateway_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise ResearchGatewayAPIError(
                status_code=0,
                error_type="provider_failure",
                code="gateway_unavailable",
                message="Research gateway request failed",
            ) from exc

        return response.json()

    def _search_payload(
        self,
        *,
        query: str,
        num_results: int,
        search_type: str,
        livecrawl: str,
        context_max_characters: int | None,
        session_id: str | None,
    ) -> ResearchWebSearchRequest:
        normalized_query = str(query).strip()
        if not normalized_query:
            raise ValueError("query is required")

        if not 1 <= int(num_results) <= 100:
            raise ValueError("num_results must be between 1 and 100")

        normalized_type = str(search_type).strip().lower()
        if normalized_type not in VALID_SEARCH_TYPES:
            raise ValueError(
                f"search_type must be one of: {', '.join(sorted(VALID_SEARCH_TYPES))}"
            )
        typed_search_type = cast(ResearchWebSearchType, normalized_type)

        normalized_livecrawl = str(livecrawl).strip().lower()
        if normalized_livecrawl not in VALID_LIVECRAWL_VALUES:
            raise ValueError(
                "livecrawl must be one of: "
                f"{', '.join(sorted(VALID_LIVECRAWL_VALUES))}"
            )
        typed_livecrawl = cast(ResearchWebSearchLivecrawl, normalized_livecrawl)

        payload: ResearchWebSearchRequest = {
            "query": normalized_query,
            "numResults": int(num_results),
            "type": typed_search_type,
            "livecrawl": typed_livecrawl,
            "sessionID": self.resolve_session_id(session_id),
        }
        if context_max_characters is not None:
            if not 500 <= int(context_max_characters) <= 50000:
                raise ValueError("context_max_characters must be between 500 and 50000")
            payload["contextMaxCharacters"] = int(context_max_characters)
        return payload

    @staticmethod
    def resolve_session_id(session_id: str | None = None) -> str:
        explicit = str(session_id or "").strip()
        if explicit and explicit != "_":
            if len(explicit) > 200:
                raise ValueError("session_id must be 200 characters or fewer")
            return explicit

        for key in SESSION_ENV_KEYS:
            value = os.environ.get(key, "").strip()
            if value:
                return value[:200]
        return DEFAULT_SESSION_ID


def _gateway_error_from_response(response: httpx.Response) -> ResearchGatewayAPIError:
    error = _extract_gateway_error(response)
    return ResearchGatewayAPIError(
        status_code=response.status_code,
        error_type=error.get("type") or "http_error",
        code=error.get("code") or "http_error",
        message=error.get("message") or response.reason_phrase or "Research gateway error",
        details=error.get("details"),
    )


def _extract_gateway_error(response: httpx.Response) -> ResearchGatewayErrorBody:
    try:
        body = response.json()
    except ValueError:
        return {
            "type": "http_error",
            "code": "http_error",
            "message": response.text[:200] or response.reason_phrase,
        }

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return {
                "type": str(error.get("type") or "http_error"),
                "code": str(error.get("code") or "http_error"),
                "message": str(
                    error.get("message") or response.reason_phrase or "Research gateway error"
                ),
                "details": error.get("details"),
            }
    return {
        "type": "http_error",
        "code": "http_error",
        "message": response.reason_phrase or "Research gateway error",
    }


RESEARCH_CLIENT = ResearchClient()

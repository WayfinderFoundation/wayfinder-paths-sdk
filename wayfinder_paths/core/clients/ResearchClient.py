from __future__ import annotations

import os
from typing import Any, cast

import httpx

from wayfinder_paths.core.clients.research_types import (
    ResearchGatewayErrorBody,
    ResearchWebContentType,
    ResearchWebFetchRequest,
    ResearchWebFetchResponse,
    ResearchWebSearchCategory,
    ResearchWebSearchLivecrawl,
    ResearchWebSearchRequest,
    ResearchWebSearchResponse,
    ResearchWebSearchType,
)
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

VALID_SEARCH_TYPES: set[str] = {
    "auto",
    "fast",
    "instant",
    "deep-lite",
    "deep",
    "deep-reasoning",
    "neural",
}
VALID_SEARCH_CATEGORIES: set[str] = {
    "company",
    "people",
    "research paper",
    "news",
    "personal site",
    "financial report",
}
VALID_LIVECRAWL_VALUES: set[str] = {"fallback", "preferred"}
VALID_CONTENT_TYPES: set[str] = {"highlights", "text", "summary"}
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
        category: ResearchWebSearchCategory | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        start_published_date: str | None = None,
        end_published_date: str | None = None,
        max_age_hours: int | None = None,
        additional_queries: list[str] | None = None,
        content_type: ResearchWebContentType = "highlights",
        livecrawl: ResearchWebSearchLivecrawl = "fallback",
        context_max_characters: int | None = None,
        session_id: str | None = None,
    ) -> ResearchWebSearchResponse:
        """Search the web through the backend-controlled research gateway."""
        payload = self._search_payload(
            query=query,
            num_results=num_results,
            search_type=search_type,
            category=category,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            start_published_date=start_published_date,
            end_published_date=end_published_date,
            max_age_hours=max_age_hours,
            additional_queries=additional_queries,
            content_type=content_type,
            livecrawl=livecrawl,
            context_max_characters=context_max_characters,
            session_id=session_id,
        )

        return await self._post_gateway("websearch", payload)

    async def fetch(
        self,
        *,
        urls: list[str],
        query: str | None = None,
        content_type: ResearchWebContentType = "text",
        livecrawl: ResearchWebSearchLivecrawl = "fallback",
        max_age_hours: int | None = None,
        subpages: int | None = None,
        subpage_target: list[str] | None = None,
        context_max_characters: int | None = None,
        session_id: str | None = None,
    ) -> ResearchWebFetchResponse:
        """Fetch public URLs through the backend-controlled research gateway."""
        payload = self._fetch_payload(
            urls=urls,
            query=query,
            content_type=content_type,
            livecrawl=livecrawl,
            max_age_hours=max_age_hours,
            subpages=subpages,
            subpage_target=subpage_target,
            context_max_characters=context_max_characters,
            session_id=session_id,
        )
        return await self._post_gateway("webfetch", payload)

    async def _post_gateway(
        self,
        path: str,
        payload: ResearchWebSearchRequest | ResearchWebFetchRequest,
    ) -> Any:
        try:
            response = await self._authed_request(
                "POST",
                self._research_url(path),
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
        category: str | None,
        include_domains: list[str] | None,
        exclude_domains: list[str] | None,
        start_published_date: str | None,
        end_published_date: str | None,
        max_age_hours: int | None,
        additional_queries: list[str] | None,
        content_type: str,
        livecrawl: str,
        context_max_characters: int | None,
        session_id: str | None,
    ) -> ResearchWebSearchRequest:
        normalized_query = str(query).strip()
        if not normalized_query:
            raise ValueError("query is required")

        if not 1 <= int(num_results) <= 100:
            raise ValueError("num_results must be between 1 and 100")

        payload: ResearchWebSearchRequest = {
            "query": normalized_query,
            "numResults": int(num_results),
            "type": self._search_type(search_type),
            "contentType": self._content_type(content_type),
            "livecrawl": self._livecrawl(livecrawl),
            "sessionID": self.resolve_session_id(session_id),
        }
        if normalized_category := self._optional_category(category):
            payload["category"] = normalized_category
        if include_domains:
            payload["includeDomains"] = self._string_list(
                include_domains, "include_domains"
            )
        if exclude_domains:
            payload["excludeDomains"] = self._string_list(
                exclude_domains, "exclude_domains"
            )
        if start_published_date:
            payload["startPublishedDate"] = str(start_published_date).strip()
        if end_published_date:
            payload["endPublishedDate"] = str(end_published_date).strip()
        if max_age_hours is not None:
            payload["maxAgeHours"] = self._bounded_int(
                max_age_hours,
                field_name="max_age_hours",
                min_value=0,
                max_value=720,
            )
        if additional_queries:
            payload["additionalQueries"] = self._string_list(
                additional_queries,
                "additional_queries",
            )
        if context_max_characters is not None:
            payload["contextMaxCharacters"] = self._context_max(context_max_characters)
        return payload

    def _fetch_payload(
        self,
        *,
        urls: list[str],
        query: str | None,
        content_type: str,
        livecrawl: str,
        max_age_hours: int | None,
        subpages: int | None,
        subpage_target: list[str] | None,
        context_max_characters: int | None,
        session_id: str | None,
    ) -> ResearchWebFetchRequest:
        payload: ResearchWebFetchRequest = {
            "urls": self._string_list(urls, "urls"),
            "contentType": self._content_type(content_type),
            "livecrawl": self._livecrawl(livecrawl),
            "sessionID": self.resolve_session_id(session_id),
        }
        if query is not None and str(query).strip() and str(query).strip() != "_":
            payload["query"] = str(query).strip()
        if max_age_hours is not None:
            payload["maxAgeHours"] = self._bounded_int(
                max_age_hours,
                field_name="max_age_hours",
                min_value=0,
                max_value=720,
            )
        if subpages is not None:
            payload["subpages"] = self._bounded_int(
                subpages,
                field_name="subpages",
                min_value=0,
                max_value=10,
            )
        if subpage_target:
            payload["subpageTarget"] = self._string_list(
                subpage_target, "subpage_target"
            )
        if context_max_characters is not None:
            payload["contextMaxCharacters"] = self._context_max(context_max_characters)
        return payload

    def _search_type(self, value: str) -> ResearchWebSearchType:
        normalized = str(value).strip().lower()
        if normalized not in VALID_SEARCH_TYPES:
            raise ValueError(
                f"search_type must be one of: {', '.join(sorted(VALID_SEARCH_TYPES))}"
            )
        return cast(ResearchWebSearchType, normalized)

    def _optional_category(self, value: str | None) -> ResearchWebSearchCategory | None:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized == "_":
            return None
        if normalized not in VALID_SEARCH_CATEGORIES:
            raise ValueError(
                f"category must be one of: {', '.join(sorted(VALID_SEARCH_CATEGORIES))}"
            )
        return cast(ResearchWebSearchCategory, normalized)

    def _livecrawl(self, value: str) -> ResearchWebSearchLivecrawl:
        normalized = str(value).strip().lower()
        if normalized not in VALID_LIVECRAWL_VALUES:
            raise ValueError(
                f"livecrawl must be one of: {', '.join(sorted(VALID_LIVECRAWL_VALUES))}"
            )
        return cast(ResearchWebSearchLivecrawl, normalized)

    def _content_type(self, value: str) -> ResearchWebContentType:
        normalized = str(value).strip().lower()
        if normalized not in VALID_CONTENT_TYPES:
            raise ValueError(
                f"content_type must be one of: {', '.join(sorted(VALID_CONTENT_TYPES))}"
            )
        return cast(ResearchWebContentType, normalized)

    def _context_max(self, value: int) -> int:
        return self._bounded_int(
            value,
            field_name="context_max_characters",
            min_value=500,
            max_value=50000,
        )

    def _bounded_int(
        self,
        value: int,
        *,
        field_name: str,
        min_value: int,
        max_value: int,
    ) -> int:
        parsed = int(value)
        if not min_value <= parsed <= max_value:
            raise ValueError(
                f"{field_name} must be between {min_value} and {max_value}"
            )
        return parsed

    def _string_list(self, values: list[str], field_name: str) -> list[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if not normalized:
            raise ValueError(f"{field_name} must include at least one value")
        return normalized

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
        message=error.get("message")
        or response.reason_phrase
        or "Research gateway error",
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
                    error.get("message")
                    or response.reason_phrase
                    or "Research gateway error"
                ),
                "details": error.get("details"),
            }
    return {
        "type": "http_error",
        "code": "http_error",
        "message": response.reason_phrase or "Research gateway error",
    }


RESEARCH_CLIENT = ResearchClient()

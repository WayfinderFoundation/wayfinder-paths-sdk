from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core.clients.ResearchClient import (
    ResearchClient,
    ResearchGatewayAPIError,
)

research_client_module = importlib.import_module(
    "wayfinder_paths.core.clients.ResearchClient"
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        research_client_module,
        "get_api_base_url",
        lambda: "https://example.com/api/v1/",
    )


@pytest.mark.asyncio
async def test_search_posts_gateway_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response(
            {
                "query": {
                    "query": "reth docs",
                    "numResults": 2,
                    "type": "deep",
                    "livecrawl": "preferred",
                    "sessionID": "ses_123",
                    "contextMaxCharacters": 1500,
                },
                "results": [],
                "provider": {"name": "exa", "requestId": "req_1", "cached": False},
                "usage": {
                    "provider": {"name": "exa", "cached": False},
                    "credits": None,
                },
            }
        )
    )

    result = await client.search(
        query=" reth docs ",
        num_results=2,
        search_type="deep",
        livecrawl="preferred",
        context_max_characters=1500,
        session_id="ses_123",
    )

    assert result["query"]["sessionID"] == "ses_123"
    client._authed_request.assert_awaited_once()
    args, kwargs = client._authed_request.await_args
    assert args == ("POST", "https://example.com/api/v1/research/websearch/")
    assert kwargs["json"] == {
        "query": "reth docs",
        "numResults": 2,
        "type": "deep",
        "contentType": "highlights",
        "livecrawl": "preferred",
        "sessionID": "ses_123",
        "contextMaxCharacters": 1500,
    }


@pytest.mark.asyncio
async def test_search_resolves_session_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "wf-opencode-123")
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response(
            {
                "query": {
                    "query": "defillama stablecoin flows",
                    "numResults": 8,
                    "type": "auto",
                    "livecrawl": "fallback",
                    "sessionID": "wf-opencode-123",
                    "contextMaxCharacters": None,
                },
                "results": [],
                "provider": {"name": "exa", "requestId": None, "cached": False},
                "usage": {
                    "provider": {"name": "exa", "cached": False},
                    "credits": None,
                },
            }
        )
    )

    await client.search(query="defillama stablecoin flows")

    assert client._authed_request.await_args.kwargs["json"]["sessionID"] == (
        "wf-opencode-123"
    )


@pytest.mark.asyncio
async def test_search_posts_curated_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response({"results": []})
    )

    await client.search(
        query="latest protocol docs",
        search_type="deep-reasoning",
        category="news",
        include_domains=["docs.example.com"],
        exclude_domains=["spam.example"],
        start_published_date="2026-05-01T00:00:00Z",
        end_published_date="2026-05-14T00:00:00Z",
        max_age_hours=24,
        additional_queries=["official changelog"],
        content_type="text",
        session_id="ses_123",
    )

    payload = client._authed_request.await_args.kwargs["json"]
    assert payload["type"] == "deep-reasoning"
    assert payload["category"] == "news"
    assert payload["includeDomains"] == ["docs.example.com"]
    assert payload["excludeDomains"] == ["spam.example"]
    assert payload["startPublishedDate"] == "2026-05-01T00:00:00Z"
    assert payload["endPublishedDate"] == "2026-05-14T00:00:00Z"
    assert payload["maxAgeHours"] == 24
    assert payload["additionalQueries"] == ["official changelog"]
    assert payload["contentType"] == "text"


@pytest.mark.asyncio
async def test_fetch_posts_gateway_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response(
            {
                "query": {
                    "urls": ["https://example.com/a"],
                    "sessionID": "ses_123",
                },
                "results": [],
                "statuses": [],
                "provider": {"name": "exa", "requestId": "req_1", "cached": False},
                "usage": {
                    "provider": {"name": "exa", "cached": False},
                    "credits": None,
                },
            }
        )
    )

    result = await client.fetch(
        urls=[" https://example.com/a "],
        query="main facts",
        content_type="summary",
        livecrawl="preferred",
        max_age_hours=12,
        subpages=2,
        subpage_target=["docs"],
        context_max_characters=1500,
        session_id="ses_123",
    )

    assert result["query"]["sessionID"] == "ses_123"
    args, kwargs = client._authed_request.await_args
    assert args == ("POST", "https://example.com/api/v1/research/webfetch/")
    assert kwargs["json"] == {
        "urls": ["https://example.com/a"],
        "query": "main facts",
        "contentType": "summary",
        "livecrawl": "preferred",
        "maxAgeHours": 12,
        "subpages": 2,
        "subpageTarget": ["docs"],
        "sessionID": "ses_123",
        "contextMaxCharacters": 1500,
    }


@pytest.mark.asyncio
async def test_search_raises_structured_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    response = httpx.Response(
        429,
        json={
            "error": {
                "type": "rate_limit",
                "code": "credits_exhausted",
                "message": "Available Wayfinder credits exhausted",
                "details": {"remaining": 0},
            }
        },
        request=httpx.Request("POST", "https://example.com/api/v1/research/websearch/"),
    )
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "rate limited",
            request=response.request,
            response=response,
        )
    )

    with pytest.raises(ResearchGatewayAPIError) as exc_info:
        await client.search(query="latest protocol docs")

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rate_limit"
    assert exc_info.value.code == "credits_exhausted"
    assert exc_info.value.details == {"remaining": 0}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": ""}, "query is required"),
        ({"query": "x", "num_results": 0}, "num_results"),
        ({"query": "x", "search_type": "slow"}, "search_type"),
        ({"query": "x", "category": "blog"}, "category"),
        ({"query": "x", "content_type": "markdown"}, "content_type"),
        ({"query": "x", "livecrawl": "always"}, "livecrawl"),
        ({"query": "x", "context_max_characters": 100}, "context_max_characters"),
    ],
)
@pytest.mark.asyncio
async def test_search_validates_request(kwargs: dict, message: str) -> None:
    client = ResearchClient()

    with pytest.raises(ValueError, match=message):
        await client.search(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"urls": []}, "urls"),
        ({"urls": ["https://example.com"], "content_type": "markdown"}, "content_type"),
        ({"urls": ["https://example.com"], "subpages": 11}, "subpages"),
    ],
)
@pytest.mark.asyncio
async def test_fetch_validates_request(kwargs: dict, message: str) -> None:
    client = ResearchClient()

    with pytest.raises(ValueError, match=message):
        await client.fetch(**kwargs)

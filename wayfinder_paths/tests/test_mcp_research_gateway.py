from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.mcp.tools import research_gateway


@pytest.mark.asyncio
async def test_research_web_search_converts_gateway_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {
            "search": AsyncMock(
                return_value={
                    "query": {"query": "goldsky subgraph docs", "sessionID": "ses_abc"},
                    "results": [],
                    "provider": {"name": "exa", "cached": False},
                    "usage": {"provider": {"name": "exa", "cached": False}},
                }
            ),
            "fetch": AsyncMock(return_value={"results": [], "statuses": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_web_search(
        query="goldsky subgraph docs",
        numResults="3",
        type="fast",
        category="news",
        includeDomains="docs.example.com,github.com",
        additionalQueries="official changelog\napi reference",
        maxAgeHours="24",
        contentType="text",
        livecrawl="preferred",
        contextMaxCharacters="2000",
        sessionID="ses_abc",
    )

    assert result["ok"] is True
    fake_client.search.assert_awaited_once_with(
        query="goldsky subgraph docs",
        num_results=3,
        search_type="fast",
        category="news",
        include_domains=["docs.example.com", "github.com"],
        exclude_domains=None,
        start_published_date=None,
        end_published_date=None,
        max_age_hours=24,
        additional_queries=["official changelog", "api reference"],
        content_type="text",
        livecrawl="preferred",
        context_max_characters=2000,
        session_id="ses_abc",
    )


@pytest.mark.asyncio
async def test_research_web_search_allows_backend_context_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {
            "search": AsyncMock(
                return_value={
                    "query": {"query": "defillama api", "sessionID": "mcp"},
                    "results": [],
                    "provider": {"name": "exa", "cached": False},
                    "usage": {"provider": {"name": "exa", "cached": False}},
                }
            ),
            "fetch": AsyncMock(return_value={"results": [], "statuses": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_web_search(query="defillama api")

    assert result["ok"] is True
    assert fake_client.search.await_args.kwargs["context_max_characters"] is None
    assert fake_client.search.await_args.kwargs["session_id"] == "_"


@pytest.mark.asyncio
async def test_research_web_fetch_converts_gateway_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {
            "search": AsyncMock(return_value={"results": []}),
            "fetch": AsyncMock(
                return_value={
                    "query": {"urls": ["https://example.com"], "sessionID": "ses_abc"},
                    "results": [],
                    "statuses": [],
                    "provider": {"name": "exa", "cached": False},
                    "usage": {"provider": {"name": "exa", "cached": False}},
                }
            ),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_web_fetch(
        urls="https://example.com/a\nhttps://example.com/b",
        query="main facts",
        contentType="summary",
        livecrawl="preferred",
        maxAgeHours="24",
        subpages="2",
        subpageTarget="docs,pricing",
        contextMaxCharacters="2000",
        sessionID="ses_abc",
    )

    assert result["ok"] is True
    fake_client.fetch.assert_awaited_once_with(
        urls=["https://example.com/a", "https://example.com/b"],
        query="main facts",
        content_type="summary",
        livecrawl="preferred",
        max_age_hours=24,
        subpages=2,
        subpage_target=["docs", "pricing"],
        context_max_characters=2000,
        session_id="ses_abc",
    )

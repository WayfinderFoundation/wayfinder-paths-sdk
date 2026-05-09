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
            )
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_web_search(
        query="goldsky subgraph docs",
        numResults="3",
        type="fast",
        livecrawl="preferred",
        contextMaxCharacters="2000",
        sessionID="ses_abc",
    )

    assert result["ok"] is True
    fake_client.search.assert_awaited_once_with(
        query="goldsky subgraph docs",
        num_results=3,
        search_type="fast",
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
            )
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_web_search(query="defillama api")

    assert result["ok"] is True
    assert fake_client.search.await_args.kwargs["context_max_characters"] is None
    assert fake_client.search.await_args.kwargs["session_id"] == "_"

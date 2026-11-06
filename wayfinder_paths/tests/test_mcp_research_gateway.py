from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core.clients.ResearchClient import ResearchClient
from wayfinder_paths.mcp.tools import research_gateway


@pytest.fixture(autouse=True)
def disable_tool_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *a, **k: None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_research_tool_keeps_busy_retries_inside_one_call(
    monkeypatch: pytest.MonkeyPatch, success: bool
) -> None:
    client = ResearchClient()
    request = httpx.Request("POST", "https://example.com/research/websearch/")
    response = httpx.Response(
        429,
        request=request,
        json={
            "error": {
                "code": "research_busy",
                "type": "rate_limit",
                "message": "Wait for the current research",
                "details": {"retryAfterSeconds": 2},
            }
        },
    )
    busy = httpx.HTTPStatusError("busy", request=request, response=response)
    transport = AsyncMock(
        side_effect=(
            [busy, httpx.Response(200, json={"results": []}, request=request)]
            if success
            else [busy] * 5
        )
    )
    monkeypatch.setattr(client, "_authed_request", transport)
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", client)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    result = await research_gateway.core_web_search(
        query="category mechanism", sessionID="same-native-child"
    )

    if success:
        assert result == {"ok": True, "result": {"results": []}}
        sleep.assert_awaited_once_with(2)
    else:
        assert result == {
            "ok": False,
            "error": {
                "code": "research_busy",
                "message": "Wait for the current research",
                "details": {"retryAfterSeconds": 2},
            },
        }
        assert sleep.await_count == 4
    assert transport.await_count == (2 if success else 5)
    assert all(
        call == transport.await_args_list[0] for call in transport.await_args_list
    )


@pytest.mark.asyncio
async def test_core_web_search_converts_gateway_arguments(
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
                }
            ),
            "fetch": AsyncMock(return_value={"results": [], "statuses": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.core_web_search(
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
    assert "provider" not in result["result"]
    assert "usage" not in result["result"]
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
async def test_core_web_search_allows_backend_context_default(
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
                }
            ),
            "fetch": AsyncMock(return_value={"results": [], "statuses": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.core_web_search(query="defillama api")

    assert result["ok"] is True
    assert fake_client.search.await_args.kwargs["context_max_characters"] is None
    assert fake_client.search.await_args.kwargs["session_id"] == "_"


@pytest.mark.asyncio
async def test_core_web_search_accepts_int_num_results_and_news_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {
            "search": AsyncMock(return_value={"results": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.core_web_search(
        query="ethena catalyst",
        numResults=5,
        type="news",
    )

    assert result["ok"] is True
    fake_client.search.assert_awaited_once()
    kwargs = fake_client.search.await_args.kwargs
    assert kwargs["num_results"] == 5
    assert kwargs["search_type"] == "auto"
    assert kwargs["category"] == "news"


@pytest.mark.asyncio
async def test_core_web_search_returns_allowed_values_for_bad_type() -> None:
    result = await research_gateway.core_web_search(
        query="ethena catalyst",
        type="bad-mode",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "type"
    assert "auto" in result["error"]["details"]["allowed_values"]


@pytest.mark.asyncio
async def test_core_web_search_suggests_category_for_type_category() -> None:
    result = await research_gateway.core_web_search(
        query="ethena catalyst",
        type="news",
        category="company",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["suggested_arguments"] == {
        "type": "auto",
        "category": "news",
    }


@pytest.mark.asyncio
async def test_core_web_fetch_converts_gateway_arguments(
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
                }
            ),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.core_web_fetch(
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
    assert "provider" not in result["result"]
    assert "usage" not in result["result"]
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


@pytest.mark.asyncio
async def test_core_web_fetch_accepts_list_urls_and_int_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {
            "fetch": AsyncMock(return_value={"results": [], "statuses": []}),
        },
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.core_web_fetch(
        urls=["https://example.com/a", "https://example.com/b"],
        maxAgeHours=24,
        subpages=2,
        contextMaxCharacters=2000,
    )

    assert result["ok"] is True
    kwargs = fake_client.fetch.await_args.kwargs
    assert kwargs["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert kwargs["max_age_hours"] == 24
    assert kwargs["subpages"] == 2
    assert kwargs["context_max_characters"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        (
            "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=hyperliquid,jupiter-exchange-solana,gmx",
            [
                "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=hyperliquid,jupiter-exchange-solana,gmx"
            ],
        ),
        ("https://example.com/a,b", ["https://example.com/a,b"]),
        (
            " https://example.com/?ids=a,b, https://example.org/?ids=c,d ",
            ["https://example.com/?ids=a,b", "https://example.org/?ids=c,d"],
        ),
        (
            "https://example.com/a,b\r\n\nhttps://example.org/c,d",
            ["https://example.com/a,b", "https://example.org/c,d"],
        ),
        (
            [
                "https://example.com/?redirect=a,https://example.org/b",
                "https://example.net/c,d",
            ],
            [
                "https://example.com/?redirect=a,https://example.org/b",
                "https://example.net/c,d",
            ],
        ),
        (
            "https://example.com/a,HTTPS://example.org/b,http://example.net/c",
            ["https://example.com/a", "HTTPS://example.org/b", "http://example.net/c"],
        ),
        (
            "https://example.com/a,http://127.0.0.1/private,file:///not-public",
            ["https://example.com/a", "http://127.0.0.1/private", "file:///not-public"],
        ),
    ],
)
async def test_core_web_fetch_preserves_url_commas_and_list_boundaries(
    monkeypatch: pytest.MonkeyPatch, urls: str | list[str], expected: list[str]
) -> None:
    fetch = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(research_gateway.RESEARCH_CLIENT, "fetch", fetch)

    response = await research_gateway.core_web_fetch(urls=urls)

    assert response["ok"]
    assert fetch.await_count == 1
    assert fetch.await_args is not None
    # Parsing does not silently discard entries that the gateway must reject.
    assert fetch.await_args.kwargs["urls"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", ["", "_", "none", "null", [], "\n\r\n"])
async def test_core_web_fetch_still_requires_urls(
    monkeypatch: pytest.MonkeyPatch, urls: str | list[str]
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(research_gateway.RESEARCH_CLIENT, "fetch", fetch)

    response = await research_gateway.core_web_fetch(urls=urls)

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_argument"
    assert response["error"]["details"]["field"] == "urls"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_web_fetch_still_caps_url_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(research_gateway.RESEARCH_CLIENT, "fetch", fetch)

    response = await research_gateway.core_web_fetch(
        urls=",".join(f"https://example.com/{i}?ids=a,b" for i in range(26))
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_argument"
    assert "25 values or fewer" in response["error"]["message"]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_research_crypto_sentiment_uses_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {"crypto_sentiment": AsyncMock(return_value={"results": []})},
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_crypto_sentiment(sessionID="ses_abc")

    assert result["ok"] is True
    fake_client.crypto_sentiment.assert_awaited_once_with(session_id="ses_abc")


@pytest.mark.asyncio
async def test_research_social_x_search_converts_gateway_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = type(
        "FakeResearchClient",
        (),
        {"social_x_search": AsyncMock(return_value={"result": {"content": ""}})},
    )()
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", fake_client)

    result = await research_gateway.research_social_x_search(
        query="$ENA launch",
        allowedXHandles="ethena_labs, EthenaGrowth",
        fromDate="2026-05-01",
        toDate="2026-05-14",
        sessionID="ses_abc",
    )

    assert result["ok"] is True
    fake_client.social_x_search.assert_awaited_once_with(
        query="$ENA launch",
        allowed_x_handles=["ethena_labs", "EthenaGrowth"],
        excluded_x_handles=None,
        from_date="2026-05-01",
        to_date="2026-05-14",
        session_id="ses_abc",
    )


@pytest.mark.asyncio
async def test_research_social_x_search_caps_handle_filters() -> None:
    handles = ",".join(f"handle_{index}" for index in range(11))

    result = await research_gateway.research_social_x_search(
        query="$ENA launch",
        allowedXHandles=handles,
    )

    assert result["ok"] is False
    assert "10 values or fewer" in result["error"]["message"]

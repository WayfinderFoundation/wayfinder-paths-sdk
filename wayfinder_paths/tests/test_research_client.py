from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core.clients.GatewayClient import GatewayClient
from wayfinder_paths.core.clients.ResearchClient import (
    ResearchClient,
    ResearchGatewayAPIError,
    _extract_gateway_error,
    _gateway_error_from_response,
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
    assert "provider" not in result
    assert "usage" not in result
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
            }
        )
    )

    await client.search(query="defillama stablecoin flows")

    assert client._authed_request.await_args.kwargs["json"]["sessionID"] == (
        "wf-opencode-123"
    )


def test_research_rejects_overlong_explicit_session_id() -> None:
    with pytest.raises(ValueError, match="200 characters or fewer"):
        ResearchClient.resolve_session_id("x" * 201)


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
    assert "provider" not in result
    assert "usage" not in result
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
async def test_crypto_sentiment_posts_gateway_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response(
            {"results": [], "provider": {"name": "alternative_me_fng"}}
        )
    )

    await client.crypto_sentiment(session_id="ses_123")

    args, kwargs = client._authed_request.await_args
    assert args == ("POST", "https://example.com/api/v1/research/crypto/sentiment/")
    assert kwargs["json"] == {"sessionID": "ses_123"}


@pytest.mark.asyncio
async def test_social_x_search_posts_gateway_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response({"result": {"content": ""}})
    )

    await client.social_x_search(
        query=" $ENA launch ",
        allowed_x_handles=["ethena_labs"],
        from_date="2026-05-01",
        to_date="2026-05-14",
        session_id="ses_123",
    )

    args, kwargs = client._authed_request.await_args
    assert args == ("POST", "https://example.com/api/v1/research/social/x-search/")
    assert kwargs["json"] == {
        "query": "$ENA launch",
        "allowedXHandles": ["ethena_labs"],
        "fromDate": "2026-05-01",
        "toDate": "2026-05-14",
        "sessionID": "ses_123",
    }


@pytest.mark.asyncio
async def test_social_x_search_rejects_conflicting_handle_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()

    with pytest.raises(ValueError, match="cannot both be set"):
        await client.social_x_search(
            query="$ENA launch",
            allowed_x_handles=["ethena_labs"],
            excluded_x_handles=["spam"],
        )


@pytest.mark.asyncio
async def test_social_x_search_rejects_too_many_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()

    with pytest.raises(ValueError, match="10 values or fewer"):
        await client.social_x_search(
            query="$ENA launch",
            allowed_x_handles=[f"handle_{index}" for index in range(11)],
        )


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
    client._authed_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "denied_again", "cancelled"])
async def test_busy_backpressure_retries_same_read_with_bounded_wait(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    error = ResearchGatewayAPIError(
        status_code=429,
        error_type="rate_limit",
        code="research_busy",
        message="Research is already in progress",
        details={"retryAfterSeconds": 2},
    )
    post = AsyncMock(
        side_effect=[error] * 4 + [error if outcome == "denied_again" else {}]
    )
    monkeypatch.setattr(GatewayClient, "_post_gateway", post)
    sleep = AsyncMock(
        side_effect=asyncio.CancelledError if outcome == "cancelled" else None
    )
    monkeypatch.setattr(research_client_module.asyncio, "sleep", sleep)
    client = ResearchClient()
    if outcome == "success":
        assert (
            await client.search(query="same question", session_id="same-session") == {}
        )
    else:
        with pytest.raises(
            asyncio.CancelledError
            if outcome == "cancelled"
            else ResearchGatewayAPIError
        ):
            await client.search(query="same question", session_id="same-session")
    assert [call.args[0] for call in sleep.await_args_list] == (
        [2] if outcome == "cancelled" else [2, 4, 8, 16]
    )
    assert post.await_count == (1 if outcome == "cancelled" else 5)
    assert all(call == post.await_args_list[0] for call in post.await_args_list)
    async with asyncio.timeout(1):
        for _ in range(4):
            await client._requests.acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details",
    [
        None,
        [],
        {},
        {"retryAfterSeconds": True},
        {"retryAfterSeconds": "2"},
        {"retryAfterSeconds": -1},
        {"retryAfterSeconds": 0},
        {"retryAfterSeconds": 31},
        {"retryAfterSeconds": float("nan")},
        {"retryAfterSeconds": float("inf")},
    ],
)
async def test_busy_retry_requires_valid_server_delay(
    monkeypatch: pytest.MonkeyPatch, details: Any
) -> None:
    error = ResearchGatewayAPIError(
        status_code=429,
        error_type="rate_limit",
        code="research_busy",
        message="Busy",
        details=details,
    )
    post = AsyncMock(side_effect=error)
    monkeypatch.setattr(GatewayClient, "_post_gateway", post)
    sleep = AsyncMock()
    monkeypatch.setattr(research_client_module.asyncio, "sleep", sleep)
    with pytest.raises(ResearchGatewayAPIError) as caught:
        await ResearchClient().fetch(urls=["https://example.com/source"])
    assert caught.value is error
    post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "denied_again", "cancelled"])
async def test_opt_in_hourly_reset_wait_retries_identical_request_once(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setenv("WAYFINDER_RESEARCH_WAIT_FOR_HOURLY_RESET", "1")
    error = ResearchGatewayAPIError(
        status_code=429,
        error_type="rate_limit",
        code="research_budget_exhausted",
        message="Hour exhausted",
        details={
            "window": "hour",
            "resetAt": (datetime.now(UTC) + timedelta(seconds=75)).isoformat(),
        },
    )
    post = AsyncMock(side_effect=[error, error if outcome == "denied_again" else {}])
    monkeypatch.setattr(GatewayClient, "_post_gateway", post)
    sleep = AsyncMock(
        side_effect=asyncio.CancelledError if outcome == "cancelled" else None
    )
    monkeypatch.setattr(research_client_module.asyncio, "sleep", sleep)
    client = ResearchClient()
    if outcome == "success":
        assert (
            await client.search(query="test mechanism", session_id="same-session") == {}
        )
    else:
        with pytest.raises(
            asyncio.CancelledError
            if outcome == "cancelled"
            else ResearchGatewayAPIError
        ):
            await client.search(query="test mechanism", session_id="same-session")
    sleep.assert_awaited_once()
    assert sleep.await_args is not None
    assert 70 < sleep.await_args.args[0] <= 76
    assert post.await_count == (1 if outcome == "cancelled" else 2)
    assert all(call == post.await_args_list[0] for call in post.await_args_list)
    # A cancelled pending read releases backpressure, without retrying it later.
    async with asyncio.timeout(1):
        for _ in range(4):
            await client._requests.acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,status,code,details",
    [
        (False, 429, "research_budget_exhausted", "valid"),
        (True, 401, "research_budget_exhausted", "valid"),
        (True, 429, "credits_exhausted", "valid"),
        (True, 429, "concurrency_limit", "valid"),
        (True, 503, "research_busy", {"retryAfterSeconds": 2}),
        (True, 401, "research_busy", {"retryAfterSeconds": 2}),
        (True, 429, "provider_rate_limit", {"retryAfterSeconds": 2}),
        (True, 503, "research_budget_unavailable", "valid"),
        (True, 429, "research_budget_exhausted", None),
        (True, 429, "research_budget_exhausted", []),
        (True, 429, "research_budget_exhausted", {"window": "day"}),
        (True, 429, "research_budget_exhausted", {"window": "month"}),
        (True, 429, "research_budget_exhausted", {"window": "hour"}),
        (True, 429, "research_budget_exhausted", {"window": "hour", "resetAt": 123}),
        (True, 429, "research_budget_exhausted", {"window": "hour", "resetAt": "bad"}),
        (True, 429, "research_budget_exhausted", "naive"),
        (True, 429, "research_budget_exhausted", "past"),
        (True, 429, "research_budget_exhausted", "distant"),
    ],
)
async def test_hourly_reset_wait_does_not_hide_other_failures(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    status: int,
    code: str,
    details: Any,
) -> None:
    monkeypatch.delenv("WAYFINDER_RESEARCH_WAIT_FOR_HOURLY_RESET", raising=False)
    if enabled:
        monkeypatch.setenv("WAYFINDER_RESEARCH_WAIT_FOR_HOURLY_RESET", "1")
    if isinstance(details, str):
        reset = datetime.now(UTC) + timedelta(seconds=75)
        if details == "naive":
            reset = reset.replace(tzinfo=None)
        elif details == "past":
            reset -= timedelta(hours=1)
        elif details == "distant":
            reset += timedelta(hours=2)
        details = {"window": "hour", "resetAt": reset.isoformat()}
    error = ResearchGatewayAPIError(
        status_code=status,
        error_type="rate_limit",
        code=code,
        message="Denied",
        details=details,
    )
    post = AsyncMock(side_effect=error)
    monkeypatch.setattr(GatewayClient, "_post_gateway", post)
    sleep = AsyncMock()
    monkeypatch.setattr(research_client_module.asyncio, "sleep", sleep)
    with pytest.raises(ResearchGatewayAPIError) as caught:
        await ResearchClient().fetch(urls=["https://example.com/source"])
    assert caught.value is error
    post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_worker_reads_share_backpressure(monkeypatch) -> None:
    _patch_base_url(monkeypatch)
    client = ResearchClient()
    active = peak = 0
    saturated, release = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            saturated.set()
        try:
            await release.wait()
            return _Response({"results": []})
        finally:
            active -= 1

    client._authed_request = AsyncMock(side_effect=request)
    tasks = [
        asyncio.create_task(
            client.search(query=f"candidate {i}")
            if i % 2
            else client.fetch(urls=[f"https://example.com/{i}"])
        )
        for i in range(10)
    ]
    try:
        await asyncio.wait_for(saturated.wait(), timeout=1)
        assert client._authed_request.await_count == 4
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        await asyncio.gather(*tasks[1:])
        assert peak == 4
        assert active == 0
        assert client._authed_request.await_count == 10
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        previous = client._requests
        await client.aclose()
        assert client._requests is not previous


def test_research_gateway_error_helpers_remain_available() -> None:
    request = httpx.Request("POST", "https://example.com/api/v1/research/websearch/")
    text_response = httpx.Response(502, text="bad gateway body", request=request)
    assert _extract_gateway_error(text_response) == {
        "type": "http_error",
        "code": "http_error",
        "message": "bad gateway body",
    }

    json_response = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request",
                "code": "bad_query",
                "message": "Bad query",
                "details": {"field": "query"},
            }
        },
        request=request,
    )
    exc = _gateway_error_from_response(json_response)
    assert isinstance(exc, ResearchGatewayAPIError)
    assert exc.status_code == 400
    assert exc.error_type == "invalid_request"
    assert exc.code == "bad_query"
    assert exc.details == {"field": "query"}


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

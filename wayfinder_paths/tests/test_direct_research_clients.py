from __future__ import annotations

import httpx
import pytest

from wayfinder_paths.core.clients.direct import DefiLlamaFreeClient as llama_module
from wayfinder_paths.core.clients.direct import GoldskyDirectClient as goldsky_module
from wayfinder_paths.mcp.tools import goldsky_direct


class _FakeAsyncClient:
    calls: list[tuple[str, str, dict]] = []
    get_body = {"data": []}
    post_body = {"data": {"ok": True}}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, url: str, params: dict | None = None):
        self.calls.append(("GET", url, {"params": params or {}}))
        request = httpx.Request("GET", url, params=params or {})
        return httpx.Response(200, json=self.get_body, request=request)

    async def post(self, url: str, headers: dict, json: dict):
        self.calls.append(("POST", url, {"headers": headers, "json": json}))
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=self.post_body, request=request)


@pytest.mark.asyncio
async def test_defillama_free_uses_direct_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {"data": []}
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.tvl("aave")

    assert _FakeAsyncClient.calls == [
        ("GET", "https://api.llama.fi/tvl/aave", {"params": {}})
    ]
    assert result["provider"] == "defillama_free"
    assert result["evidence"][0]["clientDirect"] is True
    assert result["evidence"][0]["attributionRequired"] is True


@pytest.mark.asyncio
async def test_defillama_free_open_interest_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    await llama_module.DEFILLAMA_FREE_CLIENT.open_interest_overview()

    assert _FakeAsyncClient.calls == [
        ("GET", "https://api.llama.fi/overview/open-interest", {"params": {}})
    ]


@pytest.mark.asyncio
async def test_defillama_free_protocol_search_compacts_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = [
        {"name": "Pendle", "slug": "pendle", "category": "Yield", "tvl": 10},
        {"name": "Other", "slug": "other", "category": "DEX", "tvl": 5},
    ]
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.protocol_search("pendle")

    assert _FakeAsyncClient.calls == [
        ("GET", "https://api.llama.fi/protocols", {"params": {}})
    ]
    assert result["result"]["matches"] == [
        {
            "name": "Pendle",
            "slug": "pendle",
            "symbol": None,
            "category": "Yield",
            "chains": None,
            "tvl": 10,
            "change_1d": None,
            "change_7d": None,
            "url": None,
        }
    ]


@pytest.mark.asyncio
async def test_defillama_free_protocol_fees_returns_daily_and_weekly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {
        "totalDataChart": [[1778803200, 100], [1778889600, 200]],
        "totalDataChartBreakdown": [[1778803200, {"Ethereum": {"Pendle": 100}}]],
    }
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.protocol_fees(
        "pendle",
        data_type="dailyFees",
        days=365,
    )

    assert _FakeAsyncClient.calls == [
        (
            "GET",
            "https://api.llama.fi/summary/fees/pendle",
            {"params": {"dataType": "dailyFees"}},
        )
    ]
    assert result["result"]["dailyRows"][-1]["value"] == 200
    assert result["result"]["weeklyRollups"][0]["sum"] == 300
    assert result["result"]["chainDailyRows"][0]["breakdown"] == {
        "Ethereum": {"Pendle": 100}
    }


@pytest.mark.asyncio
async def test_defillama_free_protocol_tvl_history_compacts_chain_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {
        "tvl": [
            {"date": 1778803200, "totalLiquidityUSD": 1000},
            {"date": 1778889600, "totalLiquidityUSD": 1200},
        ],
        "chainTvls": {
            "Plasma": {
                "tvl": [
                    {"date": 1778803200, "totalLiquidityUSD": 100},
                    {"date": 1778889600, "totalLiquidityUSD": 300},
                ]
            }
        },
    }
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.protocol_tvl_history(
        "pendle",
        days=365,
    )

    assert _FakeAsyncClient.calls == [
        ("GET", "https://api.llama.fi/protocol/pendle", {"params": {}})
    ]
    assert result["result"]["latestDaily"]["tvlUsd"] == 1200
    assert result["result"]["chainSummary"][0]["chain"] == "Plasma"
    assert result["result"]["chainSummary"][0]["changeUsd"] == 200


@pytest.mark.asyncio
async def test_defillama_free_validates_path_params() -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        await llama_module.DEFILLAMA_FREE_CLIENT.tvl("aave?bad=true")


@pytest.mark.asyncio
async def test_goldsky_private_endpoint_uses_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.post_body = {"data": {"ok": True}}
    monkeypatch.setattr(goldsky_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("GOLDSKY_API_TOKEN", "goldsky_test_token")

    endpoint = "https://api.goldsky.com/api/private/project/subgraphs/foo/prod/gn"
    await goldsky_module.GOLDSKY_DIRECT_CLIENT.query(
        endpoint=endpoint,
        query="query { pools(first: 1) { id } }",
    )

    method, url, kwargs = _FakeAsyncClient.calls[0]
    assert method == "POST"
    assert url == endpoint
    assert kwargs["headers"]["Authorization"] == "Bearer goldsky_test_token"


@pytest.mark.asyncio
async def test_goldsky_rejects_mutation() -> None:
    with pytest.raises(ValueError, match="only read-only"):
        await goldsky_module.GOLDSKY_DIRECT_CLIENT.query(
            endpoint="https://api.goldsky.com/api/public/project/subgraphs/foo/prod/gn",
            query="mutation { bad }",
        )


@pytest.mark.asyncio
async def test_goldsky_rejects_non_graphql_endpoint() -> None:
    with pytest.raises(ValueError, match="end with /gn"):
        await goldsky_module.GOLDSKY_DIRECT_CLIENT.query(
            endpoint="https://api.goldsky.com/api/public/project/subgraphs/foo/prod",
            query="query { pools(first: 1) { id } }",
        )


@pytest.mark.asyncio
async def test_goldsky_truncates_large_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.post_body = {"data": {"items": ["x" * 201_000]}}
    monkeypatch.setattr(goldsky_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await goldsky_module.GOLDSKY_DIRECT_CLIENT.query(
        endpoint="https://api.goldsky.com/api/public/project/subgraphs/foo/prod/gn",
        query="query { pools(first: 1) { id } }",
    )

    assert result["result"]["truncated"] is True
    assert result["result"]["maxResponseCharacters"] == 200_000


@pytest.mark.asyncio
async def test_goldsky_search_and_schema_tools() -> None:
    search = await goldsky_direct.research_goldsky_search(query="projectx")
    assert search["ok"] is True
    endpoint_id = search["result"]["results"][0]["id"]

    schema = await goldsky_direct.research_goldsky_schema(endpointId=endpoint_id)
    assert schema["ok"] is True
    assert schema["result"]["result"]["schemaSummary"]["entities"] == [
        "positions",
        "swaps",
    ]

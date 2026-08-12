from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wayfinder_paths.core.clients.direct import DefiLlamaFreeClient as llama_module
from wayfinder_paths.core.clients.direct import GoldskyDirectClient as goldsky_module
from wayfinder_paths.mcp.tools import goldsky_direct
from wayfinder_paths.mcp.tools.defillama_free import research_defillama_free


class _FakeStreamResponse:
    def __init__(
        self,
        *,
        url: str,
        params: dict,
        chunks: list[bytes],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = httpx.URL(url, params=params)
        self.headers = httpx.Headers(headers or {})
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    calls: list[tuple[str, str, dict]] = []
    get_body = {"data": []}
    get_chunks: list[bytes] | None = None
    get_headers: dict[str, str] = {}
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

    def stream(self, method: str, url: str, params: dict | None = None):
        request_params = params or {}
        self.calls.append((method, url, {"params": request_params}))
        chunks = self.get_chunks
        if chunks is None:
            chunks = [json.dumps(self.get_body).encode()]
        return _FakeStreamResponse(
            url=url,
            params=request_params,
            chunks=chunks,
            headers=self.get_headers,
        )

    async def post(self, url: str, headers: dict, json: dict):
        self.calls.append(("POST", url, {"headers": headers, "json": json}))
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=self.post_body, request=request)


@pytest.fixture(autouse=True)
def _reset_fake_async_client() -> None:
    _FakeAsyncClient.get_chunks = None
    _FakeAsyncClient.get_headers = {}


@pytest.mark.asyncio
async def test_defillama_free_uses_direct_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {"data": []}
    _FakeAsyncClient.get_chunks = None
    _FakeAsyncClient.get_headers = {}
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
    _FakeAsyncClient.get_body = {"protocols": []}
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    await llama_module.DEFILLAMA_FREE_CLIENT.open_interest_overview()

    assert _FakeAsyncClient.calls == [
        (
            "GET",
            "https://api.llama.fi/overview/open-interest",
            {
                "params": {
                    "excludeTotalDataChart": "true",
                    "excludeTotalDataChartBreakdown": "true",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_defillama_free_stablecoins_uses_stablecoins_host_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {
        "peggedAssets": [
            {
                "id": "usdt",
                "name": "Tether",
                "symbol": "USDT",
                "circulating": {"peggedUSD": 100},
            },
            {
                "id": "usdc",
                "name": "USD Coin",
                "symbol": "USDC",
                "circulating": {"peggedUSD": 90},
            },
        ],
        "chains": [{"name": "Ethereum"}],
    }
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.stablecoins(limit=1)

    assert _FakeAsyncClient.calls == [
        ("GET", "https://stablecoins.llama.fi/stablecoins", {"params": {}})
    ]
    assert result["result"]["items"][0]["symbol"] == "USDT"
    assert result["result"]["page"]["nextCursor"] == "1"
    assert result["result"]["rawPayloadOmitted"] is True


@pytest.mark.asyncio
async def test_defillama_free_fees_overview_compacts_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {
        "total24h": 300,
        "total7d": 700,
        "protocols": [
            {
                "name": "Small",
                "slug": "small",
                "total24h": 10,
                "breakdown24h": {"ethereum": {"Small": 10}},
            },
            {
                "name": "Large",
                "slug": "large",
                "total24h": 200,
                "breakdown24h": {"base": {"Large": 200}},
            },
        ],
        "totalDataChart": [[1, 2]],
        "totalDataChartBreakdown": [[1, {"ethereum": {"Large": 200}}]],
    }
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.fees_overview(limit=1)

    assert _FakeAsyncClient.calls == [
        (
            "GET",
            "https://api.llama.fi/overview/fees",
            {
                "params": {
                    "excludeTotalDataChart": "true",
                    "excludeTotalDataChartBreakdown": "true",
                }
            },
        )
    ]
    assert result["result"]["items"][0]["name"] == "Large"
    assert result["result"]["page"]["nextCursor"] == "1"
    assert result["result"]["totals"]["total24h"] == 300
    assert "totalDataChart" not in result["result"]


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
async def test_defillama_free_current_prices_uses_coins_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {"coins": {"base:0xabc": {"price": 1.23}}}
    _FakeAsyncClient.get_chunks = None
    _FakeAsyncClient.get_headers = {}
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.current_prices("base:0xabc")

    assert _FakeAsyncClient.calls == [
        (
            "GET",
            "https://coins.llama.fi/prices/current/base:0xabc",
            {"params": {}},
        )
    ]
    assert result["result"]["coins"]["base:0xabc"]["price"] == 1.23


@pytest.mark.asyncio
async def test_defillama_free_protocol_resolves_compact_exact_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = [
        {
            "name": "Morpho Blue",
            "slug": "morpho-blue",
            "description": "Permissionless lending markets",
            "chains": ["Ethereum", "Base"],
            "tvl": 123,
            "audit_links": ["https://example.com/audit"],
            "chainTvls": {"oversized": "history must not leak"},
        }
    ]
    _FakeAsyncClient.get_chunks = None
    _FakeAsyncClient.get_headers = {}
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.protocol("MORPHO-BLUE")

    assert _FakeAsyncClient.calls == [
        ("GET", "https://api.llama.fi/protocols", {"params": {}})
    ]
    assert result["result"]["slug"] == "morpho-blue"
    assert result["result"]["description"] == "Permissionless lending markets"
    assert result["result"]["rawPayloadOmitted"] is True
    assert "chainTvls" not in result["result"]


@pytest.mark.asyncio
async def test_defillama_free_rejects_chunked_oversize_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_chunks = [b'{"data":', b'"too large"}']
    _FakeAsyncClient.get_headers = {}
    monkeypatch.setattr(llama_module, "MAX_UPSTREAM_RESPONSE_BYTES", 10)
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        llama_module.json,
        "loads",
        lambda *_args, **_kwargs: pytest.fail("oversized response was decoded"),
    )

    result = await research_defillama_free(dataset="tvl", protocolSlug="aave")

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_response_too_large"
    assert result["error"]["details"]["receivedBytes"] > 10


@pytest.mark.asyncio
async def test_defillama_free_large_protocol_history_returns_compact_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_rows = [{"date": index, "totalLiquidityUSD": index} for index in range(100_000)]
    recent_rows = [
        {"date": 1778803200, "totalLiquidityUSD": 1000},
        {"date": 1778889600, "totalLiquidityUSD": 1200},
    ]
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {"tvl": [*old_rows, *recent_rows], "chainTvls": {}}
    _FakeAsyncClient.get_chunks = None
    _FakeAsyncClient.get_headers = {}
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await llama_module.DEFILLAMA_FREE_CLIENT.protocol_tvl_history(
        "pendle", days=365
    )

    assert result["result"]["dailyRows"] == [
        {"date": "2026-05-15", "tvlUsd": 1000.0},
        {"date": "2026-05-16", "tvlUsd": 1200.0},
    ]
    assert len(json.dumps(result)) <= llama_module.MAX_RESPONSE_CHARACTERS


@pytest.mark.asyncio
async def test_defillama_free_concurrent_research_fanout_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FanoutClient(_FakeAsyncClient):
        def stream(self, method: str, url: str, params: dict | None = None):
            self.calls.append((method, url, {"params": params or {}}))
            if url.endswith("/protocols"):
                body = [{"name": "Pendle", "slug": "pendle", "tvl": 1}]
            elif url.endswith("/pools"):
                body = {"data": [{"project": "pendle", "tvlUsd": 1}]}
            else:
                body = {"protocols": [{"name": "Pendle", "total24h": 1}]}
            return _FakeStreamResponse(
                url=url,
                params=params or {},
                chunks=[json.dumps(body).encode()],
            )

    _FanoutClient.calls = []
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FanoutClient)

    results = await asyncio.wait_for(
        asyncio.gather(
            llama_module.DEFILLAMA_FREE_CLIENT.protocol("pendle"),
            llama_module.DEFILLAMA_FREE_CLIENT.yields_pools(limit=5),
            llama_module.DEFILLAMA_FREE_CLIENT.fees_overview(limit=5),
        ),
        timeout=1,
    )

    assert all(
        len(json.dumps(result)) <= llama_module.MAX_RESPONSE_CHARACTERS
        for result in results
    )


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

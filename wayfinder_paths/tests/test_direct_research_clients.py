from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core.clients.direct import DefiLlamaFreeClient as llama_module
from wayfinder_paths.core.clients.direct import GoldskyDirectClient as goldsky_module
from wayfinder_paths.mcp.tools import defillama_free, goldsky_direct


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
    assert result["result"]["methodology"] is None
    assert result["result"]["methodologyURL"] is None
    assert result["result"]["breakdownMethodology"] is None
    assert result["result"]["totals"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("data_type", ["dailyFees", "dailyRevenue"])
async def test_defillama_protocol_fees_preserves_definitions_and_reported_totals(
    monkeypatch: pytest.MonkeyPatch, data_type: str
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_body = {
        "description": "Example protocol",
        "methodology": {
            "Fees": "Onchain buy-and-burn only; excludes offchain subscription sales.",
            "Revenue": "Onchain buy-and-burn only, not total business revenue.",
        },
        "methodologyURL": "https://example.org/adapter",
        "breakdownMethodology": {"Fees": {"Burn": "USD value of tokens burned."}},
        "total24h": 0,
        "total7d": 700,
        "total30d": 2500,
        "total1y": None,
        "annualized1y": 30416.67,
    }
    monkeypatch.setattr(llama_module.httpx, "AsyncClient", _FakeAsyncClient)

    response = await llama_module.DEFILLAMA_FREE_CLIENT.protocol_fees(
        "example", data_type=data_type
    )

    assert _FakeAsyncClient.calls == [
        (
            "GET",
            "https://api.llama.fi/summary/fees/example",
            {"params": {"dataType": data_type}},
        )
    ]
    result = response["result"]
    for key in ("description", "methodology", "methodologyURL", "breakdownMethodology"):
        assert result[key] == _FakeAsyncClient.get_body[key]
    assert result["totals"] == {
        "total24h": 0,
        "total7d": 700,
        "total30d": 2500,
        "total1y": None,
    }
    assert result["dailyRows"] == []
    assert response["evidence"][0]["url"] == response["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_breakdown", [False, True])
async def test_defillama_tool_keeps_large_chain_history_opt_in(
    monkeypatch: pytest.MonkeyPatch, include_breakdown: bool
) -> None:
    breakdown = [
        {
            "date": f"2026-09-{day:02}",
            "breakdown": {f"Chain {chain}": {"Protocol": 100} for chain in range(40)},
        }
        for day in range(1, 31)
    ]
    metrics = {
        "protocolSlug": "example",
        "dataType": "dailyRevenue",
        "totals": {"total30d": 120000},
        "dailyRows": [{"date": row["date"], "value": 4000} for row in breakdown],
        "methodology": {"Revenue": "Net protocol fees, not holder distributions."},
        "methodologyURL": "https://example.org/methodology",
    }
    evidence = [{"url": "https://api.llama.fi/summary/fees/example"}]
    lookup = AsyncMock(
        return_value={
            "result": {**metrics, "chainDailyRows": breakdown},
            "evidence": evidence,
        }
    )
    monkeypatch.setattr(defillama_free.DEFILLAMA_FREE_CLIENT, "protocol_fees", lookup)
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *a, **k: None
    )

    response = await defillama_free.research_defillama_free(
        dataset="protocol_fees",
        protocolSlug="example",
        dataType="dailyRevenue",
        **({"includeChainBreakdown": True} if include_breakdown else {}),
    )

    lookup.assert_awaited_once_with("example", data_type="dailyRevenue", days=30)
    assert response["ok"]
    assert response["result"]["evidence"] == evidence
    if include_breakdown:
        assert response["result"]["result"] == {**metrics, "chainDailyRows": breakdown}
        assert len(json.dumps(response, indent=2)) > 50000
    else:
        assert response["result"]["result"] == {
            **metrics,
            "chainDailyRowsOmitted": True,
        }
        assert len(json.dumps(response, indent=2)) < 10000


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

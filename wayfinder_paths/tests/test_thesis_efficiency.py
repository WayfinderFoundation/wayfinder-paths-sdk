import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.clients.direct.DefiLlamaFreeClient import (
    DefiLlamaFreeClient,
    _enforce_response_budget,
)
from wayfinder_paths.mcp.tools import thesis_quantification as tool
from wayfinder_paths.tests.test_thesis_quantification import variant


@pytest.mark.parametrize("module", ["assessment", "draft"])
def test_notebook_import_does_not_initialize_execution_clients(module: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            f"import wayfinder_paths.core.theses.{module}; "
            "import sys; "
            "assert not {'wayfinder_paths.mcp.utils', "
            "'wayfinder_paths.core.clients', 'web3', 'pandas'} & sys.modules.keys()",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_catalog_shared_concurrent_reads_are_isolated_and_expire():
    client = DefiLlamaFreeClient()
    client._get = AsyncMock(
        return_value={
            "url": "https://api.llama.fi/protocols",
            "result": [{"name": "Test", "slug": "test"}],
        }
    )
    first, second = await asyncio.gather(client.protocols(), client.protocols())
    first["result"].clear()
    assert second["result"]
    assert client._get.await_count == 1
    client._catalog_expires = 0
    await client.protocols()
    assert client._get.await_count == 2


@pytest.mark.asyncio
async def test_catalog_failure_does_not_poison_cache():
    client = DefiLlamaFreeClient()
    client._get = AsyncMock(side_effect=[TimeoutError(), {"result": []}])
    with pytest.raises(TimeoutError):
        await client.protocols()
    assert await client.protocols() == {"result": []}
    assert client._get.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "Revenue venue 市場 " * 70])
async def test_catalog_pages_fit_rendered_output_without_losing_candidates(
    description: str,
) -> None:
    client = DefiLlamaFreeClient()
    client._get = AsyncMock(
        return_value={
            "url": "https://api.llama.fi/protocols",
            "result": [
                {
                    "name": f"Venue {index}",
                    "slug": f"venue-{index}",
                    "category": "Derivatives",
                    "description": description,
                }
                for index in range(130)
            ],
        }
    )
    cursor = "_"
    slugs = []
    while True:
        response = await client.protocol_search(
            "_", category="Derivatives", limit=100, cursor=cursor
        )
        rendered = json.dumps({"ok": True, "result": response}, indent=2)
        assert len(rendered.encode()) < 50 * 1024
        assert len(rendered.splitlines()) < 2000
        page = response["result"]["page"]
        rows = response["result"]["matches"]
        assert rows
        assert page["returned"] == len(rows)
        slugs.extend(row["slug"] for row in rows)
        if not page["hasMore"]:
            break
        assert int(page["nextCursor"]) == len(slugs)
        cursor = page["nextCursor"]
    assert slugs == [f"venue-{index}" for index in range(130)]
    assert client._get.await_count == 1


def test_oversized_single_row_reports_gap_not_empty_skipped_page() -> None:
    response = _enforce_response_budget(
        {
            "result": {
                "items": [{"description": "x" * 60_000}],
                "page": {"cursor": "0", "nextCursor": "1", "hasMore": True},
            }
        }
    )
    assert response["result"]["truncated"]
    assert "page" not in response["result"]
    assert response["result"]["items"] == []


@pytest.mark.asyncio
async def test_bulk_fees_filter_before_limit_and_preserve_missing():
    client = DefiLlamaFreeClient()
    client._get = AsyncMock(
        return_value={
            "url": "https://api.llama.fi/overview/fees",
            "result": {
                "protocols": [
                    {"slug": "large", "total24h": 1000},
                    {"slug": "small", "total24h": None},
                ]
            },
        }
    )
    result = await client.fees_overview(
        limit=1, protocol_slugs=["small", "missing"], data_type="dailyHoldersRevenue"
    )
    assert result["result"]["items"][0]["slug"] == "small"
    assert result["result"]["items"][0]["total24h"] is None
    assert result["result"]["unavailableSlugs"] == ["missing"]
    assert client._get.call_args.kwargs["params"]["dataType"] == "dailyHoldersRevenue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data_type", ["dailyFees", "dailyRevenue", "dailyHoldersRevenue"]
)
@pytest.mark.parametrize("include_slug", [False, True])
async def test_bulk_fees_accepts_exact_module_alias_without_merging_family(
    data_type: str,
    include_slug: bool,
) -> None:
    client = DefiLlamaFreeClient()
    client._get = AsyncMock(
        return_value={
            "url": "https://api.llama.fi/overview/fees",
            "result": {
                "protocols": [
                    {"slug": "unrelated", "module": "unrelated", "total24h": 1000},
                    {
                        "slug": "jupiter-aggregator",
                        "module": "jupiter",
                        "parentProtocol": "parent#jupiter",
                        "total24h": None,
                    },
                    {
                        "slug": "jupiter-perpetual-exchange",
                        "module": "jupiter-perpetual",
                        "parentProtocol": "parent#jupiter",
                        "total24h": 500,
                    },
                    {"module": "module-only", "total24h": 0},
                ]
            },
        }
    )
    result = (
        await client.fees_overview(
            limit=1,
            protocol_slugs=[
                "JUPITER",
                *(["jupiter-aggregator"] if include_slug else []),
                "module-only",
                "parent#jupiter",
                "missing",
            ],
            data_type=data_type,
        )
    )["result"]
    assert [row["slug"] for row in result["items"]] == ["jupiter-aggregator"]
    assert result["items"][0]["total24h"] is None
    assert result["items"][0]["parentProtocol"] == "parent#jupiter"
    assert result["page"]["totalAvailable"] == 2
    assert result["page"]["hasMore"]
    # Both aliases are found before pagination; family membership is not a match.
    assert result["unavailableSlugs"] == ["missing", "parent#jupiter"]
    assert client._get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("compare_implementations", [False, True])
async def test_alternatives_are_measured_not_allocated_and_sizing_reuses_history(
    compare_implementations: bool,
) -> None:
    tool._market_cache.clear()
    draft = variant(capital_bps=10000)
    alternative = variant(
        kind="token", instrument_id="UBTC/USDC", capital_bps=10000
    ).positions[0]
    with (
        patch.object(
            tool,
            "_read_market",
            AsyncMock(return_value={"prices": {0: 100, 86400000: 110}}),
        ) as read,
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_search_mid_prices",
            AsyncMock(
                return_value={
                    "ok": True,
                    "result": {"depth": {"BTC-USDC": {"ask_notional_usd_50bps": 50}}},
                }
            ),
        ) as depth,
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio(
            [draft, draft.model_copy(update={"budget_usd": 100000})],
            alternatives=[alternative],
            compare_implementations=compare_implementations,
        )
        assert result["ok"]
        report = result["result"]
        assert set(report["portfolio_quantification"]["assets"]) == {
            "BTC-USDC",
            "UBTC/USDC",
        }
        scope = report["implementation_comparison_scope"]
        assert scope["instruments"] == "supplied_only"
        assert scope["alternative_discovery_performed"] is False
        assert set(report["implementation_comparisons"]) == (
            {"BTC-USDC", "UBTC/USDC"} if compare_implementations else set()
        )
        if compare_implementations:
            assert report["implementation_comparisons"]["BTC-USDC"][
                "notional_by_budget"
            ] == {"100": 100, "100000": 100000}
        assert (
            report["portfolio_quantification"]["portfolios"][0]["gross_notional_bps"]
            == 10000
        )
        await tool.research_quantify_portfolio([variant(capital_bps=5000)])
        assert read.await_count == 2
        assert depth.await_count == (2 if compare_implementations else 0)
    tool._market_cache.clear()


@pytest.mark.asyncio
async def test_unavailable_history_does_not_prevent_liquidity_comparison_or_get_cached():
    tool._market_cache.clear()
    with (
        patch.object(tool, "_read_market", AsyncMock(side_effect=TimeoutError)) as read,
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.hyperliquid_search_mid_prices",
            AsyncMock(
                return_value={
                    "ok": True,
                    "result": {"depth": {"BTC-USDC": {"ask_notional_usd_50bps": 50}}},
                }
            ),
        ),
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
    ):
        result = await tool.research_quantify_portfolio(
            [variant()], compare_implementations=True
        )
        assert result["ok"]
        assert result["result"]["implementation_comparisons"]["BTC-USDC"][
            "observations"
        ]["depth"]
        await tool.research_quantify_portfolio([variant()])
        assert read.await_count == 2
    tool._market_cache.clear()

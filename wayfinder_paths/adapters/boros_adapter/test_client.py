"""Tests for BorosClient response-shape handling."""

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.boros_adapter.client import BorosClient


@pytest.mark.asyncio
async def test_get_market_extracts_from_results_list():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": [
                {"marketId": 50, "address": "0x50"},
                {
                    "marketId": 51,
                    "address": "0x51",
                    "metadata": {"assetSymbol": "HYPE"},
                },
            ],
            "total": 2,
            "skip": 0,
        }
    )

    market = await client.get_market(51)
    assert market["marketId"] == 51
    assert market["address"] == "0x51"


@pytest.mark.asyncio
async def test_get_market_falls_back_to_pagination():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(side_effect=Exception("boom"))  # type: ignore[method-assign]

    page_0 = [{"marketId": i, "address": f"0x{i:02x}"} for i in range(1, 101)]
    page_1 = [{"marketId": 123, "address": "0x7b"}]
    client.list_markets = AsyncMock(side_effect=[page_0, page_1])  # type: ignore[method-assign]

    market = await client.get_market(123)
    assert market["marketId"] == 123
    assert market["address"] == "0x7b"


@pytest.mark.asyncio
async def test_list_markets_clamps_limit_to_100():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(return_value={"results": []})  # type: ignore[method-assign]

    await client.list_markets(skip=0, limit=250, is_whitelisted=True)

    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.kwargs["params"]["limit"] == 100

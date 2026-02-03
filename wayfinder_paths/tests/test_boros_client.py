from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.boros_adapter.client import BorosClient


@pytest.mark.asyncio
async def test_get_market_history_passes_params_and_unwraps_chart_dict():
    client = BorosClient(base_url="https://example.com")
    client._http = AsyncMock(return_value={"chart": [{"t": 1}]})  # type: ignore[method-assign]

    out = await client.get_market_history(
        123,
        time_frame="5m",
        start_ts=1700000000,
        end_ts=1700003600,
    )

    assert out == [{"t": 1}]
    client._http.assert_awaited_once()
    args, kwargs = client._http.await_args
    assert args == ("GET", client.endpoints["market_chart"])
    assert kwargs["params"] == {
        "marketId": 123,
        "timeFrame": "5m",
        "startTimestamp": 1700000000,
        "endTimestamp": 1700003600,
    }


@pytest.mark.asyncio
async def test_get_market_history_accepts_list_payload():
    client = BorosClient(base_url="https://example.com")
    client._http = AsyncMock(return_value=[{"t": 1}])  # type: ignore[method-assign]

    out = await client.get_market_history(123, time_frame="1h")

    assert out == [{"t": 1}]
    args, kwargs = client._http.await_args
    assert kwargs["params"] == {"marketId": 123, "timeFrame": "1h"}

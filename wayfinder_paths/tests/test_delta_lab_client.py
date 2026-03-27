from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


delta_lab_client_module = importlib.import_module("wayfinder_paths.core.clients.DeltaLabClient")


@pytest.mark.asyncio
async def test_get_asset_timeseries_serializes_series_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delta_lab_client_module,
        "get_api_base_url",
        lambda: "https://example.com/api/v1",
    )
    client = DeltaLabClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response({"asset_id": 163, "symbol": "VIRTUAL"})
    )

    out = await client.get_asset_timeseries(
        symbol="VIRTUAL",
        series=["price", "funding", "lending"],
        lookback_days=30,
        limit=2000,
    )

    assert out == {}
    client._authed_request.assert_awaited_once()
    args, kwargs = client._authed_request.await_args
    assert args == ("GET", "https://example.com/api/v1/delta-lab/assets/VIRTUAL/timeseries")
    assert kwargs["params"] == {
        "lookback_days": 30,
        "limit": 2000,
        "series": "price,funding,lending",
    }


@pytest.mark.asyncio
async def test_get_asset_timeseries_keeps_string_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delta_lab_client_module,
        "get_api_base_url",
        lambda: "https://example.com/api/v1",
    )
    client = DeltaLabClient()
    client._authed_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_Response({"asset_id": 163, "symbol": "VIRTUAL"})
    )

    await client.get_asset_timeseries(
        symbol="VIRTUAL",
        series="price,funding,lending",
    )

    _, kwargs = client._authed_request.await_args
    assert kwargs["params"]["series"] == "price,funding,lending"

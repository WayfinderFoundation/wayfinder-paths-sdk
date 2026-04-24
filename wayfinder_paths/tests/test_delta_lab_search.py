"""Unit tests for Pass 3: search methods."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient

delta_lab_client_module = importlib.import_module(
    "wayfinder_paths.core.clients.DeltaLabClient"
)


def _patch_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delta_lab_client_module, "get_api_base_url", lambda: "https://x/api/v1"
    )


class _Resp:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _make_client(monkeypatch, payloads):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    mock = AsyncMock(side_effect=[_Resp(p) for p in payloads])
    c._authed_request = mock  # type: ignore[method-assign]
    return c, mock


@pytest.mark.asyncio
async def test_search_assets_v2_hits_new_endpoint_and_passes_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"asset_id": 2, "symbol": "ETH"}], "count": 1, "has_more": False}],
    )
    page = await c.search_assets_v2(
        q="ETH", chain_id=1, basis_root="ETH", has_address=True, fields="full"
    )
    assert page["items"][0]["symbol"] == "ETH"
    url = mock.await_args.args[1]
    assert url == "https://x/api/v1/delta-lab/search/assets/"
    assert mock.await_args.kwargs["params"] == {
        "q": "ETH",
        "chain_id": 1,
        "basis_root": "ETH",
        "has_address": True,
        "limit": 25,
        "offset": 0,
        "fields": "full",
    }


@pytest.mark.asyncio
async def test_search_assets_v2_does_not_collide_with_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Legacy `search_assets` targets /assets/search/; new one targets /search/assets/.
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    legacy_mock = AsyncMock(return_value=_Resp({"results": []}))
    c._authed_request = legacy_mock  # type: ignore[method-assign]
    await c.search_assets(query="ETH")
    legacy_url = legacy_mock.await_args.args[1]
    assert "/assets/search" in legacy_url and "/search/assets" not in legacy_url


@pytest.mark.asyncio
async def test_search_markets_params(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"market_id": 912}], "count": 1, "has_more": False}],
    )
    await c.search_markets(
        venue="aave-bsc", market_type="LENDING", asset_id=2, limit=10
    )
    url = mock.await_args.args[1]
    assert url.endswith("/search/markets/")
    # _dl_request strips None-valued params
    assert mock.await_args.kwargs["params"] == {
        "venue": "aave-bsc",
        "market_type": "LENDING",
        "asset_id": 2,
        "limit": 10,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_search_instruments_serializes_maturity_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(monkeypatch, [{"items": [], "count": 0, "has_more": False}])
    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    await c.search_instruments(
        instrument_type="PERP",
        maturity_after=cutoff,
        maturity_before="2026-12-31T00:00:00+00:00",
    )
    params = mock.await_args.kwargs["params"]
    assert params["maturity_after"] == cutoff.isoformat()
    assert params["maturity_before"] == "2026-12-31T00:00:00+00:00"


@pytest.mark.asyncio
async def test_search_opportunities_side_guard() -> None:
    c = DeltaLabClient()
    with pytest.raises(ValueError, match="side must be"):
        await c.search_opportunities(side="both")


@pytest.mark.asyncio
async def test_search_opportunities_params(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"instrument_id": 37459}], "count": 1, "has_more": False}],
    )
    await c.search_opportunities(basis_root="ETH", side="LONG", venue="boros", limit=5)
    assert mock.await_args.kwargs["params"] == {
        "basis_root": "ETH",
        "side": "LONG",
        "venue": "boros",
        "limit": 5,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_search_venues_params(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"name": "aave-bsc"}], "count": 1, "has_more": False}],
    )
    await c.search_venues(q="aave", venue_type="LENDING", chain_id=56, limit=3)
    assert mock.await_args.args[1].endswith("/search/venues/")


@pytest.mark.asyncio
async def test_search_all_walks_until_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        {"items": [{"n": 1}, {"n": 2}], "has_more": True},
        {"items": [{"n": 3}, {"n": 4}], "has_more": True},
        {"items": [{"n": 5}], "has_more": False},
    ]
    c, mock = _make_client(monkeypatch, pages)
    collected = [
        item async for item in c.search_all(c.search_assets_v2, q="ETH", batch=2)
    ]
    assert [c["n"] for c in collected] == [1, 2, 3, 4, 5]
    offsets = [call.kwargs["params"]["offset"] for call in mock.await_args_list]
    assert offsets == [0, 2, 4]


@pytest.mark.asyncio
async def test_search_all_respects_max_items(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {"items": [{"n": i} for i in range(1, 6)], "has_more": True},
        {"items": [{"n": i} for i in range(6, 11)], "has_more": True},
    ]
    c, _ = _make_client(monkeypatch, pages)
    collected = [
        item
        async for item in c.search_all(
            c.search_assets_v2, q="ETH", batch=5, max_items=7
        )
    ]
    assert [c["n"] for c in collected] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.asyncio
async def test_search_all_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [{"items": [], "has_more": True}]  # server says has_more but sends nothing
    c, _ = _make_client(monkeypatch, pages)
    collected = [item async for item in c.search_all(c.search_assets_v2, q="X")]
    assert collected == []

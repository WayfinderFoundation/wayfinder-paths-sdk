"""Unit tests for Pass 2: entity lookups, catalog listings, and graph methods."""

from __future__ import annotations

import importlib
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


def _make_client(
    monkeypatch: pytest.MonkeyPatch, payloads: list
) -> tuple[DeltaLabClient, AsyncMock]:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    mock = AsyncMock(side_effect=[_Resp(p) for p in payloads])
    c._authed_request = mock  # type: ignore[method-assign]
    return c, mock


# ---------- Entity ----------


@pytest.mark.asyncio
async def test_get_asset_by_id_returns_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch, [{"asset_id": 2, "symbol": "ETH", "chain_id": 1}]
    )
    ai = await c.get_asset_by_id(asset_id=2)
    assert ai.asset_id == 2 and ai.symbol == "ETH" and ai.chain_id == 1
    url = mock.await_args.args[1]
    assert url == "https://x/api/v1/delta-lab/assets/id/2/"


@pytest.mark.asyncio
async def test_get_asset_markets_unwraps(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {"market_id": 1, "role": "BASE"},
                    {"market_id": 2, "role": "LENDING_ASSET"},
                ],
                "count": 2,
            }
        ],
    )
    rows = await c.get_asset_markets(symbol="ETH", chain_id=8453)
    assert [r["market_id"] for r in rows] == [1, 2]
    kwargs = mock.await_args.kwargs
    assert kwargs["params"] == {"chain_id": 8453}


@pytest.mark.asyncio
async def test_get_asset_markets_drops_none_chain_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(monkeypatch, [{"items": [], "count": 0}])
    await c.get_asset_markets(symbol="ETH")
    assert mock.await_args.kwargs["params"] == {}


@pytest.mark.asyncio
async def test_venue_market_instrument_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "venue_id": 7,
                "name": "aave-bsc",
                "venue_type": "LENDING",
                "chain_id": 56,
                "market_count": 1,
            },
            {"venue_id": 7, "name": "aave-bsc"},
            {"market_id": 912, "venue": "aave-bsc", "market_type": "LENDING"},
            {"market_id": 912, "venue": "aave-bsc"},
            {"instrument_id": 37459, "base_symbol": "WETH", "market_id": 18900},
        ],
    )
    v = await c.get_venue_by_id(venue_id=7)
    v2 = await c.get_venue_by_name(name="aave-bsc")
    m = await c.get_market_by_id(market_id=912)
    m2 = await c.get_market_by_venue_external(venue="aave-bsc", external_id="0xabc")
    i = await c.get_instrument_by_id(instrument_id=37459)

    assert v.venue_type == "LENDING" and v2.name == "aave-bsc"
    assert m.market_type == "LENDING" and m2.market_id == 912
    assert i.instrument_id == 37459 and i.base_symbol == "WETH"

    urls = [call.args[1] for call in mock.await_args_list]
    assert urls == [
        "https://x/api/v1/delta-lab/venues/id/7/",
        "https://x/api/v1/delta-lab/venues/aave-bsc/",
        "https://x/api/v1/delta-lab/markets/id/912/",
        "https://x/api/v1/delta-lab/markets/aave-bsc/0xabc/",
        "https://x/api/v1/delta-lab/instruments/id/37459/",
    ]


# ---------- Catalog ----------


@pytest.mark.asyncio
async def test_list_basis_roots_sends_offset_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"symbol": "0G"}], "count": 1, "total_count": 3891}],
    )
    page = await c.list_basis_roots(limit=5, offset=10)
    assert page["total_count"] == 3891
    assert mock.await_args.kwargs["params"] == {"limit": 5, "offset": 10}


@pytest.mark.asyncio
async def test_list_basis_members_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [{"items": [{"symbol": "stETH"}, {"symbol": "wstETH"}], "count": 2}],
    )
    members = await c.list_basis_members(root_symbol="ETH")
    assert [m["symbol"] for m in members] == ["stETH", "wstETH"]


@pytest.mark.asyncio
async def test_list_venues_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(monkeypatch, [{"items": [{"name": "aave-bsc"}], "count": 1}])
    venues = await c.list_venues(venue_type="LENDING", chain_id=56)
    assert venues[0]["name"] == "aave-bsc"
    assert mock.await_args.kwargs["params"] == {"venue_type": "LENDING", "chain_id": 56}


@pytest.mark.asyncio
async def test_list_chains_and_instrument_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {"items": [{"chain_id": 1}, {"chain_id": 56}], "count": 2},
            {"items": [{"instrument_type": "LENDING_SUPPLY"}], "count": 1},
        ],
    )
    chains = await c.list_chains()
    types = await c.list_instrument_types()
    assert [c["chain_id"] for c in chains] == [1, 56]
    assert types[0]["instrument_type"] == "LENDING_SUPPLY"


@pytest.mark.asyncio
async def test_iter_list_walks_pages_via_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 3 pages of 2 items each, then a short page of 1, then stop.
    pages = [
        {"items": [{"n": 1}, {"n": 2}], "total_count": 5},
        {"items": [{"n": 3}, {"n": 4}], "total_count": 5},
        {"items": [{"n": 5}], "total_count": 5},
    ]
    c, mock = _make_client(monkeypatch, pages)
    collected = []
    async for item in c.iter_list("/list/basis-roots/", batch=2):
        collected.append(item["n"])
    assert collected == [1, 2, 3, 4, 5]
    # Offsets sent: 0, 2, 4
    offsets = [call.kwargs["params"]["offset"] for call in mock.await_args_list]
    assert offsets == [0, 2, 4]


@pytest.mark.asyncio
async def test_iter_list_respects_total_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Server keeps returning full batches — iter_list must stop at total_count.
    pages = [
        {"items": [{"n": i} for i in range(1, 3)], "total_count": 4},
        {"items": [{"n": i} for i in range(3, 5)], "total_count": 4},
    ]
    c, _ = _make_client(monkeypatch, pages)
    collected = [item async for item in c.iter_list("/list/basis-roots/", batch=2)]
    assert [c["n"] for c in collected] == [1, 2, 3, 4]


# ---------- Graph ----------


@pytest.mark.asyncio
async def test_get_asset_relations_passes_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {"relation_type": "WRAPS", "path_symbols": ["ETH", "wstETH"]}
                ],
                "count": 1,
            }
        ],
    )
    rels = await c.get_asset_relations(
        asset_id=2, direction="forward", depth=2, relation_types="WRAPS"
    )
    assert rels[0]["relation_type"] == "WRAPS"
    assert mock.await_args.kwargs["params"] == {
        "direction": "forward",
        "depth": 2,
        "relation_type": "WRAPS",
    }


def test_get_asset_relations_guards_depth() -> None:
    c = DeltaLabClient()
    import asyncio

    with pytest.raises(ValueError, match="depth must be"):
        asyncio.run(c.get_asset_relations(asset_id=2, depth=0))
    with pytest.raises(ValueError, match="depth must be"):
        asyncio.run(c.get_asset_relations(asset_id=2, depth=4))


@pytest.mark.asyncio
async def test_get_graph_paths_serializes_relation_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"asset_symbols": ["ETH", "WETH", "BTC"]}], "count": 1}],
    )
    paths = await c.get_graph_paths(
        from_asset_id=2,
        to_asset_id=1,
        max_hops=2,
        relation_types=["WRAPS", "REBASING_TO_BASE"],
    )
    assert paths[0]["asset_symbols"][0] == "ETH"
    assert mock.await_args.kwargs["params"] == {
        "from_asset_id": 2,
        "to_asset_id": 1,
        "max_hops": 2,
        "relation_types": "WRAPS,REBASING_TO_BASE",
    }


@pytest.mark.asyncio
async def test_get_asset_relations_accepts_list_of_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(
        monkeypatch,
        [{"items": [{"relation_type": "WRAPS"}], "count": 1}],
    )
    await c.get_asset_relations(
        asset_id=2, relation_types=["WRAPS", "REBASING_TO_BASE"]
    )
    assert mock.await_args.kwargs["params"]["relation_type"] == "WRAPS,REBASING_TO_BASE"


@pytest.mark.asyncio
async def test_summarize_asset_relations_groups_by_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {"relation_type": "WRAPS", "from_asset_symbol": "wstETH"},
                    {"relation_type": "WRAPS", "from_asset_symbol": "sfrxETH"},
                    {"relation_type": "WRAPS", "from_asset_symbol": "rETH"},
                    {"relation_type": "WRAPS", "from_asset_symbol": "ankrETH"},
                    {"relation_type": "REBASING_TO_BASE", "from_asset_symbol": "stETH"},
                    {"relation_type": "BASIS", "from_asset_symbol": "LP-ETH"},
                ],
                "count": 6,
            }
        ],
    )
    summary = await c.summarize_asset_relations(asset_id=2, examples_per_type=2)
    assert summary["asset_id"] == 2 and summary["total"] == 6
    groups = summary["by_relation_type"]
    assert groups["WRAPS"]["count"] == 4
    assert groups["WRAPS"]["examples"] == ["wstETH", "sfrxETH"]  # capped at 2
    assert groups["REBASING_TO_BASE"]["count"] == 1
    assert groups["BASIS"]["count"] == 1
    # Raw items still reachable
    assert len(summary["items"]) == 6


def test_get_graph_paths_guards_max_hops() -> None:
    c = DeltaLabClient()
    import asyncio

    with pytest.raises(ValueError, match="max_hops must be"):
        asyncio.run(c.get_graph_paths(from_asset_id=1, to_asset_id=2, max_hops=0))
    with pytest.raises(ValueError, match="max_hops must be"):
        asyncio.run(c.get_graph_paths(from_asset_id=1, to_asset_id=2, max_hops=5))

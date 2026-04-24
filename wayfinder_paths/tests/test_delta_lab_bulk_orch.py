"""Unit tests for Pass 5: bulk auto-chunking + orchestration."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from wayfinder_paths.core.clients.delta_lab_types import (
    BacktestBundle,
    LendingLatest,
    PriceLatest,
)
from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient

delta_lab_client_module = importlib.import_module(
    "wayfinder_paths.core.clients.DeltaLabClient"
)


def _patch_base_url(monkeypatch):
    monkeypatch.setattr(
        delta_lab_client_module, "get_api_base_url", lambda: "https://x/api/v1"
    )


class _Resp:
    def __init__(self, payload):
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


# ---------- _bulk_chunked ----------


@pytest.mark.asyncio
async def test_bulk_chunked_splits_at_cap_and_merges(monkeypatch):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    c._BULK_CAP = 2
    mock = AsyncMock(
        side_effect=[
            _Resp({"1": [{"ts": "2026-04-22T00:00:00+00:00", "v": 1}], "2": []}),
            _Resp({"3": [{"ts": "2026-04-22T00:00:00+00:00", "v": 3}], "4": []}),
            _Resp({"5": []}),
        ]
    )
    c._authed_request = mock  # type: ignore[method-assign]
    merged = await c._bulk_chunked("/bulk/x/", [1, 2, 3, 4, 5])
    assert sorted(merged.keys()) == ["1", "2", "3", "4", "5"]
    ids_sent = [call.kwargs["params"]["ids"] for call in mock.await_args_list]
    assert ids_sent == ["1,2", "3,4", "5"]


@pytest.mark.asyncio
async def test_bulk_chunked_dedupes_ids(monkeypatch):
    c, mock = _make_client(monkeypatch, [{"1": [], "2": []}])
    await c._bulk_chunked("/bulk/x/", [1, 2, 1, 2])
    assert mock.await_args.kwargs["params"]["ids"] == "1,2"


@pytest.mark.asyncio
async def test_bulk_chunked_empty_returns_empty(monkeypatch):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    mock = AsyncMock(return_value=_Resp({"should_not": "be_called"}))
    c._authed_request = mock  # type: ignore[method-assign]
    assert await c._bulk_chunked("/bulk/x/", []) == {}
    mock.assert_not_awaited()


# ---------- _bulk_pairs_chunked ----------


@pytest.mark.asyncio
async def test_bulk_pairs_uses_get_when_small(monkeypatch):
    c, mock = _make_client(monkeypatch, [{"912:2": [], "50:7": []}])
    await c._bulk_pairs_chunked("/bulk/lending/", [(912, 2), (50, 7)])
    assert mock.await_args.args[0] == "GET"
    assert mock.await_args.kwargs["params"]["pairs"] == "912:2,50:7"


@pytest.mark.asyncio
async def test_bulk_pairs_switches_to_post_over_20(monkeypatch):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    c._BULK_CAP = 25  # allow a single big chunk of 21 pairs
    pairs = [(i, i + 1000) for i in range(21)]
    payload = {f"{m}:{a}": [] for m, a in pairs}
    mock = AsyncMock(return_value=_Resp(payload))
    c._authed_request = mock  # type: ignore[method-assign]
    await c._bulk_pairs_chunked("/bulk/lending/", pairs)
    assert mock.await_args.args[0] == "POST"
    body = mock.await_args.kwargs["json"]
    assert body["pairs"][0] == [0, 1000]
    assert len(body["pairs"]) == 21


@pytest.mark.asyncio
async def test_bulk_pairs_dedupes(monkeypatch):
    c, mock = _make_client(monkeypatch, [{"1:2": []}])
    await c._bulk_pairs_chunked("/bulk/lending/", [(1, 2), (1, 2), (1, 2)])
    assert mock.await_args.kwargs["params"]["pairs"] == "1:2"


# ---------- Typed bulk TS methods ----------


@pytest.mark.asyncio
async def test_bulk_prices_returns_df_map(monkeypatch):
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "1": [
                    {"ts": "2026-04-22T20:00:00+00:00", "price_usd": 77000.0},
                    {"ts": "2026-04-22T21:00:00+00:00", "price_usd": 77500.0},
                ],
                "2": [{"ts": "2026-04-22T20:00:00+00:00", "price_usd": 2400.0}],
            }
        ],
    )
    dfs = await c.bulk_prices(asset_ids=[1, 2], lookback_days=1, limit_per_key=2)
    assert set(dfs.keys()) == {1, 2}
    assert isinstance(dfs[1].index, pd.DatetimeIndex)
    assert len(dfs[1]) == 2 and len(dfs[2]) == 1
    assert list(dfs[1].columns) == ["price_usd"]


@pytest.mark.asyncio
async def test_bulk_lending_returns_tuple_keyed_map(monkeypatch):
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "912:2": [
                    {
                        "ts": "2026-04-22T20:00:00+00:00",
                        "supply_apr": 0.008,
                        "market_id": 912,
                    }
                ],
                "50:7": [],
            }
        ],
    )
    dfs = await c.bulk_lending(pairs=[(912, 2), (50, 7)])
    assert set(dfs.keys()) == {(912, 2), (50, 7)}
    assert "supply_apr" in dfs[(912, 2)].columns


# ---------- Typed bulk latest methods ----------


@pytest.mark.asyncio
async def test_bulk_latest_prices_returns_typed_or_none(monkeypatch):
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "1": {
                    "asset_id": 1,
                    "asof_ts": "2026-04-24T18:00:00+00:00",
                    "price_usd": 77000.0,
                },
                "2": None,
                "3": {
                    "asset_id": 3,
                    "asof_ts": "2026-04-24T18:00:00+00:00",
                    "price_usd": 2.0,
                },
            }
        ],
    )
    result = await c.bulk_latest_prices(asset_ids=[1, 2, 3])
    assert isinstance(result[1], PriceLatest) and result[1].price_usd == 77000.0
    assert result[2] is None
    assert result[3].asset_id == 3  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_bulk_latest_lending_tuple_keyed(monkeypatch):
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "912:2": {
                    "market_id": 912,
                    "asset_id": 2,
                    "asof_ts": "2026-04-24T18:00:00+00:00",
                    "venue_name": "aave-bsc",
                    "net_supply_apr_now": 0.008,
                },
                "50:7": None,
            }
        ],
    )
    result = await c.bulk_latest_lending(pairs=[(912, 2), (50, 7)])
    assert isinstance(result[(912, 2)], LendingLatest)
    assert result[(912, 2)].venue_name == "aave-bsc"  # type: ignore[union-attr]
    assert result[(50, 7)] is None


# ---------- explore ----------


@pytest.mark.asyncio
async def test_explore_passes_params(monkeypatch):
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "query": "ETH",
                "asset": {},
                "relations": [],
                "markets": [],
                "price_latest": {},
                "yield_latest": None,
            }
        ],
    )
    out = await c.explore(symbol="ETH", chain_id=1, relations_depth=1)
    assert out["query"] == "ETH"
    assert mock.await_args.args[1].endswith("/explore/ETH/")
    assert mock.await_args.kwargs["params"] == {"chain_id": 1, "relations_depth": 1}


def test_explore_guards_depth_range():
    import asyncio as _a

    c = DeltaLabClient()
    with pytest.raises(ValueError, match="relations_depth must be"):
        _a.run(c.explore(symbol="ETH", relations_depth=0))
    with pytest.raises(ValueError, match="relations_depth must be"):
        _a.run(c.explore(symbol="ETH", relations_depth=4))


@pytest.mark.asyncio
async def test_explore_warns_on_high_depth(monkeypatch):
    c, _ = _make_client(monkeypatch, [{"query": "ETH"}])
    with pytest.warns(UserWarning, match="relations_depth=2"):
        await c.explore(symbol="ETH", relations_depth=2)


# ---------- fetch_backtest_bundle ----------


@pytest.mark.asyncio
async def test_fetch_backtest_bundle_parses_structure(monkeypatch):
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "basis_root": "ETH",
                "side": "LONG",
                "lookback_days": 2,
                "start": "2026-04-22T00:00:00+00:00",
                "end": "2026-04-24T00:00:00+00:00",
                "opportunities": [
                    {"instrument_id": 37459, "venue": "boros", "market_id": 18900}
                ],
                "funding_ts": {
                    "37459": [
                        {
                            "ts": "2026-04-22T20:00:00+00:00",
                            "funding_rate": 0.0,
                            "instrument_id": 37459,
                        }
                    ]
                },
                "lending_ts": {
                    "912:2": [
                        {
                            "ts": "2026-04-22T20:00:00+00:00",
                            "supply_apr": 0.008,
                            "market_id": 912,
                        }
                    ]
                },
            }
        ],
    )
    bundle = await c.fetch_backtest_bundle(
        basis_root="ETH", side="LONG", lookback_days=2, instrument_limit=3
    )
    assert isinstance(bundle, BacktestBundle)
    assert bundle.basis_root == "ETH" and bundle.side == "LONG"
    assert len(bundle.opportunities) == 1
    assert list(bundle.funding_ts.keys()) == [37459]
    assert list(bundle.lending_ts.keys()) == [(912, 2)]
    assert isinstance(bundle.funding_ts[37459].index, pd.DatetimeIndex)
    assert bundle.start is not None and bundle.start.year == 2026

    # POST body shape
    body = mock.await_args.kwargs["json"]
    assert body == {
        "basis_root": "ETH",
        "lookback_days": 2,
        "limit_per_key": 500,
        "side": "LONG",
        "instrument_limit": 3,
    }


@pytest.mark.asyncio
async def test_fetch_backtest_bundle_side_guard():
    c = DeltaLabClient()
    with pytest.raises(ValueError, match="side must be"):
        await c.fetch_backtest_bundle(basis_root="ETH", side="both")


@pytest.mark.asyncio
async def test_fetch_lending_bundle_composes_search_and_bulk(monkeypatch):
    c, mock = _make_client(
        monkeypatch,
        [
            # 1st call: search_opportunities
            {
                "items": [
                    {
                        "instrument_id": 100,
                        "market_id": 912,
                        "deposit_asset_id": 2,
                        "side": "LONG",
                    },
                    {
                        "instrument_id": 101,
                        "market_id": 913,
                        "deposit_asset_id": 2,
                        "side": "LONG",
                    },
                ],
                "count": 2,
                "has_more": False,
            },
            # 2nd call: bulk_lending
            {
                "912:2": [{"ts": "2026-04-22T20:00:00+00:00", "supply_apr": 0.008}],
                "913:2": [{"ts": "2026-04-22T20:00:00+00:00", "supply_apr": 0.009}],
            },
        ],
    )
    bundle = await c.fetch_lending_bundle(
        basis_root="ETH", side="LONG", lookback_days=2, instrument_limit=5
    )
    assert bundle.basis_root == "ETH" and bundle.side == "LONG"
    assert len(bundle.opportunities) == 2
    assert set(bundle.lending_ts.keys()) == {(912, 2), (913, 2)}
    assert bundle.funding_ts == {}  # lending-only doesn't fan out funding
    # Verify the URL path targeted search/opportunities then bulk/lending
    paths = [call.args[1] for call in mock.await_args_list]
    assert "/search/opportunities/" in paths[0]
    assert "/bulk/lending/" in paths[1]


@pytest.mark.asyncio
async def test_fetch_perp_bundle_fans_funding(monkeypatch):
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "items": [{"instrument_id": 200, "market_id": 18900, "side": "LONG"}],
                "count": 1,
                "has_more": False,
            },
            {"200": [{"ts": "2026-04-22T20:00:00+00:00", "funding_rate": 0.0001}]},
        ],
    )
    bundle = await c.fetch_perp_bundle(basis_root="ETH", side="LONG", lookback_days=2)
    assert bundle.funding_ts.keys() == {200}
    assert bundle.lending_ts == {}  # perp-only doesn't fan out lending


@pytest.mark.asyncio
async def test_fetch_lending_bundle_side_guard():
    c = DeltaLabClient()
    with pytest.raises(ValueError, match="side must be"):
        await c.fetch_lending_bundle(basis_root="ETH", side="BOTH")


def test_alias_screen_items_adds_items_key():
    out = DeltaLabClient._alias_screen_items({"data": [{"a": 1}], "count": 1})
    assert out["items"] is out["data"]


def test_alias_screen_items_preserves_existing_items():
    original = {"items": [{"a": 1}], "count": 1}
    out = DeltaLabClient._alias_screen_items(original)
    assert out is original  # idempotent
    assert "data" not in out


def test_alias_screen_items_no_op_on_non_dict():
    assert DeltaLabClient._alias_screen_items([1, 2, 3]) == [1, 2, 3]
    assert DeltaLabClient._alias_screen_items(None) is None

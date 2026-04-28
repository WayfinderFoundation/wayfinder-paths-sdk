"""Unit tests for Pass 4: point timeseries and latest snapshots."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from wayfinder_paths.core.clients.delta_lab_types import (
    BorosLatest,
    FundingLatest,
    LendingLatest,
    PendleLatest,
    PriceLatest,
    YieldLatest,
)
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


class _ErrResp:
    def __init__(self, payload, *, status: int) -> None:
        self._payload = payload
        self.status_code = status
        self.reason_phrase = "Not Found"
        self.text = str(payload)

    def json(self):
        return self._payload


def _make_client(monkeypatch, payloads):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    mock = AsyncMock(side_effect=[_Resp(p) for p in payloads])
    c._authed_request = mock  # type: ignore[method-assign]
    return c, mock


def _make_client_with_404(monkeypatch):
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    err = _ErrResp({"error": "not_found", "message": "no snapshot"}, status=404)
    mock = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "/"),
            response=err,  # type: ignore[arg-type]
        )
    )
    c._authed_request = mock  # type: ignore[method-assign]
    return c, mock


_PRICE_ROWS = [
    {"ts": "2026-04-22T20:00:00+00:00", "price_usd": 2400.0},
    {"ts": "2026-04-22T21:00:00+00:00", "price_usd": 2410.0},
]


# ---------- Price ----------


@pytest.mark.asyncio
async def test_price_ts_returns_indexed_dataframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, mock = _make_client(monkeypatch, [{"items": _PRICE_ROWS, "count": 2}])
    df = await c.get_asset_price_ts(
        asset_id=2, lookback_days=3, limit=5, start=datetime(2026, 4, 22, tzinfo=UTC)
    )
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.columns) == ["price_usd"]
    assert len(df) == 2
    url = mock.await_args.args[1]
    assert url == "https://x/api/v1/delta-lab/assets/id/2/price/"
    params = mock.await_args.kwargs["params"]
    assert params["lookback_days"] == 3 and params["limit"] == 5
    assert params["start"] == "2026-04-22T00:00:00+00:00"


@pytest.mark.asyncio
async def test_price_latest_returns_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "asset_id": 2,
                "asof_ts": "2026-04-24T18:00:00+00:00",
                "price_usd": 2318.7,
                "ret_7d": -0.047,
            }
        ],
    )
    latest = await c.get_asset_price_latest(asset_id=2)
    assert isinstance(latest, PriceLatest)
    assert latest.price_usd == 2318.7 and latest.ret_7d == -0.047


@pytest.mark.asyncio
async def test_price_latest_soft_404_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = _make_client_with_404(monkeypatch)
    assert await c.get_asset_price_latest(asset_id=99999) is None


# ---------- Yield ----------


@pytest.mark.asyncio
async def test_yield_ts_and_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {
                        "ts": "2026-04-22T20:00:00+00:00",
                        "apy_base": 0.03,
                        "tvl_usd": 1e9,
                    }
                ],
                "count": 1,
            },
            {
                "asset_id": 2,
                "asof_ts": "2026-04-22T20:00:00+00:00",
                "apy_base": 0.026,
                "exchange_rate": 1.0,
            },
        ],
    )
    df = await c.get_asset_yield_ts(asset_id=2, lookback_days=1, limit=1)
    assert len(df) == 1 and "apy_base" in df.columns
    yl = await c.get_asset_yield_latest(asset_id=2)
    assert isinstance(yl, YieldLatest) and yl.apy_base == 0.026


@pytest.mark.asyncio
async def test_yield_latest_soft_404(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client_with_404(monkeypatch)
    assert await c.get_asset_yield_latest(asset_id=2) is None


# ---------- Lending ----------


@pytest.mark.asyncio
async def test_lending_ts_requires_asset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {
                        "ts": "2026-04-22T20:00:00+00:00",
                        "supply_apr": 0.008,
                        "market_id": 912,
                    }
                ],
                "count": 1,
            }
        ],
    )
    df = await c.get_market_lending_ts(market_id=912, asset_id=2, lookback_days=2)
    assert "supply_apr" in df.columns
    params = mock.await_args.kwargs["params"]
    assert params["asset_id"] == 2  # required param passed through


@pytest.mark.asyncio
async def test_lending_latest_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    c, mock = _make_client(
        monkeypatch,
        [
            {
                "market_id": 912,
                "asset_id": 2,
                "asof_ts": "2026-04-24T18:00:00+00:00",
                "venue_name": "aave-bsc",
                "net_supply_apr_now": 0.008,
            }
        ],
    )
    latest = await c.get_market_lending_latest(market_id=912, asset_id=2)
    assert isinstance(latest, LendingLatest)
    assert latest.venue_name == "aave-bsc"
    assert mock.await_args.kwargs["params"] == {"asset_id": 2}


# ---------- Boros / Pendle ----------


@pytest.mark.asyncio
async def test_boros_ts_and_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {
                        "ts": "2026-04-22T20:00:00+00:00",
                        "pv": 16.5,
                        "fixed_rate_mark": 0.48,
                        "market_id": 18900,
                    }
                ],
                "count": 1,
            },
            {
                "market_id": 18900,
                "asof_ts": "2026-04-24T18:00:00+00:00",
                "pv": 16.4,
                "fixed_rate_mark": 0.47,
            },
        ],
    )
    df = await c.get_market_boros_ts(market_id=18900)
    assert "fixed_rate_mark" in df.columns
    bl = await c.get_market_boros_latest(market_id=18900)
    assert isinstance(bl, BorosLatest) and bl.fixed_rate_mark == 0.47


@pytest.mark.asyncio
async def test_pendle_latest_soft_404(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client_with_404(monkeypatch)
    assert await c.get_market_pendle_latest(market_id=99999) is None


@pytest.mark.asyncio
async def test_pendle_latest_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [{"market_id": 42, "asof_ts": "2026-04-24T18:00:00+00:00"}],
    )
    pl = await c.get_market_pendle_latest(market_id=42)
    assert isinstance(pl, PendleLatest) and pl.market_id == 42


# ---------- Funding ----------


@pytest.mark.asyncio
async def test_funding_ts_and_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = _make_client(
        monkeypatch,
        [
            {
                "items": [
                    {
                        "ts": "2026-04-22T20:00:00+00:00",
                        "funding_rate": 0.0001,
                        "instrument_id": 100,
                    }
                ],
                "count": 1,
            },
            {
                "instrument_id": 100,
                "asof_ts": "2026-04-22T20:00:00+00:00",
                "funding_rate": 0.0002,
                "venue": "hyperliquid",
            },
        ],
    )
    df = await c.get_instrument_funding_ts(instrument_id=100)
    assert "funding_rate" in df.columns
    fl = await c.get_instrument_funding_latest(instrument_id=100)
    assert isinstance(fl, FundingLatest) and fl.venue == "hyperliquid"


# ---------- Cross-cutting ----------


@pytest.mark.asyncio
async def test_ts_empty_payload_returns_empty_dataframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = _make_client(monkeypatch, [{"items": [], "count": 0}])
    df = await c.get_asset_price_ts(asset_id=2)
    assert df.empty

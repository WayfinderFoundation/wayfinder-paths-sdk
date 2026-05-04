"""Unit tests for Pass 1 (foundation): URL normalisation, error parsing,
typed records, and shared helpers on DeltaLabClient.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from wayfinder_paths.core.clients.delta_lab_types import (
    AssetInfo,
    BorosLatest,
    DeltaLabAPIError,
    FundingLatest,
    InstrumentInfo,
    LendingLatest,
    MarketInfo,
    PendleLatest,
    PriceLatest,
    VenueInfo,
    YieldLatest,
)
from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient

delta_lab_client_module = importlib.import_module(
    "wayfinder_paths.core.clients.DeltaLabClient"
)


def _patch_base_url(
    monkeypatch: pytest.MonkeyPatch, base: str = "https://x/api/v1"
) -> None:
    monkeypatch.setattr(delta_lab_client_module, "get_api_base_url", lambda: base)


class _Response:
    def __init__(self, payload, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _ErrResponse:
    """Mimics an httpx.Response enough for _extract_error + HTTPStatusError."""

    def __init__(self, payload, *, status: int) -> None:
        self._payload = payload
        self.status_code = status
        self.reason_phrase = "Error"
        self.text = str(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ---------- URL normalisation ----------


def test_dl_url_appends_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch, "https://x/api/v1")
    c = DeltaLabClient()
    assert c._dl_url("/basis-symbols") == "https://x/api/v1/delta-lab/basis-symbols/"
    assert c._dl_url("basis-symbols") == "https://x/api/v1/delta-lab/basis-symbols/"
    assert c._dl_url("/basis-symbols/") == "https://x/api/v1/delta-lab/basis-symbols/"


def test_dl_url_strips_double_slashes_when_base_trailing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch, "https://x/api/v1/")
    c = DeltaLabClient()
    url = c._dl_url("/venues/id/7/")
    assert url == "https://x/api/v1/delta-lab/venues/id/7/"


# ---------- _dl_request behavior ----------


@pytest.mark.asyncio
async def test_dl_request_drops_none_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    c._authed_request = AsyncMock(return_value=_Response({"ok": True}))  # type: ignore[method-assign]

    await c._dl_request("GET", "/x", params={"a": 1, "b": None, "c": "y"})

    _, kwargs = c._authed_request.await_args
    assert kwargs["params"] == {"a": 1, "c": "y"}


@pytest.mark.asyncio
async def test_dl_request_soft_not_found_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    err_resp = _ErrResponse(
        {"error": "not_found", "message": "No snapshot"}, status=404
    )
    c._authed_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "/"), response=err_resp
        )  # type: ignore[arg-type]
    )

    result = await c._dl_request(
        "GET", "/assets/id/1/yield/latest/", soft_not_found=True
    )
    assert result is None


@pytest.mark.asyncio
async def test_dl_request_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    err_resp = _ErrResponse(
        {"error": "bulk_cap_exceeded", "message": "max 100"}, status=400
    )
    c._authed_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "400", request=httpx.Request("GET", "/"), response=err_resp
        )  # type: ignore[arg-type]
    )

    with pytest.raises(DeltaLabAPIError) as exc_info:
        await c._dl_request("GET", "/bulk/prices/", params={"ids": "1,2"})

    err = exc_info.value
    assert err.code == "bulk_cap_exceeded"
    assert err.status == 400
    assert err.message == "max 100"


@pytest.mark.asyncio
async def test_dl_request_soft_not_found_still_raises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    err_resp = _ErrResponse(
        {"error": "invalid_parameter", "message": "bad"}, status=400
    )
    c._authed_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "400", request=httpx.Request("GET", "/"), response=err_resp
        )  # type: ignore[arg-type]
    )

    with pytest.raises(DeltaLabAPIError) as exc_info:
        await c._dl_request("GET", "/x", soft_not_found=True)
    assert exc_info.value.code == "invalid_parameter"


@pytest.mark.asyncio
async def test_dl_request_falls_back_when_body_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url(monkeypatch)
    c = DeltaLabClient()
    err_resp = _ErrResponse(None, status=502)
    err_resp.text = "bad gateway"
    c._authed_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "502", request=httpx.Request("GET", "/"), response=err_resp
        )  # type: ignore[arg-type]
    )

    with pytest.raises(DeltaLabAPIError) as exc_info:
        await c._dl_request("GET", "/x")
    assert exc_info.value.code == "http_error"
    assert exc_info.value.status == 502


# ---------- _unwrap_items / _to_df ----------


def test_unwrap_items_handles_envelope_list_and_none() -> None:
    assert DeltaLabClient._unwrap_items({"items": [{"a": 1}], "count": 1}) == [{"a": 1}]
    assert DeltaLabClient._unwrap_items([{"a": 1}]) == [{"a": 1}]
    assert DeltaLabClient._unwrap_items(None) == []
    assert DeltaLabClient._unwrap_items({"unexpected": "shape"}) == []


def test_to_df_sets_datetime_index() -> None:
    rows = [
        {"ts": "2026-04-22T20:00:00+00:00", "price_usd": 2400.0},
        {"ts": "2026-04-22T21:00:00+00:00", "price_usd": 2410.0},
    ]
    df = DeltaLabClient._to_df(rows)
    assert list(df.columns) == ["price_usd"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert len(df) == 2


def test_to_df_empty_rows_returns_empty_dataframe() -> None:
    df = DeltaLabClient._to_df([])
    assert df.empty


# ---------- Typed records ----------


def test_price_latest_parses_raw_and_stats() -> None:
    data = {
        "asset_id": 2,
        "asof_ts": "2026-04-24T18:00:00+00:00",
        "price_usd": 2318.7,
        "ret_1d": 0.003,
        "ret_7d": -0.047,
        "ret_30d": 0.07,
        "ret_90d": -0.2,
        "vol_7d": 0.38,
        "vol_30d": 0.53,
        "vol_90d": 0.69,
        "mdd_30d": -0.35,
        "mdd_90d": -0.4,
    }
    pl = PriceLatest.from_dict(data)
    assert pl.asset_id == 2
    assert pl.price_usd == 2318.7
    assert pl.ret_7d == -0.047
    assert pl.raw is data


def test_lending_latest_parses_subset_and_preserves_raw() -> None:
    data = {
        "market_id": 912,
        "asset_id": 2,
        "asof_ts": "2026-04-24T18:00:00+00:00",
        "venue_id": 7,
        "venue_name": "aave-bsc",
        "net_supply_apr_now": 0.008,
        "net_borrow_apr_now": 0.017,
        "util_now": 0.55,
        "combined_net_supply_apr_now": 0.008,
        "net_supply_mean_7d": 0.01,
    }
    ll = LendingLatest.from_dict(data)
    assert ll.market_id == 912 and ll.asset_id == 2
    assert ll.venue_name == "aave-bsc"
    assert ll.net_supply_apr_now == 0.008
    # forward-compat fields still reachable through raw
    assert ll.raw["net_supply_mean_7d"] == 0.01
    assert ll.raw["combined_net_supply_apr_now"] == 0.008


def test_asset_info_parses_minimal_and_full() -> None:
    minimal = AssetInfo.from_dict({"asset_id": 7, "symbol": "ETH"})
    assert minimal.symbol == "ETH" and minimal.chain_id is None
    full = AssetInfo.from_dict(
        {
            "asset_id": 2,
            "symbol": "ETH",
            "name": "Ether",
            "decimals": 18,
            "chain_id": 1,
            "address": "0x0",
            "coingecko_id": "ethereum",
            "source": "coingecko",
        }
    )
    assert full.coingecko_id == "ethereum"


def test_venue_market_instrument_records() -> None:
    v = VenueInfo.from_dict(
        {
            "venue_id": 7,
            "name": "aave-bsc",
            "venue_type": "LENDING",
            "chain_id": 56,
            "market_count": 1,
            "extra": {},
        }
    )
    assert v.venue_type == "LENDING"
    m = MarketInfo.from_dict(
        {
            "market_id": 912,
            "venue": "aave-bsc",
            "venue_id": 7,
            "venue_type": "LENDING",
            "market_type": "LENDING",
            "external_id": "0xabc",
            "chain_id": 56,
            "is_listed": True,
            "extra": {},
        }
    )
    assert m.market_id == 912
    i = InstrumentInfo.from_dict(
        {
            "instrument_id": 37459,
            "venue": "boros",
            "chain_id": 42161,
            "market_id": 18900,
            "base_symbol": "WETH",
            "base_asset_id": 1263,
            "quote_asset_id": 2,
            "maturity_ts": "2026-06-26T00:00:00+00:00",
            "extra": {},
        }
    )
    assert i.instrument_id == 37459 and i.base_symbol == "WETH"
    assert i.maturity_ts is not None and i.maturity_ts.year == 2026


def test_yield_funding_boros_pendle_latest() -> None:
    y = YieldLatest.from_dict(
        {
            "underlying_asset_id": 2,
            "ts": "2026-04-22T20:00:00+00:00",
            "apy_base": 0.026,
            "exchange_rate": 1.0,
        }
    )
    assert y.asset_id == 2 and y.apy_base == 0.026

    f = FundingLatest.from_dict(
        {
            "instrument_id": 100,
            "asof_ts": "2026-04-22T20:00:00+00:00",
            "venue": "hyperliquid",
            "funding_rate": 0.0001,
            "mark_price_usd": 2400.0,
            "oi_usd": 10000.0,
            "volume_usd": 50000.0,
        }
    )
    assert f.instrument_id == 100 and f.funding_rate == 0.0001

    b = BorosLatest.from_dict(
        {
            "market_id": 18900,
            "asof_ts": "2026-04-24T18:00:00+00:00",
            "pv": 16.5,
            "fixed_rate_mark": 0.48,
            "floating_rate_oracle": None,
        }
    )
    assert b.pv == 16.5 and b.fixed_rate_mark == 0.48

    p = PendleLatest.from_dict(
        {"market_id": 42, "asof_ts": "2026-04-24T18:00:00+00:00"}
    )
    assert p.market_id == 42


def test_delta_lab_api_error_repr_preserves_code_and_url() -> None:
    err = DeltaLabAPIError(
        "bulk_cap_exceeded", "max 100", status=400, url="http://x/y/"
    )
    s = str(err)
    assert "bulk_cap_exceeded" in s and "max 100" in s and "http://x/y/" in s
    assert err.status == 400

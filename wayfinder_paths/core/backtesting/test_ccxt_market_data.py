from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from wayfinder_paths.core.backtesting import data


class _FakeBinance:
    def __init__(self) -> None:
        self.urls = {
            "api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
            }
        }
        self.public_url_at_fetch: str | None = None

    async def fetch_ohlcv(
        self,
        pair: str,
        interval: str,
        *,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        self.public_url_at_fetch = self.urls["api"]["public"]
        return [[since, 100.0, 101.0, 99.0, 100.5, 1.0]]


class _FakeAdapter:
    instance: _FakeAdapter | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.binance = _FakeBinance()
        self.exchanges = kwargs.get("exchanges")
        self.closed = False
        type(self).instance = self

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_ccxt_prices_use_binance_market_data_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(data, "CCXTAdapter", _FakeAdapter)
    start = datetime(2026, 8, 1, tzinfo=UTC)

    result = await data._fetch_prices_ccxt(
        ["BTC"],
        start,
        start + timedelta(minutes=5),
        "5m",
    )

    adapter = _FakeAdapter.instance
    assert adapter is not None
    assert adapter.exchanges == {
        "binance": {"options": {"fetchMarkets": {"types": ["spot"]}}}
    }
    assert (
        adapter.binance.public_url_at_fetch == "https://data-api.binance.vision/api/v3"
    )
    assert adapter.binance.urls["api"]["private"] == ("https://api.binance.com/api/v3")
    assert adapter.closed is True
    assert result.iloc[0]["BTC"] == 100.5

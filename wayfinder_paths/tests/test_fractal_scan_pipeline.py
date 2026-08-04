from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.quant import fractal_scan_pipeline as pipeline
from wayfinder_paths.quant.fractal_scan_context import (
    create_fractal_scan_request,
)

INTERVAL_MS = 5 * 60_000


def _rows(count: int, *, scale: float = 1.0) -> list[dict[str, Any]]:
    shape = [100, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115]
    closes = [shape[index % len(shape)] * scale for index in range(count)]
    return [
        {
            "t": index * INTERVAL_MS,
            "o": close,
            "h": close * 1.01,
            "l": close * 0.99,
            "c": close,
            "v": 1000 + index,
        }
        for index, close in enumerate(closes)
    ]


def _request(
    kind: str = "hyperliquid", *, start_bar: int = 84
) -> pipeline.FractalScanRequest:
    common = {
        "kind": kind,
        "interval": "5m",
        "start_ms": start_bar * INTERVAL_MS,
        "end_ms": (start_bar + 11) * INTERVAL_MS,
        "display_symbol": "BTC",
        "market_id": "hl-perp-btc",
        "chart_id": "hl-perp-btc",
        "selected_price_min": 99.0,
        "selected_price_max": 116.0,
    }
    if kind == "hyperliquid":
        return create_fractal_scan_request(**common, hl_coin="BTC")
    return create_fractal_scan_request(
        **common,
        chain_id=8453,
        token_address="0x1111111111111111111111111111111111111111",
    )


@pytest.fixture(autouse=True)
def clear_scan_cache() -> None:
    pipeline._clear_fractal_scan_cache()


async def test_scan_returns_exact_baseline_and_reuses_cached_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def get_candles_response(
        coin: str, start_ms: int, end_ms: int, interval: str
    ) -> dict[str, Any]:
        calls.append(coin)
        return {"rows": _rows(500)}

    monkeypatch.setattr(
        pipeline.HYPERLIQUID_DATA_CLIENT,
        "get_candles_response",
        get_candles_response,
    )
    exact = await pipeline.run_fractal_scan(
        request=_request(start_bar=484),
        now_ms=510 * INTERVAL_MS,
    )
    assert calls == ["BTC"]
    assert exact["coverage"]["source"] == "hyperliquid"
    assert exact["evidence"]["same_market_samples"] == len(exact["matches"])
    assert exact["matches"]
    assert set(exact["outcome_distributions"]) == {"1h", "4h", "12h", "24h"}
    assert "scope_used" not in exact
    assert "view_data" not in exact

    cached = await pipeline.run_fractal_scan(
        request=_request(start_bar=484),
        now_ms=510 * INTERVAL_MS,
    )
    assert calls.count("BTC") == 1
    assert cached["scan_id"] == exact["scan_id"]


async def test_onchain_history_pages_before_selected_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows(100)
    calls: list[int | None] = []

    async def get_candles(
        coin: str,
        interval: str,
        *,
        chain_id: int,
        before_timestamp: int | None = None,
    ) -> dict[str, Any]:
        calls.append(before_timestamp)
        return {"rows": rows[50:] if len(calls) == 1 else rows[:50]}

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_fractal_scan(
        request=_request("onchain"),
        now_ms=110 * INTERVAL_MS,
    )

    assert len(calls) == 2
    assert calls[0] == 96 * INTERVAL_MS // 1000
    assert calls[1] == 50 * INTERVAL_MS // 1000 - 1
    assert result["coverage"]["actual_bars"] == 12
    assert result["coverage"]["source"] == "coingecko_onchain:8453"


async def test_onchain_history_recovers_from_empty_bounded_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows(100)
    calls: list[int | None] = []

    async def get_candles(
        coin: str,
        interval: str,
        *,
        chain_id: int,
        before_timestamp: int | None = None,
    ) -> dict[str, Any]:
        calls.append(before_timestamp)
        pages = ([], rows[50:], rows[:50])
        return {"rows": pages[len(calls) - 1]}

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_fractal_scan(
        request=_request("onchain"),
        now_ms=110 * INTERVAL_MS,
    )

    assert calls == [96 * INTERVAL_MS // 1000, None, 50 * INTERVAL_MS // 1000 - 1]
    assert result["coverage"]["actual_bars"] == 12


async def test_onchain_history_stops_when_a_page_makes_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows(100)
    calls: list[int | None] = []

    async def get_candles(
        coin: str,
        interval: str,
        *,
        chain_id: int,
        before_timestamp: int | None = None,
    ) -> dict[str, Any]:
        calls.append(before_timestamp)
        return {"rows": rows}

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_fractal_scan(
        request=_request("onchain"),
        now_ms=110 * INTERVAL_MS,
    )

    assert len(calls) == 2
    assert result["coverage"]["history_bars"] == 100


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("1m", "1m"),
        ("3m", "5m"),
        ("5m", "5m"),
        ("15m", "15m"),
        ("30m", "1h"),
        ("1h", "1h"),
        ("2h", "4h"),
        ("4h", "4h"),
        ("8h", "1d"),
        ("12h", "1d"),
        ("1d", "1d"),
    ],
)
def test_scan_window_uses_a_supported_interval(requested: str, expected: str) -> None:
    expected_ms = pipeline.INTERVAL_MS[expected]
    request = create_fractal_scan_request(
        kind="hyperliquid",
        interval=requested,
        start_ms=10 * expected_ms,
        end_ms=21 * expected_ms,
        display_symbol="BTC",
        market_id="hl-perp-btc",
        chart_id="hl-perp-btc",
        hl_coin="BTC",
    )

    window = pipeline._scan_window(request, now_ms=100 * 86_400_000)

    assert window.interval == expected


def test_forward_horizons_are_wall_clock_based() -> None:
    assert pipeline._forward_horizon_bars(5 * 60_000) == {
        "1h": 12,
        "4h": 48,
        "12h": 144,
        "24h": 288,
    }
    assert pipeline._forward_horizon_bars(4 * 60 * 60_000) == {
        "1h": 1,
        "4h": 1,
        "12h": 3,
        "24h": 6,
    }


def test_request_requires_exact_market_identity() -> None:
    with pytest.raises(ValueError, match="exact EVM contract"):
        create_fractal_scan_request(
            kind="onchain",
            interval="5m",
            start_ms=0,
            end_ms=20 * INTERVAL_MS,
            display_symbol="TOKEN",
            market_id="onchain-token",
            chart_id="onchain-token",
            chain_id=8453,
            token_address="TOKEN",
        )

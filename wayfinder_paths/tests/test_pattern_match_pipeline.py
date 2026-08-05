from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.core.perps.ccxt_history import CcxtPerpHistory
from wayfinder_paths.quant import pattern_match_pipeline as pipeline
from wayfinder_paths.quant.pattern_match_context import (
    create_pattern_match_request,
)

INTERVAL_MS = 5 * 60_000


def _rows(
    count: int,
    *,
    scale: float = 1.0,
    interval_ms: int = INTERVAL_MS,
) -> list[dict[str, Any]]:
    shape = [100, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115]
    closes = [shape[index % len(shape)] * scale for index in range(count)]
    return [
        {
            "t": index * interval_ms,
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
) -> pipeline.PatternMatchRequest:
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
        return create_pattern_match_request(**common, hl_coin="BTC")
    return create_pattern_match_request(
        **common,
        chain_id=8453,
        token_address="0x1111111111111111111111111111111111111111",
    )


@pytest.fixture(autouse=True)
def clear_match_cache() -> None:
    pipeline._clear_pattern_match_cache()


async def test_match_returns_exact_baseline_and_reuses_cached_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def get_candles(
        coin: str, start_ms: int, end_ms: int, interval: str
    ) -> list[dict[str, Any]]:
        calls.append(coin)
        return _rows(500)

    monkeypatch.setattr(
        pipeline.HYPERLIQUID_DATA_CLIENT,
        "get_candles",
        get_candles,
    )
    exact = await pipeline.run_pattern_match(
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

    cached = await pipeline.run_pattern_match(
        request=_request(start_bar=484),
        now_ms=510 * INTERVAL_MS,
    )
    assert calls.count("BTC") == 1
    assert cached["match_id"] == exact["match_id"]


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
    ) -> list[dict[str, Any]]:
        calls.append(before_timestamp)
        return rows[50:] if len(calls) == 1 else rows[:50]

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_pattern_match(
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
    ) -> list[dict[str, Any]]:
        calls.append(before_timestamp)
        pages = ([], rows[50:], rows[:50])
        return pages[len(calls) - 1]

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_pattern_match(
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
    ) -> list[dict[str, Any]]:
        calls.append(before_timestamp)
        return rows

    monkeypatch.setattr(pipeline.TOKEN_CLIENT, "get_candles", get_candles)
    result = await pipeline.run_pattern_match(
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
def test_match_window_uses_a_supported_interval(requested: str, expected: str) -> None:
    expected_ms = pipeline.INTERVAL_MS[expected]
    request = create_pattern_match_request(
        kind="hyperliquid",
        interval=requested,
        start_ms=10 * expected_ms,
        end_ms=21 * expected_ms,
        display_symbol="BTC",
        market_id="hl-perp-btc",
        chart_id="hl-perp-btc",
        hl_coin="BTC",
    )

    window = pipeline._match_window(request, now_ms=100 * 86_400_000)

    assert window.interval == expected


def test_forward_horizons_are_wall_clock_based() -> None:
    assert pipeline._forward_horizon_bars(5 * 60_000) == {
        "1h": 12,
        "4h": 48,
        "12h": 144,
        "24h": 288,
    }
    assert pipeline._forward_horizon_bars(4 * 60 * 60_000) == {
        "4h": 1,
        "12h": 3,
        "24h": 6,
    }
    assert pipeline._suppressed_forward_horizons(4 * 60 * 60_000) == ["1h"]


@pytest.mark.parametrize(
    ("interval", "bars"),
    [("15m", 200), ("1h", 136), ("1h", 256)],
)
def test_match_window_preserves_requested_interval_through_256_bars(
    interval: str, bars: int
) -> None:
    interval_ms = pipeline.INTERVAL_MS[interval]
    request = create_pattern_match_request(
        kind="hyperliquid",
        interval=interval,
        start_ms=10 * interval_ms,
        end_ms=(10 + bars - 1) * interval_ms,
        display_symbol="SOL",
        market_id="hl-perp-sol",
        chart_id="hl-perp-sol",
        hl_coin="SOL",
    )

    window = pipeline._match_window(request, now_ms=400 * 86_400_000)

    assert window.interval == interval


@pytest.mark.parametrize(
    ("value", "expected"),
    [("btc/usdt", "BTC"), ("WETH-USDC", "ETH"), ("wbtc", "BTC")],
)
def test_ccxt_symbol_reuses_canonical_symbol_normalization(
    value: str, expected: str
) -> None:
    assert pipeline._normalize_ccxt_symbol(value) == expected


@pytest.mark.parametrize(
    ("expected_interval", "interval_ms", "expected_horizons"),
    [
        (
            "5m",
            5 * 60_000,
            {"1h": 12, "4h": 48, "12h": 144, "24h": 288},
        ),
        (
            "15m",
            15 * 60_000,
            {"1h": 4, "4h": 16, "12h": 48, "24h": 96},
        ),
    ],
)
async def test_ccxt_proxy_reuses_exact_match_at_selected_timeframe(
    monkeypatch: pytest.MonkeyPatch,
    expected_interval: str,
    interval_ms: int,
    expected_horizons: dict[str, int],
) -> None:
    exact_calls = 0
    ccxt_calls = 0
    rows = _rows(600, interval_ms=interval_ms)

    async def get_candles(
        coin: str, start_ms: int, end_ms: int, requested_interval: str
    ) -> list[dict[str, Any]]:
        nonlocal exact_calls
        exact_calls += 1
        assert requested_interval == expected_interval
        return rows

    async def fetch_perp_history(
        base_symbol: str,
        requested_interval: str,
        *,
        interval_ms: int,
        start_ms: int,
        end_ms: int,
        exchange_ids: tuple[str, ...] = ("okx", "bitget", "gate"),
    ) -> CcxtPerpHistory:
        nonlocal ccxt_calls
        ccxt_calls += 1
        assert base_symbol == "BTC"
        assert requested_interval == expected_interval
        return CcxtPerpHistory(
            exchange_id="okx",
            market_symbol="BTC/USDT:USDT",
            rows=rows,
            failures=(),
        )

    monkeypatch.setattr(
        pipeline.HYPERLIQUID_DATA_CLIENT,
        "get_candles",
        get_candles,
    )
    monkeypatch.setattr(
        "wayfinder_paths.core.perps.ccxt_history.fetch_ccxt_perp_history",
        fetch_perp_history,
    )
    request = create_pattern_match_request(
        kind="hyperliquid",
        interval=expected_interval,
        start_ms=584 * interval_ms,
        end_ms=595 * interval_ms,
        display_symbol="WBTC",
        market_id="hl-perp-btc",
        chart_id="hl-perp-btc",
        hl_coin="BTC",
    )
    exact = await pipeline.run_pattern_match(
        request=request,
        now_ms=610 * interval_ms,
    )
    proxy = await pipeline.run_pattern_match_ccxt_proxy(
        match_id=exact["match_id"],
        symbol="WBTC",
    )
    cached_proxy = await pipeline.run_pattern_match_ccxt_proxy(
        match_id=exact["match_id"],
        symbol="WBTC",
    )

    assert exact_calls == 1
    assert ccxt_calls == 1
    assert proxy["proxy"] == {
        "symbol": "BTC",
        "source": "ccxt:okx:swap",
        "interval": expected_interval,
        "exchange": "okx",
        "market_symbol": "BTC/USDT:USDT",
        "market_type": "swap",
    }
    assert {
        label: details["bars"] for label, details in proxy["forward_horizons"].items()
    } == expected_horizons
    assert proxy["matches"]
    assert {match["match_scope"] for match in proxy["matches"]} == {"same_asset_proxy"}
    assert proxy["forward_path_distribution"]["samples"] == len(proxy["matches"])
    assert proxy["visual_spec"]["operation"] == "upsert_overlay"
    assert len(proxy["visual_spec"]["overlay"]["series"]) == 2
    assert cached_proxy == proxy


def test_request_requires_exact_market_identity() -> None:
    with pytest.raises(ValueError, match="exact EVM contract"):
        create_pattern_match_request(
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

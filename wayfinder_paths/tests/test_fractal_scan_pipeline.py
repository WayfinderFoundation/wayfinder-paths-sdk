from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.quant import fractal_scan_pipeline as pipeline
from wayfinder_paths.quant.fractal_scan_context import create_fractal_scan_request

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


def _request(kind: str = "hyperliquid") -> pipeline.FractalScanRequest:
    common = {
        "kind": kind,
        "interval": "5m",
        "start_ms": 84 * INTERVAL_MS,
        "end_ms": 95 * INTERVAL_MS,
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


async def test_adaptive_followup_reuses_exact_history_and_labels_fuzzy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def get_candles_response(
        coin: str, start_ms: int, end_ms: int, interval: str
    ) -> dict[str, Any]:
        calls.append(coin)
        scale = 1.0 if coin == "BTC" else 1.0 + len(calls) / 10
        return {"rows": _rows(100, scale=scale)}

    monkeypatch.setattr(
        pipeline.HYPERLIQUID_DATA_CLIENT,
        "get_candles_response",
        get_candles_response,
    )
    exact = await pipeline.run_fractal_scan(
        request=_request(),
        scope="same_market",
        now_ms=110 * INTERVAL_MS,
    )
    assert calls == ["BTC"]
    assert exact["scope_used"] == "same_market"
    assert {match["match_scope"] for match in exact["matches"]} == {"same_market"}

    adaptive = await pipeline.run_fractal_scan(
        scan_id=exact["scan_id"], scope="adaptive"
    )
    assert calls.count("BTC") == 1
    assert set(calls[1:]) == {"ETH", "SOL", "BNB", "XRP"}
    assert adaptive["scope_used"] == "adaptive"
    assert adaptive["evidence"]["fuzzy_samples"] > 0
    assert "fuzzy_analogues_included" in adaptive["warnings"]
    assert adaptive["confidence"] != "high"
    assert adaptive["view_data"]["forward_fan"] is not None


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
        scope="same_market",
        now_ms=110 * INTERVAL_MS,
    )

    assert len(calls) == 2
    assert calls[0] == 96 * INTERVAL_MS // 1000
    assert calls[1] == 50 * INTERVAL_MS // 1000 - 1
    assert result["coverage"]["actual_bars"] == 12
    assert result["coverage"]["sources"] == ["coingecko_onchain:8453"]


async def test_unknown_followup_scan_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown or expired"):
        await pipeline.run_fractal_scan(scan_id="missing", scope="adaptive")


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

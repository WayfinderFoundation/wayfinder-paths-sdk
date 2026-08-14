from __future__ import annotations

import numpy as np
import pytest

from wayfinder_paths.quant.pattern_match import PriceSeries, find_price_analogs


def _series(
    symbol: str,
    source: str,
    closes: list[float],
    *,
    start_ms: int = 0,
    interval_ms: int = 60_000,
) -> PriceSeries:
    return PriceSeries(
        symbol=symbol,
        source=source,
        timestamps_ms=[start_ms + index * interval_ms for index in range(len(closes))],
        closes=closes,
    )


def test_finds_non_overlapping_multi_source_matches_and_horizons() -> None:
    shape = [100, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115]
    history = shape + [116, 118] + [90] * 12 + shape + [120, 117]
    peer = [value * 2 for value in shape] + [234, 240] + [180] * 20

    result = find_price_analogs(
        _series("BTC", "hyperliquid", shape, start_ms=10_000_000),
        [
            _series("BTC", "hyperliquid", history),
            _series("ETH", "binance", peer),
        ],
        horizons=(1, 2),
        top=3,
    )

    assert len(result["matches"]) == 3
    assert {row["source"] for row in result["matches"]} == {"hyperliquid", "binance"}
    assert set(result["outcome_distributions"]) == {"1_bar", "2_bar"}
    assert result["outcome_distributions"]["1_bar"]["samples"] == 3
    assert result["pattern"]["shape_path_bps"][0] == 0.0


def test_excludes_the_live_pattern_window_from_same_market() -> None:
    closes = list(np.linspace(100, 200, 40))
    timestamps = [index * 60_000 for index in range(len(closes))]
    history = PriceSeries("BTC", "hyperliquid", timestamps, closes)
    pattern = PriceSeries("BTC", "hyperliquid", timestamps[-12:], closes[-12:])

    result = find_price_analogs(pattern, [history], horizons=(1,), top=20)

    assert all(match["end_ms"] < timestamps[-12] for match in result["matches"])


def test_returns_empty_distributions_when_history_is_insufficient() -> None:
    pattern = _series("BTC", "hyperliquid", list(range(100, 112)))
    history = _series("BTC", "hyperliquid", list(range(100, 113)), start_ms=1_000_000)

    result = find_price_analogs(pattern, [history], horizons=(1, 3), top=5)

    assert result["matches"] == []
    assert result["outcome_distributions"]["3_bar"]["samples"] == 0


def test_ranking_accounts_for_magnitude_and_volatility() -> None:
    log_shape = np.asarray(
        [0.0, 0.01, 0.025, 0.015, 0.04, 0.055, 0.045, 0.07, 0.09, 0.08, 0.11, 0.13]
    )
    pattern = (100 * np.exp(log_shape)).tolist()
    same_scale = (200 * np.exp(log_shape)).tolist() + [230, 232]
    double_scale = (100 * np.exp(log_shape * 2)).tolist() + [132, 134]

    result = find_price_analogs(
        _series("SOL", "hyperliquid", pattern, start_ms=10_000_000),
        [
            _series("SOL", "same-scale", same_scale),
            _series("SOL", "double-scale", double_scale),
        ],
        horizons=(1, 2),
        top=2,
    )

    assert result["matches"][0]["source"] == "same-scale"
    assert (
        result["matches"][0]["similarity_score"]
        > result["matches"][1]["similarity_score"]
    )
    assert result["matches"][0]["magnitude_similarity"] == pytest.approx(1.0)
    assert result["matches"][1]["magnitude_similarity"] == pytest.approx(0.5, abs=0.01)


def test_forward_distribution_uses_all_matches_but_exposes_three_paths() -> None:
    shape = [100, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115]
    histories = [
        _series(
            "SOL",
            f"venue-{index}",
            [value * (index + 1) for value in shape] + [116 + index, 117 + index],
        )
        for index in range(5)
    ]

    result = find_price_analogs(
        _series("SOL", "hyperliquid", shape, start_ms=10_000_000),
        histories,
        horizons=(1, 2),
        top=5,
        forward_paths=3,
    )

    assert result["forward_path_distribution"]["samples"] == 5
    assert sum("forward_path_bps" in match for match in result["matches"]) == 3


@pytest.mark.parametrize(
    "closes, message",
    [
        ([100.0] * 12, "zero price variance"),
        (list(range(100, 111)), "at least 12 bars"),
    ],
)
def test_rejects_unusable_patterns(closes: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        find_price_analogs(
            _series("BTC", "hyperliquid", closes),
            [_series("BTC", "hyperliquid", closes + [101, 102, 103])],
        )

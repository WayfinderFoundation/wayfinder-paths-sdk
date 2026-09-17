from __future__ import annotations

import numpy as np
import pytest

from wayfinder_paths.quant.pattern_forecast import (
    OhlcSeries,
    PatternForecastConfig,
    forecast_price_analogs,
)


def _series(*, source: str = "spot", count: int = 1_500) -> OhlcSeries:
    index = np.arange(count)
    close = 100 * np.exp(0.0002 * index + 0.02 * np.sin(2 * np.pi * index / 96))
    open_ = close * np.exp(-0.0001)
    return OhlcSeries(
        symbol="TEST",
        source=source,
        timestamps_ms=index * 900_000,
        opens=open_,
        highs=np.maximum(open_, close) * 1.002,
        lows=np.minimum(open_, close) * 0.998,
        closes=close,
    )


def _config() -> PatternForecastConfig:
    return PatternForecastConfig(
        pattern_bars=24,
        horizons=(2, 4, 8, 12),
        history_limit=1_400,
        top_matches=20,
        display_matches=5,
        range_horizon_bars=12,
    )


def test_forecast_is_deterministic_and_exposes_distribution() -> None:
    first = forecast_price_analogs(_series(), config=_config())
    second = forecast_price_analogs(_series(), config=_config())

    assert first == second
    assert first.match_count == 20
    assert len(first.median_path_bps) == 13
    assert sum(match.forward_path_bps is not None for match in first.matches) == 5
    assert 0 <= first.probability_up <= 1
    assert first.range_q25 <= first.range_q50 <= first.range_q75


def test_same_forecast_contract_supports_spot_and_perps() -> None:
    spot = forecast_price_analogs(_series(source="spot"), config=_config())
    perp = forecast_price_analogs(_series(source="perp"), config=_config())

    assert spot.probability_up == perp.probability_up
    assert spot.endpoint_q50 == perp.endpoint_q50


def test_rejects_gaps_and_invalid_ohlc() -> None:
    series = _series()
    timestamps = list(series.timestamps_ms)
    timestamps[100] += 1
    with pytest.raises(ValueError, match="consecutive"):
        forecast_price_analogs(
            OhlcSeries(
                series.symbol,
                series.source,
                timestamps,
                series.opens,
                series.highs,
                series.lows,
                series.closes,
            ),
            config=_config(),
        )

    with pytest.raises(ValueError, match="high"):
        forecast_price_analogs(
            OhlcSeries("TEST", "spot", [0], [2], [1], [1], [2]),
            config=_config(),
        )

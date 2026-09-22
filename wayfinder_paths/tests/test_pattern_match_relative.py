import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant import pattern_match_relative as relative
from wayfinder_paths.quant.pattern_match_positioning import PRICE_COLUMNS


def candles(count: int = 2500) -> tuple[pd.DataFrame, pd.DataFrame]:
    times = pd.date_range("2026-03-15T00:15Z", periods=count, freq="15min")
    t = np.arange(count)
    hedge = 100 * np.exp(0.03 * np.sin(t / 51) + 0.00001 * t)
    target = 100 * np.exp(0.06 * np.sin(t / 51) + 0.005 * np.sin(t / 7) + 0.00002 * t)
    return (
        pd.DataFrame({"timestamp": times, "close": target}),
        pd.DataFrame({"timestamp": times, "close": hedge}),
    )


def test_beta_uses_past_returns_and_prior_bar_beta_builds_the_index() -> None:
    bars, hedge = candles()
    state = relative.relative_state(bars, hedge)
    warmup = relative.BETA_BARS + relative.RMS_BARS
    assert state.residual_rms.iloc[:warmup].isna().all()
    assert state.residual_rms.iloc[warmup:].notna().all()
    index = 2300
    a, b = np.log(bars.close).diff(), np.log(hedge.close).diff()
    window = slice(index - relative.BETA_BARS + 1, index + 1)
    expected = a.iloc[window].cov(b.iloc[window]) / b.iloc[window].var()
    assert state.beta.iloc[index] == pytest.approx(np.clip(expected, -3, 3))
    assert np.log(state.relative_index).diff().iloc[index] == pytest.approx(
        a.iloc[index] - state.beta.iloc[index - 1] * b.iloc[index]
    )


def test_price_features_ignore_future_data_and_keep_research_geometry() -> None:
    bars, hedge = candles()
    features = relative.relative_price_features(bars, hedge)
    cutoff = 2300
    pd.testing.assert_frame_equal(
        features.iloc[:cutoff],
        relative.relative_price_features(bars.iloc[:cutoff], hedge.iloc[:cutoff]),
    )
    state = relative.relative_state(bars, hedge)
    log = np.log(state.relative_index.iloc[-96:].to_numpy())
    shape = (log - log.mean()) / max(log.std(), 1e-12)
    points = np.linspace(0, 95, 24).astype(int)
    np.testing.assert_allclose(
        features.iloc[-1][PRICE_COLUMNS[:24]].to_numpy(dtype=float),
        shape[points] * np.sqrt(0.65 / 24),
    )
    assert features.log_range.iloc[-1] == pytest.approx(
        np.log(log.max() - log.min()) * np.sqrt(0.20)
    )
    assert features.log_variation.iloc[-1] == pytest.approx(
        np.log(np.sqrt(np.square(np.diff(log)).sum())) * np.sqrt(0.15)
    )
    assert features.scale.iloc[-1] == pytest.approx(
        state.residual_rms.iloc[-1] * np.sqrt(96) / (1 + abs(state.beta.iloc[-1]))
    )
    bars.loc[cutoff:, "close"] *= 5
    hedge.loc[cutoff:, "close"] *= 2
    pd.testing.assert_frame_equal(
        relative.relative_price_features(bars, hedge).iloc[:cutoff],
        features.iloc[:cutoff],
    )


@pytest.mark.parametrize("count", [0, 20, 96, 2100])
def test_short_or_flat_history_is_unknown_not_a_tradable_forecast(count: int) -> None:
    bars, hedge = candles(count)
    bars["close"] = 100.0
    hedge["close"] = 100.0
    result = relative.relative_price_features(bars, hedge)
    assert len(result) == count
    assert result.scale.isna().all()
    assert result.log_range.isna().all()
    assert result.log_variation.isna().all()


@pytest.mark.parametrize("invalid", ["misaligned", "missing", "zero", "nan"])
def test_bad_price_data_is_not_forward_filled(invalid: str) -> None:
    bars, hedge = candles()
    if invalid == "misaligned":
        hedge.timestamp += pd.Timedelta(minutes=15)
    elif invalid == "missing":
        bars, hedge = bars.drop(index=2), hedge.drop(index=2)
    else:
        bars.loc[2, "close"] = 0 if invalid == "zero" else np.nan
    with pytest.raises(ValueError):
        relative.relative_price_features(bars, hedge)

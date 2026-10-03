from __future__ import annotations

import numpy as np
import pandas as pd

from wayfinder_paths.jobs import factor_model as fm


def _bars(symbols: int = 10, days: int = 90, freq: str = "1h", seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    stamps = pd.date_range("2026-03-01", periods=days * 24, freq=freq, tz="UTC")
    rows = []
    for i in range(symbols):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(stamps))))
        spread = np.abs(rng.normal(0, 0.004, len(stamps)))
        rows.append(
            pd.DataFrame(
                {
                    "timestamp": stamps,
                    "symbol": "BTC" if i == 0 else f"S{i}",
                    "open": close * (1 + rng.normal(0, 0.002, len(stamps))),
                    "high": close * (1 + spread),
                    "low": close * (1 - spread),
                    "close": close,
                    "volume": rng.uniform(1e3, 1e4, len(stamps)),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def _models(bars: pd.DataFrame) -> list[fm.RidgeFactorModel]:
    panel = fm.factor_panel(fm.resample_bars(bars))
    return fm.fit_walk_forward(
        panel, first_train_end=pd.Timestamp("2026-04-15", tz="UTC"), refit=pd.Timedelta(days=15), min_rows=500
    )


def test_scores_never_use_a_model_fitted_after_the_bar() -> None:
    bars = _bars()
    models = _models(bars)
    assert len(models) >= 2
    scored = fm.score_panel(fm.factor_panel(fm.resample_bars(bars)), models)
    assert scored["timestamp"].min() > models[0].train_end
    first = scored[scored["timestamp"] <= models[1].train_end]
    alone = fm.score_panel(fm.factor_panel(fm.resample_bars(bars)), models[:1])
    merged = first.merge(alone, on=["timestamp", "symbol"], suffixes=("", "_first"))
    np.testing.assert_allclose(merged[fm.FACTOR_PRED_FEATURE], merged[f"{fm.FACTOR_PRED_FEATURE}_first"])


def test_future_bars_do_not_change_past_scores() -> None:
    bars = _bars()
    models = _models(bars)
    cut = pd.Timestamp("2026-05-10", tz="UTC")
    full = fm.factor_feature_frames(bars, pd.DatetimeIndex(sorted(bars["timestamp"].unique())), models)
    shocked = bars.copy()
    later = shocked["timestamp"] > cut
    shocked.loc[later, ["open", "high", "low", "close"]] *= 3.0
    truncated = fm.factor_feature_frames(
        bars[~later], pd.DatetimeIndex(sorted(bars.loc[~later, "timestamp"].unique())), models
    )
    shocked_frames = fm.factor_feature_frames(
        shocked, pd.DatetimeIndex(sorted(bars["timestamp"].unique())), models
    )
    for name in (fm.FACTOR_PRED_FEATURE, fm.FACTOR_RANK_FEATURE):
        before = full[name].loc[:cut].dropna(how="all")
        assert len(before) > 100
        pd.testing.assert_frame_equal(before, truncated[name].loc[before.index], check_freq=False)
        pd.testing.assert_frame_equal(before, shocked_frames[name].loc[before.index], check_freq=False)


def test_a_partial_trailing_bin_is_dropped() -> None:
    bars = _bars(symbols=2, days=2)
    bars = bars[bars["timestamp"] <= pd.Timestamp("2026-03-02 10:00", tz="UTC")]
    resampled = fm.resample_bars(bars, "4h")
    assert resampled["timestamp"].max() == pd.Timestamp("2026-03-02 08:00", tz="UTC")


def test_small_universes_get_no_factor_feature() -> None:
    bars = _bars(symbols=4)
    assert fm.factor_feature_frames(bars, pd.DatetimeIndex(sorted(bars["timestamp"].unique()))) == {}


def test_rank_is_centred_and_rows_are_per_symbol() -> None:
    bars = _bars()
    index = pd.DatetimeIndex(sorted(bars["timestamp"].unique()))
    frames = fm.factor_feature_frames(bars, index, _models(bars))
    rank = frames[fm.FACTOR_RANK_FEATURE].dropna(how="all")
    assert rank.min().min() >= -0.5 and rank.max().max() <= 0.5
    rows = fm.factor_store_rows(frames, stamps=index[::12], written_at="t")
    assert {row["symbol"] for row in rows} == set(bars["symbol"].unique())
    assert {row["name"] for row in rows} == {fm.FACTOR_PRED_FEATURE, fm.FACTOR_RANK_FEATURE}


def test_artifacts_round_trip_and_the_shipped_set_loads(tmp_path) -> None:
    bars = _bars()
    models = _models(bars)
    path = tmp_path / "models.json"
    fm.save_models(models, path, provenance={"test": True})
    loaded = fm.load_models(path)
    panel = fm.factor_panel(fm.resample_bars(bars))
    np.testing.assert_allclose(loaded[-1].predict(panel), models[-1].predict(panel), atol=1e-9)
    shipped = fm.load_models()
    assert len(shipped) > 12
    assert all(a.train_end < b.train_end for a, b in zip(shipped, shipped[1:], strict=False))

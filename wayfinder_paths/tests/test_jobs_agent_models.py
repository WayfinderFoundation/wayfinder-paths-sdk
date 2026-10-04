from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from wayfinder_paths.jobs import agent_models as am


def _bars(symbols: int = 10, days: int = 150, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    stamps = pd.date_range("2026-01-01", periods=days * 24, freq="1h", tz="UTC")
    rows = []
    for i in range(symbols):
        close = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, len(stamps))))
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


def test_tree_nodes_score_exactly_like_sklearn() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(4000, 6))
    x[rng.random(x.shape) < 0.05] = np.nan
    y = (
        np.nan_to_num(x[:, 0])
        - 0.5 * np.nan_to_num(x[:, 1]) ** 2
        + rng.normal(0, 0.1, 4000)
    )
    estimator = HistGradientBoostingRegressor(
        max_depth=3, max_iter=50, min_samples_leaf=50
    ).fit(x, y)
    tree = am.TreeModel(
        features=[f"f{i}" for i in range(6)],
        baseline=float(np.ravel(estimator._baseline_prediction)[0]),
        trees=[
            {n: p[0].nodes[n].tolist() for n in am._NODE_FIELDS}
            for p in estimator._predictors
        ],
        train_end=pd.Timestamp("2026-01-01", tz="UTC"),
    )
    frame = pd.DataFrame(x, columns=tree.features)
    np.testing.assert_allclose(tree.predict(frame), estimator.predict(x), atol=1e-10)


@pytest.mark.parametrize("kind", ["ridge", "tree"])
def test_trained_models_round_trip_and_score_causally(tmp_path, kind) -> None:
    bars = _bars()
    model = am.train(
        bars,
        name="probe",
        kind=kind,
        features=["returns", "trend", "realized"],
        horizon="1d",
        discovery_end=pd.Timestamp("2026-05-01", tz="UTC"),
    )
    assert model.diagnostics["decisions"] > 0
    loaded = am.load(am.save(model, tmp_path))
    frames = {
        s: g.drop(columns="symbol").reset_index(drop=True)
        for s, g in bars.groupby("symbol")
    }
    full = am.model_scores(frames, loaded)
    cut = pd.Timestamp("2026-05-10", tz="UTC")
    early = {
        s: f[f["timestamp"] <= cut].reset_index(drop=True) for s, f in frames.items()
    }
    truncated = am.model_scores(early, loaded)
    for symbol, frame in early.items():
        rows = len(frame)
        pd.testing.assert_frame_equal(
            full[symbol].iloc[:rows], truncated[symbol], check_dtype=False
        )
        assert full[symbol]["model_rank"].dropna().between(-0.5, 0.5).all()
    first_scored = min(m.train_end for m in loaded.models)
    stamps = pd.to_datetime(frames["BTC"]["timestamp"], utc=True)
    assert full["BTC"].loc[stamps <= first_scored, "model_score"].isna().all()


def test_model_requests_are_bounded() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="unknown model input"):
        am.train(bars, name="x1", kind="ridge", features=["sentiment"])
    with pytest.raises(ValueError, match="unavailable here"):
        am.train(bars, name="x2", kind="ridge", features=["funding"])
    with pytest.raises(ValueError, match="kind"):
        am.train(bars, name="x3", kind="forest", features=["returns"])
    with pytest.raises(ValueError, match="symbols"):
        am.train(
            bars[bars["symbol"].isin(["BTC", "S1", "S2"])],
            name="x4",
            kind="ridge",
            features=["returns"],
        )
    with pytest.raises(ValueError, match="inputs"):
        am.train(
            bars,
            name="x5",
            kind="ridge",
            features=[
                "returns",
                "volatility",
                "trend",
                "cross_section",
                "market",
                "leader",
                "realized",
            ],
        )


def test_cached_scores_never_leak_across_datasets(tmp_path) -> None:
    bars = _bars()
    model = am.load(
        am.save(
            am.train(bars, name="probe", kind="ridge", features=["returns"]), tmp_path
        )
    )
    frames = {
        s: g.drop(columns="symbol").reset_index(drop=True)
        for s, g in bars.groupby("symbol")
    }
    other = _bars(seed=11)
    other_frames = {
        s: g.drop(columns="symbol").reset_index(drop=True)
        for s, g in other.groupby("symbol")
    }
    first = am.model_scores(frames, model)["S1"]["model_score"]
    second = am.model_scores(other_frames, model)["S1"]["model_score"]
    assert not np.allclose(first.dropna(), second.dropna())
    again = am.model_scores(frames, model)["S1"]["model_score"]
    pd.testing.assert_series_equal(first, again)


def test_a_window_shorter_than_one_4h_bar_scores_nothing(tmp_path) -> None:
    bars = _bars()
    model = am.load(
        am.save(
            am.train(bars, name="probe", kind="ridge", features=["returns"]), tmp_path
        )
    )
    first = bars["timestamp"].min()
    tiny = bars[bars["timestamp"] < first + pd.Timedelta(hours=2)]
    frames = {
        s: g.drop(columns="symbol").reset_index(drop=True)
        for s, g in tiny.groupby("symbol")
    }
    scores = am.model_scores(frames, model)
    assert all(frame["model_rank"].isna().all() for frame in scores.values())
    assert all(len(scores[s]) == len(frames[s]) for s in frames)

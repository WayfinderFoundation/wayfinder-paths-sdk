"""User-trained models as indicators: numpy-JSON format, joblib path, purity.

The no-deps path (LinearModel + fit_linear_from_frame + JSON artifacts) must
work with only numpy/pandas installed. sklearn/joblib tests skip when the
optional `ml` group is absent — except the missing-dep error message, which
runs everywhere via sys.modules poisoning so the no-deps UX stays covered.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.purity import PurityViolation, purity_sandbox
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies.models import (
    LinearModel,
    fit_linear_from_frame,
    load_model,
)
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes

# ---------------------------------------------------------------------------
# Pure-numpy training + JSON round-trip (no optional deps)
# ---------------------------------------------------------------------------


def _planted_frame(rows: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    x1 = rng.normal(0, 1, rows)
    x2 = rng.normal(0, 2, rows)
    noise = rng.normal(0, 0.01, rows)
    return pd.DataFrame(
        {"x1": x1, "x2": x2, "y": 3.0 * x1 - 1.5 * x2 + 0.25 + noise}
    )


def test_fit_linear_recovers_planted_coefficients() -> None:
    model = fit_linear_from_frame(
        _planted_frame(), ["x1", "x2"], "y", standardize=False
    )
    assert model.coef[0] == pytest.approx(3.0, abs=0.01)
    assert model.coef[1] == pytest.approx(-1.5, abs=0.01)
    assert model.intercept == pytest.approx(0.25, abs=0.01)


def test_standardized_fit_predicts_the_same_values() -> None:
    frame = _planted_frame()
    plain = fit_linear_from_frame(frame, ["x1", "x2"], "y", standardize=False)
    standardized = fit_linear_from_frame(frame, ["x1", "x2"], "y", standardize=True)
    X = frame[["x1", "x2"]].head(20)
    np.testing.assert_allclose(
        plain.predict(X), standardized.predict(X), rtol=1e-8
    )


def test_json_round_trip(tmp_path: Path) -> None:
    frame = _planted_frame()
    model = fit_linear_from_frame(
        frame, ["x1", "x2"], "y", out_path=tmp_path / "m.json"
    )
    restored = load_model(tmp_path / "m.json")
    assert isinstance(restored, LinearModel)
    assert restored.features == ["x1", "x2"]
    X = frame[["x1", "x2"]].head(10)
    np.testing.assert_allclose(model.predict(X), restored.predict(X))


def test_logistic_fit_bounds_and_separation() -> None:
    frame = _planted_frame()
    frame["label"] = (frame["y"] > frame["y"].median()).astype(float)
    # The label is near-separable from (x1, x2); l2 keeps IRLS coefficients
    # finite so probabilities stay strictly inside (0, 1).
    model = fit_linear_from_frame(
        frame, ["x1", "x2"], "label", kind="logistic", l2=1.0
    )
    probs = model.predict(frame[["x1", "x2"]])
    assert np.all((probs > 0) & (probs < 1))
    # Predictions must actually separate the classes.
    hit_rate = float(((probs > 0.5) == (frame["label"] == 1.0)).mean())
    assert hit_rate > 0.9


def test_deterministic_training() -> None:
    frame = _planted_frame()
    first = fit_linear_from_frame(frame, ["x1", "x2"], "y")
    second = fit_linear_from_frame(frame, ["x1", "x2"], "y")
    assert first.to_dict() == second.to_dict()


def test_predict_validates_feature_count() -> None:
    model = LinearModel(
        kind="linear", features=["a", "b"], coef=np.array([1.0, 2.0]), intercept=0.0
    )
    with pytest.raises(ValueError, match="expected 2 features"):
        model.predict(np.array([[1.0, 2.0, 3.0]]))


def test_unsupported_suffix_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported model artifact suffix"):
        load_model(tmp_path / "model.onnx")


def test_missing_joblib_error_mentions_ml_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "model.joblib").write_bytes(b"not-a-real-artifact")
    monkeypatch.setitem(sys.modules, "joblib", None)
    with pytest.raises(RuntimeError, match="poetry install --with ml"):
        load_model(tmp_path / "model.joblib")


# ---------------------------------------------------------------------------
# End-to-end: JSON model as indicator inside decide()
# ---------------------------------------------------------------------------

MODEL_STRATEGY = """
from wayfinder_paths.jobs.strategies.models import load_model

MODEL = load_model("models/momo.json", module_file=__file__)


def decide(ctx):
    frame = ctx.view.to_frame()
    closes = frame[frame["symbol"] == "SNX"]["close"].astype(float).tolist()
    if len(closes) < 2:
        return []
    momentum = closes[-1] / closes[-2] - 1.0
    score = float(MODEL.predict([[momentum]])[0])
    if "SNX" not in ctx.ledger.positions and score > 0.01:
        return [{"action": "OPEN", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "buy", "size": 5}]
    if "SNX" in ctx.ledger.positions and score < -0.01:
        return [{"action": "CLOSE", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "sell", "size": 5, "reduce_only": True}]
    return []
""".lstrip()


def _model_job(tmp_path: Path) -> Path:
    script = tmp_path / "workspace" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(MODEL_STRATEGY, encoding="utf-8")
    # score == momentum: identity model, threshold logic lives in the script.
    LinearModel(
        kind="linear", features=["momentum"], coef=np.array([1.0]), intercept=0.0
    ).save(tmp_path / "workspace" / "models" / "momo.json")
    return script


def _run_model_job(script: Path):
    closes = [10.0, 10.0, 10.5, 11.0, 11.2, 10.9, 10.5, 10.4, 10.4]
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return simulate_execution(
        script,
        PreparedExecutionDataset.from_rows(bars_from_closes(closes, symbol="SNX")),
        spec,
        {"initial_capital": 1000.0},
    )


def test_model_as_indicator_end_to_end(tmp_path: Path) -> None:
    script = _model_job(tmp_path)
    result = _run_model_job(script)
    assert result.validation["execution_valid"] is True
    # momentum +5% at bar2 -> OPEN fills bar3; -2.7% at bar5 -> CLOSE fills bar6
    fills = [f for f in result.trace["fills"] if f["status"] == "filled"]
    assert [f["reduce_only"] for f in fills] == [False, True]


def test_model_strategy_is_deterministic(tmp_path: Path) -> None:
    script = _model_job(tmp_path)
    first = _run_model_job(script)
    second = _run_model_job(script)
    assert first.stats == second.stats
    assert first.trace["fills"] == second.trace["fills"]
    assert first.equity_curve == second.equity_curve


# ---------------------------------------------------------------------------
# Purity sandbox: inference allowed, global numpy RNG blocked
# ---------------------------------------------------------------------------


def test_model_predict_passes_purity_sandbox() -> None:
    model = LinearModel(
        kind="logistic", features=["m"], coef=np.array([2.0]), intercept=-0.1
    )
    with purity_sandbox():
        out = model.predict(np.array([[0.5]]))
    assert 0 < float(out[0]) < 1


def test_np_random_raises_inside_sandbox_and_restores() -> None:
    with purity_sandbox():
        with pytest.raises(PurityViolation, match="np.random.rand"):
            np.random.rand(3)
        with pytest.raises(PurityViolation, match="np.random.random"):
            np.random.random(3)
        # Seeded generators stay allowed — deterministic by construction.
        assert np.random.default_rng(1).random() == np.random.default_rng(1).random()
    assert np.random.rand(3).shape == (3,)  # restored on exit
    assert np.random.random(3).shape == (3,)


def test_np_random_in_decide_fails_simulation() -> None:
    class RandomStrategy:
        def decide(self, ctx: Any) -> list[dict[str, Any]]:
            np.random.rand(1)
            return []

    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    with pytest.raises(PurityViolation, match="np.random.rand"):
        simulate_execution(
            lambda params: RandomStrategy(),
            PreparedExecutionDataset.from_rows(
                bars_from_closes([10.0, 10.1, 10.2], symbol="SNX")
            ),
            spec,
            {},
        )


# ---------------------------------------------------------------------------
# Optional sklearn/joblib path
# ---------------------------------------------------------------------------


def test_sklearn_joblib_round_trip(tmp_path: Path) -> None:
    sklearn_linear = pytest.importorskip("sklearn.linear_model")
    joblib = pytest.importorskip("joblib")
    frame = _planted_frame()
    estimator = sklearn_linear.LinearRegression()
    estimator.fit(frame[["x1", "x2"]], frame["y"])
    joblib.dump(estimator, tmp_path / "model.joblib")

    loaded = load_model(tmp_path / "model.joblib")
    X = frame[["x1", "x2"]].head(10)
    np.testing.assert_allclose(loaded.predict(X), estimator.predict(X))

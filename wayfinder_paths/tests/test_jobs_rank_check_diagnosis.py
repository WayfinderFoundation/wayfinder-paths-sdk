"""rank_ic fails loud on inputs where rank-IC is undefined by construction —
panel-wide (cross-sectionally constant) columns and pair-wise columns with a
self-hole — instead of the silent n=0 that wedged the BTC-exog lane."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.research import rank_ic

_N = 120


def _frame(values: np.ndarray) -> pd.DataFrame:
    ts = pd.date_range("2026-06-01", periods=_N, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"timestamp": ts, "col": values, "close": 100 + np.arange(_N) * 0.1}
    )


def test_panel_wide_column_fails_loud() -> None:
    shared = np.sin(np.arange(_N))  # varies in time, constant across symbols
    frames = {s: _frame(shared.copy()) for s in ("A", "B", "C", "D")}
    with pytest.raises(ValueError, match="cross-sectionally constant"):
        rank_ic(frames, "col")


def test_self_hole_column_fails_loud() -> None:
    rng = np.random.default_rng(3)
    frames = {s: _frame(rng.normal(size=_N)) for s in ("A", "B", "C", "D")}
    frames["A"]["col"] = np.nan  # pair-wise column's self-symbol hole
    with pytest.raises(ValueError, match="entirely NaN for \\['A'\\]"):
        rank_ic(frames, "col")


def test_varying_column_still_ranks() -> None:
    rng = np.random.default_rng(7)
    frames = {s: _frame(rng.normal(size=_N)) for s in ("A", "B", "C", "D")}
    result = rank_ic(frames, "col")
    assert result["horizons"][0]["n"] > 0


def test_basket_relative_column_derives_cross_sectionally(tmp_path) -> None:
    """The basket z-column must vary ACROSS symbols per bar (rankable)."""
    import json

    import yaml

    from wayfinder_paths.jobs.derived_features import derive_features_job
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("basket-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    symbols = ["A", "B", "C", "D"]
    job_yaml = {
        "id": job.id,
        "execution_spec": {"data_contract": {"bar_interval": "5m", "symbols": symbols}},
        "execution_params": {"symbols": symbols},
    }
    (root / "job.yaml").write_text(yaml.safe_dump(job_yaml), encoding="utf-8")
    ts = pd.date_range("2026-06-01", periods=200, freq="5min", tz="UTC")
    rng = np.random.default_rng(11)
    bars = []
    for i, symbol in enumerate(symbols):
        prices = 50 * (i + 1) + np.cumsum(rng.normal(0, 0.3, len(ts)))
        for t, price in zip(ts, prices, strict=True):
            bars.append(
                {
                    "timestamp": t.isoformat(),
                    "symbol": symbol,
                    "open": price,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price,
                    "volume": 5.0,
                }
            )
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps({"bars": bars}), encoding="utf-8"
    )
    result = derive_features_job(job.id, sets=("cross",), store=store)
    assert "ratioz_basket96" in result["features"]
    rows = [
        json.loads(line)
        for line in (root / "state" / "features.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    basket = [r for r in rows if r["name"] == "ratioz_basket96"]
    by_ts: dict[str, set[float]] = {}
    for r in basket:
        by_ts.setdefault(r["timestamp"], set()).add(round(float(r["value"]), 6))
    varying = [ts for ts, vals in by_ts.items() if len(vals) > 1]
    assert varying, "basket z must differ across symbols at the same bar"

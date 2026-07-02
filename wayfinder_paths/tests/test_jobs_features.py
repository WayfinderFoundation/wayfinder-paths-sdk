"""Exogenous feature feed: backtest/live parity, as-of no-lookahead,
staleness policies, and bit-for-bit back-compat for feature-less jobs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.driver import tick_job, view_hash
from wayfinder_paths.jobs.execution.features import (
    FeatureSpec,
    feature_staleness,
    load_feature_rows,
    merge_features,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.job import _load_dataset, _resolve_dataset
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.execution.reconcile import reconcile_job
from wayfinder_paths.jobs.execution.simulator import simulate_execution
from wayfinder_paths.jobs.features import append_feature, list_features
from wayfinder_paths.tests.test_jobs_live_driver import (
    PERP_CAPS,
    FakeAdapter,
    _bars,
    _make_job,
    _now,
)

FEATURE_STRATEGY = """
def decide(ctx):
    try:
        sentiment = float(ctx.view.feature("sentiment"))
    except ValueError:
        return []
    if "SNX" not in ctx.ledger.positions and sentiment > 0.5:
        return [{"action": "OPEN", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "buy", "size": 1}]
    if "SNX" in ctx.ledger.positions and sentiment < -0.5:
        return [{"action": "CLOSE", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "sell", "size": 1, "reduce_only": True}]
    return []
""".lstrip()

SENTIMENT_ROWS = [
    {"timestamp": "2026-01-01T00:02:00Z", "name": "sentiment", "value": 0.9},
    {"timestamp": "2026-01-01T00:12:00Z", "name": "sentiment", "value": -0.9},
]


def _feature_job(tmp_path: Path):
    store, job, root = _make_job(tmp_path)
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(FEATURE_STRATEGY, encoding="utf-8")
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [{"name": "sentiment"}]
    job.execution_spec = spec.to_dict()
    store.save(job)
    features_path = root / "state" / "features.jsonl"
    features_path.parent.mkdir(parents=True, exist_ok=True)
    features_path.write_text(
        "\n".join(json.dumps(row) for row in SENTIMENT_ROWS) + "\n",
        encoding="utf-8",
    )
    return store, job, root


def _fill_key(rows):
    return [
        (r["symbol"], r["side"], r["filled_size"], r["avg_price"], r["timestamp"])
        for r in rows
        if r["status"] == "filled"
    ]


def test_backtest_and_driver_agree_on_features(tmp_path: Path) -> None:
    """The parity anchor: identical bars + identical feature rows produce
    identical fills in backtest and through the live driver, and the
    reconciler replays the recorded ticks exactly."""
    store, job, root = _feature_job(tmp_path)
    bars = _bars(6)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)

    dataset = _load_dataset(root, spec, job.to_dict())
    backtest = simulate_execution(
        root / "workspace" / "src" / "strategy.py",
        dataset,
        spec,
        job.execution_params,
    )
    assert _fill_key(backtest.trace["fills"]), "feature strategy must trade"

    async def _drive():
        broker = PaperBroker(capabilities=PERP_CAPS)
        fills = []
        for count in range(1, len(bars) + 1):
            view = CompletedBarsView.from_rows(bars[:count])
            result = await tick_job(
                job,
                root,
                "paper",
                store=store,
                adapters={"hyperliquid": FakeAdapter(view, broker)},
                now=_now(view),
            )
            fills.extend(result["fills"])
        return fills

    driver_fills = asyncio.run(_drive())
    assert _fill_key(driver_fills) == _fill_key(backtest.trace["fills"])

    report = reconcile_job(job.id, store=store)
    assert report["intent_match_rate"] == 1.0
    assert report["data_drift_ticks"] == 0


def test_merge_is_as_of_never_lookahead() -> None:
    bars = CompletedBarsView.from_rows(_bars(4))  # 00:00, 00:05, 00:10, 00:15
    frames = {
        "sentiment": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-01-01T00:02:00Z", "2026-01-01T00:12:00Z"], utc=True
                ),
                "value": [0.9, -0.9],
                "symbol": [None, None],
            }
        )
    }
    merged = merge_features(bars, frames, [FeatureSpec(name="sentiment")])
    frame = merged.to_frame()
    by_ts = {
        row["timestamp"].isoformat(): row["sentiment"]
        for row in frame.to_dict(orient="records")
    }
    assert by_ts["2026-01-01T00:00:00+00:00"] is None  # row at 00:02 is future
    assert by_ts["2026-01-01T00:05:00+00:00"] == 0.9
    assert by_ts["2026-01-01T00:10:00+00:00"] == 0.9  # carried forward
    assert by_ts["2026-01-01T00:15:00+00:00"] == -0.9


def test_per_symbol_features_do_not_leak_across_symbols() -> None:
    rows = _bars(2) + [
        {**row, "symbol": "IMX"} for row in _bars(2)
    ]
    view = CompletedBarsView.from_rows(rows)
    frames = {
        "flow": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"], utc=True
                ),
                "value": [1.0, 2.0],
                "symbol": ["SNX", "IMX"],
            }
        )
    }
    merged = merge_features(view, frames, [FeatureSpec(name="flow")])
    assert merged.feature("flow", symbol="SNX") == 1.0
    assert merged.feature("flow", symbol="IMX") == 2.0


def test_feature_accessor_raises_when_absent() -> None:
    view = CompletedBarsView.from_rows(_bars(2))
    with pytest.raises(ValueError, match="No feature column"):
        view.feature("sentiment")
    merged = merge_features(
        view, {"sentiment": pd.DataFrame(columns=["timestamp", "value", "symbol"])},
        [FeatureSpec(name="sentiment")],
    )
    with pytest.raises(ValueError, match="No values yet"):
        merged.feature("sentiment")


async def test_stale_feature_skip_policy_skips_tick(tmp_path: Path) -> None:
    store, job, root = _feature_job(tmp_path)
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {"name": "sentiment", "max_age_seconds": 60, "stale_policy": "skip"}
    ]
    job.execution_spec = spec.to_dict()
    store.save(job)

    view = CompletedBarsView.from_rows(_bars(2))
    late = _now(view) + pd.Timedelta(hours=6)  # far past feature freshness
    result = await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))},
        now=late,
    )
    assert result["skipped"] is True
    assert result["skip_reason"] == "stale_feature"
    assert any(e["kind"] == "stale_feature" for e in result["guard_events"])


async def test_stale_feature_decide_anyway_proceeds(tmp_path: Path) -> None:
    store, job, root = _feature_job(tmp_path)
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {"name": "sentiment", "max_age_seconds": 60, "stale_policy": "decide_anyway"}
    ]
    spec.data_contract["max_bar_staleness_seconds"] = 10**9  # isolate features
    job.execution_spec = spec.to_dict()
    store.save(job)

    view = CompletedBarsView.from_rows(_bars(2))
    late = _now(view) + pd.Timedelta(minutes=30)
    result = await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))},
        now=late,
    )
    assert result["skip_reason"] != "stale_feature"
    assert any(e["kind"] == "stale_feature" for e in result["guard_events"])


def test_no_features_is_bit_identical(tmp_path: Path) -> None:
    """Jobs without declared features never touch the merge path."""
    store, job, root = _make_job(tmp_path)
    spec = ExecutionSpec.from_dict(job.execution_spec)
    assert parse_feature_specs(spec) == []

    bars = _bars(4)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    merged = _load_dataset(root, spec, job.to_dict())
    raw = _resolve_dataset(root, spec, job.to_dict())
    assert merged.bars.to_rows() == raw.bars.to_rows()
    assert view_hash(merged.bars) == view_hash(raw.bars)


def test_writer_and_reader_round_trip(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    append_feature(store, job.id, name="sentiment", value=0.7)
    append_feature(
        store, job.id, name="temp_f", value=91.5, symbol="KXHIGHNY", timestamp="2026-01-01T00:00:00Z"
    )

    rows = list_features(store, job.id)
    assert len(rows) == 2
    only_temp = list_features(store, job.id, name="temp_f")
    assert len(only_temp) == 1
    assert only_temp[0]["symbol"] == "KXHIGHNY"

    specs = [FeatureSpec(name="temp_f")]
    frames = load_feature_rows([root], specs)
    assert len(frames["temp_f"]) == 1
    assert frames["temp_f"]["value"].iloc[0] == 91.5


def test_staleness_helper_missing_rows_counts_as_stale() -> None:
    specs = [
        FeatureSpec(name="sentiment", max_age_seconds=60, stale_policy="skip")
    ]
    guards, skip = feature_staleness(
        specs,
        {"sentiment": pd.DataFrame(columns=["timestamp", "value", "symbol"])},
        pd.Timestamp("2026-01-01T00:00:00Z"),
    )
    assert skip is True
    assert guards[0]["age_seconds"] is None


def test_validation_flags_feature_schema_and_availability(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.execution.validation import _feature_checks

    spec = ExecutionSpec()
    spec.data_contract["features"] = [{"name": "sentiment"}]
    checks = _feature_checks(tmp_path, spec)
    availability = next(
        c for c in checks if c["name"] == "declared_features_available"
    )
    assert availability["passed"] is False
    assert availability["blocking"] is False
    assert availability["missing"] == ["sentiment"]

    spec.data_contract["features"] = [{"name": "bad", "stale_policy": "explode"}]
    checks = _feature_checks(tmp_path, spec)
    assert checks[0]["name"] == "declared_features_valid"
    assert checks[0]["passed"] is False

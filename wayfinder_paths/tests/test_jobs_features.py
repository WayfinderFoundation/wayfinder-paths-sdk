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
    feature_gaps,
    feature_staleness,
    load_feature_rows,
    merge_features,
    parse_feature_specs,
    summarize_features,
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


def _feature_job(tmp_path: Path, features: list[dict] | None = None):
    store, job, root = _make_job(tmp_path)
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(FEATURE_STRATEGY, encoding="utf-8")
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = features or [{"name": "sentiment"}]
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
    rows = _bars(2) + [{**row, "symbol": "IMX"} for row in _bars(2)]
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
        view,
        {"sentiment": pd.DataFrame(columns=["timestamp", "value", "symbol"])},
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
        adapters={
            "hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))
        },
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
        adapters={
            "hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))
        },
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
        store,
        job.id,
        name="temp_f",
        value=91.5,
        symbol="KXHIGHNY",
        timestamp="2026-01-01T00:00:00Z",
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
    specs = [FeatureSpec(name="sentiment", max_age_seconds=60, stale_policy="skip")]
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
    availability = next(c for c in checks if c["name"] == "declared_features_available")
    assert availability["passed"] is False
    assert availability["blocking"] is False
    assert availability["missing"] == ["sentiment"]

    spec.data_contract["features"] = [{"name": "bad", "stale_policy": "explode"}]
    checks = _feature_checks(tmp_path, spec)
    assert checks[0]["name"] == "declared_features_valid"
    assert checks[0]["passed"] is False


def test_feature_coverage_metadata_and_summary_note(tmp_path: Path) -> None:
    """A feature spanning only the tail of the dataset must be measured
    (metadata.feature_coverage) and called out in the backtest summary —
    a 1-year funding file silently handicapped a signal against 6 years of
    candles in a live comparison."""
    from wayfinder_paths.jobs.execution.job import summarize_backtest_payload

    store, job, root = _feature_job(tmp_path)
    bars = _bars(40)  # 40 x 5min bars
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    # sentiment rows cover only the last ~10% of the bar span
    tail_rows = [
        {"timestamp": bars[-3]["timestamp"], "name": "sentiment", "value": 0.9},
        {"timestamp": bars[-1]["timestamp"], "name": "sentiment", "value": 0.2},
    ]
    (root / "state" / "features.jsonl").write_text(
        "\n".join(json.dumps(row) for row in tail_rows) + "\n", encoding="utf-8"
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)
    dataset = _load_dataset(root, spec, job.to_dict())
    coverage = dataset.metadata["feature_coverage"]["sentiment"]
    assert coverage["rows"] == 2
    assert coverage["coverage_fraction"] < 0.2

    payload = {
        "type": "single",
        "result": {"run_id": "r", "params": {}, "stats": {}, "validation": {}},
        "dataset": dict(dataset.metadata),
    }
    summary = summarize_backtest_payload(payload)
    note = summary.get("feature_coverage_note")
    assert note and "sentiment" in note and "biased" in note
    assert summary["feature_coverage"]["sentiment"]["rows"] == 2

    # Full-coverage feature -> no note.
    full_rows = [
        {"timestamp": bars[0]["timestamp"], "name": "sentiment", "value": 0.1},
        {"timestamp": bars[-1]["timestamp"], "name": "sentiment", "value": 0.2},
    ]
    (root / "state" / "features.jsonl").write_text(
        "\n".join(json.dumps(row) for row in full_rows) + "\n", encoding="utf-8"
    )
    dataset = _load_dataset(root, spec, job.to_dict())
    payload["dataset"] = dict(dataset.metadata)
    summary = summarize_backtest_payload(payload)
    assert "feature_coverage_note" not in summary


# ---- cadence, smoothing and reconciliation ---------------------------------


def _frame(rows: list[tuple[str, float | None, str | None]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([r[0] for r in rows], utc=True),
            "value": [r[1] for r in rows],
            "symbol": [r[2] for r in rows],
        }
    )


def _at(view: CompletedBarsView, column: str) -> pd.Series:
    frame = view.to_frame()
    return frame.set_index("timestamp")[column]


def test_load_feature_rows_keeps_the_last_written_row_per_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "features.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "name": "rate", "value": 0.05},
        {"timestamp": "2026-01-01T01:00:00Z", "name": "rate", "value": 0.06},
        # the same stamp re-appended by a refresh with the source's revised value
        {"timestamp": "2026-01-01T00:00:00Z", "name": "rate", "value": 0.055},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    frame = load_feature_rows([tmp_path], [FeatureSpec(name="rate")])["rate"]
    assert list(frame["value"]) == [0.055, 0.06]
    assert summarize_features is not None


def test_cadence_keeps_one_observation_per_period_without_lookahead() -> None:
    spec = FeatureSpec.from_dict({"name": "rate", "cadence": "1h"})
    feature = _frame(
        [
            ("2026-01-01T00:00:07Z", 0.05, None),
            ("2026-01-01T00:30:00Z", 0.07, None),
            ("2026-01-01T01:00:03Z", 0.06, None),
        ]
    )
    merged = merge_features(
        CompletedBarsView.from_rows(_bars(15)), {"rate": feature}, [spec]
    )
    at = _at(merged, "rate")
    assert at[pd.Timestamp("2026-01-01T00:00:00Z")] is None  # observed 7 s later
    assert (
        at[pd.Timestamp("2026-01-01T00:25:00Z")] is None
    )  # 00:00:07 row lost to 00:30
    assert at[pd.Timestamp("2026-01-01T00:30:00Z")] == 0.07
    assert at[pd.Timestamp("2026-01-01T01:00:00Z")] == 0.07  # 01:00:03 not yet visible
    assert at[pd.Timestamp("2026-01-01T01:05:00Z")] == 0.06


def test_mean_smoothing_is_trailing_over_the_window_in_feed_time() -> None:
    spec = FeatureSpec.from_dict(
        {
            "name": "rate",
            "cadence": "1h",
            "smoothing": {"method": "mean", "window": "3h"},
        }
    )
    feature = _frame(
        [(f"2026-01-01T0{h}:00:00Z", float(h + 1), None) for h in range(4)]
    )
    merged = merge_features(
        CompletedBarsView.from_rows(_bars(50)), {"rate": feature}, [spec]
    )
    smooth = _at(merged, "rate")
    raw = _at(merged, "rate__raw")
    assert smooth[pd.Timestamp("2026-01-01T00:30:00Z")] == pytest.approx(1.0)
    assert smooth[pd.Timestamp("2026-01-01T02:30:00Z")] == pytest.approx(2.0)
    assert smooth[pd.Timestamp("2026-01-01T03:30:00Z")] == pytest.approx(3.0)
    assert raw[pd.Timestamp("2026-01-01T02:30:00Z")] == 3.0
    assert raw[pd.Timestamp("2026-01-01T03:30:00Z")] == 4.0


def test_ewm_smoothing_uses_the_window_as_half_life() -> None:
    spec = FeatureSpec.from_dict(
        {
            "name": "rate",
            "cadence": "1h",
            "smoothing": {"method": "ewm", "window": "1h"},
        }
    )
    feature = _frame(
        [("2026-01-01T00:00:00Z", 0.0, None), ("2026-01-01T01:00:00Z", 1.0, None)]
    )
    merged = merge_features(
        CompletedBarsView.from_rows(_bars(20)), {"rate": feature}, [spec]
    )
    smooth = _at(merged, "rate")
    assert smooth[pd.Timestamp("2026-01-01T00:30:00Z")] == pytest.approx(0.0)
    # one half-life later the old observation weighs half of the new one
    assert smooth[pd.Timestamp("2026-01-01T01:30:00Z")] == pytest.approx(2 / 3)


def test_feed_gap_guard_fires_when_rows_are_more_than_two_periods_apart() -> None:
    spec = FeatureSpec.from_dict({"name": "rate", "cadence": "1h"})
    now = pd.Timestamp("2026-01-01T03:10:00Z")
    gapped = _frame(
        [("2026-01-01T00:00:00Z", 1.0, None), ("2026-01-01T03:00:00Z", 2.0, None)]
    )
    events, skip = feature_staleness([spec], {"rate": gapped}, now)
    assert not skip and [e["kind"] for e in events] == ["feed_gap"]
    assert events[0]["gap_seconds"] == 3 * 3600 and events[0]["cadence_seconds"] == 3600
    steady = _frame(
        [("2026-01-01T02:00:00Z", 1.0, None), ("2026-01-01T03:00:00Z", 2.0, None)]
    )
    assert feature_staleness([spec], {"rate": steady}, now) == ([], False)
    assert feature_gaps(gapped, 3600) == {
        "count": 1,
        "largest_seconds": 3 * 3600.0,
        "first_at": "2026-01-01T01:00:00+00:00",
    }


def test_two_cadences_line_up_on_five_minute_bars() -> None:
    hourly = FeatureSpec.from_dict({"name": "hourly", "cadence": "1h"})
    daily = FeatureSpec.from_dict({"name": "daily", "cadence": "1d"})
    frames = {
        "hourly": _frame(
            [("2026-01-01T00:00:00Z", 0.1, None), ("2026-01-01T01:00:00Z", 0.2, None)]
        ),
        "daily": _frame(
            [("2025-12-31T00:00:00Z", 5.0, None), ("2026-01-01T00:00:00Z", 6.0, None)]
        ),
    }
    merged = merge_features(
        CompletedBarsView.from_rows(_bars(15)), frames, [hourly, daily]
    )
    assert _at(merged, "hourly")[pd.Timestamp("2026-01-01T00:30:00Z")] == 0.1
    assert _at(merged, "hourly")[pd.Timestamp("2026-01-01T01:05:00Z")] == 0.2
    assert _at(merged, "daily")[pd.Timestamp("2026-01-01T00:30:00Z")] == 6.0
    assert _at(merged, "daily")[pd.Timestamp("2026-01-01T01:05:00Z")] == 6.0


def test_spec_validation_rejects_bad_feed_cadence_and_smoothing() -> None:
    with pytest.raises(ValueError, match="feed.kind"):
        FeatureSpec.from_dict({"name": "x", "feed": {"kind": "nope"}})
    with pytest.raises(ValueError, match="cadence"):
        FeatureSpec.from_dict({"name": "x", "cadence": "soon"})
    with pytest.raises(ValueError, match="needs a cadence"):
        FeatureSpec.from_dict(
            {"name": "x", "smoothing": {"method": "mean", "window": "1h"}}
        )
    with pytest.raises(ValueError, match="at least the cadence"):
        FeatureSpec.from_dict(
            {
                "name": "x",
                "cadence": "1d",
                "smoothing": {"method": "mean", "window": "1h"},
            }
        )
    with pytest.raises(ValueError, match="smoothing.method"):
        FeatureSpec.from_dict(
            {
                "name": "x",
                "cadence": "1h",
                "smoothing": {"method": "median", "window": "1h"},
            }
        )
    spec = ExecutionSpec.from_dict(
        {
            "data_contract": {
                "features": [
                    {
                        "name": "token_price:ethereum-base",
                        "feed": {"kind": "token_price", "token_id": "ethereum-base"},
                        "cadence": "1h",
                        "smoothing": {"method": "none"},
                    }
                ]
            }
        }
    )
    parsed = parse_feature_specs(spec)
    assert parsed[0].feed == {"kind": "token_price", "token_id": "ethereum-base"}
    assert parsed[0].cadence_seconds == 3600 and parsed[0].smoothing_method == "none"


def test_summary_reports_cadence_gaps_and_revised_rows(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "state" / "features.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "name": "rate", "value": 0.05},
        {"timestamp": "2026-01-01T03:00:00Z", "name": "rate", "value": 0.06},
        {"timestamp": "2026-01-01T03:00:00Z", "name": "rate", "value": 0.061},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    spec = ExecutionSpec.from_dict(
        {"data_contract": {"features": [{"name": "rate", "cadence": "1h"}]}}
    )
    summary = summarize_features(root, spec, now=pd.Timestamp("2026-01-01T04:00:00Z"))
    assert summary is not None
    entry = summary[0]
    assert entry["cadence"] == "1h" and entry["smoothing"] is None
    assert entry["gaps"]["count"] == 1 and entry["revised_rows"] == 1
    assert entry["latest_value"] == 0.061


def test_backtest_and_driver_agree_with_smoothing_on(tmp_path: Path) -> None:
    """Parity with a cadenced, smoothed feature: the smoothed column is
    computed from feed rows alone, so backtest and driver fill identically."""
    store, job, root = _feature_job(
        tmp_path,
        features=[
            {
                "name": "sentiment",
                "cadence": "5m",
                "smoothing": {"method": "mean", "window": "10m"},
            }
        ],
    )
    bars = _bars(6)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)
    dataset = _load_dataset(root, spec, job.to_dict())
    assert "sentiment__raw" in dataset.bars.to_frame().columns
    backtest = simulate_execution(
        root / "workspace" / "src" / "strategy.py", dataset, spec, job.execution_params
    )
    assert _fill_key(backtest.trace["fills"]), "smoothed feature strategy must trade"

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
    assert report["intent_match_rate"] == 1.0 and report["data_drift_ticks"] == 0

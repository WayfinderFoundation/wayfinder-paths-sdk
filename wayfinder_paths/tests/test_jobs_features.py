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


def test_feature_accessor_default_covers_missing_history_not_missing_columns() -> None:
    view = CompletedBarsView.from_rows(_bars(2))
    merged = merge_features(
        view,
        {"macro_regime": pd.DataFrame(columns=["timestamp", "value", "symbol"])},
        [FeatureSpec(name="macro_regime")],
    )
    # A declared column with no history yet takes the default (the macro
    # label needs 28 days of closes before its first row).
    assert merged.feature("macro_regime", default=0.0) == 0.0
    with pytest.raises(ValueError, match="No values yet"):
        merged.feature("macro_regime")
    # An undeclared column is a contract error, default or not.
    with pytest.raises(ValueError, match="No feature column"):
        view.feature("macro_regime", default=0.0)


def test_driver_queues_bars_only_and_shadows_merge_their_own_declared_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live driver -> queued probation view -> shadow lane, incumbent and
    candidate both declaring `sentiment`: the queue carries bars only (no
    incumbent column to collide with or to leak) and the candidate reads the
    values its own declaration merges."""
    import asyncio

    from wayfinder_paths.jobs import background
    from wayfinder_paths.jobs.candidate_shadow import run_candidate_shadows
    from wayfinder_paths.jobs.gating import compute_workspace_revision
    from wayfinder_paths.jobs.probation import (
        PROBATION_VIEW_PATH,
        stage_evolution_probation,
    )

    store, job, root = _feature_job(tmp_path)
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n"
        "    sentiment = ctx.view.feature('sentiment', default=0.0)\n"
        "    if sentiment not in (0.0, 0.9, -0.9):\n"
        "        raise ValueError('a column collided or leaked')\n"
        "    if str(ctx.timestamp) >= '2026-01-01T00:25' and sentiment != -0.9:\n"
        "        raise ValueError('the candidate never saw its declared feature')\n"
        "    return []\n",
        encoding="utf-8",
    )
    stage_evolution_probation(
        store,
        job.id,
        candidate_id="candidate-1",
        candidate_root=root,
        revision=compute_workspace_revision(root),
        source="evolution_campaign",
        family="feature-aware",
        evidence={"objective": {"candidate": {"trade_count": 12}}},
        now=pd.Timestamp("2025-12-31T23:00:00Z").to_pydatetime(),
    )
    monkeypatch.setattr(background, "spawn_detached_op", lambda *a, **k: None)
    bars = _bars(6)

    async def _drive() -> None:
        broker = PaperBroker(capabilities=PERP_CAPS)
        for count in range(1, len(bars) + 1):
            view = CompletedBarsView.from_rows(bars[:count])
            await tick_job(
                job,
                root,
                "paper",
                store=store,
                adapters={"hyperliquid": FakeAdapter(view, broker)},
                now=_now(view),
            )

    asyncio.run(_drive())
    queued = json.loads((root / PROBATION_VIEW_PATH).read_text(encoding="utf-8"))
    assert queued["rows"] and all("sentiment" not in row for row in queued["rows"])

    rows = asyncio.run(run_candidate_shadows(store, job.id))
    candidate = [row for row in rows if row["role"] == "candidate"]
    assert candidate and not any(row["skipped"] for row in candidate)
    assert max(row["bar_timestamp"] for row in candidate) >= "2026-01-01T00:25"


def test_feature_paths_are_contained_to_the_job_store_or_workspace(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.execution.features import DEFAULT_FEATURES_PATH

    assert FeatureSpec.from_dict({"name": "x"}).path == DEFAULT_FEATURES_PATH
    assert (
        FeatureSpec.from_dict({"name": "x", "path": "workspace/data/x.jsonl"}).path
        == "workspace/data/x.jsonl"
    )
    for bad in (
        "/etc/passwd",
        "../other-job/state/features.jsonl",
        "workspace/../../x.jsonl",
        "state/custom.jsonl",
        "results/x.jsonl",
    ):
        with pytest.raises(ValueError, match="path must be"):
            FeatureSpec.from_dict({"name": "x", "path": bad})

    # A symlink under workspace/ that points out of the root is refused by
    # the loader even though its declared path is well-formed.
    root = tmp_path / "job"
    (root / "workspace").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        '{"timestamp": "2026-01-01T00:00:00Z", "name": "x", "value": 1}\n'
    )
    (root / "workspace" / "link.jsonl").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes its root"):
        load_feature_rows(
            [root],
            [FeatureSpec.from_dict({"name": "x", "path": "workspace/link.jsonl"})],
        )


def test_validation_refuses_the_skip_policy_until_backtests_replay_freshness(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.execution.validation import _feature_checks

    root = tmp_path / "job"
    (root / "state").mkdir(parents=True)
    (root / "state" / "features.jsonl").write_text(
        '{"timestamp": "2026-01-01T00:00:00Z", "name": "x", "value": 1}\n'
    )
    spec = ExecutionSpec.from_dict(
        {
            "data_contract": {
                "bar_interval": "5m",
                "symbols": ["BTC"],
                "features": [
                    {"name": "x", "max_age_seconds": 60, "stale_policy": "skip"}
                ],
            }
        }
    )
    failed = [check for check in _feature_checks(root, spec) if not check["passed"]]
    assert [check["name"] for check in failed] == ["feature_policy_replayable"]
    assert failed[0]["blocking"] is True and "decide_anyway" in failed[0]["hint"]
    spec.data_contract["features"][0]["stale_policy"] = "decide_anyway"
    assert all(check["passed"] for check in _feature_checks(root, spec))


async def test_stale_feature_skip_does_not_run_precompute_over_the_missing_column(
    tmp_path: Path,
) -> None:
    """precompute() consuming the declared column used to raise KeyError on
    a skipped tick, because the merge was omitted but the hook still ran."""
    store, job, root = _feature_job(tmp_path)
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def precompute(frames):\n"
        "    return {\n"
        "        symbol: frame[['sentiment']].rename(columns={'sentiment': 'sentiment_copy'})\n"
        "        for symbol, frame in frames.items()\n"
        "    }\n\n"
        "def decide(ctx):\n"
        "    return []\n",
        encoding="utf-8",
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {"name": "sentiment", "max_age_seconds": 60, "stale_policy": "skip"}
    ]
    job.execution_spec = spec.to_dict()
    store.save(job)
    view = CompletedBarsView.from_rows(_bars(2))
    result = await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={
            "hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))
        },
        now=_now(view) + pd.Timedelta(hours=6),
    )
    assert result["skipped"] is True and result["skip_reason"] == "stale_feature"


def _row(stamp: str, name: str, value: float) -> str:
    return (
        json.dumps({"timestamp": stamp, "name": name, "value": value, "symbol": None})
        + "\n"
    )


def test_feature_roots_are_owned_by_path_class(tmp_path: Path) -> None:
    """(bundle, protected_root): the store is read from the protected root
    even when the bundle carries one; a workspace/ file is read from the
    bundle even when the job's own workspace has one."""
    from wayfinder_paths.jobs.execution.features import DEFAULT_FEATURES_PATH

    job, bundle = tmp_path / "job", tmp_path / "bundle"
    for base in (job, bundle):
        (base / "state").mkdir(parents=True)
        (base / "workspace" / "data").mkdir(parents=True)
    (job / DEFAULT_FEATURES_PATH).write_text(_row("2026-01-01T00:00:00Z", "store", 1.0))
    (bundle / DEFAULT_FEATURES_PATH).write_text(
        _row("2026-01-01T00:00:00Z", "store", 9.0)
    )
    (job / "workspace/data/sig.jsonl").write_text(
        _row("2026-01-01T00:00:00Z", "sig", 1.0)
    )
    specs = [
        FeatureSpec.from_dict({"name": "store"}),
        FeatureSpec.from_dict({"name": "sig", "path": "workspace/data/sig.jsonl"}),
    ]
    frames = load_feature_rows([bundle, job], specs)
    assert frames["store"]["value"].tolist() == [1.0]
    assert frames["sig"].empty  # the job's workspace file is not the bundle's
    (bundle / "workspace/data/sig.jsonl").write_text(
        _row("2026-01-01T00:00:00Z", "sig", 7.0)
    )
    assert load_feature_rows([bundle, job], specs)["sig"]["value"].tolist() == [7.0]
    single = load_feature_rows([job], specs)
    assert single["store"]["value"].tolist() == [1.0]
    assert single["sig"]["value"].tolist() == [1.0]


def test_load_dataset_merges_bundle_workspace_features_with_the_protected_store(
    tmp_path: Path,
) -> None:
    """The choke point every gating lane calls: the bundle's workspace file and
    the protected root's store land on the same bars."""
    store, job, root = _feature_job(tmp_path)
    bars = _bars(6)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    bundle = tmp_path / "bundle"
    (bundle / "workspace" / "data").mkdir(parents=True)
    (bundle / "workspace/data/sig.jsonl").write_text(
        _row(bars[2]["timestamp"], "sig", 1.0)
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {"name": "sentiment"},
        {"name": "sig", "source": "file", "path": "workspace/data/sig.jsonl"},
    ]
    dataset = _load_dataset(root, spec, job.to_dict(), feature_roots=(bundle, root))
    frame = dataset.bars.to_frame()
    assert frame["sentiment"].notna().any()
    assert frame["sig"].notna().any()


def test_validation_reports_an_escaping_feature_file_instead_of_raising(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.execution.validation import validate_execution_job

    store, job, root = _feature_job(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_row("2026-01-01T00:00:00Z", "sig", 1.0))
    (root / "workspace" / "data").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "data" / "link.jsonl").symlink_to(outside)
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {"name": "sig", "source": "file", "path": "workspace/data/link.jsonl"}
    ]
    job.execution_spec = spec.to_dict()
    store.save(job)
    report = validate_execution_job(job.id, store=store)
    failed = [c for c in report["checks"] if c["name"] == "declared_features_valid"]
    assert failed and failed[0]["passed"] is False
    assert "escapes its root" in failed[0]["error"]


def test_validation_flags_feature_reads_the_spec_does_not_declare(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.execution.validation import validate_execution_job

    store, job, root = _feature_job(tmp_path)

    def check(report: dict) -> dict:
        return next(
            c for c in report["checks"] if c["name"] == "undeclared_feature_read"
        )

    assert check(validate_execution_job(job.id, store=store))["passed"] is True
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n"
        "    ctx.view.feature('sentiment')\n"
        "    ctx.view.feature('leader_state', default=0.0)\n"
        "    ctx.view.feature(name='macro_regime')\n"
        "    return []\n",
        encoding="utf-8",
    )
    flagged = check(validate_execution_job(job.id, store=store))
    assert flagged["passed"] is False and flagged["blocking"] is True
    assert flagged["details"] == ["leader_state", "macro_regime"]

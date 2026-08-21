"""Derived-features bridge: cross-symbol/exogenous columns into the feature
store so single-symbol scans can see between symbols and outside the panel."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.derived_features import derive_features_job
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _panel_frame(count: int = 400) -> list[dict]:
    base = pd.Timestamp("2026-07-01T00:00:00Z")
    rows = []
    for i in range(count):
        ts = (base + pd.Timedelta(minutes=5 * i)).isoformat()
        for symbol, phase in (("LIT", 0.0), ("SOL", 0.7)):
            price = (
                100 + 5 * math.sin(i / 9 + phase) + (0.01 * i if symbol == "SOL" else 0)
            )
            rows.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "open": price,
                    "high": price + 0.4,
                    "low": price - 0.4,
                    "close": price + 0.1,
                    "volume": 10.0,
                }
            )
    return rows


def _make_job(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "derive-demo",
        script=".wayfinder/jobs/derive-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars_path.write_text(json.dumps(_panel_frame()), encoding="utf-8")
    return store, job.id


def _fake_fetch(coin: str, start_ms: int, end_ms: int) -> pd.Series:
    stamps = pd.date_range(
        pd.Timestamp(start_ms, unit="ms", tz="UTC"),
        pd.Timestamp(end_ms, unit="ms", tz="UTC"),
        freq="5min",
    )
    values = [50_000 + i for i in range(len(stamps))]
    return pd.Series(values, index=stamps, dtype=float)


def test_derive_features_appends_and_dedupes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path)

    result = derive_features_job(
        job_id, sets=("cross", "exog"), store=store, fetch_closes=_fake_fetch
    )

    assert result["rows_appended"] > 0
    assert result["market_state"]["path"].endswith("market_state.json")
    assert (
        store.job_dir(job_id) / "results" / "research" / "market_state.json"
    ).exists()
    names = set(result["per_feature"])
    assert {"breadth_sma50", "panelret_lag1", "btc_ret12", "btc_trend"} <= names
    assert any(n.startswith("corr_sol") for n in names)  # LIT gets corr to SOL
    assert any(n.startswith("ratioz_") for n in names)

    rows = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "state" / "features.jsonl")
        .read_text()
        .splitlines()
    ]
    sample = next(r for r in rows if r["name"] == "btc_trend")
    assert sample["symbol"] in {"LIT", "SOL"}
    assert sample["value"] in (0.0, 1.0)

    # Idempotent: re-run appends nothing (dedup on timestamp/name/symbol).
    again = derive_features_job(
        job_id, sets=("cross", "exog"), store=store, fetch_closes=_fake_fetch
    )
    assert again["rows_appended"] == 0

    with pytest.raises(ValueError, match="unknown feature sets"):
        derive_features_job(job_id, sets=("magic",), store=store)


def test_venue_basis_per_symbol(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path)

    def fetch(coin: str, start_ms: int, end_ms: int) -> pd.Series:
        # HL trades 10bps above the dataset price for every symbol.
        stamps = pd.date_range(
            pd.Timestamp(start_ms, unit="ms", tz="UTC"),
            pd.Timestamp(end_ms, unit="ms", tz="UTC"),
            freq="5min",
        )
        return pd.Series(100.0 * 1.001, index=stamps)

    result = derive_features_job(
        job_id, sets=("venue",), store=store, fetch_closes=fetch
    )
    assert result["per_feature"].get("venue_basis_bps", 0) > 0


def test_derived_columns_reach_research_frames(tmp_path: Path) -> None:
    # The bug the watcher hit live: derive-features wrote rows, but research
    # frames only merged CONTRACT-declared features -> "available non-bar
    # columns: []". Research loaders must merge store features too.
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    store, job_id = _make_job(tmp_path)
    derive_features_job(
        job_id, sets=("cross", "exog"), store=store, fetch_closes=_fake_fetch
    )
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)

    research = _load_dataset(root, spec, job_data, include_store_features=True)
    frame = research.bars.to_frame()
    assert "btc_trend" in frame.columns
    assert "breadth_sma50" in frame.columns

    # Execution path unchanged: undeclared features stay out of live frames.
    execution = _load_dataset(root, spec, job_data)
    assert "btc_trend" not in execution.bars.to_frame().columns


def test_refresh_if_stale_gates_and_journals(tmp_path) -> None:
    from wayfinder_paths.jobs.derived_features import (
        REFRESH_STAMP_PATH,
        refresh_derived_features_if_stale,
    )
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("refresh-demo", agent_mode="intervene")
    store.save(job)

    calls: list[str] = []

    def fake_derive(job_id, **kwargs):
        calls.append(job_id)
        return {"rows_appended": 7, "sets": list(kwargs["sets"])}

    # Stale (no stamp) -> derives and stamps.
    first = refresh_derived_features_if_stale(job.id, store=store, derive=fake_derive)
    assert first == {"refreshed": True, "rows_appended": 7}
    assert calls == [job.id]
    stamp = store.read_json(job.id, REFRESH_STAMP_PATH)
    assert stamp["rows_appended"] == 7 and "regime" in stamp["sets"]

    # Fresh stamp -> no second derive.
    second = refresh_derived_features_if_stale(job.id, store=store, derive=fake_derive)
    assert second["refreshed"] is False and calls == [job.id]

    # Failure: journaled, degraded, no stamp update, never raises.
    store.write_json(job.id, REFRESH_STAMP_PATH, {})

    def boom(job_id, **kwargs):
        raise RuntimeError("exog feed down")

    degraded = refresh_derived_features_if_stale(job.id, store=store, derive=boom)
    assert degraded["refreshed"] is False and "exog feed down" in degraded["reason"]
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "derived_features_refresh_failed" in journal


def test_research_substrate_block_reads_disk(tmp_path) -> None:
    import json as _json

    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.worker import _research_substrate_block

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("substrate-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        _json.dumps(
            {
                "bars": [],
                "metadata": {"fetched_at": "2026-07-27T01:43:00+00:00", "days": 14},
            }
        ),
        encoding="utf-8",
    )
    (root / "state").mkdir(exist_ok=True)
    rows = [
        {"timestamp": "2026-07-26T01:05:00+00:00", "name": "btc_trend"},
        {"timestamp": "2026-07-27T00:50:00+00:00", "name": "btc_trend"},
    ]
    (root / "state" / "features.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    block = _research_substrate_block(root)
    assert block["dataset_fetched_at"] == "2026-07-27T01:43:00+00:00"
    assert block["derived_features_newest_ts"] == "2026-07-27T00:50:00+00:00"
    assert "EVERY wake" in block["_basis"]

    # Empty job -> empty block, no crash.
    job2 = WayfinderJob.new("substrate-empty", agent_mode="intervene")
    store.save(job2)
    assert _research_substrate_block(store.job_dir(job2.id)) == {}


def test_exog_fetch_is_incremental_after_backfill(tmp_path: Path) -> None:
    """First run backfills the dataset span; later runs fetch only the tail
    plus rolling warmup — the full-span fetch every 30 minutes was the
    request storm behind the 429/credit burn."""
    store, job_id = _make_job(tmp_path)
    windows: list[tuple[int, int]] = []

    def recording_fetch(coin: str, start_ms: int, end_ms: int) -> pd.Series:
        windows.append((start_ms, end_ms))
        return _fake_fetch(coin, start_ms, end_ms)

    derive_features_job(
        job_id, sets=("exog",), store=store, fetch_closes=recording_fetch
    )
    first_start, first_end = windows[0]

    derive_features_job(
        job_id, sets=("exog",), store=store, fetch_closes=recording_fetch
    )
    second_start, _ = windows[-1]

    # The dataset spans 400 bars; the incremental window must skip most of it
    # (tail + warmup only), never re-request the full span.
    assert second_start > first_start
    span = first_end - first_start
    assert (first_end - second_start) < span / 2


def test_refresh_escalates_after_consecutive_failures_and_recovers(tmp_path) -> None:
    from wayfinder_paths.jobs.derived_features import (
        REFRESH_STAMP_PATH,
        refresh_derived_features_if_stale,
    )
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("degrade-demo", agent_mode="intervene")
    store.save(job)

    def boom(job_id, **kwargs):
        raise RuntimeError("rate_limited: HTTP 429 from candles endpoint")

    for _ in range(4):
        refresh_derived_features_if_stale(
            job.id, store=store, derive=boom, refresh_dataset=False
        )

    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    degraded = [line for line in journal.splitlines() if "data_feed_degraded" in line]
    assert len(degraded) == 1, "escalates once per episode, not per failure"
    assert "rate_limited" in degraded[0]
    stamp = store.read_json(job.id, REFRESH_STAMP_PATH)
    assert stamp["consecutive_failures"] == 4
    assert stamp.get("degraded_since")

    def healthy(job_id, **kwargs):
        return {
            "rows_appended": 3,
            "sets": list(kwargs["sets"]),
            "newest_feature_ts": pd.Timestamp.now(tz="UTC").isoformat(),
        }

    result = refresh_derived_features_if_stale(
        job.id, store=store, derive=healthy, refresh_dataset=False
    )
    assert result["refreshed"] is True
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "data_feed_recovered" in journal
    stamp = store.read_json(job.id, REFRESH_STAMP_PATH)
    assert stamp["consecutive_failures"] == 0
    assert not stamp.get("degraded_since")


def test_refresh_alarms_on_stale_features_despite_success(tmp_path) -> None:
    """A refresh that 'succeeds' while the newest feature is hours old is a
    degradation too — the silent-wedge mode seen live (rows_appended: 0,
    features 15h stale)."""
    from wayfinder_paths.jobs.derived_features import (
        refresh_derived_features_if_stale,
    )
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("stale-demo", agent_mode="intervene")
    store.save(job)

    stale_ts = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=15)).isoformat()

    def wedged(job_id, **kwargs):
        return {
            "rows_appended": 0,
            "sets": list(kwargs["sets"]),
            "newest_feature_ts": stale_ts,
        }

    refresh_derived_features_if_stale(
        job.id, store=store, derive=wedged, refresh_dataset=False
    )
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "data_feed_degraded" in journal
    assert "features_stale" in journal


def test_refresh_extends_stale_dataset_with_recorded_provenance(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.derived_features import (
        refresh_derived_features_if_stale,
    )
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("dataset-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=20)).isoformat()
    bars_path.write_text(
        json.dumps(
            {
                "bars": [{"timestamp": old, "symbol": "LIT", "close": 1.0}],
                "metadata": {
                    "days": 120,
                    "source": "ccxt",
                    "exchange": "binance",
                    "interval": "5m",
                    "symbols": ["LIT"],
                },
            }
        ),
        encoding="utf-8",
    )

    fetches: list[dict] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.build_live_dataset",
        lambda job_id, **kwargs: fetches.append({"job_id": job_id, **kwargs}),
    )

    def healthy(job_id, **kwargs):
        return {
            "rows_appended": 1,
            "sets": list(kwargs["sets"]),
            "newest_feature_ts": pd.Timestamp.now(tz="UTC").isoformat(),
        }

    result = refresh_derived_features_if_stale(job.id, store=store, derive=healthy)
    assert result["refreshed"] is True
    assert len(fetches) == 1
    assert fetches[0]["days"] == 120
    assert fetches[0]["source"] == "ccxt"
    assert fetches[0]["exchange"] == "binance"

    # refresh_dataset=False (the in-dataset-build call) never re-fetches.
    store.write_json(job.id, "results/research/derived_refresh.json", {})
    refresh_derived_features_if_stale(
        job.id, store=store, derive=healthy, refresh_dataset=False
    )
    assert len(fetches) == 1

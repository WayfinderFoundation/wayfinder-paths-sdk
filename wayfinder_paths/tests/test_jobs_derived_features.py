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

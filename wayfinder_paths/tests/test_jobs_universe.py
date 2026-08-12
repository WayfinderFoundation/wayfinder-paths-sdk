"""Universe screen: liquidity filter + current-symbol exclusion, pooled BH
across the sweep, shortlist artifact + ledger row."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import yaml

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.universe import (
    UNIVERSE_SCAN_PATH,
    universe_scan_job,
)


def _mk_job(tmp_path):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("uni-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    (root / "job.yaml").write_text(
        yaml.safe_dump(
            {
                "id": job.id,
                "execution_spec": {
                    "data_contract": {"bar_interval": "5m", "symbols": ["LIT"]}
                },
                "execution_params": {"symbols": ["LIT"]},
            }
        ),
        encoding="utf-8",
    )
    return store, job.id


def _bars(symbol: str, count: int = 900) -> pd.DataFrame:
    ts = pd.date_range("2026-07-10", periods=count, freq="5min", tz="UTC")
    rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
    price = 10 + np.cumsum(rng.normal(0, 0.05, count))
    return pd.DataFrame(
        {
            "timestamp": [t.isoformat() for t in ts],
            "symbol": symbol,
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 100.0,
        }
    )


def test_universe_scan_filters_pools_and_persists(tmp_path) -> None:
    store, job_id = _mk_job(tmp_path)
    universe = [
        {"symbol": "AAA", "volume_24h_usd": 90e6},
        {"symbol": "BBB", "volume_24h_usd": 40e6},
        {"symbol": "LIT", "volume_24h_usd": 50e6},  # current -> excluded
        {"symbol": "TINY", "volume_24h_usd": 1e6},  # below floor -> excluded
        {"symbol": "GONE", "volume_24h_usd": 80e6, "delisted": True},
        {"symbol": "CCC", "volume_24h_usd": 30e6},
    ]
    fetched: list[str] = []

    def fake_bars(symbol: str, days: int):
        fetched.append(symbol)
        if symbol == "CCC":
            return None  # venue returned nothing -> reported, not fatal
        return _bars(symbol)

    result = universe_scan_job(
        job_id,
        top=3,
        min_volume_usd=5e6,
        store=store,
        fetch_universe=lambda: universe,
        fetch_bars=fake_bars,
    )
    # Volume-ranked shortlist excludes current/delisted/below-floor.
    assert fetched == ["AAA", "BBB", "CCC"]
    by_symbol = {c["symbol"]: c for c in result["candidates"]}
    assert by_symbol["AAA"]["scanned"] and by_symbol["BBB"]["scanned"]
    assert by_symbol["CCC"]["scanned"] is False
    assert by_symbol["AAA"]["current_regime"]
    # Pooled family covers BOTH scanned symbols' rows.
    assert result["pooled_tests"] > 0
    assert result["filters"]["excluded_current"] == ["LIT"]
    assert "SHORTLIST" in result["read"] or "SCREEN" in result["read"]
    # every scanned candidate has verdict-derived fields
    assert "promote" in by_symbol["AAA"] and "best_rows" in by_symbol["AAA"]

    # Artifact + research ledger row persisted.
    saved = json.loads(
        (store.job_dir(job_id) / UNIVERSE_SCAN_PATH).read_text(encoding="utf-8")
    )
    assert saved["pooled_tests"] == result["pooled_tests"]
    ledger = (store.job_dir(job_id) / "ledgers" / "candidates.jsonl").read_text(
        encoding="utf-8"
    )
    assert "universe-scan-" in ledger


def test_resolve_dataset_keeps_file_metadata(tmp_path) -> None:
    """input_bars.json metadata (days/fetched_at) must survive into dataset
    metadata — it is the only source for the UI's window labels."""
    import json as _json

    from wayfinder_paths.jobs.execution.job import _load_dataset
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec

    root = tmp_path
    (root / "results" / "backtest").mkdir(parents=True)
    bars = [
        {
            "timestamp": f"2026-07-01T00:{m:02d}:00+00:00",
            "symbol": "LIT",
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 2.0,
        }
        for m in range(10)
    ]
    (root / "results" / "backtest" / "input_bars.json").write_text(
        _json.dumps(
            {"bars": bars, "metadata": {"days": 14, "fetched_at": "2026-07-27"}}
        ),
        encoding="utf-8",
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    dataset = _load_dataset(root, spec, {})
    assert dataset.metadata["days"] == 14
    assert dataset.metadata["fetched_at"] == "2026-07-27"
    assert "input_bars.json" in dataset.metadata["source"]

"""Evidence-window gate + backtest replication monitor: force long history
when it exists, 30d floor only with proof of unavailability, and flag
deploy-time edges that stop reproducing on refreshed data."""

from __future__ import annotations

import json

from wayfinder_paths.jobs.execution.validation import _evidence_window_check
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.replication import replication_job
from wayfinder_paths.jobs.store import JobStore


def _root_with_dataset(tmp_path, *, days, days_received, source="ccxt", extra=None):
    root = tmp_path
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(
            {
                "bars": [],
                "metadata": {
                    "days": days,
                    "days_received": days_received,
                    "source": source,
                    **(extra or {}),
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_evidence_window_policy_paths(tmp_path) -> None:
    # Long history -> pass.
    check = _evidence_window_check(
        _root_with_dataset(tmp_path / "a", days=120, days_received=119.5)
    )[0]
    assert check["passed"] and check["tier"] == "long_history"

    # Short but PROVEN unavailable (asked for the target) -> pass with note.
    check = _evidence_window_check(
        _root_with_dataset(tmp_path / "b", days=120, days_received=41.0)
    )[0]
    assert check["passed"] and check["tier"] == "short_history_proven"
    assert "30d floor" in check["note"]

    # Short and UNPROVEN (only asked for 14) -> fail loud with the fix.
    check = _evidence_window_check(
        _root_with_dataset(tmp_path / "c", days=14, days_received=14.0)
    )[0]
    assert not check["passed"]
    assert "--source ccxt" in check["error"]

    # VENUE shortfall is NOT proof — ccxt has the history (the Aug 2 hole:
    # a default-source refetch got 40d from the venue and passed as proven).
    check = _evidence_window_check(
        _root_with_dataset(
            tmp_path / "g", days=120, days_received=40.4, source="live_fetch"
        )
    )[0]
    assert not check["passed"]
    assert "NOT proof" in check["error"] and "--source ccxt" in check["error"]

    # Venue data with PROBED ccxt unavailability (HIP-3 symbols) -> proven.
    check = _evidence_window_check(
        _root_with_dataset(
            tmp_path / "h",
            days=120,
            days_received=52.0,
            source="live_fetch",
            extra={"ccxt_missing_markets": ["xyz:MU", "xyz:SNDK"]},
        )
    )[0]
    assert check["passed"] and check["tier"] == "short_history_proven"
    assert "no market on the long-history exchange" in check["note"]

    # Probed-missing but below the floor -> still fails (too new).
    check = _evidence_window_check(
        _root_with_dataset(
            tmp_path / "i",
            days=120,
            days_received=12.0,
            source="live_fetch",
            extra={"ccxt_missing_markets": ["NEWCOIN"]},
        )
    )[0]
    assert not check["passed"] and "floor" in check["error"]

    # Empty missing list (probe ran, all symbols exist on ccxt) -> venue
    # shortfall still rejected.
    check = _evidence_window_check(
        _root_with_dataset(
            tmp_path / "j",
            days=120,
            days_received=40.0,
            source="live_fetch",
            extra={"ccxt_missing_markets": []},
        )
    )[0]
    assert not check["passed"] and "NOT proof" in check["error"]

    # Below the floor even with the target requested -> fail.
    check = _evidence_window_check(
        _root_with_dataset(tmp_path / "d", days=120, days_received=12.0)
    )[0]
    assert not check["passed"]
    assert "floor" in check["error"]

    # Hand-written dataset (no metadata) -> non-blocking provenance note.
    root = tmp_path / "e"
    (root / "results" / "backtest").mkdir(parents=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps({"bars": []}), encoding="utf-8"
    )
    check = _evidence_window_check(root)[0]
    assert check["passed"] and check["blocking"] is False

    # No dataset at all (fixture contexts) -> no check emitted.
    assert _evidence_window_check(tmp_path / "f") == []


def test_replication_pins_baseline_and_flags_decay(tmp_path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("rep-demo", agent_mode="intervene")
    store.save(job)

    runs = [
        {  # deploy-era: strong edge
            "revision": "rev-a",
            "dataset": {"days": 120, "days_received": 119.0, "source": "ccxt"},
            "result": {
                "stats": {
                    "net_return": 0.21,
                    "avg_trade_pnl": 0.06,
                    "total_trades": 600,
                    "win_rate": 0.58,
                }
            },
        },
        {  # refreshed window: edge gone
            "revision": "rev-a",
            "dataset": {"days": 120, "days_received": 119.0, "source": "ccxt"},
            "result": {
                "stats": {
                    "net_return": -0.004,
                    "avg_trade_pnl": -0.001,
                    "total_trades": 590,
                    "win_rate": 0.5,
                }
            },
        },
        {  # new revision: baseline resets
            "revision": "rev-b",
            "dataset": {"days": 120, "days_received": 119.0, "source": "ccxt"},
            "result": {
                "stats": {
                    "net_return": 0.05,
                    "avg_trade_pnl": 0.01,
                    "total_trades": 300,
                    "win_rate": 0.55,
                }
            },
        },
    ]
    calls = {"n": 0}

    def fake_backtest(job_id, **kwargs):
        payload = runs[min(calls["n"], len(runs) - 1)]
        calls["n"] += 1
        return payload

    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job", fake_backtest
    )

    first = replication_job(job.id, store=store)
    assert first["available"] and first["decayed"] is False
    assert first["baseline"]["net_return"] == 0.21

    # Fresh cache -> no rerun without force.
    cached = replication_job(job.id, store=store)
    assert calls["n"] == 1 and cached["computed_at"] == first["computed_at"]

    # Forced rerun on the refreshed window: edge collapsed -> decayed.
    second = replication_job(job.id, store=store, force=True)
    assert second["decayed"] is True
    assert second["baseline"]["net_return"] == 0.21  # baseline pinned
    assert second["current"]["net_return"] == -0.004

    # Revision change resets the baseline; small positive edge, no decay.
    third = replication_job(job.id, store=store, force=True)
    assert third["revision"] == "rev-b"
    assert third["baseline"]["net_return"] == 0.05
    assert third["decayed"] is False


def test_replication_failure_degrades_and_journals(tmp_path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("rep-fail", agent_mode="intervene")
    store.save(job)

    def boom(job_id, **kwargs):
        raise RuntimeError("no dataset")

    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job", boom
    )
    doc = replication_job(job.id, store=store)
    assert doc["available"] is False
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "replication_failed" in journal


def _mk_dataset_job(tmp_path, symbols=("SNX",)):
    import yaml

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("inc-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    (root / "job.yaml").write_text(
        yaml.safe_dump(
            {
                "id": job.id,
                "execution_spec": {
                    "data_contract": {"bar_interval": "1h", "symbols": list(symbols)}
                },
                "execution_params": {"symbols": list(symbols)},
            }
        ),
        encoding="utf-8",
    )
    return store, job.id, root


class _CountingFeed:
    """Fake venue feed: serves a continuous hourly series ending now and
    records the lookback of every call."""

    symbol_map = {"SNX": "SNX"}

    def __init__(self):
        self.lookbacks: list[int] = []

    async def get_completed_bars(self, symbols, interval, *, lookback_bars, as_of=None):
        import pandas as pd

        from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

        self.lookbacks.append(lookback_bars)
        end = pd.Timestamp.now(tz="UTC").floor("1h")
        rows = []
        for symbol in symbols:
            for i in range(lookback_bars):
                ts = end - pd.Timedelta(hours=lookback_bars - i)
                price = 100 + (ts.value // 3_600_000_000_000) % 50
                rows.append(
                    {
                        "timestamp": ts.isoformat(),
                        "symbol": symbol,
                        "open": price,
                        "high": price + 1,
                        "low": price - 1,
                        "close": price,
                        "volume": 3.0,
                    }
                )
        return CompletedBarsView.from_rows(rows)


def test_incremental_refresh_fetches_only_the_tail(tmp_path) -> None:
    import json as _json

    from wayfinder_paths.jobs.execution.preflight import build_live_dataset

    store, job_id, root = _mk_dataset_job(tmp_path)
    feed = _CountingFeed()

    first = build_live_dataset(job_id, days=10, store=store, source="ccxt", feed=feed)
    assert feed.lookbacks[0] == 240  # full 10d of hourly bars
    bars_full = first["bars"]

    second = build_live_dataset(job_id, days=10, store=store, source="ccxt", feed=feed)
    # Tail-only: gap ~0 -> just the 2-bar overlap, not another 240.
    assert feed.lookbacks[1] <= 4
    assert second["metadata"]["incremental"] is True
    assert abs(second["bars"] - bars_full) <= 3  # merged + trimmed, no dupes
    doc = _json.loads((root / "results" / "backtest" / "input_bars.json").read_text())
    keys = [(r["timestamp"], r["symbol"]) for r in doc["bars"]]
    assert len(keys) == len(set(keys))

    # Longer window than on disk -> cannot backfill incrementally -> full.
    third = build_live_dataset(job_id, days=20, store=store, source="ccxt", feed=feed)
    assert feed.lookbacks[2] == 480
    assert "incremental" not in third["metadata"]

    # --full escape hatch.
    build_live_dataset(
        job_id, days=10, store=store, source="ccxt", feed=feed, incremental=False
    )
    assert feed.lookbacks[3] == 240


def test_incremental_provenance_mismatch_forces_full(tmp_path) -> None:
    from wayfinder_paths.jobs.execution.preflight import build_live_dataset

    store, job_id, root = _mk_dataset_job(tmp_path)
    feed = _CountingFeed()
    build_live_dataset(job_id, days=5, store=store, source="ccxt", feed=feed)

    # Different exchange in stored metadata -> full refetch.
    import json as _json

    path = root / "results" / "backtest" / "input_bars.json"
    doc = _json.loads(path.read_text())
    doc["metadata"]["exchange"] = "bybit"
    path.write_text(_json.dumps(doc), encoding="utf-8")
    build_live_dataset(job_id, days=5, store=store, source="ccxt", feed=feed)
    assert feed.lookbacks[1] == 120  # full 5d again


def test_replication_window_change_repins_not_decays(tmp_path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("rep-window", agent_mode="intervene")
    store.save(job)
    runs = [
        {  # 120d baseline, strong
            "revision": "rev-a",
            "dataset": {"days": 120, "days_received": 119.0, "source": "ccxt"},
            "result": {
                "stats": {
                    "net_return": 0.20,
                    "avg_trade_pnl": 0.006,
                    "total_trades": 1300,
                    "win_rate": 0.51,
                }
            },
        },
        {  # window collapsed to 40d — NOT decay; re-pin
            "revision": "rev-a",
            "dataset": {"days": 120, "days_received": 40.0, "source": "live_fetch"},
            "result": {
                "stats": {
                    "net_return": 0.008,
                    "avg_trade_pnl": 0.007,
                    "total_trades": 226,
                    "win_rate": 0.52,
                }
            },
        },
    ]
    calls = {"n": 0}

    def fake_backtest(job_id, **kwargs):
        payload = runs[min(calls["n"], len(runs) - 1)]
        calls["n"] += 1
        return payload

    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job", fake_backtest
    )
    first = replication_job(job.id, store=store)
    assert first["baseline"]["dataset_days"] == 119.0
    second = replication_job(job.id, store=store, force=True)
    assert second["decayed"] is False  # window change, not edge decay
    assert second["baseline"]["dataset_days"] == 40.0  # re-pinned

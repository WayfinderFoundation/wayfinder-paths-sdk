"""Evidence-window gate + backtest replication monitor: force long history
when it exists, 30d floor only with proof of unavailability, and flag
deploy-time edges that stop reproducing on refreshed data."""

from __future__ import annotations

import json

from wayfinder_paths.jobs.execution.validation import _evidence_window_check
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.replication import replication_job
from wayfinder_paths.jobs.store import JobStore


def _root_with_dataset(tmp_path, *, days, days_received):
    root = tmp_path
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(
            {
                "bars": [],
                "metadata": {"days": days, "days_received": days_received},
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

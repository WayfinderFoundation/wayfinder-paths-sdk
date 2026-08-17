"""Wake productivity: matured promotion verdicts fire a trigger wake, and
research staleness is a visible ledger metric — the machinery audit found
~450 healthy wakes across a 10-day proposal/experiment drought with the
archive/lineage instrumentation never fired (starved upstream)."""

from __future__ import annotations

import json

import pandas as pd

from wayfinder_paths.jobs.counterfactual import _maybe_record_promotion_verdict
from wayfinder_paths.jobs.evolution_ledger import build_evolution_report
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS


def _job(tmp_path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("wp-demo", agent_mode="intervene")
    store.save(job)
    return store, job.id


def _mature_artifact(proposal_id: str = "prop-m") -> dict:
    return {
        "available": True,
        "proposal_id": proposal_id,
        "window": {"days": 5.0},
        "actual": {"closes": 6, "net_pnl": 0.1},
        "shadow": {"closes": 6, "net_pnl": 0.1},
        "delta_net_pnl": 0.0,
    }


def test_verdict_matured_is_always_wake_event() -> None:
    assert "verdict_matured" in ALWAYS_WAKE_EVENTS


def test_matured_verdict_fires_trigger(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path)
    fired: list[tuple[str, list[str], str]] = []

    def fake_fire(store_arg, job, events, *, source):
        fired.append((job.id, events, source))
        return {"queued": True}

    monkeypatch.setattr("wayfinder_paths.jobs.triggers.fire_triggers", fake_fire)

    # Immature verdict -> pending, no trigger.
    immature = dict(_mature_artifact("prop-young"))
    immature["window"] = {"days": 0.5}
    immature["actual"] = {"closes": 0, "net_pnl": 0.0}
    immature["shadow"] = {"closes": 0, "net_pnl": 0.0}
    _maybe_record_promotion_verdict(store, job_id, immature)
    assert fired == []

    # Matured (neutral) verdict -> trigger fires once.
    _maybe_record_promotion_verdict(store, job_id, _mature_artifact("prop-m"))
    assert fired == [(job_id, ["verdict_matured"], "promotion_verdict")]

    # Re-processing the same matured verdict does not re-fire.
    _maybe_record_promotion_verdict(store, job_id, _mature_artifact("prop-m"))
    assert len(fired) == 1

    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-m"]["verdict"] == "neutral"


def test_trigger_failure_never_breaks_verdict_bookkeeping(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("trigger plumbing down")

    monkeypatch.setattr("wayfinder_paths.jobs.triggers.fire_triggers", boom)
    _maybe_record_promotion_verdict(store, job_id, _mature_artifact("prop-x"))
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-x"]["verdict"] == "neutral"


def test_research_staleness_metrics(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)

    # No proposals/experiments yet: staleness is unknowable, not zero.
    report = build_evolution_report(store, job_id)
    staleness = report["research_staleness"]
    assert staleness["days_since_last_proposal"] is None
    assert staleness["days_since_last_experiment"] is None
    assert staleness["wakes_since_last_proposal"] is None

    five_days_ago = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=5)).isoformat()
    two_days_ago = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).isoformat()

    proposals_dir = root / "proposals"
    proposals_dir.mkdir(exist_ok=True)
    (proposals_dir / "p1.json").write_text(
        json.dumps({"proposal_id": "p1", "created_at": five_days_ago})
    )
    experiments = root / "results" / "backtest" / "experiments.jsonl"
    experiments.parent.mkdir(parents=True, exist_ok=True)
    experiments.write_text(json.dumps({"ts": two_days_ago, "grid_id": "g1"}) + "\n")

    # 3 wakes after the proposal, 1 before.
    journal = root / "journal.jsonl"
    rows = [
        {
            "type": "agent_wakeup",
            "ts": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=6)).isoformat(),
        },
        {
            "type": "agent_wakeup",
            "ts": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=4)).isoformat(),
        },
        {
            "type": "agent_wakeup",
            "ts": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).isoformat(),
        },
        {"type": "agent_wakeup", "ts": utc_now_iso()},
        {"type": "feature_store_compacted", "ts": utc_now_iso()},
    ]
    with journal.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    report = build_evolution_report(store, job_id)
    staleness = report["research_staleness"]
    assert staleness["days_since_last_proposal"] == pytest_approx(5.0)
    assert staleness["days_since_last_experiment"] == pytest_approx(2.0)
    assert staleness["wakes_since_last_proposal"] == 3


def pytest_approx(value: float):
    import pytest

    return pytest.approx(value, abs=0.1)


def test_snapshot_block_renders_staleness(tmp_path) -> None:
    from wayfinder_paths.jobs.evolution_ledger import evolution_snapshot_block

    store, job_id = _job(tmp_path)
    block = evolution_snapshot_block(store, job_id)
    assert "research_staleness" in block
    # Branch-revival lane needs the missed-opportunity signal in the wake
    # context, not buried in the full report.
    assert "opportunity_recall" in block


def test_worker_stable_rules_carry_research_mandate(tmp_path) -> None:
    import inspect

    from wayfinder_paths.jobs import worker

    source = inspect.getsource(worker)
    assert "verdict_matured" in source
    assert "research_staleness" in source
    assert "NOT a complete wake" in source

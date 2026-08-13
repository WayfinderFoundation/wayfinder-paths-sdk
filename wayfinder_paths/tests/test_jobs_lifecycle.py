"""PR B: typed predicates, candidate archive + Pareto frontier, and the
mechanical lifecycle controller."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.archive import (
    archive_snapshot_block,
    load_archive,
    record_candidate,
    set_candidate_status,
    set_incumbent,
)
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.predicates import evaluate_predicates, forward_metrics
from wayfinder_paths.jobs.probation import load_probation, record_probation_leg
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.watchdog import _run_lifecycle_pass


def _store(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "lifecycle-demo",
        goal="Mechanical rules.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def test_predicates_ops_prerequisites_and_missing_metrics() -> None:
    rules = {"min_closed_trades": 10, "win_rate__lt": 0.2, "drawdown_usd__gte": 5.0}
    # Prerequisite unmet → pending, never met/not_met.
    pending = evaluate_predicates(rules, {"closed_trades": 4, "win_rate": 0.1})
    assert pending["status"] == "pending" and "closed_trades" in pending["waiting_on"]
    # Both conditions hold → met.
    met = evaluate_predicates(
        rules, {"closed_trades": 12, "win_rate": 0.15, "drawdown_usd": 6.0}
    )
    assert met["status"] == "met"
    # One fails → not_met.
    not_met = evaluate_predicates(
        rules, {"closed_trades": 12, "win_rate": 0.5, "drawdown_usd": 6.0}
    )
    assert not_met["status"] == "not_met"
    # Missing metric → pending: silence never satisfies a rule.
    missing = evaluate_predicates(rules, {"closed_trades": 12, "win_rate": 0.1})
    assert missing["status"] == "pending" and missing["missing"]
    # No rules registered → pending (legacy prose leg).
    assert evaluate_predicates(None, {})["status"] == "pending"
    assert evaluate_predicates({}, {})["status"] == "pending"


def test_forward_metrics_filters_and_measures() -> None:
    trades = [
        {"symbol": "IMX", "timestamp": "2026-08-01T00:00:00+00:00", "net_pnl": -1.0},
        {"symbol": "IMX", "timestamp": "2026-08-02T00:00:00+00:00", "net_pnl": 2.0},
        {"symbol": "OP", "timestamp": "2026-08-03T00:00:00+00:00", "net_pnl": -3.0},
        {"symbol": "IMX", "timestamp": "2026-08-04T00:00:00+00:00", "net_pnl": -0.5},
        # Before the leg deployed — must be excluded.
        {"symbol": "IMX", "timestamp": "2026-07-01T00:00:00+00:00", "net_pnl": 9.0},
    ]
    metrics = forward_metrics(
        trades,
        symbol="IMX",
        since="2026-07-15T00:00:00+00:00",
        now_iso="2026-08-05T00:00:00+00:00",
    )
    assert metrics["closed_trades"] == 3
    assert metrics["win_rate"] == pytest.approx(1 / 3)
    assert metrics["net_pnl"] == pytest.approx(0.5)
    assert metrics["loss_streak"] == 1
    assert metrics["days"] == pytest.approx(21.0)
    assert metrics["drawdown_usd"] == pytest.approx(1.0)


def test_archive_frontier_and_status_flow(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    vec = lambda g, d, t_, dd: {  # noqa: E731
        "net_log_growth": g,
        "downside_deviation": d,
        "tail_loss": t_,
        "max_drawdown_pct": dd,
    }
    record_candidate(
        store, job_id, candidate_id="c-strong", family="params",
        summary="high growth low risk", status="archived",
        objective=vec(0.10, 0.01, 0.02, 0.05),
    )
    record_candidate(
        store, job_id, candidate_id="c-dominated", family="params",
        summary="worse everywhere", status="archived",
        objective=vec(0.05, 0.02, 0.03, 0.08),
    )
    record_candidate(
        store, job_id, candidate_id="c-tradeoff", family="code",
        summary="less growth, less risk", status="archived",
        objective=vec(0.06, 0.005, 0.01, 0.03),
    )
    doc = load_archive(store, job_id)
    frontier = {e["candidate_id"] for e in doc["candidates"] if e["on_frontier"]}
    assert frontier == {"c-strong", "c-tradeoff"}

    # Refuted entries leave the frontier but never the archive; c-dominated
    # stays off it because c-strong beats it on every axis.
    set_candidate_status(store, job_id, "c-tradeoff", "refuted", evidence="0/3 folds")
    doc = load_archive(store, job_id)
    frontier = {e["candidate_id"] for e in doc["candidates"] if e["on_frontier"]}
    assert frontier == {"c-strong"}
    assert len(doc["candidates"]) == 3

    set_incumbent(store, job_id, "c-strong")
    doc = load_archive(store, job_id)
    assert next(
        e for e in doc["candidates"] if e["candidate_id"] == "c-strong"
    )["status"] == "incumbent"

    # Promoting another demotes the previous incumbent to an archived branch.
    set_incumbent(store, job_id, "c-dominated")
    doc = load_archive(store, job_id)
    statuses = {e["candidate_id"]: e["status"] for e in doc["candidates"]}
    assert statuses["c-strong"] == "archived"
    assert statuses["c-dominated"] == "incumbent"

    block = archive_snapshot_block(store, job_id)
    assert block["counts"]["refuted"] == 1
    assert any(r["candidate_id"] == "c-tradeoff" for r in block["recent_refuted"])
    with pytest.raises(ValueError):
        set_candidate_status(store, job_id, "c-strong", "bogus-status")


def _write_trades(store: JobStore, job_id: str, rows: list[dict]) -> None:
    path = store.job_dir(job_id) / "results" / "forward" / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def test_lifecycle_controller_kills_on_typed_rules(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    record_probation_leg(
        store,
        job_id,
        name="op-leg",
        symbol="OP",
        size_fraction=0.25,
        graduate_criterion="Sharpe > 1 after 20 trades",
        kill_criterion="WR < 20% after 10 trades",
        graduate_rules={"min_closed_trades": 20, "win_rate__gte": 0.5},
        kill_rules={"min_closed_trades": 10, "win_rate__lt": 0.2},
    )
    deployed = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    doc = load_probation(store, job_id)
    doc["legs"][0]["deployed_at"] = deployed
    store.write_json(job_id, "probation.json", doc)

    losing = [
        {
            "symbol": "OP",
            "timestamp": (
                datetime.now(UTC) - timedelta(days=9) + timedelta(hours=i)
            ).isoformat(),
            "net_pnl": -1.0 if i % 10 else 1.0,
        }
        for i in range(12)
    ]
    _write_trades(store, job_id, losing)

    events = _run_lifecycle_pass(store, job_id, datetime.now(UTC))
    assert events and events[0]["action"] == "lifecycle_killed"
    doc = load_probation(store, job_id)
    assert doc["legs"][0]["status"] == "killed"
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "lifecycle_decision" in journal

    # Stamp-gated: an immediate second pass does nothing.
    assert _run_lifecycle_pass(store, job_id, datetime.now(UTC)) == []


def test_lifecycle_controller_pending_below_sample_gate(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    record_probation_leg(
        store,
        job_id,
        name="young-leg",
        symbol="OP",
        size_fraction=0.25,
        graduate_criterion="prose",
        kill_criterion="prose",
        kill_rules={"min_closed_trades": 10, "win_rate__lt": 0.2},
    )
    _write_trades(
        store,
        job_id,
        [
            {"symbol": "OP", "timestamp": utc_now_iso(), "net_pnl": -1.0}
            for _ in range(3)
        ],
    )
    events = _run_lifecycle_pass(store, job_id, datetime.now(UTC))
    assert events == []
    doc = load_probation(store, job_id)
    leg = doc["legs"][0]
    assert leg["status"] == "active"
    # But the controller left visible progress on the leg.
    assert "kill=pending" in (leg["graduate"]["progress"] or "")

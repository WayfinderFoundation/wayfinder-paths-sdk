"""Economic promotion gate: objective math, readiness policy, constitution
loading, enforcement, promotion verdicts, and the evolution ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.constitution import (
    DEFAULT_CONSTITUTION,
    load_constitution,
)
from wayfinder_paths.jobs.counterfactual import _maybe_record_promotion_verdict
from wayfinder_paths.jobs.economics import (
    block_bootstrap_lcb,
    daily_log_returns,
    evaluate_economic_readiness,
    objective_vector,
    paired_daily_deltas,
    utility,
)
from wayfinder_paths.jobs.evolution_ledger import build_evolution_report
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _equity(values: list[float], start_day: int = 1) -> list[dict]:
    return [
        {"timestamp": f"2026-08-{start_day + i:02d}T00:00:00+00:00", "equity": value}
        for i, value in enumerate(values)
    ]


def test_objective_vector_prefers_downside_over_variance() -> None:
    # Upside volatility must not be punished: same growth, one path swings UP.
    smooth = objective_vector(_equity([100, 101, 102, 103, 104]), trades=[])
    spiky_up = objective_vector(_equity([100, 103, 102.5, 103.5, 104]), trades=[])
    assert smooth["downside_deviation"] < 0.01
    assert spiky_up["net_log_growth"] == pytest.approx(
        smooth["net_log_growth"], abs=1e-9
    )
    # The up-spike path has one small down day; downside dev stays tiny.
    assert spiky_up["downside_deviation"] < 0.01


def test_objective_vector_tail_and_fees() -> None:
    trades = [{"pnl": -5.0, "fee": 0.5}, {"pnl": 2.0, "fee": 0.5}, {"pnl": -1.0, "fee": 0.5}]
    vector = objective_vector(_equity([100, 99, 98, 100]), trades)
    assert vector["tail_loss"] == pytest.approx(5.0 / 100)
    assert vector["fee_load"] == pytest.approx(1.5 / 100)
    assert vector["max_drawdown_pct"] == pytest.approx(0.02)
    assert vector["trade_count"] == 3


def test_utility_applies_owner_weights() -> None:
    vector = {
        "net_log_growth": 0.10,
        "downside_deviation": 0.02,
        "tail_loss": 0.05,
        "fee_load": 0.01,
    }
    weights = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}
    assert utility(vector, weights) == pytest.approx(0.10 - 0.01 - 0.05 - 0.0025)


def test_paired_deltas_align_on_common_days() -> None:
    baseline = daily_log_returns(_equity([100, 101, 102, 103]))
    candidate = daily_log_returns(_equity([100, 102, 103, 105]))
    deltas = paired_daily_deltas(baseline, candidate)
    assert len(deltas) == 3
    assert sum(deltas) > 0


def test_block_bootstrap_lcb_is_deterministic_and_ordered() -> None:
    up = [0.01] * 30
    lcb_up = block_bootstrap_lcb(up, block_len=5, iterations=200, confidence=0.9)
    assert lcb_up == pytest.approx(0.30, abs=1e-9)
    mixed = [0.01, -0.012] * 15
    lcb_mixed = block_bootstrap_lcb(mixed, block_len=5, iterations=200, confidence=0.9)
    assert lcb_mixed is not None and lcb_mixed < lcb_up
    again = block_bootstrap_lcb(mixed, block_len=5, iterations=200, confidence=0.9)
    assert again == lcb_mixed
    assert block_bootstrap_lcb([0.01], block_len=5, iterations=50, confidence=0.9) is None


def _ok_report(**overrides) -> dict:
    report = {
        "status": "ok",
        "objective": {
            "candidate": {
                "max_drawdown_pct": 0.05,
                "tail_loss": 0.02,
                "trade_count": 20,
            },
        },
        "positive_folds": 3,
        "fold_count": 4,
        "paired_incumbent_delta": {"estimate": 0.02, "lcb": 0.005, "confidence": 0.9},
        "audit_slice": {"delta_utility": 0.001},
    }
    report.update(overrides)
    return report


def test_readiness_passes_and_each_reason_blocks() -> None:
    constitution = json.loads(json.dumps(DEFAULT_CONSTITUTION))
    assert evaluate_economic_readiness(_ok_report(), constitution)["ready"] is True

    weak_lcb = _ok_report(
        paired_incumbent_delta={"estimate": 0.02, "lcb": -0.001, "confidence": 0.9}
    )
    result = evaluate_economic_readiness(weak_lcb, constitution)
    assert result["ready"] is False and "LCB" in result["reasons"][0]

    # Probation bar: LCB exempt, point estimate rules.
    assert (
        evaluate_economic_readiness(weak_lcb, constitution, probation=True)["ready"]
        is True
    )
    negative = _ok_report(
        paired_incumbent_delta={"estimate": -0.01, "lcb": -0.02, "confidence": 0.9}
    )
    assert (
        evaluate_economic_readiness(negative, constitution, probation=True)["ready"]
        is False
    )

    breach = _ok_report()
    breach["objective"]["candidate"]["max_drawdown_pct"] = 0.60
    assert evaluate_economic_readiness(breach, constitution)["ready"] is False

    few_folds = _ok_report(positive_folds=1)
    assert evaluate_economic_readiness(few_folds, constitution)["ready"] is False

    bad_audit = _ok_report(audit_slice={"delta_utility": -0.5})
    result = evaluate_economic_readiness(bad_audit, constitution)
    assert result["ready"] is False and "audit-slice" in result["reasons"][0]

    unavailable = {"status": "insufficient_history", "detail": "too few bars"}
    assert evaluate_economic_readiness(unavailable, constitution)["ready"] is False


def test_constitution_defaults_file_and_broken(tmp_path: Path) -> None:
    defaults = load_constitution(tmp_path)
    assert defaults["enforcement"] == "advisory" and defaults["source"] == "defaults"

    (tmp_path / "constitution.yaml").write_text(
        "enforcement: blocking\npromotion:\n  min_oos_trades: 30\n", encoding="utf-8"
    )
    loaded = load_constitution(tmp_path)
    assert loaded["enforcement"] == "blocking"
    assert loaded["promotion"]["min_oos_trades"] == 30
    # Unspecified keys inherit defaults; revision is stamped.
    assert loaded["promotion"]["required_positive_folds"] == 2
    assert loaded["revision"]

    (tmp_path / "constitution.yaml").write_text(":\n  - broken", encoding="utf-8")
    assert load_constitution(tmp_path)["enforcement"] == "advisory"


def _store_with_job(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "econ-gate-demo",
        goal="Beat the incumbent honestly.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def test_enforcement_blocks_only_computed_false_under_blocking(tmp_path: Path) -> None:
    store, job_id = _store_with_job(tmp_path)
    base = {
        "revision": "cand111",
        "validation_summary": {"status": "passed"},
        "gate": {"live_ready": True, "reasons": []},
        "mode": "full",
    }

    def proposal_with(economic) -> dict:
        return {
            "proposal_id": "prop-x",
            "candidate_report": {**base, "economic": economic},
            "application": {"status": "not_requested"},
        }

    blocking_false = {
        "enforcement": "blocking",
        "ready": False,
        "reasons": ["paired utility delta LCB -0.0100 not > 0 at 90% confidence"],
    }
    with pytest.raises(ValueError, match="not economic-ready"):
        store._ensure_candidate_report_gate(
            job_id, proposal_with(blocking_false), allow_ungated=False
        )

    for economic in (
        {"enforcement": "advisory", "ready": False, "reasons": ["weak"]},
        {"enforcement": "blocking", "ready": None, "reasons": ["unavailable"]},
        {"enforcement": "blocking", "ready": True, "reasons": []},
        None,
    ):
        try:
            store._ensure_candidate_report_gate(
                job_id, proposal_with(economic), allow_ungated=False
            )
        except ValueError as exc:
            # The freshness guard may complain about the missing candidate
            # bundle in this fixture — economic enforcement must not.
            assert "economic-ready" not in str(exc)


def test_promotion_verdict_records_once_when_mature(tmp_path: Path) -> None:
    store, job_id = _store_with_job(tmp_path)

    immature = {
        "available": True,
        "proposal_id": "prop-young",
        "window": {"days": 1.0},
        "actual": {"closes": 5, "net_pnl": 1.0},
        "shadow": {"closes": 5, "net_pnl": 0.0},
        "delta_net_pnl": 1.0,
    }
    _maybe_record_promotion_verdict(store, job_id, immature)
    assert store.read_json(job_id, "state/promotion_verdicts.json") is None

    mature = {
        "available": True,
        "proposal_id": "prop-mature",
        "window": {"days": 5.5},
        "actual": {"closes": 8, "net_pnl": 3.0},
        "shadow": {"closes": 7, "net_pnl": 1.0},
        "delta_net_pnl": 2.0,
    }
    _maybe_record_promotion_verdict(store, job_id, mature)
    _maybe_record_promotion_verdict(store, job_id, mature)
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-mature"]["verdict"] == "beat"
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    assert journal.count("promotion_verdict") == 1

    hurt = {**mature, "proposal_id": "prop-hurt", "delta_net_pnl": -2.0}
    _maybe_record_promotion_verdict(store, job_id, hurt)
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-hurt"]["verdict"] == "hurt"


def test_evolution_report_aggregates_path_and_reliability(tmp_path: Path) -> None:
    store, job_id = _store_with_job(tmp_path)
    proposals_dir = store.job_dir(job_id) / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    fixtures = [
        {
            "proposal_id": "prop-a",
            "status": "approved",
            "created_at": "2026-08-01T00:00:00+00:00",
            "proposed_change": {"summary": "widen stop", "execution_params": {"x": 1}},
            "application": {"status": "applied"},
        },
        {
            "proposal_id": "prop-b",
            "status": "rejected",
            "created_at": "2026-08-02T00:00:00+00:00",
            "proposed_change": {"summary": "bad idea", "execution_params": {"x": 2}},
            "rejection": {"by": "agent", "reason": "gate failed"},
            "application": {"status": "not_requested"},
        },
        {
            "proposal_id": "prop-c",
            "status": "rejected",
            "created_at": "2026-08-03T00:00:00+00:00",
            "proposed_change": {"summary": "owner said no"},
            "rejection": {"by": "owner", "reason": "too risky"},
            "application": {"status": "not_requested"},
        },
    ]
    for fixture in fixtures:
        (proposals_dir / f"{fixture['proposal_id']}.json").write_text(
            json.dumps(fixture), encoding="utf-8"
        )
    store.write_json(
        job_id,
        "state/promotion_verdicts.json",
        {"prop-a": {"verdict": "beat", "delta_net_pnl": 2.0}},
    )

    report = build_evolution_report(store, job_id)
    assert report["proposals_total"] == 3
    assert report["by_family"]["params"]["promoted"] == 1
    assert report["by_family"]["params"]["agent_rejected"] == 1
    assert report["by_family"]["code"]["owner_rejected"] == 1
    reliability = report["promotion_reliability"]
    assert reliability["beat"] == 1 and reliability["beat_rate"] == 1.0
    promoted_row = next(r for r in report["proposals"] if r["proposal_id"] == "prop-a")
    assert promoted_row["forward_verdict"] == "beat"

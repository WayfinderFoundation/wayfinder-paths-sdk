"""Economic promotion gate: objective math, readiness policy, constitution
loading, enforcement, promotion verdicts, and the evolution ledger."""

from __future__ import annotations

import json
import math
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
    trades = [
        {"pnl": -5.0, "fee": 0.5},
        {"pnl": 2.0, "fee": 0.5},
        {"pnl": -1.0, "fee": 0.5},
    ]
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
    assert (
        block_bootstrap_lcb([0.01], block_len=5, iterations=50, confidence=0.9) is None
    )


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


def test_regime_specialist_gate_uses_target_edge_and_bounds_outside_loss() -> None:
    constitution = json.loads(json.dumps(DEFAULT_CONSTITUTION))
    report = _ok_report(
        regime_contract={
            "enabled": True,
            "target_regimes": ["up_highvol"],
            "objective": {
                "candidate": {
                    # A transition strategy can enter before the target cell;
                    # whole-strategy activity, not in-cell entries, is binding.
                    "target": {"trade_count": 0, "day_count": 12},
                    "target_utility": 0.04,
                    "outside": {"loss_pct": 0.01},
                }
            },
            "paired_target_delta": {
                "estimate": 0.03,
                "lcb": 0.005,
                "confidence": 0.9,
            },
            "audit": {
                "candidate": {
                    "target_utility": 0.01,
                    "outside": {"loss_pct": 0.005},
                },
                "delta_utility": 0.002,
            },
        }
    )

    assert evaluate_economic_readiness(report, constitution)["ready"] is True
    report["regime_contract"]["objective"]["candidate"]["outside"]["loss_pct"] = 0.03
    result = evaluate_economic_readiness(report, constitution)
    assert result["ready"] is False
    assert any("out-of-regime loss" in reason for reason in result["reasons"])

    # In-regime edge never licenses replacing a better whole-strategy incumbent.
    inferior = _ok_report(
        regime_contract=report["regime_contract"],
        paired_incumbent_delta={"estimate": -0.01, "lcb": -0.02, "confidence": 0.9},
    )
    inferior["regime_contract"]["objective"]["candidate"]["outside"]["loss_pct"] = 0.01
    result = evaluate_economic_readiness(inferior, constitution)
    assert result["ready"] is False
    assert any("inferior overall" in reason for reason in result["reasons"])


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
    # Immature windows now TRACK as pending (so censoring has a target)
    # instead of leaving no record.
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-young"]["verdict"] == "pending"

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
    # The maturing promotion CENSORS the still-pending prior verdict.
    assert verdicts["prop-young"]["verdict"] == "censored_by_next_change"
    assert verdicts["prop-young"]["censored_by"] == "prop-mature"
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    # Two journal entries — the censor record + the beat verdict — and the
    # repeated call added neither twice (dedup holds).
    # Count typed rows exactly — trigger wakes fired by matured verdicts
    # reference source="promotion_verdict" in their own journal rows.
    assert journal.count('"type": "promotion_verdict"') == 2

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


def test_verdict_ages_out_to_insufficient_evidence(tmp_path: Path) -> None:
    """A promotion whose window exceeds maximum_days without reaching the
    trade floor finalizes as insufficient_evidence — never a fake neutral."""
    store, job_id = _store_with_job(tmp_path)
    aged = {
        "available": True,
        "proposal_id": "prop-sparse",
        "window": {"days": 45.0},
        "actual": {"closes": 1, "net_pnl": 0.2},
        "shadow": {"closes": 1, "net_pnl": 0.1},
        "delta_net_pnl": 0.1,
    }
    _maybe_record_promotion_verdict(store, job_id, aged)
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json")
    assert verdicts["prop-sparse"]["verdict"] == "insufficient_evidence"


def test_trial_lineage_records_every_run(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.execution.experiments import record_trial_lineage

    store, job_id = _store_with_job(tmp_path)
    payload = {
        "revision": "abc123",
        "result": {
            "grid_id": "g1",
            "optimizer": "grid",
            "rank_by": "net_return",
            "runs": [
                {
                    "run_id": f"r{i}",
                    "params": {"x": i},
                    "stats": {
                        "net_return": i / 100,
                        "trade_count": 10 + i,
                        "win_rate": 0.5,
                        "max_drawdown_pct": 0.05,
                    },
                }
                for i in range(7)
            ],
        },
    }
    recorded = record_trial_lineage(job_id, payload, store=store)
    assert recorded == 7
    path = store.job_dir(job_id) / "results" / "backtest" / "trials.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 7
    assert rows[3]["behavior"]["trade_count"] == 13.0
    assert rows[3]["rank_metric"] == 0.03


def test_evolution_reliability_ci_and_opportunity_recall(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.archive import record_candidate, set_incumbent
    from wayfinder_paths.jobs.evolution_ledger import build_evolution_report

    store, job_id = _store_with_job(tmp_path)
    store.write_json(
        job_id,
        "state/promotion_verdicts.json",
        {
            "p1": {"verdict": "beat", "delta_net_pnl": 2.0},
            "p2": {"verdict": "hurt", "delta_net_pnl": -1.0},
            "p3": {"verdict": "pending"},
        },
    )
    vec = lambda g: {  # noqa: E731
        "net_log_growth": g,
        "downside_deviation": 0.01,
        "tail_loss": 0.01,
        "max_drawdown_pct": 0.05,
    }
    record_candidate(
        store,
        job_id,
        candidate_id="inc",
        family="params",
        summary="incumbent",
        status="archived",
        objective=vec(0.02),
    )
    set_incumbent(store, job_id, "inc")
    record_candidate(
        store,
        job_id,
        candidate_id="better",
        family="params",
        summary="unpromoted better",
        status="archived",
        objective=vec(0.09),
    )

    report = build_evolution_report(store, job_id)
    reliability = report["promotion_reliability"]
    assert reliability["judged"] == 2
    assert reliability["pending"] == 1
    low, high = reliability["beat_rate_ci95"]
    assert 0.0 <= low < 0.5 < high <= 1.0  # n=2: wide interval, honest
    recall = report["opportunity_recall"]
    assert recall["missed"] is True and recall["best_candidate_id"] == "better"


def test_benchmark_constitution_profile_loads_strict() -> None:
    from wayfinder_paths.jobs.constitution import load_benchmark_constitution

    profile = load_benchmark_constitution()
    assert profile["enforcement"] == "blocking"
    assert profile["evaluation"]["confidence"] == 0.95
    assert profile["promotion"]["probation_requires_lcb"] is True
    assert profile["verdict"]["maximum_days"] == 30.0
    assert profile["revision"]


def test_chained_fold_equity_removes_restart_artifacts() -> None:
    from wayfinder_paths.jobs.economics import _chain_fold_equity

    def fold(values: list[float], start_day: int) -> list[dict[str, object]]:
        return [
            {
                "timestamp": f"2026-01-{start_day + index:02d}T00:00:00+00:00",
                "equity": v,
            }
            for index, v in enumerate(values)
        ]

    fold_a = fold([10_000.0, 12_000.0], start_day=1)
    fold_b = fold([10_000.0, 12_000.0], start_day=3)
    naive = objective_vector([*fold_a, *fold_b], trades=[])
    # Today's artifacts: the restart reads as a loss and a carried drawdown.
    assert naive["max_drawdown_pct"] == pytest.approx(1 / 6)
    assert naive["net_log_growth"] == pytest.approx(math.log(1.2))
    pool: list[dict[str, object]] = []
    for piece in (fold_a, fold_b):
        pool.extend(_chain_fold_equity(pool, piece))
    chained = objective_vector(pool, trades=[])
    assert chained["max_drawdown_pct"] == pytest.approx(0.0)
    assert chained["net_log_growth"] == pytest.approx(2 * math.log(1.2))
    assert pool[-1]["equity"] == pytest.approx(14_400.0)
    assert _chain_fold_equity([], fold_a) == fold_a


def test_readiness_applies_the_trial_haircut_outside_probation() -> None:
    from wayfinder_paths.jobs.gating import _gate_haircut

    constitution = json.loads(json.dumps(DEFAULT_CONSTITUTION))
    below = {"trials": 21, "t_stat": 1.1, "expected_max_t": 1.9, "cleared": False}
    result = evaluate_economic_readiness(_ok_report(), constitution, haircut=below)
    assert (
        result["ready"] is False and "21-trial expected maximum" in result["reasons"][0]
    )
    # Probation is the forward certificate: the haircut is recorded, not binding.
    assert (
        evaluate_economic_readiness(
            _ok_report(), constitution, probation=True, haircut=below
        )["ready"]
        is True
    )
    unknown = {"trials": 21, "t_stat": None, "expected_max_t": 1.9, "cleared": None}
    assert (
        evaluate_economic_readiness(_ok_report(), constitution, haircut=unknown)[
            "ready"
        ]
        is True
    )
    assert _gate_haircut({"status": "ok"}, None) is None
    cleared = _gate_haircut(
        {"status": "ok", "paired_incumbent_delta": {"t_stat": 2.5}}, 21
    )
    assert cleared["cleared"] is True and cleared["trials"] == 21
    assert (
        _gate_haircut({"status": "ok", "paired_incumbent_delta": {"t_stat": 1.0}}, 21)[
            "cleared"
        ]
        is False
    )

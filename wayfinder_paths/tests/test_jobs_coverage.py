from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.coverage import (
    audit_exhaustion_claim,
    build_coverage_certificate,
)
from wayfinder_paths.jobs.evolution_ledger import (
    build_evolution_report,
    build_process_efficiency,
    evolution_snapshot_block,
)
from wayfinder_paths.jobs.exhaustion import (
    audit_and_adjudicate_exhaustion_claim,
    claim_settles_lane,
    file_exhaustion_claim,
    reopen_exhaustion_claim,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job
from wayfinder_paths.jobs.watchdog import recover_stalled_applications


def _store(tmp_path: Path, job_id: str = "coverage-demo") -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def _append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _scan_meta(
    *,
    scan_id: str = "scan-1",
    signals: list[str] | None = None,
    horizons: list[int] | None = None,
) -> dict:
    signals = signals or ["sig_a"]
    horizons = horizons or [1]
    return {
        "ts": "2026-08-20T00:00:00+00:00",
        "kind": "scan_meta",
        "scan_id": scan_id,
        "symbols": ["BTC"],
        "timeframes": ["1h"],
        "horizons": {"BTC": {"1h": horizons}},
        "declared_signals": signals,
        "signal_families": dict.fromkeys(signals, "momentum"),
        "declared_regimes": ["base"],
        "bh_family_size": 50,
        "bh_min_family_size": 50,
        "multiplicity_method": "benjamini_hochberg",
        "bh_family_mode": "canonical",
        "cost_revision": {"round_trip_cost_bps": 17.0},
        "incumbent_controls": [
            {"symbol": "BTC", "signal": signals[0], "timeframe": "1h", "horizon": 1}
        ],
    }


def _scan_test(
    *,
    signal: str = "sig_a",
    horizon: int = 1,
    verdict: str | None = None,
    status: str | None = None,
) -> dict:
    row = {
        "ts": "2026-08-20T00:01:00+00:00",
        "kind": "scan_test" if status is None else "scan_cell",
        "scan_id": "scan-1",
        "symbol": "BTC",
        "signal": signal,
        "family": "momentum",
        "library": "canonical",
        "timeframe": "1h",
        "horizon": horizon,
        "regime": None,
        "t": -0.5,
        "t_net": -1.2,
        "round_trip_cost_bps": 17.0,
        "min_detectable_edge_bps": 12.0,
        "verdict": verdict,
    }
    if status is not None:
        row["status"] = status
        row["reason"] = "synthetic gap"
    return row


def _write_scan(store: JobStore, job_id: str, rows: list[dict]) -> None:
    _append_rows(
        store.job_dir(job_id) / "results/research/signal_scan/ledger.jsonl", rows
    )


def test_certificate_counts_declared_gaps_without_laundering_negative(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    _write_scan(
        store,
        job_id,
        [
            _scan_meta(signals=["sig_a", "sig_b"], horizons=[1, 2]),
            _scan_test(signal="sig_a", horizon=1),
            _scan_test(signal="sig_b", horizon=1),
            _scan_test(signal="sig_a", horizon=2, status="blocked_infrastructure"),
        ],
    )

    certificate = build_coverage_certificate(job_id, "majors", store=store)
    assert certificate["cell_counts"] == {
        "completed_valid": 2,
        "negative": 2,
        "positive": 0,
        "near_miss": 0,
        "blocked_infrastructure": 1,
        "invalid_harness": 0,
        "underpowered": 0,
        "not_run": 1,
    }
    assert certificate["negative_evidence_count"] == 2
    assert certificate["detectable_edge"]["minimum_bps"] == 12.0
    assert certificate["multiplicity"]["family_size"] == 50
    assert certificate["incumbent_controls"]["declared"] == 1

    claim = {
        "lane": "majors",
        "refs": [],
        "provenance": "data-wall",
    }
    audit = audit_exhaustion_claim(store, job_id, claim)
    assert audit["verdict"] == "narrow"
    assert len(audit["audited_scope"]["cells"]) == 2
    statuses = {row["status"] for row in audit["required_next_experiments"]}
    assert statuses == {"blocked_infrastructure", "not_run"}

    for minute in range(3):
        store.append_journal(
            job_id,
            {
                "ts": f"2026-08-19T00:0{minute}:00+00:00",
                "type": "agent_wakeup",
            },
        )
    filed = file_exhaustion_claim(
        store,
        job_id,
        lane="majors",
        evidence="two completed cells; two declared gaps",
        provenance="data-wall",
        next_region="cross-asset",
    )
    applied = audit_and_adjudicate_exhaustion_claim(store, job_id, filed["claim_id"])
    assert applied["status"] == "audit_passed"
    assert applied["audit"]["verdict"] == "narrow"
    assert claim_settles_lane(applied)
    assert (
        store.read_json(job_id, "state/research_lane.json")["active_lane"]
        == "cross-asset"
    )
    marker = store.read_json(job_id, "state/research_impasse.json")
    assert marker["status"] == "mandated_work"
    assert {
        row["status"] for row in marker["mandate"]["required_next_experiments"]
    } == {
        "blocked_infrastructure",
        "not_run",
    }
    recover_stalled_applications(store=store)
    assert (
        store.read_json(job_id, "state/research_impasse.json")["status"]
        == "mandated_work"
    )


def test_legacy_scan_cells_require_contiguous_write_provenance(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-legacy-scan")
    legacy_cell = _scan_test()
    legacy_cell.pop("scan_id")
    _write_scan(store, job_id, [_scan_meta(scan_id="legacy-scan"), legacy_cell])
    legacy = build_coverage_certificate(job_id, "majors", store=store)
    assert legacy["cell_counts"]["completed_valid"] == 1

    store, job_id = _store(tmp_path, "coverage-compacted-scan")
    unrelated = _scan_test()
    unrelated["scan_id"] = "different-scan"
    _write_scan(store, job_id, [_scan_meta(scan_id="old-scan"), unrelated])
    compacted = build_coverage_certificate(job_id, "majors", store=store)
    assert compacted["cell_counts"]["completed_valid"] == 0
    assert compacted["cell_counts"]["not_run"] == 1


def test_unmeasured_incumbent_control_is_a_critical_gap(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-incumbent-gap")
    meta = _scan_meta()
    meta["incumbent_controls"] = [
        {
            "symbol": "ETH",
            "signal": "sig_a",
            "timeframe": "1h",
            "horizon": 1,
        }
    ]
    _write_scan(store, job_id, [meta, _scan_test()])

    certificate = build_coverage_certificate(job_id, "majors", store=store)
    assert certificate["incumbent_controls"]["cells"][0]["status"] == "not_run"
    assert certificate["cell_counts"]["not_run"] == 1
    audit = audit_exhaustion_claim(
        store,
        job_id,
        {"lane": "majors", "refs": [], "provenance": "data-wall"},
    )
    assert audit["verdict"] == "narrow"
    assert any(
        row["id"].startswith("coverage-cell:ETH:sig_a:1h:1")
        for row in audit["required_next_experiments"]
    )


def test_pass_auto_settles_opens_next_region_and_owner_can_reopen(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path, "coverage-pass")
    _write_scan(store, job_id, [_scan_meta(), _scan_test()])
    claim = file_exhaustion_claim(
        store,
        job_id,
        lane="majors",
        evidence="structured scan is complete",
        provenance="holdout-refuted",
        next_region="cross-asset",
    )

    audited = audit_and_adjudicate_exhaustion_claim(store, job_id, claim["claim_id"])
    assert audited["status"] == "audit_passed"
    assert audited["adjudication"]["by"] == "coverage-audit"
    assert claim_settles_lane(audited)
    assert (
        store.read_json(job_id, "state/research_lane.json")["active_lane"]
        == "cross-asset"
    )
    journal_types = {row["type"] for row in store.read_jsonl(job_id, "journal.jsonl")}
    assert "exhaustion_claim_audit_passed" in journal_types
    assert "research_region_opened" in journal_types
    assert (
        store.read_json(job_id, "scorecard.json")["coverage_audit"]["verdict"] == "pass"
    )

    with pytest.raises(PermissionError):
        reopen_exhaustion_claim(
            store, job_id, claim["claim_id"], by="agent", reason="disagree"
        )
    reopened = reopen_exhaustion_claim(
        store,
        job_id,
        claim["claim_id"],
        by="owner",
        reason="scope omitted a required market",
    )
    assert reopened["status"] == "reopened"
    assert not claim_settles_lane(reopened)
    assert (
        store.read_json(job_id, "state/research_lane.json")["active_lane"] == "majors"
    )
    assert (
        store.read_json(job_id, "state/research_impasse.json")["status"]
        == "mandated_work"
    )


def test_parked_probation_candidate_rejects_and_names_mandate(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path, "coverage-reject")
    _write_scan(store, job_id, [_scan_meta(), _scan_test(verdict="probation")])
    claim = file_exhaustion_claim(
        store,
        job_id,
        lane="majors",
        evidence="agent says done",
        provenance="data-wall",
        next_region="other",
    )
    audited = audit_and_adjudicate_exhaustion_claim(store, job_id, claim["claim_id"])
    assert audited["status"] == "rejected"
    assert audited["audit"]["verdict"] == "reject"
    required = audited["required_next_experiments"]
    assert required[0]["id"].startswith("candidate-followup:BTC:sig_a:1h:1")
    marker = store.read_json(job_id, "state/research_impasse.json")
    assert marker["status"] == "mandated_work"
    assert marker["mandate"]["required_next_experiments"] == required


def test_probation_followup_is_not_parked_and_kill_is_negative_evidence(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path, "coverage-probation-followup")
    _write_scan(store, job_id, [_scan_meta(), _scan_test(verdict="probation")])
    store.append_journal(
        job_id,
        {"type": "paper_probation_opened", "leg": "sig-a-paper", "symbol": "BTC"},
    )

    active = build_coverage_certificate(job_id, "majors", store=store)
    assert active["parked_candidates"] == []
    assert active["unresolved_candidates"][0]["state"] == "probation_active"
    active_audit = audit_exhaustion_claim(
        store,
        job_id,
        {"lane": "majors", "refs": [], "provenance": "data-wall"},
    )
    assert active_audit["verdict"] == "reject"

    store.append_journal(
        job_id,
        {"type": "probation_leg_killed", "leg": "sig-a-paper"},
    )
    killed = build_coverage_certificate(job_id, "majors", store=store)
    assert killed["unresolved_candidates"] == []
    assert killed["candidate_followups"][0]["state"] == "refuted"
    killed_audit = audit_exhaustion_claim(
        store,
        job_id,
        {"lane": "majors", "refs": [], "provenance": "data-wall"},
    )
    assert killed_audit["verdict"] == "pass"


def test_paper_entry_refusal_completes_requirement_and_refutes_candidate(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path, "coverage-paper-refusal")
    _write_scan(store, job_id, [_scan_meta(), _scan_test(verdict="probation")])
    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:02:00+00:00",
            "type": "operator_note",
            "required_experiments": [
                {
                    "id": "btc-paper-probation",
                    "kind": "paper_probation",
                    "symbol": "BTC",
                }
            ],
        },
    )
    before = build_coverage_certificate(job_id, "majors", store=store)
    assert not before["required_experiments"][0]["satisfied"]
    assert before["candidate_followups"][0]["state"] == "parked"

    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:03:00+00:00",
            "type": "paper_probation_entry_refused",
            "leg": "sig-a-paper",
            "symbol": "BTC",
            "signal": "sig_a",
            "timeframe": "1h",
            "horizon": 1,
            "artifact": "results/research/sig-a-refusal.json",
            "entry": {"eligible": False, "reasons": ["clearly worse"]},
        },
    )
    after = build_coverage_certificate(job_id, "majors", store=store)
    assert after["required_experiments"][0]["satisfied"]
    assert after["required_experiments"][0]["status"] == "completed"
    assert after["candidate_followups"][0]["state"] == "refuted"
    assert after["unresolved_candidates"] == []


def test_killed_probation_outweighs_its_admission_holdout(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-kill-after-holdout")
    _write_scan(
        store,
        job_id,
        [
            _scan_meta(),
            _scan_test(verdict="probation"),
            {
                **_scan_test(verdict="probation"),
                "kind": "holdout_check",
                "ts": "2026-08-20T00:02:00+00:00",
                "verdict": "confirmed",
            },
        ],
    )
    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:03:00+00:00",
            "type": "paper_probation_opened",
            "leg": "sig-a-paper",
            "symbol": "BTC",
        },
    )
    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:04:00+00:00",
            "type": "probation_leg_killed",
            "leg": "sig-a-paper",
        },
    )
    certificate = build_coverage_certificate(job_id, "majors", store=store)
    assert certificate["candidate_followups"][0]["state"] == "refuted"
    assert certificate["unresolved_candidates"] == []


def test_structured_operator_requirement_is_a_named_gap(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-requirement")
    _write_scan(store, job_id, [_scan_meta(), _scan_test()])
    store.append_journal(
        job_id,
        {
            "type": "operator_note",
            "required_experiments": [
                {
                    "id": "hype-multi-timeframe",
                    "kind": "signal_scan",
                    "symbols": ["HYPE"],
                    "timeframes": ["15m", "30m", "1h", "4h"],
                }
            ],
        },
    )
    audit = audit_exhaustion_claim(
        store,
        job_id,
        {"lane": "majors", "refs": [], "provenance": "data-wall"},
    )
    assert audit["verdict"] == "narrow"
    assert any(
        row["id"] == "hype-multi-timeframe"
        for row in audit["required_next_experiments"]
    )


def test_declared_scan_or_insufficient_holdout_does_not_complete_requirement(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path, "coverage-requirement-evidence")
    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:00:00+00:00",
            "type": "operator_note",
            "required_experiments": [
                {
                    "id": "btc-scan",
                    "kind": "signal_scan",
                    "symbols": ["BTC"],
                    "timeframes": ["1h"],
                    "signals": ["sig_a"],
                },
                {
                    "id": "btc-holdout",
                    "kind": "holdout_check",
                    "symbol": "BTC",
                    "signal": "sig_a",
                    "timeframe": "1h",
                    "horizon": 1,
                },
            ],
        },
    )
    meta = _scan_meta()
    meta["ts"] = "2026-08-20T00:01:00+00:00"
    insufficient = {
        **_scan_test(),
        "kind": "holdout_check",
        "ts": "2026-08-20T00:02:00+00:00",
        "hash": "holdout-1",
        "verdict": "insufficient",
    }
    _write_scan(store, job_id, [meta, insufficient])

    declared_only = build_coverage_certificate(job_id, "majors", store=store)
    requirements = {row["id"]: row for row in declared_only["required_experiments"]}
    assert not requirements["btc-scan"]["satisfied"]
    assert not requirements["btc-holdout"]["satisfied"]

    measured = _scan_test()
    measured["ts"] = "2026-08-20T00:03:00+00:00"
    failed_holdout = {
        **insufficient,
        "ts": "2026-08-20T00:04:00+00:00",
        "verdict": "failed",
    }
    _write_scan(store, job_id, [measured, failed_holdout])
    completed = build_coverage_certificate(job_id, "majors", store=store)
    requirements = {row["id"]: row for row in completed["required_experiments"]}
    assert requirements["btc-scan"]["satisfied"]
    assert requirements["btc-holdout"]["satisfied"]


def test_owner_reopen_window_expires(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-reopen-expired")
    _write_scan(store, job_id, [_scan_meta(), _scan_test()])
    claim = file_exhaustion_claim(
        store,
        job_id,
        lane="majors",
        evidence="complete negative scan",
        provenance="data-wall",
        next_region="cross-asset",
    )
    audited = audit_and_adjudicate_exhaustion_claim(store, job_id, claim["claim_id"])
    audited["owner_override_until"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    store.write_json(
        job_id,
        f"research/exhaustion_claims/{claim['claim_id']}.json",
        audited,
    )

    with pytest.raises(ValueError, match="expired"):
        reopen_exhaustion_claim(
            store,
            job_id,
            claim["claim_id"],
            by="owner",
            reason="too late",
        )


def test_rank_requirement_needs_controller_completion_event(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, "coverage-rank-requirement")
    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:00:00+00:00",
            "type": "operator_note",
            "required_experiments": [
                {
                    "id": "basket-rank",
                    "kind": "rank_check",
                    "column": "ratioz_basket96",
                    "horizons": [1, 4],
                }
            ],
        },
    )
    before = build_coverage_certificate(job_id, "majors", store=store)
    assert not before["required_experiments"][0]["satisfied"]

    store.append_journal(
        job_id,
        {
            "ts": "2026-08-20T00:01:00+00:00",
            "type": "rank_check_completed",
            "column": "ratioz_basket96",
            "horizons": [1, 4, 16],
            "artifact": "results/research/rank_check.json",
        },
    )
    after = build_coverage_certificate(job_id, "majors", store=store)
    assert after["required_experiments"][0]["satisfied"]


def test_process_efficiency_classifies_learning_activity_and_waste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, job_id = _store(tmp_path, "efficiency")
    base = datetime(2026, 8, 20, tzinfo=UTC)
    experiments = [
        {
            "ts": (base + timedelta(minutes=1)).isoformat(),
            "semantic_hash": "unique-a",
            "invalid_count": 2,
        },
        {
            "ts": (base + timedelta(minutes=11)).isoformat(),
            "semantic_hash": "unique-a",
            "invalid_count": 0,
        },
    ]
    _append_rows(
        store.job_dir(job_id) / "results/backtest/experiments.jsonl", experiments
    )
    journal = [
        {"ts": (base + timedelta(minutes=5)).isoformat(), "type": "agent_wakeup"},
        {
            "ts": (base + timedelta(minutes=12)).isoformat(),
            "type": "exhaustion_claim_filed",
        },
        {"ts": (base + timedelta(minutes=15)).isoformat(), "type": "agent_wakeup"},
        {
            "ts": (base + timedelta(minutes=21)).isoformat(),
            "type": "probation_leg_killed",
        },
        {
            "ts": (base + timedelta(minutes=22)).isoformat(),
            "type": "operator_note",
        },
        {
            "ts": (base + timedelta(minutes=23)).isoformat(),
            "type": "exhaustion_claim_reopened",
            "by": "owner",
        },
        {"ts": (base + timedelta(minutes=25)).isoformat(), "type": "agent_wakeup"},
        {"ts": (base + timedelta(minutes=35)).isoformat(), "type": "agent_wakeup"},
    ]
    journal_path = store.job_dir(job_id) / "journal.jsonl"
    journal_path.write_text("", encoding="utf-8")
    _append_rows(journal_path, journal)
    ops = store.job_dir(job_id) / "state/background_ops"
    ops.mkdir(parents=True)
    (ops / "rank_check.json").write_text(
        json.dumps({"op": "rank_check", "state": "failed"}), encoding="utf-8"
    )

    efficiency = build_process_efficiency(store, job_id)
    assert efficiency == {
        "wakes_total": 4,
        "wakes_with_valid_learning": 2,
        "activity_only_wakes": 1,
        "duplicate_experiments": 1,
        "infra_invalid_experiments": 1,
        "manual_interventions": 2,
        "false_closure_reversals": 1,
        "stalled_background_ops": 1,
        "wakes_per_valid_learning": 2.0,
    }
    store.refresh_scorecard(job_id)
    assert store.read_json(job_id, "scorecard.json")["process_efficiency"] == efficiency
    assert (
        snapshot_job(job_id, store=store)["scorecard"]["process_efficiency"]
        == efficiency
    )
    assert build_evolution_report(store, job_id)["process_efficiency"] == efficiency
    assert evolution_snapshot_block(store, job_id)["process_efficiency"] == efficiency

    from click.testing import CliRunner

    import wayfinder_paths.jobs.cli as cli_module

    monkeypatch.setattr(cli_module, "JobStore", lambda: store)
    report = CliRunner().invoke(cli_module.job_cli, ["report", job_id])
    assert report.exit_code == 0, report.output
    assert "2.0 wakes/valid-learning" in report.output

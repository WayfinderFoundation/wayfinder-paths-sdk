"""Evidence reuse across the proposal lifecycle: a proposal validated in
full at propose time (candidate backtest + preflight + execution validation +
economic gate) must not re-run the expensive pieces when the candidate,
dataset, and baseline are PROVABLY unchanged and the frozen evidence is green.
Covers the eligibility matrix (`assess_validation_reuse` /
`assess_evidence_reuse`), the journal contract (`*_reused` with proof inline
/ `*_rerun` with reason), the per-piece kill-switches, the propose-time
dataset fingerprint, revalidate-time reuse (which must never short-circuit
the cure of a poisoned report — the #700 incident shape), economic paired-
fold persistence, the live-capable dataset freshness bound, and the 2026-08
production incident shape (one-line params change re-backtested for 30+
minutes with trading lanes paused)."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs import validation as validation_module
from wayfinder_paths.jobs.application import (
    assess_validation_reuse,
    claim_application,
    complete_application,
)
from wayfinder_paths.jobs.constitution import load_constitution
from wayfinder_paths.jobs.evidence_reuse import assess_evidence_reuse
from wayfinder_paths.jobs.gating import (
    PAIRED_FOLDS_RELATIVE,
    dataset_content_fingerprint,
    evaluate_economic_gate,
    evaluate_live_gate,
)
from wayfinder_paths.jobs.proposals import propose_change, revalidate_proposal
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_application_gate import _patch_runner
from wayfinder_paths.tests.test_jobs_gating import _make_job
from wayfinder_paths.tests.test_jobs_preflight import _bars
from wayfinder_paths.tests.test_wayfinder_jobs import _intent_contract


def _count_behavior_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count invocations of the expensive behavioral half (full-dataset
    candidate backtest + preflight) without changing its behavior."""
    calls: list[str] = []
    real = validation_module._candidate_behavior_checks

    def counting(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(str(kwargs.get("candidate_dir")))
        return real(**kwargs)

    monkeypatch.setattr(validation_module, "_candidate_behavior_checks", counting)
    return calls


def _ready_economic(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "ready": True,
        "reasons": [],
        "enforcement": "advisory",
        "constitution_revision": None,
        "status": "evaluated",
    }


def _journal_entries(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _entries_of(store: JobStore, job_id: str, kind: str) -> list[dict[str, Any]]:
    return [item for item in _journal_entries(store, job_id) if item["type"] == kind]


def _proposed_and_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[JobStore, str, Path, str]:
    _patch_runner(monkeypatch)
    # The 8-bar fixture's real economic verdict is ready=False (insufficient
    # history); reuse requires green frozen evidence, so neutralize it.
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params=params or {"hype_short_enabled": False},
    )
    pid = proposal["proposal_id"]
    store.approve_proposal(job_id, pid)
    claim_application(store, job_id, pid)
    return store, job_id, root, pid


def _mutate_report(store: JobStore, job_id: str, pid: str, **updates: Any) -> None:
    proposal = store.load_proposal(job_id, pid)
    report = proposal["candidate_report"]
    for key, value in updates.items():
        if value is None:
            report.pop(key, None)
        else:
            report[key] = value
    store.write_proposal(job_id, proposal)


def test_incident_shape_green_unchanged_apply_skips_revalidation_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production incident: a one-line params proposal validated in full
    at propose time, approved minutes later, then re-backtested for 30+
    minutes at apply with trading lanes paused — while the journal's own
    `candidate_reused` proved the bundle was identical. With green frozen
    evidence + unchanged candidate + unchanged dataset, apply must reuse."""
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    assert _entries_of(store, job_id, "candidate_reused"), "claim reused candidate"
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert completed["proposal"]["application"]["status"] == "applied"
    assert behavior_calls == [], "candidate backtest must NOT re-run"
    assert not _entries_of(store, job_id, "apply_validation_rerun")
    reused = _entries_of(store, job_id, "apply_validation_reused")
    assert len(reused) == 1
    deterministic = completed["deterministic_validation"]
    assert deterministic["status"] == "reused"
    assert deterministic["source"] == "propose-time report"
    assert deterministic["reused_summary"]["status"] == "passed"
    proof = deterministic["reuse_proof"]
    report = store.load_proposal(job_id, pid)["candidate_report"]
    assert proof["candidate_revision"] == report["revision"]
    assert proof["base_revision"] == report["base_revision"]
    assert proof["dataset_fingerprint"] == report["dataset_fingerprint"]
    assert proof["report_hash"]
    for key in ("base_revision", "candidate_revision", "dataset_fingerprint"):
        assert reused[0][key] == proof[key]
    # Cheap invariants still ran and the promotion is fully wired.
    assert deterministic["checks"]
    assert store.load(job_id).execution_params["hype_short_enabled"] is False
    assert evaluate_live_gate(job_id, store=store)["live_ready"] is True


def test_pause_resume_window_unchanged_on_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse shortens the pause window; it must not change its shape — loops
    pause at claim and resume at completion exactly as before."""
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    job = store.load(job_id)
    job.script_loop.runner_job_name = "job-gate-demo"
    store.save(job)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params={"hype_short_enabled": False},
    )
    pid = proposal["proposal_id"]
    store.approve_proposal(job_id, pid)
    calls = _patch_runner(monkeypatch)
    claim_application(store, job_id, pid)
    completed = complete_application(store, job_id, pid, status="applied")

    assert completed["deterministic_validation"]["status"] == "reused"
    assert ("pause", "job-gate-demo") in calls
    assert ("resume", "job-gate-demo") in calls


def test_mutated_candidate_forces_full_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    candidate_script = (
        root / "applications" / pid / "candidate" / "workspace" / "src" / "strategy.py"
    )
    candidate_script.write_text(
        candidate_script.read_text(encoding="utf-8") + "\n# mutated after report\n",
        encoding="utf-8",
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert len(behavior_calls) == 1, "full re-validation must run"
    rerun = _entries_of(store, job_id, "apply_validation_rerun")
    assert rerun and rerun[0]["reason"] == "candidate_mismatch"
    assert not _entries_of(store, job_id, "apply_validation_reused")
    assert completed["deterministic_validation"]["status"] == "passed"


def test_changed_dataset_fingerprint_forces_full_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    # Byte-different, parse-identical: the fingerprint is content-derived,
    # so ANY dataset rewrite invalidates the frozen evidence.
    bars_path.write_text(
        json.dumps(json.loads(bars_path.read_text(encoding="utf-8")), indent=2),
        encoding="utf-8",
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "apply_validation_rerun")
    assert rerun and rerun[0]["reason"] == "dataset_changed"
    assert completed["proposal"]["application"]["status"] == "applied"


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"validation_summary": {"status": "failed", "failed_checks": []}},
            "report_not_green",
        ),
        ({"economic": {"ready": False, "reasons": ["regression"]}}, "report_not_green"),
        ({"economic": {"ready": None, "reasons": ["crashed"]}}, "report_not_green"),
        ({"dataset_fingerprint": None}, "no_dataset_fingerprint"),
        ({"mode": "validation_only"}, "report_missing_or_not_full"),
    ],
)
def test_non_reusable_frozen_evidence_forces_full_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
    reason: str,
) -> None:
    """Failed/poisoned/absent frozen evidence is never reused — the apply
    falls back to today's authoritative full re-validation."""
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    _mutate_report(store, job_id, pid, **updates)
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "apply_validation_rerun")
    assert rerun and rerun[0]["reason"] == reason
    assert completed["proposal"]["application"]["status"] == "applied"


def test_kill_switch_forces_full_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    monkeypatch.setenv("WAYFINDER_APPLY_ALWAYS_REVALIDATE", "1")
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "apply_validation_rerun")
    assert rerun and rerun[0]["reason"] == "kill_switch"
    assert completed["proposal"]["application"]["status"] == "applied"


def test_assess_reuse_reports_baseline_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active workspace moved past the staged base is never reusable (the
    drift guard refuses the promotion outright; the assessment is
    self-contained and must agree)."""
    store, job_id, root, pid = _proposed_and_claimed(tmp_path, monkeypatch)
    active_script = root / "workspace" / "src" / "strategy.py"
    active_script.write_text(
        active_script.read_text(encoding="utf-8") + "\n# intervening apply\n",
        encoding="utf-8",
    )

    proposal = store.load_proposal(job_id, pid)
    result = assess_validation_reuse(
        store, job_id, proposal, root / "applications" / pid / "candidate"
    )

    assert result["eligible"] is False
    assert result["reason"] == "baseline_drift"


def test_propose_records_dataset_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Loosen the entry threshold.",
        intent_contract=_intent_contract(),
        params={"threshold": 10.7},
    )

    fingerprint = proposal["candidate_report"]["dataset_fingerprint"]
    bars_path = root / "results" / "backtest" / "input_bars.json"
    assert fingerprint["path"] == str(bars_path)
    assert fingerprint["sha256"] == hashlib.sha256(bars_path.read_bytes()).hexdigest()
    assert fingerprint["bytes"] == bars_path.stat().st_size


def test_dataset_content_fingerprint_resolution_and_features(
    tmp_path: Path,
) -> None:
    candidate_dir = tmp_path / "candidate"
    job_dir = tmp_path / "job"
    for base in (candidate_dir, job_dir):
        base.mkdir()

    assert dataset_content_fingerprint(candidate_dir, job_dir) is None

    job_bars = job_dir / "results" / "backtest" / "input_bars.json"
    job_bars.parent.mkdir(parents=True)
    job_bars.write_text("[1, 2, 3]", encoding="utf-8")
    from_job = dataset_content_fingerprint(candidate_dir, job_dir)
    assert from_job is not None
    assert from_job["path"] == str(job_bars)
    assert from_job["sha256"] == hashlib.sha256(b"[1, 2, 3]").hexdigest()

    # Candidate-bundle bars win over the job root (mirrors _resolve_dataset).
    candidate_bars = candidate_dir / "workspace" / "config" / "backtest_bars.json"
    candidate_bars.parent.mkdir(parents=True)
    candidate_bars.write_text("[4]", encoding="utf-8")
    from_candidate = dataset_content_fingerprint(candidate_dir, job_dir)
    assert from_candidate is not None
    assert from_candidate["path"] == str(candidate_bars)

    # Declared feature stores ride in the fingerprint, first-root-wins.
    job_features = job_dir / "state" / "features.jsonl"
    job_features.parent.mkdir(parents=True)
    job_features.write_text('{"name": "funding"}\n', encoding="utf-8")
    with_features = dataset_content_fingerprint(
        candidate_dir, job_dir, feature_paths=("state/features.jsonl",)
    )
    assert with_features is not None
    assert with_features["features"] == {
        "state/features.jsonl": hashlib.sha256(job_features.read_bytes()).hexdigest()
    }
    job_features.write_text('{"name": "funding", "value": 1}\n', encoding="utf-8")
    moved = dataset_content_fingerprint(
        candidate_dir, job_dir, feature_paths=("state/features.jsonl",)
    )
    assert moved != with_features, "feature churn must invalidate the fingerprint"


# ── revalidate-time evidence reuse ───────────────────────────────────────────


def _proposed_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[JobStore, str, Path, str]:
    """Propose only (proposal stays PENDING — the revalidate precondition),
    with green frozen economic evidence like `_proposed_and_claimed`."""
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params={"hype_short_enabled": False},
    )
    return store, job_id, root, proposal["proposal_id"]


def test_revalidate_reuses_green_validation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged candidate + dataset + baseline with green frozen validation:
    revalidate must not re-run the expensive candidate backtest — it keeps
    the cheap invariants and regenerates the report around the prior run."""
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidated = revalidate_proposal(store, job_id, pid)

    assert behavior_calls == [], "candidate backtest must NOT re-run"
    reused = _entries_of(store, job_id, "revalidate_evidence_reused")
    assert len(reused) == 1
    assert not _entries_of(store, job_id, "revalidate_evidence_rerun")
    report = revalidated["candidate_report"]
    assert report["mode"] == "full"
    assert report["validation_summary"]["status"] == "passed"
    assert report["gate"]["live_ready"] is True, report["gate"]["reasons"]
    assert report["comparison"]["candidate"]["stats"]
    proof = report["evidence_reuse"]
    assert proof["phase"] == "revalidate"
    assert proof["candidate_revision"] == report["revision"]
    for key in ("base_revision", "candidate_revision", "dataset_fingerprint"):
        assert reused[0][key] == proof[key]


def test_revalidate_reruns_on_mutated_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    candidate_script = (
        root / "applications" / pid / "candidate" / "workspace" / "src" / "strategy.py"
    )
    candidate_script.write_text(
        candidate_script.read_text(encoding="utf-8") + "\n# edited after report\n",
        encoding="utf-8",
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidated = revalidate_proposal(store, job_id, pid)

    assert len(behavior_calls) == 1, "full re-validation must run"
    rerun = _entries_of(store, job_id, "revalidate_evidence_rerun")
    assert rerun and rerun[0]["reason"] == "candidate_mismatch"
    assert not _entries_of(store, job_id, "revalidate_evidence_reused")
    assert revalidated["candidate_report"]["validation_summary"]["status"] == "passed"


def test_revalidate_reruns_on_changed_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.write_text(
        json.dumps(json.loads(bars_path.read_text(encoding="utf-8")), indent=2),
        encoding="utf-8",
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidate_proposal(store, job_id, pid)

    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "revalidate_evidence_rerun")
    assert rerun and rerun[0]["reason"] == "dataset_changed"


def test_revalidate_reruns_failed_validation_and_cures_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse must never short-circuit the cure: a frozen FAILED validation
    half is exactly what revalidate exists to re-run (#700 recovery shape)."""
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    _mutate_report(
        store,
        job_id,
        pid,
        validation_summary={
            "status": "failed",
            "failed_checks": ["candidate_backtest_valid"],
            "failure_kind": "infrastructure",
        },
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidated = revalidate_proposal(store, job_id, pid)

    assert len(behavior_calls) == 1, "the poisoned half must re-run"
    rerun = _entries_of(store, job_id, "revalidate_evidence_rerun")
    assert rerun and rerun[0]["reason"] == "report_not_green"
    assert revalidated["candidate_report"]["validation_summary"]["status"] == "passed"


def test_revalidate_kill_switch_forces_full_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    monkeypatch.setenv("WAYFINDER_REVALIDATE_ALWAYS_RERUN", "1")
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidate_proposal(store, job_id, pid)

    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "revalidate_evidence_rerun")
    assert rerun and rerun[0]["reason"] == "kill_switch"


def test_revalidate_reuse_still_cures_poisoned_economic_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #700 incident shape WITH reuse active: validation green and
    provably unchanged (reused — zero backtests), economic block poisoned by
    a box condition (ready=None + error). Revalidate must reuse the
    validation half AND refresh the economic half — the cure is the point."""
    store, job_id, root, pid = _proposed_pending(tmp_path, monkeypatch)
    _mutate_report(
        store,
        job_id,
        pid,
        economic={
            "ready": None,
            "reasons": ["economic evaluation failed: No backtest bars found."],
            "enforcement": "advisory",
            "constitution_revision": None,
            "status": "error",
            "escalate": True,
        },
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    revalidated = revalidate_proposal(store, job_id, pid)

    assert behavior_calls == [], "green validation half must be reused"
    assert _entries_of(store, job_id, "revalidate_evidence_reused")
    economic = revalidated["candidate_report"]["economic"]
    assert economic["ready"] is True, "the poisoned economic block must refresh"
    assert economic["reasons"] == []

    store.approve_proposal(job_id, pid)
    assert store.load_proposal(job_id, pid)["status"] == "approved"


# ── full lifecycle: the expensive backtest runs EXACTLY once ─────────────────


def test_lifecycle_unchanged_candidate_backtests_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """propose → revalidate → approve → claim → apply for an unchanged
    candidate: the expensive behavioral half (full-dataset candidate backtest
    + preflight) runs exactly ONCE — at propose."""
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    behavior_calls = _count_behavior_checks(monkeypatch)

    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params={"hype_short_enabled": False},
    )
    pid = proposal["proposal_id"]
    assert len(behavior_calls) == 1, "propose runs the backtest once"

    revalidate_proposal(store, job_id, pid)
    assert len(behavior_calls) == 1, "revalidate reuses"

    store.approve_proposal(job_id, pid)
    claim_application(store, job_id, pid)
    completed = complete_application(store, job_id, pid, status="applied")

    assert len(behavior_calls) == 1, "apply reuses"
    assert completed["proposal"]["application"]["status"] == "applied"
    assert completed["deterministic_validation"]["status"] == "reused"
    assert store.load(job_id).execution_params["hype_short_enabled"] is False
    assert evaluate_live_gate(job_id, store=store)["live_ready"] is True


# ── freshness bound: live-capable jobs refuse stale-dataset evidence ─────────


def _stamp_dataset_fetched_at(root: Path, fetched_at: str) -> None:
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.write_text(
        json.dumps({"bars": _bars(), "metadata": {"fetched_at": fetched_at}}),
        encoding="utf-8",
    )


def _make_live_capable(root: Path) -> None:
    summary = root / "results" / "forward" / "summary.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({"runs": {"count": 3}}), encoding="utf-8")


def _proposed_and_claimed_with_stale_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    age_hours: float,
) -> tuple[JobStore, str, Path, str]:
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    fetched_at = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    _stamp_dataset_fetched_at(root, fetched_at)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params={"hype_short_enabled": False},
    )
    pid = proposal["proposal_id"]
    store.approve_proposal(job_id, pid)
    claim_application(store, job_id, pid)
    return store, job_id, root, pid


def test_stale_dataset_refuses_reuse_and_recompute_for_live_capable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LIVE-CAPABLE + dataset older than the evidence ceiling: reusing would
    promote on stale evidence, blindly re-validating would validate against
    the same stale bars — the apply must refuse BOTH and route to
    refresh + revalidate."""
    store, job_id, root, pid = _proposed_and_claimed_with_stale_dataset(
        tmp_path, monkeypatch, age_hours=48.0
    )
    _make_live_capable(root)
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert behavior_calls == [], "no blind recompute against stale bars"
    assert completed["proposal"]["application"]["status"] == "failed"
    error = completed["proposal"]["application"]["error"]
    assert "dataset stale — refresh and revalidate" in error
    refused = _entries_of(store, job_id, "apply_refused_stale_dataset")
    assert len(refused) == 1
    assert refused[0]["max_age_hours"] == 24.0
    assert refused[0]["age_hours"] > 24.0
    assert not _entries_of(store, job_id, "apply_validation_reused")


def test_stale_dataset_still_reuses_for_paper_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment: a job that never entered paper/live keeps reusing old
    evidence — the freshness bound protects live capital only."""
    store, job_id, root, pid = _proposed_and_claimed_with_stale_dataset(
        tmp_path, monkeypatch, age_hours=48.0
    )
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert behavior_calls == []
    assert completed["proposal"]["application"]["status"] == "applied"
    assert completed["deterministic_validation"]["status"] == "reused"


def test_evidence_max_age_env_raises_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_and_claimed_with_stale_dataset(
        tmp_path, monkeypatch, age_hours=48.0
    )
    _make_live_capable(root)
    monkeypatch.setenv("WAYFINDER_EVIDENCE_MAX_AGE_HOURS", "1000")
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert behavior_calls == []
    assert completed["proposal"]["application"]["status"] == "applied"
    assert completed["deterministic_validation"]["status"] == "reused"


def test_fresh_dataset_reuses_for_live_capable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, pid = _proposed_and_claimed_with_stale_dataset(
        tmp_path, monkeypatch, age_hours=1.0
    )
    _make_live_capable(root)
    behavior_calls = _count_behavior_checks(monkeypatch)

    completed = complete_application(store, job_id, pid, status="applied")

    assert behavior_calls == []
    assert completed["deterministic_validation"]["status"] == "reused"


def test_stale_dataset_forces_revalidate_rerun_for_live_capable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At revalidate the stale bound refuses REUSE only — the rerun proceeds
    (revalidate is the named cure; the refreshed report's fingerprint then
    covers whatever bars are on disk)."""
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", _ready_economic
    )
    store, job_id, root = _make_job(tmp_path)
    fetched_at = (datetime.now(UTC) - timedelta(hours=48.0)).isoformat()
    _stamp_dataset_fetched_at(root, fetched_at)
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary="Disable the short leg.",
        intent_contract=_intent_contract(),
        params={"hype_short_enabled": False},
    )
    pid = proposal["proposal_id"]
    _make_live_capable(root)
    candidate_dir = root / "applications" / pid / "candidate"

    result = assess_evidence_reuse(
        store,
        job_id,
        store.load_proposal(job_id, pid),
        candidate_dir,
        phase="revalidate",
    )
    assert result["eligible"] is False
    assert result["reason"] == "dataset_stale"

    behavior_calls = _count_behavior_checks(monkeypatch)
    revalidate_proposal(store, job_id, pid)
    assert len(behavior_calls) == 1
    rerun = _entries_of(store, job_id, "revalidate_evidence_rerun")
    assert rerun and rerun[0]["reason"] == "dataset_stale"


# ── economic paired-fold persistence ─────────────────────────────────────────

_GREEN_VECTOR = {
    "net_log_growth": 0.05,
    "downside_deviation": 0.01,
    "tail_loss": 0.0,
    "fee_load": 0.001,
    "max_drawdown_pct": 0.05,
    "trade_count": 20,
    "day_count": 30,
}
_BASE_VECTOR = {**_GREEN_VECTOR, "net_log_growth": 0.01, "trade_count": 18}


def _fake_evaluation(*, positive_folds: int = 4) -> dict[str, Any]:
    return {
        "status": "ok",
        "folds": [
            {"fold": index, "delta_utility": 0.01 if index < positive_folds else -0.01}
            for index in range(4)
        ],
        "fold_count": 4,
        "positive_folds": positive_folds,
        "objective": {"baseline": _BASE_VECTOR, "candidate": _GREEN_VECTOR},
        "paired_incumbent_delta": {
            "estimate": 0.02,
            "lcb": 0.01,
            "confidence": 0.9,
            "paired_days": 30,
        },
        "audit_slice": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-07T00:00:00Z",
            "bars": 10,
            "baseline": _BASE_VECTOR,
            "candidate": _GREEN_VECTOR,
            "delta_utility": 0.01,
        },
    }


def _economic_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **evaluation_kwargs: Any
) -> tuple[JobStore, str, Path, Path, list[int]]:
    store, job_id, root = _make_job(tmp_path)
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    shutil.copytree(root / "workspace", candidate_dir / "workspace")
    shutil.copy2(root / "job.yaml", candidate_dir / "job.yaml")
    calls: list[int] = []

    def fake_paired(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return _fake_evaluation(**evaluation_kwargs)

    monkeypatch.setattr(
        "wayfinder_paths.jobs.economics.paired_fold_evaluation", fake_paired
    )
    return store, job_id, root, candidate_dir, calls


def test_economic_folds_persisted_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(tmp_path, monkeypatch)

    first = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert first["ready"] is True
    assert "reused" not in first
    assert len(calls) == 1
    assert (candidate_dir / PAIRED_FOLDS_RELATIVE).exists()

    second = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert second["ready"] is True
    assert second["reused"] is True
    assert len(calls) == 1, "green matching folds must not recompute"
    assert second["objective"] == first["objective"]
    reused = _entries_of(store, job_id, "economic_evaluation_reused")
    assert len(reused) == 1
    assert reused[0]["candidate_revision"]


def test_economic_folds_recompute_on_constitution_revision_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(tmp_path, monkeypatch)
    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert len(calls) == 1

    (root / "constitution.yaml").write_text("enforcement: advisory\n")
    assert load_constitution(root)["revision"], "constitution now has a revision"

    result = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert len(calls) == 2, "constitution revision change must invalidate"
    assert "reused" not in result


def test_economic_folds_recompute_on_candidate_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(tmp_path, monkeypatch)
    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)

    script = candidate_dir / "workspace" / "src" / "strategy.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8"
    )

    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert len(calls) == 2


def test_economic_folds_recompute_on_dataset_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(tmp_path, monkeypatch)
    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)

    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.write_text(
        json.dumps(json.loads(bars_path.read_text(encoding="utf-8")), indent=2),
        encoding="utf-8",
    )

    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert len(calls) == 2


def test_non_green_economic_folds_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(
        tmp_path, monkeypatch, positive_folds=0
    )
    first = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert first["ready"] is False

    second = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert second["ready"] is False
    assert "reused" not in second
    assert len(calls) == 2, "non-green evidence recomputes every time"


def test_economic_kill_switch_forces_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, calls = _economic_fixture(tmp_path, monkeypatch)
    evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    monkeypatch.setenv("WAYFINDER_ECONOMIC_ALWAYS_RECOMPUTE", "1")

    result = evaluate_economic_gate(job_id, candidate_dir=candidate_dir, store=store)
    assert len(calls) == 2
    assert "reused" not in result


# ── scenario replay cache ────────────────────────────────────────────────────


def _scenario_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, expect: dict[str, Any]
) -> tuple[JobStore, str, Path, Path, dict[str, Any], list[int]]:
    store, job_id, root = _make_job(tmp_path)
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    shutil.copytree(root / "workspace", candidate_dir / "workspace")
    shutil.copy2(root / "job.yaml", candidate_dir / "job.yaml")
    proposal = {
        "job_id": job_id,
        "intent_contract": _intent_contract(),
        "scenario_plan": {
            "scenarios": [{"name": "replay", "bars": _bars(), "expect": expect}]
        },
    }
    calls: list[int] = []
    real = validation_module.simulate_execution

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(validation_module, "simulate_execution", counting)
    return store, job_id, root, candidate_dir, proposal, calls


def _run_cheap_validation(
    store: JobStore, root: Path, candidate_dir: Path, proposal: dict[str, Any]
) -> dict[str, Any]:
    return validation_module.validate_candidate_application(
        repo_root=store.repo_root,
        job_dir=root,
        proposal=proposal,
        candidate_dir=candidate_dir,
        skip_behavior_checks=True,
    )


def test_scenario_results_reused_across_validations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, proposal, calls = _scenario_fixture(
        tmp_path, monkeypatch, expect={"execution_valid": True}
    )

    first = _run_cheap_validation(store, root, candidate_dir, proposal)
    assert first["status"] == "passed"
    assert len(calls) == 1
    cache = candidate_dir / "reports" / "validation" / "scenario_cache.json"
    assert cache.exists()

    second = _run_cheap_validation(store, root, candidate_dir, proposal)
    assert second["status"] == "passed"
    assert len(calls) == 1, "cached scenario replay must not re-simulate"
    reused_rows = [check for check in second["checks"] if check.get("reused") is True]
    assert len(reused_rows) == 1
    assert reused_rows[0]["name"] == "scenario_replay"


def test_scenario_hash_invalidates_on_definition_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, proposal, calls = _scenario_fixture(
        tmp_path, monkeypatch, expect={"execution_valid": True}
    )
    _run_cheap_validation(store, root, candidate_dir, proposal)
    assert len(calls) == 1

    proposal["scenario_plan"]["scenarios"][0]["expect"] = {
        "execution_valid": True,
        "max_trades": 5,
    }
    second = _run_cheap_validation(store, root, candidate_dir, proposal)
    assert len(calls) == 2, "changed scenario definition must re-simulate"
    assert not any(check.get("reused") for check in second["checks"])


def test_scenario_cache_invalidates_on_candidate_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, proposal, calls = _scenario_fixture(
        tmp_path, monkeypatch, expect={"execution_valid": True}
    )
    _run_cheap_validation(store, root, candidate_dir, proposal)

    script = candidate_dir / "workspace" / "src" / "strategy.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8"
    )
    _run_cheap_validation(store, root, candidate_dir, proposal)
    assert len(calls) == 2


def test_failed_scenarios_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, proposal, calls = _scenario_fixture(
        tmp_path, monkeypatch, expect={"min_trades": 99}
    )

    first = _run_cheap_validation(store, root, candidate_dir, proposal)
    assert first["status"] == "failed"
    cache = candidate_dir / "reports" / "validation" / "scenario_cache.json"
    assert not cache.exists(), "a failed scenario is a verdict, never cached"

    _run_cheap_validation(store, root, candidate_dir, proposal)
    assert len(calls) == 2


def test_scenario_kill_switch_forces_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root, candidate_dir, proposal, calls = _scenario_fixture(
        tmp_path, monkeypatch, expect={"execution_valid": True}
    )
    _run_cheap_validation(store, root, candidate_dir, proposal)
    monkeypatch.setenv("WAYFINDER_SCENARIOS_ALWAYS_RUN", "1")

    _run_cheap_validation(store, root, candidate_dir, proposal)
    assert len(calls) == 2

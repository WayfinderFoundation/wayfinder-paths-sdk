"""Apply-time evidence reuse: a proposal validated in full at propose time
(candidate backtest + preflight + execution validation + economic gate) must
not re-run the expensive candidate backtest at apply time when the candidate,
dataset, and baseline are PROVABLY unchanged and the frozen evidence is green.
Covers the eligibility matrix (`assess_validation_reuse`), the journal
contract (`apply_validation_reused` / `apply_validation_rerun`), the
kill-switch, the propose-time dataset fingerprint, and the 2026-08 production
incident shape (one-line params change re-backtested for 30+ minutes with
trading lanes paused)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs import validation as validation_module
from wayfinder_paths.jobs.application import (
    assess_validation_reuse,
    claim_application,
    complete_application,
)
from wayfinder_paths.jobs.gating import (
    dataset_content_fingerprint,
    evaluate_live_gate,
)
from wayfinder_paths.jobs.proposals import propose_change
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_application_gate import _patch_runner
from wayfinder_paths.tests.test_jobs_gating import _make_job
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

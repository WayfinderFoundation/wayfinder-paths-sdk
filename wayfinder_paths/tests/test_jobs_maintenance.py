"""Behavior-equivalent maintenance is mechanical and ownerless."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from wayfinder_paths.jobs import apply_launcher
from wayfinder_paths.jobs.proposals import propose_change
from wayfinder_paths.tests.test_jobs_gating import _make_job
from wayfinder_paths.tests.test_wayfinder_jobs import _intent_contract


def _candidate_workspace(root: Path, tmp_path: Path, suffix: str) -> Path:
    candidate = tmp_path / f"candidate-{suffix}"
    shutil.copytree(root / "workspace", candidate)
    return candidate


def test_equivalent_refactor_auto_applies_without_economic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    candidate = _candidate_workspace(root, tmp_path, "equivalent")
    strategy = candidate / "src" / "strategy.py"
    strategy.write_text(
        strategy.read_text(encoding="utf-8")
        + "\n# Implementation-only refactor marker.\n",
        encoding="utf-8",
    )
    launched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        apply_launcher,
        "launch_application",
        lambda _store, jid, pid: launched.append((jid, pid)),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate",
        lambda *args, **kwargs: pytest.fail("economic gate must not run"),
    )

    proposal = propose_change(
        store,
        job_id,
        kind="code_change",
        summary="Remove redundant indicator recomputation.",
        intent_contract=_intent_contract(),
        candidate_source=candidate,
        acceptance_policy="behavior_equivalence",
    )

    assert proposal["status"] == "approved"
    assert proposal["application"]["status"] == "queued"
    assert proposal["approval"]["required"] is False
    assert proposal["approval"]["by"] == "behavior-equivalence-gate"
    report = proposal["candidate_report"]
    assert report["maintenance"]["ready"] is True
    assert (
        report["maintenance"]["baseline_digest"]
        == report["maintenance"]["candidate_digest"]
    )
    assert report["economic"]["status"] == "not_applicable"
    assert launched == [(job_id, proposal["proposal_id"])]
    assert store.proposal_queue(job_id)["pending"] == []


def test_behavior_mismatch_creates_no_owner_proposal(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    candidate = _candidate_workspace(root, tmp_path, "different")
    strategy = candidate / "src" / "strategy.py"
    strategy.write_text(
        strategy.read_text(encoding="utf-8").replace(
            'float(latest["close"]) > 10.4', 'float(latest["close"]) > 999.0'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="behavior equivalence"):
        propose_change(
            store,
            job_id,
            kind="code_change",
            summary="Change entry behavior while claiming maintenance.",
            intent_contract=_intent_contract(),
            candidate_source=candidate,
            acceptance_policy="behavior_equivalence",
        )

    assert store.proposals(job_id) == []
    assert any(
        row.get("type") == "maintenance_candidate_rejected"
        for row in store.read_jsonl(job_id, "journal.jsonl")
    )


def test_maintenance_rejects_config_or_non_python_changes_before_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    candidate = tmp_path / "candidate-config"
    shutil.copytree(root / "workspace", candidate / "workspace")
    shutil.copy2(root / "job.yaml", candidate / "job.yaml")
    (candidate / "job.yaml").write_text(
        (candidate / "job.yaml").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.validate_candidate_bundle",
        lambda *args, **kwargs: pytest.fail("invalid surface must fail before sims"),
    )

    with pytest.raises(ValueError, match="invalid maintenance change surface"):
        propose_change(
            store,
            job_id,
            kind="code_change",
            summary="Try to change config through maintenance.",
            intent_contract=_intent_contract(),
            candidate_source=candidate,
            acceptance_policy="behavior_equivalence",
        )

    assert store.proposals(job_id) == []

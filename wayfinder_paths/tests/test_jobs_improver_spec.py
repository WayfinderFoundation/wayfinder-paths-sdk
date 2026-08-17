"""ImproverSpec U1: the search policy as a versioned, stamped artifact.
Defaults must equal the legacy literals (a pure refactor at U1), a file
override must change both behavior and revision, and every artifact type
must carry the improver+governance provenance stamp."""

from __future__ import annotations

import json

import pytest
import yaml

from wayfinder_paths.jobs.improver.spec import (
    DEFAULT_IMPROVER_REVISION,
    IMPROVER_FILENAME,
    ImproverSpec,
    revision_stamp,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "spec-demo",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def test_defaults_match_legacy_literals(tmp_path) -> None:
    spec = ImproverSpec.load(tmp_path)
    assert spec.revision == DEFAULT_IMPROVER_REVISION
    assert spec.source == "defaults"
    assert spec.staleness_experiment_days == 3.0
    assert spec.staleness_wakes == 100
    assert spec.ideation_due_s == 20 * 3600
    assert spec.ideation_overdue_s == 48 * 3600
    assert spec.stuck_same_family_non_wins == 2
    assert spec.probation_max_active_legs == 2
    assert spec.probation_max_size_fraction == 0.5
    assert abs(sum(spec.island_weights.values()) - 1.0) < 1e-9
    assert 0 < spec.exploration_floor < 1


def test_file_override_changes_values_and_revision(tmp_path) -> None:
    (tmp_path / IMPROVER_FILENAME).write_text(
        yaml.safe_dump({"staleness": {"experiment_days": 1.5}})
    )
    spec = ImproverSpec.load(tmp_path)
    assert spec.source == "file"
    assert spec.revision != DEFAULT_IMPROVER_REVISION
    assert spec.staleness_experiment_days == 1.5
    assert spec.staleness_wakes == 100  # merged over defaults

    (tmp_path / IMPROVER_FILENAME).write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="not a mapping"):
        ImproverSpec.load(tmp_path)


def test_revision_stamp_tracks_governance_source(tmp_path) -> None:
    # No standard installed: governance revision is None, improver defaults.
    stamp = revision_stamp(tmp_path)
    assert stamp == {
        "improver_revision": DEFAULT_IMPROVER_REVISION,
        "governance_revision": None,
    }

    # Legacy constitution: governance revision = the file hash.
    from wayfinder_paths.jobs.constitution import load_constitution

    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    (root / "constitution.yaml").write_text("enforcement: blocking\n")
    assert (
        revision_stamp(root)["governance_revision"]
        == load_constitution(root)["revision"]
    )

    # Migrated governance plane: composite revision, matching the facade.
    from wayfinder_paths.jobs.governance import migrate_from_constitution

    migrate_from_constitution(tmp_path, job_id, root)
    doc = load_constitution(root)
    assert doc["source"] == "governance"
    assert revision_stamp(root)["governance_revision"] == doc["revision"]


def test_journal_rows_carry_stamp(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    store.append_journal(job_id, {"type": "test_event"})
    row = json.loads(
        (store.job_dir(job_id) / "journal.jsonl").read_text().splitlines()[-1]
    )
    assert row["improver_revision"] == DEFAULT_IMPROVER_REVISION
    assert "governance_revision" in row

    # A caller-provided stamp (e.g. a replayed event) is not overridden.
    store.append_journal(job_id, {"type": "replay", "improver_revision": "U0-x"})
    row = json.loads(
        (store.job_dir(job_id) / "journal.jsonl").read_text().splitlines()[-1]
    )
    assert row["improver_revision"] == "U0-x"


def test_artifact_stamps_probation_archive_experiments_verdict(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)

    from wayfinder_paths.jobs.probation import record_probation_leg

    leg = record_probation_leg(
        store,
        job_id,
        name="leg-1",
        symbol="HYPE",
        size_fraction=0.25,
        graduate_criterion="beat",
        kill_criterion="hurt",
    )
    assert leg["improver_revision"] == DEFAULT_IMPROVER_REVISION
    assert "governance_revision" in leg

    from wayfinder_paths.jobs.archive import record_candidate

    entry = record_candidate(
        store,
        job_id,
        candidate_id="cand-1",
        family="volz",
        summary="test",
        status="frontier",
        objective={"net_log_growth": 0.01},
    )
    assert entry["improver_revision"] == DEFAULT_IMPROVER_REVISION

    from wayfinder_paths.jobs.execution.experiments import record_experiment

    grid_payload = {
        "revision": "rev-1",
        "dataset": {"days": 30},
        "result": {
            "grid_id": "g1",
            "rank_by": "net_return",
            "optimizer": "grid",
            "ranked": [],
            "runs": [{"run_id": "r1", "params": {}, "stats": {"net_return": 0.1}}],
            "invalid": [],
        },
    }
    row = record_experiment(job_id, grid_payload, store=store)
    assert row["improver_revision"] == DEFAULT_IMPROVER_REVISION
    trial = json.loads(
        (root / "results" / "backtest" / "trials.jsonl").read_text().splitlines()[-1]
    )
    assert trial["improver_revision"] == DEFAULT_IMPROVER_REVISION


def test_probation_caps_come_from_spec(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    (root / IMPROVER_FILENAME).write_text(
        yaml.safe_dump({"probation": {"max_active_legs": 1, "max_size_fraction": 0.3}})
    )
    from wayfinder_paths.jobs.probation import record_probation_leg

    with pytest.raises(ValueError, match=r"\(0, 0.3\]"):
        record_probation_leg(
            store,
            job_id,
            name="too-big",
            symbol="HYPE",
            size_fraction=0.4,
            graduate_criterion="g",
            kill_criterion="k",
        )
    record_probation_leg(
        store,
        job_id,
        name="leg-1",
        symbol="HYPE",
        size_fraction=0.2,
        graduate_criterion="g",
        kill_criterion="k",
    )
    with pytest.raises(ValueError, match="max 1 concurrent"):
        record_probation_leg(
            store,
            job_id,
            name="leg-2",
            symbol="BTC",
            size_fraction=0.2,
            graduate_criterion="g",
            kill_criterion="k",
        )


def test_research_priors_render_from_spec(tmp_path) -> None:
    from pathlib import Path

    from wayfinder_paths.jobs.worker import _render_research_priors

    text = Path("wayfinder_paths/jobs/prompts/research_priors.md").read_text()
    assert "%%T1_Q%%" in text  # doc holds tokens, not numbers
    rendered = _render_research_priors(text, ImproverSpec.load(tmp_path))
    assert "%%" not in rendered
    assert "q<=0.10 + 3/4 folds" in rendered
    assert "Max 2 concurrent probation legs" in rendered
    assert "2 consecutive neutral/hurt verdicts" in rendered


def test_staleness_block_uses_spec_thresholds(tmp_path) -> None:
    from wayfinder_paths.jobs.evolution_ledger import _research_staleness

    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    block = _research_staleness(root, proposals=[])
    assert block["stale"] is True  # no experiment ever -> research should start
    assert block["thresholds"]["experiment_days"] == 3.0

    import pandas as pd

    exp_path = root / "results" / "backtest" / "experiments.jsonl"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=6)).isoformat()
    exp_path.write_text(json.dumps({"ts": fresh}) + "\n")
    assert _research_staleness(root, proposals=[])["stale"] is False

    (root / IMPROVER_FILENAME).write_text(
        yaml.safe_dump({"staleness": {"experiment_days": 0.1}})
    )
    assert _research_staleness(root, proposals=[])["stale"] is True


def test_improver_change_proposal_validation(tmp_path) -> None:
    from wayfinder_paths.jobs.proposals import _validate_improver_payload

    _validate_improver_payload({"staleness": {"experiment_days": 2.0}})
    with pytest.raises(ValueError, match="sum to 1.0"):
        _validate_improver_payload(
            {"islands": {"weights": {"exploit": 0.9, "divergent": 0.5}}}
        )
    with pytest.raises(ValueError, match="max_size_fraction"):
        _validate_improver_payload({"probation": {"max_size_fraction": 1.5}})


def test_apply_improver_change_writes_spec_and_checks_drift(tmp_path) -> None:
    from wayfinder_paths.jobs.application import _apply_improver_change

    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    payload = {"version": 1, "staleness": {"experiment_days": 2.0}}
    proposal = {
        "proposal_id": "p1",
        "kind": "improver_change",
        "proposed_change": {"improver": payload},
        "base_improver_revision": DEFAULT_IMPROVER_REVISION,
    }
    _apply_improver_change(store, job_id, proposal)
    spec = ImproverSpec.load(root)
    assert spec.source == "file"
    assert spec.staleness_experiment_days == 2.0
    events = [
        json.loads(line) for line in (root / "journal.jsonl").read_text().splitlines()
    ]
    applied = [e for e in events if e["type"] == "improver_spec_applied"]
    assert applied and applied[-1]["applied_improver_revision"] == spec.revision

    # Spec moved since propose time -> refuse (lands in the rollback path).
    stale = dict(proposal, proposal_id="p2")
    with pytest.raises(ValueError, match="improver spec drift"):
        _apply_improver_change(store, job_id, stale)

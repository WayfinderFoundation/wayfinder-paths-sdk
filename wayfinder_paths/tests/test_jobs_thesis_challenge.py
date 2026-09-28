"""Jobs no evolution campaign improves: the intervene lane is their improver,
challenges the core thesis daily, and may propose on historical evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.improver.spec import ImproverSpec
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import (
    _THESIS_CHALLENGE_PATH,
    _build_worker_prompt_sections,
)
from wayfinder_paths.tests.test_wayfinder_jobs import (
    _valid_ideation_doc,
    _worker_snapshot,
)


def _job(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "one-off",
        script="workspace/src/loop.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    # A fresh ideation artifact: the daily expedition is not due, so this
    # wake is free for the thesis challenge.
    ideation = store.job_dir(job.id) / "research" / "ideation" / "latest.json"
    ideation.parent.mkdir(parents=True, exist_ok=True)
    ideation.write_text(
        json.dumps(_valid_ideation_doc(datetime.now(UTC).isoformat())),
        encoding="utf-8",
    )
    return store, job


def _prompt(store: JobStore, job: WayfinderJob) -> dict:
    return _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )


def _record_challenge(store: JobStore, job: WayfinderJob, *, hours_ago: float) -> None:
    path = store.job_dir(job.id) / _THESIS_CHALLENGE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC) - timedelta(hours=hours_ago)
    path.write_text(
        json.dumps({"challenged_at": stamp.isoformat(), "outcome": "thesis_holds"}),
        encoding="utf-8",
    )


def test_non_evolution_job_is_told_it_is_the_improver(tmp_path: Path) -> None:
    store, job = _job(tmp_path)

    sections = _prompt(store, job)

    assert "YOU ARE THIS JOB'S ONLY IMPROVER" in sections["stable_prefix"]
    assert (
        "NEVER blocks a proposal whose evidence is the job's own"
        in (sections["stable_prefix"])
    )


def test_thesis_challenge_is_due_daily(tmp_path: Path) -> None:
    store, job = _job(tmp_path)

    never = _prompt(store, job)["dynamic_context"]
    assert "THESIS CHALLENGE — due (it has never run" in never
    assert "This wake is a THESIS CHALLENGE" in never
    assert "one SIMPLER" in never and "one DIFFERENT mechanism" in never

    _record_challenge(store, job, hours_ago=3)
    assert "THESIS CHALLENGE" not in _prompt(store, job)["dynamic_context"]

    _record_challenge(store, job, hours_ago=30)
    stale = _prompt(store, job)["dynamic_context"]
    assert "THESIS CHALLENGE — due (it last ran 30h ago" in stale


def test_thesis_challenge_never_shares_the_ideation_wake(tmp_path: Path) -> None:
    store, job = _job(tmp_path)
    (store.job_dir(job.id) / "research" / "ideation" / "latest.json").unlink()

    context = _prompt(store, job)["dynamic_context"]

    assert "IDEATION SESSION" in context
    assert "THESIS CHALLENGE" not in context


def test_evolution_jobs_keep_the_sensor_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ImproverSpec,
        "evolution_eligibility",
        lambda self, root, job_id: {"eligible": True, "reasons": []},
    )
    store, job = _job(tmp_path)

    sections = _prompt(store, job)

    assert "THESIS CHALLENGE" not in sections["dynamic_context"]
    assert "YOU ARE THIS JOB'S ONLY IMPROVER" not in sections["stable_prefix"]

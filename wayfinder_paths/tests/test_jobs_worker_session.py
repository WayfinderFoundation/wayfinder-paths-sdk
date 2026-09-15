"""The worker's OpenCode session bootstrap must survive a slow health probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs import worker


class _FlakyClient:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = list(answers)
        self.created: list[str] = []

    def healthy(self) -> bool:
        return self.answers.pop(0) if self.answers else False

    def find_child_session(self, *, parent_id, title):  # noqa: ANN001
        return None

    def create_session(self, *, parent_id=None, title=None, agent=None):  # noqa: ANN001
        self.created.append(str(title))
        return "ses-created"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    return slept


def test_worker_session_survives_a_late_health_probe(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    client = _FlakyClient([False, False, True])
    monkeypatch.setattr(worker, "OPENCODE_CLIENT", client)
    assert worker._ensure_worker_session("job-a", "monitor") == "ses-created"
    assert client.created == ["job/job-a/monitor"]
    assert len(no_sleep) == 2


def test_worker_session_gives_up_after_the_last_probe(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    client = _FlakyClient([False, False, False])
    monkeypatch.setattr(worker, "OPENCODE_CLIENT", client)
    assert worker._ensure_worker_session("job-a", "monitor") is None
    assert client.created == []
    assert len(no_sleep) == 2


def test_unqueued_wake_report_keeps_the_last_real_check(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "wake-demo",
        script="workspace/src/tick.py",
        interval_seconds=60,
        timeout_seconds=60,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle", "origin": "inline"},
    )
    store.init_layout(job)
    store.save(job)

    worker._write_report(
        store=store,
        job_id=job.id,
        mode="monitor",
        status="green",
        summary="monitor wakeup queued in OpenCode session ses-1",
        session_id="ses-1",
        queued=True,
        error=None,
    )
    checked = store.read_json(job.id, "scorecard.json")
    assert checked["last_agent_summary"].startswith("monitor wakeup queued")

    worker._write_report(
        store=store,
        job_id=job.id,
        mode="monitor",
        status="yellow",
        summary="Worker could not queue an OpenCode wakeup",
        session_id=None,
        queued=False,
        error="OpenCode server unavailable",
        stamp_check=False,
    )
    after = store.read_json(job.id, "scorecard.json")
    assert after["last_agent_check_at"] == checked["last_agent_check_at"]
    assert after["last_agent_summary"] == checked["last_agent_summary"]
    assert after["health"] == "green"
    assert after["last_agent_wake_error"] == "OpenCode server unavailable"
    assert after["last_agent_wake_error_at"] > after["last_agent_check_at"]
    # The failed attempt is still on disk for the Activity feed and journal.
    report = store.read_json(job.id, "reports/monitor/latest.json")
    assert (
        report["queued"] is False and report["error"] == "OpenCode server unavailable"
    )

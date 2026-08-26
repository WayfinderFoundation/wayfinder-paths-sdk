"""Wake economy: saturated paper jobs skip LLM wakes until evidence moves,
and a backed-off remediation case never satisfies a wake."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.exhaustion import file_exhaustion_claim
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.remediation import (
    sync_remediation_with_health,
    update_remediation_progress,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.wake_economy import (
    REMEDIATION_QUIET_LINE,
    WAKE_ECONOMY_PATH,
    maybe_skip_wake,
    record_full_wake,
    research_saturation_posture,
    saturation_watermark,
)
from wayfinder_paths.jobs.worker import (
    _build_worker_prompt_sections,
    _standing_checks_block,
    run_job_worker,
)
from wayfinder_paths.tests.test_jobs_remediation import _health
from wayfinder_paths.tests.test_wayfinder_jobs import _worker_snapshot


class ForbiddenOpenCodeClient:
    """A skipped wake must never touch the OpenCode client."""

    def healthy(self) -> bool:
        raise AssertionError("OpenCode client touched on a skipped wake")

    def find_child_session(self, **kwargs) -> str | None:  # noqa: ANN003
        raise AssertionError("OpenCode client touched on a skipped wake")

    def create_session(self, **kwargs) -> str:  # noqa: ANN003
        raise AssertionError("OpenCode client touched on a skipped wake")

    def prompt_async(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        raise AssertionError("OpenCode client touched on a skipped wake")


class FakeOpenCodeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def healthy(self) -> bool:
        return True

    def find_child_session(self, *, parent_id, title):  # noqa: ANN001
        return None

    def create_session(self, *, parent_id=None, title=None, agent=None):  # noqa: ANN001
        return "session-wake-economy"

    def prompt_async(self, session_id: str, text: str, *, agent=None) -> bool:  # noqa: ANN001
        self.prompts.append(text)
        return True


def _saturated_job(
    tmp_path: Path, job_id: str = "quiet-demo"
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/loop.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    # Operational (bootstrap owns never-operational jobs, not the economy).
    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    return store, job


def _record_closed_trades(store: JobStore, job_id: str, count: int) -> None:
    summary = store.read_json(job_id, "results/forward/summary.json", default={}) or {}
    trades = dict(summary.get("trades") or {})
    trades["closed_count"] = count
    trades["last_trade_at"] = f"2026-08-25T12:00:{count % 60:02d}+00:00"
    summary["trades"] = trades
    store.write_json(job_id, "results/forward/summary.json", summary)


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line).get("type"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_posture_saturated_when_nothing_is_mid_flight(tmp_path: Path) -> None:
    store, job = _saturated_job(tmp_path)

    posture = research_saturation_posture(store, job.id)

    assert posture == {"posture": "saturated", "blockers": []}


def test_each_mid_flight_condition_defeats_saturation(tmp_path: Path) -> None:
    cases = [
        (
            "not_operational",
            lambda store, job: store.refresh_scorecard(
                job.id, {"last_script_run_at": None}
            ),
        ),
        (
            "research_lane_active",
            lambda store, job: store.write_json(
                job.id, "state/research_lane.json", {"active_lane": "vol-regimes"}
            ),
        ),
        (
            "impasse_mandate_outstanding",
            lambda store, job: store.write_json(
                job.id, "state/research_impasse.json", {"alerted_at": utc_now_iso()}
            ),
        ),
        (
            "exhaustion_claim_pending",
            lambda store, job: file_exhaustion_claim(
                store,
                job.id,
                lane="funding-lane",
                evidence="all cells negative",
                provenance="data-wall",
                next_region="session-effects",
            ),
        ),
        (
            "proposals_in_flight",
            lambda store, job: (
                store.write_proposal(
                    job.id, {"proposal_id": "prop-a", "status": "pending"}
                ),
                store.refresh_scorecard(job.id),
            ),
        ),
        (
            "remediation_case_active",
            lambda store, job: sync_remediation_with_health(store, job.id, _health()),
        ),
        (
            "forward_sample_adequate",
            lambda store, job: _record_closed_trades(store, job.id, 20),
        ),
    ]
    for index, (blocker, arrange) in enumerate(cases):
        store, job = _saturated_job(tmp_path / f"case-{index}", f"posture-{index}")
        arrange(store, job)

        posture = research_saturation_posture(store, job.id)

        assert posture["posture"] == "in_flight", blocker
        assert posture["blockers"] == [blocker]


def test_backed_off_remediation_case_does_not_defeat_saturation(
    tmp_path: Path,
) -> None:
    store, job = _saturated_job(tmp_path)
    sync_remediation_with_health(store, job.id, _health())
    update_remediation_progress(
        store, job.id, state="blocked", note="Waiting for forward evidence"
    )

    assert research_saturation_posture(store, job.id) == {
        "posture": "saturated",
        "blockers": [],
    }


def test_skip_path_writes_quiet_report_without_touching_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _saturated_job(tmp_path)
    record_full_wake(store, job)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.OPENCODE_CLIENT", ForbiddenOpenCodeClient()
    )

    report = run_job_worker(job.id, mode="intervene")

    assert report["status"] == "quiet"
    assert report["skip_reason"] == "saturation_watermark_unchanged"
    assert report["watermark"]["closed_trades"] == 0
    assert report["next_full_wake_by"]
    latest = json.loads(
        (store.job_dir(job.id) / "reports" / "intervene" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["status"] == "quiet"
    assert _journal_types(store, job.id).count("wake_skipped_saturated") == 1

    # Repeat skips roll into the state counter — no second heartbeat entry.
    second = run_job_worker(job.id, mode="intervene")
    assert second["status"] == "quiet"
    assert second["skips"]["count"] == 2
    assert _journal_types(store, job.id).count("wake_skipped_saturated") == 1


def test_watermark_movement_forces_full_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _saturated_job(tmp_path)
    record_full_wake(store, job)
    client = FakeOpenCodeClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)

    # One closed trade (still below the gate minimum → still saturated) moves
    # the watermark: the next wake must run in full and re-anchor the state.
    _record_closed_trades(store, job.id, 1)
    report = run_job_worker(job.id, mode="intervene")

    assert report["status"] == "green"
    assert len(client.prompts) == 1
    state = store.read_json(job.id, WAKE_ECONOMY_PATH)
    assert state["watermark"]["closed_trades"] == 1
    assert "skips" not in state

    # Watermark unchanged after the re-anchor: the follow-up wake skips.
    followup = run_job_worker(job.id, mode="intervene")
    assert followup["status"] == "quiet"
    assert len(client.prompts) == 1


def test_quiet_max_floor_forces_full_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _saturated_job(tmp_path)
    record_full_wake(store, job, now=datetime.now(UTC) - timedelta(hours=25))

    assert maybe_skip_wake(store, job, mode="intervene", apply_proposal_id=None) is None


def test_live_apply_and_auto_wakes_never_skip(tmp_path: Path) -> None:
    store, job = _saturated_job(tmp_path)
    record_full_wake(store, job)

    assert (
        maybe_skip_wake(store, job, mode="intervene", apply_proposal_id="prop-1")
        is None
    )
    assert maybe_skip_wake(store, job, mode="auto", apply_proposal_id=None) is None

    job.script_loop.mode = "live"
    store.save(job)
    assert maybe_skip_wake(store, job, mode="intervene", apply_proposal_id=None) is None


def test_kill_switch_disables_the_skip_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_WAKE_ECONOMY", "0")
    store, job = _saturated_job(tmp_path)
    record_full_wake(store, job)

    assert maybe_skip_wake(store, job, mode="intervene", apply_proposal_id=None) is None


def test_prompt_quiet_line_present_exactly_when_remediation_quiet(
    tmp_path: Path,
) -> None:
    store, job = _saturated_job(tmp_path)

    def prompt() -> str:
        return _build_worker_prompt_sections(
            store=store,
            job_id=job.id,
            mode="intervene",
            snapshot=_worker_snapshot(job),
        )["prompt"]

    # No case: neither the override directive nor the quiet line.
    baseline = prompt()
    assert "REGIME REMEDIATION REQUIRED" not in baseline
    assert REMEDIATION_QUIET_LINE not in baseline

    # Open case without an accountable outcome: the override directive.
    sync_remediation_with_health(store, job.id, _health())
    open_prompt = prompt()
    assert "REGIME REMEDIATION REQUIRED" in open_prompt
    assert REMEDIATION_QUIET_LINE not in open_prompt

    # Blocked with a recorded note and unmoved evidence: quiet line only.
    update_remediation_progress(
        store, job.id, state="blocked", note="Waiting for forward evidence"
    )
    quiet_prompt = prompt()
    assert REMEDIATION_QUIET_LINE in quiet_prompt
    assert "REGIME REMEDIATION REQUIRED" not in quiet_prompt

    # Forward evidence moved since the note: the case is due again.
    _record_closed_trades(store, job.id, 1)
    due_prompt = prompt()
    assert "REGIME REMEDIATION REQUIRED" in due_prompt
    assert REMEDIATION_QUIET_LINE not in due_prompt


def test_standing_checks_carry_remediation_recheck_block(tmp_path: Path) -> None:
    store, job = _saturated_job(tmp_path)
    sync_remediation_with_health(store, job.id, _health())
    update_remediation_progress(
        store, job.id, state="blocked", note="Waiting for forward evidence"
    )

    block = _standing_checks_block(store.job_dir(job.id), store=store, job_id=job.id)

    remediation = block["remediation"]
    assert remediation["state"] == "blocked"
    assert remediation["recheck"]["next_retry_seconds"] > 0
    assert remediation["next_recheck_at"] > utc_now_iso()


def test_saturation_watermark_components_move_independently(tmp_path: Path) -> None:
    store, job = _saturated_job(tmp_path)
    base = saturation_watermark(store, job.id)

    _record_closed_trades(store, job.id, 1)
    assert saturation_watermark(store, job.id) != base

    store2, job2 = _saturated_job(tmp_path / "legs", "legs-demo")
    base2 = saturation_watermark(store2, job2.id)
    store2.write_json(
        job2.id,
        "probation.json",
        {"legs": [{"name": "leg-a", "status": "active"}]},
    )
    assert saturation_watermark(store2, job2.id) != base2

    store3, job3 = _saturated_job(tmp_path / "campaign", "campaign-demo")
    base3 = saturation_watermark(store3, job3.id)
    store3.write_json(
        job3.id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active", "stage": "generate"},
    )
    assert saturation_watermark(store3, job3.id) != base3
    posture = research_saturation_posture(store3, job3.id)
    assert posture["posture"] == "in_flight"
    assert "evolution_campaign_open" in posture["blockers"]

    store4, job4 = _saturated_job(tmp_path / "experiment", "experiment-demo")
    base4 = saturation_watermark(store4, job4.id)
    experiment = {
        "experiment_id": "experiment-1",
        "status": "qualifying",
        "admissions": {"control": 0, "evolution": 0},
        "arms": {
            arm: {"champion": {"revision": f"{arm}-baseline"}}
            for arm in ("control", "evolution")
        },
        "proposals": {
            arm: {"active": None, "history": []} for arm in ("control", "evolution")
        },
    }
    store4.write_json(job4.id, "state/evolution_experiment.json", experiment)
    staged = saturation_watermark(store4, job4.id)
    assert staged != base4

    experiment["proposals"]["evolution"]["active"] = {
        "candidate_id": "candidate-1",
        "revision": "revision-1",
        "status": "queued",
        "last_common_bar": "2026-08-25T00:00:00+00:00",
    }
    store4.write_json(job4.id, "state/evolution_experiment.json", experiment)
    active = saturation_watermark(store4, job4.id)
    assert active != staged

    experiment["proposals"]["evolution"]["active"]["last_common_bar"] = (
        "2026-08-25T01:00:00+00:00"
    )
    store4.write_json(job4.id, "state/evolution_experiment.json", experiment)
    assert saturation_watermark(store4, job4.id) == active

    experiment["proposals"]["evolution"] = {
        "active": None,
        "history": [
            {
                "candidate_id": "candidate-1",
                "revision": "revision-1",
                "status": "rejected",
            }
        ],
    }
    store4.write_json(job4.id, "state/evolution_experiment.json", experiment)
    assert saturation_watermark(store4, job4.id) != active

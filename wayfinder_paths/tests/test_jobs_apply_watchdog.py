"""Deterministic apply + application watchdog: no proposal apply may stall a
job. Covers the launcher (claim + detached completer, spawn-failure safety),
the watchdog recovery matrix (applying/queued x deterministic/agent-owned x
completer liveness), the torn-workspace backup preservation, and the removal
of the worker's claim-before-prompt path."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.application import claim_application, complete_application
from wayfinder_paths.jobs.apply_launcher import launch_application, start_application
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.watchdog import (
    WATCHDOG_RUNNER_JOB_NAME,
    _run_evolution_campaign_pass,
    ensure_application_watchdog,
    recover_stalled_applications,
)
from wayfinder_paths.jobs.worker import run_job_worker
from wayfinder_paths.tests.test_wayfinder_jobs import (
    _intent_contract,
    _prepare_candidate_script,
    _scenario_plan,
)


def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def pause(self, name: str) -> dict:
            calls.append(("pause", name))
            return {"ok": True}

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):  # noqa: ANN003
            calls.append(("ensure_watchdog", kwargs["name"]))
            return {"ok": True}

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job):  # noqa: ANN001
            calls.append(("compile", job.id))
            return {"job_id": job.id, "jobs": []}

    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", FakeBridge)
    monkeypatch.setattr("wayfinder_paths.jobs.application.JobCompiler", FakeCompiler)
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.RunnerBridge", FakeBridge)
    return calls


def _make_job(store: JobStore, job_id: str) -> WayfinderJob:
    job = WayfinderJob.new(
        job_id,
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    return job


def _write_proposal(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    status: str = "approved",
    application: dict | None = None,
    candidate_report: dict | None = None,
) -> None:
    proposal = {
        "proposal_id": proposal_id,
        "job_id": job_id,
        "status": status,
        "application": application or {"status": "queued"},
        "proposed_change": {"summary": "Tighten the entry guard."},
        "intent_contract": _intent_contract(),
        "scenario_plan": _scenario_plan(),
    }
    if candidate_report is not None:
        proposal["candidate_report"] = candidate_report
    store.write_proposal(job_id, proposal)


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _iso_ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def test_start_application_claims_spawns_and_records_pid(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "spawn-demo")
    _write_proposal(store, job.id, "prop_spawn", candidate_report={"gate": "green"})

    spawned: list[dict] = []

    def fake_spawn(*, job_id, proposal_id, store):  # noqa: ANN001
        spawned.append({"job_id": job_id, "proposal_id": proposal_id})
        return 4242

    result = start_application(store, job.id, "prop_spawn", spawn=fake_spawn)

    assert result["spawned"] is True
    assert spawned == [{"job_id": job.id, "proposal_id": "prop_spawn"}]
    proposal = store.load_proposal(job.id, "prop_spawn")
    assert proposal["application"]["status"] == "applying"
    assert proposal["application"]["apply_worker"]["pid"] == 4242
    assert ("pause", "spawn-demo-script") in calls
    assert ("pause", "spawn-demo-agent") in calls
    assert "application_apply_spawned" in _journal_types(store, job.id)


def test_start_application_spawn_failure_fails_and_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "spawn-fail-demo")
    _write_proposal(store, job.id, "prop_boom", candidate_report={"gate": "green"})

    def broken_spawn(**_kwargs):  # noqa: ANN003
        raise OSError("fork bomb prevention")

    result = start_application(store, job.id, "prop_boom", spawn=broken_spawn)

    assert result["spawned"] is False
    proposal = store.load_proposal(job.id, "prop_boom")
    assert proposal["application"]["status"] == "failed"
    assert ("resume", "spawn-fail-demo-script") in calls
    assert ("resume", "spawn-fail-demo-agent") in calls


def test_launch_application_routes_by_candidate_report(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "route-demo")

    wakes: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.run_job_worker",
        lambda job_id, mode="monitor", *, apply_proposal_id=None: wakes.append(
            apply_proposal_id
        )
        or {"status": "green"},
    )

    _write_proposal(store, job.id, "prop_ungated")
    ungated = launch_application(store, job.id, "prop_ungated")

    assert ungated["mode"] == "agent_wake"
    assert wakes == ["prop_ungated"]
    # The ungated path must NOT claim: loops keep running, status stays queued.
    assert (
        store.load_proposal(job.id, "prop_ungated")["application"]["status"] == "queued"
    )
    assert ("pause", "route-demo-script") not in calls

    monkeypatch.setattr(
        "wayfinder_paths.jobs.apply_launcher._default_spawn",
        lambda *, job_id, proposal_id, store: 4243,
    )
    _write_proposal(store, job.id, "prop_gated", candidate_report={"gate": "green"})
    gated = launch_application(store, job.id, "prop_gated")
    assert gated["mode"] == "deterministic"
    assert gated["spawned"] is True
    assert ("pause", "route-demo-script") in calls


def test_watchdog_recovers_stalled_deterministic_applying(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-det-demo")
    _write_proposal(
        store,
        job.id,
        "prop_stalled",
        application={"status": "applying", "started_at": _iso_ago(30)},
        candidate_report={"gate": "green"},
    )

    completions: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda store, job_id, proposal_id, *, status, **kw: completions.append(
            (job_id, proposal_id, status)
        )
        or {},
    )

    report = recover_stalled_applications(store=store)

    assert completions == [(job.id, "prop_stalled", "applied")]
    assert report["errors"] == []
    assert len(report["recovered"]) == 1
    assert "application_watchdog_recovered" in _journal_types(store, job.id)


def test_watchdog_fails_stalled_agent_owned_applying(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-agent-demo")

    completions: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda store, job_id, proposal_id, *, status, **kw: completions.append(
            (proposal_id, status)
        )
        or {},
    )

    # Inside the agent window: untouched.
    _write_proposal(
        store,
        job.id,
        "prop_agent_fresh",
        application={"status": "applying", "started_at": _iso_ago(30)},
    )
    recover_stalled_applications(store=store)
    assert completions == []

    # Past the agent window: failed so loops resume; claim can retry later.
    _write_proposal(
        store,
        job.id,
        "prop_agent_stale",
        application={"status": "applying", "started_at": _iso_ago(90)},
    )
    recover_stalled_applications(store=store)
    assert completions == [("prop_agent_stale", "failed")]


def test_watchdog_recovers_orphaned_apply_from_top(tmp_path: Path, monkeypatch) -> None:
    """An apply orphaned by daemon death (status "applying", stale started_at,
    dead completer pid) is recovered well before the full applying window and
    re-entered from the TOP of the completer pipeline — complete_application
    re-runs the drift guard + full candidate validation, never a mid-stage
    resume. (2026-08-24: runnerd RSS-exited 17s into an owner-approved apply;
    the old signature waited out the full window.)"""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-orphan-demo")
    _write_proposal(
        store,
        job.id,
        "prop_orphan",
        application={
            "status": "applying",
            "started_at": _iso_ago(6),
            "apply_worker": {"pid": 424242, "spawned_at": utc_now_iso()},
        },
        candidate_report={"gate": "green"},
    )
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog._pid_alive", lambda pid: False)

    completions: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda store, job_id, proposal_id, *, status, **kw: completions.append(
            (proposal_id, status)
        )
        or {},
    )

    report = recover_stalled_applications(store=store)

    assert completions == [("prop_orphan", "applied")]
    events = [e for e in report["recovered"] if e.get("stalled_status") == "applying"]
    assert len(events) == 1
    assert events[0]["orphaned"] is True
    assert events[0]["worker_pid"] == 424242
    assert "application_watchdog_recovered" in _journal_types(store, job.id)


def test_watchdog_gives_dead_completer_a_short_grace(
    tmp_path: Path, monkeypatch
) -> None:
    """Inside the orphan grace window (claim→spawn gap, slow pid record) a
    dead/absent completer pid is not yet a stall."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-orphan-fresh")
    _write_proposal(
        store,
        job.id,
        "prop_fresh_orphan",
        application={"status": "applying", "started_at": _iso_ago(2)},
        candidate_report={"gate": "green"},
    )
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog._pid_alive", lambda pid: False)

    completions: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda *a, **kw: completions.append("called") or {},
    )

    report = recover_stalled_applications(store=store)

    assert completions == []
    assert report["recovered"] == []


def test_watchdog_orphan_fast_path_is_deterministic_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent-owned applies record no completer pid — a dead-pid signature must
    not cut their 60-minute window short."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-orphan-agent")
    _write_proposal(
        store,
        job.id,
        "prop_agent_orphan",
        application={"status": "applying", "started_at": _iso_ago(30)},
    )
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog._pid_alive", lambda pid: False)

    completions: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda *a, **kw: completions.append("called") or {},
    )

    report = recover_stalled_applications(store=store)

    assert completions == []
    assert report["recovered"] == []


def test_watchdog_skips_live_completer(tmp_path: Path, monkeypatch) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-live-demo")
    _write_proposal(
        store,
        job.id,
        "prop_live",
        application={
            "status": "applying",
            "started_at": _iso_ago(20),
            "apply_worker": {"pid": os.getpid(), "spawned_at": utc_now_iso()},
        },
        candidate_report={"gate": "green"},
    )

    completions: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application",
        lambda *a, **kw: completions.append("called") or {},
    )

    report = recover_stalled_applications(store=store)

    assert completions == []
    assert report["recovered"] == []


def test_watchdog_recovers_queued_approved_with_candidate_report(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-queued-demo")

    started: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.apply_launcher.start_application",
        lambda store, job_id, proposal_id, **kw: started.append(proposal_id) or {},
    )

    # Ungated queued: agent-owned, loops running — never touched.
    _write_proposal(
        store,
        job.id,
        "prop_q_ungated",
        application={"status": "queued", "requested_at": _iso_ago(30)},
    )
    # Gated queued, fresh: inside the window — untouched.
    _write_proposal(
        store,
        job.id,
        "prop_q_fresh",
        application={"status": "queued", "requested_at": _iso_ago(2)},
        candidate_report={"gate": "green"},
    )
    # Gated queued, stale: approve→spawn crash window — recovered.
    _write_proposal(
        store,
        job.id,
        "prop_q_stale",
        application={"status": "queued", "requested_at": _iso_ago(30)},
        candidate_report={"gate": "green"},
    )

    recover_stalled_applications(store=store)

    assert started == ["prop_q_stale"]


def test_watchdog_race_already_completed_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "wd-race-demo")
    _write_proposal(
        store,
        job.id,
        "prop_race",
        application={"status": "applying", "started_at": _iso_ago(30)},
        candidate_report={"gate": "green"},
    )

    def losing_complete(*_a, **_kw):
        raise ValueError("Proposal application is not applying")

    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.complete_application", losing_complete
    )

    report = recover_stalled_applications(store=store)

    assert report["errors"] == []
    assert report["recovered"] == []
    assert "application_watchdog_skipped" in _journal_types(store, job.id)


def test_torn_workspace_backup_preserved(tmp_path: Path, monkeypatch) -> None:
    """Crash between rmtree(active) and copytree(candidate): the re-run must
    not rebuild the backup from the missing active tree — the old backup is
    the only copy of the pre-apply workspace."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "torn-demo")
    _write_proposal(store, job.id, "prop_torn")

    claim_application(store, job.id, "prop_torn")
    _prepare_candidate_script(
        store,
        job.id,
        "prop_torn",
        rearm_reason="rearm_guard: SNX still below SMA50.",
    )

    root = store.job_dir(job.id)
    backup_dir = root / "applications" / "prop_torn" / "backup"
    (backup_dir / "workspace").mkdir(parents=True)
    (backup_dir / "workspace" / "ORIGINAL.md").write_text(
        "pre-apply state", encoding="utf-8"
    )
    (backup_dir / "job.yaml").write_text(
        (root / "job.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    # Simulate the torn state: active workspace is gone.
    import shutil

    shutil.rmtree(root / "workspace")

    completed = complete_application(
        store,
        job.id,
        "prop_torn",
        status="applied",
        changed_files=["workspace/src/fast_loop.py"],
        allow_legacy=True,
    )

    assert completed["proposal"]["application"]["status"] == "applied"
    # The pre-apply backup survived the re-run…
    assert (backup_dir / "workspace" / "ORIGINAL.md").read_text(
        encoding="utf-8"
    ) == "pre-apply state"
    # …and the active workspace was restored from the candidate.
    assert (root / "workspace" / "src" / "fast_loop.py").exists()


def _stamp_candidate_revisions(store: JobStore, job_id: str, proposal_id: str) -> None:
    """Record base/candidate revisions the way the propose flow does."""
    from wayfinder_paths.jobs.gating import compute_workspace_revision

    proposal = store.load_proposal(job_id, proposal_id)
    proposal["base_revision"] = compute_workspace_revision(store.job_dir(job_id))
    candidate_dir = store.repo_root / proposal["application"]["candidate_dir"]
    proposal["candidate_report"] = {
        "revision": compute_workspace_revision(candidate_dir)
    }
    store.write_proposal(job_id, proposal)


def test_complete_applied_refuses_stale_baseline(tmp_path: Path, monkeypatch) -> None:
    """A candidate staged before an intervening apply must not promote: the
    wholesale workspace replace would revert the intervening change."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "drift-demo")
    _write_proposal(store, job.id, "prop_drift")

    claim_application(store, job.id, "prop_drift")
    _prepare_candidate_script(
        store,
        job.id,
        "prop_drift",
        rearm_reason="rearm_guard: SNX still below SMA50.",
    )
    _stamp_candidate_revisions(store, job.id, "prop_drift")

    # An intervening apply moves the active workspace past the staged base.
    root = store.job_dir(job.id)
    intervening = root / "workspace" / "src" / "graduated_sizing.py"
    intervening.parent.mkdir(parents=True, exist_ok=True)
    intervening.write_text("HYPE_FRACTION = 0.25\n", encoding="utf-8")

    completed = complete_application(
        store,
        job.id,
        "prop_drift",
        status="applied",
        allow_legacy=True,
    )

    application = completed["proposal"]["application"]
    assert application["status"] == "failed"
    assert "baseline drift" in str(application.get("error"))
    # The intervening change survived — no wholesale revert.
    assert intervening.read_text(encoding="utf-8") == "HYPE_FRACTION = 0.25\n"
    assert "stale_baseline_promotion_refused" in _journal_types(store, job.id)


def test_complete_applied_allows_crash_resume_after_promotion(
    tmp_path: Path, monkeypatch
) -> None:
    """If a completer crashed after the promotion itself, the active workspace
    already equals the candidate — re-completion must finish, not refuse."""
    import shutil

    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "resume-demo")
    _write_proposal(store, job.id, "prop_resume")

    claim_application(store, job.id, "prop_resume")
    _prepare_candidate_script(
        store,
        job.id,
        "prop_resume",
        rearm_reason="rearm_guard: SNX still below SMA50.",
    )
    _stamp_candidate_revisions(store, job.id, "prop_resume")

    # Simulate a crash after _promote_candidate: active == candidate content
    # (promotion copies both the workspace and job.yaml).
    root = store.job_dir(job.id)
    proposal = store.load_proposal(job.id, "prop_resume")
    candidate_dir = store.repo_root / proposal["application"]["candidate_dir"]
    shutil.rmtree(root / "workspace")
    shutil.copytree(candidate_dir / "workspace", root / "workspace")
    shutil.copy2(candidate_dir / "job.yaml", root / "job.yaml")

    completed = complete_application(
        store,
        job.id,
        "prop_resume",
        status="applied",
        allow_legacy=True,
    )

    assert completed["proposal"]["application"]["status"] == "applied"
    assert "stale_baseline_promotion_refused" not in _journal_types(store, job.id)


def test_run_job_worker_apply_does_not_claim(tmp_path: Path, monkeypatch) -> None:
    calls = _patch_runner(monkeypatch)

    class FakeOpenCodeClient:
        def healthy(self) -> bool:
            return True

        def find_child_session(self, *, parent_id, title):  # noqa: ANN001
            return None

        def create_session(self, *, parent_id=None, title=None, agent=None):  # noqa: ANN001
            return "session-no-claim"

        def prompt_async(self, session_id: str, text: str, *, agent=None) -> bool:  # noqa: ANN001
            return True

    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "no-claim-demo")
    _write_proposal(store, job.id, "prop_noclaim", candidate_report={"gate": "green"})
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.OPENCODE_CLIENT", FakeOpenCodeClient()
    )

    report = run_job_worker(job.id, mode="intervene", apply_proposal_id="prop_noclaim")

    assert report["status"] == "green"
    assert (
        store.load_proposal(job.id, "prop_noclaim")["application"]["status"] == "queued"
    )
    assert not any(call for call in calls if call[0] == "pause")


def test_ensure_application_watchdog_idempotent(tmp_path: Path, monkeypatch) -> None:
    registrations: list[dict] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def add_or_update_script_job(self, **kwargs):  # noqa: ANN003
            registrations.append(kwargs)
            return {"ok": True}

    store = JobStore(repo_root=tmp_path)
    bridge = FakeBridge(repo_root=tmp_path)

    first = ensure_application_watchdog(store=store, bridge=bridge)
    second = ensure_application_watchdog(store=store, bridge=bridge)

    assert first["runner_job_name"] == WATCHDOG_RUNNER_JOB_NAME
    assert second["runner_job_name"] == WATCHDOG_RUNNER_JOB_NAME
    assert len(registrations) == 2
    assert all(r["name"] == WATCHDOG_RUNNER_JOB_NAME for r in registrations)
    driver = store.runs_jobs_dir / "application_watchdog.py"
    assert driver.exists()
    assert "recover_stalled_applications" in driver.read_text(encoding="utf-8")


def test_claim_survives_sync_failure(tmp_path: Path, monkeypatch) -> None:
    # The post-claim backend sync is telemetry: a backend hiccup there must
    # not sever the claim->spawn chain (2026-07-23 incident: claim stood,
    # completer never spawned, job dark until watchdog recovery).
    calls = _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "sync-fail-demo")
    _write_proposal(store, job.id, "prop_syncfail", candidate_report={"gate": "ok"})

    def broken_sync(*, store):  # noqa: ANN001
        raise RuntimeError("backend 502")

    monkeypatch.setattr("wayfinder_paths.jobs.application.sync_all_jobs", broken_sync)

    claimed = claim_application(store, job.id, "prop_syncfail")

    assert claimed["proposal"]["application"]["status"] == "applying"
    assert ("pause", "sync-fail-demo-script") in calls
    assert "claim_sync_failed" in _journal_types(store, job.id)


def _write_restage_proposal(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    params: dict | None,
    finished_minutes_ago: float = 45,
) -> None:
    proposal = {
        "proposal_id": proposal_id,
        "job_id": job_id,
        "status": "approved",
        "proposed_change": {
            "summary": "Approved change awaiting re-stage",
            **({"execution_params": params} if params else {}),
        },
        "intent_contract": _intent_contract(),
        "scenario_plan": _scenario_plan(),
        "candidate_report": {"revision": "abc123def456"},
        "application": {
            "status": "failed",
            "restage_requested": True,
            "finished_at": _iso_ago(finished_minutes_ago),
        },
    }
    store.write_proposal(job_id, proposal)


def test_watchdog_mechanically_restages_params_carryover(
    tmp_path: Path, monkeypatch
) -> None:
    """A params re-stage needs no authoring: the watchdog re-stages it
    directly instead of waiting on an agent session — bounded to one
    mechanical re-stage per pass (each re-runs the gate backtest)."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "restage-mech")
    _write_restage_proposal(store, job.id, "prop_params_a", params={"stop_pct": 0.01})
    _write_restage_proposal(store, job.id, "prop_params_b", params={"tp_pct": 0.05})

    restaged: list[str] = []

    def fake_restage(store_, job_id, proposal_id):  # noqa: ANN001
        restaged.append(proposal_id)
        return {"status": "approved", "application": {"status": "queued"}}

    monkeypatch.setattr("wayfinder_paths.jobs.proposals.restage_proposal", fake_restage)

    result = recover_stalled_applications(store=store)

    assert len(restaged) == 1, "one mechanical restage per watchdog pass"
    actions = [event["action"] for event in result["recovered"]]
    assert actions == ["mechanical_restage"]
    assert "application_watchdog_recovered" in _journal_types(store, job.id)

    # Next pass picks up the second one.
    restaged.clear()
    recover_stalled_applications(store=store)
    assert len(restaged) == 1


def test_watchdog_renags_code_change_restage(tmp_path: Path, monkeypatch) -> None:
    """A code-change re-stage the agent has not resolved gets the wake
    re-fired after the nag window — once per window, not every pass."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "restage-nag")
    _write_restage_proposal(store, job.id, "prop_code_x", params=None)

    fired: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.triggers.fire_triggers",
        lambda store_, job_, events, *, source: fired.append(source) or {"ok": True},
    )

    result = recover_stalled_applications(store=store)
    assert fired == ["watchdog-nag:prop_code_x"]
    assert [e["action"] for e in result["recovered"]] == ["restage_wake_nag"]

    # Within the nag window: no re-fire.
    second = recover_stalled_applications(store=store)
    assert fired == ["watchdog-nag:prop_code_x"]
    assert second["recovered"] == []


def test_watchdog_leaves_fresh_code_restage_to_the_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Inside the nag window the agent still owns the task — no watchdog
    action, no duplicate wake."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "restage-fresh")
    _write_restage_proposal(
        store, job.id, "prop_code_y", params=None, finished_minutes_ago=5
    )

    fired: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.triggers.fire_triggers",
        lambda *a, **k: fired.append(1),
    )

    result = recover_stalled_applications(store=store)
    assert fired == []
    assert result["recovered"] == []


def _write_resume_failed_proposal(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    ok: bool = False,
    recovered: bool = False,
    app_status: str = "applied",
) -> None:
    response = {"ok": True} if ok else {"ok": False, "error": "connect_failed"}
    proposal = {
        "proposal_id": proposal_id,
        "job_id": job_id,
        "status": "approved",
        "proposed_change": {"summary": "Applied change"},
        "intent_contract": _intent_contract(),
        "scenario_plan": _scenario_plan(),
        "application": {
            "status": app_status,
            "finished_at": _iso_ago(90),
            "runner_responses": [
                {
                    "loop": "script",
                    "runner_job_name": f"{job_id}-script",
                    "response": response,
                },
                {
                    "loop": "agent",
                    "runner_job_name": f"{job_id}-agent",
                    "response": response,
                },
            ],
            **({"resume_recovered": True} if recovered else {}),
        },
    }
    store.write_proposal(job_id, proposal)


def test_watchdog_resumes_orphaned_pause(tmp_path: Path, monkeypatch) -> None:
    """Resumes that timed out at apply completion are re-issued until they
    succeed — a completed apply must never leave the lanes dark."""
    calls = _patch_runner(monkeypatch)

    # _recover_orphaned_pause constructs RunnerBridge directly — patch it there.
    class RecordingBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True, "result": {"name": name, "status": "ACTIVE"}}

    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.RunnerBridge", RecordingBridge)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "orphan-pause")
    _write_resume_failed_proposal(store, job.id, "prop_orphan")

    result = recover_stalled_applications(store=store)

    resumed = [name for kind, name in calls if kind == "resume"]
    assert sorted(resumed) == ["orphan-pause-agent", "orphan-pause-script"]
    events = [e for e in result["recovered"] if e["action"] == "resume_orphaned_pause"]
    assert len(events) == 1 and events[0]["outcome"] == "resumed"
    reloaded = store.load_proposal(job.id, "prop_orphan")
    assert reloaded["application"]["resume_recovered"] is True

    # Terminal: second pass never re-resumes (owner pauses stay owned).
    calls.clear()
    second = recover_stalled_applications(store=store)
    assert [name for kind, name in calls if kind == "resume"] == []
    assert all(e["action"] != "resume_orphaned_pause" for e in second["recovered"])


def test_watchdog_ignores_healthy_resumes_and_in_flight_applies(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _patch_runner(monkeypatch)

    class RecordingBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True}

    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.RunnerBridge", RecordingBridge)
    store = JobStore(repo_root=tmp_path)
    # Healthy completion: resumes recorded ok — nothing to do.
    job_a = _make_job(store, "orphan-healthy")
    _write_resume_failed_proposal(store, job_a.id, "prop_ok", ok=True)
    # Failed resume BUT another apply is queued — the live apply owns pausing.
    job_b = _make_job(store, "orphan-inflight")
    _write_resume_failed_proposal(store, job_b.id, "prop_bad")
    _write_proposal(
        store,
        job_b.id,
        "prop_live",
        application={"status": "queued"},
        candidate_report={"gate": "green"},
    )

    result = recover_stalled_applications(store=store)

    assert [name for kind, name in calls if kind == "resume"] == []
    assert all(e["action"] != "resume_orphaned_pause" for e in result["recovered"])


def _gate(live_ready: bool, reasons: list[str]) -> dict:
    return {"live_ready": live_ready, "reasons": reasons}


_MISMATCH = "backtest is for revision aaaa11112222, workspace is bbbb33334444"


def test_watchdog_restamps_revision_mismatch_gate(tmp_path: Path, monkeypatch) -> None:
    """Gate red PURELY from revision mismatch → the watchdog re-runs the
    stamp chain; substantive reds are never touched."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "gate-stale")

    gates = iter([_gate(False, [_MISMATCH]), _gate(True, [])])
    chain: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.evaluate_live_gate",
        lambda job_id, store=None, **k: next(gates),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.compute_workspace_revision",
        lambda root: "bbbb33334444",
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job",
        lambda job_id, store=None, **k: chain.append("backtest"),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.run_preflight",
        lambda job_id, store=None, **k: chain.append("preflight"),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.validation.validate_execution_job",
        lambda job_id, store=None, **k: chain.append("validate"),
    )

    result = recover_stalled_applications(store=store)

    assert chain == ["backtest", "preflight", "validate"]
    events = [e for e in result["recovered"] if e["action"] == "gate_restamp"]
    assert len(events) == 1 and events[0]["outcome"] == "green"
    marker = store.read_json(job.id, "state/gate_restamp.json")
    assert marker["revision"] == "bbbb33334444"


def test_watchdog_never_touches_substantive_red_gate(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    _make_job(store, "gate-real-red")
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.evaluate_live_gate",
        lambda job_id, store=None, **k: _gate(
            False, [_MISMATCH, "candidate validation is not passed: failed"]
        ),
    )
    chain: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job",
        lambda job_id, store=None, **k: chain.append("backtest"),
    )

    result = recover_stalled_applications(store=store)

    assert chain == []
    assert all(e.get("stalled_status") != "stale_gate" for e in result["recovered"])


def test_watchdog_gate_restamp_escalates_instead_of_looping(
    tmp_path: Path, monkeypatch
) -> None:
    """Red at a revision we already re-stamped → one escalation event, then
    stand down — never a backtest every 5 minutes."""
    _patch_runner(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "gate-loop")
    store.write_json(job.id, "state/gate_restamp.json", {"revision": "bbbb33334444"})
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.evaluate_live_gate",
        lambda job_id, store=None, **k: _gate(False, [_MISMATCH]),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.compute_workspace_revision",
        lambda root: "bbbb33334444",
    )
    chain: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job",
        lambda job_id, store=None, **k: chain.append("backtest"),
    )

    first = recover_stalled_applications(store=store)
    second = recover_stalled_applications(store=store)

    assert chain == []
    escalations = [
        e
        for r in (first, second)
        for e in r["recovered"]
        if e["action"] == "gate_restamp_not_converging"
    ]
    assert len(escalations) == 1, "escalates once, then stands down"


def test_watchdog_mechanically_finalizes_then_expires_campaign(
    tmp_path: Path, monkeypatch
) -> None:
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "campaign-watchdog")
    deadline = datetime(2026, 8, 25, 12, tzinfo=UTC)
    store.write_json(
        job.id,
        "state/evolution_campaign.json",
        {
            "campaign_id": "campaign-1",
            "status": "active",
            "stage": "generate",
            "deadline_at": deadline.isoformat(),
        },
    )
    launches: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.background.op_status_summary",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.background.spawn_detached_op",
        lambda store, job_id, op, payload: (
            launches.append((op, payload)) or {"pid": 123}
        ),
    )

    first = _run_evolution_campaign_pass(store, job.id, deadline)
    assert first == {
        "action": "evolution_campaign_finalize_started",
        "campaign_id": "campaign-1",
        "attempt": 1,
        "pid": 123,
    }
    assert launches == [("evolution_finalize", {"job_id": job.id})]
    state = store.read_json(job.id, "state/evolution_campaign.json")
    assert state["status"] == "finalizing"

    assert (
        _run_evolution_campaign_pass(store, job.id, deadline + timedelta(minutes=4))
        is None
    )
    retried = _run_evolution_campaign_pass(
        store, job.id, deadline + timedelta(minutes=5)
    )
    assert retried["attempt"] == 2

    expired = _run_evolution_campaign_pass(
        store, job.id, deadline + timedelta(minutes=61)
    )
    assert expired["action"] == "evolution_campaign_expired"
    assert store.read_json(job.id, "state/evolution_campaign.json")["status"] == (
        "expired"
    )


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line).get("type"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _finalizing_campaign(
    store: JobStore, job_id: str, deadline: datetime, *, pid: int
) -> Path:
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {
            "campaign_id": "campaign-1",
            "status": "finalizing",
            "stage": "finalizing",
            "deadline_at": deadline.isoformat(),
            "finalize_attempts": 1,
            "finalize_last_attempt_at": deadline.isoformat(),
        },
    )
    ops = store.job_dir(job_id) / "state" / "background_ops"
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "evolution_finalize.json").write_text(
        json.dumps(
            {
                "op": "evolution_finalize",
                "job_id": job_id,
                "state": "running",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    log = ops / "evolution_finalize.log"
    log.write_text("[backtest] bar 100/27648 · 12 bars/s\n", encoding="utf-8")
    return log


def test_watchdog_extends_grace_while_finalize_is_healthy(tmp_path: Path) -> None:
    """A live finalize with a fresh log past the grace is EXTENDED, not
    expired — expiry once fired mid-Optuna and the orphan ran 8+ hours
    holding the replication lock. The still-running journal is deduped to
    once per campaign."""
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "campaign-healthy")
    deadline = datetime(2026, 8, 25, 12, tzinfo=UTC)
    log = _finalizing_campaign(store, job.id, deadline, pid=os.getpid())
    now = deadline + timedelta(minutes=61)
    os.utime(log, (now.timestamp() - 60, now.timestamp() - 60))

    first = _run_evolution_campaign_pass(store, job.id, now)

    assert first == {
        "action": "evolution_finalize_still_running",
        "campaign_id": "campaign-1",
        "pid": os.getpid(),
    }
    state = store.read_json(job.id, "state/evolution_campaign.json")
    assert state["status"] == "finalizing"  # extended, not expired

    later = now + timedelta(minutes=5)
    os.utime(log, (later.timestamp() - 60, later.timestamp() - 60))
    assert _run_evolution_campaign_pass(store, job.id, later) is None
    assert _journal_types(store, job.id).count("evolution_finalize_still_running") == 1


def test_watchdog_reaps_stale_live_finalize_on_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live pid whose log went stale is wedged: the watchdog SIGKILLs the
    op's process group (pgid == pid via start_new_session) BEFORE expiring,
    and journals the reap with the pid."""
    import signal

    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "campaign-wedged")
    deadline = datetime(2026, 8, 25, 12, tzinfo=UTC)
    log = _finalizing_campaign(store, job.id, deadline, pid=os.getpid())
    now = deadline + timedelta(minutes=61)
    stale = now.timestamp() - 20 * 60
    os.utime(log, (stale, stale))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.watchdog.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    expired = _run_evolution_campaign_pass(store, job.id, now)

    assert expired["action"] == "evolution_campaign_expired"
    assert killed == [(os.getpid(), 0), (os.getpid(), signal.SIGKILL)]
    assert store.read_json(job.id, "state/evolution_campaign.json")["status"] == (
        "expired"
    )
    types = _journal_types(store, job.id)
    assert "evolution_finalize_reaped" in types
    assert "evolution_campaign_expired" in types


def test_watchdog_expires_dead_finalize_without_reap(tmp_path: Path) -> None:
    """A dead finalize pid past the grace expires exactly as before — no
    process group to reap, no reap journal."""
    import subprocess
    import sys

    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "campaign-dead")
    deadline = datetime(2026, 8, 25, 12, tzinfo=UTC)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _finalizing_campaign(store, job.id, deadline, pid=proc.pid)

    expired = _run_evolution_campaign_pass(
        store, job.id, deadline + timedelta(minutes=61)
    )

    assert expired["action"] == "evolution_campaign_expired"
    assert store.read_json(job.id, "state/evolution_campaign.json")["status"] == (
        "expired"
    )
    assert "evolution_finalize_reaped" not in _journal_types(store, job.id)

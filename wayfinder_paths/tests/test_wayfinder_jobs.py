from __future__ import annotations

import json
from pathlib import Path

import yaml

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    validate_application_candidate,
)
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.worker import (
    DYNAMIC_CONTEXT_MARKER,
    STABLE_PREFIX_END_MARKER,
    _build_worker_prompt_sections,
    _ideation_bookkeeping,
    run_job_worker,
)


def _intent_contract() -> dict:
    return {
        "intent": "Add an explicit rearm guard without allowing one-sided entries.",
        "rules_changed": ["Blocked SNX re-arm states must surface rearm_guard."],
        "rules_unchanged": [
            "Entries still require both SNX and IMX above SMA50.",
            "In-progress candles are ignored.",
        ],
        "risk_constraints": ["Do not place live orders.", "Do not duplicate stops."],
        "entry_conditions": ["SNX close > SMA50 and IMX close > SMA50."],
        "exit_conditions": ["No exit change."],
        "known_non_goals": ["Do not loosen both-leg confirmation."],
    }


def _scenario_plan() -> dict:
    return {
        "decision_function": "decide_from_snapshot",
        "scenarios": [
            {
                "name": "entry_allowed",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.224,
                        "snx_sma50": 0.220,
                        "imx_close": 0.136,
                        "imx_sma50": 0.134,
                        "bar_complete": True,
                    }
                },
                "expect": {"action": "paper_enter", "reason_contains": "both legs"},
            },
            {
                "name": "blocked_rearm",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.217,
                        "snx_sma50": 0.220,
                        "imx_close": 0.1335,
                        "imx_sma50": 0.134,
                        "bar_complete": True,
                    }
                },
                "expect": {"action": "wait", "reason_contains": "rearm_guard"},
            },
            {
                "name": "skip_in_progress",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.230,
                        "snx_sma50": 0.220,
                        "imx_close": 0.140,
                        "imx_sma50": 0.134,
                        "bar_complete": False,
                    }
                },
                "expect": {"action": "wait", "reason_contains": "in-progress"},
            },
        ],
    }


def _write_decision_script(path: Path, *, rearm_reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""from __future__ import annotations

from wayfinder_paths.jobs.forward import get_forward_recorder


def decide_from_snapshot(snapshot: dict, state: dict | None = None) -> dict:
    latest = snapshot["latest"]
    if latest.get("bar_complete") is False:
        return {{"action": "wait", "reason": "in-progress candle ignored"}}
    if latest["snx_close"] > latest["snx_sma50"] and latest["imx_close"] > latest["imx_sma50"]:
        return {{"action": "paper_enter", "reason": "both legs cleared SMA50"}}
    return {{"action": "wait", "reason": {rearm_reason!r}}}


def main() -> None:
    result = decide_from_snapshot(
        {{
            "latest": {{
                "snx_close": 0.217,
                "snx_sma50": 0.220,
                "imx_close": 0.1335,
                "imx_sma50": 0.134,
                "bar_complete": True,
            }}
        }},
        {{}},
    )
    try:
        get_forward_recorder().record_run(
            decision=result["action"],
            reason=result["reason"],
            state={{"latest": "fixture"}},
        )
    except RuntimeError:
        pass


if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )


def _prepare_candidate_script(
    store: JobStore, job_id: str, proposal_id: str, *, rearm_reason: str
) -> Path:
    proposal = store.load_proposal(job_id, proposal_id)
    candidate_dir = store.repo_root / proposal["application"]["candidate_dir"]
    script_path = candidate_dir / "workspace" / "src" / "fast_loop.py"
    _write_decision_script(script_path, rearm_reason=rearm_reason)
    job_yaml_path = candidate_dir / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8"))
    job_yaml["script_loop"]["entrypoint"] = (
        f".wayfinder/jobs/{job_id}/workspace/src/fast_loop.py"
    )
    job_yaml_path.write_text(
        yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
    )
    return script_path


def test_job_store_creates_versioned_bundle(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "SNX IMX Re-arm",
        name="SNX / IMX Re-arm",
        goal="Trade only after both legs re-arm.",
        script=".wayfinder/jobs/snx-imx-re-arm/workspace/src/fast_loop.py",
        interval_seconds=300,
        agent_mode="monitor",
        agent_wake_seconds=3600,
    )

    path = store.save(job)
    loaded = store.load("snx-imx-re-arm")

    assert path == tmp_path / ".wayfinder/jobs/snx-imx-re-arm/job.yaml"
    assert loaded.id == "snx-imx-re-arm"
    assert loaded.script_loop.enabled is True
    assert loaded.agent_loop.mode == "monitor"
    assert (tmp_path / ".wayfinder/jobs/snx-imx-re-arm/memory.md").exists()
    assert (tmp_path / ".wayfinder/jobs/snx-imx-re-arm/scorecard.json").exists()


def test_job_compiler_writes_runner_wrappers(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def ensure_started(self):
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"name": kwargs["name"]}}

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    script = tmp_path / ".wayfinder/jobs/example/workspace/src/fast_loop.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    job = WayfinderJob.new(
        "example",
        script=str(script),
        interval_seconds=60,
        agent_mode="monitor",
        agent_wake_seconds=300,
    )
    store.save(job)

    result = JobCompiler(store=store).compile(job, start_daemon=False)

    assert len(calls) == 2
    assert calls[0]["name"] == "example-script"
    assert calls[0]["script_path"] == ".wayfinder_runs/jobs/example_script.py"
    assert calls[1]["name"] == "example-agent"
    assert calls[1]["script_path"] == ".wayfinder_runs/jobs/example_agent.py"
    assert (tmp_path / ".wayfinder_runs/jobs/example_script.py").exists()
    assert (tmp_path / ".wayfinder_runs/jobs/example_agent.py").exists()
    links = json.loads(
        (tmp_path / ".wayfinder/jobs/example/runner_links.json").read_text(
            encoding="utf-8"
        )
    )
    assert links == result


def test_job_compiler_resolves_workspace_entrypoint_to_job_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def ensure_started(self):
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):
            return {"ok": True, "result": {"name": kwargs["name"]}}

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "workspace-script",
        script="workspace/src/loop.py",
        interval_seconds=60,
    )
    root = store.init_layout(job)
    script = root / "workspace" / "src" / "loop.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    store.save(job)

    JobCompiler(store=store).compile(job, start_daemon=False)

    wrapper = tmp_path / ".wayfinder_runs/jobs/workspace_script_script.py"
    assert str(script) in wrapper.read_text(encoding="utf-8")


def test_legacy_agent_modes_normalize() -> None:
    improve_job = WayfinderJob.from_dict(
        {
            "id": "legacy-improve",
            "name": "Legacy Improve",
            "script_loop": {"enabled": True},
            "agent_loop": {"enabled": True, "mode": "improve"},
        }
    )
    decide_job = WayfinderJob.from_dict(
        {
            "id": "legacy-decide",
            "name": "Legacy Decide",
            "script_loop": {"enabled": False},
            "agent_loop": {"enabled": True, "mode": "decide"},
        }
    )

    assert improve_job.agent_loop.mode == "intervene"
    assert improve_job.job_kind == "script_agent"
    assert decide_job.agent_loop.mode == "auto"
    assert decide_job.job_kind == "agent_only"


def test_auto_agent_job_can_run_without_script() -> None:
    job = WayfinderJob.new(
        "auto-demo",
        agent_mode="auto",
        auto_limits={
            "enabled_venues": ["hyperliquid"],
            "allowed_symbols": ["BTC"],
            "max_notional_per_decision": 25,
            "max_daily_notional": 100,
            "max_open_positions": 1,
            "max_open_orders": 2,
        },
    )

    assert job.job_kind == "agent_only"
    assert job.script_loop.enabled is False
    assert job.agent_loop.enabled is True
    assert job.agent_loop.mode == "auto"
    assert job.agent_loop.wake_interval_seconds == 900
    assert job.agent_loop.auto_limits["allowed_symbols"] == ["BTC"]


def test_auto_worker_blocks_missing_limits(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("auto-demo", agent_mode="auto")
    store.save(job)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)

    report = run_job_worker("auto-demo", mode="auto")

    assert report["status"] == "red"
    assert report["queued"] is False
    assert "enabled_venues" in str(report["error"])
    latest = json.loads(
        (tmp_path / ".wayfinder/jobs/auto-demo/reports/auto/latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["summary"].startswith("Auto agent blocked")


def _worker_snapshot(job: WayfinderJob, **overrides: object) -> dict:
    """Minimal snapshot with the full snapshot_job shape."""
    snapshot: dict = {
        "job": job.to_dict(),
        "scorecard": {},
        "forward": {},
        "runner_links": {},
        "proposals": [],
        "proposal_queue": {},
        "reports": {},
        "backtest": {},
    }
    snapshot.update(overrides)
    return snapshot


def test_worker_prompt_keeps_dynamic_context_after_stable_prefix(
    tmp_path: Path,
) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "cache-demo",
        goal="Keep the long-lived contract stable.",
        script="workspace/src/loop.py",
        agent_mode="monitor",
    )
    store.save(job)
    first = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )

    store.append_journal(job.id, {"type": "script_run", "summary": "new run"})
    store.write_json(
        job.id,
        "reports/monitor/latest.json",
        {"created_at": "dynamic", "summary": "changed"},
    )
    second = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot=_worker_snapshot(
            job,
            scorecard={"health": "yellow"},
            reports={"monitor": {"summary": "changed"}},
        ),
    )

    assert first["stable_prefix"] == second["stable_prefix"]
    assert first["stable_prefix_hash"] == second["stable_prefix_hash"]
    assert first["dynamic_context_hash"] != second["dynamic_context_hash"]
    assert first["prompt"].index(STABLE_PREFIX_END_MARKER) < first["prompt"].index(
        DYNAMIC_CONTEXT_MARKER
    )
    assert "new run" not in first["stable_prefix"]
    assert "new run" in second["dynamic_context"]


def test_worker_prompt_stable_hash_changes_when_memory_changes(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("cache-memory", agent_mode="monitor")
    store.save(job)
    snapshot = _worker_snapshot(job)
    first = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot=snapshot,
    )

    (store.job_dir(job.id) / "memory.md").write_text(
        "# Cache Memory\n\nKnown lessons:\n- New durable lesson.\n",
        encoding="utf-8",
    )
    second = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot=snapshot,
    )

    assert first["stable_prefix_hash"] != second["stable_prefix_hash"]
    assert "New durable lesson" in second["stable_prefix"]


def test_worker_prompt_includes_apply_lifecycle(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("apply-prompt", agent_mode="intervene")
    store.save(job)
    prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
        apply_proposal_id="prop_001",
    )["prompt"]

    assert "Apply approved proposal `prop_001`" in prompt
    assert "if it is already applying, do not claim again" in prompt
    assert "claim it yourself" in prompt
    assert "watchdog clock" in prompt
    assert 'core_jobs(action="claim_application"' in prompt
    assert 'core_jobs(action="validate_application"' in prompt
    assert 'core_jobs(action="complete_application"' in prompt
    assert "runner loops pause only after the apply worker claims" in prompt


def test_worker_report_includes_cache_metadata(tmp_path: Path, monkeypatch) -> None:
    class FakeOpenCodeClient:
        def healthy(self) -> bool:
            return True

        def find_child_session(self, *, parent_id, title):  # noqa: ANN001
            return None

        def create_session(self, *, parent_id=None, title=None, agent=None):  # noqa: ANN001
            return "session-cache-demo-monitor"

        def prompt_async(self, session_id: str, text: str, *, agent=None) -> bool:  # noqa: ANN001
            assert session_id == "session-cache-demo-monitor"
            assert text.index(STABLE_PREFIX_END_MARKER) < text.index(
                DYNAMIC_CONTEXT_MARKER
            )
            return True

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("cache-report", agent_mode="monitor")
    store.save(job)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.OPENCODE_CLIENT", FakeOpenCodeClient()
    )

    report = run_job_worker(job.id, mode="monitor")

    assert report["status"] == "green"
    assert report["cache"]["prompt_cache_key"] == "session-cache-demo-monitor"
    assert len(report["cache"]["stable_prefix_hash"]) == 64
    assert len(report["cache"]["dynamic_context_hash"]) == 64
    scorecard = store.read_json(job.id, "scorecard.json", default={})
    assert scorecard["last_agent_cache"] == report["cache"]


def test_proposal_approval_queues_without_pausing(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "proposal-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    proposal_path = store.job_dir(job.id) / "proposals" / "prop_001.json"
    proposal_path.write_text(
        json.dumps(
            {
                "proposal_id": "prop_001",
                "job_id": job.id,
                "status": "pending",
                "proposed_change": {"summary": "Tighten the entry guard."},
                "intent_contract": _intent_contract(),
                "scenario_plan": _scenario_plan(),
                "approval": {"required": True, "status": "pending"},
            }
        ),
        encoding="utf-8",
    )

    proposal = store.approve_proposal(job.id, "prop_001")

    assert proposal["status"] == "approved"
    assert proposal["application"]["status"] == "queued"
    assert store.proposal_queue(job.id)["queued"][0]["proposal_id"] == "prop_001"
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "proposal_apply_queued" in journal


def test_claim_application_pauses_loops_then_complete_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def pause(self, name: str) -> dict:
            calls.append(("pause", name))
            return {"ok": True, "paused": name}

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True, "resumed": name}

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job):  # noqa: ANN001
            calls.append(("compile", job.id))
            return {"job_id": job.id, "jobs": []}

    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", FakeBridge)
    monkeypatch.setattr("wayfinder_paths.jobs.application.JobCompiler", FakeCompiler)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "apply-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    store.write_proposal(
        job.id,
        {
            "proposal_id": "prop_apply",
            "job_id": job.id,
            "status": "approved",
            "application": {"status": "queued"},
            "proposed_change": {"summary": "Move script into job workspace."},
            "intent_contract": _intent_contract(),
            "scenario_plan": _scenario_plan(),
        },
    )

    claimed = claim_application(store, job.id, "prop_apply")

    assert claimed["proposal"]["application"]["status"] == "applying"
    assert ("pause", "apply-demo-script") in calls
    assert ("pause", "apply-demo-agent") in calls
    _prepare_candidate_script(
        store,
        job.id,
        "prop_apply",
        rearm_reason="rearm_guard: SNX still below SMA50.",
    )
    candidate_validation = validate_application_candidate(
        store, job.id, "prop_apply", allow_legacy=True
    )
    assert candidate_validation["status"] == "passed"

    completed = complete_application(
        store,
        job.id,
        "prop_apply",
        status="applied",
        changed_files=[".wayfinder/jobs/apply-demo/workspace/src/fast_loop.py"],
        validation={"syntax": "ok"},
        allow_legacy=True,
    )

    assert completed["proposal"]["application"]["status"] == "applied"
    assert completed["deterministic_validation"]["status"] == "passed"
    assert completed["promoted_revision"]
    assert ("compile", "apply-demo") in calls
    assert ("resume", "apply-demo-script") in calls
    assert ("resume", "apply-demo-agent") in calls


def test_complete_application_fails_runnable_strategy_that_violates_intent(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def pause(self, name: str) -> dict:
            calls.append(("pause", name))
            return {"ok": True, "paused": name}

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True, "resumed": name}

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job):  # noqa: ANN001
            calls.append(("compile", job.id))
            return {"job_id": job.id, "jobs": []}

    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", FakeBridge)
    monkeypatch.setattr("wayfinder_paths.jobs.application.JobCompiler", FakeCompiler)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "apply-fail-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    original_job_yaml = (store.job_dir(job.id) / "job.yaml").read_text(encoding="utf-8")
    store.write_proposal(
        job.id,
        {
            "proposal_id": "prop_bad",
            "job_id": job.id,
            "status": "approved",
            "application": {"status": "queued"},
            "proposed_change": {"summary": "Add the rearm guard."},
            "intent_contract": _intent_contract(),
            "scenario_plan": _scenario_plan(),
        },
    )

    claim_application(store, job.id, "prop_bad")
    _prepare_candidate_script(
        store,
        job.id,
        "prop_bad",
        rearm_reason="SNX still below SMA50.",
    )

    completed = complete_application(
        store,
        job.id,
        "prop_bad",
        status="applied",
        changed_files=["workspace/src/fast_loop.py"],
        validation={"syntax": "ok"},
    )

    assert completed["proposal"]["application"]["status"] == "failed"
    assert completed["deterministic_validation"]["status"] == "failed"
    assert completed["promoted_revision"] is None
    assert ("compile", "apply-fail-demo") not in calls
    assert ("resume", "apply-fail-demo-script") in calls
    assert ("resume", "apply-fail-demo-agent") in calls
    assert (store.job_dir(job.id) / "job.yaml").read_text(
        encoding="utf-8"
    ) == original_job_yaml


def test_complete_application_validation_exception_marks_failed_and_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def pause(self, name: str) -> dict:
            calls.append(("pause", name))
            return {"ok": True, "paused": name}

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True, "resumed": name}

    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "apply-error-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
    )
    store.save(job)
    store.write_proposal(
        job.id,
        {
            "proposal_id": "prop_error",
            "job_id": job.id,
            "status": "approved",
            "application": {"status": "queued"},
            "proposed_change": {"summary": "Add the rearm guard."},
            "intent_contract": _intent_contract(),
            "scenario_plan": _scenario_plan(),
        },
    )
    claim_application(store, job.id, "prop_error")

    def raise_validation(**kwargs):  # noqa: ANN003
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(
        "wayfinder_paths.jobs.application.validate_candidate_application",
        raise_validation,
    )

    completed = complete_application(
        store,
        job.id,
        "prop_error",
        status="applied",
        changed_files=["workspace/src/fast_loop.py"],
    )

    assert completed["proposal"]["application"]["status"] == "failed"
    assert completed["proposal"]["application"]["error"] == "validator exploded"
    assert completed["deterministic_validation"]["status"] == "failed"
    assert ("resume", "apply-error-demo-script") in calls
    assert ("resume", "apply-error-demo-agent") in calls
    report = json.loads(
        (store.job_dir(job.id) / "reports/apply/latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "red"
    assert report["error"] == "validator exploded"


def test_runner_bridge_starts_daemon_with_defaults(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_ensure_daemon_started(**kwargs):
        captured.update(kwargs)
        return True, {"status": "ok"}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.runner_bridge.ensure_daemon_started",
        fake_ensure_daemon_started,
    )

    result = RunnerBridge(repo_root=tmp_path).ensure_started()

    assert result["ok"] is True
    assert captured["paths"].repo_root == tmp_path.resolve()
    assert captured["tick_seconds"] == 1.0
    from wayfinder_paths.runner.constants import DEFAULT_MAX_WORKERS

    assert captured["max_workers"] == DEFAULT_MAX_WORKERS
    assert captured["max_failures"] == 5
    assert captured["default_timeout_seconds"] == 20 * 60
    assert captured["log_level"] == "INFO"


def test_sync_all_jobs_noops_outside_opencode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    store = JobStore(repo_root=tmp_path)
    store.save(WayfinderJob.new("local-script", script="workspace/src/loop.py"))

    sync_all_jobs(store=store)


def test_worker_prompt_ledgers_and_backtest_are_dynamic_only(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.ledger import append_ledger_row

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("loop-context", agent_mode="intervene")
    store.save(job)
    snapshot = _worker_snapshot(
        job,
        backtest={"available": True, "stats": {"sharpe": 1.23}},
        gate={"live_ready": True, "reasons": []},
    )
    first = _build_worker_prompt_sections(
        store=store, job_id=job.id, mode="intervene", snapshot=snapshot
    )
    assert '"backtest"' in first["dynamic_context"]
    assert '"ledgers"' in first["dynamic_context"]
    assert '"backtest"' not in first["stable_prefix"]

    # Appending a ledger row is DYNAMIC history: dynamic hash moves, the
    # stable cache prefix must not.
    append_ledger_row(
        store,
        job.id,
        "candidates",
        {"name": "chop-filter-variant", "bucket": "adjacent", "status": "no_edge"},
    )
    second = _build_worker_prompt_sections(
        store=store, job_id=job.id, mode="intervene", snapshot=snapshot
    )
    assert first["stable_prefix_hash"] == second["stable_prefix_hash"]
    assert first["dynamic_context_hash"] != second["dynamic_context_hash"]
    assert "chop-filter-variant" in second["dynamic_context"]
    assert "chop-filter-variant" not in second["stable_prefix"]


def test_worker_prompt_fences_backtest_when_forward_empty(tmp_path: Path) -> None:
    """The backtest is labeled historical on EVERY wake (the agent conflates it
    with forward even on non-empty wakes); the zero-evidence marker appears only
    when forward is actually empty. Stats stay so the agent can still diagnose."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("fence-empty", agent_mode="intervene")
    store.save(job)

    empty_snapshot = {  # no forward key -> forward is empty
        "job": job.to_dict(),
        "backtest": {
            "available": True,
            "stats": {"win_rate": 1.0, "net_pnl": 588.44},
        },
    }
    dyn = _build_worker_prompt_sections(
        store=store, job_id=job.id, mode="intervene", snapshot=empty_snapshot
    )["dynamic_context"]
    assert "NO_FORWARD_DATA" in dyn
    assert "HISTORICAL_BACKTEST" in dyn
    assert '"backtest"' in dyn  # stats retained, just fenced
    assert '"win_rate"' in dyn

    live_snapshot = {  # populated forward: backtest still labeled, no empty marker
        "job": job.to_dict(),
        "backtest": {"available": True, "stats": {"win_rate": 1.0}},
        "forward": {
            "summary": {"runs": {"count": 3}, "trades": {"closed_count": 2}},
            "recent_trades": [{"pnl": 1.0}],
        },
    }
    dyn_live = _build_worker_prompt_sections(
        store=store, job_id=job.id, mode="intervene", snapshot=live_snapshot
    )["dynamic_context"]
    assert "NO_FORWARD_DATA" not in dyn_live
    assert "HISTORICAL_BACKTEST" in dyn_live  # always-on: backtest is historical


def test_forward_detail_capped_so_ledgers_survive_prompt(tmp_path: Path) -> None:
    """Regression: bulky forward telemetry must not truncate the ledgers/
    proposals out of the 12k dynamic prompt (keys serialize alphabetically,
    so an un-capped `forward` starves the later high-signal keys)."""
    from wayfinder_paths.jobs.ledger import append_ledger_row

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("forward-heavy", agent_mode="intervene")
    store.save(job)
    append_ledger_row(
        store,
        job.id,
        "candidates",
        {"name": "seeded-trap-family", "bucket": "adjacent", "status": "no_edge"},
    )
    bulky_trades = [
        {
            "trade": i,
            "pnl": -0.1 * i,
            "reason": "verbose reconciliation note " * 8,
            "symbol": "EVAL",
        }
        for i in range(25)
    ]
    snapshot = _worker_snapshot(
        job,
        forward={
            "summary": {"win_rate": 0.3, "current_loss_streak": 4},
            "recent_trades": bulky_trades,
            "recent_runs": bulky_trades,
        },
    )
    sections = _build_worker_prompt_sections(
        store=store, job_id=job.id, mode="intervene", snapshot=snapshot
    )
    dyn = sections["dynamic_context"]
    assert '"ledgers"' in dyn
    assert "seeded-trap-family" in dyn
    assert '"win_rate"' in dyn  # summary survives
    # Detail rows are capped, not all 25 present.
    assert dyn.count('"reason"') <= 12


class _FakeBridge:
    """RunnerBridge stand-in: records calls, never touches a daemon."""

    def __init__(self, *, repo_root=None):  # noqa: ANN001
        self.repo_root = repo_root

    def ensure_started(self):
        return {"ok": True}

    def add_or_update_script_job(self, **kwargs):
        return {"ok": True, "result": {"name": kwargs["name"]}}


def test_legacy_compile_fails_loudly_on_missing_entrypoint(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    """A legacy (runpy) wrapper against a nonexistent script must fail at
    COMPILE time, not at 3am when the runner fires it — this exact wrapper
    shipped broken in a live session (pointed at /wf/sdk/strategy.py)."""
    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ghost-script",
        script="strategy.py",
        interval_seconds=60,
        execution_contract="legacy",
    )
    store.save(job)
    with pytest.raises(ValueError, match="jobs_v1"):
        JobCompiler(store=store).compile(job, start_daemon=False)


def test_jobs_v1_wrapper_uses_scheduled_tick_driver(
    tmp_path: Path, monkeypatch
) -> None:
    """jobs_v1 wrappers call run_scheduled_tick(JOB_DIR) — no entrypoint file
    needs to exist because the driver resolves the strategy at tick time."""
    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "tick-driven",
        script="strategy.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    JobCompiler(store=store).compile(job, start_daemon=False)
    wrapper = tmp_path / ".wayfinder_runs/jobs/tick_driven_script.py"
    text = wrapper.read_text(encoding="utf-8")
    assert "run_scheduled_tick" in text
    assert "runpy" not in text


def test_mcp_create_defaults_to_jobs_v1(tmp_path: Path, monkeypatch) -> None:
    """core_jobs(action='create') births jobs_v1 by default — the legacy
    default is what compiled broken runpy wrappers for every agent-created
    strategy job."""
    import asyncio

    from wayfinder_paths.mcp.tools import jobs as jobs_tools

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    monkeypatch.setattr(jobs_tools, "JobStore", lambda: JobStore(repo_root=tmp_path))
    monkeypatch.setattr(jobs_tools, "sync_all_jobs", lambda store=None: None)

    result = asyncio.run(
        jobs_tools.core_jobs(
            action="create",
            job_id="fresh-strategy",
            script="strategy.py",
            interval_seconds=3600,
        )
    )
    assert result["ok"], result
    store = JobStore(repo_root=tmp_path)
    job = store.load("fresh-strategy")
    assert job.execution_contract == "jobs_v1"
    wrapper = tmp_path / ".wayfinder_runs/jobs/fresh_strategy_script.py"
    assert "run_scheduled_tick" in wrapper.read_text(encoding="utf-8")
    # Create tells the agent exactly where the strategy module lives —
    # layout guessing cost real tool calls in live sessions.
    # The scaffold pins the module inside the versioned workspace.
    assert result["result"]["script_entrypoint"].endswith("workspace/src/strategy.py")
    assert "workspace/src/" in result["result"]["hint"]


def test_mcp_sync_heals_stale_wrapper_after_contract_flip(
    tmp_path: Path, monkeypatch
) -> None:
    """Agents hand-edit job.yaml (legacy -> jobs_v1); sync must recompile the
    wrapper instead of leaving the stale runpy one to fail on schedule."""
    import asyncio

    from wayfinder_paths.mcp.tools import jobs as jobs_tools

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    monkeypatch.setattr(jobs_tools, "JobStore", lambda: JobStore(repo_root=tmp_path))
    monkeypatch.setattr(jobs_tools, "sync_all_jobs", lambda store=None: None)

    store = JobStore(repo_root=tmp_path)
    # Born legacy with a real script so create-time compile succeeds.
    root = tmp_path / ".wayfinder/jobs/flip-me"
    script = root / "workspace" / "loop.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    job = WayfinderJob.new(
        "flip-me",
        script="workspace/loop.py",
        interval_seconds=60,
        execution_contract="legacy",
    )
    store.save(job)
    JobCompiler(store=store).compile(job, start_daemon=False)
    wrapper = tmp_path / ".wayfinder_runs/jobs/flip_me_script.py"
    assert "runpy" in wrapper.read_text(encoding="utf-8")

    # The hand edit agents actually perform:
    job.execution_contract = "jobs_v1"
    store.save(job)

    result = asyncio.run(jobs_tools.core_jobs(action="sync"))
    assert result["ok"], result
    assert "flip-me" in result["result"]["recompiled"]
    text = wrapper.read_text(encoding="utf-8")
    assert "run_scheduled_tick" in text
    assert "runpy" not in text


def test_bridge_requires_env() -> None:
    """update_job replaces the runner payload wholesale — a schedule-only
    update without env silently reverted WAYFINDER_JOB_MODE to paper on a
    LIVE job in production. env is now mandatory."""
    import pytest

    from wayfinder_paths.jobs.runner_bridge import RunnerBridge

    bridge = RunnerBridge.__new__(RunnerBridge)  # skip daemon paths
    with pytest.raises(ValueError, match="JobCompiler.compile"):
        bridge.add_or_update_script_job(
            name="x-script", script_path="x.py", interval_seconds=60, env={}
        )


def test_bridge_update_path_carries_env(monkeypatch) -> None:
    from wayfinder_paths.jobs import runner_bridge as rb

    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def call(self, method, params):
            calls.append((method, params))
            if method == "add_job":
                return {"ok": False, "error": "UNIQUE constraint failed: jobs.name"}
            return {"ok": True}

    bridge = rb.RunnerBridge.__new__(rb.RunnerBridge)
    bridge.client = FakeClient()
    resp = bridge.add_or_update_script_job(
        name="x-script",
        script_path="x.py",
        interval_seconds=60,
        env={"WAYFINDER_JOB_MODE": "live"},
    )
    assert resp["ok"]
    update_calls = [p for m, p in calls if m == "update_job"]
    assert update_calls and update_calls[0]["payload"]["env"] == {
        "WAYFINDER_JOB_MODE": "live"
    }


def test_worker_prompt_requires_withdrawing_superseded_drafts(
    tmp_path: Path,
) -> None:
    """A live wake left v1/v2 drafts of the same fix pending — the owner
    reviewed stale drafts. The wake rules must direct the worker to reject a
    superseded draft before proposing its replacement."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("draft-hygiene", agent_mode="intervene")
    store.save(job)
    prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]
    assert "ONE open proposal per concern" in prompt
    assert "reject_proposal" in prompt
    assert "superseded by <new-id>" in prompt


def test_create_job_copies_external_script_into_workspace_src(
    tmp_path: Path,
) -> None:
    """Strategy code outside workspace/ is invisible to revisions and
    proposals — create must move it to the one versionable home."""
    from wayfinder_paths.jobs.gating import compute_workspace_revision

    external = tmp_path / "elsewhere" / "momo.py"
    external.parent.mkdir(parents=True)
    external.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("scaffold-copy", script=str(external), interval_seconds=3600)
    store.create_job(job)

    assert job.script_loop.entrypoint == "workspace/src/momo.py"
    copied = store.job_dir(job.id) / "workspace" / "src" / "momo.py"
    assert copied.read_text(encoding="utf-8").startswith("def decide")
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "entrypoint_scaffolded" in journal
    # The copied file is now inside the revision hash.
    before = compute_workspace_revision(store.job_dir(job.id))
    copied.write_text("def decide(ctx):\n    return list()\n", encoding="utf-8")
    assert compute_workspace_revision(store.job_dir(job.id)) != before


def test_create_job_defaults_missing_script_to_workspace_src(
    tmp_path: Path,
) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "scaffold-default", script="strategy.py", interval_seconds=3600
    )
    store.create_job(job)

    assert job.script_loop.entrypoint == "workspace/src/strategy.py"
    # No stub: execution_script_exists must stay honest until the agent
    # writes the real module.
    assert not (store.job_dir(job.id) / "workspace" / "src" / "strategy.py").exists()


def test_create_job_keeps_workspace_relative_entrypoint(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "scaffold-noop", script="workspace/src/loop.py", interval_seconds=3600
    )
    store.create_job(job)

    assert job.script_loop.entrypoint == "workspace/src/loop.py"
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "entrypoint_scaffolded" not in journal


def test_compiler_journals_entrypoint_outside_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    store = JobStore(repo_root=tmp_path)
    rogue = tmp_path / "rogue.py"
    rogue.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    job = WayfinderJob.new("rogue-entrypoint", script=str(rogue), interval_seconds=3600)
    # Raw save (no scaffold) mirrors the legacy jobs already on disk.
    store.save(job)

    JobCompiler(store=store).compile(job, start_daemon=False)

    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "entrypoint_outside_workspace" in journal


def test_worker_prompt_states_workspace_staging_rule(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("staging-rule", agent_mode="intervene")
    store.save(job)
    prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]

    assert "Proposals stage ONLY `workspace/` + `job.yaml`" in prompt
    assert "FIRST proposal must migrate it into" in prompt


def test_worker_prompt_intervene_ladder_and_retry_budget(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("ladder", agent_mode="intervene")
    store.save(job)
    prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]

    assert "Wake priority ladder" in prompt
    assert "healthy, no change warranted" in prompt
    assert "after 2 failed propose attempts in a wake" in prompt
    # The exploration lane: sub-floor forward samples gate exploitation, not
    # research-side analysis.
    assert "Exploration vs exploitation" in prompt
    assert "gates EXPLOITATION only" in prompt
    # The ideation cadence rung + the agenda bootstrap marker (no agenda file
    # exists in this fixture).
    assert "Ideation cadence" in prompt
    assert "bootstrap it on the next healthy ideation wake" in prompt

    # A seeded agenda is embedded verbatim in the dynamic context.
    agenda_dir = store.job_dir(job.id) / "research"
    agenda_dir.mkdir(parents=True, exist_ok=True)
    (agenda_dir / "agenda.md").write_text(
        "# Research agenda\nLast ideation session: 2026-07-22T00:00:00Z\n"
    )
    seeded = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]
    assert "Last ideation session: 2026-07-22T00:00:00Z" in seeded
    # The ladder is intervene/monitor task guidance, not part of the apply wake.
    apply_prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
        apply_proposal_id="prop_x",
    )["prompt"]
    assert "Wake priority ladder" not in apply_prompt


def test_reject_proposal_records_provenance(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("reject-demo", agent_mode="intervene")
    store.save(job)
    for pid in ("prop_owner", "prop_agent"):
        store.write_proposal(
            job.id,
            {
                "proposal_id": pid,
                "job_id": job.id,
                "status": "pending",
                "proposed_change": {"summary": "x"},
                "approval": {"required": True, "status": "pending"},
            },
        )

    owner = store.reject_proposal(job.id, "prop_owner")
    assert owner["rejection"]["by"] == "owner"
    assert owner["rejection"]["reason"] is None

    agent = store.reject_proposal(
        job.id, "prop_agent", reason="superseded by v2", rejected_by="agent"
    )
    assert agent["rejection"] == {
        "reason": "superseded by v2",
        "by": "agent",
        "ts": agent["rejection"]["ts"],
    }
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text()
    assert '"rejected_by": "owner"' in journal
    assert '"rejected_by": "agent"' in journal


def test_worker_prompt_hoists_restage_tasks_out_of_snapshot(tmp_path: Path) -> None:
    """Pending re-stages must be PROMPT TEXT, not payload-only: the snapshot
    JSON truncates at 12k chars (sort_keys), which once swallowed the
    instruction and the agent burned a carried-over approval on a duplicate
    proposal."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "restage-prompt-demo",
        goal="Carry approvals over.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
    )
    store.save(job)
    proposals_dir = store.job_dir(job.id) / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / "prop-params-update-aaaa1111.json").write_text(
        json.dumps(
            {
                "proposal_id": "prop-params-update-aaaa1111",
                "status": "approved",
                "proposed_change": {
                    "summary": "Tighten stop",
                    "execution_params": {"stop_pct": 0.01},
                },
                "application": {"status": "failed", "restage_requested": True},
            }
        ),
        encoding="utf-8",
    )

    sections = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )

    dynamic = sections["dynamic_context"]
    priority_at = dynamic.index("PRIORITY — approved changes awaiting re-stage")
    assert priority_at < dynamic.index("Current snapshot:")
    assert "Do NOT create a new proposal" in dynamic
    assert "wayfinder job restage" in dynamic
    assert "prop-params-update-aaaa1111" in dynamic[: priority_at + 2000]
    assert "FIRST: complete the PRIORITY re-stage tasks" in dynamic

    # No pending re-stage → no priority section.
    (proposals_dir / "prop-params-update-aaaa1111.json").unlink()
    clean = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    assert (
        "PRIORITY — approved changes awaiting re-stage" not in clean["dynamic_context"]
    )


def test_worker_prompt_hoists_red_gate_out_of_snapshot(tmp_path: Path) -> None:
    """A red gate must be PROMPT TEXT above the snapshot: buried in the
    truncated JSON, a wake once reported "gate green" while approvals were
    blocked for 28 hours."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "gate-alert-demo",
        goal="See the truth.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
    )
    store.save(job)

    red = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(
            job,
            scorecard={"health": "green"},
            gate={
                "live_ready": False,
                "reasons": [
                    "backtest is for revision aaaa11112222, workspace is bbbb33334444"
                ],
            },
        ),
    )
    dynamic = red["dynamic_context"]
    alert_at = dynamic.index("GATE STATUS: RED")
    assert alert_at < dynamic.index("Current snapshot:")
    assert "DO NOT report the gate as green" in dynamic
    assert "revision aaaa11112222" in dynamic[: alert_at + 600]

    green = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(
            job,
            scorecard={"health": "green"},
            gate={"live_ready": True, "reasons": []},
        ),
    )
    assert "GATE STATUS: RED" not in green["dynamic_context"]


def _write_ideation_artifact(store: JobStore, job_id: str, *, age_hours: float) -> None:
    import datetime as dt

    path = store.job_dir(job_id) / "research" / "ideation" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)
    path.write_text(
        json.dumps(
            {
                "generated_at": stamp.isoformat(),
                "sources_consulted": [
                    {"tool": "research_search_alpha", "query": "sol unlocks", "takeaway": "none"},
                    {"tool": "research_crypto_sentiment", "query": "SOL", "takeaway": "neutral"},
                    {"tool": "research_social_x_search", "query": "solana catalyst", "takeaway": "fee vote"},
                ],
                "hypotheses": [
                    {"title": "A", "thesis": "t", "bucket": "testable", "next_step": "scan"},
                    {"title": "B", "thesis": "t", "bucket": "starved", "next_step": "needs events feed"},
                    {"title": "C", "thesis": "t", "bucket": "refuted", "next_step": "no edge in 2024-2026"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_worker_prompt_forces_ideation_when_artifact_stale(tmp_path: Path) -> None:
    """Ideation is a FORCED session: prose suggestions produced 130 straight
    "nothing new" wakes with zero external tool calls. When the expedition
    artifact is missing or >20h old, the wake's task IS the expedition."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ideation-demo",
        goal="Find edges.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
    )
    store.save(job)

    forced = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    dynamic = forced["dynamic_context"]
    at = dynamic.index("IDEATION SESSION — this wake is a research EXPEDITION")
    assert at < dynamic.index("Current snapshot:")
    assert "research/ideation/latest.json" in dynamic
    assert '"testable"|"starved"|"refuted"' in dynamic
    assert "This wake is an IDEATION SESSION" in dynamic
    assert "at least 3 DISTINCT external sources" in dynamic

    # Fresh artifact → back to routine wakes.
    _write_ideation_artifact(store, job.id, age_hours=2)
    routine = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" not in routine["dynamic_context"]

    # Stale again after ~a day.
    _write_ideation_artifact(store, job.id, age_hours=26)
    stale = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" in stale["dynamic_context"]
    assert "26h old" in stale["dynamic_context"]


def test_ideation_defers_to_ops_priorities(tmp_path: Path) -> None:
    """The expedition lands on the next CLEAN wake: red gates, pending
    re-stages, apply wakes, and monitor mode all suppress it."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ideation-defer-demo",
        goal="Ops first.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
    )
    store.save(job)

    red_gate = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(
            job,
            scorecard={"health": "green"},
            gate={"live_ready": False, "reasons": ["backtest is stale"]},
        ),
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" not in red_gate["dynamic_context"]

    monitor = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="monitor",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" not in monitor["dynamic_context"]

    apply_wake = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
        apply_proposal_id="prop-params-update-aaaa1111",
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" not in apply_wake["dynamic_context"]

    proposals_dir = store.job_dir(job.id) / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / "prop-params-update-bbbb2222.json").write_text(
        json.dumps(
            {
                "proposal_id": "prop-params-update-bbbb2222",
                "status": "approved",
                "proposed_change": {"summary": "s", "execution_params": {"x": 1}},
                "application": {"status": "failed", "restage_requested": True},
            }
        ),
        encoding="utf-8",
    )
    restage_wake = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job, scorecard={"health": "green"}),
    )
    assert "IDEATION SESSION — this wake is a research EXPEDITION" not in restage_wake["dynamic_context"]


def test_ideation_bookkeeping_journals_artifacts_and_overdue(tmp_path: Path) -> None:
    """New artifacts journal once with bucket counts; a >48h-overdue
    expedition escalates once per episode."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ideation-books-demo",
        goal="Honest ledger.",
        script="workspace/src/loop.py",
        agent_mode="intervene",
    )
    store.save(job)
    journal_path = store.job_dir(job.id) / "journal.jsonl"

    def journal_types() -> list[str]:
        if not journal_path.exists():
            return []
        return [
            json.loads(line)["type"]
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # No artifact ever → escalate once, not every wake.
    _ideation_bookkeeping(store, job.id)
    _ideation_bookkeeping(store, job.id)
    assert journal_types().count("ideation_incomplete") == 1

    # Fresh artifact → journaled once with bucket counts, seen-stamped.
    _write_ideation_artifact(store, job.id, age_hours=1)
    _ideation_bookkeeping(store, job.id)
    _ideation_bookkeeping(store, job.id)
    assert journal_types().count("ideation_artifact") == 1
    artifact_event = next(
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "ideation_artifact"
    )
    assert artifact_event["hypotheses"] == 3
    assert artifact_event["sources"] == 3
    assert artifact_event["buckets"] == {"testable": 1, "starved": 1, "refuted": 1}

    # A NEW expedition (different generated_at) journals again.
    _write_ideation_artifact(store, job.id, age_hours=0)
    _ideation_bookkeeping(store, job.id)
    assert journal_types().count("ideation_artifact") == 2

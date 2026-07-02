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
    assert "if it is applying, do not claim again" in prompt
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
    assert captured["max_workers"] == 4
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

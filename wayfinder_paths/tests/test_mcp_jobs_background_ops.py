"""Detached background job ops: a backtest cannot fit through the MCP
request window on the box (client timeout kills the run mid-grind, and the
memory spike OOM-killed the conversation server — observed live). The op
runs detached with a status file; op_status polls it."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

import wayfinder_paths.mcp.tools.jobs as jobs_module
from wayfinder_paths.jobs.background import op_status_summary
from wayfinder_paths.jobs.execution.op_process import (
    recorded_process_alive,
    terminate_campaign_ops,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.mcp.tools.jobs import (
    _background_op_status,
    _background_ops_dir,
    _start_background_op,
    core_jobs,
)


def _store(tmp_path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job_id = "bg-demo"
    store.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    return store, job_id


@pytest.mark.asyncio
async def test_background_op_end_to_end_via_echo(tmp_path) -> None:
    store, job_id = _store(tmp_path)
    payload = {"hello": "world"}

    started = await _start_background_op(store, job_id, "__echo__", payload)
    assert started["result"]["started"] is True
    assert "op_status" in started["result"]["check"]

    # The reaper finalizes the status file when the detached child exits.
    for _ in range(100):
        status = _background_op_status(store, job_id, "__echo__")
        if status["result"]["state"] != "running":
            break
        await asyncio.sleep(0.1)
    assert status["result"]["state"] == "done"
    assert status["result"]["exit_code"] == 0
    assert status["result"]["result"] == payload


@pytest.mark.asyncio
async def test_background_op_idempotent_while_running(tmp_path) -> None:
    store, job_id = _store(tmp_path)
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    import os

    # A "running" status with THIS process's pid reads as alive.
    (ops_dir / "backtest_job.json").write_text(
        json.dumps({"op": "backtest_job", "state": "running", "pid": os.getpid()})
    )
    again = await _start_background_op(store, job_id, "backtest_job", {})
    assert again["result"]["already_running"] is True


def test_op_status_detects_lost_and_orphan_done(tmp_path) -> None:
    store, job_id = _store(tmp_path)
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)

    # Dead pid + no result file -> the run is lost (MCP server restarted and
    # the detached child died with nothing to show).
    (ops_dir / "backtest_job.json").write_text(
        json.dumps({"op": "backtest_job", "state": "running", "pid": 2**22 - 1})
    )
    status = _background_op_status(store, job_id, "backtest_job")
    assert status["result"]["state"] == "lost"
    assert "hint" in status["result"]

    # Dead pid + parseable result -> the detached child finished on its own
    # while nobody was watching; the result is authoritative.
    (ops_dir / "experiments.json").write_text(
        json.dumps({"op": "experiments", "state": "running", "pid": 2**22 - 1})
    )
    (ops_dir / "experiments.result.json").write_text(json.dumps({"ranked": []}))
    status = _background_op_status(store, job_id, "experiments")
    assert status["result"]["state"] == "done"
    assert status["result"]["result"] == {"ranked": []}

    missing = _background_op_status(store, job_id, "never_ran")
    assert missing["error"]["code"] == "not_found"


def test_sync_status_reconciles_dead_detached_operation(tmp_path) -> None:
    store, job_id = _store(tmp_path)
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    status_path = ops_dir / "evolution_finalize.json"
    status_path.write_text(
        json.dumps({"op": "evolution_finalize", "state": "running", "pid": 2**22 - 1})
    )

    summary = op_status_summary(store.job_dir(job_id), "evolution_finalize")

    assert summary and summary["status"] == "failed"
    reconciled = json.loads(status_path.read_text())
    assert reconciled["state"] == "failed"
    assert reconciled["reconciled_at"]
    assert "without a result" in reconciled["error"]


def test_detached_process_identity_rejects_pid_reuse_after_restart(monkeypatch) -> None:
    import wayfinder_paths.jobs.execution.op_process as process_module

    monkeypatch.setattr(process_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        process_module,
        "process_identity_fields",
        lambda pid: {"boot_id": "new-boot", "process_start_ticks": 222},
    )
    assert not recorded_process_alive(
        {
            "pid": 42,
            "boot_id": "old-boot",
            "process_start_ticks": 111,
        }
    )
    assert not recorded_process_alive(
        {"pid": 42, "boot_id": "new-boot", "process_start_ticks": 111}
    )
    assert recorded_process_alive(
        {"pid": 42, "boot_id": "new-boot", "process_start_ticks": 222}
    )


def test_legacy_detached_status_older_than_boot_is_not_alive(monkeypatch) -> None:
    import wayfinder_paths.jobs.execution.op_process as process_module

    monkeypatch.setattr(process_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(process_module, "process_identity_fields", lambda pid: {})
    monkeypatch.setattr(
        process_module,
        "_linux_booted_at",
        lambda: datetime(2026, 8, 28, 5, tzinfo=UTC),
    )

    assert not recorded_process_alive(
        {"pid": 42, "started_at": "2026-08-28T04:38:54+00:00"}
    )


def test_campaign_close_reaps_registered_runner_group(tmp_path, monkeypatch) -> None:
    store, job_id = _store(tmp_path)
    registry = store.job_dir(job_id) / "state" / "running_ops"
    registry.mkdir(parents=True)
    (registry / "4242.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "process_group": 4242,
                "op": "evolution_prepare",
                "campaign_id": "campaign-1",
                "resource_tier": "control",
                "process_start_ticks": 1234,
            }
        )
    )
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    status_path = ops_dir / "evolution_prepare.json"
    status_path.write_text(
        json.dumps({"pid": 4242, "op": "evolution_prepare", "state": "running"})
    )
    killed = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.op_process._pid_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.op_process._pid_matches_runner",
        lambda pid, op, start_ticks: True,
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.op_process.os.getpgid", lambda pid: pid
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.op_process.os.killpg",
        lambda process_group, sig: killed.append((process_group, sig)),
    )

    reaped = terminate_campaign_ops(store, job_id, "campaign-1")

    assert reaped == [
        {"pid": 4242, "op": "evolution_prepare", "resource_tier": "control"}
    ]
    assert killed and killed[0][0] == 4242
    assert not (registry / "4242.json").exists()
    status = json.loads(status_path.read_text())
    assert status["state"] == "killed"
    assert status["reason"] == "owning evolution campaign closed"


@pytest.mark.asyncio
async def test_backtest_job_defaults_to_background(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(store, job_id, op, kwargs):
        captured.update({"job_id": job_id, "op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"started": True}}

    async def fake_sync(op, kwargs):
        captured["sync_op"] = op
        return {"ok": True, "result": {}}

    monkeypatch.setattr(jobs_module, "_start_background_op", fake_start)
    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))

    result = await core_jobs(action="backtest_job", job_id="bg-demo")
    assert result["result"]["started"] is True
    assert captured["op"] == "backtest_job"
    assert captured["kwargs"]["job_id"] == "bg-demo"
    assert "sync_op" not in captured

    # Explicit background=False keeps the synchronous path (quick iterations).
    await core_jobs(action="backtest_job", job_id="bg-demo", background=False)
    assert captured["sync_op"] == "backtest_job"


@pytest.mark.asyncio
async def test_robustness_check_defaults_to_background(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(store, job_id, op, kwargs):
        captured.update({"job_id": job_id, "op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"started": True}}

    async def fake_sync(op, kwargs):
        captured["sync_op"] = op
        return {"ok": True, "result": {}}

    monkeypatch.setattr(jobs_module, "_start_background_op", fake_start)
    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))

    plan = {"leverage": [1, 2]}
    result = await core_jobs(
        action="robustness_check", job_id="bg-demo", robustness_plan=plan
    )
    assert result["result"]["started"] is True
    assert captured == {
        "job_id": "bg-demo",
        "op": "robustness_check",
        "kwargs": {
            "job_id": "bg-demo",
            "candidate_dir": None,
            "robustness_plan": plan,
        },
    }

    await core_jobs(
        action="robustness_check",
        job_id="bg-demo",
        robustness_plan=plan,
        background=False,
    )
    assert captured["sync_op"] == "robustness_check"


@pytest.mark.asyncio
async def test_evolution_heavy_stages_default_to_background(
    tmp_path, monkeypatch
) -> None:
    captured: list[tuple[str, dict]] = []

    async def fake_start(store, job_id, op, kwargs):
        captured.append((op, kwargs))
        return {"ok": True, "result": {"started": True}}

    monkeypatch.setattr(jobs_module, "_start_background_op", fake_start)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))

    evaluated = await core_jobs(
        action="evolution_evaluate",
        job_id="majors-5m-lab",
        candidate_id="candidate-1",
    )
    finalized = await core_jobs(action="evolution_finalize", job_id="majors-5m-lab")
    experienced = await core_jobs(action="forward_experience", job_id="majors-5m-lab")
    assert evaluated["result"]["started"] is True
    assert finalized["result"]["started"] is True
    assert experienced["result"]["started"] is True
    assert captured == [
        (
            "evolution_evaluate",
            {"job_id": "majors-5m-lab", "candidate_id": "candidate-1"},
        ),
        ("evolution_finalize", {"job_id": "majors-5m-lab"}),
        ("forward_experience", {"job_id": "majors-5m-lab"}),
    ]


@pytest.mark.asyncio
async def test_evolution_design_is_a_bounded_synchronous_control_op(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}

    async def fake_sync(op, kwargs):
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"accepted": True}}

    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    design = {"hypotheses": [{"id": "h1"}], "slots": [{"slot_id": "s1"}]}

    result = await core_jobs(
        action="evolution_design",
        job_id="majors-5m-lab",
        campaign_design=design,
    )

    assert result["result"]["accepted"] is True
    assert captured == {
        "op": "evolution_design",
        "kwargs": {"job_id": "majors-5m-lab", "campaign_design": design},
    }


@pytest.mark.asyncio
async def test_evolution_designer_can_launch_then_end_before_stage_nudge(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}

    async def fake_start(store, job_id, op, kwargs):
        captured.update({"job_id": job_id, "op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"started": True}}

    monkeypatch.setattr(jobs_module, "_start_background_op", fake_start)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    design = {"hypotheses": [{"id": "h1"}], "slots": [{"slot_id": "s1"}]}

    result = await core_jobs(
        action="evolution_design",
        job_id="majors-5m-lab",
        campaign_design=design,
        background=True,
    )

    assert result["result"]["started"] is True
    assert captured == {
        "job_id": "majors-5m-lab",
        "op": "evolution_design",
        "kwargs": {"job_id": "majors-5m-lab", "campaign_design": design},
    }


@pytest.mark.asyncio
async def test_evolution_compose_is_a_synchronous_control_op(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.execution.op_process import _CONTROL_PLANE_OPS
    from wayfinder_paths.jobs.execution.op_runner import _NUDGE_OPS

    captured: dict = {}

    async def fake_sync(op, kwargs):
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"status": "scanned"}}

    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    proposals = [{"name": "ws_x", "expression": "close(f) > 0", "min_bars": 2}]

    result = await core_jobs(
        action="evolution_compose",
        job_id="majors-5m-lab",
        signal_proposals=proposals,
    )

    assert result["result"]["status"] == "scanned"
    assert captured == {
        "op": "evolution_compose",
        "kwargs": {"job_id": "majors-5m-lab", "signal_proposals": proposals},
    }
    # An empty list is a valid submission (it ends composition); a missing
    # list is not.
    captured.clear()
    await core_jobs(
        action="evolution_compose", job_id="majors-5m-lab", signal_proposals=[]
    )
    assert captured["kwargs"] == {"job_id": "majors-5m-lab", "signal_proposals": []}
    missing = await core_jobs(action="evolution_compose", job_id="majors-5m-lab")
    assert "requires job_id and signal_proposals" in json.dumps(missing)
    assert (
        "evolution_compose" in _CONTROL_PLANE_OPS and "evolution_compose" in _NUDGE_OPS
    )


@pytest.mark.asyncio
async def test_evolution_mechanism_grid_is_a_synchronous_control_op(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.execution.op_process import _CONTROL_PLANE_OPS

    captured: dict = {}

    async def fake_sync(op, kwargs):
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"pointer": "/mechanism_grids/0"}}

    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    result = await core_jobs(
        action="evolution_mechanism_grid",
        job_id="majors-5m-lab",
        signal_ref="/validated_signals/replicated/0",
        side="short",
    )
    assert result["result"]["pointer"] == "/mechanism_grids/0"
    assert captured == {
        "op": "evolution_mechanism_grid",
        "kwargs": {
            "job_id": "majors-5m-lab",
            "signal_ref": "/validated_signals/replicated/0",
            "side": "short",
        },
    }
    missing = await core_jobs(action="evolution_mechanism_grid", job_id="majors-5m-lab")
    assert "requires job_id and signal_ref" in json.dumps(missing)
    assert "evolution_mechanism_grid" in _CONTROL_PLANE_OPS


async def test_evolution_redesign_is_a_synchronous_control_op(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.execution.op_process import _CONTROL_PLANE_OPS
    from wayfinder_paths.jobs.execution.op_runner import _NUDGE_OPS

    captured: dict = {}

    async def fake_sync(op, kwargs):
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"status": "accepted"}}

    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    decision = {"abandon": ["c04"], "keep": ["c02"], "hypotheses": [], "slots": []}

    result = await core_jobs(
        action="evolution_redesign", job_id="majors-5m-lab", redesign=decision
    )

    assert result["result"]["status"] == "accepted"
    assert captured == {
        "op": "evolution_redesign",
        "kwargs": {"job_id": "majors-5m-lab", "redesign": decision},
    }
    missing = await core_jobs(action="evolution_redesign", job_id="majors-5m-lab")
    assert "requires job_id and redesign" in json.dumps(missing)
    assert (
        "evolution_redesign" in _CONTROL_PLANE_OPS
        and "evolution_redesign" in _NUDGE_OPS
    )


def test_redesign_nudge_retries_while_the_designer_session_is_busy(monkeypatch) -> None:
    from wayfinder_paths.jobs.execution import op_runner

    seen: list[str] = []
    replies = iter([{"busy": True}, {"transition_pending": True}, {"ok": True}])

    def fake_nudge(store, job_id):
        seen.append(job_id)
        return next(replies, {"ok": True})

    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.nudge_evolution_session", fake_nudge
    )
    monkeypatch.setattr(op_runner.time, "sleep", lambda _seconds: None)
    op_runner._nudge_evolution("evolution_redesign", {"job_id": "majors-5m-lab"})
    assert len(seen) == 3


# --- heavy lane: hosted boxes queue heavy ops behind the live loop ----------


def _fake_lane_submission(entry: str = "e-ours") -> dict:
    return {
        "queued": True,
        "entry": entry,
        "class": "owner",
        "position": 1,
        "op": "experiments",
        "job_id": "bg-demo",
        "state": "queued",
        "queued_at": "2026-09-22T10:00:00+00:00",
        "queue_entry": entry,
        "lane_class": "owner",
        "submitted_by": "mcp",
    }


@pytest.mark.asyncio
async def test_experiments_defaults_to_background(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(store, job_id, op, kwargs):
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"started": True}}

    async def fake_sync(op, kwargs):
        captured["sync_op"] = op
        return {"ok": True, "result": {}}

    monkeypatch.setattr(jobs_module, "_start_background_op", fake_start)
    monkeypatch.setattr(jobs_module, "_run_job_op", fake_sync)
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))

    grid = {"threshold": [1, 2]}
    result = await core_jobs(action="experiments", job_id="bg-demo", grid=grid)
    assert result["result"]["started"] is True
    assert captured["op"] == "experiments"
    assert captured["kwargs"]["grid"] == grid
    assert "sync_op" not in captured

    await core_jobs(action="experiments", job_id="bg-demo", grid=grid, background=False)
    assert captured["sync_op"] == "experiments"


@pytest.mark.asyncio
async def test_lane_op_queues_on_hosted_box_with_runnerd(tmp_path, monkeypatch) -> None:
    store, job_id = _store(tmp_path)
    submitted: dict = {}

    def fake_submit(repo_root, job_id, op, kwargs, *, submitted_by, notify=None):
        submitted.update(
            {"repo_root": repo_root, "op": op, "kwargs": kwargs, "by": submitted_by}
        )
        submitted["notify"] = notify
        return _fake_lane_submission()

    monkeypatch.setattr(jobs_module, "lane_enabled", lambda: True)
    monkeypatch.setattr(jobs_module, "_runnerd_reachable", lambda repo_root: True)
    monkeypatch.setattr(jobs_module, "submit_heavy_op", fake_submit)
    monkeypatch.setattr(
        jobs_module,
        "queued_entries",
        lambda repo_root: [
            {
                "entry_id": "e-other",
                "job_id": "other",
                "op": "backtest_job",
                "class": "owner",
            },
            {
                "entry_id": "e-ours",
                "job_id": job_id,
                "op": "experiments",
                "class": "owner",
            },
        ],
    )
    monkeypatch.setattr(
        jobs_module, "lane_snapshot", lambda repo_root: {"running": None, "queued": []}
    )
    monkeypatch.setattr(
        jobs_module,
        "_submitting_session",
        lambda: {"session_id": "ses_user", "kind": "user", "wake": False},
    )

    started = await _start_background_op(store, job_id, "experiments", {"grid": {}})

    result = started["result"]
    assert result["queued"] is True
    assert result["position"] == 1
    assert result["entry"] == "e-ours"
    assert result["ahead"] == [
        {"job_id": "other", "op": "backtest_job", "class": "owner"}
    ]
    assert result["notify"] == {"mode": "prompt", "session_id": "ses_user"}
    assert "live trading loop" in result["note"] and "op_cancel" in result["note"]
    assert "op_status" in result["check"]
    assert submitted["by"] == "mcp"
    assert submitted["notify"]["kind"] == "user"
    assert submitted["repo_root"] == store.repo_root

    # A duplicate submission passes the existing slot straight through.
    monkeypatch.setattr(
        jobs_module,
        "submit_heavy_op",
        lambda *a, **k: {"already_queued": True, "position": 0, "state": "queued"},
    )
    again = await _start_background_op(store, job_id, "experiments", {"grid": {}})
    assert again["result"]["already_queued"] is True
    assert "op_status" in again["result"]["check"]


def _echo_command(op: str) -> list[str]:
    import sys

    return [sys.executable, "-c", "import sys; sys.stdin.read(); print('{}')"]


@pytest.mark.asyncio
async def test_lane_op_spawns_directly_off_hosted_boxes(tmp_path, monkeypatch) -> None:
    store, job_id = _store(tmp_path)
    monkeypatch.setattr(jobs_module, "lane_enabled", lambda: False)
    monkeypatch.setattr(jobs_module, "op_runner_command", _echo_command)

    def never(*args, **kwargs):
        raise AssertionError("the lane must not be consulted off a hosted box")

    monkeypatch.setattr(jobs_module, "submit_heavy_op", never)
    monkeypatch.setattr(jobs_module, "_runnerd_reachable", never)

    started = await _start_background_op(store, job_id, "experiments", {})
    assert started["result"]["started"] is True
    assert "lane" not in started["result"]


@pytest.mark.asyncio
async def test_lane_op_bypasses_lane_when_runnerd_unreachable(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _store(tmp_path)
    monkeypatch.setattr(jobs_module, "lane_enabled", lambda: True)
    monkeypatch.setattr(jobs_module, "_runnerd_reachable", lambda repo_root: False)
    monkeypatch.setattr(jobs_module, "op_runner_command", _echo_command)

    def never(*args, **kwargs):
        raise AssertionError("no daemon means no lane submission")

    monkeypatch.setattr(jobs_module, "submit_heavy_op", never)

    started = await _start_background_op(store, job_id, "backtest_job", {})
    assert started["result"]["started"] is True
    assert started["result"]["lane"] == "bypassed"


def test_op_status_reports_queued_position(tmp_path, monkeypatch) -> None:
    store, job_id = _store(tmp_path)
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    (ops_dir / "experiments.json").write_text(
        json.dumps(
            {
                "op": "experiments",
                "job_id": job_id,
                "state": "queued",
                "queued_at": datetime.now(UTC).isoformat(),
                "queue_entry": "e-ours",
                "lane_class": "owner",
            }
        )
    )
    monkeypatch.setattr(jobs_module, "queue_position", lambda repo_root, entry: 2)
    monkeypatch.setattr(
        jobs_module,
        "lane_snapshot",
        lambda repo_root: {"running": {"job_id": "other"}, "queued": []},
    )

    status = _background_op_status(store, job_id, "experiments")["result"]

    assert status["state"] == "queued"
    assert status["position"] == 2
    assert status["waited_s"] >= 0
    assert status["lane"]["running"] == {"job_id": "other"}
    assert "op_status" in status["check"]

    # Running with a lane class reports elapsed time against its ceiling.
    import os

    (ops_dir / "experiments.json").write_text(
        json.dumps(
            {
                "op": "experiments",
                "state": "running",
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "lane_class": "research",
            }
        )
    )
    running = _background_op_status(store, job_id, "experiments")["result"]
    assert running["state"] == "running"
    assert running["running_s"] >= 0
    assert running["max_runtime_s"] == 1800

    # Terminal states carry the completion hook's notification stamps.
    (ops_dir / "experiments.json").write_text(
        json.dumps(
            {
                "op": "experiments",
                "state": "done",
                "notified_at": "2026-09-22T10:05:00+00:00",
            }
        )
    )
    (ops_dir / "experiments.result.json").write_text(json.dumps({"ranked": []}))
    done = _background_op_status(store, job_id, "experiments")["result"]
    assert done["notified_at"] == "2026-09-22T10:05:00+00:00"
    assert done["result"] == {"ranked": []}


@pytest.mark.asyncio
async def test_op_cancel_dispatches_to_the_lane(tmp_path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(jobs_module, "JobStore", lambda: JobStore(repo_root=tmp_path))
    monkeypatch.setattr(
        jobs_module,
        "cancel_heavy_op",
        lambda repo_root, job_id, op: (
            calls.append((repo_root, job_id, op))
            or {"cancelled": True, "was": "queued", "op": op, "job_id": job_id}
        ),
    )

    cancelled = await core_jobs(action="op_cancel", job_id="bg-demo", op="experiments")
    assert cancelled["result"]["cancelled"] is True
    assert calls == [(tmp_path.resolve(), "bg-demo", "experiments")]

    monkeypatch.setattr(
        jobs_module,
        "cancel_heavy_op",
        lambda repo_root, job_id, op: {"cancelled": False, "error": "not_found"},
    )
    failed = await core_jobs(action="op_cancel", job_id="bg-demo", op="experiments")
    assert failed["error"]["code"] == "op_cancel_failed"
    assert "not_found" in failed["error"]["message"]

    missing = await core_jobs(action="op_cancel", job_id="bg-demo")
    assert missing["error"]["code"] == "invalid_request"


def _capture_subprocess(monkeypatch) -> dict:
    captured: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def capturing_exec(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        captured["proc"] = proc
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)
    return captured


def _process_group_gone(pid: int) -> bool:
    import os

    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_run_job_op_cancellation_kills_the_child(monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "op_runner_command", lambda op: ["sleep", "30"])
    captured = _capture_subprocess(monkeypatch)

    task = asyncio.create_task(jobs_module._run_job_op("experiments", {}))
    for _ in range(50):
        if "proc" in captured:
            break
        await asyncio.sleep(0.02)
    proc = captured["proc"]
    assert proc.returncode is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.returncode is not None and proc.returncode < 0
    assert _process_group_gone(proc.pid)
    # A lane-class op forced synchronous shares the box with the live tick.
    assert captured["env"]["WAYFINDER_MAX_BACKTEST_WORKERS"] == "1"


@pytest.mark.asyncio
async def test_run_job_op_timeout_kills_the_child(monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "op_runner_command", lambda op: ["sleep", "30"])
    monkeypatch.setattr(jobs_module, "SYNC_OP_TIMEOUT_S", 0.2)
    captured = _capture_subprocess(monkeypatch)

    result = await jobs_module._run_job_op("attribution", {})

    assert result["error"]["code"] == "job_op_timeout"
    assert "background=True" in result["error"]["message"]
    proc = captured["proc"]
    assert proc.returncode is not None and proc.returncode < 0
    assert _process_group_gone(proc.pid)
    assert captured["env"] is None


def test_cli_heavy_op_queues_and_waits_on_hosted_box(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    store, job_id = _store(tmp_path)
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    submissions: list = []

    def fake_submit(repo_root, job_id, op, kwargs, *, submitted_by, notify=None):
        submissions.append((op, kwargs, submitted_by))
        # The daemon already ran it: the wait loop reads the finished status.
        (ops_dir / f"{op}.json").write_text(json.dumps({"op": op, "state": "done"}))
        (ops_dir / f"{op}.result.json").write_text(json.dumps({"ranked": [1]}))
        return {"queued": True, "entry": "e1", "position": 0}

    monkeypatch.setattr(cli_module, "JobStore", lambda: store)
    monkeypatch.setattr(cli_module, "lane_enabled", lambda: True)
    monkeypatch.setattr(cli_module, "submit_heavy_op", fake_submit)
    monkeypatch.setattr(cli_module, "LANE_POLL_S", 0.0)
    monkeypatch.setattr(cli_module, "run_experiment", lambda *a, **k: {"inline": True})
    runner = CliRunner()

    waited = runner.invoke(
        cli_module.job_cli, ["experiments", job_id, "--grid", "grid.json"]
    )
    assert waited.exit_code == 0, waited.output
    assert json.loads(waited.output)["result"] == {"ranked": [1]}
    assert submissions[-1][0] == "experiments"
    assert submissions[-1][1]["grid"] == "grid.json"
    assert submissions[-1][2] == "cli"

    detached = runner.invoke(
        cli_module.job_cli, ["experiments", job_id, "--grid", "grid.json", "--detach"]
    )
    assert detached.exit_code == 0, detached.output
    assert json.loads(detached.output)["result"]["queued"] is True

    foreground = runner.invoke(
        cli_module.job_cli,
        ["experiments", job_id, "--grid", "grid.json", "--foreground"],
    )
    assert foreground.exit_code == 0, foreground.output
    assert json.loads(foreground.output)["result"] == {"inline": True}
    assert len(submissions) == 2

    # A failed lane run exits non-zero and says why.
    def failing_submit(repo_root, job_id, op, kwargs, *, submitted_by, notify=None):
        (ops_dir / f"{op}.json").write_text(json.dumps({"op": op, "state": "killed"}))
        return {"queued": True, "entry": "e2", "position": 0}

    monkeypatch.setattr(cli_module, "submit_heavy_op", failing_submit)
    killed = runner.invoke(
        cli_module.job_cli, ["experiments", job_id, "--grid", "grid.json"]
    )
    assert killed.exit_code == 1
    assert "op_killed" in killed.output


def test_cli_heavy_op_runs_as_before_off_hosted_boxes(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    store, job_id = _store(tmp_path)
    monkeypatch.setattr(cli_module, "JobStore", lambda: store)
    monkeypatch.setattr(cli_module, "lane_enabled", lambda: False)
    spawned: list = []
    monkeypatch.setattr(
        cli_module,
        "spawn_detached_op",
        lambda store, job_id, op, kwargs: spawned.append(op) or {"started": True},
    )
    monkeypatch.setattr(
        cli_module, "robustness_check_job", lambda *a, **k: {"inline": True}
    )
    monkeypatch.setattr(cli_module, "run_experiment", lambda *a, **k: {"inline": True})
    runner = CliRunner()

    # robustness-check keeps its detached default; --foreground stays inline.
    outcome = runner.invoke(cli_module.job_cli, ["robustness-check", job_id])
    assert outcome.exit_code == 0, outcome.output
    assert spawned == ["robustness_check"]
    outcome = runner.invoke(
        cli_module.job_cli, ["robustness-check", job_id, "--foreground"]
    )
    assert json.loads(outcome.output)["result"] == {"inline": True}

    # experiments keeps its synchronous default.
    outcome = runner.invoke(
        cli_module.job_cli, ["experiments", job_id, "--grid", "grid.json"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert json.loads(outcome.output)["result"] == {"inline": True}
    assert spawned == ["robustness_check"]

"""Detached background job ops: a backtest cannot fit through the MCP
request window on the box (client timeout kills the run mid-grind, and the
memory spike OOM-killed the conversation server — observed live). The op
runs detached with a status file; op_status polls it."""

from __future__ import annotations

import asyncio
import json

import pytest

import wayfinder_paths.mcp.tools.jobs as jobs_module
from wayfinder_paths.jobs.background import op_status_summary
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

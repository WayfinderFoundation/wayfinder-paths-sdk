from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.forward import (
    ForwardRecorder,
    get_forward_recorder,
    load_forward_snapshot,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def test_forward_recorder_uses_env_and_preserves_loose_rows(
    tmp_path: Path, monkeypatch
) -> None:
    job_dir = tmp_path / ".wayfinder" / "jobs" / "demo-job"
    forward_dir = job_dir / "results" / "forward"
    monkeypatch.setenv("WAYFINDER_HIGH_LEVEL_JOB_ID", "demo-job")
    monkeypatch.setenv("WAYFINDER_JOB_DIR", str(job_dir))
    monkeypatch.setenv("WAYFINDER_FORWARD_DIR", str(forward_dir))
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.setenv("WAYFINDER_JOB_REVISION", "abc123")
    monkeypatch.setenv("WAYFINDER_RUN_ID", "run-1")

    recorder = get_forward_recorder()
    run = recorder.record_run(
        decision="wait",
        reason="SNX blocked",
        state={"SNX": {"rearm": "blocked"}},
        metrics={"gap_pct": -2.7},
        custom_dimension="kept",
    )
    recorder.record_order(
        order_id="stop-1",
        order_type="stop_loss",
        status="pending",
        reconciliation="order still live",
    )
    recorder.record_fill(order_id="entry-1", price=1.23, partial=True)
    recorder.record_trade({"trade": 1, "pnl": {"net_usd": -0.9}, "reason": "stop"})

    assert run["job_id"] == "demo-job"
    assert run["mode"] == "paper"
    assert run["revision"] == "abc123"
    assert run["custom_dimension"] == "kept"
    assert read_jsonl(forward_dir / "runs.jsonl")[0]["decision"]["action"] == "wait"
    assert read_jsonl(forward_dir / "orders.jsonl")[0]["status"] == "pending"

    summary = json.loads((forward_dir / "summary.json").read_text("utf-8"))
    assert summary["runs"]["count"] == 1
    assert summary["orders"]["pending_count"] == 1
    assert summary["fills"]["count"] == 1
    assert summary["trades"]["closed_count"] == 1
    assert summary["trades"]["losses"] == 1


def test_forward_snapshot_is_capped_and_accepts_missing_files(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "snapshot-job",
        script=".wayfinder_runs/noop.py",
        interval_seconds=300,
        agent_mode="monitor",
    )
    store.save(job)
    recorder = ForwardRecorder(
        job_id=job.id,
        forward_dir=store.job_dir(job.id) / "results" / "forward",
    )
    for idx in range(3):
        recorder.record_run(decision=f"wait_{idx}", reason="test")
        recorder.record_trade({"trade": idx, "pnl": idx - 1})

    snapshot = load_forward_snapshot(job.id, store=store, limit=2)

    assert snapshot["summary"]["runs"]["count"] == 3
    assert [row["decision"]["action"] for row in snapshot["recent_runs"]] == [
        "wait_1",
        "wait_2",
    ]
    assert [row["trade"] for row in snapshot["recent_trades"]] == [1, 2]
    assert snapshot["recent_orders"] == []


def test_job_store_initializes_forward_files(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "files-job",
        script=".wayfinder_runs/noop.py",
        interval_seconds=300,
    )
    root = store.init_layout(job)

    for name in ("runs.jsonl", "trades.jsonl", "orders.jsonl", "fills.jsonl"):
        assert (root / "results" / "forward" / name).exists()
    summary = json.loads((root / "results" / "forward" / "summary.json").read_text())
    assert summary["job_id"] == "files-job"


def test_compiler_exposes_forward_env(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "env-job",
        script=".wayfinder_runs/noop.py",
        interval_seconds=300,
    )
    root = store.init_layout(job)

    env = JobCompiler(store=store)._job_env(job, root)

    assert env["WAYFINDER_HIGH_LEVEL_JOB_ID"] == "env-job"
    assert env["WAYFINDER_JOB_DIR"] == str(root)
    assert env["WAYFINDER_FORWARD_DIR"] == str(root / "results" / "forward")
    assert env["WAYFINDER_JOB_MODE"] == "paper"

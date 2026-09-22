"""wayfinder job remove/restore: delete the runner loops, archive the job
directory with every sidecar, refuse while money or a background op is at
stake, and put it all back on restore."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from wayfinder_paths.jobs import remove as remove_module
from wayfinder_paths.jobs.execution.op_process import process_identity_fields
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.remove import (
    ARCHIVE_DIRNAME,
    ARCHIVE_MANIFEST,
    ARTIFACTS_SUBDIR,
    remove_job,
    restore_job,
)
from wayfinder_paths.jobs.store import JobStore

JOB_ID = "rm-demo"


def _job(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        JOB_ID,
        script="strategy.py",
        interval_seconds=60,
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job


def _seed_sidecars(tmp_path: Path) -> dict[str, Path]:
    module = JOB_ID.replace("-", "_")
    sidecars = {
        "runner_log": tmp_path
        / ".wayfinder/runner/logs"
        / f"{JOB_ID}-script"
        / "x.log",
        "monitor_state": (
            tmp_path / ".wayfinder/runner/job_state" / f"{JOB_ID}-agent" / "k.json"
        ),
        "wrapper": tmp_path / ".wayfinder_runs/jobs" / f"{module}_script.py",
        "governance": tmp_path / "governance" / JOB_ID / "external_target.yaml",
    }
    for path in sidecars.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return sidecars


def _fake_bridge(monkeypatch, *, response: dict[str, Any] | None = None) -> list[str]:
    deleted: list[str] = []
    reply = response or {"ok": True, "result": {"deleted": True}}

    class Bridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def delete(self, name: str) -> dict[str, Any]:
            deleted.append(name)
            return dict(reply)

    monkeypatch.setattr(remove_module, "RunnerBridge", Bridge)
    return deleted


def _patch_sync(monkeypatch) -> list[bool]:
    synced: list[bool] = []
    monkeypatch.setattr(
        remove_module, "sync_all_jobs", lambda **kwargs: synced.append(True)
    )
    return synced


def _journal(where: Path) -> list[dict[str, Any]]:
    text = (where / "journal.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.strip().splitlines()]


def _archives(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / ".wayfinder" / ARCHIVE_DIRNAME).glob(f"{JOB_ID}.*"))


def test_remove_archives_job_with_loops_and_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    sidecars = _seed_sidecars(tmp_path)
    deleted = _fake_bridge(monkeypatch)
    synced = _patch_sync(monkeypatch)

    result = remove_job(store, job.id)

    assert result["removed"] is True
    assert result["forced"] is False
    assert result["undo"] == {"command": f"wayfinder job restore {JOB_ID}"}
    assert not store.job_dir(job.id).exists()
    assert store.list_jobs() == []
    assert deleted == [f"{JOB_ID}-script", f"{JOB_ID}-agent"]
    assert synced == [True]

    archives = _archives(tmp_path)
    assert len(archives) == 1
    archive = archives[0]
    assert re.fullmatch(rf"{JOB_ID}\.\d{{8}}T\d{{6}}Z", archive.name)
    assert result["archive_dir"] == str(archive.relative_to(tmp_path))
    assert (archive / "job.yaml").exists()

    manifest = json.loads((archive / ARCHIVE_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["job_id"] == JOB_ID
    assert manifest["by"] == "owner"
    assert manifest["forced"] is False
    assert manifest["archive_dir"] == result["archive_dir"]
    assert [row["loop"] for row in manifest["runner_responses"]] == ["script", "agent"]
    assert manifest["undo"] == result["undo"]
    assert {row["original"] for row in manifest["moved"]} == {
        str(path.parent.relative_to(tmp_path))
        if key != "wrapper"
        else str(path.relative_to(tmp_path))
        for key, path in sidecars.items()
    }

    tail = _journal(archive)[-1]
    assert tail["type"] == "job_removed"
    assert tail["undo"] == result["undo"]
    assert tail["archive_dir"] == result["archive_dir"]

    for path in sidecars.values():
        assert not path.exists()
    artifacts = archive / ARTIFACTS_SUBDIR
    assert (artifacts / "runner_logs" / "script" / "x.log").exists()
    assert (artifacts / "runner_state" / f"{JOB_ID}-agent" / "k.json").exists()
    assert (artifacts / "wrappers" / "rm_demo_script.py").exists()
    assert (artifacts / "governance" / "external_target.yaml").exists()
    assert not (tmp_path / "governance" / JOB_ID).exists()


def test_remove_refuses_live_job(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    job.script_loop.mode = "live"
    store.save(job)
    deleted = _fake_bridge(monkeypatch)
    synced = _patch_sync(monkeypatch)

    with pytest.raises(ValueError, match=rf"^cannot remove: {JOB_ID} is live"):
        remove_job(store, job.id)

    assert store.job_yaml_path(job.id).exists()
    assert deleted == []
    assert synced == []
    assert _archives(tmp_path) == []


def test_remove_refuses_venue_funded_job(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    job.execution_params["initial_capital"] = 52.8
    store.save(job)
    store.write_json(job.id, "state/funding.json", {"venue_funded": True})
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)

    with pytest.raises(
        ValueError, match=r"^cannot remove: .* holds venue capital \(\$52\.8 declared\)"
    ):
        remove_job(store, job.id)
    assert store.job_yaml_path(job.id).exists()

    # Funded flag without declared capital is not venue money at stake.
    job.execution_params["initial_capital"] = 0
    store.save(job)
    assert remove_job(store, job.id)["removed"] is True


def test_remove_refuses_live_engine_positions(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    store.write_json(
        job.id,
        "state/engine_state.json",
        {
            "mode": "live",
            "positions": {"ETH": {"qty": 2}, "BTC": {"qty": 1}, "SOL": None},
        },
    )
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)

    with pytest.raises(
        ValueError,
        match=r"^cannot remove: the live engine holds open positions \(BTC, ETH\)",
    ):
        remove_job(store, job.id)
    assert store.job_yaml_path(job.id).exists()


def test_force_bypasses_only_the_capital_checks(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    job.script_loop.mode = "live"
    job.execution_params["initial_capital"] = 100
    store.save(job)
    store.write_json(job.id, "state/funding.json", {"venue_funded": True})
    store.write_json(
        job.id,
        "state/engine_state.json",
        {"mode": "live", "positions": {"BTC": {"qty": 1}}},
    )
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)

    result = remove_job(store, job.id, force=True)

    assert result["forced"] is True
    archive = _archives(tmp_path)[0]
    manifest = json.loads((archive / ARCHIVE_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["forced"] is True
    assert _journal(archive)[-1]["forced"] is True


def test_remove_refuses_running_background_op_even_when_forced(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    ops_dir = store.job_dir(job.id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True)
    (ops_dir / "backtest.json").write_text(
        json.dumps(
            {
                "op": "backtest",
                "job_id": job.id,
                "state": "running",
                "pid": os.getpid(),
                "started_at": utc_now_iso(),
                **process_identity_fields(os.getpid()),
            }
        ),
        encoding="utf-8",
    )
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)

    with pytest.raises(
        ValueError,
        match=r"^cannot remove: a background operation is still running \(backtest\)",
    ):
        remove_job(store, job.id, force=True)
    assert store.job_yaml_path(job.id).exists()


def test_remove_ignores_finished_background_ops(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    ops_dir = store.job_dir(job.id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True)
    (ops_dir / "backtest.json").write_text(
        json.dumps({"op": "backtest", "state": "done", "pid": os.getpid()}),
        encoding="utf-8",
    )
    # A result file is never an op status, whatever it says.
    (ops_dir / "backtest.result.json").write_text(
        json.dumps({"state": "running", "pid": os.getpid()}), encoding="utf-8"
    )
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)

    assert remove_job(store, job.id)["removed"] is True


def test_remove_keeps_everything_when_runner_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    sidecars = _seed_sidecars(tmp_path)
    _fake_bridge(
        monkeypatch, response={"ok": False, "error": "job is currently running"}
    )
    synced = _patch_sync(monkeypatch)
    journal_before = (store.job_dir(job.id) / "journal.jsonl").read_text(
        encoding="utf-8"
    )

    with pytest.raises(
        ValueError,
        match=rf"^cannot remove: runner refused to delete loop {JOB_ID}-script: "
        r"job is currently running",
    ):
        remove_job(store, job.id)

    assert store.job_yaml_path(job.id).exists()
    journal_after = (store.job_dir(job.id) / "journal.jsonl").read_text(
        encoding="utf-8"
    )
    assert journal_after == journal_before
    assert not (store.job_dir(job.id) / ARCHIVE_MANIFEST).exists()
    assert _archives(tmp_path) == []
    assert all(path.exists() for path in sidecars.values())
    assert synced == []


def test_remove_accepts_loops_the_runner_never_had(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    deleted = _fake_bridge(
        monkeypatch, response={"ok": False, "error": f"Job not found: {JOB_ID}-agent"}
    )
    _patch_sync(monkeypatch)

    result = remove_job(store, job.id)

    assert result["removed"] is True
    assert deleted == [f"{JOB_ID}-script", f"{JOB_ID}-agent"]
    assert all(not row["response"]["ok"] for row in result["loops"])


def test_cli_remove_refuses_then_forces(tmp_path: Path, monkeypatch) -> None:
    from wayfinder_paths.jobs import cli as cli_module

    store, job = _job(tmp_path)
    job.execution_params["initial_capital"] = 52.8
    store.save(job)
    store.write_json(job.id, "state/funding.json", {"venue_funded": True})
    monkeypatch.setattr(cli_module, "JobStore", lambda: store)
    _fake_bridge(monkeypatch)
    _patch_sync(monkeypatch)
    runner = CliRunner()

    refused = runner.invoke(cli_module.job_cli, ["remove", job.id])
    assert refused.exit_code != 0
    assert "Error: cannot remove:" in refused.output
    assert "withdraw" in refused.output
    assert store.job_yaml_path(job.id).exists()

    forced = runner.invoke(cli_module.job_cli, ["remove", job.id, "--force"])
    assert forced.exit_code == 0, forced.output
    payload = json.loads(forced.output)
    assert payload["ok"] is True
    assert payload["result"]["forced"] is True
    assert not store.job_yaml_path(job.id).exists()


def test_restore_round_trip(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    sidecars = _seed_sidecars(tmp_path)
    _fake_bridge(monkeypatch)
    synced = _patch_sync(monkeypatch)
    compiled: list[str] = []
    paused: list[str] = []

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job, *, start_daemon: bool = True):  # noqa: ANN001
            compiled.append(job.id)
            return {"job_id": job.id, "jobs": [{"loop": "script"}]}

    def fake_pause(store, job_id):  # noqa: ANN001
        paused.append(job_id)
        return [{"loop": "script", "response": {"ok": True}}]

    monkeypatch.setattr(remove_module, "JobCompiler", FakeCompiler)
    monkeypatch.setattr(remove_module, "pause_job_loops", fake_pause)

    with pytest.raises(ValueError, match=r"^cannot restore: no archive of never-was"):
        restore_job(store, "never-was")

    removed = remove_job(store, job.id)
    result = restore_job(store, job.id)

    assert result["restored"] is True
    assert result["from"] == removed["archive_dir"]
    assert result["compile"] == {"job_id": JOB_ID, "jobs": [{"loop": "script"}]}
    assert result["loops"] == [{"loop": "script", "response": {"ok": True}}]
    assert compiled == [JOB_ID]
    assert paused == [JOB_ID]
    assert store.load(job.id).id == JOB_ID
    assert [item.id for item in store.list_jobs()] == [JOB_ID]
    assert not (store.job_dir(job.id) / ARCHIVE_MANIFEST).exists()
    assert _archives(tmp_path) == []
    assert all(path.exists() for path in sidecars.values())
    assert not (store.job_dir(job.id) / ARTIFACTS_SUBDIR).exists()
    tail = _journal(store.job_dir(job.id))[-1]
    assert tail["type"] == "job_restored"
    assert tail["from"] == removed["archive_dir"]
    assert tail["undo"] == {"command": f"wayfinder job remove {JOB_ID}"}
    assert synced == [True, True]

    with pytest.raises(
        ValueError, match="^cannot restore: a job with this id already exists"
    ):
        restore_job(store, job.id)

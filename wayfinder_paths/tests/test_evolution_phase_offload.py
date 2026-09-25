from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

import wayfinder_paths.jobs.evolution_campaign as campaign_module
from wayfinder_paths.jobs.backtest_runner import (
    RUNNERS,
    BacktestRunner,
    ComputeUnavailable,
    PhaseFailed,
    RunnerConfig,
)
from wayfinder_paths.jobs.evolution_campaign import (
    _apply_returned_writes,
    _isolated_economic_gate,
    _isolated_full_dev,
    _returned_phase_writes,
    _verified_protected_dataset_root,
    prepare_candidate,
    resolve_candidate_bundle,
    start_campaign,
)
from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.sprite_bundle import extract_archive, sha256
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.synthetic.strategies import CHURNER
from wayfinder_paths.tests.test_jobs_evolution_campaign import (
    _enable_protected_folds,
    _evaluatable_job,
)

STARTED = datetime(2026, 8, 25, 12, tzinfo=UTC)
# Wall-clock timing and cache provenance legitimately differ between two runs.
_VOLATILE = {"checked_at", "sim_wall_seconds", "profile", "run_id"}


class RemoteRunner(BacktestRunner):
    """A remote provider stand-in: a fresh interpreter over the shipped archive."""

    refuse: str | None = None
    lose_worker = False
    shipped: list[list[str]] = []

    def __init__(self, config: RunnerConfig, *, owner_pid: int | None = None):
        super().__init__(config, owner_pid=owner_pid)
        self.records: dict[str, dict[str, Any]] = {}

    def _directory(self, run_id: str) -> Path:
        return self.config.runs_dir / "remote" / run_id

    def submit_archive(self, archive: Path) -> dict[str, Any]:
        if RemoteRunner.refuse:
            raise ComputeUnavailable(RemoteRunner.refuse)
        with tarfile.open(archive) as tar:
            RemoteRunner.shipped.append(sorted(tar.getnames()))
        run_id = str(uuid.uuid4())
        directory = self._directory(run_id)
        directory.mkdir(parents=True)
        if RemoteRunner.lose_worker:
            self.records[run_id] = {
                "id": run_id,
                "provider": "remote",
                "status": "failed",
                "error": "worker lost",
                "result": {},
                "artifacts": {},
            }
            return self.records[run_id]
        summary, artifacts = directory / "summary.json", directory / "artifacts.tgz"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "wayfinder_paths.jobs.sprite_runtime",
                "--bundle",
                str(archive),
                "--root",
                str(directory / "workspace"),
                "--output",
                str(summary),
                "--artifacts",
                str(artifacts),
            ],
            capture_output=True,
            timeout=300,
        )
        self.records[run_id] = {
            "id": run_id,
            "provider": "remote",
            "status": "succeeded" if proc.returncode == 0 else "failed",
            "error": "" if proc.returncode == 0 else proc.stderr.decode()[-2000:],
            "result": {"output": json.loads(summary.read_text())},
            "artifacts": {
                "sha256": sha256(artifacts),
                "size": artifacts.stat().st_size,
            },
        }
        return self.records[run_id]

    def status(self, run_id: str) -> dict[str, Any]:
        return self.records[run_id]

    def cancel(self, run_id: str) -> None:
        self.records[run_id]["status"] = "cancelled"

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        extract_archive(self._directory(run_id) / "artifacts.tgz", destination)
        return self.records[run_id]


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> type[RemoteRunner]:
    monkeypatch.setitem(RUNNERS, "remote", RemoteRunner)
    monkeypatch.setattr(RemoteRunner, "refuse", None)
    monkeypatch.setattr(RemoteRunner, "lose_worker", False)
    monkeypatch.setattr(RemoteRunner, "shipped", [])
    for name in (
        "WAYFINDER_BACKTEST_RUNNER",
        "WAYFINDER_CONFIG_PATH",
        "WAYFINDER_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    return RemoteRunner


def _configure(store: JobStore, **section: Any) -> None:
    (store.repo_root / "config.json").write_text(
        json.dumps(
            {
                "backtest_runner": {
                    "provider": "remote",
                    "runs_dir": str(store.repo_root / "runs"),
                    **section,
                }
            }
        ),
        encoding="utf-8",
    )


def _unconfigure(store: JobStore) -> None:
    (store.repo_root / "config.json").unlink()


def _mutated_candidate(store: JobStore, job_id: str) -> dict[str, Any]:
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="offloaded full-dev probe",
        now=STARTED + timedelta(hours=1),
    )
    script = store.job_dir(job_id) / candidate["bundle"] / "workspace/src/strategy.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\nFULL_DEV_PROBE = True\n",
        encoding="utf-8",
    )
    return candidate


def _journal(store: JobStore, job_id: str, kind: str) -> list[dict[str, Any]]:
    path = store.job_dir(job_id) / "journal.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [row for row in rows if row.get("type") == kind]


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable(item) for key, item in value.items() if key not in _VOLATILE
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def test_offloaded_full_dev_matches_the_local_supervised_phase(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    local = _isolated_full_dev(store, job_id, candidate, tune=False)

    _configure(store)
    offloaded = _isolated_full_dev(store, job_id, candidate, tune=False)

    assert _stable(offloaded) == _stable(local)
    (shipped,) = remote.shipped
    campaign = (
        f".wayfinder/jobs/{job_id}/research/evolution/campaigns/{state['campaign_id']}"
    )
    assert f"{campaign}/manifest.json" in shipped
    assert any(name.startswith(f"{campaign}/source/") for name in shipped)
    assert any(name.startswith(f"{campaign}/dataset/") for name in shipped)
    bundle = f".wayfinder/jobs/{job_id}/{candidate['bundle']}/"
    assert any(name.startswith(bundle) for name in shipped)
    assert all(
        name.startswith((f"{campaign}/", f".wayfinder/jobs/{job_id}/state/"))
        or name == "sprite-request.json"
        for name in shipped
    ), shipped
    assert f".wayfinder/jobs/{job_id}/journal.jsonl" not in shipped
    (row,) = _journal(store, job_id, "evolution_phase_offloaded")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:full_dev_phase"
    assert row["candidate_id"] == candidate["candidate_id"]
    assert row["provider"] == "remote"


def test_offloaded_certification_returns_its_evidence_access(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    _enable_protected_folds(store, job_id)
    state = start_campaign(store, job_id, now=STARTED)
    candidate = prepare_candidate(
        store,
        job_id,
        family="hourly_round_trip",
        summary="exercise the protected certificate remotely",
        now=STARTED + timedelta(hours=1),
    )
    script = store.job_dir(job_id) / candidate["bundle"] / "workspace/src/strategy.py"
    script.write_text(CHURNER, encoding="utf-8")
    ledger = tmp_path / "audit" / job_id / "evidence_access.jsonl"
    _configure(store)

    outcome = _isolated_full_dev(store, job_id, candidate, tune=False)

    assert outcome["evaluation_plan"]["mode"] == "protected_chronological_folds_v1"
    assert len(outcome["dev"]["validation"]["folds"]) == 4
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["op"] for row in rows] == ["evolution_protected_certification"]
    assert rows[0]["campaign_id"] == state["campaign_id"]
    (shipped,) = remote.shipped
    protected = f"audit/{job_id}/evolution/campaigns/{state['campaign_id']}/dataset/"
    assert any(name.startswith(protected) for name in shipped)
    assert f"audit/{job_id}/evidence_access.jsonl" not in shipped
    # The snapshot is shipped, never written back, so it still verifies.
    _verified_protected_dataset_root(store, job_id, str(state["campaign_id"]))


def test_offloaded_economic_gate_matches_the_local_supervised_phase(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    campaign_id = str(state["campaign_id"])
    candidate = _mutated_candidate(store, job_id)
    _configure(store)

    offloaded = _isolated_economic_gate(
        store, job_id, candidate, campaign_id=campaign_id
    )
    _unconfigure(store)
    local = _isolated_economic_gate(store, job_id, candidate, campaign_id=campaign_id)

    assert _stable(offloaded) == _stable(local)
    (shipped,) = remote.shipped
    source = (
        f".wayfinder/jobs/{job_id}/research/evolution/campaigns/{campaign_id}/source/"
    )
    assert any(name.startswith(source) for name in shipped)
    (row,) = _journal(store, job_id, "evolution_phase_offloaded")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:economic_gate_phase"


@pytest.mark.parametrize("failure", ["refused", "unstartable", "lost_worker"])
def test_remote_that_cannot_run_the_phase_falls_back_to_the_local_child(
    tmp_path, remote, monkeypatch, failure
):
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    local = Mock(return_value={"status": "dev_frontier", "local": True})
    monkeypatch.setattr(campaign_module, "run_isolated_phase", local)
    if failure == "refused":
        monkeypatch.setattr(RemoteRunner, "refuse", "worker limit reached")
        _configure(store)
    elif failure == "unstartable":
        _configure(store, sdk_commit="0" * 40)  # a checkpoint on another SDK
    else:
        monkeypatch.setattr(RemoteRunner, "lose_worker", True)
        _configure(store)

    outcome = _isolated_full_dev(store, job_id, candidate, tune=False)

    assert outcome == {"status": "dev_frontier", "local": True}
    local.assert_called_once()
    (row,) = _journal(store, job_id, "evolution_phase_ran_locally")
    assert row["provider"] == "remote" and row["reason"]
    assert not _journal(store, job_id, "evolution_phase_offloaded")


def test_unconfigured_or_local_runner_keeps_the_supervised_child(
    tmp_path, remote, monkeypatch
):
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    local = Mock(return_value={"status": "dev_frontier"})
    monkeypatch.setattr(campaign_module, "run_isolated_phase", local)
    _isolated_full_dev(store, job_id, candidate, tune=False)
    _configure(store, provider="local")
    _isolated_full_dev(store, job_id, candidate, tune=False)
    assert local.call_count == 2 and not remote.shipped
    _configure(store, provider="typo")
    with pytest.raises(TransientInfrastructureError, match="backtest_runner"):
        _isolated_full_dev(store, job_id, candidate, tune=False)


def test_an_unmutated_candidate_fails_the_same_way_remotely(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=STARTED)
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="no effective mutation",
        now=STARTED + timedelta(hours=1),
    )
    with pytest.raises(RuntimeError) as local:
        _isolated_full_dev(store, job_id, candidate, tune=False)
    _configure(store)
    with pytest.raises(RuntimeError) as offloaded:
        _isolated_full_dev(store, job_id, candidate, tune=False)
    assert not isinstance(offloaded.value, TransientInfrastructureError)
    assert str(offloaded.value) == str(local.value)
    assert "no effective mutation" in str(offloaded.value)


@pytest.mark.parametrize(
    "stage,error_type,error,expected",
    [
        ("execute", "ValueError", "window-invariance probe failed", RuntimeError),
        ("execute", "MemoryError", "", TransientInfrastructureError),
        ("execute", "ComputeLockBusy", "busy", TransientInfrastructureError),
        ("execute", "RuntimeError", "worker killed", TransientInfrastructureError),
    ],
)
def test_remote_phase_failures_are_classified_like_the_local_child(
    tmp_path, remote, monkeypatch, stage, error_type, error, expected
):
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    _configure(store)
    failure = PhaseFailed(
        "phase failed",
        run={"status": "failed"},
        error=error,
        error_type=error_type,
        stage=stage,
        outputs_path=None,
    )
    monkeypatch.setattr(campaign_module, "run_phase", Mock(side_effect=failure))
    with pytest.raises(expected) as raised:
        _isolated_full_dev(store, job_id, candidate, tune=False)
    if expected is RuntimeError:
        assert not isinstance(raised.value, TransientInfrastructureError)


def test_entrypoint_outside_the_bundle_is_not_shipped(tmp_path, remote, monkeypatch):
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    root = store.job_dir(job_id) / candidate["bundle"]
    definition = yaml.safe_load((root / "job.yaml").read_text())
    definition["script_loop"]["entrypoint"] = str(tmp_path / "elsewhere/strategy.py")
    (root / "job.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    local = Mock(return_value={"status": "dev_frontier"})
    monkeypatch.setattr(campaign_module, "run_isolated_phase", local)
    _configure(store)
    _isolated_full_dev(store, job_id, candidate, tune=False)
    local.assert_called_once()
    (row,) = _journal(store, job_id, "evolution_phase_ran_locally")
    assert "entrypoint outside its bundle" in row["reason"]
    assert not remote.shipped


def test_phase_writes_return_to_the_source_repository(tmp_path):
    source, job_id = _evaluatable_job(tmp_path / "source")
    state = start_campaign(source, job_id, now=STARTED)
    candidate = _mutated_candidate(source, job_id)
    candidate_root = resolve_candidate_bundle(
        source, job_id, candidate, campaign_id=str(state["campaign_id"])
    )
    copy_root = tmp_path / "copy"
    for relative in (
        candidate_root.relative_to(source.repo_root),
        Path(".wayfinder/jobs", job_id, "journal.jsonl"),
    ):
        target = copy_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if (source.repo_root / relative).is_dir():
            shutil.copytree(source.repo_root / relative, target)
        else:
            target.write_bytes((source.repo_root / relative).read_bytes())
    copy = JobStore(repo_root=copy_root)
    outputs = copy_root / "outputs"
    args = {
        "job_id": job_id,
        "candidate": candidate,
        "campaign_id": str(state["campaign_id"]),
        "source_root": str(source.repo_root),
    }
    journal_before = (source.job_dir(job_id) / "journal.jsonl").read_bytes()

    with _returned_phase_writes(copy, outputs, args):
        tuned = copy.job_dir(job_id) / candidate["bundle"] / "job.yaml"
        tuned.write_text(tuned.read_text() + "# tuned\n", encoding="utf-8")
        copy.append_journal(job_id, {"type": "probe", "path": f"{copy_root}/x"})
        campaign_module.record_evidence_access(copy_root, job_id, "probe_access")

    _apply_returned_writes(source, job_id, outputs, candidate_root)

    assert (candidate_root / "job.yaml").read_text().endswith("# tuned\n")
    journal = (source.job_dir(job_id) / "journal.jsonl").read_bytes()
    assert journal.startswith(journal_before)
    (probe,) = _journal(source, job_id, "probe")
    assert probe["path"] == f"{source.repo_root}/x"
    ledger = source.repo_root / "audit" / job_id / "evidence_access.jsonl"
    assert json.loads(ledger.read_text())["op"] == "probe_access"
    (outputs / "files" / "unexpected.txt").parent.mkdir(parents=True, exist_ok=True)
    (outputs / "files" / "unexpected.txt").write_text("x")
    with pytest.raises(TransientInfrastructureError, match="unexpected"):
        _apply_returned_writes(source, job_id, outputs, candidate_root)

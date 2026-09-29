from __future__ import annotations

import json
import math
import random
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
    _prepare_campaign_candidates,
)

STARTED = datetime(2026, 8, 25, 12, tzinfo=UTC)
# Wall-clock timing and cache provenance legitimately differ between two runs.
_VOLATILE = {"checked_at", "sim_wall_seconds", "wall_seconds", "profile", "run_id"}


class RemoteRunner(BacktestRunner):
    """A remote provider stand-in: a fresh interpreter over the shipped
    archive, extracted over its base."""

    refuse: str | None = None
    lose_worker = False
    released: list[str] = []
    purposes: list[str] = []
    shipped: list[list[str]] = []
    bases: list[list[str]] = []
    destination = {
        "runner": "remote",
        "lease_id": "lease-1",
        "worker_host": "w.example",
    }

    def __init__(self, config: RunnerConfig, *, owner_pid: int | None = None):
        super().__init__(config, owner_pid=owner_pid)
        self.records: dict[str, dict[str, Any]] = {}

    def _directory(self, run_id: str) -> Path:
        return self.config.runs_dir / "remote" / run_id

    def submit_archive(
        self, archive: Path, *, base: Path | None = None, purpose: str = ""
    ) -> dict[str, Any]:
        if RemoteRunner.refuse:
            raise ComputeUnavailable(RemoteRunner.refuse)
        RemoteRunner.purposes.append(purpose)
        with tarfile.open(archive) as tar:
            RemoteRunner.shipped.append(sorted(tar.getnames()))
        if base is not None:
            with tarfile.open(base) as tar:
                RemoteRunner.bases.append(sorted(tar.getnames()))
        run_id = str(uuid.uuid4())
        directory = self._directory(run_id)
        directory.mkdir(parents=True)
        if RemoteRunner.lose_worker:
            self.records[run_id] = {
                "id": run_id,
                "provider": "remote",
                "destination": RemoteRunner.destination,
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
                *(["--base", str(base)] if base is not None else []),
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
            "destination": RemoteRunner.destination,
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

    def release_idle_leases(self, *, reason: str) -> list[str]:
        RemoteRunner.released.append(reason)
        return ["lease-1"]


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> type[RemoteRunner]:
    monkeypatch.setitem(RUNNERS, "remote", RemoteRunner)
    monkeypatch.setattr(RemoteRunner, "refuse", None)
    monkeypatch.setattr(RemoteRunner, "lose_worker", False)
    monkeypatch.setattr(RemoteRunner, "released", [])
    monkeypatch.setattr(RemoteRunner, "purposes", [])
    monkeypatch.setattr(RemoteRunner, "shipped", [])
    monkeypatch.setattr(RemoteRunner, "bases", [])
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
    (base,) = remote.bases
    campaign = (
        f".wayfinder/jobs/{job_id}/research/evolution/campaigns/{state['campaign_id']}"
    )
    assert f"{campaign}/manifest.json" in shipped
    assert any(name.startswith(f"{campaign}/source/") for name in shipped)
    # The campaign dataset is the base every phase of the campaign shares.
    assert base and all(name.startswith(f"{campaign}/dataset/") for name in base)
    assert not any(name.startswith(f"{campaign}/dataset/") for name in shipped)
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
    # The journal says why the phase left this node and where it ran; the runner
    # got the label the lease audit log records.
    assert row["purpose"] == "evolution:full_dev_phase"
    assert "backtest_runner.provider 'remote'" in row["reason"]
    assert row["destination"] == RemoteRunner.destination
    assert remote.purposes[-1] == "evolution:full_dev_phase"


def test_offloaded_screen_matches_the_local_screen(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    campaign_id = str(state["campaign_id"])
    candidate = _mutated_candidate(store, job_id)
    local = campaign_module._screen(store, job_id, candidate, campaign_id=campaign_id)
    reference = campaign_module._reference_result_path(
        store, job_id, candidate, campaign_id
    )
    cached = reference.read_bytes() if reference.exists() else None
    reference.unlink(missing_ok=True)

    _configure(store)
    offloaded = campaign_module._screen(
        store, job_id, candidate, campaign_id=campaign_id
    )

    assert _stable(offloaded) == _stable(local)
    # A reference result the remote computed is cached back in the source.
    assert (reference.read_bytes() if reference.exists() else None) == cached
    (shipped,) = remote.shipped
    campaign = f".wayfinder/jobs/{job_id}/research/evolution/campaigns/{campaign_id}"
    pack = store.job_dir(job_id) / campaign_module.CAMPAIGN_ROOT / campaign_id
    if (pack / campaign_module.DIAGNOSTIC_PACK).exists():
        assert f"{campaign}/{campaign_module.DIAGNOSTIC_PACK}" in shipped
    if candidate.get("reference_bundle"):
        reference_bundle = f".wayfinder/jobs/{job_id}/{candidate['reference_bundle']}/"
        assert any(name.startswith(reference_bundle) for name in shipped)
    (row,) = _journal(store, job_id, "evolution_phase_offloaded")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:screen_phase"
    assert row["wall_seconds"] >= 0 and row["node_cpu_seconds"] >= 0


def _optuna_candidate(
    store: JobStore, job_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    """A parameter candidate with a typed search space, and a small Optuna budget
    with no timeout, so a remote search is trial-for-trial the local one."""
    manifest_path = store.job_dir(job_id) / state["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy"].update(
        {
            "inner_optuna_trials": 6,
            "inner_optuna_timeout_seconds": 0,
            "inner_optuna_preview_trials": 4,
            "inner_optuna_preview_timeout_seconds": 0,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    parameter = _prepare_campaign_candidates(store, job_id, STARTED)[-1]
    assert parameter["mutation_kind"] == "parameter"
    (store.job_dir(job_id) / parameter["bundle"] / "search_space.json").write_text(
        json.dumps({"lookback": {"type": "int", "low": 12, "high": 96}}),
        encoding="utf-8",
    )
    return parameter


def test_offloaded_optuna_tuning_matches_the_local_search(tmp_path, remote):
    pytest.importorskip("optuna")
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    parameter = _optuna_candidate(store, job_id, state)
    definition = store.job_dir(job_id) / parameter["bundle"] / "job.yaml"
    original = definition.read_bytes()
    local = _isolated_full_dev(store, job_id, parameter, tune=True)
    tuned = definition.read_bytes()
    definition.write_bytes(original)  # The remote run starts from the same candidate.

    _configure(store)
    offloaded = _isolated_full_dev(store, job_id, parameter, tune=True)

    assert local["tuning"]["trials"] == 6
    assert _stable(offloaded) == _stable(local)
    # The re-tuned definition comes back to the source repository.
    assert definition.read_bytes() == tuned
    (row,) = _journal(store, job_id, "evolution_phase_offloaded")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:full_dev_phase"


def test_offloaded_screen_runs_the_same_optuna_preview(tmp_path, remote):
    pytest.importorskip("optuna")
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    campaign_id = str(state["campaign_id"])
    parameter = _optuna_candidate(store, job_id, state)
    # A strategy that closes trades, so the quick screen reaches the preview.
    script = store.job_dir(job_id) / parameter["bundle"] / "workspace/src/strategy.py"
    script.write_text(CHURNER, encoding="utf-8")
    local = campaign_module._screen(store, job_id, parameter, campaign_id=campaign_id)

    _configure(store)
    offloaded = campaign_module._screen(
        store, job_id, parameter, campaign_id=campaign_id
    )

    assert local["tuning_preview"]["trials"] == 4
    assert _stable(offloaded) == _stable(local)
    (row,) = _journal(store, job_id, "evolution_phase_offloaded")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:screen_phase"


def _with_history(store: JobStore, job_id: str, bars: int = 1500) -> None:
    """Enough seeded random-walk history for campaign start's scans to find rows."""
    rng = random.Random(3)
    price, rows = 10.0, []
    start = datetime(2026, 6, 1, tzinfo=UTC)
    for index in range(bars):
        price *= math.exp(rng.gauss(0.0, 0.01))
        rows.append(
            {
                "timestamp": (start + timedelta(hours=index)).isoformat(),
                "symbol": "IMX",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100.0 + index % 7,
            }
        )
    path = store.job_dir(job_id) / "results" / "backtest" / "input_bars.json"
    path.write_text(
        json.dumps({"metadata": {"days": bars // 24}, "bars": rows}), encoding="utf-8"
    )


def test_offloaded_campaign_start_matches_the_local_start(
    tmp_path, remote, monkeypatch
):
    local_store, local_job = _evaluatable_job(tmp_path / "local")
    remote_store, remote_job = _evaluatable_job(tmp_path / "remote")
    for store, job_id in ((local_store, local_job), (remote_store, remote_job)):
        _with_history(store, job_id)
    captured: dict[str, Any] = {}
    start_scans, offloaded_phase = (
        campaign_module._start_scans,
        campaign_module._offloaded_phase,
    )

    def local_scans(*args, **kwargs):
        captured["local"] = start_scans(*args, **kwargs)
        return captured["local"]

    def remote_phase(*args, **kwargs):
        result = offloaded_phase(*args, **kwargs)
        if result is not None:
            captured.setdefault("remote", result)
        return result

    monkeypatch.setattr(campaign_module, "_start_scans", local_scans)
    monkeypatch.setattr(campaign_module, "_offloaded_phase", remote_phase)
    start_campaign(local_store, local_job, now=STARTED)
    _configure(remote_store)
    state = start_campaign(remote_store, remote_job, now=STARTED)

    assert _stable(captured["remote"]) == _stable(captured["local"])
    assert captured["local"]["policy_scan"]  # The scans had data to work on.
    (row,) = _journal(remote_store, remote_job, "evolution_phase_offloaded")
    assert (
        row["phase"] == "wayfinder_paths.jobs.evolution_campaign:campaign_scans_phase"
    )
    assert row["candidate_id"] is None
    # The campaign's next phase reuses the start's base: one lease, one upload.
    candidate = _mutated_candidate(remote_store, remote_job)
    campaign_module._screen(
        remote_store, remote_job, candidate, campaign_id=str(state["campaign_id"])
    )
    assert len(remote.bases) == 2 and remote.bases[0] == remote.bases[1]


def test_a_completed_campaign_ends_its_lease(tmp_path, remote):
    store, job_id = _evaluatable_job(tmp_path)
    # No runner: nothing to end.
    campaign_module._end_campaign_leases(store, job_id, "c1")
    _configure(store, provider="local")
    campaign_module._end_campaign_leases(store, job_id, "c1")
    assert remote.released == []
    assert not _journal(store, job_id, "evolution_leases_released")
    _configure(store)
    campaign_module._end_campaign_leases(store, job_id, "c1")
    assert remote.released == ["evolution campaign c1 completed"]
    (row,) = _journal(store, job_id, "evolution_leases_released")
    assert (row["campaign_id"], row["provider"], row["lease_ids"]) == (
        "c1",
        "remote",
        ["lease-1"],
    )


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
    (base,) = remote.bases
    protected = f"audit/{job_id}/evolution/campaigns/{state['campaign_id']}/dataset/"
    assert any(name.startswith(protected) for name in base)
    assert not any(name.startswith(protected) for name in shipped)
    assert f"audit/{job_id}/evidence_access.jsonl" not in shipped + base
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


@pytest.mark.parametrize("failure", ["refused", "unstartable", "lost_worker"])
def test_a_screen_the_remote_cannot_run_is_computed_locally(
    tmp_path, remote, monkeypatch, failure
):
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    # The same function the remote phase runs, computed here instead.
    local = Mock(return_value={"status": "quick_complete", "local": True})
    monkeypatch.setattr(campaign_module, "_evaluate_candidate", local)
    if failure == "refused":
        monkeypatch.setattr(RemoteRunner, "refuse", "worker limit reached")
        _configure(store)
    elif failure == "unstartable":
        _configure(store, sdk_commit="0" * 40)  # a runtime on another SDK
    else:
        monkeypatch.setattr(RemoteRunner, "lose_worker", True)
        _configure(store)

    outcome = campaign_module._screen(
        store, job_id, candidate, campaign_id=str(state["campaign_id"])
    )

    assert outcome == {"status": "quick_complete", "local": True}
    local.assert_called_once()
    (row,) = _journal(store, job_id, "evolution_phase_ran_locally")
    assert row["phase"] == "wayfinder_paths.jobs.evolution_campaign:screen_phase"
    assert row["purpose"] == "evolution:screen_phase"
    assert row["provider"] == "remote" and row["reason"]
    assert not _journal(store, job_id, "evolution_phase_offloaded")


def test_screens_stay_local_without_a_remote_runner(tmp_path, remote, monkeypatch):
    store, job_id = _evaluatable_job(tmp_path)
    state = start_campaign(store, job_id, now=STARTED)
    candidate = _mutated_candidate(store, job_id)
    local = Mock(return_value={"status": "quick_complete"})
    monkeypatch.setattr(campaign_module, "_evaluate_candidate", local)
    campaign_id = str(state["campaign_id"])
    campaign_module._screen(store, job_id, candidate, campaign_id=campaign_id)
    _configure(store, provider="local")
    campaign_module._screen(store, job_id, candidate, campaign_id=campaign_id)
    assert local.call_count == 2 and not remote.shipped
    assert not _journal(store, job_id, "evolution_phase_ran_locally")


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

    reference = campaign_module._reference_result_path(
        source, job_id, candidate, str(state["campaign_id"])
    )
    reference.unlink(missing_ok=True)
    with _returned_phase_writes(copy, outputs, args):
        tuned = copy.job_dir(job_id) / candidate["bundle"] / "job.yaml"
        tuned.write_text(tuned.read_text() + "# tuned\n", encoding="utf-8")
        copy.append_journal(job_id, {"type": "probe", "path": f"{copy_root}/x"})
        campaign_module.record_evidence_access(copy_root, job_id, "probe_access")
        # Screening caches the candidate's reference result.
        cached = copy_root / reference.relative_to(source.repo_root)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text('{"revision": "r", "slices": {}}', encoding="utf-8")

    returnable = [candidate_root / "job.yaml", reference]
    _apply_returned_writes(source, job_id, outputs, returnable)

    assert (candidate_root / "job.yaml").read_text().endswith("# tuned\n")
    assert json.loads(reference.read_text()) == {"revision": "r", "slices": {}}
    journal = (source.job_dir(job_id) / "journal.jsonl").read_bytes()
    assert journal.startswith(journal_before)
    (probe,) = _journal(source, job_id, "probe")
    assert probe["path"] == f"{source.repo_root}/x"
    ledger = source.repo_root / "audit" / job_id / "evidence_access.jsonl"
    assert json.loads(ledger.read_text())["op"] == "probe_access"
    (outputs / "files" / "unexpected.txt").parent.mkdir(parents=True, exist_ok=True)
    (outputs / "files" / "unexpected.txt").write_text("x")
    with pytest.raises(TransientInfrastructureError, match="unexpected"):
        _apply_returned_writes(source, job_id, outputs, returnable)

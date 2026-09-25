from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from wayfinder_paths.jobs.backtest_runner import (
    LocalRunner,
    RunnerConfig,
    SpritesRunner,
    _prune_receipts,
    create_runner,
    load_runner_config,
)
from wayfinder_paths.jobs.execution.op_process import (
    process_identity_fields,
    recorded_process_alive,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.sprite_bundle import (
    apply_job_outputs,
    extract_archive,
    pack_job,
    sha256,
)
from wayfinder_paths.jobs.sprite_client import SpriteBacktestsClient
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def test_configuration_defaults_environment_precedence_and_no_secret_repr(tmp_path):
    default = load_runner_config(repo_root=tmp_path, config={}, environ={})
    assert default.provider == "local"
    # Unlimited like in-place execution; bounded disk via retention.
    assert default.timeout_seconds is None and default.retain_runs == 10
    document = {
        "backtest_runner": {
            "provider": "sprites",
            "runs_dir": "runs",
            "sprites": {
                "backend": "https://backend.example",
                "app_name": "shell",
                "preset": "pinned",
            },
        },
        "system": {"api_key": "test-secret"},
    }
    config = load_runner_config(repo_root=tmp_path, config=document, environ={})
    assert config.provider == "sprites" and config.configured
    assert config.preset == "pinned" and config.runs_dir == tmp_path.resolve() / "runs"
    assert "test-secret" not in repr(config)
    local = load_runner_config(
        repo_root=tmp_path,
        config=document,
        environ={
            "WAYFINDER_BACKTEST_RUNNER": "local",
            "WAYFINDER_BACKTEST_TIMEOUT_SECONDS": "20",
        },
    )
    assert local.provider == "local" and local.timeout_seconds == 20
    assert isinstance(create_runner(config=local), LocalRunner)


@pytest.mark.parametrize(
    "section",
    [
        None,
        {"provider": "typo"},
        {"providre": "sprites"},
        {"provider": "sprites"},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 21601},
        {"timeout_seconds": 1.5},
        {"retain_runs": 0},
        {"extra_paths": "wrong"},
        {"sdk_commit": "branch"},
    ],
)
def test_invalid_configuration_does_not_fall_back_to_local(tmp_path, section):
    with pytest.raises(ValueError):
        load_runner_config(
            repo_root=tmp_path, config={"backtest_runner": section}, environ={}
        )


def test_corrupt_config_file_fails_and_explicit_config_path_is_respected(tmp_path):
    (tmp_path / "config.json").write_text("not JSON")
    with pytest.raises(ValueError):
        load_runner_config(repo_root=tmp_path, environ={})
    (tmp_path / "compute.json").write_text('{"backtest_runner":{"provider":"local"}}')
    config = load_runner_config(
        repo_root=tmp_path, environ={"WAYFINDER_CONFIG_PATH": "compute.json"}
    )
    assert config.configured


def make_script(tmp_path, body):
    store, job_id, root = _make_job(tmp_path / "source")
    script = root / "workspace/src/compute.py"
    script.write_text(body)
    return store, job_id, {"path": str(script.relative_to(store.repo_root))}


@pytest.mark.parametrize("provider", ["local", "sprites"])
def test_same_submission_and_artifact_contract_across_providers(
    tmp_path, provider, monkeypatch
):
    store, job_id, options = make_script(
        tmp_path,
        "import json\nopen('full-trace.bin','wb').write(b'trace' * 300000)\njson.dump({'pnl':12.5,'undefined':float('nan')},open('result.json','w'))\n",
    )
    original = (store.job_dir(job_id) / "job.yaml").read_bytes()
    config = RunnerConfig(
        provider=provider, runs_dir=tmp_path / "runs", timeout_seconds=20
    )
    if provider == "local":
        runner = LocalRunner(config)
    else:
        # Exercise the real HTTP client and portable runtime while substituting
        # the provider/Django control plane. No external services or credentials.
        remote = tmp_path / "remote"
        remote.mkdir()
        workspace = remote / "input.tar.gz"
        archive = remote / "artifacts.tar.gz"
        summary = remote / "summary.json"
        state = {
            "id": "remote-run",
            "status": "ready",
            "result": {},
            "artifacts": {},
            "error": "",
        }
        parts = []

        def api(request):
            if request.url.path.endswith("/sprite-backtests/"):
                return httpx.Response(
                    201,
                    json={
                        **state,
                        "auth_token": "scoped",
                        "runtime": {"capabilities": ["sdk-workspace-v1"]},
                    },
                )
            if request.method == "PUT":
                assert request.headers["Authorization"] == "Bearer scoped"
                assert (
                    hashlib.sha256(request.content).hexdigest()
                    == request.headers["X-Content-SHA256"]
                )
                parts.append(request.content)
                workspace.write_bytes(b"".join(parts))
                return httpx.Response(
                    200,
                    json={
                        "sha256": sha256(workspace),
                        "size": workspace.stat().st_size,
                    },
                )
            if request.url.path.endswith("/jobs/"):
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "wayfinder_paths.jobs.sprite_runtime",
                        "--bundle",
                        str(workspace),
                        "--root",
                        str(remote / "workspace"),
                        "--output",
                        str(summary),
                        "--artifacts",
                        str(archive),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                assert proc.returncode == 0, proc.stderr
                state.update(
                    status="succeeded",
                    result={"output": json.loads(summary.read_text())},
                    artifacts={
                        "sha256": sha256(archive),
                        "size": archive.stat().st_size,
                    },
                )
                return httpx.Response(202, json={"status": "queued"})
            assert request.headers["X-API-Key"] == "owner-key"
            if request.url.path.endswith("/artifacts/"):
                return httpx.Response(200, content=archive.read_bytes())
            return httpx.Response(200, json=state)

        http = httpx.Client(
            transport=httpx.MockTransport(api), base_url="https://backend.example"
        )
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "owner-key", client=http
        )
        runner = SpritesRunner(config, client=client)
    with runner:
        submitted = runner.submit(store, job_id, op="script", options=options)
        result = runner.wait(submitted["id"], poll_interval=0.05)
        assert result["provider"] == provider and result["status"] == "succeeded", (
            result
        )
        assert result["result"]["output"]["summary"] == {"pnl": 12.5, "undefined": None}
        destination = tmp_path / "collected"
        collected = runner.collect(submitted["id"], destination)
        assert collected["artifacts"] == result["artifacts"]
        assert (destination / "full-trace.bin").stat().st_size == 1500000
        assert "NaN" in (destination / "operation-result.json").read_text()
        with pytest.raises(FileExistsError):
            runner.collect(submitted["id"], destination)
        assert (store.job_dir(job_id) / "job.yaml").read_bytes() == original
    if provider == "sprites":
        assert not http.is_closed  # Injected transports are still caller-owned.
        http.close()


def test_local_native_process_grid_and_checksum_validation(tmp_path):
    store, job_id, _ = _make_job(tmp_path / "source")
    runner = LocalRunner(
        RunnerConfig(provider="local", runs_dir=tmp_path / "runs", timeout_seconds=60)
    )
    submitted = runner.submit(
        store,
        job_id,
        op="experiments",
        options={
            "grid": {"initial_capital": [1000, 2000]},
            "parallel": "process",
            "workers": 2,
        },
    )
    result = runner.wait(submitted["id"], poll_interval=0.05)
    assert result["status"] == "succeeded", result
    run_dir = runner.config.runs_dir / submitted["id"]
    assert not (run_dir / "workspace").exists()
    assert not (run_dir / "workspace.tar.gz").exists()
    runner.collect(submitted["id"], tmp_path / "collected")
    full = json.loads((tmp_path / "collected/operation-result.json").read_text())
    assert len(full["backtest"]["result"]["runs"]) == 2
    (runner.config.runs_dir / submitted["id"] / "artifacts.tar.gz").write_bytes(
        b"corrupted"
    )
    with pytest.raises(ValueError, match="checksum"):
        runner.collect(submitted["id"], tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("ending", ["failure", "timeout", "cancel"])
def test_local_failure_timeout_and_cancel_preserve_partial_results(tmp_path, ending):
    body = "import time\nopen('partial.txt','w').write('useful')\n"
    body += (
        "raise RuntimeError('intentional failure')\n"
        if ending == "failure"
        else "time.sleep(30)\n"
    )
    store, job_id, options = make_script(tmp_path, body)
    config = RunnerConfig(
        provider="local",
        runs_dir=tmp_path / "runs",
        timeout_seconds=3 if ending == "timeout" else 20,
    )
    runner = LocalRunner(config)
    submitted = runner.submit(store, job_id, op="script", options=options)
    if ending == "cancel":
        for _ in range(200):
            if (config.runs_dir / submitted["id"] / "workspace/partial.txt").exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail("Worker did not start the submitted script")
        runner.cancel(submitted["id"])
    result = runner.wait(submitted["id"], poll_interval=0.05)
    assert (
        result["status"]
        == {"failure": "failed", "timeout": "timed_out", "cancel": "cancelled"}[ending]
    ), result
    runner.collect(submitted["id"], tmp_path / "collected")
    assert (tmp_path / "collected/partial.txt").read_text() == "useful"
    runner.cancel(submitted["id"])
    assert runner.status(submitted["id"])["status"] == result["status"]


def test_submit_only_survives_cli_exit_and_does_not_inherit_credentials(tmp_path):
    store, job_id, options = make_script(
        tmp_path,
        "import os,json\nfrom wayfinder_paths.core.config import CONFIG\nassert not os.environ.get('WAYFINDER_API_KEY')\nassert not os.environ.get('PRIVATE_TEST_TOKEN')\nassert not CONFIG['system'].get('api_key')\njson.dump({'ok':True},open('result.json','w'))\n",
    )
    config = {
        "system": {"api_key": "must-stay-on-parent"},
        "backtest_runner": {"provider": "local", "runs_dir": str(tmp_path / "runs")},
    }
    (store.repo_root / "config.json").write_text(json.dumps(config))
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(options))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "wayfinder_paths.jobs.backtest_cli",
            "--repo",
            str(store.repo_root),
            "--job-id",
            job_id,
            "--op",
            "script",
            "--options",
            str(options_path),
            "--submit-only",
        ],
        env={
            **os.environ,
            "WAYFINDER_API_KEY": "must-stay-on-parent",
            "PRIVATE_TEST_TOKEN": "must-stay-on-parent",
            "WAYFINDER_CONFIG_PATH": str(store.repo_root / "config.json"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    submitted = json.loads(proc.stdout)
    assert "must-stay-on-parent" not in proc.stdout
    with create_runner(
        config=load_runner_config(repo_root=store.repo_root, config=config, environ={})
    ) as reopened:
        result = reopened.wait(submitted["id"], poll_interval=0.05)
        assert result["status"] == "succeeded", result
        reopened.collect(submitted["id"], tmp_path / "collected")


def test_agent_entrypoint_selects_runner_only_for_portable_ops(monkeypatch, tmp_path):
    from wayfinder_paths.jobs import backtest_runner
    from wayfinder_paths.jobs.execution import op_runner

    native = Mock(return_value={"native": True})
    configured = Mock(return_value={"provider": "sprites", "status": "succeeded"})
    ledger = Mock()
    config = RunnerConfig(provider="sprites", runs_dir=tmp_path, configured=True)
    monkeypatch.setattr(op_runner, "_run", native)
    monkeypatch.setattr(op_runner, "_record_evidence_access", ledger)
    monkeypatch.setattr(backtest_runner, "load_runner_config", lambda: config)
    monkeypatch.setattr(backtest_runner, "run_configured_operation", configured)
    assert (
        op_runner._run_entrypoint("backtest_job", {"job_id": "job"})["provider"]
        == "sprites"
    )
    configured.assert_called_once_with("backtest_job", {"job_id": "job"}, config=config)
    ledger.assert_called_once_with("backtest_job", {"job_id": "job"})
    assert op_runner._run_entrypoint("fetch_dataset", {}) == {"native": True}
    monkeypatch.setattr(
        backtest_runner,
        "load_runner_config",
        lambda: replace(config, provider="local", configured=False),
    )
    assert op_runner._run_entrypoint("backtest_job", {}) == {"native": True}


def test_local_run_ids_cannot_escape_storage(tmp_path):
    runner = LocalRunner(RunnerConfig(provider="local", runs_dir=tmp_path))
    with pytest.raises(ValueError):
        runner.status("../../elsewhere")


def _configured_env(repo: Path, runs: Path) -> dict[str, str]:
    (repo / "config.json").write_text(
        json.dumps({"backtest_runner": {"provider": "local", "runs_dir": str(runs)}})
    )
    return {**os.environ, "WAYFINDER_CONFIG_PATH": str(repo / "config.json")}


def _running_run(runs: Path) -> Path:
    for _ in range(400):
        for status in runs.glob("*/status.json"):
            supervisor = status.parent / "supervisor.json"
            if (
                json.loads(status.read_text())["status"] == "running"
                and supervisor.exists()
                and "child" in json.loads(supervisor.read_text())
            ):
                return status.parent
        time.sleep(0.05)
    pytest.fail("Local run never started its computation")


def test_existing_agent_entrypoint_applies_configured_local_results(tmp_path):
    store, job_id, root = _make_job(tmp_path / "source")
    # An absolute entrypoint is rebased inside the copy, which changes the
    # copy's revision hash; applied stamps must still name the job's revision.
    job_yaml = root / "job.yaml"
    job = yaml.safe_load(job_yaml.read_text())
    job["script_loop"]["entrypoint"] = str(root / "workspace/src/strategy.py")
    job_yaml.write_text(yaml.safe_dump(job, sort_keys=False))
    original = job_yaml.read_bytes()
    proc = subprocess.run(
        [sys.executable, "-m", "wayfinder_paths.jobs.execution.op_runner"],
        input=json.dumps({"op": "backtest_job", "kwargs": {"job_id": job_id}}),
        cwd=store.repo_root,
        env=_configured_env(store.repo_root, tmp_path / "runs"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["provider"] == "local" and result["status"] == "succeeded"
    artifacts = Path(result["artifacts_path"])
    runtime = json.loads((artifacts / "sprite-runtime.json").read_text())
    assert runtime["workspace_revision"] != compute_workspace_revision(root)
    assert (
        f".wayfinder/jobs/{job_id}/results/backtest/latest.json"
        in result["applied"]["updated"]
    )
    for name in result["applied"]["updated"]:
        assert runtime["workspace_root"] not in (store.repo_root / name).read_text()
    assert job_yaml.read_bytes() == original
    stamp = json.loads((root / "results/backtest/latest.json").read_text())
    assert stamp["revision"] == compute_workspace_revision(root)
    reasons = evaluate_live_gate(job_id, store=store)["reasons"]
    assert not [
        reason
        for reason in reasons
        if "revision" in reason or "no backtest" in reason or "no validation" in reason
    ], reasons
    assert (store.repo_root / "audit" / job_id / "evidence_access.jsonl").exists()


def test_apply_keeps_concurrent_job_changes_and_merges_ledgers(tmp_path):
    store, job_id, root = _make_job(tmp_path / "source")
    for relative, content in {
        "results/backtest/latest.json": '{"run": "before"}',
        "reports/validation/latest.json": '{"run": "before"}',
        "state/features.jsonl": '{"row": 1}\n',
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(content)
    pack_job(store, job_id, tmp_path / "bundle.tar.gz")
    artifacts = tmp_path / "artifacts"
    extract_archive(tmp_path / "bundle.tar.gz", artifacts)
    copied = artifacts / ".wayfinder/jobs" / job_id
    remote = "/remote/workspace"
    (artifacts / "sprite-runtime.json").write_text(
        json.dumps({"workspace_root": remote})
    )
    # The run's outputs, plus strategy edits that must never be applied.
    (copied / "results/backtest/latest.json").write_text('{"run": "remote"}')
    (copied / "reports/validation/latest.json").write_text('{"run": "remote"}')
    (copied / "reports/preflight").mkdir(parents=True)
    (copied / "reports/preflight/latest.json").write_text(
        json.dumps({"trace": f"{remote}/.wayfinder/jobs/{job_id}/trace.json"})
    )
    with (copied / "state/features.jsonl").open("a") as stream:
        stream.write('{"row": "remote"}\n')
    (copied / "workspace/src/strategy.py").write_text("tampered = True\n")
    (copied / "job.yaml").write_text("tampered: true\n")
    # Meanwhile the job itself moved on.
    (root / "reports/validation/latest.json").write_text('{"run": "newer"}')
    with (root / "state/features.jsonl").open("a") as stream:
        stream.write('{"row": "live"}\n')
    strategy = (root / "workspace/src/strategy.py").read_bytes()
    definition = (root / "job.yaml").read_bytes()

    applied = apply_job_outputs(store, artifacts)

    prefix = f".wayfinder/jobs/{job_id}/"
    assert applied == {
        "updated": [
            prefix + "reports/preflight/latest.json",
            prefix + "results/backtest/latest.json",
        ],
        "appended": [prefix + "state/features.jsonl"],
        "skipped": [prefix + "reports/validation/latest.json"],
    }
    assert json.loads((root / "results/backtest/latest.json").read_text()) == {
        "run": "remote"
    }
    assert json.loads((root / "reports/validation/latest.json").read_text()) == {
        "run": "newer"
    }
    assert json.loads((root / "reports/preflight/latest.json").read_text()) == {
        "trace": str(root / "trace.json")
    }
    assert (root / "state/features.jsonl").read_text().splitlines() == [
        '{"row": 1}',
        '{"row": "live"}',
        '{"row": "remote"}',
    ]
    assert (root / "workspace/src/strategy.py").read_bytes() == strategy
    assert (root / "job.yaml").read_bytes() == definition
    with pytest.raises(ValueError, match="not packed from this repository"):
        apply_job_outputs(JobStore(repo_root=tmp_path / "elsewhere"), artifacts)


def test_cli_apply_writes_results_into_the_job(tmp_path):
    store, job_id, root = _make_job(tmp_path / "source")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "wayfinder_paths.jobs.backtest_cli",
            "--repo",
            str(store.repo_root),
            "--job-id",
            job_id,
            "--output",
            str(tmp_path / "collected"),
            "--apply",
        ],
        env=_configured_env(store.repo_root, tmp_path / "runs"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.split("\n", 1)[1])
    assert (
        f".wayfinder/jobs/{job_id}/results/backtest/latest.json"
        in result["applied"]["updated"]
    )
    assert (root / "results/backtest/latest.json").exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_terminated_agent_operation_cancels_its_local_run(tmp_path, sig):
    store, job_id, options = make_script(tmp_path, "import time\ntime.sleep(30)\n")
    runs = tmp_path / "runs"
    owner = subprocess.Popen(
        [sys.executable, "-m", "wayfinder_paths.jobs.execution.op_runner"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=store.repo_root,
        env=_configured_env(store.repo_root, runs),
        start_new_session=True,
    )
    assert owner.stdin is not None
    owner.stdin.write(
        json.dumps({"op": "script", "kwargs": {"job_id": job_id, **options}}).encode()
    )
    owner.stdin.close()
    directory = _running_run(runs)
    # SIGKILL of the op's process group is what the watchdog reapers send.
    os.killpg(owner.pid, sig)
    owner.wait(timeout=10)
    runner = LocalRunner(RunnerConfig(provider="local", runs_dir=runs))
    result = runner.wait(directory.name, poll_interval=0.05)
    assert result["status"] == "cancelled", result
    if sig == signal.SIGKILL:
        assert "Submitting process exited" in result["error"]
    child = json.loads((directory / "supervisor.json").read_text())["child"]
    assert not recorded_process_alive(child)


def test_dead_worker_does_not_leave_its_computation_running(tmp_path):
    store, job_id, options = make_script(tmp_path, "import time\ntime.sleep(30)\n")
    runner = LocalRunner(RunnerConfig(provider="local", runs_dir=tmp_path / "runs"))
    submitted = runner.submit(store, job_id, op="script", options=options)
    supervisor = json.loads(
        (_running_run(runner.config.runs_dir) / "supervisor.json").read_text()
    )
    os.kill(supervisor["pid"], signal.SIGKILL)
    result = runner.wait(submitted["id"], poll_interval=0.05)
    assert result["status"] == "failed" and "without a completion" in result["error"]
    for _ in range(100):
        if not recorded_process_alive(supervisor["child"]):
            break
        time.sleep(0.05)
    else:
        pytest.fail("Compute child outlived its supervisor")


def test_retention_prunes_finished_runs_and_receipts(tmp_path):
    store, job_id, options = make_script(tmp_path, "print('done')\n")
    runner = LocalRunner(
        RunnerConfig(provider="local", runs_dir=tmp_path / "runs", retain_runs=2)
    )
    finished = []
    for _ in range(3):
        run = runner.submit(store, job_id, op="script", options=options)
        assert runner.wait(run["id"], poll_interval=0.05)["status"] == "succeeded"
        finished.append(run["id"])
    latest = runner.submit(store, job_id, op="script", options=options)
    assert {path.name for path in runner.config.runs_dir.iterdir()} == {
        *finished[1:],
        latest["id"],
    }
    runner.wait(latest["id"], poll_interval=0.05)

    receipts = tmp_path / "runs" / "receipts"
    live_owner = {"pid": os.getpid(), **process_identity_fields(os.getpid())}
    for name, status, owner, modified in [
        ("old", "succeeded", {}, 1000),
        ("abandoned", "queued", {"pid": 2**22 - 1}, 2000),  # owner was killed
        ("new", "failed", {}, 3000),
        ("live", "queued", live_owner, 500),
    ]:
        (receipts / name).mkdir(parents=True)
        (receipts / name / "run.json").write_text(
            json.dumps({"status": status, "owner": owner})
        )
        os.utime(receipts / name / "run.json", (modified, modified))
    _prune_receipts(tmp_path / "runs", 1)
    assert {path.name for path in receipts.iterdir()} == {"new", "live"}


def test_lost_local_worker_is_reported_instead_of_polling_forever(tmp_path):
    import uuid

    runner = LocalRunner(RunnerConfig(provider="local", runs_dir=tmp_path))
    run_id = str(uuid.uuid4())
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "status.json").write_text(
        json.dumps({"id": run_id, "status": "running", "error": ""})
    )
    (directory / "supervisor.json").write_text(json.dumps({"pid": 2**22 - 1}))
    result = runner.wait(run_id, poll_interval=0.05)
    assert result["status"] == "failed" and "without a completion" in result["error"]

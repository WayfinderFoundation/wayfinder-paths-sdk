"""Synchronous, isolated driver for full self-improvement campaign A/Bs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from wayfinder_paths.jobs.archive import behavior_cell
from wayfinder_paths.jobs.background import op_status_summary
from wayfinder_paths.jobs.bench.aggregate import aggregate_experiment
from wayfinder_paths.jobs.bench.env import (
    atomic_json,
    free_port,
    git_sha,
    load_json,
    sandbox_relative,
    sha256_file,
    sha256_json,
)
from wayfinder_paths.jobs.bench.forward_replay import race_bundles, replay_probation
from wayfinder_paths.jobs.bench.identity import (
    assert_isolation,
    compare_identities,
    ensure_model_declared,
    runtime_identity,
)
from wayfinder_paths.jobs.bench.world import (
    install_development_world,
    load_world,
    reveal_holdout_features,
)
from wayfinder_paths.jobs.benchmarks.agent_adapter import (
    DEFAULT_OPENCODE,
    install_agent_workspace,
    meter_session_ids,
    run_agent_prompt,
)
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.evolution_campaign import (
    campaign_prompt_block,
    campaign_status,
    finalize_campaign,
    start_campaign,
)
from wayfinder_paths.jobs.evolution_funnel import summarize_evolution_funnel
from wayfinder_paths.jobs.execution.op_process import terminate_campaign_ops
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import load_probation, resolve_probation_bundle
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import build_evolution_stage_prompt

EXPERIMENT_SCHEMA_VERSION = "1.0"
SUPPORTED_IDENTITY_DIFFERENCES = (
    "model",
    "variant",
    "sdk_ref",
    "agent_hashes",
    "agent_temperatures",
    "initial_prompt_sha256",
    "arm_parameters",
)


def run_experiment(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_json(config_path)
    _validate_config(config)
    output_dir = _from_config(config_path, config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=False)
    # Freeze the question before the first model call.
    pins = _runtime_pins(config_path, config)
    frozen_config = {
        **config,
        "config_sha256": sha256_file(config_path),
        "runtime_pins": pins,
    }
    atomic_json(output_dir / "experiment.json", frozen_config)
    runtime_config = {
        **config,
        "_config_dir": str(config_path.parent),
        "_runtime_pins": pins,
    }
    registered_runs = [
        (
            _from_config(config_path, world_config["world_dir"]),
            _from_config(config_path, world_config["sealed_dir"]),
            int(seed),
            arm,
        )
        for world_config in config["worlds"]
        for seed in config["seeds"]
        for arm in config["arms"]
    ]
    ordered_rows: list[dict[str, Any] | None] = [None] * len(registered_runs)
    identities: dict[tuple[str, int, str], dict[str, Any]] = {}
    max_parallel = int(config.get("max_parallel_arms") or 1)
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                _run_arm_in_declared_sdk,
                config=runtime_config,
                arm=arm,
                seed=seed,
                world_dir=world_dir,
                sealed_dir=sealed_dir,
                output_dir=output_dir,
            ): index
            for index, (world_dir, sealed_dir, seed, arm) in enumerate(registered_runs)
        }
        for future in as_completed(futures):
            index = futures[future]
            row = future.result()
            ordered_rows[index] = row
            identities[(row["world_id"], row["seed"], row["arm"])] = row["identity"]
            atomic_json(output_dir / "runs" / f"{row['run_id']}.json", row)
    rows = [row for row in ordered_rows if row is not None]
    parity = _identity_parity(config, identities)
    atomic_json(output_dir / "identity_parity.json", parity)
    if not parity["comparable"]:
        report = {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "decision": "invalid_identity_mismatch",
            "identity": parity,
            "runs": len(rows),
        }
        atomic_json(output_dir / "aggregate.json", report)
        return report
    return aggregate_experiment(
        rows,
        arm_order=[str(arm["name"]) for arm in config["arms"]],
        output_dir=output_dir,
        confidence=float(config.get("confidence") or 0.90),
        max_cost_ratio=float(config.get("max_cost_ratio") or 1.25),
        cost_parity_basis=str(config.get("cost_parity_basis") or "tokens"),
        pilot=bool(config.get("pilot")),
    )


def _run_arm_in_declared_sdk(
    *,
    config: dict[str, Any],
    arm: dict[str, Any],
    seed: int,
    world_dir: Path | None,
    sealed_dir: Path | None,
    output_dir: Path,
    run_id: str | None = None,
    request_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the controller under the arm's SDK, not the launcher's imports.

    The neutral benchmark package is overlaid onto a private copy of the arm
    SDK. Everything it calls (campaign state, prompts, screens, gates, and
    probation) therefore resolves from that arm's declared checkout. A
    recurrence run names its own ``run_id`` and request fields instead of a
    pre-built world.
    """
    if run_id is None:
        if world_dir is None:
            raise ValueError("an arm run needs a world_dir or an explicit run_id")
        world_id = str(load_json(world_dir / "world.json")["world_id"])
        run_id = f"{world_id}-{seed}-{arm['name']}"
    controller = output_dir / "controllers" / _safe_name(run_id)
    if controller.exists():
        raise FileExistsError(f"arm controller already exists: {controller}")
    sdk_root = _resolve_sdk_root(config, arm)
    source_package = sdk_root / "wayfinder_paths"
    if not source_package.is_dir():
        raise FileNotFoundError(f"arm SDK package is missing: {source_package}")
    shutil.copytree(
        source_package,
        controller / "wayfinder_paths",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
    )
    _overlay_bench_package(controller)
    request = {
        "config": config,
        "arm": arm,
        "seed": seed,
        "world_dir": str(world_dir) if world_dir is not None else None,
        "sealed_dir": str(sealed_dir) if sealed_dir is not None else None,
        "output_dir": str(output_dir),
        "result_path": str(controller / "result.json"),
        **dict(request_extra or {}),
    }
    atomic_json(controller / "request.json", request)
    python = sdk_root / ".venv" / "bin" / "python"
    command = [
        str(python if python.exists() else Path(sys.executable)),
        "-m",
        "wayfinder_paths.jobs.bench.arm_entry",
        str(controller / "request.json"),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(controller)
    log_path = controller / "controller.log"
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            command,
            cwd=controller,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=float(config.get("arm_timeout_seconds") or 43_200),
        )
    if completed.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2_000:]
        raise RuntimeError(
            f"benchmark arm {run_id} exited {completed.returncode}: {tail}"
        )
    return load_json(controller / "result.json")


def run_arm(
    *,
    config: dict[str, Any],
    arm: dict[str, Any],
    seed: int,
    world_dir: Path,
    sealed_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    world = load_world(world_dir, sealed_dir)
    run_id = f"{world['manifest']['world_id']}-{seed}-{arm['name']}"
    sandbox = prepare_sandbox(
        config=config,
        arm=arm,
        world_dir=world_dir,
        sealed_dir=sealed_dir,
        output_dir=output_dir,
        run_id=run_id,
        # The stores are physically isolated, so arms can share the same job
        # id. This keeps the production prompt bytes identical across a pair.
        job_id=f"bench-{_safe_name(world['manifest']['world_id'])}-s{seed}",
    )
    generation_cutoff = _parse(world["manifest"]["generation_cutoff"])
    prompt_hashes: list[str] = []
    sessions: list[dict[str, Any]] = []
    stage_sessions: dict[str, str] = {}
    env = _arm_env(sandbox["run_root"], virtual_now=generation_cutoff)
    started = time.monotonic()
    with bench_mcp_server(sandbox, env=env):
        invalid_reason = run_campaign_phase(
            sandbox,
            config=config,
            seed=seed,
            env=env,
            virtual_now=generation_cutoff,
            sessions=sessions,
            prompt_hashes=prompt_hashes,
            stage_sessions=stage_sessions,
            smoke=bool(config.get("interface_smoke", True)),
        )
    forward, holdout, invalid_reason = run_probation_phase(
        sandbox,
        world=world,
        world_dir=world_dir,
        sealed_dir=sealed_dir,
        invalid_reason=invalid_reason,
    )
    audit = audit_and_score(
        sandbox,
        sessions=sessions,
        started=started,
        protected_roots=[world_dir, sealed_dir],
        invalid_reason=invalid_reason,
        forward=forward,
        holdout=holdout,
    )
    scorecard = audit["scorecard"]
    identity = runtime_identity(
        sdk_ref=_arm_runtime_pin(config, arm)["sdk_ref"],
        sandbox=sandbox["run_root"],
        model=sandbox["model"],
        variant=sandbox["variant"],
        repeat_seed=seed,
        world_manifest=world["manifest"],
        prompt_hashes=prompt_hashes,
        declared_differences=list(
            config.get("allowed_identity_differences") or ["model", "variant"]
        ),
        arm_parameters={"campaign": sandbox["arm_campaign"]},
        opencode=sandbox["opencode"],
    )
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "run_id": run_id,
        "arm": str(arm["name"]),
        "world_id": world["manifest"]["world_id"],
        "seed": seed,
        "model": sandbox["model"],
        "variant": sandbox["variant"],
        "invalid_reason": audit["invalid_reason"],
        "funnel": scorecard["funnel"],
        "verdicts": scorecard["verdicts"],
        "forward": forward,
        "holdout": holdout,
        "diversity": scorecard["diversity"],
        "process": scorecard["process"],
        "cost": audit["cost"],
        "session_diagnostics": audit["session_diagnostics"],
        "isolation": audit["isolation"],
        "identity": identity,
        "workspace": str(sandbox["run_root"]),
    }


def prepare_sandbox(
    *,
    config: dict[str, Any],
    arm: dict[str, Any],
    world_dir: Path,
    sealed_dir: Path,
    output_dir: Path,
    run_id: str,
    job_id: str,
    verify_world_pin: bool = True,
) -> dict[str, Any]:
    """Isolated workspace + agent config + the world's incumbent installed as
    the sandbox job. Returns the handles every later phase needs."""
    run_root = output_dir / "workspaces" / _safe_name(run_id)
    if run_root.exists():
        raise FileExistsError(f"run workspace already exists: {run_root}")
    _assert_bench_root(run_root)
    assert_isolation(sandbox=run_root, sealed_dir=sealed_dir)
    assert_isolation(sandbox=run_root, sealed_dir=world_dir)
    sdk_root = _resolve_sdk_root(config, arm)
    runtime_opencode_config = _resolve_runtime_opencode_config(config)
    _verify_arm_pins(
        config,
        arm=arm,
        sdk_root=sdk_root,
        runtime_opencode_config=runtime_opencode_config,
    )
    if verify_world_pin:
        _verify_world_pin(config, world_dir=world_dir, sealed_dir=sealed_dir)
    model = str(arm["model"])
    variant = str(arm.get("variant") or "") or None
    opencode = Path(config.get("opencode") or DEFAULT_OPENCODE).expanduser()
    port = free_port()
    install_agent_workspace(
        sandbox=run_root,
        repo_root=sdk_root,
        mcp_url=f"http://127.0.0.1:{port}/mcp",
        runtime_config=runtime_opencode_config,
    )
    _overlay_bench_package(run_root)
    config_path = run_root / ".opencode" / "opencode.json"
    if base_url := str(config.get("wayfinder_base_url") or "").strip():
        _set_provider_base_url(config_path, provider="wayfinder", base_url=base_url)
    ensure_model_declared(config_path, model)
    arm_campaign = dict(arm.get("campaign") or {})
    store, installed_job_id = _install_job(
        run_root,
        world_dir=world_dir,
        policy={**dict(config.get("campaign") or {}), **arm_campaign},
        job_id_override=job_id,
    )
    return {
        "run_id": run_id,
        "run_root": run_root,
        "store": store,
        "job_id": installed_job_id,
        "model": model,
        "variant": variant,
        "opencode": opencode,
        "port": port,
        "sdk_root": sdk_root,
        "arm_campaign": arm_campaign,
    }


@contextmanager
def bench_mcp_server(
    sandbox: dict[str, Any], *, env: dict[str, str]
) -> Iterator[subprocess.Popen]:
    server = _start_mcp(sandbox["run_root"], port=sandbox["port"], env=env)
    try:
        yield server
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def run_campaign_phase(
    sandbox: dict[str, Any],
    *,
    config: dict[str, Any],
    seed: int,
    env: dict[str, str],
    virtual_now: datetime,
    sessions: list[dict[str, Any]],
    prompt_hashes: list[str],
    stage_sessions: dict[str, str],
    smoke: bool,
) -> str | None:
    """Optional interface smoke, then one full campaign through the production
    harness. Returns the invalid reason, if any. Needs the MCP server up."""
    store: JobStore = sandbox["store"]
    job_id = str(sandbox["job_id"])
    invalid_reason: str | None = None
    if smoke:
        smoke_path = (store.job_dir(job_id) / "job.yaml").resolve()
        result = run_agent_prompt(
            sandbox=sandbox["run_root"],
            prompt=(
                f"Read exactly `{smoke_path}` with the read tool, then reply "
                "with exactly BENCH_READY. Do not call any other tool."
            ),
            model=sandbox["model"],
            variant=sandbox["variant"],
            title=f"bench-smoke-{_safe_name(str(sandbox['run_id']))}",
            opencode=sandbox["opencode"],
            agent="wayfinder-evolution-designer",
            timeout_s=int(config.get("turn_timeout_seconds") or 1_800),
            env=env,
        )
        sessions.append({"stage": "interface-smoke", **result})
        if result["exit_code"] != 0 or "BENCH_READY" not in result["stdout_tail"]:
            invalid_reason = "model interface smoke failed"
    if invalid_reason is None:
        start_campaign(store, job_id, now=virtual_now, force=True)
        invalid_reason = _drive_campaign(
            store=store,
            job_id=job_id,
            model=sandbox["model"],
            variant=sandbox["variant"],
            opencode=sandbox["opencode"],
            sandbox=sandbox["run_root"],
            env=env,
            virtual_now=virtual_now,
            repeat_seed=seed,
            max_turns=int(config.get("max_turns") or 40),
            timeout_s=int(config.get("turn_timeout_seconds") or 1_800),
            settle_timeout_s=int(config.get("settle_timeout_seconds") or 3_600),
            prompt_hashes=prompt_hashes,
            sessions=sessions,
            stage_sessions=stage_sessions,
        )
    return invalid_reason


def run_probation_phase(
    sandbox: dict[str, Any],
    *,
    world: dict[str, Any],
    world_dir: Path,
    sealed_dir: Path,
    invalid_reason: str | None,
    campaign_id: str | None = None,
    holdout_output: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Replay the staged trial over the sealed holdout and race it against
    the incumbent. ``campaign_id`` scopes both to one campaign's trial."""
    store: JobStore = sandbox["store"]
    job_id = str(sandbox["job_id"])
    run_root: Path = sandbox["run_root"]
    development = list(world["development"].get("bars") or [])
    holdout_rows = list(world["holdout"].get("bars") or [])
    generation_cutoff = _parse(world["manifest"]["generation_cutoff"])
    forward: dict[str, Any]
    holdout: dict[str, Any]
    if invalid_reason is not None:
        state = campaign_status(store, job_id)
        active_campaign = str(state.get("campaign_id") or "")
        if active_campaign:
            terminate_campaign_ops(store, job_id, active_campaign)
        forward = {
            "available": False,
            "status": "invalid",
            "reason": invalid_reason,
        }
        holdout = {"verdict": "invalid", "reason": invalid_reason}
        return forward, holdout, invalid_reason
    reveal_holdout_features(sealed_dir, store.job_dir(job_id))
    forward = replay_probation(
        store,
        job_id,
        development_rows=development,
        holdout_rows=holdout_rows,
        generation_cutoff=generation_cutoff,
        campaign_id=campaign_id,
    )
    trials = list(load_probation(store, job_id).get("trials") or [])
    if campaign_id is None:
        trial = next(iter(trials), None)
    else:
        trial = next(
            (row for row in trials if row.get("campaign_id") == campaign_id), None
        )
    if trial:
        candidate_bundle = resolve_probation_bundle(store, job_id, trial["candidate"])
        reference_bundle = resolve_probation_bundle(store, job_id, trial["reference"])
        holdout = race_bundles(
            candidate_bundle,
            reference_bundle,
            world_dir=world_dir,
            sealed_dir=sealed_dir,
            output_dir=holdout_output or (run_root / "results" / "holdout"),
            feature_root=store.job_dir(job_id),
        )
        if holdout.get("verdict") == "invalid":
            invalid_reason = "holdout_race_invalid"
    else:
        # No survivor means the process retained the incumbent. Its
        # candidate-minus-incumbent endpoint is exactly zero; do not run
        # a fake incumbent-vs-itself race just to manufacture activity.
        holdout = {
            "verdict": "no_candidate",
            "paired_daily_utility_delta": {
                "days": 0,
                "estimate": 0.0,
                "lcb": None,
                "values": [],
            },
        }
    return forward, holdout, invalid_reason


def audit_and_score(
    sandbox: dict[str, Any],
    *,
    sessions: list[dict[str, Any]],
    started: float,
    protected_roots: list[Path],
    invalid_reason: str | None,
    forward: dict[str, Any],
    holdout: dict[str, Any],
) -> dict[str, Any]:
    """Meter the sessions, audit isolation against the protected roots, and
    score the campaign state. Only these sessions count."""
    store: JobStore = sandbox["store"]
    job_id = str(sandbox["job_id"])
    run_root: Path = sandbox["run_root"]
    session_db = _session_db(run_root)
    session_ids = []
    missing_transcripts = []
    for row in sessions:
        session_id = row.get("session_id") or _lookup_session_id(
            session_db, row["title"]
        )
        if session_id:
            session_ids.append(str(session_id))
        else:
            missing_transcripts.append(str(row["title"]))
    cost = meter_session_ids(session_ids, session_db=session_db)
    elapsed_seconds = round(time.monotonic() - started, 3)
    cost["agent_wall_seconds"] = cost["wall_seconds"]
    cost["wall_seconds"] = elapsed_seconds
    cost["other_seconds"] = round(
        max(
            elapsed_seconds
            - float(cost.get("model_seconds") or 0)
            - float(cost.get("tool_seconds") or 0),
            0,
        ),
        3,
    )
    isolation = _audit_session_isolation(
        session_ids,
        session_db=session_db,
        sandbox=run_root,
        protected_roots=protected_roots,
        missing_transcripts=missing_transcripts,
    )
    if not isolation["passed"] and invalid_reason is None:
        invalid_reason = "isolation_breach"
    state = campaign_status(store, job_id)
    funnel = summarize_evolution_funnel(state)
    scorecard = _scorecard(
        state=state,
        funnel=funnel,
        forward=forward,
        holdout=holdout,
        sessions=sessions,
        cost=cost,
        signals=_validated_signal_usage(store, job_id, state),
    )
    return {
        "cost": cost,
        "isolation": isolation,
        "invalid_reason": invalid_reason,
        "state": state,
        "scorecard": scorecard,
        "session_diagnostics": _session_diagnostics(sessions),
    }


def _session_diagnostics(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: row.get(key)
            for key in (
                "stage",
                "artifact_key",
                "turn",
                "title",
                "session_id",
                "exit_code",
                "stdout_tail",
                "stderr_tail",
            )
            if row.get(key) is not None
        }
        for row in sessions
    ]


def _install_job(
    sandbox: Path,
    *,
    world_dir: Path,
    policy: dict[str, Any],
    job_id_override: str | None = None,
) -> tuple[JobStore, str]:
    world_manifest = load_json(world_dir / "world.json")
    source = world_dir / str(world_manifest["incumbent"]["path"])
    job_id = job_id_override or str(world_manifest["source_job"])
    store = JobStore(repo_root=sandbox)
    destination = store.job_dir(job_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_job_bundle(source, destination)
    job_yaml_path = destination / "job.yaml"
    job_yaml = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8")) or {}
    job_yaml.update(
        {
            "id": job_id,
            "name": str(job_yaml.get("name") or job_id),
            "execution_contract": "jobs_v1",
        }
    )
    job_yaml["script_loop"] = {
        **dict(job_yaml.get("script_loop") or {}),
        "enabled": True,
        "mode": "paper",
    }
    job_yaml["agent_loop"] = {
        **dict(job_yaml.get("agent_loop") or {}),
        "enabled": True,
        "mode": "intervene",
    }
    execution_params = dict(job_yaml.get("execution_params") or {})
    execution_params.pop("wallet_label", None)
    job_yaml["execution_params"] = execution_params
    job_yaml_path.write_text(
        yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
    )
    job = WayfinderJob.from_dict(job_yaml)
    store.init_layout(job)
    install_development_world(world_dir, destination_job=destination)
    evolution = {
        "enabled": True,
        "allowed_job_ids": [job_id],
        "excluded_job_ids": [],
        "pricing_schedule": {"blocked_windows_utc": []},
        **policy,
    }
    (destination / "improver.yaml").write_text(
        yaml.safe_dump({"evolution": evolution}, sort_keys=False), encoding="utf-8"
    )
    return store, job_id


def _drive_campaign(
    *,
    store: JobStore,
    job_id: str,
    model: str,
    variant: str | None,
    opencode: Path,
    sandbox: Path,
    env: dict[str, str],
    virtual_now: datetime,
    repeat_seed: int,
    max_turns: int,
    timeout_s: int,
    settle_timeout_s: int,
    prompt_hashes: list[str],
    sessions: list[dict[str, Any]],
    stage_sessions: dict[str, str],
) -> str | None:
    prior_handoff: dict[str, Any] | None = None
    failed_once: set[str] = set()
    for turn in range(max_turns):
        state = campaign_status(store, job_id)
        if state.get("status") in {"complete", "failed", "expired"}:
            return (
                None if state.get("status") == "complete" else str(state.get("status"))
            )
        block = campaign_prompt_block(store, job_id, now=virtual_now)
        if block is None:
            return "campaign prompt disappeared before completion"
        if block.get("status") == "blocked":
            if not _wait_for_settle(store, job_id, timeout_s=settle_timeout_s):
                return str(block.get("reason") or "campaign remained blocked")
            continue
        block = sandbox_relative(block, root=sandbox)
        rendered = build_evolution_stage_prompt(
            job_id, block, prior_handoff=prior_handoff
        )
        prompt = (
            f"{rendered['prompt']}\n\n"
            f"BENCHMARK REPETITION TOKEN: {repeat_seed}. This token distinguishes "
            "stochastic repeats; it does not change evaluation rules or data."
        )
        prompt_hashes.append(hashlib.sha256(prompt.encode()).hexdigest())
        stage = rendered["session_stage"]
        artifact_key = rendered["artifact_key"]
        title = f"{rendered['title']}/seed-turn-{turn:02d}"
        existing_session = stage_sessions.get(artifact_key)
        result = run_agent_prompt(
            sandbox=sandbox,
            prompt=prompt,
            model=model,
            variant=variant,
            title=title,
            session_id=existing_session,
            opencode=opencode,
            agent=rendered["agent_name"],
            timeout_s=timeout_s,
            env=env,
        )
        result.update({"stage": stage, "artifact_key": artifact_key, "turn": turn})
        sessions.append(result)
        if result["exit_code"] != 0:
            # One bad tool call or a crashed turn is a failed turn, not a
            # verdict: retry the stage once in a fresh session, the way the
            # production watchdog restarts a stale stage.
            if artifact_key not in failed_once:
                failed_once.add(artifact_key)
                result["retried"] = True
                stage_sessions.pop(artifact_key, None)
                continue
            return f"OpenCode stage {stage} exited {result['exit_code']} twice"
        session_db = _session_db(sandbox)
        session_id = existing_session or _lookup_session_id(session_db, title)
        if session_id:
            stage_sessions[artifact_key] = session_id
            result["session_id"] = session_id
        prior_handoff = {
            "from_stage": stage,
            "final_summary": sandbox_relative(
                result["stdout_tail"][-1_200:], root=sandbox
            ),
        }
        try:
            settled = _wait_for_settle(store, job_id, timeout_s=settle_timeout_s)
        except CampaignSettleError as exc:
            return f"campaign stage {stage} did not settle: {exc}"
        if not settled:
            return f"campaign stage {stage} did not settle"
    return f"campaign exceeded max_turns={max_turns}"


class CampaignSettleError(RuntimeError):
    """The campaign cannot settle: its finalize died and the retry failed."""


def _wait_for_settle(store: JobStore, job_id: str, *, timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    prior = ""
    stable = 0
    time.sleep(0.5)
    while time.monotonic() < deadline:
        state = campaign_status(store, job_id)
        if state.get("status") == "finalizing":
            _recover_dead_finalize(store, job_id)
            state = campaign_status(store, job_id)
        encoded = json.dumps(state, sort_keys=True, default=str)
        running = state.get("status") == "finalizing" or any(
            str(candidate.get("status") or "").endswith("_running")
            for candidate in state.get("candidates") or []
        )
        if encoded == prior and not running:
            stable += 1
            if stable >= 3:
                return True
        else:
            stable = 0
        prior = encoded
        time.sleep(1.0)
    return False


def _recover_dead_finalize(store: JobStore, job_id: str) -> None:
    """Production's watchdog relaunches a finalize whose detached op died
    with the campaign still ``finalizing``; the bench has no watchdog, so
    without this a dead finalize costs the whole settle timeout and voids
    the loop with no reason recorded. Run it in the foreground once; a
    failure names the cause instead of the clock."""
    op = op_status_summary(store.job_dir(job_id), "evolution_finalize")
    if op is None or op.get("status") == "running":
        return
    try:
        finalize_campaign(store, job_id)
    except Exception as exc:  # noqa: BLE001 - the reason is the verdict
        raise CampaignSettleError(
            f"finalize op {op.get('status')}; foreground retry failed: {exc}"
        ) from exc


def _start_mcp(sandbox: Path, *, port: int, env: dict[str, str]) -> subprocess.Popen:
    python = sandbox / ".venv" / "bin" / "python"
    command = [
        str(python if python.exists() else Path(sys.executable)),
        "-m",
        "wayfinder_paths.jobs.bench.mcp_server",
        "--port",
        str(port),
    ]
    log = (sandbox / "mcp.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=sandbox,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("benchmark MCP server exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return process
        except OSError:
            time.sleep(0.25)
    process.terminate()
    raise TimeoutError("benchmark MCP server did not start")


def _arm_env(sandbox: Path, *, virtual_now: datetime | None = None) -> dict[str, str]:
    env = dict(os.environ)
    isolated_home = sandbox / ".bench-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(isolated_home)
    env["PYTHONPATH"] = str(sandbox)
    env["XDG_DATA_HOME"] = str(sandbox / ".bench-data")
    env["XDG_CACHE_HOME"] = str(sandbox / ".bench-cache")
    env["WAYFINDER_BENCHMARK"] = "1"
    if virtual_now is not None:
        env["WAYFINDER_BENCHMARK_NOW"] = _aware_utc(virtual_now).isoformat()
    env["WAYFINDER_BURST_STATE_PATH"] = str(
        sandbox / ".bench-state" / "burst-governor.json"
    )
    return env


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _session_db(sandbox: Path) -> Path:
    root = sandbox / ".bench-data"
    matches = sorted(root.rglob("opencode.db")) if root.exists() else []
    return matches[0] if matches else root / "opencode" / "opencode.db"


def _lookup_session_id(database: Path, title: str) -> str | None:
    if not database.exists():
        return None
    connection = sqlite3.connect(str(database))
    try:
        row = connection.execute(
            "SELECT id FROM session WHERE title=? ORDER BY time_updated DESC LIMIT 1",
            (title,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def _audit_session_isolation(
    session_ids: list[str],
    *,
    session_db: Path,
    sandbox: Path,
    protected_roots: list[Path],
    missing_transcripts: list[str],
) -> dict[str, Any]:
    """Detect successful future/sibling/network access in persisted tool calls."""
    breaches: list[dict[str, Any]] = [
        {"type": "missing_transcript", "title": title} for title in missing_transcripts
    ]
    if not session_db.exists():
        if session_ids:
            breaches.append({"type": "missing_session_database"})
        return {
            "passed": not breaches,
            "breaches": breaches,
            "denied_attempts": [],
        }
    sandbox = sandbox.resolve()
    protected = [path.resolve() for path in protected_roots]
    denied_attempts: list[dict[str, Any]] = []
    connection = sqlite3.connect(str(session_db))
    try:
        for session_id in dict.fromkeys(session_ids):
            try:
                rows = connection.execute(
                    "SELECT json_extract(data,'$.tool'), "
                    "json_extract(data,'$.state.input'), "
                    "json_extract(data,'$.state.status'), "
                    "json_extract(data,'$.state.error') FROM part "
                    "WHERE session_id=? AND json_extract(data,'$.type')='tool'",
                    (session_id,),
                )
            except sqlite3.OperationalError:
                breaches.append(
                    {"type": "unreadable_transcript", "session_id": session_id}
                )
                continue
            for tool, raw_input, status, raw_error in rows:
                tool_name = str(tool or "unknown")
                lowered = tool_name.lower()
                policy_denied = str(status or "") == "error" and any(
                    marker in str(raw_error or "").lower()
                    for marker in (
                        "prevents you from using this specific tool call",
                        "permission denied",
                        "url must start with http:// or https://",
                    )
                )
                if any(
                    marker in lowered
                    for marker in ("bash", "shell", "fetch", "http", "browser", "web")
                ):
                    target = denied_attempts if policy_denied else breaches
                    target.append(
                        {
                            "type": (
                                "denied_network_or_shell_tool"
                                if policy_denied
                                else "network_or_shell_tool"
                            ),
                            "session_id": session_id,
                            "tool": tool_name,
                        }
                    )
                    continue
                try:
                    tool_input = json.loads(raw_input) if raw_input else {}
                except (TypeError, ValueError):
                    tool_input = {}
                if tool_name == "core_jobs":
                    continue
                for key, value in _input_strings(tool_input):
                    if not any(
                        marker in key.lower()
                        for marker in ("path", "file", "directory", "root")
                    ):
                        continue
                    candidate = Path(value).expanduser()
                    resolved = (
                        candidate.resolve()
                        if candidate.is_absolute()
                        else (sandbox / candidate).resolve()
                    )
                    outside = not resolved.is_relative_to(sandbox)
                    protected_access = any(
                        resolved == root or resolved.is_relative_to(root)
                        for root in protected
                        if root != sandbox
                    )
                    if outside or protected_access:
                        target = denied_attempts if policy_denied else breaches
                        target.append(
                            {
                                "type": (
                                    "denied_filesystem_escape"
                                    if policy_denied
                                    else "filesystem_escape"
                                ),
                                "session_id": session_id,
                                "tool": tool_name,
                                "path": value[:300],
                            }
                        )
    finally:
        connection.close()
    return {
        "passed": not breaches,
        "breaches": breaches,
        "denied_attempts": denied_attempts,
    }


def _input_strings(value: Any, key: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [
            pair
            for child_key, child in value.items()
            for pair in _input_strings(child, str(child_key))
        ]
    if isinstance(value, list):
        return [pair for child in value for pair in _input_strings(child, key)]
    return [(key, value)] if isinstance(value, str) else []


def _validated_signal_usage(
    store: Any, job_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    """How many validated signals the pack offered and how many slots cited one."""
    pack = (
        store.read_json(job_id, str(state.get("diagnostic_pack") or ""), default={})
        or {}
    )
    validated = pack.get("validated_signals") or {}
    design = (
        store.read_json(job_id, str(state.get("campaign_design") or ""), default={})
        or {}
    )
    citing = 0
    for hypothesis in design.get("hypotheses") or []:
        if any(
            str(ref).startswith("/validated_signals/")
            for ref in hypothesis.get("evidence_refs") or []
        ):
            citing += 1
    return {
        "enabled": bool(validated),
        "available": bool(validated.get("available")),
        "offered": len(validated.get("signals") or []),
        "hypotheses_citing": citing,
    }


def _scorecard(
    *,
    state: dict[str, Any],
    funnel: dict[str, Any],
    forward: dict[str, Any],
    holdout: dict[str, Any],
    sessions: list[dict[str, Any]],
    cost: dict[str, Any],
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = list(state.get("candidates") or [])
    funnel_counts = state.get("counts") or {}
    quick = funnel.get("quick_screen") or {}
    full_development = funnel.get("full_development") or {}
    finalist = funnel.get("finalist_gate") or {}
    elites = [row for row in candidates if row.get("elite_eligible") is True]
    elite_trades = [
        int((row.get("elite_activity") or {}).get("validation_trades") or 0)
        for row in elites
    ]
    cells = {
        cell
        for row in elites
        if (cell := behavior_cell(row.get("behavior"))) is not None
    }
    behavior_flags = [
        value
        for row in candidates
        for attempt in row.get("attempts") or []
        if isinstance(
            value := ((attempt.get("postmortem") or {}).get("behavior_diff") or {}).get(
                "material_change"
            ),
            bool,
        )
    ]
    attempt_outcomes = [
        attempt.get("outcome") or {}
        for row in candidates
        for attempt in row.get("attempts") or []
    ]
    attempt_postmortems = [
        attempt.get("postmortem") or {}
        for row in candidates
        for attempt in row.get("attempts") or []
    ]
    return {
        "funnel": {
            "candidates_generated": int(funnel_counts.get("generated") or 0),
            "attempts": int(
                funnel_counts.get("quick_attempts")
                or funnel_counts.get("quick_evaluated")
                or 0
            ),
            "screen_positive": int(quick.get("passed") or 0),
            "full_dev": int(full_development.get("evaluated") or 0),
            "gate_passed": int(
                finalist.get("advanced_to_probation")
                or finalist.get("advanced_to_paper")
                or 0
            ),
            "staged": 1 if forward.get("available") else 0,
            "awaiting_regime": sum(
                1 for row in candidates if row.get("status") == "awaiting_regime"
            ),
            "focus": dict(state.get("focus") or {}),
            "validated_signals": dict(signals or {}),
            "production_summary": funnel,
        },
        "verdicts": {
            "burn_in_admitted": bool(
                (forward.get("burn_in") or {}).get("status") == "passed"
            ),
            "graduated": forward.get("status") == "graduated",
            "killed": forward.get("status") == "killed",
            "inconclusive": forward.get("status") == "inconclusive",
        },
        "diversity": {
            "qd_cells_occupied": len(cells),
            "distinct_families": len(
                {str(row.get("family")) for row in candidates if row.get("family")}
            ),
            "mean_elite_trade_count": (
                round(sum(elite_trades) / len(elite_trades), 3) if elite_trades else 0.0
            ),
            "parent_sources": sorted(
                {
                    str(row.get("parent_source"))
                    for row in candidates
                    if row.get("parent_source")
                }
            ),
        },
        "process": {
            "turns": len(
                [row for row in sessions if row.get("stage") != "interface-smoke"]
            ),
            "failed_turns": sum(1 for row in sessions if row.get("exit_code") != 0),
            "fact_citations": sum(
                len(row.get("evidence_refs") or []) for row in candidates
            ),
            "wildcards_used": sum(bool(row.get("wildcard")) for row in candidates),
            "postmortems_consumed": sum(
                max(int(row.get("attempt_count") or 0) - 1, 0) for row in candidates
            ),
            "behavior_changed_attempts": sum(value is True for value in behavior_flags),
            "behavior_unchanged_attempts": sum(
                value is False for value in behavior_flags
            ),
            "quick_simulations": sum(
                outcome.get("quick_simulation_ran") is True
                for outcome in attempt_outcomes
            ),
            "behavior_preview_rejections": sum(
                ((outcome.get("behavior_preview") or {}).get("status") == "unchanged")
                for outcome in attempt_outcomes
            ),
            "behavior_preview_ticks": sum(
                int((outcome.get("behavior_preview") or {}).get("ticks_evaluated") or 0)
                for outcome in attempt_outcomes
            ),
            "sequence_previews": sum(
                bool(outcome.get("sequence_preview")) for outcome in attempt_outcomes
            ),
            "sequence_preview_frozen": sum(
                (outcome.get("sequence_preview") or {}).get("status")
                == "armed_no_entry"
                for outcome in attempt_outcomes
            ),
            "no_progress_preview_rejections": sum(
                postmortem.get("primary_failure") == "no_progress_preview"
                for postmortem in attempt_postmortems
            ),
            "holdout_verdict": holdout.get("verdict"),
        },
        "cost": cost,
    }


def _identity_parity(
    config: dict[str, Any],
    identities: dict[tuple[str, int, str], dict[str, Any]],
) -> dict[str, Any]:
    arms = [str(arm["name"]) for arm in config["arms"]]
    checks = []
    for world_id, seed in sorted({(key[0], key[1]) for key in identities}):
        left = identities[(world_id, seed, arms[0])]
        right = identities[(world_id, seed, arms[1])]
        check = compare_identities(
            left,
            right,
            allowed={
                *(config.get("allowed_identity_differences") or ["model", "variant"]),
                "opencode_config_sha256",
                "prompt_hashes",
            },
        )
        checks.append({"world_id": world_id, "seed": seed, **check})
    return {
        "comparable": bool(checks) and all(row["comparable"] for row in checks),
        "checks": checks,
    }


def _validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {EXPERIMENT_SCHEMA_VERSION}")
    if len(config.get("arms") or []) != 2:
        raise ValueError("experiments require exactly two arms")
    if not config.get("worlds") or not config.get("seeds"):
        raise ValueError("experiments require worlds and seeds")
    if not config.get("pilot") and len(config["seeds"]) < 4:
        raise ValueError("experiments require at least four seeds per arm/world")
    max_parallel = int(config.get("max_parallel_arms") or 1)
    if not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel_arms must be between 1 and 8")
    names = [str(arm.get("name") or "") for arm in config["arms"]]
    if not all(names) or len(set(names)) != 2:
        raise ValueError("arm names must be non-empty and unique")
    allowed_identity = set(
        config.get("allowed_identity_differences") or ["model", "variant"]
    )
    unsupported = allowed_identity - set(SUPPORTED_IDENTITY_DIFFERENCES)
    if unsupported:
        raise ValueError(
            f"unsupported allowed_identity_differences: {sorted(unsupported)}"
        )


def _from_config(config_path: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return (
        path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()
    )


def _resolve_sdk_root(config: dict[str, Any], arm: dict[str, Any]) -> Path:
    config_dir = Path(str(config.get("_config_dir") or Path.cwd()))
    value = Path(arm.get("sdk_root") or config.get("sdk_root") or ".").expanduser()
    return value.resolve() if value.is_absolute() else (config_dir / value).resolve()


def _resolve_runtime_opencode_config(config: dict[str, Any]) -> Path | None:
    raw = str(config.get("runtime_opencode_config") or "").strip()
    if not raw:
        return None
    if raw.startswith("{env:") and raw.endswith("}"):
        variable = raw[5:-1].strip()
        if not variable:
            raise ValueError("runtime_opencode_config has an empty env variable")
        raw = str(os.environ.get(variable) or "").strip()
        if not raw:
            raise ValueError(
                f"runtime_opencode_config requires environment variable {variable}"
            )
    config_dir = Path(str(config.get("_config_dir") or Path.cwd()))
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (config_dir / path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"OpenCode runtime config is missing: {resolved}")
    return resolved


def _set_provider_base_url(config_path: Path, *, provider: str, base_url: str) -> None:
    config = load_json(config_path)
    provider_config = (config.get("provider") or {}).get(provider)
    if not isinstance(provider_config, dict):
        raise ValueError(f"provider {provider!r} is not configured")
    options = provider_config.setdefault("options", {})
    if not isinstance(options, dict):
        raise ValueError(f"provider {provider!r} options must be an object")
    options["baseURL"] = base_url.rstrip("/")
    atomic_json(config_path, config)


def _runtime_pins(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    runtime = {**config, "_config_dir": str(config_path.parent)}
    runtime_opencode_config = _resolve_runtime_opencode_config(runtime)
    worlds = []
    for item in config["worlds"]:
        world_dir = _from_config(config_path, item["world_dir"])
        sealed_dir = _from_config(config_path, item["sealed_dir"])
        loaded = load_world(world_dir, sealed_dir)
        worlds.append(
            {
                "world_dir": str(world_dir),
                "sealed_dir": str(sealed_dir),
                "world_sha256": sha256_file(world_dir / "world.json"),
                "development_sha256": loaded["manifest"]["dataset"][
                    "development_sha256"
                ],
                "holdout_commitment": loaded["manifest"]["dataset"][
                    "holdout_commitment"
                ],
            }
        )
    arms = [
        {
            "name": str(arm["name"]),
            "sdk_root": str(_resolve_sdk_root(runtime, arm)),
            "sdk_ref": git_sha(_resolve_sdk_root(runtime, arm)),
            "campaign_sha256": sha256_json(dict(arm.get("campaign") or {})),
        }
        for arm in config["arms"]
    ]
    return {
        "worlds": worlds,
        "arms": arms,
        "runtime_opencode_config": (
            {
                "path": str(runtime_opencode_config),
                "sha256": sha256_file(runtime_opencode_config),
            }
            if runtime_opencode_config is not None
            else None
        ),
    }


def _verify_runtime_pins(
    config: dict[str, Any],
    *,
    arm: dict[str, Any],
    sdk_root: Path,
    world_dir: Path,
    sealed_dir: Path,
    runtime_opencode_config: Path | None,
) -> None:
    _verify_arm_pins(
        config,
        arm=arm,
        sdk_root=sdk_root,
        runtime_opencode_config=runtime_opencode_config,
    )
    _verify_world_pin(config, world_dir=world_dir, sealed_dir=sealed_dir)


def _verify_arm_pins(
    config: dict[str, Any],
    *,
    arm: dict[str, Any],
    sdk_root: Path,
    runtime_opencode_config: Path | None,
) -> None:
    pins = dict(config.get("_runtime_pins") or {})
    arm_pin = _arm_runtime_pin(config, arm)
    if arm_pin.get("sdk_ref") != git_sha(sdk_root):
        raise ValueError("arm SDK changed after experiment registration")
    if arm_pin.get("campaign_sha256") != sha256_json(dict(arm.get("campaign") or {})):
        raise ValueError("arm campaign parameters changed after registration")
    config_pin = pins.get("runtime_opencode_config")
    if runtime_opencode_config is None:
        if config_pin is not None:
            raise ValueError("OpenCode runtime config changed after registration")
    elif (
        not isinstance(config_pin, dict)
        or config_pin.get("path") != str(runtime_opencode_config)
        or config_pin.get("sha256") != sha256_file(runtime_opencode_config)
    ):
        raise ValueError("OpenCode runtime config changed after registration")


def _verify_world_pin(
    config: dict[str, Any], *, world_dir: Path, sealed_dir: Path
) -> None:
    pins = dict(config.get("_runtime_pins") or {})
    world_pin = next(
        (
            row
            for row in pins.get("worlds") or []
            if row.get("world_dir") == str(world_dir.resolve())
            and row.get("sealed_dir") == str(sealed_dir.resolve())
        ),
        None,
    )
    if not world_pin or world_pin.get("world_sha256") != sha256_file(
        world_dir / "world.json"
    ):
        raise ValueError("benchmark world changed after experiment registration")
    loaded = load_world(world_dir, sealed_dir)
    dataset = loaded["manifest"]["dataset"]
    if world_pin.get("development_sha256") != dataset.get(
        "development_sha256"
    ) or world_pin.get("holdout_commitment") != dataset.get("holdout_commitment"):
        raise ValueError("benchmark data commitments changed after registration")


def _arm_runtime_pin(config: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
    pins = dict(config.get("_runtime_pins") or {})
    arm_pin = next(
        (row for row in pins.get("arms") or [] if row.get("name") == arm.get("name")),
        None,
    )
    if not isinstance(arm_pin, dict) or not arm_pin.get("sdk_ref"):
        raise ValueError("arm SDK runtime pin is missing")
    return arm_pin


def _overlay_bench_package(root: Path) -> None:
    source = Path(__file__).resolve().parent
    destination = root / "wayfinder_paths" / "jobs" / "bench"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)


def _assert_bench_root(path: Path) -> None:
    resolved = path.resolve()
    parts = resolved.parts
    if any(
        parts[index : index + 2] == (".wayfinder", "jobs")
        for index in range(len(parts) - 1)
    ):
        raise ValueError("benchmark workspaces cannot live inside .wayfinder/jobs")


def _parse(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )

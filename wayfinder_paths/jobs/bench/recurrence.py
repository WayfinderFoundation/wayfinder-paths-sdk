"""Chained replay of the whole evolution process over one observation window.

One sandbox job per (arm, seed) lives through every loop. Each loop builds a
world at the next weekly cutoff from the job's CURRENT incumbent, runs the
optional researcher wake and one production campaign, replays probation on
the following sealed bars (open trials carry into the next loop), applies
every graduate under the declared owner rule, and scores the strategy that
was actually deployed against the frozen original. The endpoint is the
process, not a candidate.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.application import apply_candidate_bundle
from wayfinder_paths.jobs.archive import set_incumbent
from wayfinder_paths.jobs.bench.aggregate import aggregate_experiment
from wayfinder_paths.jobs.bench.env import (
    atomic_json,
    git_sha,
    load_json,
    sha256_file,
    sha256_json,
)
from wayfinder_paths.jobs.bench.forward_replay import (
    environment_capital,
    evaluate_bundle,
    paired_daily_utility_deltas,
)
from wayfinder_paths.jobs.bench.identity import runtime_identity
from wayfinder_paths.jobs.bench.leaders import LEADER_CLOSES_RELATIVE
from wayfinder_paths.jobs.bench.runner import (
    EXPERIMENT_SCHEMA_VERSION,
    SUPPORTED_IDENTITY_DIFFERENCES,
    _arm_env,
    _arm_runtime_pin,
    _from_config,
    _identity_parity,
    _parse,
    _resolve_runtime_opencode_config,
    _resolve_sdk_root,
    _run_arm_in_declared_sdk,
    _safe_name,
    audit_and_score,
    bench_mcp_server,
    prepare_sandbox,
    run_campaign_phase,
    run_probation_phase,
)
from wayfinder_paths.jobs.bench.world import (
    install_development_world,
    load_world,
    prepare_world,
    reveal_holdout_features,
)
from wayfinder_paths.jobs.benchmarks.agent_adapter import (
    run_agent_prompt,
    run_agent_wakes,
)
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.economics import block_bootstrap_lcb
from wayfinder_paths.jobs.evolution_campaign import campaign_status, flat_bundle
from wayfinder_paths.jobs.execution.features import DEFAULT_FEATURES_PATH
from wayfinder_paths.jobs.execution.op_process import terminate_campaign_ops
from wayfinder_paths.jobs.forward import default_forward_summary
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.probation import (
    PROBATION_PATH,
    load_probation,
    resolve_probation_bundle,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import validate_ideation_artifact

RECURRENCE_SCHEMA_VERSION = "1.0"
RECURRENCE_KIND = "recurrence"
BARS_RELATIVE = Path("results") / "backtest" / "input_bars.json"
IDEATION_RELATIVE = Path("research") / "ideation" / "latest.json"
AGENDA_RELATIVE = Path("research") / "agenda.md"
RESEARCH_SEEDS_RELATIVE = Path("state") / "evolution_research_seeds.json"
FORWARD_SUMMARY_RELATIVE = "results/forward/summary.json"
_MIN_DEVELOPMENT_DAYS = 30.0
_MIN_LOOP_DAYS = 5
_OPEN_TRIAL_STATUSES = {"queued", "burn_in", "active"}
_FUNNEL_SUM_KEYS = (
    "candidates_generated",
    "attempts",
    "screen_positive",
    "full_dev",
    "gate_passed",
    "staged",
    "awaiting_regime",
)
_PROCESS_SUM_KEYS = (
    "turns",
    "failed_turns",
    "fact_citations",
    "wildcards_used",
    "postmortems_consumed",
    "behavior_changed_attempts",
    "behavior_unchanged_attempts",
    "quick_simulations",
    "behavior_preview_rejections",
    "behavior_preview_ticks",
    "sequence_previews",
    "sequence_preview_frozen",
    "no_progress_preview_rejections",
)
_COST_SUM_KEYS = (
    "sessions",
    "messages",
    "tokens_in",
    "tokens_out",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "tool_calls",
    "tool_result_bytes",
    "tool_output_bytes",
    "wall_seconds",
    "agent_wall_seconds",
    "model_seconds",
    "tool_seconds",
    "other_seconds",
)


def run_recurrence(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_json(config_path)
    source_job = _from_config(config_path, config["source_job"])
    _validate_recurrence_config(config, source_job=source_job)
    output_dir = _from_config(config_path, config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=False)
    pins = _recurrence_pins(config_path, config, source_job=source_job)
    atomic_json(
        output_dir / "recurrence.json",
        {**config, "config_sha256": sha256_file(config_path), "runtime_pins": pins},
    )
    runtime_config = {
        **config,
        "_config_dir": str(config_path.parent),
        "_runtime_pins": pins,
        "_source_job": str(source_job),
    }
    registered = [
        (int(seed), arm) for seed in config["seeds"] for arm in config["arms"]
    ]
    ordered_rows: list[dict[str, Any] | None] = [None] * len(registered)
    identities: dict[tuple[str, int, str], dict[str, Any]] = {}
    max_parallel = int(config.get("max_parallel_arms") or 1)
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                _run_arm_in_declared_sdk,
                config=runtime_config,
                arm=arm,
                seed=seed,
                world_dir=None,
                sealed_dir=None,
                output_dir=output_dir,
                run_id=f"{arm['name']}-{seed}",
                request_extra={"kind": RECURRENCE_KIND},
            ): index
            for index, (seed, arm) in enumerate(registered)
        }
        for future in as_completed(futures):
            index = futures[future]
            row = future.result()
            ordered_rows[index] = row
            identities[(row["world_id"], row["seed"], row["arm"])] = row["identity"]
            atomic_json(output_dir / "runs" / f"{row['run_id']}.json", row)
    rows = [row for row in ordered_rows if row is not None]
    arm_order = [str(arm["name"]) for arm in config["arms"]]
    parity = _identity_parity(config, identities) if len(arm_order) == 2 else None
    if parity is not None:
        atomic_json(output_dir / "identity_parity.json", parity)
    return aggregate_recurrence(
        rows,
        arm_order=arm_order,
        output_dir=output_dir,
        confidence=float(config.get("confidence") or 0.90),
        pilot=bool(config.get("pilot")),
        identity_comparable=None if parity is None else bool(parity["comparable"]),
    )


def _validate_recurrence_config(config: dict[str, Any], *, source_job: Path) -> None:
    if config.get("schema_version") != RECURRENCE_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {RECURRENCE_SCHEMA_VERSION}")
    if config.get("kind") != RECURRENCE_KIND:
        raise ValueError(f"kind must be {RECURRENCE_KIND!r}")
    arms = list(config.get("arms") or [])
    if not 1 <= len(arms) <= 2:
        raise ValueError("recurrence runs take one or two arms")
    names = [str(arm.get("name") or "") for arm in arms]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError("arm names must be non-empty and unique")
    if "frozen" in names:
        raise ValueError("'frozen' is the built-in control, not an arm")
    if not config.get("seeds"):
        raise ValueError("recurrence runs require seeds")
    if not config.get("pilot") and len(config["seeds"]) < 4:
        raise ValueError("recurrence runs require at least four seeds unless pilot")
    max_parallel = int(config.get("max_parallel_arms") or 1)
    if not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel_arms must be between 1 and 8")
    unsupported = set(
        config.get("allowed_identity_differences") or ["model", "variant"]
    ) - set(SUPPORTED_IDENTITY_DIFFERENCES)
    if unsupported:
        raise ValueError(
            f"unsupported allowed_identity_differences: {sorted(unsupported)}"
        )
    window = _window(config)
    if window["loops"] < 1:
        raise ValueError("window.loops must be at least 1")
    if window["loop_days"] < _MIN_LOOP_DAYS:
        raise ValueError(f"window.loop_days must be at least {_MIN_LOOP_DAYS}")
    timeout = float(config.get("arm_timeout_seconds") or 43_200)
    if timeout < window["loops"] * 3_600:
        raise ValueError("arm_timeout_seconds must allow at least an hour per loop")
    for arm in arms:
        policy = {
            **dict(config.get("campaign") or {}),
            **dict(arm.get("campaign") or {}),
        }
        probation = dict(policy.get("probation") or {})
        burn_in_days = float(probation.get("burn_in_hours", 24)) / 24.0
        max_paired = int(probation.get("max_paired_days") or 14)
        # Probation carries across loops, so burn-in, the paired days, and
        # the partial cutoff day must fit inside the whole observation window
        # or no verdict can ever fire.
        if burn_in_days + max_paired + 1 > window["loops"] * window["loop_days"]:
            raise ValueError(
                f"arm {arm['name']!r}: probation cannot reach a verdict inside the "
                f"{window['loops']}x{window['loop_days']}-day window "
                "(burn_in_hours + max_paired_days)"
            )
    for relative in ("job.yaml", "workspace", str(BARS_RELATIVE)):
        if not (source_job / relative).exists():
            raise FileNotFoundError(f"source job is missing {relative}: {source_job}")


def _window(config: dict[str, Any]) -> dict[str, Any]:
    window = dict(config.get("window") or {})
    return {
        "start_cutoff": _parse(str(window["start_cutoff"])),
        "loop_days": int(window.get("loop_days") or 7),
        "loops": int(window.get("loops") or 3),
    }


def _recurrence_pins(
    config_path: Path, config: dict[str, Any], *, source_job: Path
) -> dict[str, Any]:
    runtime = {**config, "_config_dir": str(config_path.parent)}
    runtime_opencode_config = _resolve_runtime_opencode_config(runtime)
    window = _window(config)
    bars = load_json(source_job / BARS_RELATIVE)
    stamps = sorted(_parse(str(row["timestamp"])) for row in bars.get("bars") or [])
    if not stamps:
        raise ValueError("source job dataset has no bars")
    first_bar, last_bar = stamps[0], stamps[-1]
    start = window["start_cutoff"]
    end = start + timedelta(days=window["loop_days"] * window["loops"])
    if (start - first_bar).total_seconds() < _MIN_DEVELOPMENT_DAYS * 86_400:
        raise ValueError(
            f"start_cutoff needs {_MIN_DEVELOPMENT_DAYS:g} development days of bars"
        )
    if end > last_bar:
        raise ValueError("the observation window runs past the last frozen bar")
    return {
        "source": {
            "path": str(source_job),
            "revision": compute_workspace_revision(source_job),
            "bars_sha256": sha256_file(source_job / BARS_RELATIVE),
            "first_bar": first_bar.isoformat(),
            "last_bar": last_bar.isoformat(),
        },
        "window": {
            "start_cutoff": start.isoformat(),
            "loop_days": window["loop_days"],
            "loops": window["loops"],
            "cutoffs": [
                (start + timedelta(days=window["loop_days"] * index)).isoformat()
                for index in range(window["loops"])
            ],
            "end": end.isoformat(),
        },
        "arms": [
            {
                "name": str(arm["name"]),
                "sdk_root": str(_resolve_sdk_root(runtime, arm)),
                "sdk_ref": git_sha(_resolve_sdk_root(runtime, arm)),
                "campaign_sha256": sha256_json(dict(arm.get("campaign") or {})),
                "researcher": bool(arm.get("researcher", False)),
            }
            for arm in config["arms"]
        ],
        "wayfinder_api_base_url": str(config.get("wayfinder_api_base_url") or "")
        .strip()
        .rstrip("/"),
        "runtime_opencode_config": (
            {
                "path": str(runtime_opencode_config),
                "sha256": sha256_file(runtime_opencode_config),
            }
            if runtime_opencode_config is not None
            else None
        ),
    }


def run_recurrence_arm(
    *,
    config: dict[str, Any],
    arm: dict[str, Any],
    seed: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Every loop of one (arm, seed) chain, in this process, sharing one
    sandbox job. A loop that fails is recorded and the chain continues with
    the incumbent unchanged, exactly as a failed campaign changes nothing."""
    window = _window(config)
    source_job = Path(str(config["_source_job"]))
    run_id = f"{arm['name']}-{seed}"
    safe_run = _safe_name(run_id)
    loops_root = output_dir / "loops" / safe_run
    sealed_root = output_dir / "sealed" / safe_run
    world_id = _world_id(config, source_job=source_job)
    researcher_enabled = bool(arm.get("researcher", False))
    sandbox: dict[str, Any] | None = None
    frozen_bundle: Path | None = None
    frozen_environment: dict[str, Any] = {}
    frozen_revision: str | None = None
    first_manifest: dict[str, Any] = {}
    prompt_hashes: list[str] = []
    loop_rows: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    run_started = time.monotonic()
    for loop in range(window["loops"]):
        cutoff = window["start_cutoff"] + timedelta(days=window["loop_days"] * loop)
        end = cutoff + timedelta(days=window["loop_days"])
        loop_dir = loops_root / f"loop-{loop}"
        world_dir = loop_dir / "world"
        sealed_dir = sealed_root / f"loop-{loop}"
        incumbent_dir = loop_dir / "incumbent"
        row: dict[str, Any] = {
            "loop": loop,
            "cutoff": cutoff.isoformat(),
            "holdout_end": end.isoformat(),
            "world_id": f"{world_id}-loop{loop}",
            "invalid_reason": None,
        }
        loop_started = time.monotonic()
        try:
            source_bundle = source_job if sandbox is None else _job_root(sandbox)
            copy_job_bundle(source_bundle, incumbent_dir)
            _copy_loop_source_extras(
                source_job, source_bundle=source_bundle, incumbent_dir=incumbent_dir
            )
            manifest = prepare_world(
                incumbent_dir,
                world_dir,
                generation_cutoff=cutoff,
                holdout_end=end,
                sealed_dir=sealed_dir,
                world_id=row["world_id"],
                min_holdout_days=window["loop_days"],
            )
            world = load_world(world_dir, sealed_dir)
            if sandbox is None:
                sandbox = prepare_sandbox(
                    config=config,
                    arm=arm,
                    world_dir=world_dir,
                    sealed_dir=sealed_dir,
                    output_dir=output_dir,
                    run_id=run_id,
                    job_id=f"bench-{_safe_name(source_job.name)}-s{seed}",
                    verify_world_pin=False,
                )
                frozen_bundle = world_dir / "incumbent"
                frozen_environment = dict(manifest.get("execution_environment") or {})
                first_manifest = manifest
            else:
                install_development_world(world_dir, destination_job=_job_root(sandbox))
            # Revisions come from the sandbox job, not the world bundle: the
            # loop-0 install rewrites operator fields (id, modes), which would
            # otherwise read as an apply that never happened.
            incumbent_revision = compute_workspace_revision(_job_root(sandbox))
            if loop == 0:
                frozen_revision = incumbent_revision
            row["incumbent_revision"] = incumbent_revision
            row["frozen_revision"] = frozen_revision
            env = _arm_env(
                sandbox["run_root"],
                virtual_now=cutoff,
                api_base_url=str(config.get("wayfinder_api_base_url") or ""),
            )
            os.environ["WAYFINDER_BENCHMARK"] = env["WAYFINDER_BENCHMARK"]
            os.environ["WAYFINDER_BENCHMARK_NOW"] = env["WAYFINDER_BENCHMARK_NOW"]
            sessions: list[dict[str, Any]] = []
            stage_sessions: dict[str, str] = {}
            with bench_mcp_server(sandbox, env=env):
                row["researcher"] = (
                    _researcher_wake(
                        sandbox,
                        config=config,
                        env=env,
                        loop=loop,
                        sessions=sessions,
                    )
                    if researcher_enabled
                    else {"enabled": False}
                )
                invalid_reason = run_campaign_phase(
                    sandbox,
                    config=config,
                    seed=seed,
                    env=env,
                    virtual_now=cutoff,
                    sessions=sessions,
                    prompt_hashes=prompt_hashes,
                    stage_sessions=stage_sessions,
                    smoke=loop == 0 and bool(config.get("interface_smoke", True)),
                )
            store: JobStore = sandbox["store"]
            job_id = str(sandbox["job_id"])
            _preserve_finalize_record(store, job_id, loop_dir)
            state = campaign_status(store, job_id)
            campaign_id = str(state.get("campaign_id") or "") or None
            forward, holdout, invalid_reason = run_probation_phase(
                sandbox,
                world=world,
                world_dir=world_dir,
                sealed_dir=sealed_dir,
                invalid_reason=invalid_reason,
                campaign_id=campaign_id,
                holdout_output=loop_dir / "holdout",
            )
            if invalid_reason == "holdout_race_invalid":
                # The candidate-vs-incumbent race is the campaign's own record;
                # the loop's endpoint is the deployed strategy, scored below.
                invalid_reason = None
            rows = [
                *(world["development"].get("bars") or []),
                *(world["holdout"].get("bars") or []),
            ]
            # The deployed strategy may declare a derived column; score it
            # with the sandbox store, holdout rows revealed (idempotent).
            feature_root = None if sandbox is None else _job_root(sandbox)
            if feature_root is not None:
                reveal_holdout_features(sealed_dir, feature_root)
            deployed = evaluate_bundle(
                world_dir / "incumbent",
                rows=rows,
                cutoff=cutoff,
                environment=dict(manifest.get("execution_environment") or {}),
                feature_root=feature_root,
            )
            frozen = evaluate_bundle(
                frozen_bundle if frozen_bundle is not None else world_dir / "incumbent",
                rows=rows,
                cutoff=cutoff,
                environment=frozen_environment,
                feature_root=feature_root,
            )
            deltas = paired_daily_utility_deltas(
                deployed["daily_pnl"],
                frozen["daily_pnl"],
                capital=environment_capital(manifest.get("execution_environment")),
            )
            applied_trials = _apply_graduates(store, job_id, loop=loop)
            row["apply"] = {
                "applied": bool(applied_trials),
                "retire_to_flat": any(t.get("retire_to_flat") for t in applied_trials),
                "trials": applied_trials,
            }
            _write_forward_summary(store, job_id, deployed, cutoff=cutoff, end=end)
            row["probation_carried"] = _open_trial_ids(store, job_id)
            audit = audit_and_score(
                sandbox,
                sessions=sessions,
                started=loop_started,
                protected_roots=[world_dir, sealed_dir, incumbent_dir, source_job],
                invalid_reason=invalid_reason,
                forward=forward,
                holdout=holdout,
            )
            scorecard = audit["scorecard"]
            if researcher_enabled:
                row["researcher"]["campaign_hypotheses_citing"] = _ideation_usage(
                    store, job_id, state
                )
                row["researcher"]["campaign_seed_slots"] = sum(
                    1
                    for candidate in state.get("candidates") or []
                    if str(candidate.get("parent_source") or "") == "research_seed"
                )
            row.update(
                {
                    "invalid_reason": audit["invalid_reason"],
                    "campaign": {
                        "campaign_id": campaign_id,
                        "funnel": scorecard["funnel"],
                        "verdicts": scorecard["verdicts"],
                        "diversity": scorecard["diversity"],
                        "process": scorecard["process"],
                    },
                    "probation": forward,
                    "holdout": holdout,
                    "forward": {
                        "days": len(deltas),
                        "estimate": round(sum(deltas), 8),
                        "paired_deltas": [round(value, 10) for value in deltas],
                        "deployed_daily_pnl": deployed["daily_pnl"],
                        "frozen_daily_pnl": frozen["daily_pnl"],
                        "deployed_stats": deployed["stats"],
                        "frozen_stats": frozen["stats"],
                        "deployed_valid": bool(deployed["valid"]),
                        "frozen_valid": bool(frozen["valid"]),
                    },
                    "cost": audit["cost"],
                    "isolation": audit["isolation"],
                    "session_diagnostics": audit["session_diagnostics"],
                }
            )
            first_applied = applied_trials[0] if applied_trials else {}
            lineage.append(
                {
                    "loop": loop,
                    "revision_before": row["incumbent_revision"],
                    "revision_after": compute_workspace_revision(_job_root(sandbox)),
                    "applied": bool(applied_trials),
                    "trial_id": first_applied.get("trial_id"),
                    "candidate_id": first_applied.get("candidate_id"),
                    "family": first_applied.get("family"),
                    "trial_ids": [trial["trial_id"] for trial in applied_trials],
                    "candidate_ids": [
                        trial.get("candidate_id") for trial in applied_trials
                    ],
                }
            )
        except Exception as exc:  # noqa: BLE001 - one loop must not end the chain
            if sandbox is None:
                raise
            row["invalid_reason"] = f"{type(exc).__name__}: {exc}"[:400]
            _terminate_active_campaign(sandbox)
            lineage.append(
                {
                    "loop": loop,
                    "revision_before": row.get("incumbent_revision"),
                    "revision_after": compute_workspace_revision(_job_root(sandbox)),
                    "applied": False,
                    "trial_id": None,
                    "candidate_id": None,
                    "family": None,
                    "trial_ids": [],
                    "candidate_ids": [],
                }
            )
        row["wall_seconds"] = round(time.monotonic() - loop_started, 3)
        atomic_json(loop_dir / "loop.json", row)
        loop_rows.append(row)
    if sandbox is None:
        raise RuntimeError("recurrence chain produced no loops")
    return _run_row(
        config=config,
        arm=arm,
        seed=seed,
        run_id=run_id,
        world_id=world_id,
        sandbox=sandbox,
        loops=loop_rows,
        lineage=lineage,
        prompt_hashes=prompt_hashes,
        first_manifest=first_manifest,
        elapsed=round(time.monotonic() - run_started, 3),
    )


def _world_id(config: dict[str, Any], *, source_job: Path) -> str:
    window = _window(config)
    stamp = window["start_cutoff"].strftime("%Y%m%d")
    return (
        f"{_safe_name(source_job.name)}-recur-{stamp}-"
        f"{window['loops']}x{window['loop_days']}d"
    )


def _job_root(sandbox: dict[str, Any]) -> Path:
    store: JobStore = sandbox["store"]
    return store.job_dir(str(sandbox["job_id"]))


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _terminate_active_campaign(sandbox: dict[str, Any]) -> None:
    store: JobStore = sandbox["store"]
    job_id = str(sandbox["job_id"])
    campaign_id = str(campaign_status(store, job_id).get("campaign_id") or "")
    if campaign_id:
        terminate_campaign_ops(store, job_id, campaign_id)


def _researcher_wake(
    sandbox: dict[str, Any],
    *,
    config: dict[str, Any],
    env: dict[str, str],
    loop: int,
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """One production intervene wake on internal evidence, measured by what
    it leaves behind: the ideation artifact, the agenda, submitted seeds."""
    root = _job_root(sandbox)
    researcher = dict(config.get("researcher") or {})
    before = _research_fingerprint(root)
    wake = run_agent_wakes(
        sandbox=sandbox["run_root"],
        job_id=str(sandbox["job_id"]),
        wakes=1,
        model=sandbox["model"],
        opencode=sandbox["opencode"],
        agent=str(researcher.get("agent") or "wayfinder-job-worker"),
        timeout_s=int(researcher.get("timeout_seconds") or 1_200),
        variant=sandbox["variant"],
        env=env,
        title=f"bench-research-{_safe_name(str(sandbox['run_id']))}-loop-{loop}",
    )[0]
    sessions.append({"stage": "research-wake", **wake})
    clock = env.get("WAYFINDER_BENCHMARK_NOW")
    ideation = _ideation_report(root, expected_clock=clock)
    retried = False
    if not ideation["valid"]:
        # One bounded corrective turn in the same session, naming exactly
        # what the contract found missing. Production gets the same text
        # on its next scheduled wake through the journaled problems.
        retried = True
        correction = run_agent_prompt(
            sandbox=sandbox["run_root"],
            prompt=_ideation_correction_prompt(ideation, clock=str(clock or "")),
            model=sandbox["model"],
            variant=sandbox["variant"],
            title=f"bench-research-{_safe_name(str(sandbox['run_id']))}-loop-{loop}-fix",
            session_id=wake.get("session_id"),
            opencode=sandbox["opencode"],
            agent=str(researcher.get("agent") or "wayfinder-job-worker"),
            timeout_s=int(researcher.get("timeout_seconds") or 1_200),
            env=env,
        )
        sessions.append({"stage": "research-wake-fix", **correction})
        ideation = _ideation_report(root, expected_clock=clock)
        if not ideation["valid"] and bool(researcher.get("required")):
            raise RuntimeError(
                "researcher artifact invalid after one correction: "
                + "; ".join(ideation.get("problems") or [])
            )
    after = _research_fingerprint(root)
    return {
        "enabled": True,
        "exit_code": wake.get("exit_code"),
        "ideation_artifact": ideation,
        "retried": retried,
        "agenda_changed": before["agenda"] != after["agenda"],
        "seeds_submitted": max(after["seeds"] - before["seeds"], 0),
        "campaign_seed_slots": 0,
        "campaign_hypotheses_citing": 0,
    }


def _ideation_correction_prompt(report: Mapping[str, Any], *, clock: str) -> str:
    problems = "; ".join(str(problem) for problem in report.get("problems") or [])
    return (
        "Your expedition did not deliver its artifact. Mechanical check of "
        "research/ideation/latest.json: "
        + (problems or "file missing")
        + ". Write research/ideation/latest.json now with exactly this shape: "
        '{"generated_at": "'
        + clock
        + '", "sources_consulted": [{"tool": <file path or core_jobs action>, '
        '"query": ..., "takeaway": ...}] (at least 3 distinct), "hypotheses": '
        '[{"title": ..., "thesis": ..., "bucket": "testable"|"starved"|"refuted", '
        '"next_step": ...}] (at least 3, ranked best-first)}. Use the evidence you '
        "already read this session; then fold a compact summary into "
        "research/agenda.md. Do nothing else."
    )


def _ideation_usage(store: JobStore, job_id: str, state: Mapping[str, Any]) -> int:
    """Design hypotheses that ground themselves on the researcher's artifact."""
    design = (
        store.read_json(job_id, str(state.get("campaign_design") or ""), default={})
        or {}
    )
    return sum(
        1
        for hypothesis in design.get("hypotheses") or []
        if any(
            str(ref).startswith("/research_ideation/")
            for ref in hypothesis.get("evidence_refs") or []
        )
    )


def _research_fingerprint(root: Path) -> dict[str, Any]:
    agenda = root / AGENDA_RELATIVE
    seeds_path = root / RESEARCH_SEEDS_RELATIVE
    seeds = load_json(seeds_path) if seeds_path.exists() else {}
    seed_rows = seeds.get("seeds") if isinstance(seeds, dict) else seeds
    return {
        "agenda": sha256_file(agenda) if agenda.exists() else None,
        "seeds": len(seed_rows or []),
    }


def _ideation_report(
    root: Path, *, expected_clock: str | None = None
) -> dict[str, Any]:
    path = root / IDEATION_RELATIVE
    if not path.exists():
        return {
            "present": False,
            "valid": False,
            "problems": ["research/ideation/latest.json is missing"],
            "hypotheses": 0,
            "sources": 0,
        }
    doc = load_json(path)
    if not isinstance(doc, dict):
        return {
            "present": True,
            "valid": False,
            "problems": ["research/ideation/latest.json is not a JSON object"],
            "hypotheses": 0,
            "sources": 0,
        }
    return {
        "present": True,
        **validate_ideation_artifact(doc, expected_clock=expected_clock),
    }


def _apply_graduates(
    store: JobStore, job_id: str, *, loop: int
) -> list[dict[str, Any]]:
    """The declared owner rule: every graduate is applied, nothing else is.
    Probation carries across loops, so a trial staged loops ago applies in
    the loop it graduates."""
    doc = load_probation(store, job_id)
    applied_rows: list[dict[str, Any]] = []
    for trial in doc.get("trials") or []:
        if (
            trial.get("status") != "graduated"
            or (trial.get("promotion") or {}).get("status") == "applied"
        ):
            continue
        candidate_dir = resolve_probation_bundle(store, job_id, trial["candidate"])
        applied = apply_candidate_bundle(
            store,
            job_id,
            candidate_dir,
            label=f"recur-loop{loop}-{_safe_name(str(trial['trial_id']))[:40]}",
        )
        trial["promotion"] = {
            "status": "applied",
            "proposal_id": None,
            "applied_by": "bench_recurrence",
            "loop": loop,
            "promoted_revision": applied["promoted_revision"],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        store.write_json(job_id, PROBATION_PATH, doc)
        set_incumbent(store, job_id, str(trial["candidate_id"]))
        applied_rows.append(
            {
                "trial_id": trial["trial_id"],
                "candidate_id": trial.get("candidate_id"),
                "candidate_revision": trial.get("candidate_revision"),
                "family": trial.get("family"),
                "promoted_revision": applied["promoted_revision"],
                "backup_dir": applied["backup_dir"],
            }
        )
    if not applied_rows and not _open_trial_ids(store, job_id):
        verdict = campaign_status(store, job_id).get("retire_to_flat") or {}
        if verdict.get("recommended"):
            # The declared owner rule for the bench: a campaign that found
            # nothing while the incumbent lost to cash retires it to cash.
            flat_dir = flat_bundle(
                store,
                job_id,
                store.job_dir(job_id)
                / "research"
                / "evolution"
                / "flat"
                / f"loop{loop}",
            )
            applied = apply_candidate_bundle(
                store, job_id, flat_dir, label=f"recur-loop{loop}-retire-to-flat"
            )
            applied_rows.append(
                {
                    "trial_id": None,
                    "candidate_id": "flat",
                    "candidate_revision": compute_workspace_revision(flat_dir),
                    "family": "flat",
                    "promoted_revision": applied["promoted_revision"],
                    "backup_dir": applied["backup_dir"],
                    "retire_to_flat": True,
                    "reason": verdict.get("reason"),
                }
            )
    return applied_rows


_FINALIZE_RECORD_FILES = (
    "evolution_finalize.json",
    "evolution_finalize.log",
    "evolution_finalize.result.json",
)


def _copy_loop_source_extras(
    source_job: Path, *, source_bundle: Path, incumbent_dir: Path
) -> dict[str, bool]:
    """What copy_job_bundle leaves behind and a loop's world needs: the bars
    and frozen leader closes from the source job, and the sandbox job's
    feature store (macro and leader rows through this loop's cutoff, which
    a graduated candidate may declare)."""
    shutil.copy2(
        source_job / BARS_RELATIVE, _ensure_parent(incumbent_dir / BARS_RELATIVE)
    )
    copied = {"leaders": False, "features": False}
    if (source_job / LEADER_CLOSES_RELATIVE).exists():
        shutil.copy2(
            source_job / LEADER_CLOSES_RELATIVE,
            _ensure_parent(incumbent_dir / LEADER_CLOSES_RELATIVE),
        )
        copied["leaders"] = True
    store_file = source_bundle / DEFAULT_FEATURES_PATH
    if store_file.exists():
        shutil.copy2(store_file, _ensure_parent(incumbent_dir / DEFAULT_FEATURES_PATH))
        copied["features"] = True
    return copied


def _preserve_finalize_record(store: JobStore, job_id: str, loop_dir: Path) -> None:
    """Keep this loop's detached finalize record; the next loop's finalize
    overwrites the job's copy, which is how the first pilot lost the trace
    of a finalize that died two hours before anyone looked."""
    ops_dir = store.job_dir(job_id) / "state" / "background_ops"
    target = loop_dir / "finalize"
    for name in _FINALIZE_RECORD_FILES:
        source = ops_dir / name
        if source.exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)


def _write_forward_summary(
    store: JobStore,
    job_id: str,
    deployed: dict[str, Any],
    *,
    cutoff: datetime,
    end: datetime,
) -> None:
    """The deployed strategy's replayed window in the forward-summary shape
    the diagnostic pack and the wake read. ``runs.count`` stays 0 on purpose:
    a positive count marks the job live-capable and changes governance."""
    stats = dict(deployed.get("stats") or {})
    trades = list(deployed.get("trades") or [])
    net_pnl = round(sum(float(value) for value in deployed["daily_pnl"].values()), 6)
    # Trade rows are fills (size, price, fee); realized pnl is only known at
    # the strategy level, so per symbol we report activity and fees.
    by_symbol: dict[str, dict[str, float]] = {}
    for trade in trades:
        symbol = str(trade.get("symbol") or "")
        cell = by_symbol.setdefault(symbol, {"fills": 0.0, "fees": 0.0})
        cell["fills"] += 1
        cell["fees"] += float(trade.get("fee") or 0.0)
    summary = default_forward_summary(job_id, inception_at=cutoff.isoformat())
    summary.update(
        {
            "mode": "paper",
            "status": "replayed",
            "source": "bench_recurrence_replay",
            "started_at": cutoff.isoformat(),
            "last_tick_at": end.isoformat(),
            "trade_count": int(stats.get("trade_count") or 0),
            "net_pnl": net_pnl,
            "strategy_net_pnl": net_pnl,
            "operational_net_pnl": 0.0,
            "total_fees": round(float(stats.get("total_fees") or 0.0), 6),
            "by_symbol": {
                symbol: {key: round(value, 6) for key, value in cell.items()}
                for symbol, cell in by_symbol.items()
            },
        }
    )
    summary["trades"] = {
        **summary["trades"],
        "count": int(stats.get("trade_count") or 0),
        "net_pnl": net_pnl,
    }
    store.write_json(job_id, FORWARD_SUMMARY_RELATIVE, summary)


def _open_trial_ids(store: JobStore, job_id: str) -> list[str]:
    """Trials without a verdict carry into the next loop's replay."""
    doc = load_probation(store, job_id)
    return [
        str(trial["trial_id"])
        for trial in doc.get("trials") or []
        if trial.get("status") in _OPEN_TRIAL_STATUSES
    ]


def _run_row(
    *,
    config: dict[str, Any],
    arm: dict[str, Any],
    seed: int,
    run_id: str,
    world_id: str,
    sandbox: dict[str, Any],
    loops: list[dict[str, Any]],
    lineage: list[dict[str, Any]],
    prompt_hashes: list[str],
    first_manifest: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    confidence = float(config.get("confidence") or 0.90)
    values = [
        value
        for row in loops
        for value in ((row.get("forward") or {}).get("paired_deltas") or [])
    ]
    invalid_loops = [row["loop"] for row in loops if row.get("invalid_reason")]
    applied_loops = [
        row["loop"] for row in loops if (row.get("apply") or {}).get("applied")
    ]
    forward_status = [
        str((row.get("probation") or {}).get("status") or "") for row in loops
    ]
    cost = {
        key: round(
            sum(float((row.get("cost") or {}).get(key) or 0) for row in loops), 3
        )
        for key in _COST_SUM_KEYS
    }
    cost["wall_seconds"] = elapsed
    isolation_rows = [row.get("isolation") or {} for row in loops]
    identity = runtime_identity(
        sdk_ref=_arm_runtime_pin(config, arm)["sdk_ref"],
        sandbox=sandbox["run_root"],
        model=sandbox["model"],
        variant=sandbox["variant"],
        repeat_seed=seed,
        world_manifest=first_manifest,
        prompt_hashes=prompt_hashes,
        declared_differences=list(
            config.get("allowed_identity_differences") or ["model", "variant"]
        ),
        arm_parameters={
            "campaign": sandbox["arm_campaign"],
            "researcher": bool(arm.get("researcher", False)),
        },
        opencode=sandbox["opencode"],
    )
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "kind": RECURRENCE_KIND,
        "run_id": run_id,
        "arm": str(arm["name"]),
        "world_id": world_id,
        "seed": seed,
        "model": sandbox["model"],
        "variant": sandbox["variant"],
        "invalid_reason": (
            "every loop invalid" if loops and len(invalid_loops) == len(loops) else None
        ),
        "loops": loops,
        "lineage": {"chain": lineage, "depth": len(applied_loops)},
        "invalid_loops": invalid_loops,
        "holdout": {
            "rule": (
                "deployed incumbent minus frozen original, paired daily log-growth "
                "utility over every loop's holdout; loop 0 is zero by construction"
            ),
            "paired_daily_utility_delta": {
                "days": len(values),
                "estimate": round(sum(values), 8),
                "lcb": block_bootstrap_lcb(
                    values, block_len=5, iterations=500, confidence=confidence
                ),
                "values": [round(value, 10) for value in values],
            },
        },
        "dynamics": {
            "loops": len(loops),
            "staged": sum(
                int(
                    ((row.get("campaign") or {}).get("funnel") or {}).get("staged") or 0
                )
                for row in loops
            ),
            "graduated": forward_status.count("graduated"),
            "killed": forward_status.count("killed"),
            "inconclusive": forward_status.count("inconclusive"),
            "applied": len(applied_loops),
            "time_to_first_apply": applied_loops[0] if applied_loops else None,
            "false_applies": _false_applies(loops),
            "invalid_loops": len(invalid_loops),
        },
        "researcher": _researcher_totals(loops, enabled=bool(arm.get("researcher"))),
        "funnel": {
            key: sum(
                int(((row.get("campaign") or {}).get("funnel") or {}).get(key) or 0)
                for row in loops
            )
            for key in _FUNNEL_SUM_KEYS
        },
        "forward": {"status": forward_status[-1] if forward_status else None},
        "process": {
            key: sum(
                int(((row.get("campaign") or {}).get("process") or {}).get(key) or 0)
                for row in loops
            )
            for key in _PROCESS_SUM_KEYS
        },
        "diversity": (loops[-1].get("campaign") or {}).get("diversity") or {},
        "cost": cost,
        "isolation": {
            "passed": all(bool(row.get("passed", True)) for row in isolation_rows),
            "denied_attempts": [
                attempt
                for row in isolation_rows
                for attempt in (row.get("denied_attempts") or [])
            ],
        },
        "identity": identity,
        "workspace": str(sandbox["run_root"]),
    }


def _false_applies(loops: list[dict[str, Any]]) -> int:
    """An apply at loop k whose deployed-minus-frozen estimate on loop k+1's
    holdout is negative: the gate let through something that then lost."""
    count = 0
    for index, row in enumerate(loops[:-1]):
        if not (row.get("apply") or {}).get("applied"):
            continue
        following = loops[index + 1].get("forward") or {}
        if float(following.get("estimate") or 0.0) < 0:
            count += 1
    return count


def _researcher_totals(loops: list[dict[str, Any]], *, enabled: bool) -> dict[str, Any]:
    reports = [row.get("researcher") or {} for row in loops]
    return {
        "enabled": enabled,
        "wakes": sum(1 for report in reports if report.get("enabled")),
        "ideation_valid": sum(
            1
            for report in reports
            if (report.get("ideation_artifact") or {}).get("valid")
        ),
        "agenda_changed": sum(1 for report in reports if report.get("agenda_changed")),
        "retried": sum(1 for report in reports if report.get("retried")),
        "campaign_hypotheses_citing": sum(
            int(report.get("campaign_hypotheses_citing") or 0) for report in reports
        ),
        "seeds_submitted": sum(
            int(report.get("seeds_submitted") or 0) for report in reports
        ),
        "campaign_seed_slots": sum(
            int(report.get("campaign_seed_slots") or 0) for report in reports
        ),
    }


def aggregate_recurrence(
    rows: list[dict[str, Any]],
    *,
    arm_order: list[str],
    output_dir: Path | None = None,
    confidence: float = 0.90,
    pilot: bool = False,
    identity_comparable: bool | None = None,
) -> dict[str, Any]:
    """Process versus frozen per arm (seeds are replicates), the per-loop
    dynamics, and, for two arms, the paired experiment verdict."""
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in arm_order:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        totals = [
            float((row.get("holdout") or {})["paired_daily_utility_delta"]["estimate"])
            for row in arm_rows
            if not row.get("invalid_reason")
        ]
        lcb = block_bootstrap_lcb(
            totals, block_len=1, iterations=1_000, confidence=confidence
        )
        reverse = block_bootstrap_lcb(
            [-value for value in totals],
            block_len=1,
            iterations=1_000,
            confidence=confidence,
        )
        ucb = -reverse if reverse is not None else None
        applies = sum(
            int((row.get("dynamics") or {}).get("applied") or 0) for row in arm_rows
        )
        retire_to_flat_applies = sum(
            int(bool((row.get("apply") or {}).get("retire_to_flat")))
            for row in arm_rows
        )
        if not totals:
            direction = "invalid_arm_runs"
        elif applies == 0:
            direction = "no_applies"
        elif lcb is not None and lcb > 0:
            direction = "process_beats_frozen"
        elif ucb is not None and ucb < 0:
            direction = "frozen_beats_process"
        else:
            direction = "no_significant_difference"
        by_arm[arm] = {
            "runs": len(arm_rows),
            "invalid_runs": sum(bool(row.get("invalid_reason")) for row in arm_rows),
            "per_seed": [
                {
                    "seed": row.get("seed"),
                    "estimate": (row.get("holdout") or {})[
                        "paired_daily_utility_delta"
                    ]["estimate"],
                    "lcb": (row.get("holdout") or {})["paired_daily_utility_delta"][
                        "lcb"
                    ],
                    "nonzero_days": sum(
                        1
                        for value in (row.get("holdout") or {})[
                            "paired_daily_utility_delta"
                        ]["values"]
                        if value != 0
                    ),
                    "dynamics": row.get("dynamics"),
                    "lineage_depth": ((row.get("lineage") or {}).get("depth") or 0),
                    "invalid_reason": row.get("invalid_reason"),
                }
                for row in arm_rows
            ],
            "estimate": round(sum(totals) / len(totals), 8) if totals else 0.0,
            "lcb": lcb,
            "ucb": ucb,
            "applies": applies,
            "retire_to_flat_applies": retire_to_flat_applies,
            "false_applies": sum(
                int((row.get("dynamics") or {}).get("false_applies") or 0)
                for row in arm_rows
            ),
            "staged": sum(
                int((row.get("dynamics") or {}).get("staged") or 0) for row in arm_rows
            ),
            "graduated": sum(
                int((row.get("dynamics") or {}).get("graduated") or 0)
                for row in arm_rows
            ),
            "invalid_loops": sum(
                int((row.get("dynamics") or {}).get("invalid_loops") or 0)
                for row in arm_rows
            ),
            "researcher": _sum_researcher(arm_rows),
            "tokens_total": sum(
                int((row.get("cost") or {}).get(key) or 0)
                for row in arm_rows
                for key in ("tokens_in", "tokens_out", "tokens_reasoning")
            ),
            "wall_seconds": round(
                sum(
                    float((row.get("cost") or {}).get("wall_seconds") or 0)
                    for row in arm_rows
                ),
                3,
            ),
            "direction": direction,
            "decision": (
                "pilot_directional_only"
                if pilot and direction not in {"invalid_arm_runs", "no_applies"}
                else direction
            ),
        }
    report: dict[str, Any] = {
        "schema_version": RECURRENCE_SCHEMA_VERSION,
        "kind": RECURRENCE_KIND,
        "arms": arm_order,
        "pilot": pilot,
        "pre_registered_rule": {
            "primary": (
                "per arm: deployed-minus-frozen paired daily log-growth utility, "
                "summed over every loop's holdout per seed; seeds are replicates "
                "and the interval is bootstrapped over per-seed totals"
            ),
            "confidence": confidence,
            "readings": {
                "no_applies": "process == frozen; endpoint uninformative; read the dynamics",
                "frozen_beats_process": "the graduation gate let through losers",
                "process_beats_frozen": "evolution compounded within the window",
            },
        },
        "by_arm": by_arm,
        "by_loop": _by_loop(rows, arm_order),
    }
    if len(arm_order) == 2:
        pairwise = aggregate_experiment(
            rows,
            arm_order=arm_order,
            output_dir=None,
            confidence=confidence,
            max_cost_ratio=float("inf"),
            pilot=pilot,
        )
        if identity_comparable is False:
            pairwise["decision"] = "invalid_identity_mismatch"
        report["pairwise"] = {
            key: pairwise[key] for key in ("primary", "decision", "invalid_runs")
        }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(output_dir / "aggregate.json", report)
        (output_dir / "report.txt").write_text(_format_report(report), encoding="utf-8")
    return report


def _sum_researcher(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reports = [row.get("researcher") or {} for row in rows]
    keys = (
        "wakes",
        "ideation_valid",
        "retried",
        "agenda_changed",
        "seeds_submitted",
        "campaign_seed_slots",
        "campaign_hypotheses_citing",
    )
    return {
        "enabled": any(report.get("enabled") for report in reports),
        **{key: sum(int(report.get(key) or 0) for report in reports) for key in keys},
    }


def _by_loop(rows: list[dict[str, Any]], arm_order: list[str]) -> list[dict[str, Any]]:
    loops = max((len(row.get("loops") or []) for row in rows), default=0)
    table = []
    for index in range(loops):
        entry: dict[str, Any] = {"loop": index}
        for arm in arm_order:
            cells = [
                (row.get("loops") or [])[index]
                for row in rows
                if row.get("arm") == arm and len(row.get("loops") or []) > index
            ]
            valid = [cell for cell in cells if not cell.get("invalid_reason")]
            estimates = [
                float((cell.get("forward") or {}).get("estimate") or 0)
                for cell in valid
            ]
            entry[arm] = {
                "cells": len(cells),
                "invalid": len(cells) - len(valid),
                "staged": sum(
                    int(
                        ((cell.get("campaign") or {}).get("funnel") or {}).get("staged")
                        or 0
                    )
                    for cell in valid
                ),
                "graduated": sum(
                    1
                    for cell in valid
                    if (cell.get("probation") or {}).get("status") == "graduated"
                ),
                "applied": sum(
                    1 for cell in valid if (cell.get("apply") or {}).get("applied")
                ),
                "mean_estimate": (
                    round(sum(estimates) / len(estimates), 8) if estimates else 0.0
                ),
            }
        table.append(entry)
    return table


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        "Recurrence: process vs frozen incumbent",
        f"pilot: {report['pilot']}",
        "",
    ]
    for arm, stats in report["by_arm"].items():
        lines.append(
            f"arm {arm}: decision={stats['decision']} estimate={stats['estimate']} "
            f"lcb={stats['lcb']} ucb={stats['ucb']} applies={stats['applies']} "
            f"false_applies={stats['false_applies']} staged={stats['staged']} "
            f"graduated={stats['graduated']} invalid_loops={stats['invalid_loops']} "
            f"tokens={stats['tokens_total']} wall_s={stats['wall_seconds']}"
        )
        researcher = stats["researcher"]
        if researcher.get("enabled"):
            lines.append(
                f"  researcher: wakes={researcher['wakes']} "
                f"ideation_valid={researcher['ideation_valid']} "
                f"agenda_changed={researcher['agenda_changed']} "
                f"seeds_submitted={researcher['seeds_submitted']} "
                f"campaign_seed_slots={researcher['campaign_seed_slots']}"
            )
        for seed_row in stats["per_seed"]:
            lines.append(
                f"  seed {seed_row['seed']}: estimate={seed_row['estimate']} "
                f"lcb={seed_row['lcb']} nonzero_days={seed_row['nonzero_days']} "
                f"lineage_depth={seed_row['lineage_depth']} "
                f"invalid={seed_row['invalid_reason']}"
            )
    lines.append("")
    for entry in report["by_loop"]:
        cells = ", ".join(
            f"{arm}: staged={cell['staged']} graduated={cell['graduated']} "
            f"applied={cell['applied']} mean_estimate={cell['mean_estimate']} "
            f"invalid={cell['invalid']}/{cell['cells']}"
            for arm, cell in entry.items()
            if arm != "loop"
        )
        lines.append(f"loop {entry['loop']}: {cells}")
    if "pairwise" in report:
        pairwise = report["pairwise"]
        lines.append("")
        lines.append(
            f"pairwise ({' vs '.join(report['arms'])}): decision={pairwise['decision']} "
            f"estimate={pairwise['primary']['estimate']} lcb={pairwise['primary']['lcb']} "
            f"ucb={pairwise['primary']['ucb']} pairs={pairwise['primary']['pairs']}"
        )
    return "\n".join(lines) + "\n"

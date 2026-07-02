"""Structured propose flow: pre-approval candidates with evidence.

`propose_change` is the sanctioned way for an agent (or human) to put a
strategy change in front of the user: it stages the change as a candidate
workspace BEFORE approval, runs the full candidate validation (backtest +
preflight + execution validation, all revision-stamped), builds a
baseline-vs-candidate comparison, and attaches a bounded `candidate_report`
to the proposal — the exact payload the backend approve gate and the FE
review UI consume (contract C1).

The pre-approval candidate is reused at claim time (application.py verifies
its recorded revision), so the change the user approved is byte-for-byte the
change that gets promoted.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.application import (
    _prepare_candidate_workspace,
    ensure_jobs_v1_contract,
    validate_candidate_bundle,
)
from wayfinder_paths.jobs.execution.job import (
    backtest_execution_job,
    synthesize_scenario_plan,
)
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.validation import validation_summary
from wayfinder_paths.jobs.worker import JOB_RESULT_MARKER

PROPOSAL_KINDS = {"code_change", "params_update", "model_update"}


def propose_change(
    store: JobStore,
    job_id: str,
    *,
    kind: str,
    summary: str,
    intent_contract: dict[str, Any],
    params: dict[str, Any] | None = None,
    candidate_source: str | Path | None = None,
    scenario_plan: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Create a pending proposal backed by a validated pre-approval candidate.

    The change is supplied either as `params` (merged into the candidate
    job.yaml execution_params) or `candidate_source` (a directory the agent
    already edited — a full bundle with `workspace/` [+ `job.yaml`] or a bare
    workspace tree). Exactly one is required for code/params changes.

    `memo` is the human-facing markdown proposal memo (status quo / what the
    data shows / proposed change / expected impact / risks / validation /
    approval requested). It is written to `proposals/<pid>.md` and carried in
    the proposal's `change_summary`, which the backend already surfaces to
    the review UI — light surfacing with zero backend/FE changes.
    """
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(PROPOSAL_KINDS)}: {kind}")
    if params is None and candidate_source is None:
        raise ValueError("pass params and/or candidate_source — nothing to propose")
    ensure_jobs_v1_contract(store, job_id)

    root = store.job_dir(job_id)
    base_revision = compute_workspace_revision(root)
    pid = proposal_id or f"prop-{kind.replace('_', '-')}-{uuid.uuid4().hex[:8]}"

    candidate_descriptor = _prepare_candidate_workspace(
        store, job_id, pid, force_fresh=True
    )
    candidate_dir = store.repo_root / candidate_descriptor["candidate_dir"]
    if candidate_source is not None:
        _overlay_candidate_source(candidate_dir, Path(candidate_source))
    if params:
        _merge_candidate_params(candidate_dir, params)
    changed_files = _diff_workspaces(root, candidate_dir)

    job = store.load(job_id)
    resolved_plan = scenario_plan
    if not resolved_plan:
        job_data = json.loads(json.dumps(job.to_dict(), default=str))
        resolved_plan = job_data.get("execution_scenario_plan") or (
            (job_data.get("execution_spec") or {}).get("validation") or {}
        ).get("execution_scenario_plan")
        if not resolved_plan and job.script_loop.enabled:
            resolved_plan = synthesize_scenario_plan(
                root,
                ExecutionSpec.from_dict(dict(job.execution_spec or {})),
                job_data,
            )

    proposal: dict[str, Any] = {
        "proposal_id": pid,
        "job_id": job_id,
        "status": "pending",
        "kind": kind,
        "proposed_change": {
            "summary": summary,
            **({"execution_params": dict(params)} if params else {}),
        },
        "intent_contract": dict(intent_contract),
        "scenario_plan": resolved_plan or {"scenarios": []},
        "base_revision": base_revision,
        "changed_files": changed_files,
        "change_summary": memo or summary,
        "application": {"status": "not_requested", **candidate_descriptor},
    }
    if memo:
        memo_path = store.job_dir(job_id) / "proposals" / f"{pid}.md"
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text(memo.rstrip() + "\n", encoding="utf-8")

    validation = validate_candidate_bundle(store, job_id, proposal, candidate_dir)
    candidate_revision = compute_workspace_revision(candidate_dir)
    comparison = _build_comparison(
        store, job_id, candidate_dir, base_revision=base_revision, pid=pid
    )
    if comparison is not None:
        gate = evaluate_live_gate(job_id, candidate_dir=candidate_dir, store=store)
        gate_payload = {
            "live_ready": gate.get("live_ready"),
            "reasons": gate.get("reasons") or [],
        }
        mode = "full"
    else:
        # Research-only / no-dataset jobs: nothing to backtest or gate — the
        # proposal is judged on validation alone (contract C1).
        gate_payload = {
            "live_ready": None,
            "reasons": ["no execution backtest; validation-only proposal"],
        }
        mode = "validation_only"

    proposal["candidate_report"] = {
        "revision": candidate_revision,
        "base_revision": base_revision,
        "mode": mode,
        "gate": gate_payload,
        "validation_summary": validation_summary(validation),
        "comparison": _bounded_comparison(comparison),
        "generated_at": utc_now_iso(),
    }

    store.write_proposal(job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "proposal_created",
            "proposal_id": pid,
            "kind": kind,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "validation_status": validation.get("status"),
        },
    )
    store.refresh_scorecard(job_id)
    sync_all_jobs(store=store)
    # Surface a chat affordance (contract C5): the opencode harness turns this
    # marker into a job_result part; the FE renders a review deep-link chip.
    print(
        JOB_RESULT_MARKER
        + json.dumps(
            {
                "type": "job_result",
                "severity": "info",
                "summary": f"Proposal created: {summary}",
                "job_id": job_id,
                "proposal_id": pid,
            }
        )
    )
    return store.load_proposal(job_id, pid)


def _overlay_candidate_source(candidate_dir: Path, source: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"candidate_source not found: {source}")
    if (source / "workspace").exists():
        # Full bundle shape: workspace/ (+ optional job.yaml).
        _replace_tree(source / "workspace", candidate_dir / "workspace")
        if (source / "job.yaml").exists():
            shutil.copy2(source / "job.yaml", candidate_dir / "job.yaml")
        return
    # Bare workspace tree.
    _replace_tree(source, candidate_dir / "workspace")


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _merge_candidate_params(candidate_dir: Path, params: dict[str, Any]) -> None:
    yaml_path = candidate_dir / "job.yaml"
    job_yaml = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    execution_params = dict(job_yaml.get("execution_params") or {})
    execution_params.update(params)
    job_yaml["execution_params"] = execution_params
    yaml_path.write_text(yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8")


def _diff_workspaces(root: Path, candidate_dir: Path) -> list[str]:
    """Repo-relative-ish (candidate-relative) list of files whose bytes differ
    between the active bundle and the candidate. Bounded to keep the synced
    proposal payload small."""
    changed: list[str] = []
    active_ws = root / "workspace"
    candidate_ws = candidate_dir / "workspace"
    seen: set[str] = set()
    for base, other, prefix in (
        (candidate_ws, active_ws, "workspace"),
        (active_ws, candidate_ws, "workspace"),
    ):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = f"{prefix}/{path.relative_to(base)}"
            if rel in seen:
                continue
            seen.add(rel)
            counterpart = other / path.relative_to(base)
            if (
                not counterpart.exists()
                or counterpart.read_bytes() != path.read_bytes()
            ):
                changed.append(rel)
    active_yaml = root / "job.yaml"
    candidate_yaml = candidate_dir / "job.yaml"
    if (
        active_yaml.exists()
        and candidate_yaml.exists()
        and active_yaml.read_bytes() != candidate_yaml.read_bytes()
    ):
        changed.append("job.yaml")
    return sorted(changed)[:100]


def _build_comparison(
    store: JobStore,
    job_id: str,
    candidate_dir: Path,
    *,
    base_revision: str,
    pid: str,
) -> dict[str, Any] | None:
    """Baseline-vs-candidate backtest comparison on the same dataset.

    Candidate side reuses the artifact candidate validation just wrote (no
    second sim). Baseline reuses the active latest.json when it matches the
    base revision; otherwise one fresh backtest re-stamps it. Returns None
    when the job has nothing to backtest (research-only jobs)."""
    candidate_latest = _read_json(
        candidate_dir / "results" / "backtest" / "latest.json"
    )
    if not candidate_latest:
        return None
    root = store.job_dir(job_id)
    baseline_latest = _read_json(root / "results" / "backtest" / "latest.json")
    if not baseline_latest or baseline_latest.get("revision") != base_revision:
        try:
            payload = backtest_execution_job(job_id, store=store)
        except Exception:
            payload = None
        if payload:
            baseline_latest = {
                **(payload.get("result") or {}),
                "revision": payload.get("revision"),
                "dataset": payload.get("dataset"),
            }
        else:
            baseline_latest = None

    candidate_side = {
        "run_id": candidate_latest.get("run_id"),
        "revision": candidate_latest.get("revision"),
        "stats": candidate_latest.get("stats") or {},
    }
    baseline_side = (
        {
            "run_id": baseline_latest.get("run_id"),
            "revision": baseline_latest.get("revision"),
            "stats": baseline_latest.get("stats") or {},
        }
        if baseline_latest
        else None
    )
    comparison = {
        "baseline": baseline_side,
        "candidate": candidate_side,
        "deltas": _stat_deltas(
            (baseline_side or {}).get("stats") or {}, candidate_side["stats"]
        ),
        "dataset": candidate_latest.get("dataset") or {},
        "generated_at": utc_now_iso(),
    }
    comparison_path = store.job_dir(job_id) / "applications" / pid / "comparison.json"
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return comparison


def _stat_deltas(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for key, candidate_value in candidate.items():
        baseline_value = baseline.get(key)
        match candidate_value, baseline_value:
            case (int() | float(), int() | float()):
                deltas[key] = float(candidate_value) - float(baseline_value)
    return deltas


def _bounded_comparison(
    comparison: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Stats/deltas only — never point series (sync payload discipline)."""
    if comparison is None:
        return None
    return {
        "baseline": comparison.get("baseline"),
        "candidate": comparison.get("candidate"),
        "deltas": comparison.get("deltas") or {},
        "dataset": comparison.get("dataset") or {},
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    match loaded:
        case dict():
            return loaded
        case _:
            return None

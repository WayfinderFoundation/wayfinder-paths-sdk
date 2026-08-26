from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

DEFAULT_MAX_BACKTEST_AGE_DAYS = 30

# Kill-switch: force the paired fold evaluation to recompute on every call,
# ignoring persisted fold results (evidence-reuse surface, #705 env pattern).
ECONOMIC_ALWAYS_RECOMPUTE_ENV = "WAYFINDER_ECONOMIC_ALWAYS_RECOMPUTE"
# Persisted inside the candidate bundle (outside workspace/, so the candidate
# revision is unchanged): the expensive 2xK-fold paired replay, keyed by the
# full content identity it was computed under.
PAIRED_FOLDS_RELATIVE = Path("reports") / "economic" / "paired_folds.json"


def governance_hard_constraints(root: Path) -> dict[str, Any]:
    """Owner-owned risk ceilings for a job, loaded via the constitution
    facade (governance/<job_id>/hard_constraints.yaml when the protected
    namespace exists, legacy job-root constitution.yaml otherwise).

    Callers use these as CLAMPS over agent-writable knobs: ``max_leverage``
    (execution_params.leverage), ``max_drawdown`` / ``max_gross_exposure_usd``
    (workspace/risk_limits.json). The shipped defaults carry none of these
    keys, so jobs without owner-set ceilings behave byte-identically."""
    from wayfinder_paths.jobs.constitution import load_constitution

    hard = load_constitution(Path(root)).get("hard_constraints")
    return dict(hard) if isinstance(hard, Mapping) else {}


def clamp_leverage(
    leverage: Any, hard_constraints: Mapping[str, Any]
) -> tuple[float, float | None]:
    """Clamp an agent-writable leverage knob to the owner's ``max_leverage``
    ceiling. Returns (effective_leverage, ceiling) — ceiling is None when no
    clamp fired (no governance ceiling, or already within it)."""
    try:
        requested = float(leverage if leverage is not None else 1.0)
    except (TypeError, ValueError):
        requested = 1.0
    try:
        ceiling = float(hard_constraints["max_leverage"])
    except (KeyError, TypeError, ValueError):
        return requested, None
    if ceiling > 0 and requested > ceiling:
        return ceiling, ceiling
    return requested, None


def compute_workspace_revision(root: Path) -> str:
    """Content hash of workspace/* + job.yaml.

    Promotion copies the candidate byte-for-byte over the active workspace, so
    a hash computed on a candidate dir pre-promotion equals the promoted
    revision — artifacts stamped during candidate validation stay valid after
    promotion.
    """
    digest = hashlib.sha256()
    workspace = root / "workspace"
    if workspace.exists():
        for path in sorted(workspace.rglob("*")):
            # Bytecode caches appear as a side effect of validation itself
            # (py_compile, module loading) and must not perturb the revision.
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            # Agent-writable diary state, read by nothing in the execution
            # path (durable memory lives at the JOB root). Hashing it turned
            # every routine memory update into a phantom strategy change:
            # stale gate stamps + baseline drift on staged candidates.
            if path.relative_to(workspace) == Path("memory.md"):
                continue
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode("utf-8"))
                digest.update(path.read_bytes())
    job_yaml = root / "job.yaml"
    if job_yaml.exists():
        # Hash job.yaml minus self-referential bookkeeping: `versioning`
        # stores the revision this hash produces, and `updated_at` changes on
        # every save — both would make the hash unstable under pure
        # bookkeeping writes.
        try:
            loaded = yaml.safe_load(job_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = None
        match loaded:
            case dict() as data:
                data.pop("versioning", None)
                data.pop("updated_at", None)
                # Operational knobs — wallet routing, capital accounting, and
                # the paper/live flag — don't change strategy logic, so
                # editing them at go-live must not orphan the
                # validation/backtest/preflight stamps.
                match data.get("script_loop"):
                    case dict() as script_loop:
                        script_loop.pop("mode", None)
                # The agent watch level (FE "Just run it"/"Watch & suggest")
                # is an operator dial, not strategy logic. Hashing it turned
                # every mode flip into a phantom strategy change: stale gate
                # stamps, baseline drift on staged candidates, and candidate
                # promotion reverting the operator's selection. job_kind is
                # derived from the same dial, so it leaves the hash with it.
                data.pop("agent_loop", None)
                data.pop("job_kind", None)
                match data.get("execution_params"):
                    case dict() as execution_params:
                        execution_params.pop("wallet_label", None)
                        execution_params.pop("initial_capital", None)
                digest.update(
                    json.dumps(data, sort_keys=True, default=str).encode("utf-8")
                )
            case _:
                digest.update(job_yaml.read_bytes())
    return digest.hexdigest()[:12]


def dataset_content_fingerprint(
    candidate_dir: Path,
    job_dir: Path,
    *,
    feature_paths: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Content hash of the exact files a candidate backtest would consume.

    The evidence-reuse identity companion to `compute_workspace_revision`:
    revision hashes the CODE a backtest ran, this hashes the DATA it ran on.
    Resolution mirrors the candidate behavior checks (candidate bundle first,
    then the job root — `_resolve_dataset` order) plus the declared feature
    stores (first-root-wins, mirroring `load_feature_rows`). Purely
    mechanical file-byte hashing, no parsing. Returns None when no dataset
    file exists (research-only jobs)."""
    dataset_path = next(
        (
            path
            for root in (candidate_dir, job_dir)
            for path in (
                root / "results" / "backtest" / "input_bars.json",
                root / "workspace" / "config" / "backtest_bars.json",
            )
            if path.exists()
        ),
        None,
    )
    if dataset_path is None:
        return None
    fingerprint: dict[str, Any] = {
        "path": str(dataset_path),
        "sha256": _file_sha256(dataset_path),
        "bytes": dataset_path.stat().st_size,
    }
    features: dict[str, str] = {}
    for relative in dict.fromkeys(feature_paths):
        chosen = next(
            (
                root / relative
                for root in (candidate_dir, job_dir)
                if (root / relative).exists()
            ),
            None,
        )
        if chosen is not None:
            features[str(relative)] = _file_sha256(chosen)
    if features:
        fingerprint["features"] = features
    return fingerprint


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_live_gate(
    job_id: str,
    *,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
    max_backtest_age_days: int = DEFAULT_MAX_BACKTEST_AGE_DAYS,
) -> dict[str, Any]:
    """Is this job (or candidate) allowed to trade live / be promoted?

    Passing requires, all tied to the CURRENT workspace revision: a passed
    validation report, a fresh backtest whose trace validated, a passed
    preflight, and the jobs_v1 contract. The result is synced to the backend,
    which refuses approve/resume actions when live_ready is false — but the
    SDK-side re-check is the authoritative gate.
    """
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    reasons: list[str] = []
    revision = compute_workspace_revision(root)

    job_yaml = root / "job.yaml"
    job_data = (
        yaml.safe_load(job_yaml.read_text(encoding="utf-8")) or {}
        if job_yaml.exists()
        else {}
    )
    if str(job_data.get("execution_contract") or "legacy") != "jobs_v1":
        reasons.append(
            "job is on the legacy execution contract; run "
            "`wayfinder job migrate-contract` first"
        )

    validation = _read_json(root / "reports" / "validation" / "latest.json")
    validation_summary = {"status": None, "revision": None}
    if not validation:
        reasons.append("no validation report (run `wayfinder job validate`)")
    else:
        validation_summary = {
            "status": validation["status"],
            # A no-spec early-return report carries no revision stamp.
            "revision": validation.get("revision"),
        }
        if validation["status"] != "passed":
            failed = [
                check["name"] for check in validation["checks"] if not check["passed"]
            ]
            reasons.append(f"validation failed: {failed}")
        elif validation.get("revision") and validation["revision"] != revision:
            reasons.append(
                f"validation report is for revision {validation['revision']}, "
                f"workspace is {revision}"
            )

    backtest = _read_json(root / "results" / "backtest" / "latest.json")
    backtest_summary: dict[str, Any] = {}
    if not backtest:
        reasons.append("no backtest artifact (run `wayfinder job backtest`)")
    else:
        # revision/generated_at/dataset are stamp keys — absent when latest.json
        # was written without the job-level stamp, which is itself a gate reason.
        backtest_summary = {
            "revision": backtest.get("revision"),
            "generated_at": backtest.get("generated_at"),
            "stats": backtest["stats"],
            "dataset": backtest.get("dataset"),
        }
        if backtest.get("revision") != revision:
            reasons.append(
                f"backtest is for revision {backtest.get('revision')}, "
                f"workspace is {revision} (re-run `wayfinder job backtest`)"
            )
        generated_at = backtest.get("generated_at")
        if not generated_at:
            reasons.append("backtest has no generated_at stamp")
        else:
            age_days = (
                datetime.now(UTC) - datetime.fromisoformat(str(generated_at))
            ).total_seconds() / 86_400
            if age_days > max_backtest_age_days:
                reasons.append(
                    f"backtest is {age_days:.0f} days old (max {max_backtest_age_days})"
                )
        if not backtest["validation"]["execution_valid"]:
            reasons.append("latest backtest trace failed execution validation")

    preflight = _read_json(root / "reports" / "preflight" / "latest.json")
    preflight_summary = {"status": None, "revision": None}
    if not preflight:
        reasons.append("no preflight report (run `wayfinder job preflight`)")
    else:
        preflight_summary = {
            "status": preflight["status"],
            "revision": preflight["revision"],
        }
        if preflight["status"] != "passed":
            failed = [
                check["name"]
                for check in preflight["checks"]
                if not check["passed"] and check.get("blocking") is not False
            ]
            reasons.append(f"preflight failed: {failed}")
        elif preflight["revision"] != revision:
            reasons.append(
                f"preflight is for revision {preflight['revision']}, "
                f"workspace is {revision} (re-run `wayfinder job preflight`)"
            )

    return {
        "live_ready": not reasons,
        "revision": revision,
        "reasons": reasons,
        "validation": validation_summary,
        "backtest": backtest_summary,
        "preflight": preflight_summary,
        "checked_at": utc_now_iso(),
    }


def evaluate_economic_gate(
    job_id: str,
    *,
    candidate_dir: str | Path,
    baseline_dir: str | Path | None = None,
    store: JobStore | None = None,
    probation: bool = False,
    dataset_root: str | Path | None = None,
) -> dict[str, Any]:
    """Independent economic acceptance: paired candidate-vs-incumbent replay
    on identical OOS folds under the owner constitution. Computed by gate
    code — the model cannot self-certify `economic_ready`."""
    from wayfinder_paths.jobs.constitution import load_constitution
    from wayfinder_paths.jobs.economics import (
        evaluate_economic_readiness,
        paired_fold_evaluation,
    )
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    store = store or JobStore()
    root = store.job_dir(job_id)
    candidate_root = Path(candidate_dir)
    baseline_root = Path(baseline_dir) if baseline_dir is not None else root
    protected_dataset_root = Path(dataset_root) if dataset_root is not None else None
    constitution = load_constitution(root)

    baseline_yaml = _load_job_yaml(baseline_root)
    candidate_yaml = _load_job_yaml(candidate_root)
    spec_data, _ = resolve_execution_spec(candidate_root, candidate_yaml)
    if not spec_data:
        return _economic_unavailable(constitution, "no execution spec", probation)
    spec = ExecutionSpec.from_dict(spec_data)
    baseline_script = store.resolve_script_entrypoint(
        job_id,
        baseline_yaml,
        candidate_dir=baseline_root if baseline_root != root else None,
    )
    candidate_script = store.resolve_script_entrypoint(
        job_id, candidate_yaml, candidate_dir=candidate_root
    )
    if baseline_script is None or candidate_script is None:
        return _economic_unavailable(constitution, "no script entrypoint", probation)

    # Evidence reuse: the paired replay is deterministic in {candidate code,
    # baseline code, constitution, dataset content, fold layout}. When a
    # persisted evaluation carries the exact same key AND is green, replaying
    # 2xK fold sims answers a question already answered — reuse it. The
    # readiness verdict is always recomputed (pure policy, no simulation).
    fold_key = {
        "candidate_revision": compute_workspace_revision(candidate_root),
        "baseline_revision": compute_workspace_revision(baseline_root),
        "constitution_revision": constitution.get("revision"),
        "dataset_fingerprint": _candidate_dataset_fingerprint(
            candidate_root, protected_dataset_root or root
        ),
        "fold_spec": {**constitution["evaluation"], "warmup_bars": 60},
    }
    persist_path = (
        protected_dataset_root
        / "reports"
        / "economic"
        / f"{fold_key['candidate_revision']}.json"
        if protected_dataset_root is not None
        else candidate_root / PAIRED_FOLDS_RELATIVE
    )
    evaluation = _reusable_paired_evaluation(
        persist_path, fold_key, constitution, probation=probation
    )
    if evaluation is not None:
        store.append_journal(
            job_id,
            {
                "type": "economic_evaluation_reused",
                "candidate_revision": fold_key["candidate_revision"],
                "baseline_revision": fold_key["baseline_revision"],
                "constitution_revision": fold_key["constitution_revision"],
                "paired_folds_path": str(persist_path),
            },
        )
        reused = True
    else:
        try:
            dataset = _load_dataset(
                protected_dataset_root or candidate_root,
                spec,
                candidate_yaml,
                feature_roots=(protected_dataset_root,)
                if protected_dataset_root is not None
                else None,
            )
        except FileNotFoundError:
            # Candidate bundles carry workspace/ + job.yaml only. Jobs that
            # keep their dataset at the JOB root
            # (results/backtest/input_bars.json — the standard fetch-dataset
            # location) would otherwise never resolve bars here and every
            # propose would die on "No backtest bars found". Same fallback
            # candidate validation applies; if the job root ALSO has no bars
            # the error propagates — with the propose-time infra-abort, a
            # dataset that validated moments ago but vanished for the
            # economic step is a box condition, not evidence.
            if protected_dataset_root is not None:
                raise
            dataset = _load_dataset(root, spec, candidate_yaml)

        started = time.monotonic()
        try:
            evaluation = paired_fold_evaluation(
                baseline_script=baseline_script,
                candidate_script=candidate_script,
                dataset=dataset,
                spec=spec,
                baseline_params=baseline_yaml.get("execution_params") or {},
                candidate_params=candidate_yaml.get("execution_params") or {},
                constitution=constitution,
            )
        finally:
            sim_wall_seconds = time.monotonic() - started
        reused = False
        try:
            atomic_write_json(
                persist_path,
                {
                    "key": fold_key,
                    "evaluation": evaluation,
                    "generated_at": utc_now_iso(),
                },
                default=str,
            )
        except OSError:
            pass  # persistence is an optimization, never a gate failure
    readiness = evaluate_economic_readiness(
        evaluation, constitution, probation=probation
    )
    return {
        "ready": readiness["ready"],
        "reasons": readiness["reasons"],
        "probation": probation,
        "enforcement": constitution["enforcement"],
        "constitution_revision": constitution.get("revision"),
        "objective": (
            evaluation.get("objective") if evaluation.get("status") == "ok" else None
        ),
        "paired_incumbent_delta": evaluation.get("paired_incumbent_delta"),
        "positive_folds": evaluation.get("positive_folds"),
        "fold_count": evaluation.get("fold_count"),
        "audit_slice": _audit_summary(evaluation.get("audit_slice")),
        "status": evaluation.get("status"),
        "sim_wall_seconds": 0.0 if reused else round(sim_wall_seconds, 3),
        **({"reused": True} if reused else {}),
        "checked_at": utc_now_iso(),
    }


def _candidate_dataset_fingerprint(
    candidate_root: Path, root: Path
) -> dict[str, Any] | None:
    # Lazy import: validation imports gating at module level.
    from wayfinder_paths.jobs.validation import candidate_dataset_fingerprint

    return candidate_dataset_fingerprint(candidate_root, root)


def _reusable_paired_evaluation(
    persist_path: Path,
    fold_key: dict[str, Any],
    constitution: Mapping[str, Any],
    *,
    probation: bool,
) -> dict[str, Any] | None:
    """Persisted paired-fold evaluation, ONLY when the full content key
    matches and the persisted evidence is green (status ok + the readiness
    it implies under the current constitution is ready=True). Non-green
    evidence is never reused — it recomputes fresh, every time."""
    # Lazy: economics pulls pandas; gating is imported on every cheap path.
    from wayfinder_paths.jobs.economics import evaluate_economic_readiness

    if os.environ.get(ECONOMIC_ALWAYS_RECOMPUTE_ENV) == "1":
        return None
    persisted = _read_json(persist_path)
    if not persisted:
        return None
    # JSON round-trip normalization: the key was persisted via default=str,
    # so compare against the same normalization of the freshly-derived key.
    normalized_key = json.loads(json.dumps(fold_key, sort_keys=True, default=str))
    if persisted.get("key") != normalized_key:
        return None
    evaluation = persisted.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("status") != "ok":
        return None
    readiness = evaluate_economic_readiness(
        evaluation, constitution, probation=probation
    )
    if readiness.get("ready") is not True:
        return None
    return evaluation


def _audit_summary(audit: dict[str, Any] | None) -> dict[str, Any] | None:
    if not audit:
        return None
    return {
        "start": audit["start"],
        "end": audit["end"],
        "bars": audit["bars"],
        "delta_utility": audit["delta_utility"],
    }


def _economic_unavailable(
    constitution: dict[str, Any], reason: str, probation: bool
) -> dict[str, Any]:
    return {
        "ready": None,
        "reasons": [f"economic evaluation unavailable: {reason}"],
        "probation": probation,
        "enforcement": constitution["enforcement"],
        "constitution_revision": constitution.get("revision"),
        "status": "unavailable",
        # Unavailable evidence is an escalation, not an approval: the gate
        # fails closed on live-capable blocking jobs.
        "escalate": True,
        "checked_at": utc_now_iso(),
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A torn write reads as a missing artifact, not a crashed gate.
        return None

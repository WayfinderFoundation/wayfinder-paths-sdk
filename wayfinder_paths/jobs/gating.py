from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

DEFAULT_MAX_BACKTEST_AGE_DAYS = 30


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
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode("utf-8"))
                digest.update(path.read_bytes())
    job_yaml = root / "job.yaml"
    if job_yaml.exists():
        digest.update(_canonical_job_yaml_bytes(job_yaml))
    return digest.hexdigest()[:12]


def _canonical_job_yaml_bytes(path: Path) -> bytes:
    """job.yaml minus self-referential bookkeeping: `versioning` stores the
    revision this hash produces, and `updated_at` changes on every save — both
    would make the hash unstable under pure bookkeeping writes."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return path.read_bytes()
    match loaded or {}:
        case dict() as data:
            data.pop("versioning", None)
            data.pop("updated_at", None)
            return json.dumps(data, sort_keys=True, default=str).encode("utf-8")
        case _:
            return path.read_bytes()


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


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A torn write reads as a missing artifact, not a crashed gate.
        return None

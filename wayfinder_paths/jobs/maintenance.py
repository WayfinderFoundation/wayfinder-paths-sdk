"""Mechanical admission for implementation-only strategy maintenance.

This is deliberately not an economic-promotion shortcut.  It admits only
Python implementation changes under ``workspace/src`` whose job config and
full canonical-history execution outputs are byte-for-byte equivalent to the
incumbent.  Anything else belongs in the normal economic proposal lane.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from wayfinder_paths.jobs.gating import (
    compute_workspace_revision,
    dataset_fingerprint_identity,
)

BEHAVIOR_EQUIVALENCE_POLICY = "behavior_equivalence"
ECONOMIC_IMPROVEMENT_POLICY = "economic_improvement"
ACCEPTANCE_POLICIES = frozenset(
    {ECONOMIC_IMPROVEMENT_POLICY, BEHAVIOR_EQUIVALENCE_POLICY}
)

# Runtime telemetry and random run IDs are intentionally excluded.  These
# fields fully describe strategy decisions and their deterministic execution.
_BEHAVIOR_FIELDS = (
    "params",
    "equity_curve",
    "trades",
    "positions",
    "stats",
    "trace",
)


def prove_behavior_equivalence(
    *,
    active_dir: Path,
    candidate_dir: Path,
    changed_files: Sequence[str],
) -> dict[str, Any]:
    """Compare full backtest behavior under a tightly bounded change surface."""
    reasons = maintenance_change_surface_reasons(
        active_dir, candidate_dir, changed_files
    )
    baseline = _read_json(active_dir / "results" / "backtest" / "latest.json")
    candidate = _read_json(candidate_dir / "results" / "backtest" / "latest.json")
    if baseline is None:
        reasons.append("incumbent full backtest artifact is missing")
    if candidate is None:
        reasons.append("candidate full backtest artifact is missing")

    baseline_revision = compute_workspace_revision(active_dir)
    candidate_revision = compute_workspace_revision(candidate_dir)
    if baseline is not None and baseline.get("revision") != baseline_revision:
        reasons.append("incumbent backtest is not stamped for the active revision")
    if candidate is not None and candidate.get("revision") != candidate_revision:
        reasons.append("candidate backtest is not stamped for the candidate revision")

    baseline_dataset = dataset_fingerprint_identity(
        baseline.get("dataset_fingerprint") if baseline else None
    )
    candidate_dataset = dataset_fingerprint_identity(
        candidate.get("dataset_fingerprint") if candidate else None
    )
    if baseline_dataset is None or candidate_dataset is None:
        reasons.append("backtest dataset content fingerprint is missing")
    elif baseline_dataset != candidate_dataset:
        reasons.append("incumbent and candidate backtests used different dataset bytes")
    for label, artifact in (("incumbent", baseline), ("candidate", candidate)):
        if artifact is None:
            continue
        after = dataset_fingerprint_identity(artifact.get("dataset_fingerprint_after"))
        before = dataset_fingerprint_identity(artifact.get("dataset_fingerprint"))
        if artifact.get("dataset_stable") is not True or before != after:
            reasons.append(f"{label} dataset changed while its backtest was running")

    baseline_fields = _field_digests(baseline) if baseline else {}
    candidate_fields = _field_digests(candidate) if candidate else {}
    differing_fields = [
        field
        for field in _BEHAVIOR_FIELDS
        if baseline_fields.get(field) != candidate_fields.get(field)
    ]
    if baseline and candidate and differing_fields:
        reasons.append(
            "full-history execution behavior differs in: " + ", ".join(differing_fields)
        )

    ready = not reasons
    return {
        "ready": ready,
        "status": "passed" if ready else "failed",
        "policy": BEHAVIOR_EQUIVALENCE_POLICY,
        "scope": "full canonical backtest dataset",
        "method": "sha256 of canonical deterministic execution outputs",
        "reasons": reasons,
        "changed_files": list(changed_files),
        "compared_fields": list(_BEHAVIOR_FIELDS),
        "differing_fields": differing_fields,
        "baseline_revision": baseline_revision,
        "candidate_revision": candidate_revision,
        "dataset_fingerprint": candidate_dataset,
        "baseline_digest": _combined_digest(baseline_fields),
        "candidate_digest": _combined_digest(candidate_fields),
        "baseline_profile": dict((baseline or {}).get("profile") or {}),
        "candidate_profile": dict((candidate or {}).get("profile") or {}),
    }


def maintenance_change_surface_reasons(
    active_dir: Path, candidate_dir: Path, changed_files: Sequence[str]
) -> list[str]:
    reasons: list[str] = []
    if not changed_files:
        reasons.append("candidate contains no implementation change")
    disallowed: list[str] = []
    for raw in changed_files:
        path = PurePosixPath(raw)
        allowed = (
            len(path.parts) >= 3
            and path.parts[:2] == ("workspace", "src")
            and path.suffix == ".py"
        )
        if not allowed:
            disallowed.append(raw)
            continue
        candidate_path = candidate_dir / path
        if candidate_path.exists() and candidate_path.is_symlink():
            disallowed.append(raw)
    if disallowed:
        reasons.append(
            "maintenance changes are limited to regular Python files under "
            f"workspace/src: {sorted(disallowed)}"
        )
    active_yaml = active_dir / "job.yaml"
    candidate_yaml = candidate_dir / "job.yaml"
    if (
        not active_yaml.exists()
        or not candidate_yaml.exists()
        or active_yaml.read_bytes() != candidate_yaml.read_bytes()
    ):
        reasons.append("job.yaml must be byte-identical for maintenance changes")
    return reasons


def _field_digests(artifact: Mapping[str, Any]) -> dict[str, str]:
    return {field: _digest(artifact.get(field)) for field in _BEHAVIOR_FIELDS}


def _combined_digest(fields: Mapping[str, str]) -> str | None:
    return _digest(dict(fields)) if fields else None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None

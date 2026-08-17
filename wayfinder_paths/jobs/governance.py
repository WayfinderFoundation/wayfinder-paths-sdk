"""Protected governance plane: the owner-owned files that define what
"better" means, split by role and kept OUTSIDE the agent-writable job tree.

The legacy `constitution.yaml` lived at the job root — inside
`.wayfinder/jobs/**`, which the worker agent can write. That made the L4
boundary prompt-enforced, not capability-enforced: an agent could loosen the
ceilings, or relax enforcement between proposal evaluation and approval.
This module moves the standard to `governance/<job_id>/` at the REPO root
(covered by the worker manifest's catch-all edit deny) and splits it into
four objects that must not be one mutable blob:

- ``external_target.yaml``  — the invariant objective. Owner-only.
- ``hard_constraints.yaml`` — non-negotiable risk ceilings. Owner-only.
- ``audit_policy.yaml``     — enforcement, evidence requirements, promotion
                              thresholds. Owner-only.
- ``search_criterion.yaml`` — objective weights guiding internal candidate
                              ranking. Evolvable later under L4 governance.

Tamper evidence: ``epochs.jsonl`` is a hash-chained append-only record of
every committed governance revision. The owner edits files then runs
``wayfinder job governance-commit``; a file change with no commit makes the
chain head disagree with the working tree and loads surface
``chain_status: "tampered"`` — the gate layer fails closed on that for
live-capable jobs. This is tamper-EVIDENT in code; making it tamper-PROOF
is a box-ops task (root-owned, read-only files).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.models import utc_now_iso

GOVERNANCE_DIRNAME = "governance"
EPOCHS_FILENAME = "epochs.jsonl"

EXTERNAL_TARGET_FILE = "external_target.yaml"
HARD_CONSTRAINTS_FILE = "hard_constraints.yaml"
AUDIT_POLICY_FILE = "audit_policy.yaml"
SEARCH_CRITERION_FILE = "search_criterion.yaml"

GOVERNANCE_FILES = (
    EXTERNAL_TARGET_FILE,
    HARD_CONSTRAINTS_FILE,
    AUDIT_POLICY_FILE,
    SEARCH_CRITERION_FILE,
)

DEFAULT_EXTERNAL_TARGET: dict[str, Any] = {
    "version": 1,
    # The invariant the whole loop is FOR. Everything else is machinery.
    "basis": "net_log_growth_after_costs",
    "baseline": "incumbent",
    "horizon": "forward_audit_window",
}


def governance_dir(repo_root: Path, job_id: str) -> Path:
    return Path(repo_root) / GOVERNANCE_DIRNAME / job_id


def file_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def has_governance(repo_root: Path, job_id: str) -> bool:
    gov = governance_dir(repo_root, job_id)
    return gov.is_dir() and any((gov / name).exists() for name in GOVERNANCE_FILES)


def load_governance(repo_root: Path, job_id: str) -> dict[str, Any]:
    """Compose the four governance files into the legacy constitution shape
    (so every existing consumer keeps working) plus a ``governance`` metadata
    block: per-file revisions, composite revision, and chain_status."""
    return load_governance_from_dir(governance_dir(repo_root, job_id))


def load_governance_from_dir(gov_dir: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.constitution import DEFAULT_CONSTITUTION, _merge

    target = _load_yaml(gov_dir / EXTERNAL_TARGET_FILE) or dict(DEFAULT_EXTERNAL_TARGET)
    hard = _load_yaml(gov_dir / HARD_CONSTRAINTS_FILE) or dict(
        DEFAULT_CONSTITUTION["hard_constraints"]
    )
    audit = _load_yaml(gov_dir / AUDIT_POLICY_FILE) or {}
    criterion = _load_yaml(gov_dir / SEARCH_CRITERION_FILE) or {}

    composed: dict[str, Any] = dict(DEFAULT_CONSTITUTION)
    composed = _merge(
        composed,
        {
            "hard_constraints": hard,
            **{
                key: audit[key]
                for key in ("enforcement", "evaluation", "promotion", "verdict")
                if key in audit
            },
            **(
                {"objective": criterion["objective"]}
                if "objective" in criterion
                else {}
            ),
        },
    )

    revisions = {name: file_revision(gov_dir / name) for name in GOVERNANCE_FILES}
    composite = hashlib.sha256(
        "|".join(f"{name}:{revisions[name]}" for name in GOVERNANCE_FILES).encode()
    ).hexdigest()[:12]
    chain_status, epoch = verify_chain(gov_dir)

    composed["revision"] = composite
    composed["source"] = "governance"
    composed["governance"] = {
        "dir": str(gov_dir),
        "external_target": target,
        "revisions": revisions,
        "composite_revision": composite,
        "chain_status": chain_status,
        "epoch": epoch,
    }
    return composed


def verify_chain(gov_dir: Path) -> tuple[str, int]:
    """Verify the epochs hash chain and that its head matches the working
    tree. Returns (status, epoch_count) with status in:
    verified | uncommitted | tampered | empty."""
    epochs_path = gov_dir / EPOCHS_FILENAME
    current = {name: file_revision(gov_dir / name) for name in GOVERNANCE_FILES}
    has_files = any(value is not None for value in current.values())
    if not epochs_path.exists():
        return ("uncommitted" if has_files else "empty"), 0

    rows: list[dict[str, Any]] = []
    for line in epochs_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return "tampered", len(rows)
        rows.append(row)
    if not rows:
        return ("uncommitted" if has_files else "empty"), 0

    prev_hash = ""
    for row in rows:
        if str(row.get("prev") or "") != prev_hash:
            return "tampered", len(rows)
        prev_hash = _row_hash(row)

    head = rows[-1]
    if head.get("files") != current:
        return "tampered", len(rows)
    return "verified", len(rows)


def commit_epoch(gov_dir: Path, *, note: str = "") -> dict[str, Any]:
    """Record the current governance file hashes as a new chain epoch. The
    owner runs this (via CLI) after every deliberate edit; loads treat any
    uncommitted drift as tamper."""
    epochs_path = gov_dir / EPOCHS_FILENAME
    rows: list[dict[str, Any]] = []
    if epochs_path.exists():
        for line in epochs_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    prev_hash = _row_hash(rows[-1]) if rows else ""
    revisions = {name: file_revision(gov_dir / name) for name in GOVERNANCE_FILES}
    composite = hashlib.sha256(
        "|".join(f"{name}:{revisions[name]}" for name in GOVERNANCE_FILES).encode()
    ).hexdigest()[:12]
    row = {
        "epoch": len(rows),
        "ts": utc_now_iso(),
        "files": revisions,
        "composite": composite,
        "prev": prev_hash,
        "note": note,
    }
    with epochs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def _row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def migrate_from_constitution(
    repo_root: Path, job_id: str, job_root: Path
) -> dict[str, Any]:
    """Split a legacy job-root constitution.yaml (or the defaults) into the
    governance namespace and commit epoch 0. The legacy file is left in
    place — the loader prefers governance/ once it exists."""
    from wayfinder_paths.jobs.constitution import (
        CONSTITUTION_FILENAME,
        DEFAULT_CONSTITUTION,
        _merge,
    )

    legacy = _load_yaml(job_root / CONSTITUTION_FILENAME)
    merged = _merge(DEFAULT_CONSTITUTION, legacy or {})

    gov_dir = governance_dir(repo_root, job_id)
    gov_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    audit_floor = float(
        (merged.get("promotion") or {}).get("audit_min_delta_utility") or 0.0
    )
    if audit_floor < 0:
        warnings.append(
            f"audit_min_delta_utility is negative ({audit_floor}) — a full-size "
            "candidate can pass with negative audited utility; raise to >= 0.0"
        )

    _write_yaml(gov_dir / EXTERNAL_TARGET_FILE, dict(DEFAULT_EXTERNAL_TARGET))
    _write_yaml(gov_dir / HARD_CONSTRAINTS_FILE, merged["hard_constraints"])
    audit_policy = {
        "enforcement": merged["enforcement"],
        "evaluation": merged["evaluation"],
        "promotion": merged["promotion"],
    }
    if isinstance(merged.get("verdict"), dict):
        audit_policy["verdict"] = merged["verdict"]
    _write_yaml(gov_dir / AUDIT_POLICY_FILE, audit_policy)
    _write_yaml(gov_dir / SEARCH_CRITERION_FILE, {"objective": merged["objective"]})

    epoch = commit_epoch(gov_dir, note=f"migrated from legacy constitution ({job_id})")
    return {
        "job_id": job_id,
        "governance_dir": str(gov_dir),
        "epoch": epoch["epoch"],
        "composite_revision": epoch["composite"],
        "source": "legacy_constitution" if legacy else "defaults",
        "warnings": warnings,
    }


AUDIT_DIRNAME = "audit"


def record_evidence_access(
    repo_root: Path, job_id: str, op: str, detail: dict[str, Any] | None = None
) -> None:
    """Append one row to the protected evidence-access ledger
    (``audit/<job_id>/evidence_access.jsonl`` — same manifest-denied plane
    as governance). Every validation query a revision makes is on the
    record; the gate can report how mined the evidence is. Never raises —
    the ledger is telemetry, the op must not fail on it."""
    try:
        audit_dir = Path(repo_root) / AUDIT_DIRNAME / job_id
        audit_dir.mkdir(parents=True, exist_ok=True)
        row = {"op": op, "ts": utc_now_iso(), **(detail or {})}
        with (audit_dir / "evidence_access.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    except Exception:  # noqa: BLE001
        return


def evidence_access_count(repo_root: Path, job_id: str) -> int:
    try:
        path = Path(repo_root) / AUDIT_DIRNAME / job_id / "evidence_access.jsonl"
        if not path.exists():
            return 0
        return sum(
            1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    except Exception:  # noqa: BLE001
        return 0


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

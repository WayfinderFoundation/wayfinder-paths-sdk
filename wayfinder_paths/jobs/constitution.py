"""Owner-owned optimization constitution: the protected economic standard a
candidate must beat to be promoted.

The file lives at the JOB ROOT (`constitution.yaml`), deliberately outside
`workspace/` — it is not part of the workspace revision hash, is never staged
into candidate workspaces, and agents read it but cannot activate changes to
it. Agents may PROPOSE a new constitution as prose/diff for the owner; only a
human editing the file changes the standard. This is the L4 boundary: the
loop being judged must not be able to redefine what "better" means.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

CONSTITUTION_FILENAME = "constitution.yaml"

# Applied when a job has no constitution.yaml yet: the economic gate still
# computes and reports, but cannot block — existing jobs keep promoting under
# the technical gate until the owner installs a constitution.
DEFAULT_CONSTITUTION: dict[str, Any] = {
    "version": 1,
    "enforcement": "advisory",
    "objective": {
        # U = growth − λ·downside_deviation − κ·tail_loss − η·fee_load
        "weights": {"downside": 0.5, "tail": 1.0, "turnover": 0.25},
    },
    "evaluation": {
        "folds": 4,
        "confidence": 0.90,
        "bootstrap_iterations": 500,
        "block_days": 5,
        "audit_days": 7,
        "regime": {
            "min_target_days": 10,
            "bootstrap_block_days": 2,
            "max_out_of_regime_loss_pct": 0.02,
        },
    },
    "promotion": {
        "required_positive_folds": 2,
        "min_oos_trades": 8,
        "audit_min_delta_utility": -0.005,
        # Probation-flagged proposals (reduced-size canary legs) clear on a
        # positive point estimate instead of the LCB — small-sample jobs must
        # stay movable; full-size promotion is where the LCB bites.
        "probation_requires_lcb": False,
    },
    "hard_constraints": {
        "max_drawdown_pct": 0.25,
        "max_tail_loss": 0.15,
    },
}


def load_constitution(root: Path) -> dict[str, Any]:
    """Load the job's economic standard, merged over defaults.

    Prefers the protected governance namespace (``governance/<job_id>/`` at
    the repo root — OUTSIDE the agent-writable job tree; see governance.py)
    when it exists; falls back to the legacy job-root ``constitution.yaml``.
    Returns the DEFAULT_CONSTITUTION (enforcement=advisory) when neither
    exists or the legacy file is unparseable — a broken constitution must
    degrade to advisory, never brick promotion or silently enforce garbage.
    """
    gov_dir = _governance_dir_for_job_root(root)
    if gov_dir is not None:
        from wayfinder_paths.jobs.governance import load_governance_from_dir

        return load_governance_from_dir(gov_dir)

    path = root / CONSTITUTION_FILENAME
    if not path.exists():
        return {**DEFAULT_CONSTITUTION, "revision": None, "source": "defaults"}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        loaded = None
    if not isinstance(loaded, dict):
        return {**DEFAULT_CONSTITUTION, "revision": None, "source": "defaults"}
    merged = _merge(DEFAULT_CONSTITUTION, loaded)
    merged["revision"] = constitution_revision(path)
    merged["source"] = "file"
    return merged


def load_benchmark_constitution() -> dict[str, Any]:
    """The certification profile (stricter than production defaults): all WOB
    certification runs gate against this one versioned standard."""
    path = Path(__file__).parent / "benchmarks" / "constitution.benchmark.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    merged = _merge(DEFAULT_CONSTITUTION, loaded)
    merged["revision"] = constitution_revision(path)
    merged["source"] = "benchmark_profile"
    return merged


def constitution_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _governance_dir_for_job_root(root: Path) -> Path | None:
    """Map a job root to its protected governance dir, ONLY for the canonical
    ``<repo_root>/.wayfinder/jobs/<job_id>`` layout. Anything else (test
    fixtures, ad-hoc dirs) keeps legacy behavior — the mapping must never
    guess."""
    root = Path(root)
    parent = root.parent
    if parent.name == "jobs" and parent.parent.name == ".wayfinder":
        from wayfinder_paths.jobs.governance import governance_dir, has_governance

        repo_root = parent.parent.parent
        if has_governance(repo_root, root.name):
            return governance_dir(repo_root, root.name)
    return None


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge(base[key], value)
        else:
            merged[key] = value
    return merged

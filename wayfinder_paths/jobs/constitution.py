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
    """Load the job's constitution, merged over defaults.

    Returns the DEFAULT_CONSTITUTION (enforcement=advisory) when the file is
    absent or unparseable — a broken constitution must degrade to advisory,
    never brick promotion or silently enforce garbage.
    """
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


def constitution_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge(base[key], value)
        else:
            merged[key] = value
    return merged

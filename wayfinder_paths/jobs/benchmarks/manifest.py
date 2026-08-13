"""Manifests, commitments, and the public/private suite split.

Publication protocol: PUBLIC worlds ship with everything (dev paths, hidden
continuations, oracle answers) — the development set anyone can tune
against. SEALED worlds publish only their MANIFEST: world id, archetype
label, budgets, and sha256 commitments of the world data + oracle output.
Commitments are published BEFORE any results are reported, so sealed worlds
cannot be quietly regenerated after seeing scores. Private seeds live
OUTSIDE the repo (owner-held file, path via WOB_PRIVATE_SEEDS env var).

A sealed world is BURNED for final certification once its audit result has
been exposed to development — new certification uses fresh private seeds.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.benchmarks.grammar import grammar_hash

SUITE_VERSION = "exact-v0"
PRIVATE_SEEDS_ENV = "WOB_PRIVATE_SEEDS"

DEFAULT_BUDGETS = {
    "b25": {"candidate_evaluations": 25},
    "b50": {"candidate_evaluations": 50},
    "b100": {"candidate_evaluations": 100},
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def world_manifest(world: Any, oracle: dict[str, Any] | None) -> dict[str, Any]:
    """The publishable record for ANY world (public or sealed). Contains no
    mechanism, no seeds, no hidden rows — only labels, budgets, and
    commitments that later make results verifiable."""
    return {
        "suite_version": SUITE_VERSION,
        "world_id": world.world_id,
        "archetype": world.archetype,
        "grammar_hash": grammar_hash(),
        "fee_bps": world.mechanism.fee_bps,
        "budgets": DEFAULT_BUDGETS,
        "dev_paths": len(world.dev_rows),
        "hidden_paths": len(world.hidden_rows),
        "commitments": {
            "dev_rows": canonical_sha256(world.dev_rows),
            "hidden_rows": canonical_sha256(world.hidden_rows),
            "oracle": canonical_sha256(oracle) if oracle is not None else None,
        },
        "calibration": {
            "passed": world.calibration.get("passed"),
            "attempts": world.calibration.get("attempts"),
        },
    }


def public_world_payload(world: Any, oracle: dict[str, Any]) -> dict[str, Any]:
    """Everything, answers included — the published development set."""
    return {
        "manifest": world_manifest(world, oracle),
        "dev_rows": world.dev_rows,
        "hidden_rows": world.hidden_rows,
        "oracle": {
            "u_star": oracle["u_star"],
            "u_null": oracle["u_null"],
            "epsilon": oracle["epsilon"],
            "best_genome_id": oracle["best_genome_id"],
            "best_basin": oracle["best_basin"],
            "epsilon_optimal": oracle["epsilon_optimal"],
            "expected_utility": oracle["expected_utility"],
        },
    }


def sealed_world_payload(world: Any, oracle: dict[str, Any]) -> dict[str, Any]:
    """What the OWNER keeps for sealed worlds (never published, never inside
    an optimizer's filesystem): full data + answers, matching the published
    commitments bit-for-bit."""
    return public_world_payload(world, oracle)


def load_private_seeds(path: str | None = None) -> dict[str, list[int]]:
    """Owner-held archetype→seeds mapping for sealed worlds. Absent file →
    empty (public-only operation): sealed generation requires the owner."""
    location = path or os.environ.get(PRIVATE_SEEDS_ENV)
    if not location or not Path(location).exists():
        return {}
    loaded = json.loads(Path(location).read_text(encoding="utf-8"))
    return {str(k): [int(s) for s in v] for k, v in loaded.items()}


def suite_split(
    *,
    public_per_archetype: int = 2,
    sealed_per_archetype: int = 4,
    public_base: int = 1000,
    private_seeds: dict[str, list[int]] | None = None,
) -> dict[str, dict[str, list[int]]]:
    """Seed plan for a suite. PUBLIC seeds are deterministic and committed
    here (base + index per archetype — anyone can regenerate the public
    set). SEALED seeds come from the owner's private registry; the null-world
    quota is enforced by construction in the archetype lists."""
    from wayfinder_paths.jobs.benchmarks.worlds import ARCHETYPES

    public = {
        archetype: [public_base + 100 * rank + i for i in range(public_per_archetype)]
        for rank, archetype in enumerate(ARCHETYPES)
    }
    # Null quota: double the null allocation (>=25% requirement lands via
    # archetype weighting at generation time).
    public["null_world"] = [
        public_base + 900 + i for i in range(public_per_archetype * 3)
    ]
    sealed = dict(private_seeds or {})
    return {"public": public, "sealed": sealed}

"""World calibration: does the generated world actually pose the problem its
archetype claims? Runs the oracle on a subset of hidden continuations and
asserts archetype-specific structural properties. A failed assertion rejects
the world (worlds.generate_world resamples the seed) — miscalibrated worlds
must never enter a suite, silently or otherwise."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.benchmarks.grammar import GENOME_SIGNALS, enumerate_genomes
from wayfinder_paths.jobs.benchmarks.oracle import evaluate_space, prepare_continuation

CALIBRATION_PATHS = 16
_WEIGHTS = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}
# A world "has an edge" when the oracle-null gap clears this floor; a null
# world must stay under the tighter null ceiling.
_EDGE_FLOOR = 0.010
_NULL_CEILING = 0.006

_DROP_BASINS = {"new_low_5", "new_low_20", "bb20_z_le_neg2", "rsi14_le_30"}
_BAIT_BASINS = {"new_high_20", "new_high_5"}


def calibrate_world(world: Any) -> dict[str, Any]:
    genomes = enumerate_genomes()
    continuations = [
        prepare_continuation(rows, signal_names=GENOME_SIGNALS)
        for rows in world.hidden_rows[:CALIBRATION_PATHS]
    ]
    oracle = evaluate_space(
        genomes, continuations, weights=_WEIGHTS, fee_bps=world.mechanism.fee_bps
    )
    gap = oracle["u_star"] - oracle["u_null"]
    best_signal, best_direction, best_filter = oracle["best_basin"].split("|")
    epsilon_basins = {
        oracle["basins"][gid] for gid in oracle["epsilon_optimal"]
    }

    def verdict(passed: bool, reason: str) -> dict[str, Any]:
        return {
            "passed": passed,
            "reason": reason,
            "u_star": oracle["u_star"],
            "gap": gap,
            "best_basin": oracle["best_basin"],
            "epsilon_basin_count": len(epsilon_basins),
            "calibration_paths": len(continuations),
        }

    archetype = world.archetype
    if archetype == "null_world":
        if gap > _NULL_CEILING:
            return verdict(False, f"null world has discoverable edge (gap {gap:.4f})")
        return verdict(True, "no discoverable edge, as designed")

    if gap < _EDGE_FLOOR:
        return verdict(False, f"edge not discoverable in grammar (gap {gap:.4f})")

    if archetype == "interaction_edge" and best_filter not in ("high_vol", "low_vol"):
        return verdict(False, f"best basin filter is {best_filter}, expected vol gate")
    if archetype == "session_edge" and not best_filter.startswith("session_"):
        return verdict(False, f"best basin filter is {best_filter}, expected session")
    if archetype == "deceptive_multi_peak" and best_signal in _BAIT_BASINS:
        return verdict(False, "global basin collapsed onto the bait basin")
    if archetype == "spike_vs_plateau" and not (
        best_signal in _DROP_BASINS and best_direction == "long"
    ):
        return verdict(False, f"plateau basin is not optimal (best {best_signal})")
    if archetype == "equivalent_optima":
        distinct_signals = {basin.split("|")[0] for basin in epsilon_basins}
        if len(distinct_signals) < 2:
            return verdict(False, "epsilon set spans only one structural region")
    if archetype == "disconnected_regions":
        # Two profitable pockets on OPPOSITE sides, separated by dead space:
        # both directions must clear half the edge floor.
        best_by_direction = {"long": 0.0, "short": 0.0}
        for gid, value in oracle["expected_utility"].items():
            direction = oracle["basins"][gid].split("|")[1]
            best_by_direction[direction] = max(best_by_direction[direction], value)
        if min(best_by_direction.values()) < _EDGE_FLOOR / 2:
            return verdict(
                False,
                f"one-sided world (long {best_by_direction['long']:.4f} / "
                f"short {best_by_direction['short']:.4f})",
            )
    return verdict(True, "archetype structure confirmed")

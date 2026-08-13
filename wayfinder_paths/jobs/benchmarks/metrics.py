"""Regret decomposition and run scoring against the oracle.

- search regret: U* − best oracle utility ANYWHERE in the lineage (it never
  found the region)
- selection regret: best-in-lineage − selected (it found it and picked wrong)
- execution regret is a production/prospective quantity — not measurable here
- abstention on a null world is CORRECT (selected=None scores U_null)
"""

from __future__ import annotations

from typing import Any


def score_run(
    run: dict[str, Any],
    oracle: dict[str, Any],
) -> dict[str, Any]:
    expected = oracle["expected_utility"]
    u_star = float(oracle["u_star"])
    u_null = float(oracle["u_null"])
    # Floor keeps normalized regret interpretable on null-ish worlds (the
    # reviewer's epsilon floor, same rationale); null worlds are judged by
    # false-promotion, not regret.
    denominator = max(u_star - u_null, 0.02)

    lineage_utilities: list[float] = []
    best_so_far: list[float] = []
    running = u_null
    for genome, _dev in run["lineage"]:
        value = float(expected.get(genome.genome_id, u_null))
        lineage_utilities.append(value)
        running = max(running, value)
        best_so_far.append(running)

    best_found = max(lineage_utilities, default=u_null)
    selected = run.get("selected")
    selected_utility = (
        float(expected.get(selected.genome_id, u_null))
        if selected is not None
        else u_null
    )

    search_regret = u_star - best_found
    selection_regret = best_found - selected_utility
    # Anytime regret: mean normalized regret over the budget curve — an
    # optimizer must not look good only at one convenient stopping point.
    anytime = (
        sum((u_star - value) / denominator for value in best_so_far)
        / len(best_so_far)
        if best_so_far
        else 1.0
    )
    epsilon = float(oracle["epsilon"])
    return {
        "optimizer": run.get("optimizer"),
        "evaluations": len(run["lineage"]),
        "selected_genome_id": selected.genome_id if selected is not None else None,
        "abstained": selected is None,
        "selected_utility": selected_utility,
        "best_found_utility": best_found,
        "u_star": u_star,
        "epsilon_hit_search": best_found >= u_star - epsilon,
        "epsilon_hit_selected": selected_utility >= u_star - epsilon,
        "search_regret": search_regret,
        "selection_regret": selection_regret,
        "search_regret_norm": search_regret / denominator,
        "selection_regret_norm": selection_regret / denominator,
        "anytime_regret_norm": anytime,
        "basin_found": oracle["basins"].get(
            max(
                (g for g, _ in run["lineage"]),
                key=lambda g: float(expected.get(g.genome_id, u_null)),
                default=None,
            ).genome_id
        )
        if run["lineage"]
        else None,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Suite-level aggregation per optimizer: hit rates, regret quantiles,
    null-world false-promotion."""

    def quantile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(int(q * (len(ordered) - 1)), len(ordered) - 1)]

    by_optimizer: dict[str, dict[str, Any]] = {}
    rows = [row for row in rows if "error" not in row]
    for row in rows:
        bucket = by_optimizer.setdefault(
            str(row["optimizer"]),
            {"runs": 0, "eps_search": 0, "eps_selected": 0, "regrets": [],
             "select_regrets": [], "null_runs": 0, "null_promotions": 0},
        )
        bucket["runs"] += 1
        bucket["eps_search"] += int(row["epsilon_hit_search"])
        bucket["eps_selected"] += int(row["epsilon_hit_selected"])
        bucket["regrets"].append(float(row["search_regret_norm"]))
        bucket["select_regrets"].append(float(row["selection_regret_norm"]))
        if row.get("is_null_world"):
            bucket["null_runs"] += 1
            bucket["null_promotions"] += int(not row["abstained"])
    report = {}
    for name, bucket in by_optimizer.items():
        runs = bucket["runs"]
        report[name] = {
            "runs": runs,
            "epsilon_hit_rate_search": bucket["eps_search"] / runs,
            "epsilon_hit_rate_selected": bucket["eps_selected"] / runs,
            "search_regret_median": quantile(bucket["regrets"], 0.5),
            "search_regret_p95": quantile(bucket["regrets"], 0.95),
            "selection_regret_median": quantile(bucket["select_regrets"], 0.5),
            "null_false_promotion_rate": (
                bucket["null_promotions"] / bucket["null_runs"]
                if bucket["null_runs"]
                else None
            ),
        }
    return report

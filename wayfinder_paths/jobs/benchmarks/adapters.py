"""Matched-budget optimizer lanes.

Every adapter sees ONLY development paths and a candidate-evaluation budget;
it returns its full evaluated lineage (every genome it scored, in order) and
its selected genome (None = abstain). The oracle never enters an adapter —
selection regret is measurable only because lineage is complete.

`funnel_search` is the production stack's shape without the LLM: propose on
a train path, hold out a second path, refuse candidates that fail holdout —
abstention on null worlds is exactly what the false-promotion metric scores.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

from wayfinder_paths.jobs.benchmarks.grammar import Genome
from wayfinder_paths.jobs.benchmarks.oracle import (
    ContinuationSeries,
    evaluate_genome,
    utility_from_trades,
)

_WEIGHTS = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}


def dev_score(
    genome: Genome, dev_series: Sequence[ContinuationSeries], *, fee_bps: float
) -> float:
    utilities = [
        utility_from_trades(
            evaluate_genome(genome, series, fee_bps=fee_bps)["pnls"],
            weights=_WEIGHTS,
        )
        for series in dev_series
    ]
    return sum(utilities) / len(utilities)


def random_search(
    genomes: Sequence[Genome],
    dev_series: Sequence[ContinuationSeries],
    *,
    budget: int,
    fee_bps: float,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    picks = rng.sample(list(genomes), min(budget, len(genomes)))
    lineage = [(g, dev_score(g, dev_series, fee_bps=fee_bps)) for g in picks]
    selected = max(lineage, key=lambda pair: pair[1])[0]
    return {"lineage": lineage, "selected": selected, "optimizer": "random"}


def grid_search(
    genomes: Sequence[Genome],
    dev_series: Sequence[ContinuationSeries],
    *,
    budget: int,
    fee_bps: float,
    seed: int = 0,
) -> dict[str, Any]:
    """Deterministic coarse lattice: stride the enumeration order so every
    structural region gets touched at any budget."""
    stride = max(1, len(genomes) // budget)
    picks = list(genomes)[::stride][:budget]
    lineage = [(g, dev_score(g, dev_series, fee_bps=fee_bps)) for g in picks]
    selected = max(lineage, key=lambda pair: pair[1])[0]
    return {"lineage": lineage, "selected": selected, "optimizer": "grid"}


def tpe_search(
    genomes: Sequence[Genome],
    dev_series: Sequence[ContinuationSeries],
    *,
    budget: int,
    fee_bps: float,
    seed: int,
) -> dict[str, Any]:
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    index: dict[tuple[str, str, str, int, str], Genome] = {}
    exits: list[tuple[str, tuple]] = []
    for genome in genomes:
        exit_key = (genome.exit_family, genome.exit_params)
        if exit_key not in exits:
            exits.append(exit_key)
        index[
            (
                genome.signal,
                genome.direction,
                genome.confirm_filter,
                exits.index(exit_key),
                genome.sizing_family,
            )
        ] = genome
    signals = sorted({g.signal for g in genomes})
    filters = sorted({g.confirm_filter for g in genomes})
    sizings = sorted({g.sizing_family for g in genomes})
    lineage: list[tuple[Genome, float]] = []

    def objective(trial: Any) -> float:
        key = (
            trial.suggest_categorical("signal", signals),
            trial.suggest_categorical("direction", ["long", "short"]),
            trial.suggest_categorical("filter", filters),
            trial.suggest_int("exit_index", 0, len(exits) - 1),
            trial.suggest_categorical("sizing", sizings),
        )
        genome = index.get(key)
        if genome is None:
            return -1.0
        score = dev_score(genome, dev_series, fee_bps=fee_bps)
        lineage.append((genome, score))
        return score

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(objective, n_trials=budget)
    if not lineage:
        return {"lineage": [], "selected": None, "optimizer": "tpe"}
    selected = max(lineage, key=lambda pair: pair[1])[0]
    return {"lineage": lineage, "selected": selected, "optimizer": "tpe"}


def funnel_search(
    genomes: Sequence[Genome],
    dev_series: Sequence[ContinuationSeries],
    *,
    budget: int,
    fee_bps: float,
    seed: int,
    holdout_top_k: int = 5,
) -> dict[str, Any]:
    """The production funnel's shape: search on the TRAIN path only, then an
    independent holdout adjudicates the top-k. No candidate clears on train
    evidence alone; nothing positive on holdout → ABSTAIN (the incumbent
    stays). This is the lane that must refuse null worlds."""
    if len(dev_series) < 2:
        raise ValueError("funnel_search requires train + holdout dev paths")
    train, holdout = dev_series[0], dev_series[1]
    proposal = tpe_search(
        genomes, [train], budget=budget, fee_bps=fee_bps, seed=seed
    )
    lineage = proposal["lineage"]
    ranked = sorted(lineage, key=lambda pair: pair[1], reverse=True)
    top = [genome for genome, score in ranked[:holdout_top_k] if score > 0]
    best_genome, best_holdout = None, 0.0
    holdout_scores: list[tuple[str, float]] = []
    for genome in top:
        score = dev_score(genome, [holdout], fee_bps=fee_bps)
        holdout_scores.append((genome.genome_id, score))
        if score > best_holdout:
            best_genome, best_holdout = genome, score
    return {
        "lineage": lineage,
        "selected": best_genome,  # None = abstain
        "optimizer": "funnel",
        "holdout_scores": holdout_scores,
    }


ADAPTERS = {
    "random": random_search,
    "grid": grid_search,
    "tpe": tpe_search,
    "funnel": funnel_search,
}

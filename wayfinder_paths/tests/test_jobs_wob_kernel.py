"""WOB kernel: grammar space, oracle-engine parity, regret metrics,
manifests/commitments, and optimizer-lane contracts."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from wayfinder_paths.jobs.benchmarks.adapters import (
    funnel_search,
    quality_diversity_funnel,
    random_search,
)
from wayfinder_paths.jobs.benchmarks.compiler import compile_genome, write_interpreter
from wayfinder_paths.jobs.benchmarks.grammar import (
    GENOME_SIGNALS,
    Genome,
    default_space_size,
    enumerate_genomes,
    grammar_hash,
)
from wayfinder_paths.jobs.benchmarks.manifest import (
    canonical_sha256,
    public_world_payload,
    world_manifest,
)
from wayfinder_paths.jobs.benchmarks.metrics import aggregate, score_run
from wayfinder_paths.jobs.benchmarks.oracle import (
    evaluate_genome,
    evaluate_space,
    prepare_continuation,
    utility_from_trades,
)
from wayfinder_paths.jobs.signal_library import SIGNAL_LIBRARY
from wayfinder_paths.jobs.synthetic.worlds import churn_world, reversion_world


def test_grammar_space_codec_and_hash_stability() -> None:
    genomes = enumerate_genomes()
    assert len(genomes) == default_space_size() == 7680
    library_names = {signal.name for signal in SIGNAL_LIBRARY}
    assert set(GENOME_SIGNALS) <= library_names
    sample = genomes[1234]
    assert Genome.from_dict(sample.to_dict()) == sample
    assert len({g.genome_id for g in genomes}) == len(genomes)
    # The declared-space hash is part of the bounded claim: stable across
    # calls, sensitive to any grammar change.
    assert grammar_hash() == grammar_hash()
    assert grammar_hash(signals=GENOME_SIGNALS[:6]) != grammar_hash()


def test_quality_diversity_lane_spends_matched_budget_across_cells() -> None:
    genomes = enumerate_genomes(signals=("new_low_5", "rsi14_ge_70"))
    dev = [
        prepare_continuation(
            reversion_world(bars=350, seed=seed),
            signal_names=("new_low_5", "rsi14_ge_70"),
        )
        for seed in (31, 32)
    ]
    run = quality_diversity_funnel(genomes, dev, budget=24, fee_bps=4.5, seed=7)
    control = funnel_search(genomes, dev, budget=24, fee_bps=4.5, seed=7)
    assert run["train_evaluations"] == control["train_evaluations"] == 19
    assert run["evaluation_count"] <= 24
    assert control["evaluation_count"] <= 24
    assert run["occupied_cells"] >= 6
    assert run["optimizer"] == "qd_funnel"


def test_oracle_engine_parity_on_shared_contract() -> None:
    """The parity contract that legitimizes oracle truth: same genome, same
    rows -> engine and oracle agree on net PnL within tolerance (exactly for
    close-referenced exits; small end-of-data differences for the rest)."""
    from wayfinder_paths.jobs.execution import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )

    rows = reversion_world(bars=600)
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    series = prepare_continuation(rows, signal_names=["new_low_5", "rsi14_ge_70"])
    dataset = PreparedExecutionDataset.from_rows(rows)
    samples = [
        Genome("rsi14_ge_70", "short", "session_c", "time_stop",
               (("hold_bars", 16), ("stop_pct", 0.01)), "fixed", ()),
        Genome("new_low_5", "long", "none", "fixed_time",
               (("hold_bars", 4),), "fixed", ()),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        script = write_interpreter(Path(tmp))
        for genome in samples:
            oracle_pnl = sum(
                evaluate_genome(genome, series, fee_bps=4.5)["pnls"]
            )
            result = simulate_execution(
                script, dataset, spec, compile_genome(genome, fee_bps=4.5)
            )
            engine_pnl = sum(
                float(row.get("realized_pnl_delta") or 0.0) for row in result.trades
            )
            tolerance = max(1.5, 0.06 * abs(oracle_pnl))
            assert abs(oracle_pnl - engine_pnl) <= tolerance, (
                f"{genome.signal}/{genome.exit_family}: oracle {oracle_pnl:.3f} "
                f"vs engine {engine_pnl:.3f}"
            )


def test_oracle_separates_edge_from_noise() -> None:
    # Reduced space (2 signals) keeps this a unit test, not a suite run.
    genomes = enumerate_genomes(signals=("new_low_5", "new_high_5"))
    edge_paths = [
        prepare_continuation(reversion_world(bars=500, seed=s),
                             signal_names=("new_low_5", "new_high_5"))
        for s in (1, 2, 3)
    ]
    noise_paths = [
        prepare_continuation(churn_world(bars=500, seed=s),
                             signal_names=("new_low_5", "new_high_5"))
        for s in (1, 2, 3)
    ]
    weights = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}
    edge = evaluate_space(genomes, edge_paths, weights=weights, fee_bps=4.5)
    noise = evaluate_space(genomes, noise_paths, weights=weights, fee_bps=4.5)
    assert edge["u_star"] - edge["u_null"] > 3 * (noise["u_star"] - noise["u_null"])
    assert edge["best_basin"].startswith("new_low_5|long")


def test_utility_ruin_floor_and_no_trades() -> None:
    weights = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}
    assert utility_from_trades([], weights=weights) == 0.0
    assert utility_from_trades([-20_000.0], weights=weights) == -10.0


def _fake_oracle() -> dict:
    return {
        "expected_utility": {"g1": 0.10, "g2": 0.05, "g3": -0.02},
        "u_star": 0.10,
        "u_null": 0.0,
        "epsilon": 0.02,
        "epsilon_optimal": ["g1"],
        "best_genome_id": "g1",
        "best_basin": "s|long|none",
        "basins": {"g1": "s|long|none", "g2": "t|long|none", "g3": "u|short|none"},
    }


class _StubGenome:
    def __init__(self, genome_id: str) -> None:
        self.genome_id = genome_id


def test_metrics_regret_decomposition_and_abstention() -> None:
    oracle = _fake_oracle()
    # Found the best (g1) but selected g2: zero search regret, positive
    # selection regret.
    run = {
        "optimizer": "x",
        "lineage": [(_StubGenome("g1"), 0.5), (_StubGenome("g2"), 0.9)],
        "selected": _StubGenome("g2"),
    }
    scored = score_run(run, oracle)
    assert scored["search_regret"] == pytest.approx(0.0)
    assert scored["selection_regret"] == pytest.approx(0.05)
    assert scored["epsilon_hit_search"] is True
    assert scored["epsilon_hit_selected"] is False

    # Abstention scores U_null and counts as NOT promoted for null worlds.
    abstain = {"optimizer": "x", "lineage": [(_StubGenome("g3"), -0.1)],
               "selected": None}
    scored2 = score_run(abstain, oracle)
    assert scored2["abstained"] is True
    assert scored2["selected_utility"] == 0.0

    rows = [
        {**scored, "is_null_world": False, "budget": 25},
        {**scored2, "is_null_world": True, "budget": 25},
    ]
    report = aggregate(rows)
    assert report["x"]["runs"] == 2
    assert report["x"]["null_false_promotion_rate"] == 0.0


def test_manifest_commitments_are_deterministic_and_sealed() -> None:
    from wayfinder_paths.jobs.benchmarks.worlds import Mechanism, World

    world = World(
        world_id="t-1", archetype="null_world", seed=1,
        mechanism=Mechanism(rules=[{"trigger": "drop3", "drift": 0.01}]),
        dev_rows=[[{"close": 1}]], hidden_rows=[[{"close": 2}]],
        calibration={"passed": True, "attempts": 1},
    )
    oracle = _fake_oracle()
    manifest_a = world_manifest(world, oracle)
    manifest_b = world_manifest(world, oracle)
    assert manifest_a["commitments"] == manifest_b["commitments"]
    text = str(manifest_a)
    # The manifest must not leak world internals.
    assert "drift" not in text and "drop3" not in text
    assert manifest_a["commitments"]["hidden_rows"] == canonical_sha256(
        world.hidden_rows
    )
    payload = public_world_payload(world, oracle)
    assert payload["oracle"]["best_genome_id"] == "g1"


def test_adapter_lineage_budget_and_funnel_contract() -> None:
    genomes = enumerate_genomes(signals=("new_low_5",))
    dev = [
        prepare_continuation(churn_world(bars=400, seed=s),
                             signal_names=("new_low_5",))
        for s in (5, 6)
    ]
    run = random_search(genomes, dev, budget=10, fee_bps=4.5, seed=1)
    assert len(run["lineage"]) == 10
    assert run["selected"] in [genome for genome, _ in run["lineage"]]
    with pytest.raises(ValueError, match="train"):
        funnel_search(genomes, dev[:1], budget=5, fee_bps=4.5, seed=1)

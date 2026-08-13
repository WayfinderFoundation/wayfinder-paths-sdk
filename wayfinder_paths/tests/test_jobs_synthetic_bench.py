"""Optimizer regression benchmark: the promotion funnel vs planted answers.

This is the standing substrate for improver changes — run before and after,
compare the vector. A regression here means the funnel started promoting
fakes or refusing real edges, which no live-market metric would reveal.
"""

from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs.synthetic.bench import run_benchmark
from wayfinder_paths.jobs.synthetic.worlds import (
    churn_world,
    regime_world,
    reversion_world,
)


def test_worlds_are_deterministic() -> None:
    assert reversion_world() == reversion_world()
    assert churn_world() == churn_world()
    assert regime_world() == regime_world()
    # Different seeds genuinely differ.
    assert reversion_world(seed=99) != reversion_world()


def test_promotion_funnel_beats_planted_worlds(tmp_path: Path) -> None:
    report = run_benchmark(tmp_path)
    # The development-region lucky pattern must NOT clear the gate...
    assert report["false_promotion"] is False, report["spike_reasons"]
    # ...while the genuine broad edge must — with working neighbors (a spike
    # has no plateau).
    assert report["basin_promotable"] is True, report["basin_reasons"]
    assert report["plateau_neighbors_positive"] >= 1
    # Zero-edge churn dies on fees and fold structure.
    assert report["churn_blocked"] is True, report["churn_reasons"]
    # Post-regime-shift, the right-way candidate clears the gate and the
    # stale incumbent's forward losses trip typed kill rules.
    assert report["regime_flip_accepted"] is True, report["regime_reasons"]
    assert report["stale_incumbent_killed"] is True
    assert report["pass"] is True

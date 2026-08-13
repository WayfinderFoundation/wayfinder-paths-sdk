"""Optimizer regression benchmark: run the REAL promotion funnel (grid ->
paired fold evaluation -> economic readiness -> typed kill rules) against
worlds with planted answers, and report the numbers that certify or damn it:

- basin_promotable: the genuine broad edge clears the economic gate
- false_promotion: the development-region lucky pattern must NOT clear it
- churn_blocked: zero-edge fee churn must NOT clear it
- regime_flip_accepted: post-shift, the right-way candidate clears the gate
- stale_incumbent_killed: typed kill rules fire on post-shift forward losses

Strictly off-box (2GB production box): this is a local/CI harness. It is
also the substrate for any future improver change — run before and after,
compare the vector.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.constitution import DEFAULT_CONSTITUTION
from wayfinder_paths.jobs.economics import (
    evaluate_economic_readiness,
    paired_fold_evaluation,
)
from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
)
from wayfinder_paths.jobs.predicates import evaluate_predicates, forward_metrics
from wayfinder_paths.jobs.synthetic.strategies import (
    CHURNER,
    DIP_BUYER,
    TREND_HOLDER,
)
from wayfinder_paths.jobs.synthetic.worlds import (
    churn_world,
    regime_world,
    reversion_world,
)


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _constitution() -> dict[str, Any]:
    constitution = json.loads(json.dumps(DEFAULT_CONSTITUTION))
    constitution["enforcement"] = "blocking"
    constitution["revision"] = "synthetic-bench"
    return constitution


def _write(workdir: Path, name: str, source: str) -> Path:
    path = workdir / name
    path.write_text(source.lstrip(), encoding="utf-8")
    return path


def _gate(
    *,
    baseline_script: Path,
    candidate_script: Path,
    dataset: PreparedExecutionDataset,
    baseline_params: dict[str, Any],
    candidate_params: dict[str, Any],
    constitution: dict[str, Any],
) -> dict[str, Any]:
    evaluation = paired_fold_evaluation(
        baseline_script=baseline_script,
        candidate_script=candidate_script,
        dataset=dataset,
        spec=_spec(),
        baseline_params=baseline_params,
        candidate_params=candidate_params,
        constitution=constitution,
    )
    readiness = evaluate_economic_readiness(evaluation, constitution)
    return {"evaluation": evaluation, "readiness": readiness}


def run_benchmark(workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    constitution = _constitution()
    dip = _write(workdir, "dip_buyer.py", DIP_BUYER)
    churner = _write(workdir, "churner.py", CHURNER)
    trend = _write(workdir, "trend.py", TREND_HOLDER)
    report: dict[str, Any] = {}

    # ── World 1: true plateau edge + development-region lucky hour ──────
    dataset = PreparedExecutionDataset.from_rows(reversion_world())
    # The deception exists: a full-history grid should rank the lucky hour
    # competitively (that is what a naive rank_by picks up).
    grid = run_execution_grid(
        dip,
        dataset,
        _spec(),
        [
            {"mode": "dip", "dip_pct": 1.2, "hold_bars": 2, "fee_bps": 4.5},
            {"mode": "lucky_hour", "entry_hour": 17, "hold_bars": 2, "fee_bps": 4.5},
        ],
        rank_by="net_return",
    )
    report["full_history_top_mode"] = (
        (grid.ranked[0]["params"] if grid.ranked else {}).get("mode")
    )

    spike = _gate(
        baseline_script=dip,
        candidate_script=dip,
        dataset=dataset,
        baseline_params={"mode": "dip", "dip_pct": 1.2, "hold_bars": 2, "fee_bps": 4.5},
        candidate_params={"mode": "lucky_hour", "entry_hour": 17, "hold_bars": 2, "fee_bps": 4.5},
        constitution=constitution,
    )
    report["false_promotion"] = bool(spike["readiness"]["ready"])
    report["spike_reasons"] = spike["readiness"]["reasons"]

    basin = _gate(
        baseline_script=dip,
        candidate_script=dip,
        dataset=dataset,
        # Incumbent that barely trades (threshold too deep) vs the real edge.
        baseline_params={"mode": "dip", "dip_pct": 4.5, "hold_bars": 2, "fee_bps": 4.5},
        candidate_params={"mode": "dip", "dip_pct": 1.2, "hold_bars": 2, "fee_bps": 4.5},
        constitution=constitution,
    )
    report["basin_promotable"] = bool(basin["readiness"]["ready"])
    report["basin_reasons"] = basin["readiness"]["reasons"]
    # Plateau breadth: neighbors of the winning cell must also beat the
    # do-little incumbent on point estimate (a spike has no neighbors).
    neighbor_estimates = []
    for threshold in (0.9, 1.5):
        neighbor = _gate(
            baseline_script=dip,
            candidate_script=dip,
            dataset=dataset,
            baseline_params={"mode": "dip", "dip_pct": 4.5, "hold_bars": 2, "fee_bps": 4.5},
            candidate_params={"mode": "dip", "dip_pct": threshold, "hold_bars": 2, "fee_bps": 4.5},
            constitution=constitution,
        )
        delta = neighbor["evaluation"].get("paired_incumbent_delta") or {}
        neighbor_estimates.append(float(delta.get("estimate") or 0.0))
    report["plateau_neighbors_positive"] = sum(
        1 for value in neighbor_estimates if value > 0
    )

    # ── World 2: zero-edge churn must not clear the gate ────────────────
    churn_gate = _gate(
        baseline_script=dip,
        candidate_script=churner,
        dataset=PreparedExecutionDataset.from_rows(churn_world()),
        baseline_params={"mode": "dip", "dip_pct": 4.5, "hold_bars": 2, "fee_bps": 4.5},
        candidate_params={"fee_bps": 4.5},
        constitution=constitution,
    )
    report["churn_blocked"] = not bool(churn_gate["readiness"]["ready"])
    report["churn_reasons"] = churn_gate["readiness"]["reasons"]

    # ── World 3: regime flip — adapt through the gate, kill the stale ───
    regime = PreparedExecutionDataset.from_rows(regime_world())
    flip_gate = _gate(
        baseline_script=trend,
        candidate_script=trend,
        dataset=regime,
        baseline_params={"direction": "long", "fee_bps": 4.5},
        candidate_params={"direction": "short", "fee_bps": 4.5},
        constitution={
            **constitution,
            # Buy-and-hold legs produce almost no closed trades; the trade
            # floor is a churn control, not meaningful for holders.
            "promotion": {**constitution["promotion"], "min_oos_trades": 0},
        },
    )
    report["regime_flip_accepted"] = bool(flip_gate["readiness"]["ready"])
    report["regime_reasons"] = flip_gate["readiness"]["reasons"]

    # Stale incumbent's post-shift forward losses trip typed kill rules.
    post_shift_trades = [
        {"symbol": "SYN", "ts": f"2026-07-{day:02d}T12:00:00+00:00", "net_pnl": pnl}
        for day, pnl in enumerate(
            [-0.8, -0.5, 0.3, -0.9, -0.6, -0.7, 0.2, -0.5, -0.8, -0.4, -0.6, -0.9],
            start=1,
        )
    ]
    kill = evaluate_predicates(
        {"min_closed_trades": 10, "net_pnl__lt": -3.0},
        forward_metrics(
            post_shift_trades,
            symbol="SYN",
            since="2026-07-01T00:00:00+00:00",
            now_iso="2026-07-14T00:00:00+00:00",
        ),
    )
    report["stale_incumbent_killed"] = kill["status"] == "met"

    report["pass"] = (
        not report["false_promotion"]
        and report["basin_promotable"]
        and report["plateau_neighbors_positive"] >= 1
        and report["churn_blocked"]
        and report["regime_flip_accepted"]
        and report["stale_incumbent_killed"]
    )
    return report


if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = run_benchmark(Path(tmp))
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["pass"] else 1)

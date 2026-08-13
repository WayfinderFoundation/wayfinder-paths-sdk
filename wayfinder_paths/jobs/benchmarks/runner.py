"""Suite runner: generate/calibrate worlds, compute oracles, run optimizer
lanes at matched budgets, score against truth, and emit artifacts.

Artifact layout under `out_dir`:
    worlds/<world_id>/manifest.json     — publishable (commitments only)
    worlds/<world_id>/public.json       — full payload, PUBLIC worlds only
    runs.jsonl                          — one scored row per (world, lane,
                                          budget, repeat)
    report.json                         — per-optimizer aggregation

Sealed payloads (data + answers matching the published commitments) go to
`sealed_dir` — an owner-held location, NEVER inside the repo or any
optimizer-visible path.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.benchmarks.grammar import GENOME_SIGNALS, enumerate_genomes
from wayfinder_paths.jobs.benchmarks.manifest import (
    public_world_payload,
    sealed_world_payload,
    world_manifest,
)
from wayfinder_paths.jobs.benchmarks.metrics import aggregate, score_run
from wayfinder_paths.jobs.benchmarks.oracle import evaluate_space, prepare_continuation

_WEIGHTS = {"downside": 0.5, "tail": 1.0, "turnover": 0.25}
ORACLE_PATHS = 64


def run_world_job(job: dict[str, Any]) -> dict[str, Any]:
    """One world end-to-end (worker-process safe: primitives in, dicts out)."""
    from wayfinder_paths.jobs.benchmarks.adapters import ADAPTERS
    from wayfinder_paths.jobs.benchmarks.worlds import generate_world

    world = generate_world(job["archetype"], job["seed"])
    genomes = enumerate_genomes()
    fee = world.mechanism.fee_bps
    hidden = [
        prepare_continuation(rows, signal_names=GENOME_SIGNALS)
        for rows in world.hidden_rows[: job.get("oracle_paths", ORACLE_PATHS)]
    ]
    oracle = evaluate_space(genomes, hidden, weights=_WEIGHTS, fee_bps=fee)
    dev = [
        prepare_continuation(rows, signal_names=GENOME_SIGNALS)
        for rows in world.dev_rows
    ]

    rows: list[dict[str, Any]] = []
    for lane in job["optimizers"]:
        adapter = ADAPTERS[lane]
        for budget in job["budgets"]:
            for repeat in range(job["repeats"]):
                run = adapter(
                    genomes, dev, budget=budget, fee_bps=fee,
                    seed=job["seed"] * 1000 + budget + repeat,
                )
                scored = score_run(run, oracle)
                scored.update(
                    {
                        "world_id": world.world_id,
                        "archetype": world.archetype,
                        "budget": budget,
                        "repeat": repeat,
                        "is_null_world": world.archetype == "null_world",
                        "visibility": job["visibility"],
                    }
                )
                rows.append(scored)

    return {
        "world_id": world.world_id,
        "archetype": world.archetype,
        "visibility": job["visibility"],
        "manifest": world_manifest(world, oracle),
        "public_payload": (
            public_world_payload(world, oracle)
            if job["visibility"] == "public"
            else None
        ),
        "sealed_payload": (
            sealed_world_payload(world, oracle)
            if job["visibility"] == "sealed"
            else None
        ),
        "rows": rows,
    }


def run_suite(
    seed_plan: dict[str, dict[str, list[int]]],
    *,
    out_dir: Path,
    sealed_dir: Path | None,
    optimizers: list[str],
    budgets: list[int],
    repeats: int,
    oracle_paths: int = ORACLE_PATHS,
    workers: int = 1,
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    for visibility, per_archetype in seed_plan.items():
        for archetype, seeds in per_archetype.items():
            for seed in seeds:
                jobs.append(
                    {
                        "archetype": archetype,
                        "seed": seed,
                        "visibility": visibility,
                        "optimizers": optimizers,
                        "budgets": budgets,
                        "repeats": repeats,
                        "oracle_paths": oracle_paths,
                    }
                )

    results: list[dict[str, Any]] = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(run_world_job, jobs):
                results.append(result)
                print(f"[wob] {result['world_id']} done", flush=True)
    else:
        for job in jobs:
            results.append(run_world_job(job))
            print(f"[wob] {results[-1]['world_id']} done", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    for result in results:
        world_dir = out_dir / "worlds" / result["world_id"]
        world_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / "manifest.json").write_text(
            json.dumps(result["manifest"], indent=2, sort_keys=True) + "\n"
        )
        if result["public_payload"] is not None:
            (world_dir / "public.json").write_text(
                json.dumps(result["public_payload"], sort_keys=True, default=str)
            )
        if result["sealed_payload"] is not None:
            if sealed_dir is None:
                raise ValueError(
                    "sealed worlds in plan but no sealed_dir provided"
                )
            sealed_dir.mkdir(parents=True, exist_ok=True)
            (sealed_dir / f"{result['world_id']}.json").write_text(
                json.dumps(result["sealed_payload"], sort_keys=True, default=str)
            )
        all_rows.extend(result["rows"])

    with (out_dir / "runs.jsonl").open("w") as handle:
        for row in all_rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    report = {
        "suite": "exact-v0",
        "worlds": len(results),
        "rows": len(all_rows),
        "by_optimizer": aggregate(all_rows),
        "by_budget": {
            str(budget): aggregate([r for r in all_rows if r["budget"] == budget])
            for budget in budgets
        },
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return report

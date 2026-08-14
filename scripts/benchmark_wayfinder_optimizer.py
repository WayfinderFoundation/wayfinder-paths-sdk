"""Wayfinder Optimizer Benchmark CLI.

Examples:
    # Fast smoke (CI shape): 1 seed x 3 archetypes, tiny budgets
    python scripts/benchmark_wayfinder_optimizer.py --suite smoke

    # Public half of exact-v0 (anyone can regenerate this)
    python scripts/benchmark_wayfinder_optimizer.py --suite exact-v0-public \
        --out .wayfinder_runs/benchmarks/exact-v0

    # Full exact-v0 incl. sealed worlds (requires WOB_PRIVATE_SEEDS)
    python scripts/benchmark_wayfinder_optimizer.py --suite exact-v0 \
        --out .wayfinder_runs/benchmarks/exact-v0 \
        --sealed-out ~/wob-sealed/exact-v0 --workers 6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.jobs.benchmarks.manifest import (  # noqa: E402
    load_private_seeds,
    suite_split,
)
from wayfinder_paths.jobs.benchmarks.runner import run_suite  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Wayfinder Optimizer Benchmark")
    parser.add_argument(
        "--suite",
        default="smoke",
        choices=["smoke", "exact-v0-public", "exact-v0", "stress"],
    )
    parser.add_argument("--out", default=".wayfinder_runs/benchmarks/latest")
    parser.add_argument("--sealed-out", default=None)
    parser.add_argument(
        "--optimizers", default="random,grid,tpe,funnel",
        help="comma-separated lane names",
    )
    parser.add_argument("--budgets", default="25,50,100")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--oracle-paths", type=int, default=64)
    args = parser.parse_args()

    if args.suite == "stress":
        from wayfinder_paths.jobs.benchmarks.stress import run_stress_suite

        report = run_stress_suite(Path(args.out))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["grade"] == "GOVERNANCE_VALID" else 1

    if args.suite == "smoke":
        plan = {
            "public": {
                "smooth_optimum": [1000],
                "deceptive_multi_peak": [1100],
                "null_world": [1900],
            }
        }
        budgets = [25]
        repeats = 1
        oracle_paths = min(args.oracle_paths, 16)
    else:
        private = load_private_seeds() if args.suite == "exact-v0" else {}
        plan = suite_split(private_seeds=private)
        if args.suite == "exact-v0-public":
            plan = {"public": plan["public"]}
        elif not plan["sealed"]:
            raise SystemExit(
                "exact-v0 requires sealed seeds: set WOB_PRIVATE_SEEDS to the "
                "owner-held registry (or run exact-v0-public)"
            )
        budgets = [int(b) for b in args.budgets.split(",")]
        repeats = args.repeats
        oracle_paths = args.oracle_paths

    report = run_suite(
        plan,
        out_dir=Path(args.out),
        sealed_dir=Path(args.sealed_out) if args.sealed_out else None,
        optimizers=[name.strip() for name in args.optimizers.split(",")],
        budgets=budgets,
        repeats=repeats,
        oracle_paths=oracle_paths,
        workers=args.workers,
    )
    print(json.dumps(report["by_optimizer"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI for frozen bundle races and full campaign A/B experiments."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.bench.env import atomic_json, git_sha, sha256_file
from wayfinder_paths.jobs.bench.forward_replay import race_bundles
from wayfinder_paths.jobs.bench.recurrence import run_recurrence
from wayfinder_paths.jobs.bench.runner import run_experiment
from wayfinder_paths.jobs.bench.world import prepare_world
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.gating import compute_workspace_revision


def main(argv: list[str] | None = None) -> None:
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    if resolved_argv[:2] == ["race", "rerun"]:
        resolved_argv[:2] = ["race-rerun"]
    parser = argparse.ArgumentParser(prog="wayfinder-bench")
    commands = parser.add_subparsers(dest="command", required=True)

    world = commands.add_parser("prepare-world")
    world.add_argument("source_job", type=Path)
    world.add_argument("--out", required=True, type=Path)
    world.add_argument("--sealed", required=True, type=Path)
    world.add_argument("--generation-cutoff", required=True)
    world.add_argument("--holdout-end", required=True)
    world.add_argument("--world-id")
    world.add_argument("--min-holdout-days", type=float, default=14)
    world.add_argument("--max-holdout-days", type=float, default=21)

    race = commands.add_parser("race")
    race.add_argument("a_bundle", type=Path)
    race.add_argument("b_bundle", type=Path, nargs="?")
    race.add_argument("--world", required=True, type=Path)
    race.add_argument("--sealed", required=True, type=Path)
    race.add_argument("--out", required=True, type=Path)

    rerun = commands.add_parser("race-rerun")
    rerun.add_argument("race_dir", type=Path)

    experiment = commands.add_parser("run")
    experiment.add_argument("config", type=Path)

    recurrence = commands.add_parser("recur")
    recurrence.add_argument("config", type=Path)

    args = parser.parse_args(resolved_argv)
    if args.command == "prepare-world":
        result = prepare_world(
            args.source_job,
            args.out,
            generation_cutoff=_parse(args.generation_cutoff),
            holdout_end=_parse(args.holdout_end),
            sealed_dir=args.sealed,
            world_id=args.world_id,
            min_holdout_days=args.min_holdout_days,
            max_holdout_days=args.max_holdout_days,
        )
    elif args.command == "race":
        result = _run_race(
            args.a_bundle,
            args.b_bundle,
            world=args.world,
            sealed=args.sealed,
            output=args.out,
        )
    elif args.command == "race-rerun":
        result = _rerun_race(args.race_dir)
    elif args.command == "recur":
        result = run_recurrence(args.config)
    else:
        result = run_experiment(args.config)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _run_race(
    a_bundle: Path,
    b_bundle: Path | None,
    *,
    world: Path,
    sealed: Path,
    output: Path,
) -> dict[str, Any]:
    world = world.resolve()
    sealed = sealed.resolve()
    b_source = b_bundle or (world / "incumbent")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    copy_job_bundle(a_bundle.resolve(), output / "a")
    copy_job_bundle(b_source.resolve(), output / "b")
    manifest = {
        "schema_version": "1.0",
        "world": str(world),
        "sealed": str(sealed),
        "a_revision": compute_workspace_revision(output / "a"),
        "b_revision": compute_workspace_revision(output / "b"),
        "world_sha256": sha256_file(world / "world.json"),
        "sdk_ref": git_sha(Path(__file__).resolve().parents[3]),
        "rules": {
            "paired_lcb_confidence": 0.90,
            "participation_floor": 10,
            "max_drawdown_ratio": 1.25,
            "bootstrap_seed": 7,
        },
    }
    # Pre-register the complete race identity and verdict before simulating.
    atomic_json(output / "race.json", manifest)
    return race_bundles(
        output / "a",
        output / "b",
        world_dir=world,
        sealed_dir=sealed,
        output_dir=output,
    )


def _rerun_race(race_dir: Path) -> dict[str, Any]:
    race_dir = race_dir.resolve()
    manifest = json.loads((race_dir / "race.json").read_text(encoding="utf-8"))
    if compute_workspace_revision(race_dir / "a") != manifest["a_revision"]:
        raise ValueError("race A bundle changed after registration")
    if compute_workspace_revision(race_dir / "b") != manifest["b_revision"]:
        raise ValueError("race B bundle changed after registration")
    if sha256_file(Path(manifest["world"]) / "world.json") != manifest["world_sha256"]:
        raise ValueError("race world changed after registration")
    if git_sha(Path(__file__).resolve().parents[3]) != manifest["sdk_ref"]:
        raise ValueError("race SDK changed after registration")
    return race_bundles(
        race_dir / "a",
        race_dir / "b",
        world_dir=Path(manifest["world"]),
        sealed_dir=Path(manifest["sealed"]),
        output_dir=race_dir,
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    main()
